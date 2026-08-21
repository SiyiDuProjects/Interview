import {
  Bot,
  Camera,
  Check,
  ChevronRight,
  Eye,
  EyeOff,
  LoaderCircle,
  MessageSquare,
  Monitor,
  Play,
  RotateCcw,
  Send,
  Square,
  X,
} from "lucide-react";
import { KeyboardEvent as ReactKeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import {
  captureScreenshotDataUrl,
  getCaptureLabel,
  requestCaptureStream,
  startLocalAudioCapture,
  type AudioCaptureHandle,
} from "./audio";
import type { CandidateContext, RealtimeAnswer, RealtimeMessage, Speaker, TranscriptTurn } from "./types";

const API_BASE_URL =
  resolveApiBaseUrl(window.glassDesktop?.apiBaseUrl, window.glassDesktop?.localApiEnabled) ||
  resolveApiBaseUrl(import.meta.env.VITE_API_BASE_URL, false) ||
  "https://interview.reachard.co";

const MIN_AUDIO_CHUNK_BYTES = 2048;
const MAX_HISTORY_TURNS = 80;
const MAX_REALTIME_ANSWER_ITEMS = 24;
const SOCKET_RECONNECT_DELAYS_MS = [1000, 2000, 5000];

const initialContext: CandidateContext = {
  name: "",
  target_role: "后端开发工程师",
  resume: "做过内部数据服务平台，负责过 SQL 优化、缓存链路和后端接口治理。",
  job_description: "要求 SQL、Redis、分布式基础和项目表达能力都比较强。",
  custom_notes: "回答尽量简洁，优先突出结果、取舍和系统设计思路。",
};

type SendMode = "question" | "context";

export default function App() {
  const [context, setContext] = useState<CandidateContext>(() => loadStoredContext());
  const [history, setHistory] = useState<TranscriptTurn[]>([]);
  const [answers, setAnswers] = useState<RealtimeAnswer[]>([]);
  const [input, setInput] = useState("");
  const [sendMode, setSendMode] = useState<SendMode>("question");
  const [expanded, setExpanded] = useState(true);
  const [privacyMode, setPrivacyMode] = useState(false);
  const [privacyModePending, setPrivacyModePending] = useState(false);
  const [sessionStarting, setSessionStarting] = useState(false);
  const [captureActive, setCaptureActive] = useState<Record<Speaker, boolean>>({
    interviewer: false,
    candidate: false,
  });
  const [socketActive, setSocketActive] = useState<Record<Speaker, boolean>>({
    interviewer: false,
    candidate: false,
  });
  const [captureMessage, setCaptureMessage] = useState<Record<Speaker, string>>({
    interviewer: "待机",
    candidate: "待机",
  });
  const [showTranscript, setShowTranscript] = useState(false);
  const [showContext, setShowContext] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const answerFeedRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const answersRef = useRef<RealtimeAnswer[]>([]);
  const activeAnswerIdRef = useRef<string | null>(null);
  const historyRef = useRef<TranscriptTurn[]>([]);
  const contextRef = useRef<CandidateContext>(context);
  const socketsRef = useRef<Partial<Record<Speaker, WebSocket>>>({});
  const captureHandlesRef = useRef<Partial<Record<Speaker, AudioCaptureHandle>>>({});
  const captureMessageRef = useRef<Record<Speaker, string>>({ interviewer: "待机", candidate: "待机" });
  const reconnectTimersRef = useRef<Partial<Record<Speaker, number>>>({});
  const reconnectAttemptsRef = useRef<Record<Speaker, number>>({ interviewer: 0, candidate: 0 });
  const manualStopRef = useRef<Record<Speaker, boolean>>({ interviewer: false, candidate: false });
  const speechSegmentRef = useRef<Record<Speaker, { finalized: string; interim: string }>>({
    interviewer: { finalized: "", interim: "" },
    candidate: { finalized: "", interim: "" },
  });

  const sessionOnline = captureActive.interviewer || captureActive.candidate || socketActive.interviewer || socketActive.candidate;
  const latestAnswer = answers[answers.length - 1];
  const sessionButtonLabel = sessionStarting ? "启动中" : sessionOnline ? "结束面试" : error ? "重新开始" : "开始面试";
  const promptPlaceholder =
    sendMode === "question"
      ? "Ask anything about the interview..."
      : "Add candidate context without triggering an answer...";

  const orderedHistory = useMemo(() => history.slice(-40), [history]);

  useEffect(() => {
    historyRef.current = history;
  }, [history]);

  useEffect(() => {
    answersRef.current = answers;
  }, [answers]);

  useEffect(() => {
    contextRef.current = context;
    window.localStorage.setItem("sage-glass-context", JSON.stringify(context));
  }, [context]);

  useEffect(() => {
    answerFeedRef.current?.scrollTo({ top: answerFeedRef.current.scrollHeight });
  }, [answers, latestAnswer?.text, showTranscript]);

  useEffect(() => {
    return window.glassDesktop?.onCommand((command) => {
      if (command === "submit") {
        void submitManualTurn();
      }
      if (command === "toggle-session") {
        void toggleSession();
      }
      if (command === "new-chat") {
        resetConversation();
      }
    });
  }, [input, sendMode, sessionOnline]);

  useEffect(() => {
    let active = true;
    window.glassDesktop
      ?.invoke<{ enabled: boolean }>("glass:get-privacy-mode")
      .then((result) => {
        if (active) {
          setPrivacyMode(Boolean(result.enabled));
        }
      })
      .catch(() => {
        if (active) {
          setPrivacyMode(false);
        }
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    return () => {
      stopSession();
      Object.values(reconnectTimersRef.current).forEach((timerId) => {
        if (timerId) {
          window.clearTimeout(timerId);
        }
      });
    };
  }, []);

  async function toggleSession() {
    if (sessionStarting) {
      return;
    }

    if (sessionOnline) {
      stopSession();
      return;
    }

    setSessionStarting(true);
    setError(null);
    const micStarted = await startSpeakerCapture("candidate");
    const systemStarted = await startSpeakerCapture("interviewer");
    setSessionStarting(false);

    if (!micStarted && !systemStarted) {
      setError(
        `麦克风和系统音频都没有启动成功。麦克风：${captureMessageRef.current.candidate}；系统音频：${captureMessageRef.current.interviewer}`,
      );
      return;
    }
    if (micStarted && !systemStarted) {
      setError(`麦克风已启动，但系统音频启动失败：${captureMessageRef.current.interviewer}`);
    }
    if (!micStarted && systemStarted) {
      setError(`系统音频已启动，但麦克风启动失败：${captureMessageRef.current.candidate}`);
    }
  }

  async function startSpeakerCapture(speaker: Speaker) {
    if (captureHandlesRef.current[speaker]) {
      return true;
    }

    try {
      manualStopRef.current[speaker] = false;
      clearReconnectTimer(speaker);
      await ensureRealtimeSocket(speaker);
      const stream = await requestCaptureStream(speaker);
      const handle = startLocalAudioCapture({
        stream,
        onChunk: (chunk) => sendAudioChunk(speaker, chunk),
      });

      captureHandlesRef.current[speaker] = handle;
      reconnectAttemptsRef.current[speaker] = 0;
      setCaptureActive((current) => ({ ...current, [speaker]: true }));
      setSpeakerCaptureMessage(speaker, "监听中");
      return true;
    } catch (captureError) {
      const message = captureError instanceof Error ? captureError.message : "音频采集启动失败";
      manualStopRef.current[speaker] = true;
      closeRealtimeSocket(speaker, true);
      setSpeakerCaptureMessage(speaker, message);
      return false;
    }
  }

  function stopSession() {
    stopSpeakerCapture("candidate");
    stopSpeakerCapture("interviewer");
  }

  function stopSpeakerCapture(speaker: Speaker) {
    manualStopRef.current[speaker] = true;
    reconnectAttemptsRef.current[speaker] = 0;
    clearReconnectTimer(speaker);

    captureHandlesRef.current[speaker]?.stop();
    delete captureHandlesRef.current[speaker];

    closeRealtimeSocket(speaker, true);
    resetSpeakerSegments(speaker);

    setCaptureActive((current) => ({ ...current, [speaker]: false }));
    setSpeakerCaptureMessage(speaker, "待机");
  }

  async function ensureRealtimeSocket(speaker: Speaker) {
    const existing = socketsRef.current[speaker];
    if (existing && existing.readyState === WebSocket.OPEN) {
      return existing;
    }

    manualStopRef.current[speaker] = false;
    clearReconnectTimer(speaker);
    const socket = await openRealtimeSocket(speaker);
    socketsRef.current[speaker] = socket;
    return socket;
  }

  async function openRealtimeSocket(speaker: Speaker) {
    return await new Promise<WebSocket>((resolve, reject) => {
      const socket = new WebSocket(`${getRealtimeSocketBaseUrl(API_BASE_URL)}/ws/realtime/interview/${speaker}`);
      let resolved = false;
      let rejected = false;
      let timeoutId = 0;

      const fail = (error: Error) => {
        if (resolved || rejected) {
          return;
        }
        rejected = true;
        window.clearTimeout(timeoutId);
        reject(error);
      };

      timeoutId = window.setTimeout(() => {
        fail(new Error(`${getCaptureLabel(speaker)} Realtime 连接超时。`));
        socket.close();
      }, 10000);

      socket.addEventListener("open", () => {
        socket.send(JSON.stringify(buildRealtimeStartPayload()));
        setSpeakerCaptureMessage(speaker, "连接中");
      });

      socket.addEventListener("message", (event) => {
        if (typeof event.data !== "string") {
          return;
        }

        let payload: RealtimeMessage;
        try {
          payload = JSON.parse(event.data) as RealtimeMessage;
        } catch {
          return;
        }

        if (payload.type === "ready") {
          if (!resolved) {
            resolved = true;
            window.clearTimeout(timeoutId);
            reconnectAttemptsRef.current[speaker] = 0;
            setSocketActive((current) => ({ ...current, [speaker]: true }));
            resolve(socket);
          }
          return;
        }

        if (payload.type === "error" && !resolved) {
          const detail = formatRealtimeError(payload.detail ?? `${getCaptureLabel(speaker)} Realtime 失败。`);
          setSpeakerCaptureMessage(speaker, detail);
          fail(new Error(detail));
          socket.close();
          return;
        }

        handleRealtimeMessage(speaker, payload);
      });

      socket.addEventListener("error", () => {
        if (!resolved) {
          fail(new Error(`${getCaptureLabel(speaker)} Realtime 连接失败。`));
        } else {
          setError(`${getCaptureLabel(speaker)} Realtime 连接异常。`);
        }
      });

      socket.addEventListener("close", () => {
        if (!resolved) {
          fail(new Error(`${getCaptureLabel(speaker)} Realtime 已断开。`));
          return;
        }
        if (socketsRef.current[speaker] === socket) {
          delete socketsRef.current[speaker];
          setSocketActive((current) => ({ ...current, [speaker]: false }));
        }
        if (resolved && captureHandlesRef.current[speaker] && !manualStopRef.current[speaker]) {
          scheduleSocketReconnect(speaker);
        }
      });
    });
  }

  function closeRealtimeSocket(speaker: Speaker, notifyServer: boolean) {
    const socket = socketsRef.current[speaker];
    if (!socket) {
      setSocketActive((current) => ({ ...current, [speaker]: false }));
      return;
    }

    if (socket.readyState === WebSocket.OPEN) {
      if (notifyServer) {
        socket.send(JSON.stringify({ type: "finalize" }));
      }
      socket.send(JSON.stringify({ type: "close" }));
    }
    if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) {
      socket.close();
    }
    delete socketsRef.current[speaker];
    setSocketActive((current) => ({ ...current, [speaker]: false }));
  }

  function setSpeakerCaptureMessage(speaker: Speaker, message: string) {
    captureMessageRef.current = { ...captureMessageRef.current, [speaker]: message };
    setCaptureMessage((current) => ({ ...current, [speaker]: message }));
  }

  function handleRealtimeMessage(speaker: Speaker, payload: RealtimeMessage) {
    if (payload.type === "error") {
      const detail = formatRealtimeError(payload.detail ?? `${getCaptureLabel(speaker)} Realtime 失败。`);
      setError(detail);
      setSpeakerCaptureMessage(speaker, detail);
      markRealtimeAnswerError(detail);
      return;
    }

    if (payload.type === "screen_capture_request") {
      void respondToScreenCaptureRequest(payload);
      return;
    }

    if (payload.type === "answer_status") {
      updateActiveRealtimeAnswer(payload.text ?? "Realtime 正在处理...", payload.status ?? "pending", payload.detail);
      return;
    }

    if (payload.type === "answer_delta") {
      appendRealtimeAnswerDelta(payload.delta ?? "");
      return;
    }

    if (payload.type === "answer_done" || payload.type === "response_done") {
      if (payload.type === "response_done" && payload.detail && payload.detail !== "completed") {
        updateActiveRealtimeAnswer("本次回复没有返回可显示文本。", "error", payload.detail);
        activeAnswerIdRef.current = null;
        return;
      }
      finishRealtimeAnswer(payload.text);
      return;
    }

    if (payload.type === "context_update" && payload.text?.trim()) {
      appendTurn({ speaker: "candidate", text: payload.text.trim(), timestamp: formatTime() });
      setSpeakerCaptureMessage("candidate", `上下文：${truncate(payload.text ?? "")}`);
      return;
    }

    if (payload.type !== "transcript" || !payload.text?.trim()) {
      return;
    }

    const state = speechSegmentRef.current[speaker];
    if (payload.is_final) {
      state.finalized = mergeTranscriptText(state.finalized, payload.text);
      state.interim = "";
    } else {
      state.interim = payload.text.trim();
    }

    const nextText = mergeTranscriptText(state.finalized, state.interim);
    setSpeakerCaptureMessage(speaker, `识别中：${truncate(nextText)}`);

    if (payload.speech_final) {
      appendTurn({ speaker, text: nextText, timestamp: formatTime() });
      resetSpeakerSegments(speaker);
      setSpeakerCaptureMessage(speaker, "监听中");
    }
  }

  async function submitManualTurn() {
    const text = input.trim();
    if (!text) {
      inputRef.current?.focus();
      return;
    }

    const speaker: Speaker = sendMode === "question" ? "interviewer" : "candidate";
    try {
      setError(null);
      const socket = await ensureRealtimeSocket(speaker);
      if (speaker === "interviewer") {
        beginPendingRealtimeAnswer("已发送，等待 Realtime 回复...");
      }
      socket.send(JSON.stringify({ type: "manual_text", text }));
      appendTurn({ speaker, text, timestamp: formatTime() });
      setInput("");
    } catch (sendError) {
      setError(sendError instanceof Error ? sendError.message : "发送失败。");
    }
  }

  async function handleScreenshot(createResponse: boolean) {
    try {
      setError(null);
      const imageUrl = await captureCurrentScreenDataUrl();
      await ensureRealtimeSocket("interviewer");
      sendRealtimeControl("interviewer", {
        type: "screenshot",
        image_url: imageUrl,
        create_response: createResponse,
        prompt: createResponse
          ? "这是我刚截的面试题、白板或代码截图，请结合当前问题给出可以直接口述的中文回答。"
          : "这是我刚截的面试题、白板或代码截图，请作为后续回答上下文。",
      });
      if (createResponse) {
        beginPendingRealtimeAnswer("正在读取截图并生成回答...");
      }
    } catch (screenshotError) {
      setError(screenshotError instanceof Error ? screenshotError.message : "截图失败。");
    }
  }

  async function captureCurrentScreenDataUrl() {
    const activeSystemCapture = captureHandlesRef.current.interviewer;
    if (activeSystemCapture?.snapshot) {
      return activeSystemCapture.snapshot();
    }
    return captureScreenshotDataUrl();
  }

  async function respondToScreenCaptureRequest(payload: RealtimeMessage) {
    const requestId = payload.request_id;
    if (!requestId) {
      return;
    }

    updateActiveRealtimeAnswer("正在读取当前屏幕上下文...", "pending", payload.reason);
    try {
      const imageUrl = await captureCurrentScreenDataUrl();
      sendRealtimeControl("interviewer", {
        type: "screen_snapshot",
        request_id: requestId,
        image_url: imageUrl,
      });
    } catch (captureError) {
      const message = captureError instanceof Error ? captureError.message : "截图失败。";
      sendRealtimeControl("interviewer", {
        type: "screen_snapshot",
        request_id: requestId,
        error: message,
      });
      setError(message);
    }
  }

  function sendAudioChunk(speaker: Speaker, chunk: ArrayBuffer) {
    if (chunk.byteLength < MIN_AUDIO_CHUNK_BYTES) {
      return;
    }

    const socket = socketsRef.current[speaker];
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return;
    }
    socket.send(chunk);
  }

  function sendRealtimeControl(speaker: Speaker, payload: Record<string, unknown>) {
    const socket = socketsRef.current[speaker];
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return;
    }
    socket.send(JSON.stringify(payload));
  }

  function scheduleSocketReconnect(speaker: Speaker) {
    if (manualStopRef.current[speaker] || !captureHandlesRef.current[speaker]) {
      return;
    }
    if (reconnectTimersRef.current[speaker]) {
      return;
    }

    const attempt = reconnectAttemptsRef.current[speaker];
    const delay = SOCKET_RECONNECT_DELAYS_MS[Math.min(attempt, SOCKET_RECONNECT_DELAYS_MS.length - 1)];
    reconnectAttemptsRef.current[speaker] = attempt + 1;
    setSpeakerCaptureMessage(speaker, "重连中");

    reconnectTimersRef.current[speaker] = window.setTimeout(() => {
      reconnectTimersRef.current[speaker] = undefined;
      void reconnectRealtimeSocket(speaker);
    }, delay);
  }

  async function reconnectRealtimeSocket(speaker: Speaker) {
    if (manualStopRef.current[speaker] || !captureHandlesRef.current[speaker]) {
      return;
    }

    try {
      const socket = await openRealtimeSocket(speaker);
      if (manualStopRef.current[speaker] || !captureHandlesRef.current[speaker]) {
        socket.close();
        return;
      }
      socketsRef.current[speaker] = socket;
      setSpeakerCaptureMessage(speaker, "监听中");
    } catch (reconnectError) {
      setError(reconnectError instanceof Error ? reconnectError.message : `${getCaptureLabel(speaker)}重连失败。`);
      scheduleSocketReconnect(speaker);
    }
  }

  function clearReconnectTimer(speaker: Speaker) {
    const timerId = reconnectTimersRef.current[speaker];
    if (!timerId) {
      return;
    }
    window.clearTimeout(timerId);
    reconnectTimersRef.current[speaker] = undefined;
  }

  function buildRealtimeStartPayload() {
    return {
      type: "start",
      context: contextRef.current,
      answer_scope: "general",
      project_context_label: "",
    };
  }

  function appendRealtimeAnswerDelta(delta: string) {
    if (!delta) {
      return;
    }

    const activeId = activeAnswerIdRef.current;
    if (!activeId) {
      beginPendingRealtimeAnswer(delta, "streaming");
      return;
    }

    updateAnswers((current) =>
      current.map((item) =>
        item.id === activeId
          ? {
              ...item,
              text: item.status === "pending" ? delta : `${item.text}${delta}`,
              status: "streaming",
              detail: undefined,
            }
          : item,
      ),
    );
  }

  function finishRealtimeAnswer(finalText?: string) {
    const activeId = activeAnswerIdRef.current;
    if (!activeId) {
      return;
    }

    const normalizedFinalText = finalText?.trim();
    updateAnswers((current) =>
      current.map((item) =>
        item.id === activeId
          ? {
              ...item,
              text: normalizedFinalText || (item.status === "pending" ? "本次回复已完成，但没有返回可显示文本。" : item.text),
              status: "done",
              detail: undefined,
            }
          : item,
      ),
    );
    activeAnswerIdRef.current = null;
  }

  function beginPendingRealtimeAnswer(text: string, status: RealtimeAnswer["status"] = "pending", detail?: string) {
    const activeId = activeAnswerIdRef.current;
    if (activeId) {
      updateActiveRealtimeAnswer(text, status, detail);
      return;
    }

    const id = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    activeAnswerIdRef.current = id;
    updateAnswers((current) => [
      ...current,
      {
        id,
        text,
        status,
        detail,
        timestamp: formatTime(),
      },
    ]);
  }

  function updateActiveRealtimeAnswer(text: string, status: RealtimeAnswer["status"], detail?: string) {
    const activeId = activeAnswerIdRef.current;
    if (!activeId) {
      beginPendingRealtimeAnswer(text, status, detail);
      return;
    }

    updateAnswers((current) =>
      current.map((item) =>
        item.id === activeId
          ? item.status === "streaming" && status === "pending"
            ? item
            : {
                ...item,
                text,
                status,
                detail,
              }
          : item,
      ),
    );
  }

  function markRealtimeAnswerError(detail: string) {
    updateActiveRealtimeAnswer("Realtime 生成失败。", "error", detail);
    activeAnswerIdRef.current = null;
  }

  function updateAnswers(updater: (current: RealtimeAnswer[]) => RealtimeAnswer[]) {
    setAnswers((current) => {
      const next = updater(current).slice(-MAX_REALTIME_ANSWER_ITEMS);
      answersRef.current = next;
      return next;
    });
  }

  function appendTurn(turn: TranscriptTurn) {
    if (!turn.text.trim()) {
      return;
    }

    setHistory((current) => {
      const previous = current[current.length - 1];
      const next =
        previous?.speaker === turn.speaker
          ? [
              ...current.slice(0, -1),
              {
                ...previous,
                text: mergeTranscriptText(previous.text, turn.text),
                timestamp: turn.timestamp ?? previous.timestamp,
              },
            ]
          : [...current, turn];
      return next.slice(-MAX_HISTORY_TURNS);
    });
  }

  function resetConversation() {
    setHistory([]);
    historyRef.current = [];
    setAnswers([]);
    answersRef.current = [];
    activeAnswerIdRef.current = null;
    setError(null);
  }

  function resetSpeakerSegments(speaker: Speaker) {
    speechSegmentRef.current[speaker] = { finalized: "", interim: "" };
  }

  async function togglePrivacyMode() {
    if (privacyModePending) {
      return;
    }
    const next = !privacyMode;
    if (!window.glassDesktop) {
      setPrivacyMode(next);
      return;
    }
    setPrivacyModePending(true);
    try {
      const result = await window.glassDesktop?.invoke<{ enabled: boolean }>("glass:set-privacy-mode", next);
      setPrivacyMode(Boolean(result?.enabled));
    } catch (privacyError) {
      setError(privacyError instanceof Error ? privacyError.message : "隐私模式切换失败。");
      const result = await window.glassDesktop?.invoke<{ enabled: boolean }>("glass:get-privacy-mode").catch(() => ({ enabled: false }));
      setPrivacyMode(Boolean(result?.enabled));
    } finally {
      setPrivacyModePending(false);
    }
  }

  async function toggleExpanded() {
    const next = !expanded;
    setExpanded(next);
    await window.glassDesktop?.invoke("glass:set-window-expanded", next);
  }

  function updateContextField(field: keyof CandidateContext, value: string) {
    setContext((current) => ({
      ...current,
      [field]: value,
    }));
  }

  function handleInputKeyDown(event: ReactKeyboardEvent<HTMLTextAreaElement>) {
    const modifierPressed = window.glassDesktop?.platform === "darwin" ? event.metaKey : event.ctrlKey;
    if (event.key === "Enter" && modifierPressed && !event.shiftKey) {
      event.preventDefault();
      void submitManualTurn();
    }
  }

  return (
    <main className={`glass-panel ${expanded ? "expanded" : "collapsed"} ${privacyMode ? "privacy" : ""}`}>
      <header className="control-strip app-region-drag">
        <button
          type="button"
          className={`session-action app-region-no-drag ${sessionOnline ? "active" : ""} ${error && !sessionOnline ? "error" : ""}`}
          onClick={() => void toggleSession()}
          title={sessionOnline ? "结束面试" : "开始面试"}
          aria-label={sessionOnline ? "结束面试" : "开始面试"}
          disabled={sessionStarting}
        >
          {sessionStarting ? <LoaderCircle className="spin" /> : sessionOnline ? <Square /> : <Play />}
          <span>{sessionButtonLabel}</span>
        </button>

        <div className="control-spacer" />

        <button
          type="button"
          className={`icon-action app-region-no-drag ${privacyMode ? "active" : ""}`}
          onClick={() => void togglePrivacyMode()}
          title={privacyMode ? "Show in Dock" : "Hide from Dock"}
          aria-label={privacyMode ? "Show in Dock" : "Hide from Dock"}
          disabled={privacyModePending}
        >
          {privacyMode ? <EyeOff /> : <Eye />}
        </button>
        <button
          type="button"
          className="icon-action app-region-no-drag"
          onClick={() => setShowContext(true)}
          title="Candidate context"
          aria-label="Candidate context"
        >
          <Bot />
        </button>
        <button
          type="button"
          className="icon-action app-region-no-drag"
          onClick={() => void toggleExpanded()}
          title={expanded ? "Collapse" : "Expand"}
          aria-label={expanded ? "Collapse" : "Expand"}
        >
          <ChevronRight className={expanded ? "rotate-90" : ""} />
        </button>
        <button
          type="button"
          className="icon-action app-region-no-drag"
          onClick={() => void window.glassDesktop?.invoke("glass:close")}
          title="Quit"
          aria-label="Quit"
        >
          <X />
        </button>
      </header>

      <section className="prompt-bar">
        <textarea
          ref={inputRef}
          value={input}
          onChange={(event) => setInput(event.target.value)}
          onKeyDown={handleInputKeyDown}
          placeholder={promptPlaceholder}
          rows={1}
        />
        <button
          type="button"
          className={`mode-chip ${sendMode === "question" ? "active" : ""}`}
          onClick={() => setSendMode(sendMode === "question" ? "context" : "question")}
        >
          {sendMode === "question" ? "Ask" : "Context"}
        </button>
        <button type="button" className="send-button" onClick={() => void submitManualTurn()} title="Send">
          <Send />
        </button>
      </section>

      {expanded ? (
        <>
          <QuickActions
            onAddScreen={() => void handleScreenshot(false)}
            onNewChat={resetConversation}
            onSolveScreen={() => void handleScreenshot(true)}
            onToggleTranscript={() => setShowTranscript((current) => !current)}
            showTranscript={showTranscript}
          />

          <section className="answer-stage" ref={answerFeedRef}>
            {error ? (
              <div className="error-banner">
                <span>{error}</span>
              </div>
            ) : null}

            {showTranscript ? (
              <TranscriptView turns={orderedHistory} />
            ) : answers.length > 0 ? (
              answers.map((answer) => <AnswerCard answer={answer} key={answer.id} />)
            ) : (
              <EmptyAnswerState sessionOnline={sessionOnline} />
            )}
          </section>
        </>
      ) : null}
      {showContext ? (
        <ContextSheet context={context} onClose={() => setShowContext(false)} onChange={updateContextField} />
      ) : null}
      {privacyMode ? <div className="privacy-ring" aria-hidden="true" /> : null}
    </main>
  );
}

function QuickActions({
  onAddScreen,
  onNewChat,
  onSolveScreen,
  onToggleTranscript,
  showTranscript,
}: {
  onAddScreen: () => void;
  onNewChat: () => void;
  onSolveScreen: () => void;
  onToggleTranscript: () => void;
  showTranscript: boolean;
}) {
  return (
    <section className="quick-actions" aria-label="Interview tools">
      <button type="button" className="quick-action solve" onClick={onSolveScreen}>
        <Camera />
        <span>Solve screen</span>
      </button>
      <div className="quiet-actions">
        <button type="button" className="quick-action quiet" onClick={onAddScreen} title="Add screen" aria-label="Add screen">
          <Monitor />
          <span>Add screen</span>
        </button>
        <button
          type="button"
          className={`quick-action quiet ${showTranscript ? "active" : ""}`}
          onClick={onToggleTranscript}
          title={showTranscript ? "Show answers" : "Show transcript"}
          aria-label={showTranscript ? "Show answers" : "Show transcript"}
        >
          <MessageSquare />
          <span>{showTranscript ? "Answers" : "Transcript"}</span>
        </button>
        <button type="button" className="quick-action quiet icon-only" onClick={onNewChat} title="New chat" aria-label="New chat">
          <RotateCcw />
        </button>
      </div>
    </section>
  );
}

function EmptyAnswerState({ sessionOnline }: { sessionOnline: boolean }) {
  return (
    <div className="empty-state">
      <h1>{sessionOnline ? "Listening" : "Ready"}</h1>
      <p>{sessionOnline ? "Interview audio is live. The next answer will land here." : "Waiting for the next prompt."}</p>
    </div>
  );
}

function AnswerCard({ answer }: { answer: RealtimeAnswer }) {
  return (
    <article className={`answer-card ${answer.status}`}>
      <div className="answer-meta">
        <span>{answer.timestamp}</span>
        <span className="answer-state">
          {answer.status === "done" ? <Check /> : answer.status === "streaming" ? <LoaderCircle className="spin" /> : null}
          {answer.status}
        </span>
      </div>
      <p>{answer.text}</p>
      {answer.detail ? <small>{answer.detail}</small> : null}
    </article>
  );
}

function TranscriptView({ turns }: { turns: TranscriptTurn[] }) {
  if (turns.length === 0) {
    return (
      <div className="empty-state compact">
        <h1>No transcript yet</h1>
        <p>Realtime transcript appears here as interviewer and candidate audio arrive.</p>
      </div>
    );
  }

  return (
    <div className="transcript-list">
      {turns.map((turn, index) => (
        <article className={`transcript-row ${turn.speaker}`} key={`${turn.timestamp ?? index}-${turn.speaker}`}>
          <time>{turn.timestamp}</time>
          <div>
            <span>{turn.speaker === "interviewer" ? "Interviewer" : "Candidate"}</span>
            <p>{turn.text}</p>
          </div>
        </article>
      ))}
    </div>
  );
}

function ContextSheet({
  context,
  onChange,
  onClose,
}: {
  context: CandidateContext;
  onChange: (field: keyof CandidateContext, value: string) => void;
  onClose: () => void;
}) {
  return (
    <aside className="context-sheet">
      <div className="sheet-header">
        <div>
          <h2>Candidate Context</h2>
          <p>Sent with every Realtime session start.</p>
        </div>
        <button type="button" className="icon-action" onClick={onClose} aria-label="Close context">
          <X />
        </button>
      </div>
      <label>
        Name
        <input value={context.name} onChange={(event) => onChange("name", event.target.value)} />
      </label>
      <label>
        Target role
        <input value={context.target_role} onChange={(event) => onChange("target_role", event.target.value)} />
      </label>
      <label>
        Resume
        <textarea value={context.resume} onChange={(event) => onChange("resume", event.target.value)} rows={4} />
      </label>
      <label>
        Job description
        <textarea value={context.job_description} onChange={(event) => onChange("job_description", event.target.value)} rows={4} />
      </label>
      <label>
        Notes
        <textarea value={context.custom_notes} onChange={(event) => onChange("custom_notes", event.target.value)} rows={4} />
      </label>
    </aside>
  );
}

function loadStoredContext(): CandidateContext {
  try {
    const raw = window.localStorage.getItem("sage-glass-context");
    if (!raw) {
      return initialContext;
    }
    return {
      ...initialContext,
      ...JSON.parse(raw),
    };
  } catch {
    return initialContext;
  }
}

function getRealtimeSocketBaseUrl(baseUrl: string) {
  if (baseUrl.startsWith("https://")) {
    return baseUrl.replace(/^https:\/\//, "wss://");
  }
  if (baseUrl.startsWith("http://")) {
    return baseUrl.replace(/^http:\/\//, "ws://");
  }
  return baseUrl;
}

function resolveApiBaseUrl(baseUrl: string | undefined, localApiEnabled: boolean | undefined) {
  if (!baseUrl?.trim()) {
    return "";
  }
  return isLocalApiBaseUrl(baseUrl) && !localApiEnabled ? "" : baseUrl.trim();
}

function isLocalApiBaseUrl(baseUrl: string) {
  try {
    const url = new URL(baseUrl);
    return ["127.0.0.1", "localhost"].includes(url.hostname);
  } catch {
    return false;
  }
}

function formatRealtimeError(detail: string) {
  if (/OPENAI_API_KEY/i.test(detail)) {
    return `服务器未配置 OPENAI_API_KEY，无法启动 Realtime。当前连接：${API_BASE_URL}。`;
  }
  return detail;
}

function mergeTranscriptText(previous: string, next: string) {
  const trimmedNext = next.trim();
  if (!previous.trim()) {
    return trimmedNext;
  }
  if (!trimmedNext) {
    return previous.trim();
  }
  if (/^[,.!?;:，。！？；：]/.test(trimmedNext)) {
    return `${previous.trim()}${trimmedNext}`;
  }
  return `${previous.trim()} ${trimmedNext}`;
}

function truncate(text: string) {
  const normalized = text.replace(/\s+/g, " ").trim();
  return normalized.length > 42 ? `${normalized.slice(0, 42)}...` : normalized;
}

function formatTime() {
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date());
}
