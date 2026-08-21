import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { requestCaptureStream } from "./audioCapture";
import { CaptureAdapter } from "./captureAdapter";
import { SessionClient, type ClientConnectionState } from "./sessionClient";
import type {
  AnswerRecord,
  AnswerStatus,
  AnswerStore,
  ChannelState,
  DeviceStatus,
  InterviewSession,
  ServerEvent,
  SessionPhase,
  Speaker,
  TranscriptState,
} from "./types";

const IS_CAPTURE_HOST = window.interviewDesktop?.captureHost === true;
const API_BASE_URL = (
  window.interviewDesktop?.apiBaseUrl ||
  import.meta.env.VITE_API_BASE_URL ||
  (IS_CAPTURE_HOST ? "https://interview.reachard.co" : window.location.origin)
).replace(/\/+$/, "");
const CURRENT_POLL_MS = 5_000;

const EMPTY_ANSWERS: AnswerStore = { order: [], byId: {} };
const INITIAL_CHANNELS: Record<Speaker, ChannelState> = {
  interviewer: { phase: "idle", message: "采集设备离线" },
  candidate: { phase: "idle", message: "采集设备离线" },
};
const INITIAL_TRANSCRIPTS: Record<Speaker, TranscriptState> = {
  interviewer: { final: "", partial: "" },
  candidate: { final: "", partial: "" },
};

interface CurrentInterviewResponse extends InterviewSession {
  expires_at?: string;
  device_status?: {
    status?: string;
    channels?: Partial<Record<Speaker, boolean>>;
  };
  interview_state?: { active?: boolean };
}

export default function App() {
  const [sessionPhase, setSessionPhase] = useState<SessionPhase>("idle");
  const [connectionState, setConnectionState] =
    useState<ClientConnectionState>("disconnected");
  const [deviceStatus, setDeviceStatus] = useState<DeviceStatus>(
    IS_CAPTURE_HOST ? "initializing" : "offline",
  );
  const [channels, setChannels] = useState<Record<Speaker, ChannelState>>(INITIAL_CHANNELS);
  const [transcripts, setTranscripts] =
    useState<Record<Speaker, TranscriptState>>(INITIAL_TRANSCRIPTS);
  const [answers, setAnswers] = useState<AnswerStore>(EMPTY_ANSWERS);
  const [interviewActive, setInterviewActive] = useState(false);
  const [manualText, setManualText] = useState("");
  const [screenshotBusy, setScreenshotBusy] = useState(false);
  const [authRequired, setAuthRequired] = useState(false);
  const [accessToken, setAccessToken] = useState("");
  const [authBusy, setAuthBusy] = useState(false);
  const [initializationFailed, setInitializationFailed] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const sessionRef = useRef<InterviewSession | null>(null);
  const displayedSessionIdRef = useRef<string | null>(null);
  const sessionClientRef = useRef<SessionClient | null>(null);
  const captureAdapterRef = useRef<CaptureAdapter | null>(null);
  const initializationInFlightRef = useRef(false);
  const hostEnsurePromiseRef = useRef<Promise<void> | null>(null);
  const browserRequestInFlightRef = useRef(false);
  const pollTimerRef = useRef<number | undefined>(undefined);
  const disposedRef = useRef(false);
  const operationRef = useRef(0);
  const activeRef = useRef(false);
  const timelineEndRef = useRef<HTMLDivElement | null>(null);

  const answerList = useMemo(
    () => answers.order.map((responseId) => answers.byId[responseId]).filter(Boolean),
    [answers],
  );
  const clientReady = connectionState === "connected";

  useEffect(() => {
    activeRef.current = interviewActive;
  }, [interviewActive]);

  useEffect(() => {
    timelineEndRef.current?.scrollIntoView({ block: "end" });
  }, [answers.order.length]);

  useEffect(() => {
    disposedRef.current = false;
    const handleCaptureInitialization = () => {
      if (captureAdapterRef.current) {
        void ensureHostSession();
        return;
      }
      if (initializationInFlightRef.current) {
        return;
      }

      initializationInFlightRef.current = true;
      setInitializationFailed(false);
      setDeviceStatus("initializing");
      setError(null);
      setChannel("candidate", "connecting", "初始化麦克风");
      setChannel("interviewer", "connecting", "初始化系统音频");

      // Both permission requests must be created in the same user-gesture task.
      const candidatePromise = requestCaptureStream("candidate");
      const interviewerPromise = requestCaptureStream("interviewer");
      void finishCaptureInitialization(candidatePromise, interviewerPromise);
    };

    if (IS_CAPTURE_HOST) {
      window.addEventListener("sage:capture-initialize", handleCaptureInitialization);
    }

    const bootstrapTimer = window.setTimeout(() => {
      if (IS_CAPTURE_HOST) {
        void requestCaptureInitialization().catch((bootstrapError) => {
          reportCaptureInitializationFailure(bootstrapError);
        });
      } else {
        void loadBrowserCurrentInterview();
      }
    }, 0);

    return () => {
      disposedRef.current = true;
      operationRef.current += 1;
      window.clearTimeout(bootstrapTimer);
      if (IS_CAPTURE_HOST) {
        window.removeEventListener("sage:capture-initialize", handleCaptureInitialization);
      }
      clearPollTimer();
      sessionClientRef.current?.stop();
      sessionClientRef.current = null;
      captureAdapterRef.current?.dispose();
      captureAdapterRef.current = null;
    };
  }, []);

  async function finishCaptureInitialization(
    candidatePromise: Promise<MediaStream>,
    interviewerPromise: Promise<MediaStream>,
  ) {
    let candidateStream: MediaStream | null = null;
    let interviewerStream: MediaStream | null = null;
    try {
      const [candidateResult, interviewerResult] = await Promise.all([
        settleMediaRequest(candidatePromise),
        settleMediaRequest(interviewerPromise),
      ]);
      if (!candidateResult.ok || !interviewerResult.ok) {
        if (candidateResult.ok) {
          candidateResult.stream.getTracks().forEach((track) => track.stop());
        }
        if (interviewerResult.ok) {
          interviewerResult.stream.getTracks().forEach((track) => track.stop());
        }
        throw (!candidateResult.ok ? candidateResult.error : interviewerResult.error);
      }
      candidateStream = candidateResult.stream;
      interviewerStream = interviewerResult.stream;
      if (disposedRef.current) {
        candidateStream.getTracks().forEach((track) => track.stop());
        interviewerStream.getTracks().forEach((track) => track.stop());
        return;
      }

      let adapter: CaptureAdapter;
      adapter = new CaptureAdapter(
        API_BASE_URL,
        { candidate: candidateStream, interviewer: interviewerStream },
        {
          onChannelChange: (speaker, state) => {
            if (captureAdapterRef.current === adapter) {
              setChannels((current) => ({ ...current, [speaker]: state }));
            }
          },
          onError: (message) => setError(message),
          onMediaEnded: (speaker) => handleMediaEnded(adapter, speaker),
          onSessionEnded: () => handleSessionEnded(),
        },
      );
      captureAdapterRef.current?.dispose();
      captureAdapterRef.current = adapter;
      candidateStream = null;
      interviewerStream = null;
      await ensureHostSession();
    } catch (initializationError) {
      candidateStream?.getTracks().forEach((track) => track.stop());
      interviewerStream?.getTracks().forEach((track) => track.stop());
      if (!disposedRef.current) {
        setInitializationFailed(true);
        setDeviceStatus("error");
        setError(errorMessage(initializationError, "采集设备初始化失败。"));
        setChannels({
          interviewer: { phase: "error", message: "初始化失败" },
          candidate: { phase: "error", message: "初始化失败" },
        });
      }
    } finally {
      initializationInFlightRef.current = false;
    }
  }

  async function ensureHostSession() {
    if (!IS_CAPTURE_HOST || !captureAdapterRef.current || disposedRef.current) {
      return;
    }
    if (hostEnsurePromiseRef.current) {
      return hostEnsurePromiseRef.current;
    }

    const operation = operationRef.current + 1;
    operationRef.current = operation;
    const promise = (async () => {
      setSessionPhase("starting");
      setDeviceStatus("initializing");
      setInitializationFailed(false);
      try {
        const session = await createInterviewSession();
        if (disposedRef.current || operationRef.current !== operation) {
          return;
        }
        prepareForSession(session);
        const adapter = captureAdapterRef.current;
        if (!adapter) {
          throw new Error("采集设备尚未初始化。");
        }
        await Promise.all([connectSessionClient(session), adapter.connect(session)]);
        if (operationRef.current === operation) {
          setError(null);
        }
      } catch (sessionError) {
        if (!disposedRef.current && operationRef.current === operation) {
          sessionClientRef.current?.stop();
          sessionClientRef.current = null;
          captureAdapterRef.current?.disconnectSession();
          setSessionPhase("idle");
          setDeviceStatus("error");
          setInitializationFailed(true);
          setError(errorMessage(sessionError, "连接采集会话失败。"));
        }
      }
    })();
    hostEnsurePromiseRef.current = promise;
    try {
      await promise;
    } finally {
      if (hostEnsurePromiseRef.current === promise) {
        hostEnsurePromiseRef.current = null;
      }
    }
  }

  async function loadBrowserCurrentInterview() {
    if (IS_CAPTURE_HOST || disposedRef.current || browserRequestInFlightRef.current) {
      return;
    }
    browserRequestInFlightRef.current = true;
    clearPollTimer();
    try {
      const response = await fetch(`${API_BASE_URL}/api/interviews/current`, {
        method: "GET",
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (response.status === 401) {
        setAuthRequired(true);
        setDeviceOffline("需要访问密钥");
        return;
      }
      setAuthRequired(false);
      if (response.status === 204) {
        if (sessionRef.current) {
          handleSessionEnded();
        } else {
          setDeviceOffline("采集设备离线");
          scheduleCurrentPoll();
        }
        return;
      }
      if (!response.ok) {
        throw new Error(await readResponseError(response));
      }

      const current = (await response.json()) as CurrentInterviewResponse;
      const session = parseInterviewSession(current, false);
      applyDeviceStatus(current.device_status?.status, current.device_status?.channels);
      applyInterviewState(Boolean(current.interview_state?.active));

      if (
        sessionRef.current?.interview_id === session.interview_id &&
        sessionClientRef.current?.isReady()
      ) {
        return;
      }
      prepareForSession(session);
      await connectSessionClient(session);
      setError(null);
    } catch (currentError) {
      if (!disposedRef.current) {
        setError(errorMessage(currentError, "获取当前面试失败。"));
        setDeviceOffline("采集设备离线");
        scheduleCurrentPoll();
      }
    } finally {
      browserRequestInFlightRef.current = false;
    }
  }

  function prepareForSession(session: InterviewSession) {
    if (displayedSessionIdRef.current !== session.interview_id) {
      displayedSessionIdRef.current = session.interview_id;
      setAnswers(EMPTY_ANSWERS);
      setTranscripts(INITIAL_TRANSCRIPTS);
    }
    sessionRef.current = session;
  }

  async function connectSessionClient(session: InterviewSession) {
    sessionClientRef.current?.stop();
    let client: SessionClient;
    client = new SessionClient(API_BASE_URL, session, {
      onConnectionChange: (state) => {
        if (sessionClientRef.current === client) {
          setConnectionState(state);
        }
      },
      onEvent: (event) => {
        if (sessionClientRef.current === client) {
          handleServerEvent(event);
        }
      },
      onError: (message) => {
        if (sessionClientRef.current === client) {
          setError(message);
        }
      },
      onSessionUnavailable: () => {
        if (sessionClientRef.current === client) {
          handleSessionEnded();
        }
      },
    });
    sessionClientRef.current = client;
    await client.start();
  }

  async function submitBrowserLogin(event: FormEvent) {
    event.preventDefault();
    const token = accessToken.trim();
    if (!token || authBusy) {
      return;
    }
    setAuthBusy(true);
    setError(null);
    try {
      const response = await fetch(`${API_BASE_URL}/api/browser/login`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ access_token: token }),
      });
      if (!response.ok) {
        throw new Error(await readResponseError(response));
      }
      setAccessToken("");
      setAuthRequired(false);
      await loadBrowserCurrentInterview();
    } catch (loginError) {
      setError(errorMessage(loginError, "访问密钥验证失败。"));
    } finally {
      setAuthBusy(false);
    }
  }

  async function startInterview() {
    if (IS_CAPTURE_HOST && initializationFailed) {
      await retryCaptureInitialization();
      return;
    }
    if (!clientReady || deviceStatus !== "ready" || sessionPhase === "starting") {
      return;
    }
    setError(null);
    setSessionPhase("starting");
    if (!sessionClientRef.current?.send({ type: "start_interview" })) {
      setSessionPhase("idle");
      setError("会话同步正在重连，请稍后再试。");
    }
  }

  async function stopInterview() {
    const session = sessionRef.current;
    if (!session || sessionPhase === "stopping") {
      return;
    }
    setError(null);
    setSessionPhase("stopping");
    try {
      await endInterviewSession(session);
      handleSessionEnded();
    } catch (stopError) {
      setSessionPhase(interviewActive ? "live" : "idle");
      setError(errorMessage(stopError, "结束面试失败。"));
    }
  }

  function submitManualQuestion(event: FormEvent) {
    event.preventDefault();
    const text = manualText.trim();
    if (!text || !interviewActive) {
      return;
    }
    if (!sessionClientRef.current?.send({ type: "manual_text", text })) {
      setError("会话同步正在重连，请稍后再发。");
      return;
    }
    setManualText("");
    setError(null);
  }

  function captureAndAnswer() {
    if (!interviewActive || screenshotBusy) {
      return;
    }
    if (!sessionClientRef.current?.send({ type: "request_screen_capture" })) {
      setError("会话同步正在重连，请稍后再试。");
      return;
    }
    setScreenshotBusy(true);
    setError(null);
    window.setTimeout(() => setScreenshotBusy(false), 800);
  }

  async function retryCaptureInitialization() {
    setInitializationFailed(false);
    setError(null);
    try {
      if (captureAdapterRef.current) {
        await ensureHostSession();
      } else {
        await requestCaptureInitialization();
      }
    } catch (retryError) {
      reportCaptureInitializationFailure(retryError);
    }
  }

  function reportCaptureInitializationFailure(initializationError: unknown) {
    if (disposedRef.current) {
      return;
    }
    setInitializationFailed(true);
    setDeviceStatus("error");
    setChannels({
      interviewer: { phase: "error", message: "初始化失败" },
      candidate: { phase: "error", message: "初始化失败" },
    });
    setError(errorMessage(initializationError, "采集设备初始化失败，请检查系统设置。"));
  }

  function handleServerEvent(payload: ServerEvent) {
    switch (payload.type) {
      case "device_status":
        applyDeviceStatus(payload.status, payload.channels);
        return;
      case "interview_state":
        applyInterviewState(Boolean(payload.active));
        return;
      case "transcript_snapshot": {
        const next: Record<Speaker, TranscriptState> = {
          interviewer: { final: "", partial: "" },
          candidate: { final: "", partial: "" },
        };
        payload.turns?.forEach((turn) => {
          if ((turn.speaker === "interviewer" || turn.speaker === "candidate") && turn.text) {
            next[turn.speaker] = { final: turn.text, partial: "" };
          }
        });
        setTranscripts(next);
        return;
      }
      case "transcript_delta": {
        const speaker = parseSpeaker(payload.speaker);
        const delta = payload.delta ?? payload.text ?? "";
        if (!speaker || !delta) {
          return;
        }
        setTranscripts((current) => ({
          ...current,
          [speaker]: {
            ...current[speaker],
            partial: `${current[speaker].partial}${delta}`,
          },
        }));
        return;
      }
      case "transcript_final": {
        const speaker = parseSpeaker(payload.speaker);
        if (!speaker) {
          return;
        }
        setTranscripts((current) => ({
          ...current,
          [speaker]: {
            final: payload.text ?? payload.delta ?? current[speaker].partial,
            partial: "",
          },
        }));
        return;
      }
      case "answer_started":
        updateAnswerFromEvent(payload, "streaming", false);
        return;
      case "answer_delta": {
        if (!payload.response_id) {
          return;
        }
        updateAnswer(payload.response_id, (current) =>
          isTerminalAnswer(current)
            ? current
            : {
                ...current,
                text: `${current.text}${payload.delta ?? payload.text ?? ""}`,
                status: "streaming",
              },
        );
        return;
      }
      case "answer_snapshot":
        updateAnswerFromEvent(payload, parseAnswerStatus(payload.status), true);
        return;
      case "answer_completed":
        updateAnswerFromEvent(payload, "completed", false);
        return;
      case "answer_interrupted":
        updateAnswerFromEvent(payload, "interrupted", false);
        return;
      case "answer_error":
        updateAnswerFromEvent(payload, "error", false);
        return;
      case "session_ended":
        handleSessionEnded();
        return;
      case "error": {
        const detail = payload.detail ?? payload.error ?? payload.message ?? "实时会话发生错误。";
        if (payload.response_id) {
          updateAnswerFromEvent({ ...payload, detail }, "error", false);
        } else {
          setSessionPhase(activeRef.current ? "live" : "idle");
          setError(detail);
        }
        return;
      }
      default:
        return;
    }
  }

  function updateAnswerFromEvent(
    payload: ServerEvent,
    status: AnswerStatus,
    replaceText: boolean,
  ) {
    if (!payload.response_id) {
      return;
    }
    updateAnswer(payload.response_id, (current) => {
      if (!replaceText && isTerminalAnswer(current)) {
        return current;
      }
      return {
        ...current,
        question: payload.question ?? current.question,
        text: replaceText ? (payload.text ?? "") : (payload.text ?? current.text),
        status,
        createdAt: payload.created_at ?? current.createdAt,
        detail: payload.detail ?? payload.error ?? payload.message,
      };
    });
  }

  function updateAnswer(responseId: string, update: (current: AnswerRecord) => AnswerRecord) {
    setAnswers((current) => {
      const existing = current.byId[responseId];
      const base: AnswerRecord =
        existing ?? {
          responseId,
          text: "",
          status: "streaming",
          createdAt: new Date().toISOString(),
        };
      const next = update(base);
      if (existing && next === existing) {
        return current;
      }
      return {
        order: existing ? current.order : [...current.order, responseId],
        byId: { ...current.byId, [responseId]: next },
      };
    });
  }

  function applyDeviceStatus(
    rawStatus: string | undefined,
    channelReady: Partial<Record<Speaker, boolean>> | undefined,
  ) {
    if (IS_CAPTURE_HOST && !captureAdapterRef.current) {
      if (!initializationInFlightRef.current) {
        setDeviceStatus("error");
        setInitializationFailed(true);
      }
      return;
    }
    const status: DeviceStatus =
      rawStatus === "initializing" || rawStatus === "ready" ? rawStatus : "offline";
    setDeviceStatus(status);
    setInitializationFailed(false);
    setChannels((current) => {
      const next = { ...current };
      (["interviewer", "candidate"] as Speaker[]).forEach((speaker) => {
        const ready = channelReady?.[speaker] === true;
        next[speaker] = ready
          ? { phase: "listening", message: activeRef.current ? "采集中" : "已就绪" }
          : status === "initializing"
            ? { phase: "connecting", message: "初始化中" }
            : { phase: "idle", message: "采集设备离线" };
      });
      return next;
    });
  }

  function applyInterviewState(active: boolean) {
    activeRef.current = active;
    setInterviewActive(active);
    setSessionPhase(active ? "live" : "idle");
    setChannels((current) => ({
      interviewer:
        current.interviewer.phase === "listening"
          ? { ...current.interviewer, message: active ? "采集中" : "已就绪" }
          : current.interviewer,
      candidate:
        current.candidate.phase === "listening"
          ? { ...current.candidate, message: active ? "采集中" : "已就绪" }
          : current.candidate,
    }));
  }

  function handleSessionEnded() {
    if (!sessionRef.current) {
      return;
    }
    operationRef.current += 1;
    hostEnsurePromiseRef.current = null;
    sessionRef.current = null;
    sessionClientRef.current?.stop();
    sessionClientRef.current = null;
    captureAdapterRef.current?.disconnectSession();
    markStreamingAnswersInterrupted("本场面试已结束，已保留生成到这里的内容。");
    activeRef.current = false;
    setInterviewActive(false);
    setSessionPhase("idle");
    setConnectionState("disconnected");
    setDeviceStatus(IS_CAPTURE_HOST && captureAdapterRef.current ? "initializing" : "offline");
    if (IS_CAPTURE_HOST && captureAdapterRef.current) {
      window.setTimeout(() => void ensureHostSession(), 500);
    } else if (!IS_CAPTURE_HOST) {
      scheduleCurrentPoll(500);
    }
  }

  function handleMediaEnded(adapter: CaptureAdapter, speaker: Speaker) {
    if (captureAdapterRef.current !== adapter) {
      return;
    }
    adapter.dispose();
    captureAdapterRef.current = null;
    setInitializationFailed(true);
    setDeviceStatus("error");
    setChannel(speaker, "error", "采集已停止");
    setError(`${speaker === "candidate" ? "麦克风" : "系统音频"}采集已停止，请重新初始化。`);
  }

  function setDeviceOffline(message: string) {
    setDeviceStatus("offline");
    setInterviewActive(false);
    activeRef.current = false;
    setSessionPhase("idle");
    setChannels({
      interviewer: { phase: "idle", message },
      candidate: { phase: "idle", message },
    });
  }

  function scheduleCurrentPoll(delay = CURRENT_POLL_MS) {
    if (IS_CAPTURE_HOST || disposedRef.current || authRequired) {
      return;
    }
    clearPollTimer();
    pollTimerRef.current = window.setTimeout(() => {
      pollTimerRef.current = undefined;
      void loadBrowserCurrentInterview();
    }, delay);
  }

  function clearPollTimer() {
    if (pollTimerRef.current !== undefined) {
      window.clearTimeout(pollTimerRef.current);
      pollTimerRef.current = undefined;
    }
  }

  function setChannel(speaker: Speaker, phase: ChannelState["phase"], message: string) {
    setChannels((current) => ({ ...current, [speaker]: { phase, message } }));
  }

  function markStreamingAnswersInterrupted(detail: string) {
    setAnswers((current) => {
      let changed = false;
      const byId = { ...current.byId };
      current.order.forEach((responseId) => {
        const answer = byId[responseId];
        if (answer?.status === "streaming") {
          changed = true;
          byId[responseId] = { ...answer, status: "interrupted", detail };
        }
      });
      return changed ? { order: current.order, byId } : current;
    });
  }

  const startDisabled =
    !initializationFailed &&
    (!clientReady || deviceStatus !== "ready" || sessionPhase === "starting" || authRequired);
  const startLabel = initializationFailed
    ? "重试初始化"
    : sessionPhase === "starting"
      ? "启动中…"
      : deviceStatus === "offline"
        ? "等待采集设备"
        : deviceStatus === "initializing"
          ? "初始化中…"
          : "开始";

  return (
    <div className="app-shell">
      <header className="app-header">
        <div>
          <p className="eyebrow">SAGE · 模拟面试</p>
          <h1>听问题，给答案。</h1>
          <p className={`device-summary ${deviceStatus}`}>
            采集设备：{deviceStatusLabel(deviceStatus, interviewActive)} · {connectionLabel(connectionState)}
          </p>
        </div>
        <div className="session-actions">
          <button
            type="button"
            className="button secondary"
            onClick={captureAndAnswer}
            disabled={!interviewActive || !clientReady || screenshotBusy}
          >
            {screenshotBusy ? "已请求截图…" : "截图并回答"}
          </button>
          {interviewActive || sessionPhase === "stopping" ? (
            <button
              type="button"
              className="button danger"
              onClick={() => void stopInterview()}
              disabled={sessionPhase === "stopping"}
            >
              {sessionPhase === "stopping" ? "结束中…" : "停止"}
            </button>
          ) : (
            <button
              type="button"
              className="button primary"
              onClick={() => void startInterview()}
              disabled={startDisabled}
            >
              {startLabel}
            </button>
          )}
        </div>
      </header>

      {authRequired ? (
        <form className="access-form" onSubmit={(event) => void submitBrowserLogin(event)}>
          <label htmlFor="access-token">访问密钥</label>
          <input
            id="access-token"
            type="password"
            autoComplete="current-password"
            value={accessToken}
            onChange={(event) => setAccessToken(event.target.value)}
            placeholder="输入后仅用于建立安全会话"
            disabled={authBusy}
          />
          <button className="button primary" type="submit" disabled={!accessToken.trim() || authBusy}>
            {authBusy ? "验证中…" : "进入"}
          </button>
        </form>
      ) : null}

      {error ? <div className="error-banner">{error}</div> : null}

      <main className="workspace">
        <section className="left-column" aria-label="实时面试">
          <div className="channel-grid">
            <ChannelCard
              speaker="interviewer"
              state={channels.interviewer}
              transcript={transcripts.interviewer}
            />
            <ChannelCard
              speaker="candidate"
              state={channels.candidate}
              transcript={transcripts.candidate}
            />
          </div>

          <form className="manual-form" onSubmit={submitManualQuestion}>
            <label htmlFor="manual-question">手动问题</label>
            <div className="manual-input-row">
              <input
                id="manual-question"
                value={manualText}
                onChange={(event) => setManualText(event.target.value)}
                placeholder={interviewActive ? "输入要回答的问题" : "面试开始后可发送"}
                disabled={!interviewActive || !clientReady}
              />
              <button
                type="submit"
                className="button primary"
                disabled={!interviewActive || !clientReady || !manualText.trim()}
              >
                发送
              </button>
            </div>
          </form>
        </section>

        <section className="timeline-panel" aria-label="答案时间线">
          <div className="timeline-head">
            <div>
              <p className="eyebrow">答案时间线</p>
              <h2>{answerList.length ? `${answerList.length} 条回答` : "等待第一条回答"}</h2>
            </div>
            <span className="privacy-note">当前面试实时同步</span>
          </div>

          <div className="timeline" aria-live="polite">
            {answerList.length ? (
              answerList.map((answer) => <AnswerCard key={answer.responseId} answer={answer} />)
            ) : (
              <div className="empty-state">
                <span>01</span>
                <p>开始面试后，回答会按生成顺序留在这里。</p>
              </div>
            )}
            <div ref={timelineEndRef} />
          </div>
        </section>
      </main>
    </div>
  );
}

function ChannelCard({
  speaker,
  state,
  transcript,
}: {
  speaker: Speaker;
  state: ChannelState;
  transcript: TranscriptState;
}) {
  const isInterviewer = speaker === "interviewer";
  const text = transcript.partial || transcript.final;

  return (
    <article className={`channel-card ${speaker}`}>
      <div className="channel-head">
        <div>
          <p className="channel-source">{isInterviewer ? "系统音频" : "麦克风"}</p>
          <h2>{isInterviewer ? "面试官" : "你"}</h2>
        </div>
        <div className={`channel-status ${state.phase}`}>
          <span aria-hidden="true" />
          {state.message}
        </div>
      </div>
      <p className={`caption ${transcript.partial ? "live" : ""}`}>
        {text || (state.phase === "listening" ? "等待语音…" : "尚未开始")}
      </p>
    </article>
  );
}

function AnswerCard({ answer }: { answer: AnswerRecord }) {
  return (
    <article className={`answer-card ${answer.status}`}>
      <div className="answer-meta">
        <time dateTime={answer.createdAt}>{formatClock(answer.createdAt)}</time>
        <span>{answerStatusLabel(answer.status)}</span>
      </div>
      {answer.question ? <p className="answer-question">{answer.question}</p> : null}
      <p className="answer-text">{answer.text || "正在组织答案…"}</p>
      {answer.detail ? <p className="answer-detail">{answer.detail}</p> : null}
    </article>
  );
}

async function requestCaptureInitialization() {
  const request = window.interviewDesktop?.requestCaptureInitialization;
  if (!request) {
    throw new Error("Electron 采集初始化接口不可用。");
  }
  await request();
}

async function createInterviewSession(): Promise<InterviewSession> {
  const create = window.interviewDesktop?.createInterview;
  if (!create) {
    throw new Error("只有 Electron 采集端可以创建面试会话。");
  }
  return parseInterviewSession(await create(API_BASE_URL), true);
}

async function endInterviewSession(interview: InterviewSession) {
  if (IS_CAPTURE_HOST && window.interviewDesktop?.endInterview) {
    await window.interviewDesktop.endInterview(
      API_BASE_URL,
      interview.interview_id,
      interview.session_token,
    );
    return;
  }
  const response = await fetch(
    `${API_BASE_URL}/api/interviews/${encodeURIComponent(interview.interview_id)}`,
    {
      method: "DELETE",
      credentials: "include",
      headers: { Authorization: `Bearer ${interview.session_token}` },
    },
  );
  if (!response.ok && response.status !== 404) {
    throw new Error(await readResponseError(response));
  }
}

function parseInterviewSession(value: Partial<InterviewSession>, requireCaptureToken: boolean) {
  if (
    typeof value.interview_id !== "string" ||
    !value.interview_id ||
    typeof value.session_token !== "string" ||
    !value.session_token ||
    (requireCaptureToken && (typeof value.capture_token !== "string" || !value.capture_token))
  ) {
    throw new Error("服务端返回了无效面试会话。");
  }
  return {
    interview_id: value.interview_id,
    session_token: value.session_token,
    ...(requireCaptureToken ? { capture_token: value.capture_token } : {}),
  } satisfies InterviewSession;
}

async function readResponseError(response: Response) {
  try {
    const payload = (await response.json()) as { detail?: string; error?: string };
    return payload.detail ?? payload.error ?? `请求失败（${response.status}）`;
  } catch {
    return `请求失败（${response.status}）`;
  }
}

function parseSpeaker(value: unknown): Speaker | null {
  return value === "interviewer" || value === "candidate" ? value : null;
}

function isTerminalAnswer(answer: AnswerRecord) {
  return answer.status !== "streaming";
}

function parseAnswerStatus(status: string | undefined): AnswerStatus {
  return status === "completed" || status === "interrupted" || status === "error"
    ? status
    : "streaming";
}

function answerStatusLabel(status: AnswerStatus) {
  switch (status) {
    case "streaming":
      return "生成中";
    case "completed":
      return "已完成";
    case "interrupted":
      return "已打断";
    case "error":
      return "失败";
  }
}

function deviceStatusLabel(status: DeviceStatus, active: boolean) {
  if (status === "error") {
    return "异常";
  }
  if (status === "ready" && active) {
    return "采集中";
  }
  switch (status) {
    case "offline":
      return "离线";
    case "initializing":
      return "初始化";
    case "ready":
      return "已就绪";
  }
}

function connectionLabel(state: ClientConnectionState) {
  switch (state) {
    case "connected":
      return "界面已同步";
    case "connecting":
      return "连接中";
    case "reconnecting":
      return "重连中";
    case "disconnected":
      return "界面未连接";
  }
}

function formatClock(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error && error.message ? error.message : fallback;
}

async function settleMediaRequest(promise: Promise<MediaStream>) {
  try {
    return { ok: true as const, stream: await promise };
  } catch (error) {
    return { ok: false as const, error };
  }
}
