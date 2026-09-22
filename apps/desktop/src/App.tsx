import { useEffect, useLayoutEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { Button, Input, Label, ListBox, Modal, ScrollShadow, Select, TextArea, Tooltip } from "@heroui/react";
import {
  ArrowUp, CaretDown, CaretUp, GearSix,
  Microphone, Monitor, Play, Square, WarningCircle, PushPin, Minus,
  ArrowsInLineHorizontal, ArrowsOutLineHorizontal, PencilSimple, ChatCircle, X,
} from "@phosphor-icons/react";
import { requestCaptureStream } from "./audioCapture";
import { CaptureAdapter } from "./captureAdapter";
import { SessionClient, type ClientConnectionState } from "./sessionClient";
import { AnswerMarkdown, CopyTextButton } from "./AnswerMarkdown";
import { CodePanel } from "./CodePanel";
import { answerActionPayload, applyChannelHealth, listeningStatus, manualDraftKey, mergeAnswerEvent, mergeOperation, mergeTranscriptTurn, transcriptForSpeaker, newerAnswerCount, operationIsPending, operationLabel, reconcileReadingAnswer, reconcileReadingSelection, visibleAnswerOrder } from "./interviewUiState";
import type {
  AnswerRecord,
  AnswerStatus,
  AnswerStore,
  ChannelState,
  ChannelHealth,
  DeviceStatus,
  InterviewSession,
  ServerEvent,
  SessionPhase,
  Speaker,
  TranscriptState,
  TranscriptTurn,
  QuestionRecord,
  OperationRecord,
  QuickAnswerAction,
  CodeWorkspace,
  CapturedScreen,
} from "./types";

const IS_DESKTOP = window.interviewDesktop?.isElectron === true;
const IS_CAPTURE_HOST = window.interviewDesktop?.captureHost === true;
const API_BASE_URL = (
  window.interviewDesktop?.apiBaseUrl ||
  import.meta.env.VITE_API_BASE_URL ||
  (IS_CAPTURE_HOST ? "https://interview.siyidu.com" : window.location.origin)
).replace(/\/+$/, "");
const CURRENT_POLL_MS = 5_000;
const QUICK_ANSWERS = [
  { action: "shorten", label: "先给一句", hint: "保留这条回答的核心要点", Icon: ArrowsInLineHorizontal },
  { action: "expand", label: "展开说明", hint: "补充思路和一个具体例子", Icon: ArrowsOutLineHorizontal },
  { action: "rephrase", label: "换个说法", hint: "保持原意，换成更自然的表达", Icon: ChatCircle },
] as const;

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
    channel_details?: Partial<Record<Speaker, ChannelHealth>>;
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
  const [correctionOpen, setCorrectionOpen] = useState(false);
  const [correctionMode, setCorrectionMode] = useState("misheard");
  const [targetQuestionId, setTargetQuestionId] = useState<string | undefined>();
  const [turns, setTurns] = useState<TranscriptTurn[]>([]);
  const [questions, setQuestions] = useState<QuestionRecord[]>([]);
  const [currentQuestionId, setCurrentQuestionId] = useState<string | undefined>();
  const [holdAnswers, setHoldAnswers] = useState(false);
  const [codeOpen, setCodeOpen] = useState(false);
  const codeRevealRef = useRef("");
  const [importedCode, setImportedCode] = useState<{ code: string; language: string } | null>(null);
  const [codeWorkspace, setCodeWorkspace] = useState<CodeWorkspace | null>(null);
  const [collectedScreens, setCollectedScreens] = useState<CapturedScreen[]>([]);
  const [operations, setOperations] = useState<OperationRecord[]>([]);
  const [selectedAnswerId, setSelectedAnswerId] = useState<string | null>(null);
  const [toolError, setToolError] = useState<string | null>(null);
  const [modelStatus, setModelStatus] = useState<{ status: string; detail?: string }>({ status: "ready" });
  const [modelRecoveryNotice, setModelRecoveryNotice] = useState<string | null>(null);
  const [sessionMetrics, setSessionMetrics] = useState<Record<string, unknown>>({});
  const [contextStatus, setContextStatus] = useState<{ documents: number; characters: number } | null>(null);
  const [recoveryNotice, setRecoveryNotice] = useState<string | null>(null);
  const [screenSources, setScreenSources] = useState<InterviewScreenSource[]>([]);
  const [screenPickerOpen, setScreenPickerOpen] = useState(false);
  const [screenBusy, setScreenBusy] = useState(false);
  const [selectedScreenName, setSelectedScreenName] = useState("");
  const [recoveringChannel, setRecoveringChannel] = useState<Speaker | null>(null);
  const captureAfterSelectionRef = useRef(false);
  const draftSubmissionRef = useRef<{ operationId: string; text: string; draftKey: string } | null>(null);
  const answerStageRef = useRef<HTMLDivElement>(null);
  const readingOffsetsRef = useRef<Record<string, number>>({});
  const [answerSnapshotComplete, setAnswerSnapshotComplete] = useState(false);
  const manualDraftsRef = useRef<Record<string, string>>({});
  const modelRecoveringRef = useRef(false);
  const [authRequired, setAuthRequired] = useState(false);
  const [accessToken, setAccessToken] = useState("");
  const [authBusy, setAuthBusy] = useState(false);
  const [initializationFailed, setInitializationFailed] = useState(false);
  const [stopConfirmOpen, setStopConfirmOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [detailView, setDetailView] = useState<"history" | "transcript" | "device">("device");
  const [detailOpen, setDetailOpen] = useState(false);
  const [pinned, setPinned] = useState(true);
  const [windowBusy, setWindowBusy] = useState(false);
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

  const visibleAnswerIds = useMemo(() => visibleAnswerOrder(answers), [answers]);
  const answerList = useMemo(() => visibleAnswerIds.map((id) => answers.byId[id]), [answers, visibleAnswerIds]);
  const clientReady = connectionState === "connected";
  const readingId = reconcileReadingAnswer(selectedAnswerId, visibleAnswerIds);
  const readingAnswer = readingId ? answers.byId[readingId] : undefined;
  const pendingOperations = operations.filter(operationIsPending);
  const screenshotBusy = pendingOperations.some((operation) => operation.kind === "request_screen_capture");
  const quickAnswerBusy = pendingOperations.some((operation) => ["quick_answer", "answer_screens"].includes(operation.kind));
  const manualBusy = draftSubmissionRef.current?.draftKey === manualDraftKey(correctionMode, targetQuestionId)
    && draftSubmissionRef.current?.text === manualText
    && pendingOperations.some((operation) => operation.operation_id === draftSubmissionRef.current?.operationId);
  const holdBusy = pendingOperations.some((operation) => operation.kind === "set_answer_hold");
  const unreadCount = newerAnswerCount(readingId, visibleAnswerIds);

  useEffect(() => {
    if (answerSnapshotComplete) {
      setSelectedAnswerId((current) => reconcileReadingSelection(current, visibleAnswerIds, answers.order));
    }
  }, [answerSnapshotComplete, answers.order, visibleAnswerIds]);
  useLayoutEffect(() => {
    if (answerStageRef.current) answerStageRef.current.scrollTop = readingId ? readingOffsetsRef.current[readingId] ?? 0 : 0;
  }, [readingId]);

  function dispatchControl(payload: Record<string, unknown>): string | null {
    const operationId = typeof payload.operation_id === "string" ? payload.operation_id : crypto.randomUUID();
    if (!sessionClientRef.current?.send({ ...payload, operation_id: operationId })) {
      setError("会话同步正在重连，请稍后再试。");
      return null;
    }
    setOperations((current) => mergeOperation(current, { operation_id: operationId, kind: String(payload.type), status: "sent", ...(typeof payload.action === "string" ? { action: payload.action } : {}) }));
    setError(null);
    return operationId;
  }

  function requestQuickAnswer(action: QuickAnswerAction, answer = readingAnswer) {
    if (!interviewActive || !clientReady || quickAnswerBusy) return;
    dispatchControl(answerActionPayload(action, crypto.randomUUID(), answer, currentQuestionId));
  }

  function correctQuestion(mode = "misheard") {
    const questionId = readingAnswer?.questionId || currentQuestionId;
    setTargetQuestionId(questionId);
    setCorrectionMode(mode);
    setManualText(manualDraftsRef.current[manualDraftKey(mode, questionId)] ?? (mode === "misheard" ? questions.find((question) => question.question_id === questionId)?.text || transcripts.interviewer.final || transcripts.interviewer.partial : ""));
    setCorrectionOpen(true);
  }

  function changeCorrectionMode(mode: string) {
    manualDraftsRef.current[manualDraftKey(correctionMode, targetQuestionId)] = manualText;
    setCorrectionMode(mode);
    setManualText(manualDraftsRef.current[manualDraftKey(mode, targetQuestionId)] ?? (mode === "misheard" ? questions.find((question) => question.question_id === targetQuestionId)?.text || "" : ""));
  }

  function changeManualText(text: string) {
    setManualText(text);
    manualDraftsRef.current[manualDraftKey(correctionMode, targetQuestionId)] = text;
  }

  function changeTargetQuestion(questionId: string) {
    manualDraftsRef.current[manualDraftKey(correctionMode, targetQuestionId)] = manualText;
    setTargetQuestionId(questionId);
    setManualText(manualDraftsRef.current[manualDraftKey(correctionMode, questionId)] ?? (correctionMode === "misheard" ? questions.find((question) => question.question_id === questionId)?.text || "" : ""));
  }

  async function chooseScreen(captureAfterSelection = false) {
    setScreenBusy(true);
    try {
      const listSources = window.interviewDesktop?.listScreenSources;
      if (!listSources) throw new Error("请在 Electron 桌面端选择要读取的屏幕或窗口。");
      const sources = await listSources();
      setScreenSources(sources);
      const selected = sources.find((source) => source.selected);
      setSelectedScreenName(selected?.name || "");
      if (captureAfterSelection && selected) {
        dispatchControl({ type: "request_screen_capture", question_id: currentQuestionId, collect_only: true });
      } else {
        captureAfterSelectionRef.current = captureAfterSelection;
        setScreenPickerOpen(true);
      }
    } catch (screenError) { setError(errorMessage(screenError, "无法列出屏幕。")); }
    finally { setScreenBusy(false); }
  }

  async function selectScreen(source: InterviewScreenSource) {
    setScreenBusy(true);
    try {
      const selection = await window.interviewDesktop?.selectScreenSource?.(source.id);
      if (!selection) throw new Error("屏幕选择接口不可用，请重新打开桌面端。");
      setSelectedScreenName(selection.name);
      setScreenPickerOpen(false);
      if (captureAfterSelectionRef.current) dispatchControl({ type: "request_screen_capture", question_id: currentQuestionId, collect_only: true });
    } catch (screenError) { setError(errorMessage(screenError, "无法选择屏幕。")); }
    finally { setScreenBusy(false); }
  }

  async function recoverChannel(speaker: Speaker) {
    const adapter = captureAdapterRef.current;
    if (!adapter || recoveringChannel) return;
    setRecoveringChannel(speaker);
    try {
      adapter.replaceChannel(speaker, await requestCaptureStream(speaker));
      setError(null);
    } catch (captureError) { setError(errorMessage(captureError, "这路音频恢复失败，请重试。")); }
    finally { setRecoveringChannel(null); }
  }

  async function setPanelCollapsed(value: boolean) {
    if (windowBusy) return false;
    setWindowBusy(true);
    try {
      const result = IS_DESKTOP ? await window.interviewDesktop?.setCollapsed?.(value) : value;
      if (typeof result !== "boolean") throw new Error("请重新打开桌面端以启用浮窗控制。");
      setCollapsed(result);
      return true;
    } catch (windowError) {
      setError(errorMessage(windowError, "无法调整浮窗。"));
      return false;
    } finally { setWindowBusy(false); }
  }

  async function openDetails(view: typeof detailView) {
    if (collapsed && !await setPanelCollapsed(false)) return;
    setDetailView(view);
    setDetailOpen(true);
  }

  async function confirmStop() {
    if (collapsed && !await setPanelCollapsed(false)) return;
    setStopConfirmOpen(true);
  }

  async function togglePinned() {
    try {
      const result = await window.interviewDesktop?.setPinned?.(!pinned);
      if (typeof result === "boolean") setPinned(result);
    } catch (windowError) { setError(errorMessage(windowError, "无法调整置顶状态。")); }
  }

  useEffect(() => {
    activeRef.current = interviewActive;
  }, [interviewActive]);

  useEffect(() => {
    void window.interviewDesktop?.setCodeExpanded?.(codeOpen).catch(() => setError("无法调整代码区窗口大小，仍可在当前窗口内使用。"));
  }, [codeOpen]);

  useEffect(() => {
    let disposed = false;
    void window.interviewDesktop?.getWindowState?.().then((state) => {
      if (!disposed) { setCollapsed(state.collapsed); setPinned(state.pinned); setRecoveryNotice(state.recoveryNotice || null); }
    }).catch(() => { if (!disposed) setError("无法同步浮窗状态，请重新打开桌面端。"); });
    return () => { disposed = true; };
  }, []);

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
        signal: AbortSignal.timeout(10_000),
        redirect: "error",
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
      applyDeviceStatus(current.device_status?.status, current.device_status?.channels, current.device_status?.channel_details);
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
      setTurns([]);
      setQuestions([]);
      setCurrentQuestionId(undefined);
      setHoldAnswers(false);
      setCodeOpen(false);
      codeRevealRef.current = "";
      setImportedCode(null);
      setCodeWorkspace(null);
      setCollectedScreens([]);
      setOperations([]);
      setSelectedAnswerId(null);
      setManualText("");
      setCorrectionOpen(false);
      setTargetQuestionId(undefined);
      setToolError(null);
      manualDraftsRef.current = {};
      readingOffsetsRef.current = {};
      setAnswerSnapshotComplete(false);
      setModelStatus({ status: "ready" });
      setModelRecoveryNotice(null);
      modelRecoveringRef.current = false;
      setSessionMetrics({});
      setContextStatus(null);
      draftSubmissionRef.current = null;
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
          if (state !== "connected") setAnswerSnapshotComplete(false);
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
        signal: AbortSignal.timeout(10_000),
        redirect: "error",
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
    // interview_state acknowledges Start; it is not a tracked model operation.
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
    setStopConfirmOpen(false);
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
    if (!interviewActive || manualBusy) return;
    if (correctionMode === "return") {
      if (targetQuestionId && dispatchControl({ type: "quick_answer", action: "answer", question_id: targetQuestionId })) setCorrectionOpen(false);
      return;
    }
    if (!text) return;
    const kind = correctionMode === "candidate" ? "candidate_context" : correctionMode === "question" ? "question" : "correction";
    const operationId = dispatchControl({ type: "manual_text", kind, text, ...(kind === "correction" && targetQuestionId ? { question_id: targetQuestionId } : {}) });
    if (operationId) {
      draftSubmissionRef.current = { operationId, text: manualText, draftKey: manualDraftKey(correctionMode, targetQuestionId) };
      setCorrectionOpen(false);
    }
  }

  function collectScreen() {
    if (!interviewActive || screenshotBusy || screenBusy) return;
    if (IS_CAPTURE_HOST) void chooseScreen(true);
    else dispatchControl({ type: "request_screen_capture", question_id: currentQuestionId, collect_only: true });
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
      case "context_status":
        setContextStatus({ documents: payload.documents_count ?? 0, characters: payload.characters_count ?? 0 });
        return;
      case "code_state":
        if (payload.workspace) {
          setCodeWorkspace(payload.workspace);
          const revealId = payload.workspace.reveal_id || "";
          if (revealId && revealId !== codeRevealRef.current) setCodeOpen(true);
          codeRevealRef.current = revealId;
        }
        return;
      case "screen_collection":
        setCollectedScreens(payload.screens || []);
        return;
      case "device_status":
        applyDeviceStatus(payload.status, payload.channels, payload.channel_details);
        return;
      case "interview_state":
        applyInterviewState(Boolean(payload.active));
        if (typeof payload.hold_answers === "boolean") setHoldAnswers(payload.hold_answers);
        return;
      case "question_state":
        setCurrentQuestionId(payload.current_question_id);
        if (payload.questions) setQuestions(payload.questions);
        if (typeof payload.hold_answers === "boolean") setHoldAnswers(payload.hold_answers);
        return;
      case "model_status":
        if (payload.status === "recovering") modelRecoveringRef.current = true;
        else if (payload.status === "ready" && modelRecoveringRef.current) {
          setModelRecoveryNotice("模型连接已恢复，已记录的上下文保留。断线期间未被处理的音频需要重说或手动补充。");
          modelRecoveringRef.current = false;
        }
        setModelStatus({ status: payload.status || "ready", detail: payload.detail });
        return;
      case "session_metrics":
        setSessionMetrics(payload.metrics || payload as unknown as Record<string, unknown>);
        return;
      case "answer_snapshot_done":
        setAnswerSnapshotComplete(true);
        return;
      case "operation_snapshot":
        setOperations((current) => (payload.operations || []).map((operation) => ({ ...current.find((item) => item.operation_id === operation.operation_id), ...operation })));
        return;
      case "operation_status": {
        if (!payload.operation_id || !["accepted", "running", "completed", "failed", "cancelled"].includes(payload.status || "")) return;
        const operation = { ...payload, kind: payload.kind || "control", status: payload.status } as OperationRecord;
        setOperations((current) => mergeOperation(current, operation));
        const submitted = draftSubmissionRef.current;
        if (submitted?.operationId === payload.operation_id && payload.status === "completed") {
          setManualText((draft) => draft === submitted.text ? "" : draft);
          if (manualDraftsRef.current[submitted.draftKey] === submitted.text) delete manualDraftsRef.current[submitted.draftKey];
          draftSubmissionRef.current = null;
        }
        if (payload.status === "failed") {
          setError(payload.detail || "操作未完成，请重试。");
          setSessionPhase(activeRef.current ? "live" : "idle");
        }
        return;
      }
      case "tool_error":
        setToolError(payload.detail || payload.error || "工具未完成，当前会话继续回答。");
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
        setTurns((payload.turns || []).filter((turn) => turn.turn_id && parseSpeaker(turn.speaker) && typeof turn.text === "string") as TranscriptTurn[]);
        return;
      }
      case "transcript_delta": {
        const speaker = parseSpeaker(payload.speaker);
        if (speaker === "candidate" && payload.turn_id && typeof payload.text === "string") {
          setTurns((current) => mergeTranscriptTurn(current, {
            turn_id: payload.turn_id!, speaker, text: payload.text!, status: "streaming",
            question_id: payload.question_id, created_at: payload.created_at,
          }));
          return;
        }
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
        if (speaker === "candidate" && payload.turn_id && typeof payload.text === "string") {
          setTurns((current) => mergeTranscriptTurn(current, {
            turn_id: payload.turn_id!, speaker, text: payload.text!,
            status: payload.status === "interrupted" ? "interrupted" : "completed",
            question_id: payload.question_id, created_at: payload.created_at,
          }));
          return;
        }
        setTranscripts((current) => ({
          ...current,
          [speaker]: {
            final: payload.text ?? payload.delta ?? current[speaker].partial,
            partial: "",
          },
        }));
        if (payload.turn_id && typeof payload.text === "string") {
          const turn: TranscriptTurn = { turn_id: payload.turn_id, speaker, text: payload.text, question_id: payload.question_id, created_at: payload.created_at, kind: payload.kind };
          setTurns((current) => current.some((item) => item.turn_id === turn.turn_id)
            ? current.map((item) => item.turn_id === turn.turn_id ? turn : item) : [...current, turn]);
        }
        return;
      }
      case "answer_started":
      case "answer_delta":
        updateAnswerFromEvent(payload, "streaming", false);
        return;
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
    updateAnswer(payload.response_id, (current) => mergeAnswerEvent(current, payload, status, replaceText));
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
    details?: Partial<Record<Speaker, ChannelHealth>>,
  ) {
    if (IS_CAPTURE_HOST && !captureAdapterRef.current) {
      if (!initializationInFlightRef.current) {
        setDeviceStatus("error");
        setInitializationFailed(true);
      }
      return;
    }
    const status: DeviceStatus =
      rawStatus === "initializing" || rawStatus === "ready" || rawStatus === "error" ? rawStatus : "offline";
    setDeviceStatus(status);
    setInitializationFailed(false);
    setChannels((current) => {
      const next = { ...current };
      (["interviewer", "candidate"] as Speaker[]).forEach((speaker) => {
        const ready = channelReady?.[speaker] === true;
        next[speaker] = applyChannelHealth(ready, status, activeRef.current, details?.[speaker]);
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
        ["listening", "ready"].includes(current.interviewer.phase)
          ? { ...current.interviewer, phase: active ? "listening" : "ready", message: active ? "采集中" : "已就绪" }
          : current.interviewer,
      candidate:
        ["listening", "ready"].includes(current.candidate.phase)
          ? { ...current.candidate, phase: active ? "listening" : "ready", message: active ? "采集中" : "已就绪" }
          : current.candidate,
    }));
  }

  function handleSessionEnded() {
    if (!sessionRef.current) {
      return;
    }
    setStopConfirmOpen(false);
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
    setDeviceStatus("error");
    setChannel(speaker, "error", "采集已停止");
    setError(`${speaker === "candidate" ? "麦克风" : "系统音频"}采集已停止，请在设备详情中恢复这路音频。`);
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

  const liveStatus = listeningStatus({ connected: clientReady, reconnecting: connectionState === "reconnecting", active: interviewActive, deviceStatus, channels, held: holdAnswers, answering: answerList.some((answer) => answer.status === "streaming") });
  const statusText = clientReady && interviewActive && modelStatus.status === "recovering" ? "模型重连中" : liveStatus.label;
  const latestOperation = pendingOperations[pendingOperations.length - 1] || operations[operations.length - 1];
  const readingIndex = readingId ? visibleAnswerIds.indexOf(readingId) : -1;

  return (
    <div className={`app-shell ${collapsed ? "is-collapsed" : ""} ${codeOpen ? "has-code" : ""}`}>
      <header className="floating-toolbar" aria-label="面试控制">
        <div className="brand" aria-label="Sage 模拟面试">
          <span>Sage</span>
        </div>

        <Button variant="ghost" className={`status-button ${liveStatus.live && modelStatus.status !== "recovering" ? "is-live" : ""}`}
          onPress={() => openDetails("device")} aria-label={`设备详情：${statusText}`}>
          <span className="status-dot" aria-hidden="true" />{statusText}
        </Button>
        <div className="toolbar-actions">
          {IS_DESKTOP && <Tooltip><Button isIconOnly variant="ghost" aria-label={pinned ? "取消置顶" : "置顶窗口"} aria-pressed={pinned} onPress={() => void togglePinned()} className="pin-button">
            <PushPin size={15} weight={pinned ? "fill" : "regular"} />
          </Button><Tooltip.Content>{pinned ? "取消置顶" : "置顶窗口"}</Tooltip.Content></Tooltip>}
          <Button variant="ghost" className="collapse-button" onPress={() => void setPanelCollapsed(!collapsed)} isDisabled={windowBusy}
            aria-expanded={!collapsed} aria-controls="interview-panel">
            {collapsed ? <CaretDown size={16} /> : <CaretUp size={16} />}
            {collapsed ? "展开" : "收起"}
          </Button>
          {interviewActive || sessionPhase === "stopping" ? (
            <Tooltip><Button isIconOnly variant="secondary" className="stop-button" aria-label="结束面试"
              onPress={() => void confirmStop()} isDisabled={sessionPhase === "stopping"}>
              <Square size={15} weight="fill" />
            </Button><Tooltip.Content>结束面试</Tooltip.Content></Tooltip>
          ) : (
            <Button className="start-button" onPress={() => void startInterview()} isDisabled={startDisabled}>
              <Play size={14} weight="fill" />{startLabel === "开始" ? "开始面试" : startLabel}
            </Button>
          )}
          {IS_DESKTOP && <Tooltip><Button isIconOnly variant="ghost" aria-label="隐藏到托盘" onPress={() => { void window.interviewDesktop?.hideWindow?.().catch(() => setError("无法隐藏窗口。")); }}><Minus size={17} /></Button><Tooltip.Content>隐藏到托盘</Tooltip.Content></Tooltip>}
        </div>
      </header>

      <main id="interview-panel" className="interview-panel" hidden={collapsed}>
        {error ? <NoticeBanner text={error} onDismiss={() => setError(null)} /> : null}
        {toolError ? <NoticeBanner text={toolError} onDismiss={() => setToolError(null)} /> : null}
        {recoveryNotice ? <NoticeBanner text={recoveryNotice} onDismiss={() => setRecoveryNotice(null)} /> : null}
        {modelRecoveryNotice ? <NoticeBanner text={modelRecoveryNotice} onDismiss={() => setModelRecoveryNotice(null)} /> : null}
        {modelStatus.status === "recovering" && <div className="error-banner" role="status"><WarningCircle size={18} /><span>{modelStatus.detail || "模型连接正在恢复，这段时间的音频可能未被完整处理。"}</span></div>}
        {contextStatus?.characters === 0 && <div className="error-banner" role="status"><WarningCircle size={18} /><span>本场未加载有效背景资料。个人经历问题需要手动补充，或配置资料后开始新面试。</span></div>}
        {authRequired ? (
          <section className="login-panel" aria-labelledby="login-title">
            <h1 id="login-title">连接当前面试</h1>
            <p>输入访问密钥，同步桌面端的当前面试。</p>
            <form className="access-form" onSubmit={(event) => void submitBrowserLogin(event)}>
              <label htmlFor="access-token" className="sr-only">访问密钥</label>
              <Input variant="secondary" id="access-token" type="password" autoComplete="current-password" value={accessToken}
                onChange={(event) => setAccessToken(event.target.value)} placeholder="访问密钥" disabled={authBusy} />
              <Button type="submit" isDisabled={!accessToken.trim() || authBusy}>{authBusy ? "验证中…" : "连接"}</Button>
            </form>
          </section>
        ) : (
          <>
            {readingAnswer && <div className="reading-navigation" aria-label="阅读位置">
              <Button variant="ghost" size="sm" isDisabled={readingIndex <= 0} onPress={() => setSelectedAnswerId(visibleAnswerIds[readingIndex - 1])}>上一条</Button>
              <span>回答 {readingIndex + 1} / {answerList.length}</span>
              {unreadCount > 0 ? <Button variant="secondary" size="sm" onPress={() => setSelectedAnswerId(visibleAnswerIds[visibleAnswerIds.length - 1])}>{unreadCount} 条新回答 ↓</Button> : <span>正在阅读</span>}
            </div>}
            <div className="interview-workspace">
            <ScrollShadow ref={answerStageRef} className="answer-stage" aria-label="当前阅读的回答" role="region" tabIndex={0} size={16}
              onScroll={(event) => { if (readingId) readingOffsetsRef.current[readingId] = event.currentTarget.scrollTop; }}>
              {readingAnswer ? <AnswerCard key={readingAnswer.responseId} answer={readingAnswer} onAction={(action) => requestQuickAnswer(action, readingAnswer)}
                onUseCode={interviewActive && clientReady ? (code, language) => { setImportedCode({ code, language }); setCodeOpen(true); } : undefined}
                actionsDisabled={!interviewActive || !clientReady || quickAnswerBusy} /> : (
                <div className="empty-state">
                  <h1>{interviewActive ? "等待下一个问题" : "准备开始"}</h1>
                  <p>{interviewActive ? "面试官提问后，回答建议会出现在这里。" :
                    deviceStatus === "ready" ? "点击开始，面试官提问后显示回答建议。" :
                    IS_CAPTURE_HOST ? "正在准备音频，连接后即可开始。" : "打开桌面端，连接后即可开始练习。"}</p>
                </div>
              )}
            </ScrollShadow>
            <div id="code-workspace" className="code-region" hidden={!codeOpen}>
              {codeWorkspace ? <CodePanel workspace={codeWorkspace} enabled={interviewActive && clientReady && answerSnapshotComplete}
                operations={operations} dispatch={dispatchControl} importedCode={importedCode} clearImport={() => setImportedCode(null)} /> : <p className="code-loading">正在同步代码区…</p>}
            </div>
            </div>
            <div className="composer-area">
              {collectedScreens.length > 0 && <div className="screen-collection">
                <span role="status">已收集 {collectedScreens.length} 张 · 可继续截图</span>
                <Button size="sm" variant="secondary" isDisabled={!interviewActive || !clientReady || screenshotBusy || quickAnswerBusy}
                  onPress={() => dispatchControl({ type: "answer_screens", request_ids: collectedScreens.map((screen) => screen.request_id) })}>回答题目</Button>
                <Button size="sm" variant="ghost" isDisabled={!interviewActive || !clientReady || screenshotBusy}
                  onPress={() => dispatchControl({ type: "clear_screens", request_ids: collectedScreens.map((screen) => screen.request_id) })}>新一组</Button>
              </div>}
              {latestOperation && <div className={`operation-status ${latestOperation.status}`} role="status" aria-live="polite">
                <span>{operationLabel(latestOperation)}{!clientReady && operationIsPending(latestOperation) ? " · 等待重连核对" : ""}</span>
                {latestOperation.detail && <span>{latestOperation.detail}</span>}
                {pendingOperations.length > 1 && <span>另有 {pendingOperations.length - 1} 项处理中</span>}
              </div>}
              <nav className="primary-actions" aria-label="面试快捷操作">
                <Button variant="secondary" onPress={collectScreen} isDisabled={!interviewActive || !clientReady || screenshotBusy || screenBusy}><Monitor size={17} />{screenshotBusy ? "截图中…" : "截图"}</Button>
                <Button variant={codeOpen ? "primary" : "secondary"} onPress={() => setCodeOpen(!codeOpen)}
                  aria-expanded={codeOpen} aria-controls="code-workspace">代码</Button>
                <Button variant="secondary" onPress={() => correctQuestion()} isDisabled={!interviewActive || !clientReady}><PencilSimple size={17} />纠正</Button>
                <Button variant="secondary" onPress={() => requestQuickAnswer("deep")} isDisabled={!interviewActive || !clientReady || quickAnswerBusy || (!readingAnswer && !currentQuestionId)}><ArrowsOutLineHorizontal size={17} />深入</Button>
                <Button variant={holdAnswers ? "primary" : "secondary"} onPress={() => dispatchControl({ type: "set_answer_hold", hold: !holdAnswers })} isDisabled={!interviewActive || !clientReady || holdBusy} aria-pressed={holdAnswers}>{holdAnswers ? "现在回答" : "先别答"}</Button>
              </nav>
            </div>
          </>
        )}
        <footer className="panel-footer">
          <nav aria-label="面试记录">
            <Button variant="ghost" size="sm" onPress={() => correctQuestion("question")} isDisabled={!interviewActive || !clientReady}>输入问题</Button>
            <Button variant="ghost" size="sm" onPress={() => void openDetails("transcript")}>转写</Button>
            <Button variant="ghost" size="sm" onPress={() => void openDetails("history")}>回答记录{answerList.length > 0 ? ` · ${answerList.length}` : ""}</Button>
          </nav>
          <Tooltip><Button isIconOnly variant="ghost" size="sm" aria-label="设备详情" onPress={() => void openDetails("device")}><GearSix size={16} /></Button><Tooltip.Content>设备详情</Tooltip.Content></Tooltip>
        </footer>
      </main>

      <Modal.Backdrop isOpen={detailOpen} onOpenChange={setDetailOpen} className="sage-backdrop">
        <Modal.Container size="lg" placement="center">
          <Modal.Dialog className="sage-dialog" aria-label={detailView === "history" ? "回答记录" : detailView === "transcript" ? "实时转写" : "设备详情"}>
            <Modal.CloseTrigger aria-label="关闭" />
            <Modal.Header><Modal.Heading>{detailView === "history" ? "回答记录" : detailView === "transcript" ? "实时转写" : "设备详情"}</Modal.Heading></Modal.Header>
            <Modal.Body className="detail-body">
              {detailView === "history" ? <>
                <p className="detail-intro">本场 {answerList.length} 条回答，按生成顺序保留。</p>
                {answerList.length ? answerList.map((answer) => <div key={answer.responseId} className="history-answer"><Button variant="ghost" size="sm" onPress={() => { setSelectedAnswerId(answer.responseId); setDetailOpen(false); }}>阅读这条</Button><AnswerCard answer={answer} onAction={(action) => requestQuickAnswer(action, answer)} actionsDisabled={!interviewActive || !clientReady || quickAnswerBusy} /></div>) : <p className="detail-empty">第一条回答出现后，会自动保存在这里。</p>}
              </> : detailView === "transcript" ? <>
                <p className="detail-intro">两路声音分别记录；你的声音只补充上下文。</p>
                <ChannelCard speaker="interviewer" state={channels.interviewer} transcript={transcripts.interviewer} />
                <ChannelCard speaker="candidate" state={channels.candidate} transcript={transcriptForSpeaker(turns, "candidate")} />
                {turns.filter((turn) => turn.text).map((turn) => <div className="transcript-turn" key={turn.turn_id}><span>{turn.speaker === "interviewer" ? "面试官" : "你"}{turn.status === "streaming" ? " · 识别中" : turn.status === "interrupted" ? " · 识别未完成" : ""}</span><p>{turn.text}</p>{turn.speaker === "interviewer" && turn.question_id && <Button variant="ghost" size="sm" onPress={() => { setTargetQuestionId(turn.question_id); setCorrectionMode("misheard"); setManualText(turn.text); setDetailOpen(false); setCorrectionOpen(true); }}>纠正这一段</Button>}</div>)}
              </> : <>
                <p className="detail-intro">{connectionLabel(connectionState)} · {statusText}<br />{liveStatus.detail}</p>
                <div className="device-row"><Monitor size={20} /><div><strong>系统音频</strong><p>面试官 · 提问后生成回答</p></div><span>{channels.interviewer.message}</span></div>
                <div className="device-row"><Microphone size={20} /><div><strong>麦克风</strong><p>你的声音 · 仅作为对话上下文</p></div><span>{channels.candidate.message}</span></div>
                {IS_CAPTURE_HOST && initializationFailed && <Button onPress={() => void retryCaptureInitialization()}>重新连接音频</Button>}
                {IS_CAPTURE_HOST && !initializationFailed && (["interviewer", "candidate"] as Speaker[]).filter((speaker) => ["error", "interrupted", "muted"].includes(channels[speaker].phase)).map((speaker) => <Button key={speaker} variant="secondary" isDisabled={!!recoveringChannel} onPress={() => void recoverChannel(speaker)}>{recoveringChannel === speaker ? "恢复中…" : speaker === "candidate" ? "恢复麦克风" : "恢复系统音频"}</Button>)}
                {IS_CAPTURE_HOST && <div className="screen-setting"><p>{selectedScreenName ? `看题来源：${selectedScreenName}` : "尚未选择看题屏幕"}</p><Button variant="secondary" isDisabled={screenBusy} onPress={() => void chooseScreen()}>选择屏幕或窗口</Button></div>}
                {!IS_CAPTURE_HOST && <p className="detail-intro">音频由 Electron 桌面端采集，此页面同步显示。</p>}
                {operations.length > 0 && <div className="operation-history">{operations.map((operation) => <p key={operation.operation_id}>{operationLabel(operation)}{operation.detail ? ` · ${operation.detail}` : ""}</p>)}</div>}
                {Object.keys(sessionMetrics).length > 0 && <p className="detail-intro">重连 {Number(sessionMetrics.reconnections || 0)} 次 · 音频缺口 {Number(sessionMetrics.audio_gaps || 0)} 次 · 工具失败 {Number(sessionMetrics.tool_failures || 0)} 次</p>}
                {contextStatus && <p className="detail-intro">本场资料：{contextStatus.documents} 份 · {contextStatus.characters} 字符。修改资料后需开始新面试。</p>}
              </>}
            </Modal.Body>
          </Modal.Dialog>
        </Modal.Container>
      </Modal.Backdrop>

      <Modal.Backdrop isOpen={correctionOpen} onOpenChange={setCorrectionOpen} className="sage-backdrop">
        <Modal.Container size="lg" placement="center"><Modal.Dialog className="sage-dialog" aria-label="纠正与补充">
          <Modal.CloseTrigger aria-label="关闭" />
          <Modal.Header><Modal.Heading>纠正与补充</Modal.Heading></Modal.Header>
          <form onSubmit={submitManualQuestion}>
            <Modal.Body className="correction-body">
              <Select value={correctionMode} onChange={(key) => changeCorrectionMode(String(key))} aria-label="修改内容">
                <Label>修改内容</Label><Select.Trigger><Select.Value /><Select.Indicator /></Select.Trigger>
                <Select.Popover><ListBox>{[{ id: "misheard", text: "题目听错了" }, { id: "conditions", text: "条件变了" }, { id: "return", text: "回到某道题" }, { id: "candidate", text: "补充我的情况" }, { id: "question", text: "输入新问题" }].map((item) => <ListBox.Item key={item.id} id={item.id} textValue={item.text}>{item.text}<ListBox.ItemIndicator /></ListBox.Item>)}</ListBox></Select.Popover>
              </Select>
              {!["candidate", "question"].includes(correctionMode) && <Select value={targetQuestionId || null} onChange={(key) => changeTargetQuestion(String(key))} aria-label="对应题目" placeholder="选择对应的题目">
                <Label>对应题目</Label><Select.Trigger><Select.Value /><Select.Indicator /></Select.Trigger>
                <Select.Popover><ListBox>{questions.map((question, index) => <ListBox.Item key={question.question_id} id={question.question_id} textValue={`${index + 1}. ${question.text}`}>{index + 1}. {question.text}<ListBox.ItemIndicator /></ListBox.Item>)}</ListBox></Select.Popover>
              </Select>}
              {correctionMode !== "return" && <><label htmlFor="manual-question">{correctionMode === "candidate" ? "补充事实" : correctionMode === "conditions" ? "变化后的完整条件" : correctionMode === "question" ? "问题" : "正确的题目"}</label><TextArea id="manual-question" variant="secondary" rows={4} value={manualText} onChange={(event) => changeManualText(event.target.value)} placeholder={correctionMode === "candidate" ? "例如：这个项目中，我实际负责的是…" : "保留完整题意和限制条件…"} /></>}
              <p className="detail-intro">{correctionMode === "candidate" ? "作为你的上下文补充，不触发回答。" : correctionMode === "return" ? "结合完整对话，重新回答所选题目。" : "补充内容进入完整对话；已有回答保留，新回答追加显示。"}</p>
            </Modal.Body>
            <Modal.Footer><Button variant="ghost" onPress={() => { if (dispatchControl({ type: "set_answer_hold", hold: true })) setCorrectionOpen(false); }} isDisabled={!interviewActive || !clientReady || holdBusy}>等对方重说</Button><Button type="submit" isDisabled={!interviewActive || !clientReady || manualBusy || (correctionMode === "return" ? !targetQuestionId : !manualText.trim()) || (!["candidate", "question"].includes(correctionMode) && !targetQuestionId)}><ArrowUp size={16} />{correctionMode === "candidate" ? "补充上下文" : correctionMode === "return" ? "回到这题" : "提交"}</Button></Modal.Footer>
          </form>
        </Modal.Dialog></Modal.Container>
      </Modal.Backdrop>

      <Modal.Backdrop isOpen={screenPickerOpen} onOpenChange={setScreenPickerOpen} className="sage-backdrop">
        <Modal.Container size="lg" placement="center"><Modal.Dialog className="sage-dialog" aria-label="选择看题来源">
          <Modal.CloseTrigger aria-label="关闭" /><Modal.Header><Modal.Heading>选择看题来源</Modal.Heading></Modal.Header>
          <Modal.Body><p className="detail-intro">选择要读取的屏幕或窗口。每次看题只取一张截图。</p><div className="screen-source-grid">{screenSources.map((source) => <Button key={source.id} variant="secondary" className="screen-source" onPress={() => void selectScreen(source)} isDisabled={screenBusy}><img src={source.thumbnailDataUrl} alt="" /><span>{source.name}{source.selected ? " · 已选择" : ""}</span></Button>)}</div>{!screenSources.length && <p>没有可用的屏幕或窗口，请检查系统录屏权限。</p>}</Modal.Body>
        </Modal.Dialog></Modal.Container>
      </Modal.Backdrop>

      <Modal.Backdrop isOpen={stopConfirmOpen} onOpenChange={setStopConfirmOpen} isDismissable={false} className="sage-backdrop">
        <Modal.Container size="sm" placement="center">
          <Modal.Dialog className="sage-dialog" role="alertdialog" aria-labelledby="stop-dialog-title" aria-describedby="stop-dialog-description">
            <Modal.Header><Modal.Heading id="stop-dialog-title">确定结束当前面试？</Modal.Heading></Modal.Header>
            <Modal.Body><p id="stop-dialog-description">本场回答、转写和模型上下文会从服务端清除，结束后无法恢复。</p></Modal.Body>
            <Modal.Footer>
              <Button autoFocus variant="secondary" onPress={() => setStopConfirmOpen(false)}>继续面试</Button>
              <Button variant="danger-soft" onPress={() => void stopInterview()}>确认结束</Button>
            </Modal.Footer>
          </Modal.Dialog>
        </Modal.Container>
      </Modal.Backdrop>
    </div>
  );
}

function ChannelCard({ speaker, state, transcript }: {
  speaker: Speaker; state: ChannelState; transcript: TranscriptState;
}) {
  const isInterviewer = speaker === "interviewer";
  const text = transcript.partial || transcript.final;
  return (
    <article className={`channel-card ${speaker}`}>
      <div className="channel-head">
        <span>{isInterviewer ? <Monitor size={18} /> : <Microphone size={18} />}{isInterviewer ? "面试官" : "你"}</span>
        <span className={`channel-status ${state.phase}`}>{state.message}</span>
      </div>
      <p className={`caption ${transcript.partial ? "live" : ""}`}>{text || (state.phase === "listening" ? "等待语音…" : "尚未开始")}</p>
    </article>
  );
}

function NoticeBanner({ text, onDismiss }: { text: string; onDismiss: () => void }) {
  return <div className="error-banner" role="alert"><WarningCircle size={18} /><span>{text}</span><Button isIconOnly size="sm" variant="ghost" aria-label="关闭提示" onPress={onDismiss}><X size={14} /></Button></div>;
}

function AnswerCard({ answer, onAction, onUseCode, actionsDisabled = false }: { answer: AnswerRecord; onAction?: (action: QuickAnswerAction) => void; onUseCode?: (code: string, language: string) => void; actionsDisabled?: boolean }) {
  return (
    <article className={`answer-card ${answer.status}`}>
      {answer.question ? <div className="question-row"><p className="answer-question">{answer.question}</p></div> : null}
      {answer.text ? <AnswerMarkdown text={answer.text} onUseCode={answer.status === "completed" ? onUseCode : undefined} /> : <p className="answer-text">正在组织答案…</p>}
      {answer.detail ? <p className="answer-detail">{answer.detail}</p> : null}
      <div className="answer-meta">
        <span role="status">{answerStatusLabel(answer.status)}</span><span aria-hidden="true">·</span>
        <time dateTime={answer.createdAt}>{formatClock(answer.createdAt)}</time>
        <CopyTextButton text={answer.text} label="复制回答" className="copy-button" />
      </div>
      {onAction && <nav className="quick-answers answer-actions" aria-label="这条回答的操作">{QUICK_ANSWERS.map(({ action, label, hint, Icon }) => <Tooltip key={action}><Button variant="ghost" size="sm" onPress={() => onAction(action)} isDisabled={actionsDisabled || !answer.text}><Icon size={14} />{label}</Button><Tooltip.Content>{hint}</Tooltip.Content></Tooltip>)}</nav>}
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
      signal: AbortSignal.timeout(10_000),
      redirect: "error",
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
