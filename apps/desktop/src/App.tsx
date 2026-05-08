import { KeyboardEvent as ReactKeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import {
  getCaptureLabel,
  requestCaptureStream,
  startLocalAudioCapture,
  type AudioCaptureHandle,
} from "./audioCapture";
import { joinChunk } from "./liveTranscript";
import type {
  CandidateContext,
  RealtimeAnswer,
  Speaker,
  TranscriptTurn,
} from "./types";

const API_BASE_URL =
  window.interviewDesktop?.apiBaseUrl ||
  import.meta.env.VITE_API_BASE_URL ||
  "http://127.0.0.1:8000";
const AUTO_FLUSH_MS: Record<Speaker, number> = {
  interviewer: 4200,
  candidate: 2200,
};
const CAPTURE_CHUNK_MS: Record<Speaker, number> = {
  interviewer: 4800,
  candidate: 3200,
};
const CAPTURE_MIN_LEVEL: Record<Speaker, number> = {
  interviewer: 0.012,
  candidate: 0.006,
};
const MIN_AUDIO_CHUNK_BYTES = 2048;
const MAX_HISTORY_TURNS = 100;
const MAX_REALTIME_ANSWER_ITEMS = 32;
const SOCKET_RECONNECT_DELAYS_MS = [1000, 2000, 5000];

const initialContext: CandidateContext = {
  name: "",
  target_role: "后端开发工程师",
  resume: "做过内部数据服务平台，负责过 SQL 优化、缓存链路和后端接口治理。",
  job_description: "要求 SQL、Redis、分布式基础和项目表达能力都比较强。",
  custom_notes: "回答尽量简洁，优先突出结果、取舍和系统设计思路。",
};

const initialHistory: TranscriptTurn[] = [];

interface RenderedTranscriptTurn extends TranscriptTurn {
  key: string;
  statusLabel?: string;
}

interface PreparedTurn {
  turn: TranscriptTurn | null;
  punctuationAttached: boolean;
}

interface RealtimeMessage {
  type?: string;
  text?: string;
  delta?: string;
  detail?: string;
  request_id?: string;
  reason?: string;
  status?: RealtimeAnswer["status"];
  is_final?: boolean;
  speech_final?: boolean;
  speaker?: Speaker;
}

type ContextFileField = "resume" | "job_description" | "custom_notes";

export default function App() {
  const [context, setContext] = useState<CandidateContext>(initialContext);
  const [history, setHistory] = useState<TranscriptTurn[]>(initialHistory);
  const [realtimeAnswers, setRealtimeAnswers] = useState<RealtimeAnswer[]>([]);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [captureStartState, setCaptureStartState] = useState<"idle" | "starting" | "started">("idle");
  const [captureStartDots, setCaptureStartDots] = useState(0);
  const [input, setInput] = useState("");
  const [selectedSpeaker, setSelectedSpeaker] = useState<Speaker>("interviewer");
  const [buffers, setBuffers] = useState<Record<Speaker, string>>({ interviewer: "", candidate: "" });
  const [captureActive, setCaptureActive] = useState<Record<Speaker, boolean>>({ interviewer: false, candidate: false });
  const [realtimeSocketActive, setRealtimeSocketActive] = useState<Record<Speaker, boolean>>({
    interviewer: false,
    candidate: false,
  });
  const [captureMessage, setCaptureMessage] = useState<Record<Speaker, string>>({
    interviewer: "待机",
    candidate: "待机",
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const answerFeedRef = useRef<HTMLDivElement | null>(null);
  const chatListRef = useRef<HTMLDivElement | null>(null);
  const realtimeAnswersRef = useRef<RealtimeAnswer[]>([]);
  const activeRealtimeAnswerIdRef = useRef<string | null>(null);
  const historyRef = useRef<TranscriptTurn[]>(initialHistory);
  const captureMessageRef = useRef<Record<Speaker, string>>({
    interviewer: "待机",
    candidate: "待机",
  });
  const bufferRef = useRef<Record<Speaker, string>>({ interviewer: "", candidate: "" });
  const speechSegmentRef = useRef<Record<Speaker, { finalized: string; interim: string }>>({
    interviewer: { finalized: "", interim: "" },
    candidate: { finalized: "", interim: "" },
  });
  const activeSpeakerRef = useRef<Speaker | null>(null);
  const transcriptionSocketRef = useRef<Partial<Record<Speaker, WebSocket>>>({});
  const flushTimersRef = useRef<Partial<Record<Speaker, number>>>({});
  const processingQueueRef = useRef<Promise<void>>(Promise.resolve());
  const captureHandlesRef = useRef<Partial<Record<Speaker, AudioCaptureHandle>>>({});
  const reconnectTimersRef = useRef<Partial<Record<Speaker, number>>>({});
  const reconnectAttemptsRef = useRef<Record<Speaker, number>>({ interviewer: 0, candidate: 0 });
  const manualStopRef = useRef<Record<Speaker, boolean>>({ interviewer: false, candidate: false });
  const contextRef = useRef<CandidateContext>(initialContext);

  const renderedHistory = useMemo(() => buildRenderedHistory(history, buffers), [history, buffers]);
  const sessionOnline =
    captureActive.candidate ||
    captureActive.interviewer ||
    realtimeSocketActive.candidate ||
    realtimeSocketActive.interviewer;
  const startButtonLabel =
    captureStartState === "starting"
      ? `开始中${".".repeat(captureStartDots + 1)}`
      : sessionOnline
        ? "结束对话"
        : "开始对话";

  useEffect(() => {
    historyRef.current = history;
  }, [history]);

  useEffect(() => {
    captureMessageRef.current = captureMessage;
  }, [captureMessage]);

  useEffect(() => {
    realtimeAnswersRef.current = realtimeAnswers;
  }, [realtimeAnswers]);

  useEffect(() => {
    contextRef.current = context;
  }, [context]);

  useEffect(() => {
    if (captureStartState !== "starting") {
      setCaptureStartDots(0);
      return;
    }

    const timerId = window.setInterval(() => {
      setCaptureStartDots((current) => (current + 1) % 3);
    }, 360);

    return () => window.clearInterval(timerId);
  }, [captureStartState]);

  useEffect(() => {
    if (!settingsOpen) {
      return;
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setSettingsOpen(false);
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [settingsOpen]);

  useEffect(() => {
    const container = answerFeedRef.current;
    if (container) {
      container.scrollTop = container.scrollHeight;
    }
  }, [realtimeAnswers]);

  useEffect(() => {
    const container = chatListRef.current;
    if (container) {
      container.scrollTop = container.scrollHeight;
    }
  }, [renderedHistory]);

  useEffect(() => {
    return () => {
      stopInterviewCapture();
      Object.values(flushTimersRef.current).forEach((timerId) => {
        if (timerId) {
          window.clearTimeout(timerId);
        }
      });
      Object.values(reconnectTimersRef.current).forEach((timerId) => {
        if (timerId) {
          window.clearTimeout(timerId);
        }
      });
    };
  }, []);

  async function submitManualTurn(speaker: Speaker) {
    const text = input.trim();
    if (!text) {
      return;
    }

    setLoading(true);
    setError(null);
    try {
      const socket = await ensureRealtimeSocket(speaker);
      if (speaker === "interviewer") {
        beginPendingRealtimeAnswer("已发送到 Realtime，等待本次回复开始生成...");
        socket.send(JSON.stringify({ type: "manual_text", text }));
        appendTurn({ speaker, text, timestamp: formatTime() });
      } else {
        socket.send(JSON.stringify({ type: "manual_text", text }));
        appendTurn({ speaker, text, timestamp: formatTime() });
      }
      setInput("");
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "提交发言失败");
    } finally {
      setLoading(false);
    }
  }

  function updateContextField(field: keyof CandidateContext, value: string) {
    setContext((current) => ({
      ...current,
      [field]: value,
    }));
  }

  function handleSpeakerChange(nextSpeaker: Speaker) {
    setSelectedSpeaker(nextSpeaker);
  }

  async function handleCaptureToggle() {
    if (captureStartState === "starting") {
      return;
    }

    if (sessionOnline) {
      stopInterviewCapture();
      setCaptureStartState("idle");
    } else {
      setCaptureStartState("starting");
      const started = await startInterviewCapture();
      setCaptureStartState(started ? "started" : "idle");
    }
  }

  async function importContextFile(field: ContextFileField, fileList: FileList | null) {
    const file = fileList?.[0];
    if (!file) {
      return;
    }

    const isTextLike =
      file.type.startsWith("text/") ||
      /\.(txt|md|markdown|json|csv|log)$/i.test(file.name);

    if (!isTextLike) {
      setError("个人资料导入目前支持 txt、md、json、csv、log 这类文本文件。");
      return;
    }

    try {
      const content = await file.text();
      setContext((current) => ({
        ...current,
        [field]: content,
      }));
      setError(null);
    } catch (readError) {
      setError(readError instanceof Error ? readError.message : "读取文件失败。");
    }
  }

  async function startSpeakerCapture(speaker: Speaker): Promise<boolean> {
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
        chunkDurationMs: CAPTURE_CHUNK_MS[speaker],
        minLevel: CAPTURE_MIN_LEVEL[speaker],
        onChunk: (chunk) => sendAudioChunk(speaker, chunk),
      });

      captureHandlesRef.current[speaker] = handle;
      reconnectAttemptsRef.current[speaker] = 0;
      setCaptureActive((current) => ({ ...current, [speaker]: true }));
      captureMessageRef.current = { ...captureMessageRef.current, [speaker]: "监听中" };
      setCaptureMessage((current) => ({
        ...current,
        [speaker]: "监听中",
      }));
      return true;
    } catch (captureError) {
      const message = captureError instanceof Error ? captureError.message : "音频采集启动失败";
      captureMessageRef.current = { ...captureMessageRef.current, [speaker]: message };
      setCaptureMessage((current) => ({ ...current, [speaker]: message }));
      return false;
    }
  }

  async function startInterviewCapture() {
    if (captureHandlesRef.current.candidate || captureHandlesRef.current.interviewer) {
      return true;
    }

    setError(null);
    const micStarted = await startSpeakerCapture("candidate");
    const systemStarted = await startSpeakerCapture("interviewer");

    if (!micStarted && !systemStarted) {
      setError(
        `麦克风和系统音频都没有启动成功。麦克风：${captureMessageRef.current.candidate}；系统音频：${captureMessageRef.current.interviewer}`,
      );
      return false;
    }
    if (micStarted && !systemStarted) {
      setError(`麦克风已启动，但系统音频启动失败：${captureMessageRef.current.interviewer}`);
      return true;
    }
    if (!micStarted && systemStarted) {
      setError(`系统音频已启动，但麦克风启动失败：${captureMessageRef.current.candidate}`);
      return true;
    }

    return true;
  }

  function stopInterviewCapture() {
    stopSpeakerCapture("candidate");
    stopSpeakerCapture("interviewer");
  }

  function stopSpeakerCapture(speaker: Speaker) {
    const handle = captureHandlesRef.current[speaker];
    const socket = transcriptionSocketRef.current[speaker];
    if (!handle && !socket) {
      return;
    }

    manualStopRef.current[speaker] = true;
    reconnectAttemptsRef.current[speaker] = 0;
    clearReconnectTimer(speaker);
    handle?.stop();
    delete captureHandlesRef.current[speaker];
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: "finalize" }));
      socket.send(JSON.stringify({ type: "close" }));
      socket.close();
    }
    delete transcriptionSocketRef.current[speaker];
    setRealtimeSocketActive((current) => ({ ...current, [speaker]: false }));
    setCaptureActive((current) => ({ ...current, [speaker]: false }));
    setCaptureMessage((current) => ({
      ...current,
      [speaker]: "待机",
    }));
    flushSpeakerBuffer(speaker, true);
    resetSpeakerSegments(speaker);
  }

  async function openRealtimeTranscriptionSocket(speaker: Speaker) {
    return await new Promise<WebSocket>((resolve, reject) => {
      const socket = new WebSocket(
        `${getRealtimeSocketBaseUrl(API_BASE_URL)}/ws/realtime/interview/${speaker}`,
      );
      let resolved = false;

      const timeoutId = window.setTimeout(() => {
        socket.close();
        reject(new Error(`${getCaptureLabel(speaker)}实时转写连接超时。`));
      }, 10000);

      socket.addEventListener("open", () => {
        socket.send(JSON.stringify(buildRealtimeStartPayload()));
        setCaptureMessage((current) => ({
          ...current,
          [speaker]: "连接中",
        }));
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
            setRealtimeSocketActive((current) => ({ ...current, [speaker]: true }));
            resolve(socket);
          }
          return;
        }

        handleRealtimeSocketMessage(speaker, payload);
      });

      socket.addEventListener("error", () => {
        if (!resolved) {
          window.clearTimeout(timeoutId);
          reject(new Error(`${getCaptureLabel(speaker)}实时转写连接失败。`));
        } else {
          setError(`${getCaptureLabel(speaker)}实时转写连接异常。`);
        }
      });

      socket.addEventListener("close", () => {
        if (transcriptionSocketRef.current[speaker] === socket) {
          delete transcriptionSocketRef.current[speaker];
          setRealtimeSocketActive((current) => ({ ...current, [speaker]: false }));
        }
        if (resolved && captureHandlesRef.current[speaker] && !manualStopRef.current[speaker]) {
          scheduleSocketReconnect(speaker);
        }
      });
    });
  }

  function scheduleSocketReconnect(speaker: Speaker) {
    if (manualStopRef.current[speaker] || !captureHandlesRef.current[speaker]) {
      return;
    }
    if (reconnectTimersRef.current[speaker]) {
      return;
    }

    const attempt = reconnectAttemptsRef.current[speaker];
    const delay =
      SOCKET_RECONNECT_DELAYS_MS[Math.min(attempt, SOCKET_RECONNECT_DELAYS_MS.length - 1)];
    reconnectAttemptsRef.current[speaker] = attempt + 1;
    setCaptureMessage((current) => ({
      ...current,
      [speaker]: "重连中",
    }));

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
      const socket = await openRealtimeTranscriptionSocket(speaker);
      if (manualStopRef.current[speaker] || !captureHandlesRef.current[speaker]) {
        socket.close();
        return;
      }
      transcriptionSocketRef.current[speaker] = socket;
      setCaptureMessage((current) => ({
        ...current,
        [speaker]: "监听中",
      }));
    } catch (reconnectError) {
      const message = reconnectError instanceof Error ? reconnectError.message : `${getCaptureLabel(speaker)}重连失败。`;
      setError(message);
      setCaptureMessage((current) => ({
        ...current,
        [speaker]: "重连中",
      }));
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

  function handleRealtimeSocketMessage(speaker: Speaker, payload: RealtimeMessage) {
    if (payload.type === "error") {
      const detail = payload.detail ?? `${getCaptureLabel(speaker)}实时转写失败。`;
      setError(detail);
      setCaptureMessage((current) => ({
        ...current,
        [speaker]: detail,
      }));
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
        activeRealtimeAnswerIdRef.current = null;
        return;
      }
      finishRealtimeAnswer(payload.text);
      return;
    }

    if (payload.type === "context_update" && payload.text?.trim()) {
      appendTurn({ speaker: "candidate", text: payload.text.trim(), timestamp: formatTime() });
      setCaptureMessage((current) => ({
        ...current,
        candidate: `上下文：${truncatePreview(payload.text ?? "")}`,
      }));
      return;
    }

    if (payload.type !== "transcript" || !payload.text?.trim()) {
      return;
    }

    if (activeSpeakerRef.current && activeSpeakerRef.current !== speaker) {
      flushSpeakerBuffer(activeSpeakerRef.current, true);
    }

    activeSpeakerRef.current = speaker;
    const state = speechSegmentRef.current[speaker];
    if (payload.is_final) {
      state.finalized = mergeTranscriptText(state.finalized, payload.text);
      state.interim = "";
    } else {
      state.interim = payload.text.trim();
    }

    const nextBuffer = mergeTranscriptText(state.finalized, state.interim);
    bufferRef.current = { ...bufferRef.current, [speaker]: nextBuffer };
    setBuffers(bufferRef.current);
    setCaptureMessage((current) => ({
      ...current,
      [speaker]: `识别中：${truncatePreview(nextBuffer)}`,
    }));

    if (payload.speech_final) {
      flushSpeakerBuffer(speaker, true);
    }
  }

  function sendAudioChunk(speaker: Speaker, chunk: ArrayBuffer) {
    if (chunk.byteLength < MIN_AUDIO_CHUNK_BYTES) {
      return;
    }

    const socket = transcriptionSocketRef.current[speaker];
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return;
    }
    socket.send(chunk);
  }

  function sendRealtimeControl(speaker: Speaker, payload: Record<string, unknown>) {
    const socket = transcriptionSocketRef.current[speaker];
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return;
    }
    socket.send(JSON.stringify(payload));
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

    const activeId = activeRealtimeAnswerIdRef.current;
    if (!activeId) {
      beginPendingRealtimeAnswer(delta, "streaming");
      return;
    }

    updateRealtimeAnswers((current) =>
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
    const activeId = activeRealtimeAnswerIdRef.current;
    if (!activeId) {
      return;
    }

    const normalizedFinalText = finalText?.trim();
    updateRealtimeAnswers((current) =>
      current.map((item) =>
        item.id === activeId
          ? {
              ...item,
              text:
                normalizedFinalText ||
                (item.status === "pending" ? "本次回复已完成，但没有返回可显示文本。" : item.text),
              status: "done",
              detail: undefined,
            }
          : item,
      ),
    );
    activeRealtimeAnswerIdRef.current = null;
  }

  function updateRealtimeAnswers(updater: (current: RealtimeAnswer[]) => RealtimeAnswer[]) {
    setRealtimeAnswers((current) => {
      const next = clampRealtimeAnswerItems(updater(current));
      realtimeAnswersRef.current = next;
      return next;
    });
  }

  function beginPendingRealtimeAnswer(text: string, status: RealtimeAnswer["status"] = "pending", detail?: string) {
    const activeId = activeRealtimeAnswerIdRef.current;
    if (activeId) {
      updateActiveRealtimeAnswer(text, status, detail);
      return;
    }

    const id = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    activeRealtimeAnswerIdRef.current = id;
    updateRealtimeAnswers((current) => [
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
    const activeId = activeRealtimeAnswerIdRef.current;
    if (!activeId) {
      beginPendingRealtimeAnswer(text, status, detail);
      return;
    }

    updateRealtimeAnswers((current) =>
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
    activeRealtimeAnswerIdRef.current = null;
  }

  async function ensureRealtimeSocket(speaker: Speaker) {
    const existingSocket = transcriptionSocketRef.current[speaker];
    if (existingSocket && existingSocket.readyState === WebSocket.OPEN) {
      return existingSocket;
    }

    manualStopRef.current[speaker] = false;
    clearReconnectTimer(speaker);
    const socket = await openRealtimeTranscriptionSocket(speaker);
    transcriptionSocketRef.current[speaker] = socket;
    captureMessageRef.current = { ...captureMessageRef.current, [speaker]: "文本连接中" };
    setCaptureMessage((current) => ({
      ...current,
      [speaker]: "文本连接中",
    }));
    return socket;
  }

  async function handleScreenshot(createResponse: boolean) {
    if (!sessionOnline) {
      setError("请先开始 Realtime 对话，再发送截图。");
      return;
    }

    try {
      const imageUrl = await captureCurrentScreenDataUrl();
      sendRealtimeControl("interviewer", {
        type: "screenshot",
        image_url: imageUrl,
        create_response: createResponse,
        prompt: createResponse
          ? "这是我刚截的面试题、白板或代码截图，请结合当前问题给出可以直接口述的中文回答。"
          : "这是我刚截的面试题、白板或代码截图，请作为后续回答上下文。",
      });
      setError(null);
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

  function ingestLiveChunk(speaker: Speaker, chunkText: string) {
    if (activeSpeakerRef.current && activeSpeakerRef.current !== speaker) {
      flushSpeakerBuffer(activeSpeakerRef.current, true);
    }

    activeSpeakerRef.current = speaker;
    const nextBuffer = joinChunk(bufferRef.current[speaker], chunkText);
    bufferRef.current = { ...bufferRef.current, [speaker]: nextBuffer };
    setBuffers(bufferRef.current);
    scheduleAutoFlush(speaker);
  }

  function scheduleAutoFlush(speaker: Speaker) {
    const existingTimer = flushTimersRef.current[speaker];
    if (existingTimer) {
      window.clearTimeout(existingTimer);
    }
    flushTimersRef.current[speaker] = window.setTimeout(() => {
      flushSpeakerBuffer(speaker);
    }, AUTO_FLUSH_MS[speaker]);
  }

  function flushSpeakerBuffer(speaker: Speaker, force = false) {
    const buffer = bufferRef.current[speaker].trim();
    const timerId = flushTimersRef.current[speaker];
    if (timerId) {
      window.clearTimeout(timerId);
      flushTimersRef.current[speaker] = undefined;
    }

    if (!buffer) {
      if (captureHandlesRef.current[speaker]) {
        setCaptureMessage((current) => ({
          ...current,
          [speaker]: "监听中",
        }));
      }
      return;
    }

    if (!force && speaker === "interviewer" && shouldHoldInterviewerBuffer(buffer)) {
      scheduleAutoFlush(speaker);
      return;
    }

    bufferRef.current = { ...bufferRef.current, [speaker]: "" };
    setBuffers(bufferRef.current);
    activeSpeakerRef.current = null;
    resetSpeakerSegments(speaker);

    if (captureHandlesRef.current[speaker]) {
      setCaptureMessage((current) => ({
        ...current,
        [speaker]: "监听中",
      }));
    }

    enqueueTurn(speaker, buffer);
  }

  function flushAllBuffers() {
    flushSpeakerBuffer("interviewer", true);
    flushSpeakerBuffer("candidate", true);
  }

  function resetSpeakerSegments(speaker: Speaker) {
    speechSegmentRef.current[speaker] = { finalized: "", interim: "" };
  }

  function resetLiveBuffers() {
    bufferRef.current = { interviewer: "", candidate: "" };
    speechSegmentRef.current = {
      interviewer: { finalized: "", interim: "" },
      candidate: { finalized: "", interim: "" },
    };
    setBuffers(bufferRef.current);
    activeSpeakerRef.current = null;
  }

  function enqueueTurn(speaker: Speaker, text: string) {
    const turn: TranscriptTurn = { speaker, text, timestamp: formatTime() };
    processingQueueRef.current = processingQueueRef.current
      .then(async () => {
        if (speaker === "candidate") {
          appendTurn(turn);
          return;
        }

        handleCommittedTurn(turn);
      })
      .catch((queueError) => {
        setError(queueError instanceof Error ? queueError.message : "对话片段处理失败。");
      });
  }

  function handleCommittedTurn(turn: TranscriptTurn) {
    appendTurn(turn);
  }

  function appendTurn(turn: TranscriptTurn): PreparedTurn {
    const preparedPunctuation = detachLeadingPunctuation(turn, historyRef.current);
    if (preparedPunctuation.history) {
      const clampedPreparedHistory = clampHistoryTurns(preparedPunctuation.history);
      historyRef.current = clampedPreparedHistory;
      setHistory(clampedPreparedHistory);
    }

    if (!preparedPunctuation.turn) {
      return { turn: null, punctuationAttached: true };
    }

    const normalizedTurn = preparedPunctuation.turn;
    const previousTurn = historyRef.current[historyRef.current.length - 1];
    const shouldMergeSpeakerTurn = previousTurn?.speaker === normalizedTurn.speaker;

    let nextHistory: TranscriptTurn[];
    if (shouldMergeSpeakerTurn) {
      nextHistory = [
        ...historyRef.current.slice(0, -1),
        {
          ...previousTurn,
          text: mergeTranscriptText(previousTurn.text, normalizedTurn.text),
          timestamp: normalizedTurn.timestamp ?? previousTurn.timestamp,
        },
      ];
    } else {
      nextHistory = [...historyRef.current, normalizedTurn];
    }

    const clampedHistory = clampHistoryTurns(nextHistory);
    historyRef.current = clampedHistory;
    setHistory(clampedHistory);
    return { turn: normalizedTurn, punctuationAttached: normalizedTurn.text !== turn.text };
  }

  function resetAnswerFeed() {
    realtimeAnswersRef.current = [];
    activeRealtimeAnswerIdRef.current = null;
    setRealtimeAnswers([]);
  }

  return (
    <div className="app-shell">
      <div className="ambient ambient-left" />
      <div className="ambient ambient-right" />

      <div className="nav-strip">
        <div className="nav-strip-spacer" />
      </div>

      <main className="layout locked">
        <section className="panel transcript-panel live-mode">
          <div className="panel-head live-panel-head">
            <div className="live-panel-actions">
              <div className="answer-mode-actions">
                <button
                  type="button"
                  className="session-start-button panel-start-button answer-mode-button project"
                  onClick={() => void handleScreenshot(false)}
                  disabled={!sessionOnline}
                  title="截图并加入上下文"
                >
                  截图上下文
                </button>
                <button
                  type="button"
                  className="session-start-button panel-start-button answer-mode-button project"
                  onClick={() => void handleScreenshot(true)}
                  disabled={!sessionOnline}
                  title="截图并回答"
                >
                  截图回答
                </button>
              </div>
              <button
                type="button"
                className={`session-start-button panel-start-button ${captureStartState === "started" ? "active" : ""} ${captureStartState === "starting" ? "starting" : ""}`}
                onClick={handleCaptureToggle}
                aria-label={startButtonLabel}
                title={startButtonLabel}
                disabled={captureStartState === "starting"}
              >
                {startButtonLabel}
              </button>
            </div>
          </div>

          <div className="chat-stage">
            <div className="chat-list" ref={chatListRef}>
              {renderedHistory.map((item) => (
                <div className={`chat-row ${item.speaker}`} key={item.key}>
                  <article className={`bubble ${isMultiLineBubble(item.text) ? "multiline" : ""}`.trim()}>
                    {item.statusLabel ? <div className="bubble-meta">{item.statusLabel}</div> : null}
                    <p>{item.text}</p>
                  </article>
                </div>
              ))}
            </div>
          </div>

          <SessionDock
            settingsOpen={settingsOpen}
            onToggleSettings={() => setSettingsOpen((current) => !current)}
            onSelectSpeaker={handleSpeakerChange}
            selectedSpeaker={selectedSpeaker}
            captureActive={captureActive}
            captureMessage={captureMessage}
            input={input}
            onInputChange={setInput}
            loading={loading}
            onSubmitInput={() => void submitManualTurn(selectedSpeaker)}
          />

          {error ? <p className="error-text">{error}</p> : null}
        </section>

        <section className="panel answers-panel">
          <div className="panel-head sticky">
            <div>
              <p className="panel-kicker">答案</p>
              <h2>实时答案</h2>
            </div>
          </div>

          <div className="answer-feed" ref={answerFeedRef}>
            {realtimeAnswers.length > 0 ? (
              realtimeAnswers.map((item) => (
                <article className={`answer-card realtime-answer-card ${item.status}`} key={item.id}>
                  <div className="answer-card-head">
                    <span className="timestamp">{item.timestamp}</span>
                  </div>

                  <p className="realtime-answer-text">{item.text}</p>
                  {item.detail ? <p className="realtime-answer-status">{item.detail}</p> : null}
                  <p className="realtime-answer-status">
                    {item.status === "pending"
                      ? "等待本次回复..."
                      : item.status === "streaming"
                        ? "本次回复生成中..."
                        : item.status === "error"
                          ? "本次回复失败"
                          : "本次回复完成，Realtime 会话仍在监听"}
                  </p>
                </article>
              ))
            ) : (
              <div className="empty-card">
                <p>还没有回答卡片</p>
              </div>
            )}
          </div>
        </section>
      </main>

      {settingsOpen ? (
        <>
          <button
            type="button"
            className="settings-backdrop"
            aria-label="关闭设置"
            onClick={() => setSettingsOpen(false)}
          />
          <aside className="settings-drawer" id="settings-drawer" aria-label="设置">
            <div className="settings-drawer-head">
              <div>
                <p className="panel-kicker">PROFILE / SETTINGS</p>
                <h2>个人资料</h2>
              </div>
              <button type="button" className="settings-close-button" onClick={() => setSettingsOpen(false)}>
                关闭
              </button>
            </div>

            <ContextSettingsPanel
              context={context}
              onContextChange={updateContextField}
              onImportContextFile={importContextFile}
            />
          </aside>
        </>
      ) : null}
    </div>
  );
}

function ContextSettingsPanel({
  context,
  onContextChange,
  onImportContextFile,
}: {
  context: CandidateContext;
  onContextChange: (field: keyof CandidateContext, value: string) => void;
  onImportContextFile: (field: ContextFileField, fileList: FileList | null) => Promise<void>;
}) {
  return (
    <div className="settings-panel-content">
      <div className="context-grid settings-context-grid">
        <label className="context-field">
          <span>姓名</span>
          <input value={context.name} onChange={(event) => onContextChange("name", event.target.value)} />
        </label>
        <label className="context-field">
          <span>目标岗位</span>
          <input value={context.target_role} onChange={(event) => onContextChange("target_role", event.target.value)} />
        </label>
        <label className="context-field wide">
          <span>简历</span>
          <textarea
            rows={7}
            value={context.resume}
            onChange={(event) => onContextChange("resume", event.target.value)}
            placeholder="粘贴你的项目经历、指标和职责。"
          />
          <div className="file-import-row">
            <label className="file-import-button">
              导入文件
              <input
                type="file"
                accept=".txt,.md,.markdown,.json,.csv,.log,text/plain,text/markdown,application/json,text/csv"
                onChange={(event) => void onImportContextFile("resume", event.target.files)}
              />
            </label>
            <span>支持 txt / md / json / csv / log</span>
          </div>
        </label>
        <label className="context-field wide">
          <span>岗位描述</span>
          <textarea
            rows={7}
            value={context.job_description}
            onChange={(event) => onContextChange("job_description", event.target.value)}
            placeholder="粘贴目标职位 JD。"
          />
          <div className="file-import-row">
            <label className="file-import-button">
              导入文件
              <input
                type="file"
                accept=".txt,.md,.markdown,.json,.csv,.log,text/plain,text/markdown,application/json,text/csv"
                onChange={(event) => void onImportContextFile("job_description", event.target.files)}
              />
            </label>
            <span>支持 txt / md / json / csv / log</span>
          </div>
        </label>
        <label className="context-field wide full">
          <span>补充备注</span>
          <textarea
            rows={6}
            value={context.custom_notes}
            onChange={(event) => onContextChange("custom_notes", event.target.value)}
            placeholder="写清楚你希望 AI 优先强调的项目取舍、系统复杂度和结果。"
          />
          <div className="file-import-row">
            <label className="file-import-button">
              导入文件
              <input
                type="file"
                accept=".txt,.md,.markdown,.json,.csv,.log,text/plain,text/markdown,application/json,text/csv"
                onChange={(event) => void onImportContextFile("custom_notes", event.target.files)}
              />
            </label>
            <span>支持 txt / md / json / csv / log</span>
          </div>
        </label>
      </div>
    </div>
  );
}

function SessionDock({
  settingsOpen,
  onToggleSettings,
  onSelectSpeaker,
  selectedSpeaker,
  captureActive,
  captureMessage,
  input,
  onInputChange,
  loading,
  onSubmitInput,
}: {
  settingsOpen: boolean;
  onToggleSettings: () => void;
  onSelectSpeaker: (speaker: Speaker) => void;
  selectedSpeaker: Speaker;
  captureActive: Record<Speaker, boolean>;
  captureMessage: Record<Speaker, string>;
  input: string;
  onInputChange: (value: string) => void;
  loading: boolean;
  onSubmitInput: () => void;
}) {
  const speakerLabel = selectedSpeaker === "interviewer" ? "面试官" : "你";
  const placeholder = selectedSpeaker === "interviewer" ? "输入面试问题..." : "输入你的回答...";
  const canSend = !loading && Boolean(input.trim());

  function handleKeyDown(event: ReactKeyboardEvent<HTMLInputElement>) {
    if (event.key !== "Enter") {
      return;
    }
    event.preventDefault();
    if (!canSend) {
      return;
    }
    onSubmitInput();
  }

  return (
    <div className="session-dock-wrap">
      <div className="session-dock-card">
        <div className="session-dock session-dock-input">
          <div className="session-dock-top-left session-inline-speaker">
            <button
              type="button"
              className="session-speaker-switch"
              onClick={() => onSelectSpeaker(selectedSpeaker === "interviewer" ? "candidate" : "interviewer")}
            >
              切换
            </button>
          </div>

          <div className="session-dock-field">
            <input
              value={input}
              onChange={(event) => onInputChange(event.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={placeholder}
            />
          </div>

          <button
            type="button"
            className={`icon-submit-button session-send-button ${canSend ? "ready" : ""}`}
            onClick={onSubmitInput}
            aria-label="发送"
            title="发送"
            disabled={!canSend}
          >
            <span className="icon-submit-arrow" aria-hidden="true">
              <span className="icon-submit-arrow-head" />
            </span>
          </button>
        </div>
      </div>

      <div className="session-footer">
        <button
          type="button"
          className={`settings-button footer-settings-button ${settingsOpen ? "active" : ""}`}
          onClick={onToggleSettings}
          aria-expanded={settingsOpen}
          aria-controls="settings-drawer"
        >
          <SettingsIcon />
          <span>设置</span>
        </button>

        <div className="channel-status-row">
          <div className="channel-status-item">
            <span className={`channel-status-dot ${captureActive.candidate ? "active" : ""}`} />
            <span>你：{captureMessage.candidate}</span>
          </div>
          <div className="channel-status-item">
            <span className={`channel-status-dot ${captureActive.interviewer ? "active" : ""}`} />
            <span>面试官：{captureMessage.interviewer}</span>
          </div>
        </div>
      </div>
    </div>
  );
}

function SettingsIcon() {
  return (
    <span className="settings-button-icon" aria-hidden="true">
      <svg viewBox="0 0 24 24" focusable="false">
        <path d="M19.14 12.94a7.43 7.43 0 0 0 .05-.94 7.43 7.43 0 0 0-.05-.94l2.03-1.58a.5.5 0 0 0 .12-.64l-1.92-3.32a.5.5 0 0 0-.6-.22l-2.39.96a7.22 7.22 0 0 0-1.63-.94l-.36-2.54a.5.5 0 0 0-.49-.42h-3.84a.5.5 0 0 0-.49.42l-.36 2.54a7.22 7.22 0 0 0-1.63.94l-2.39-.96a.5.5 0 0 0-.6.22L2.71 8.84a.5.5 0 0 0 .12.64l2.03 1.58a7.43 7.43 0 0 0-.05.94 7.43 7.43 0 0 0 .05.94L2.83 14.52a.5.5 0 0 0-.12.64l1.92 3.32a.5.5 0 0 0 .6.22l2.39-.96c.5.39 1.04.71 1.63.94l.36 2.54a.5.5 0 0 0 .49.42h3.84a.5.5 0 0 0 .49-.42l.36-2.54c.59-.23 1.13-.55 1.63-.94l2.39.96a.5.5 0 0 0 .6-.22l1.92-3.32a.5.5 0 0 0-.12-.64Zm-7.14 2.31A3.25 3.25 0 1 1 15.25 12 3.25 3.25 0 0 1 12 15.25Z" />
      </svg>
    </span>
  );
}

function formatTime() {
  return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

async function captureScreenshotDataUrl() {
  const stream = await navigator.mediaDevices.getDisplayMedia({
    video: true,
    audio: false,
  });
  const video = document.createElement("video");
  video.srcObject = stream;
  video.muted = true;
  await video.play();

  try {
    const width = video.videoWidth || 1280;
    const height = video.videoHeight || 720;
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext("2d");
    if (!context) {
      throw new Error("无法创建截图画布。");
    }
    context.drawImage(video, 0, 0, width, height);
    return canvas.toDataURL("image/png");
  } finally {
    stream.getTracks().forEach((track) => track.stop());
    video.srcObject = null;
  }
}

function truncatePreview(text: string, limit = 60) {
  const normalized = text.trim();
  if (!normalized) {
    return "正在捕获当前这段发言...";
  }
  return normalized.length > limit ? `${normalized.slice(0, limit)}...` : normalized;
}

function getRealtimeSocketBaseUrl(baseUrl: string) {
  if (baseUrl.startsWith("https://")) {
    return `wss://${baseUrl.slice("https://".length)}`;
  }
  if (baseUrl.startsWith("http://")) {
    return `ws://${baseUrl.slice("http://".length)}`;
  }
  return baseUrl;
}

function buildRenderedHistory(
  history: TranscriptTurn[],
  buffers: Record<Speaker, string>,
): RenderedTranscriptTurn[] {
  const rendered: RenderedTranscriptTurn[] = history.map((item, index) => ({
    ...item,
    key: `${item.timestamp ?? "no-time"}-${index}`,
  }));

  (["interviewer", "candidate"] as Speaker[]).forEach((speaker) => {
    const liveText = buffers[speaker].trim();
    if (!liveText) {
      return;
    }

    const lastTurn = rendered[rendered.length - 1];
    if (lastTurn && lastTurn.speaker === speaker) {
      rendered[rendered.length - 1] = {
        ...lastTurn,
        key: `${lastTurn.key}-live`,
        text: mergeTranscriptText(lastTurn.text, liveText),
        statusLabel: "正在说...",
      };
      return;
    }

    rendered.push({
      key: `live-${speaker}`,
      speaker,
      text: liveText,
      statusLabel: "正在说...",
    });
  });

  return rendered;
}

function shouldHoldInterviewerBuffer(text: string) {
  const normalized = text.trim();
  if (!normalized) {
    return false;
  }
  if (/[。！!？?]$/.test(normalized)) {
    return false;
  }
  return normalized.length < 24;
}

function isMultiLineBubble(text: string) {
  const normalized = text.trim();
  return normalized.includes("\n") || normalized.length > 34;
}

function detachLeadingPunctuation(turn: TranscriptTurn, history: TranscriptTurn[]) {
  const text = turn.text.trimStart();
  if (!text) {
    return { turn: null, history: null as TranscriptTurn[] | null };
  }

  const punctuationMatch = text.match(/^[，。！？；：、,.!?;:）)】\]」』]+/u);
  if (!punctuationMatch) {
    return { turn: { ...turn, text }, history: null as TranscriptTurn[] | null };
  }

  const latestSpeakerIndex = findLatestSpeakerTurnIndex(history, turn.speaker);
  if (latestSpeakerIndex < 0) {
    return { turn: { ...turn, text }, history: null as TranscriptTurn[] | null };
  }

  const punctuation = punctuationMatch[0];
  const remainder = text.slice(punctuation.length).trimStart();
  const nextHistory = history.map((item, index) =>
    index === latestSpeakerIndex
      ? {
          ...item,
          text: `${item.text}${punctuation}`,
        }
      : item,
  );
  return {
    turn: remainder ? { ...turn, text: remainder } : null,
    history: nextHistory,
  };
}

function findLatestSpeakerTurnIndex(history: TranscriptTurn[], speaker: Speaker) {
  for (let index = history.length - 1; index >= 0; index -= 1) {
    if (history[index]?.speaker === speaker) {
      return index;
    }
  }
  return -1;
}

function mergeTranscriptText(left: string, right: string) {
  const normalizedLeft = left.trim();
  const normalizedRight = right.trim();
  if (!normalizedLeft) {
    return normalizedRight;
  }
  if (!normalizedRight) {
    return normalizedLeft;
  }

  const lastChar = normalizedLeft[normalizedLeft.length - 1];
  const firstChar = normalizedRight[0];
  const needsSpace = /[A-Za-z0-9]/.test(lastChar) && /[A-Za-z0-9]/.test(firstChar);
  return `${normalizedLeft}${needsSpace ? " " : ""}${normalizedRight}`;
}

function clampHistoryTurns(turns: TranscriptTurn[]) {
  return turns.length > MAX_HISTORY_TURNS ? turns.slice(-MAX_HISTORY_TURNS) : turns;
}

function clampRealtimeAnswerItems(items: RealtimeAnswer[]) {
  return items.length > MAX_REALTIME_ANSWER_ITEMS ? items.slice(-MAX_REALTIME_ANSWER_ITEMS) : items;
}
