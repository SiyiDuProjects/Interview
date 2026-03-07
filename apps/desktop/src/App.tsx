import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import {
  getCaptureLabel,
  requestCaptureStream,
  startLocalAudioCapture,
  type AudioCaptureHandle,
} from "./audioCapture";
import { joinChunk, parseLiveScript } from "./liveTranscript";
import type {
  AnswerFeedItem,
  CandidateContext,
  CoachResponse,
  DetailJobStatus,
  Speaker,
  TranscriptTurn,
} from "./types";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";
const AUTO_FLUSH_MS: Record<Speaker, number> = {
  interviewer: 4200,
  candidate: 2200,
};
const LIVE_CHUNK_DELAY_MS = 420;
const CAPTURE_CHUNK_MS: Record<Speaker, number> = {
  interviewer: 4800,
  candidate: 3200,
};
const CAPTURE_MIN_LEVEL: Record<Speaker, number> = {
  interviewer: 0.012,
  candidate: 0.006,
};
const ANSWER_CARD_REUSE_WINDOW_MS = 12000;
const COACH_REQUEST_TIMEOUT_MS = 15000;
const MIN_AUDIO_CHUNK_BYTES = 2048;
const MAX_HISTORY_TURNS = 100;
const MAX_ANSWER_FEED_ITEMS = 24;
const SOCKET_RECONNECT_DELAYS_MS = [1000, 2000, 5000];

const initialContext: CandidateContext = {
  name: "",
  target_role: "后端开发工程师",
  resume: "做过内部数据服务平台，负责过 SQL 优化、缓存链路和后端接口治理。",
  job_description: "要求 SQL、Redis、分布式基础和项目表达能力都比较强。",
  custom_notes: "回答尽量简洁，优先突出结果、取舍和系统设计思路。",
};

const initialHistory: TranscriptTurn[] = [
  { speaker: "interviewer", text: "你先做一个简短的自我介绍。", timestamp: "09:30" },
  {
    speaker: "candidate",
    text: "我主要做后端开发，最近比较聚焦数据服务、SQL 优化和缓存稳定性。",
    timestamp: "09:31",
  },
];

const complexScenario: TranscriptTurn[] = [
  { speaker: "interviewer", text: "你们项目里的 SQL 索引是怎么设计的？", timestamp: "10:00" },
  { speaker: "candidate", text: "我会先从慢查询出发，再根据过滤字段和排序字段设计索引。", timestamp: "10:01" },
  { speaker: "interviewer", text: "为什么数据库一般会用 B+ 树？", timestamp: "10:02" },
  { speaker: "candidate", text: "因为它更适合磁盘 IO，也支持范围查询。", timestamp: "10:03" },
  { speaker: "interviewer", text: "哈希索引和 B+ 树的区别是什么？", timestamp: "10:04" },
  { speaker: "candidate", text: "哈希适合等值查询，但不适合范围查询和排序。", timestamp: "10:05" },
  { speaker: "interviewer", text: "如果写入压力很大，索引会带来什么问题？", timestamp: "10:06" },
  { speaker: "candidate", text: "会增加写放大和维护成本，所以要平衡读收益和写成本。", timestamp: "10:07" },
  { speaker: "interviewer", text: "再说说 Redis 持久化，RDB 和 AOF 怎么选？", timestamp: "10:08" },
  { speaker: "candidate", text: "主要看恢复速度、磁盘开销和能接受的数据丢失窗口。", timestamp: "10:09" },
];

const initialLiveScript = `interviewer|你能解释一下 SQL 索引通常是怎么实现的吗？
candidate|大多数数据库里的索引通常都是基于 B+ 树实现的。
interviewer|为什么 B+ 树会优于哈希索引？
candidate|因为范围查询和顺序遍历在 B+ 树下效果更好。
interviewer|如果写入压力很高，你还会加很多索引吗？
candidate|不会，我会权衡读性能和写入维护成本。
interviewer|再比较一下 Redis 的 RDB 和 AOF。`;

interface RenderedTranscriptTurn extends TranscriptTurn {
  key: string;
  statusLabel?: string;
}

interface HandleTurnOptions {
  forceAnswer?: boolean;
}

interface RealtimeMessage {
  type?: string;
  text?: string;
  detail?: string;
  is_final?: boolean;
  speech_final?: boolean;
}

type PrimaryMenu = "session" | "materials";
type SessionPanel = "live" | "manual" | "simulation";
type ContextFileField = "resume" | "job_description" | "custom_notes";

export default function App() {
  const [context, setContext] = useState<CandidateContext>(initialContext);
  const [history, setHistory] = useState<TranscriptTurn[]>(initialHistory);
  const [answerFeed, setAnswerFeed] = useState<AnswerFeedItem[]>([]);
  const [recognitionLocale, setRecognitionLocale] = useState<"zh" | "en">("zh");
  const [primaryMenu, setPrimaryMenu] = useState<PrimaryMenu>("session");
  const [sessionPanel, setSessionPanel] = useState<SessionPanel>("live");
  const [input, setInput] = useState("为什么数据库通常使用 B+ 树索引，而不是哈希索引？");
  const [candidateReply, setCandidateReply] = useState("");
  const [liveScript, setLiveScript] = useState(initialLiveScript);
  const [buffers, setBuffers] = useState<Record<Speaker, string>>({ interviewer: "", candidate: "" });
  const [captureActive, setCaptureActive] = useState<Record<Speaker, boolean>>({ interviewer: false, candidate: false });
  const [captureMessage, setCaptureMessage] = useState<Record<Speaker, string>>({
    interviewer: "待机",
    candidate: "待机",
  });
  const [liveRunning, setLiveRunning] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const answerFeedRef = useRef<HTMLDivElement | null>(null);
  const chatListRef = useRef<HTMLDivElement | null>(null);
  const answerItemsRef = useRef<AnswerFeedItem[]>([]);
  const historyRef = useRef<TranscriptTurn[]>(initialHistory);
  const bufferRef = useRef<Record<Speaker, string>>({ interviewer: "", candidate: "" });
  const speechSegmentRef = useRef<Record<Speaker, { finalized: string; interim: string }>>({
    interviewer: { finalized: "", interim: "" },
    candidate: { finalized: "", interim: "" },
  });
  const activeSpeakerRef = useRef<Speaker | null>(null);
  const streamControllersRef = useRef<Map<string, AbortController>>(new Map());
  const transcriptionSocketRef = useRef<Partial<Record<Speaker, WebSocket>>>({});
  const flushTimersRef = useRef<Partial<Record<Speaker, number>>>({});
  const processingQueueRef = useRef<Promise<void>>(Promise.resolve());
  const captureHandlesRef = useRef<Partial<Record<Speaker, AudioCaptureHandle>>>({});
  const liveRunTokenRef = useRef(0);
  const reconnectTimersRef = useRef<Partial<Record<Speaker, number>>>({});
  const reconnectAttemptsRef = useRef<Record<Speaker, number>>({ interviewer: 0, candidate: 0 });
  const manualStopRef = useRef<Record<Speaker, boolean>>({ interviewer: false, candidate: false });
  const recognitionLocaleRef = useRef<"zh" | "en">(recognitionLocale);

  const renderedHistory = useMemo(() => buildRenderedHistory(history, buffers), [history, buffers]);
  const sessionOnline = captureActive.candidate || captureActive.interviewer;
  const routeLabel = recognitionLocale === "zh" ? "中文（讯飞）" : "英文（Deepgram）";
  const currentSignal = liveRunning
    ? "脚本回放中"
    : sessionOnline
      ? "双路监听中"
      : "等待启动";
  const transcriptSummary = `${history.length.toString().padStart(2, "0")} 轮对话`;
  const answerSummary = `${answerFeed.length.toString().padStart(2, "0")} 张答案卡`;
  const confidenceAverage = answerFeed.length
    ? `${Math.round(
        (answerFeed.reduce((sum, item) => sum + item.confidence, 0) / answerFeed.length) * 100,
      )}%`
    : "--";

  useEffect(() => {
    historyRef.current = history;
  }, [history]);

  useEffect(() => {
    answerItemsRef.current = answerFeed;
  }, [answerFeed]);

  useEffect(() => {
    recognitionLocaleRef.current = recognitionLocale;
  }, [recognitionLocale]);

  useEffect(() => {
    const container = answerFeedRef.current;
    if (container) {
      container.scrollTop = container.scrollHeight;
    }
  }, [answerFeed]);

  useEffect(() => {
    const container = chatListRef.current;
    if (container) {
      container.scrollTop = container.scrollHeight;
    }
  }, [renderedHistory]);

  useEffect(() => {
    return () => {
      streamControllersRef.current.forEach((controller) => controller.abort());
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
      liveRunTokenRef.current += 1;
    };
  }, []);

  async function submitInterviewerQuestion(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = input.trim();
    if (!text) {
      return;
    }

    setLoading(true);
    setError(null);
    try {
      await handleCommittedTurn(
        {
          speaker: "interviewer",
          text,
          timestamp: formatTime(),
        },
        { forceAnswer: true },
      );
      setInput("");
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "提交面试官问题失败");
    } finally {
      setLoading(false);
    }
  }

  function submitCandidateReply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = candidateReply.trim();
    if (!text) {
      return;
    }

    appendTurn({ speaker: "candidate", text, timestamp: formatTime() });
    setCandidateReply("");
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

  async function loadComplexScenario() {
    setLoading(true);
    setError(null);
    try {
      resetLiveBuffers();
      historyRef.current = clampHistoryTurns([...initialHistory]);
      setHistory(historyRef.current);
      resetAnswerFeed();

      for (const turn of complexScenario) {
        if (turn.speaker === "candidate") {
          appendTurn(turn);
          continue;
        }
        await handleCommittedTurn(turn, { forceAnswer: true });
      }
    } catch (scenarioError) {
      setError(scenarioError instanceof Error ? scenarioError.message : "长对话场景运行失败");
    } finally {
      setLoading(false);
    }
  }

  async function startLiveSimulation() {
    if (liveRunning) {
      return;
    }

    setError(null);
    setLiveRunning(true);
    resetLiveBuffers();
    const token = liveRunTokenRef.current + 1;
    liveRunTokenRef.current = token;

    try {
      for (const chunk of parseLiveScript(liveScript)) {
        if (liveRunTokenRef.current !== token) {
          return;
        }
        ingestLiveChunk(chunk.speaker, chunk.text);
        await sleep(LIVE_CHUNK_DELAY_MS);
      }
      flushAllBuffers();
      await processingQueueRef.current;
    } catch (simulationError) {
      setError(simulationError instanceof Error ? simulationError.message : "实时文本脚本模拟失败");
    } finally {
      if (liveRunTokenRef.current === token) {
        setLiveRunning(false);
      }
    }
  }

  function stopLiveSimulation() {
    liveRunTokenRef.current += 1;
    setLiveRunning(false);
    flushAllBuffers();
  }

  async function startSpeakerCapture(speaker: Speaker): Promise<boolean> {
    if (captureHandlesRef.current[speaker]) {
      return true;
    }

    let socket: WebSocket | null = null;
    try {
      manualStopRef.current[speaker] = false;
      clearReconnectTimer(speaker);
      socket = await openRealtimeTranscriptionSocket(speaker, recognitionLocale);
      const stream = await requestCaptureStream(speaker);
      const handle = startLocalAudioCapture({
        stream,
        chunkDurationMs: CAPTURE_CHUNK_MS[speaker],
        minLevel: CAPTURE_MIN_LEVEL[speaker],
        onChunk: (chunk) => sendAudioChunk(speaker, chunk),
      });

      transcriptionSocketRef.current[speaker] = socket;
      captureHandlesRef.current[speaker] = handle;
      reconnectAttemptsRef.current[speaker] = 0;
      setCaptureActive((current) => ({ ...current, [speaker]: true }));
      setCaptureMessage((current) => ({
        ...current,
        [speaker]: "监听中",
      }));
      return true;
    } catch (captureError) {
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: "close" }));
        socket.close();
      }
      delete transcriptionSocketRef.current[speaker];
      const message = captureError instanceof Error ? captureError.message : "音频采集启动失败";
      setCaptureMessage((current) => ({ ...current, [speaker]: message }));
      return false;
    }
  }

  async function startInterviewCapture() {
    if (captureHandlesRef.current.candidate || captureHandlesRef.current.interviewer) {
      return;
    }

    setError(null);
    const micStarted = await startSpeakerCapture("candidate");
    const systemStarted = await startSpeakerCapture("interviewer");

    if (!micStarted && !systemStarted) {
      setError("麦克风和系统音频都没有启动成功。");
      return;
    }
    if (micStarted && !systemStarted) {
      setError("麦克风已启动，但系统音频启动失败。");
      return;
    }
    if (!micStarted && systemStarted) {
      setError("系统音频已启动，但麦克风启动失败。");
    }
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
    setCaptureActive((current) => ({ ...current, [speaker]: false }));
    setCaptureMessage((current) => ({
      ...current,
      [speaker]: "待机",
    }));
    flushSpeakerBuffer(speaker, true);
    resetSpeakerSegments(speaker);
  }

  async function openRealtimeTranscriptionSocket(speaker: Speaker, locale: "zh" | "en") {
    return await new Promise<WebSocket>((resolve, reject) => {
      const socket = new WebSocket(
        `${getRealtimeSocketBaseUrl(API_BASE_URL)}/ws/transcribe/${speaker}?locale=${locale}`,
      );
      let resolved = false;

      const timeoutId = window.setTimeout(() => {
        socket.close();
        reject(new Error(`${getCaptureLabel(speaker)}实时转写连接超时。`));
      }, 10000);

      socket.addEventListener("open", () => {
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
      const socket = await openRealtimeTranscriptionSocket(speaker, recognitionLocaleRef.current);
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

        await handleCommittedTurn(turn);
      })
      .catch((queueError) => {
        setError(queueError instanceof Error ? queueError.message : "对话片段处理失败。");
      });
  }

  async function handleCommittedTurn(turn: TranscriptTurn, options: HandleTurnOptions = {}) {
    const historyBeforeTurn = historyRef.current;
    appendTurn(turn);
    if (turn.speaker === "interviewer" && shouldGenerateAnswerCard(turn.text, options.forceAnswer === true)) {
      await requestCoach(turn, historyBeforeTurn);
    }
  }

  function appendTurn(turn: TranscriptTurn) {
    const previousTurn = historyRef.current[historyRef.current.length - 1];
    const shouldMergeSpeakerTurn = previousTurn?.speaker === turn.speaker;

    const nextHistory = shouldMergeSpeakerTurn
      ? [
          ...historyRef.current.slice(0, -1),
          {
            ...previousTurn,
            text: mergeTranscriptText(previousTurn.text, turn.text),
            timestamp: turn.timestamp ?? previousTurn.timestamp,
          },
        ]
      : [...historyRef.current, turn];

    const clampedHistory = clampHistoryTurns(nextHistory);
    historyRef.current = clampedHistory;
    setHistory(clampedHistory);
  }

  async function requestCoach(turn: TranscriptTurn, currentHistory: TranscriptTurn[]) {
    const result = await fetchWithTimeout(
      `${API_BASE_URL}/api/coach/respond`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          turn,
          history: currentHistory,
          context,
          generation_mode: "hybrid",
        }),
      },
      COACH_REQUEST_TIMEOUT_MS,
      "AI 回答请求超时，请检查后端是否已启动，或稍后重试。",
    );

    if (!result.ok) {
      throw new Error(`回答接口失败，状态码 ${result.status}`);
    }

    const payload = (await result.json()) as CoachResponse;
    const reusableCard = findReusableAnswerCard(answerItemsRef.current, turn.text);
    const item: AnswerFeedItem = {
      ...payload,
      id: reusableCard?.id ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`,
      prompt: turn.text,
      timestamp: turn.timestamp ?? formatTime(),
      createdAtMs: Date.now(),
    };

    if (reusableCard) {
      if (reusableCard.detail_job_id) {
        const previousController = streamControllersRef.current.get(reusableCard.detail_job_id);
        previousController?.abort();
        streamControllersRef.current.delete(reusableCard.detail_job_id);
      }
      updateAnswerFeed((current) =>
        current.map((currentItem) => (currentItem.id === reusableCard.id ? item : currentItem)),
      );
    } else {
      updateAnswerFeed((current) => [...current, item]);
    }

    if (item.detail_job_id) {
      void streamDetailedAnswer(item.id, item.detail_job_id);
    }
  }

  async function streamDetailedAnswer(answerId: string, jobId: string) {
    if (streamControllersRef.current.has(jobId)) {
      return;
    }

    const controller = new AbortController();
    streamControllersRef.current.set(jobId, controller);

    try {
      const response = await fetchWithTimeout(
        `${API_BASE_URL}/api/coach/detail-stream/${jobId}`,
        { signal: controller.signal },
        COACH_REQUEST_TIMEOUT_MS,
        "详细回答流连接超时，请检查后端是否可用。",
      );

      if (!response.ok || !response.body) {
        throw new Error(`详细回答流失败，状态码 ${response.status}`);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) {
          break;
        }

        buffer += decoder.decode(value, { stream: true });
        const messages = buffer.split("\n\n");
        buffer = messages.pop() ?? "";

        for (const message of messages) {
          const dataLine = message.split("\n").find((line) => line.startsWith("data: "));
          if (!dataLine) {
            continue;
          }

          const status = JSON.parse(dataLine.slice(6)) as DetailJobStatus;
          if (!status.answer) {
            continue;
          }

          updateAnswerFeed((current) =>
            current.map((item) =>
              item.id === answerId
                ? {
                    ...item,
                    deep_answer: status.answer ?? item.deep_answer,
                  }
                : item,
            ),
          );
        }
      }
    } catch (streamError) {
      if (!(streamError instanceof DOMException && streamError.name === "AbortError")) {
        setError(streamError instanceof Error ? streamError.message : "详细回答流失败。");
      }
    } finally {
      streamControllersRef.current.delete(jobId);
    }
  }

  function updateAnswerFeed(updater: (current: AnswerFeedItem[]) => AnswerFeedItem[]) {
    setAnswerFeed((current) => {
      const next = clampAnswerFeedItems(updater(current));
      answerItemsRef.current = next;
      const retainedJobIds = new Set(
        next
          .map((item) => item.detail_job_id)
          .filter((jobId): jobId is string => Boolean(jobId)),
      );

      current.forEach((item) => {
        if (!item.detail_job_id || retainedJobIds.has(item.detail_job_id)) {
          return;
        }
        const controller = streamControllersRef.current.get(item.detail_job_id);
        controller?.abort();
        streamControllersRef.current.delete(item.detail_job_id);
      });

      return next;
    });
  }

  function resetAnswerFeed() {
    streamControllersRef.current.forEach((controller) => controller.abort());
    streamControllersRef.current.clear();
    answerItemsRef.current = [];
    setAnswerFeed([]);
  }

  return (
    <div className="app-shell">
      <div className="ambient ambient-left" />
      <div className="ambient ambient-right" />

      <header className="toolbar">
        <div className="toolbar-brand">
          <p className="hero-kicker">INTERVIEW CONTROL DECK</p>
          <h1>模拟面试助手</h1>
        </div>

        <div className="toolbar-controls">
          <div className="toolbar-group">
            <label className="field-label" htmlFor="recognition-locale">
              识别语言
            </label>
            <select
              id="recognition-locale"
              value={recognitionLocale}
              onChange={(event) => setRecognitionLocale(event.target.value as "zh" | "en")}
              disabled={captureActive.candidate || captureActive.interviewer}
            >
              <option value="zh">中文（讯飞）</option>
              <option value="en">英文（Deepgram）</option>
            </select>
          </div>

          <div className="toolbar-status">
            <span className={`signal-pill ${sessionOnline ? "live" : "idle"}`}>
              <i />
              {sessionOnline ? "录制中" : "待机"}
            </span>
            <span className="toolbar-meta">{routeLabel}</span>
            <span className="toolbar-meta">{answerSummary}</span>
          </div>
        </div>
      </header>

      <div className="nav-strip">
        <div className="primary-nav">
          <button
            type="button"
            className={`nav-button ${primaryMenu === "session" ? "active" : ""}`}
            onClick={() => setPrimaryMenu("session")}
          >
            面试面板
          </button>
          <button
            type="button"
            className={`nav-button ${primaryMenu === "materials" ? "active" : ""}`}
            onClick={() => setPrimaryMenu("materials")}
          >
            个人资料
          </button>
        </div>

        {primaryMenu === "session" ? (
          <div className="secondary-nav">
            <button
              type="button"
              className={`subnav-button ${sessionPanel === "live" ? "active" : ""}`}
              onClick={() => setSessionPanel("live")}
            >
              实时对话
            </button>
            <button
              type="button"
              className={`subnav-button ${sessionPanel === "manual" ? "active" : ""}`}
              onClick={() => setSessionPanel("manual")}
            >
              手动输入
            </button>
            <button
              type="button"
              className={`subnav-button ${sessionPanel === "simulation" ? "active" : ""}`}
              onClick={() => setSessionPanel("simulation")}
            >
              脚本模拟
            </button>
          </div>
        ) : null}
      </div>

      <main className="layout locked">
        <section className={`panel transcript-panel ${primaryMenu === "session" && sessionPanel === "live" ? "live-mode" : ""}`}>
          {primaryMenu === "session" ? (
            <>
              <div className="panel-head">
                <div>
                  <p className="panel-kicker">
                    {sessionPanel === "live"
                      ? "对话"
                      : sessionPanel === "manual"
                        ? "手动"
                        : "模拟"}
                  </p>
                  <h2>
                    {sessionPanel === "live"
                      ? "对话区"
                      : sessionPanel === "manual"
                        ? "手动投喂"
                        : "脚本回放"}
                  </h2>
                </div>
                <div className="panel-note">
                  {sessionPanel === "live"
                    ? "实时转写"
                    : sessionPanel === "manual"
                      ? "补上下文"
                      : "脚本回放"}
                </div>
              </div>

              {sessionPanel === "live" ? (
                <>
                  <div className="chat-stage">
                    <div className="chat-list" ref={chatListRef}>
                      {renderedHistory.map((item) => (
                        <div className={`chat-row ${item.speaker}`} key={item.key}>
                          <article className="bubble">
                            <div className="bubble-meta">
                              <span>{item.speaker === "interviewer" ? "面试官" : "我"}</span>
                              <span>{item.statusLabel ?? item.timestamp}</span>
                            </div>
                            <p>{item.text}</p>
                          </article>
                        </div>
                      ))}
                    </div>
                  </div>

                  <div className="capture-grid compact">
                    <article className={`capture-card status-card ${captureActive.candidate ? "active" : ""}`}>
                      <span className="capture-label">你</span>
                      <p>{captureMessage.candidate}</p>
                    </article>
                    <article className={`capture-card status-card ${captureActive.interviewer ? "active" : ""}`}>
                      <span className="capture-label">面试官</span>
                      <p>{captureMessage.interviewer}</p>
                    </article>
                  </div>

                  <section className="field-card control-card compact-control">
                    <div>
                      <span className="panel-kicker">采集</span>
                      <h3>双路音频</h3>
                    </div>
                    <button
                      className={`capture-toggle ${sessionOnline ? "live" : ""}`}
                      type="button"
                      onClick={sessionOnline ? stopInterviewCapture : () => void startInterviewCapture()}
                      disabled={liveRunning && !sessionOnline}
                    >
                      {sessionOnline ? "停止" : "开始"}
                    </button>
                  </section>
                </>
              ) : null}

              {sessionPanel === "manual" ? (
                <div className="workspace-grid single-column">
                  <form className="field-card" onSubmit={submitInterviewerQuestion}>
                    <div className="field-head">
                      <span className="panel-kicker">MANUAL TRIGGER</span>
                      <h3>手动输入面试官问题</h3>
                    </div>
                    <textarea
                      id="interviewer-input"
                      rows={6}
                      value={input}
                      onChange={(event) => setInput(event.target.value)}
                      placeholder="输入完整问题，立即触发右侧答案卡。"
                    />
                    <button disabled={loading || liveRunning} type="submit">
                      {loading ? "生成中..." : "提交问题"}
                    </button>
                  </form>

                  <form className="field-card" onSubmit={submitCandidateReply}>
                    <div className="field-head">
                      <span className="panel-kicker">CONTEXT ONLY</span>
                      <h3>手动输入你的回答</h3>
                    </div>
                    <textarea
                      id="candidate-input"
                      rows={6}
                      value={candidateReply}
                      onChange={(event) => setCandidateReply(event.target.value)}
                      placeholder="这部分只进入上下文，不会直接生成新的答案卡。"
                    />
                    <button type="submit" disabled={liveRunning}>
                      写入上下文
                    </button>
                  </form>
                </div>
              ) : null}

              {sessionPanel === "simulation" ? (
                <div className="workspace-grid single-column">
                  <section className="field-card">
                    <div className="field-head">
                      <span className="panel-kicker">SCENARIO</span>
                      <h3>复杂面试场景</h3>
                    </div>
                    <p className="field-copy">一键灌入长对话，验证追问、上下文和答案复用逻辑。</p>
                    <button
                      className="ghost-button"
                      onClick={loadComplexScenario}
                      type="button"
                      disabled={loading || liveRunning}
                    >
                      {loading ? "正在灌入复杂场景..." : "运行长对话场景"}
                    </button>
                  </section>

                  <section className="field-card script-card">
                    <div className="field-head">
                      <span className="panel-kicker">SCRIPT PLAYBACK</span>
                      <h3>实时文本片段模拟</h3>
                    </div>
                    <textarea
                      id="live-script"
                      rows={12}
                      value={liveScript}
                      onChange={(event) => setLiveScript(event.target.value)}
                      placeholder="每行一条，使用 interviewer| 或 candidate| 作为前缀。"
                    />
                    <div className="button-row">
                      <button type="button" onClick={startLiveSimulation} disabled={liveRunning || loading}>
                        {liveRunning ? "脚本流正在回放..." : "开始脚本回放"}
                      </button>
                      <button className="ghost-button" type="button" onClick={stopLiveSimulation} disabled={!liveRunning}>
                        停止回放
                      </button>
                    </div>
                  </section>
                </div>
              ) : null}

              {error ? <p className="error-text">{error}</p> : null}
            </>
          ) : (
            <>
              <div className="panel-head">
                <div>
                  <p className="panel-kicker">PROFILE / GROUND TRUTH</p>
                  <h2>候选人上下文</h2>
                </div>
                <div className="panel-note">简历、岗位 JD 和备注会直接影响右侧回答风格。</div>
              </div>

              <div className="context-grid context-grid-panel">
                <label className="context-field">
                  <span>姓名</span>
                  <input value={context.name} onChange={(event) => setContext({ ...context, name: event.target.value })} />
                </label>
                <label className="context-field">
                  <span>目标岗位</span>
                  <input
                    value={context.target_role}
                    onChange={(event) => setContext({ ...context, target_role: event.target.value })}
                  />
                </label>
                <label className="context-field wide">
                  <span>简历</span>
                  <textarea
                    rows={7}
                    value={context.resume}
                    onChange={(event) => setContext({ ...context, resume: event.target.value })}
                    placeholder="粘贴你的项目经历、指标和职责。"
                  />
                  <div className="file-import-row">
                    <label className="file-import-button">
                      导入文件
                      <input
                        type="file"
                        accept=".txt,.md,.markdown,.json,.csv,.log,text/plain,text/markdown,application/json,text/csv"
                        onChange={(event) => void importContextFile("resume", event.target.files)}
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
                    onChange={(event) => setContext({ ...context, job_description: event.target.value })}
                    placeholder="粘贴目标职位 JD。"
                  />
                  <div className="file-import-row">
                    <label className="file-import-button">
                      导入文件
                      <input
                        type="file"
                        accept=".txt,.md,.markdown,.json,.csv,.log,text/plain,text/markdown,application/json,text/csv"
                        onChange={(event) => void importContextFile("job_description", event.target.files)}
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
                    onChange={(event) => setContext({ ...context, custom_notes: event.target.value })}
                    placeholder="写清楚你希望 AI 优先强调的项目取舍、系统复杂度和结果。"
                  />
                  <div className="file-import-row">
                    <label className="file-import-button">
                      导入文件
                      <input
                        type="file"
                        accept=".txt,.md,.markdown,.json,.csv,.log,text/plain,text/markdown,application/json,text/csv"
                        onChange={(event) => void importContextFile("custom_notes", event.target.files)}
                      />
                    </label>
                    <span>支持 txt / md / json / csv / log</span>
                  </div>
                </label>
              </div>
            </>
          )}
        </section>

        <section className="panel answers-panel">
          <div className="panel-head sticky">
            <div>
              <p className="panel-kicker">答案</p>
              <h2>答案卡片</h2>
            </div>
            <div className="panel-note">快答在前，详答补全</div>
          </div>

          <div className="answer-feed" ref={answerFeedRef}>
            {answerFeed.length === 0 ? (
              <div className="empty-card">
                <span className="panel-kicker">NO CURRENT CARD</span>
                <p>还没有激活的问题。你可以开启采集、提交问题，或运行脚本回放。</p>
              </div>
            ) : (
              answerFeed.map((item) => (
                <article className="answer-card" key={item.id}>
                  <div className="answer-card-head">
                    <div className="tag-row">
                      <span className="tag primary">{item.topic}</span>
                      <span className="tag">{item.question_type}</span>
                      {item.detected_follow_up ? <span className="tag accent">追问</span> : null}
                    </div>
                    <span className="timestamp">{item.timestamp}</span>
                  </div>

                  <p className="answer-prompt">问题：{item.prompt}</p>

                  <section className="variant-block fast">
                    <div className="variant-head">
                      <h3>{item.fast_answer.label}</h3>
                      <span>{item.fast_answer.source}</span>
                    </div>
                    <p className="variant-summary">{item.fast_answer.short_answer}</p>
                    <ul>
                      {item.fast_answer.talking_points.map((point) => (
                        <li key={point}>{point}</li>
                      ))}
                    </ul>
                  </section>

                  <section className="variant-block deep">
                    <div className="variant-head">
                      <h3>{item.deep_answer.label}</h3>
                      <span>{item.deep_answer.source}</span>
                    </div>
                    <p className="variant-summary">{item.deep_answer.short_answer}</p>
                    <ul>
                      {item.deep_answer.talking_points.map((point) => (
                        <li key={point}>{point}</li>
                      ))}
                    </ul>
                  </section>

                  <div className="meta-grid">
                    <div className="meta-box">
                      <h4>可追问方向</h4>
                      <ul>
                        {item.follow_up_angles.map((angle) => (
                          <li key={angle}>{angle}</li>
                        ))}
                      </ul>
                    </div>
                    <div className="meta-box">
                      <h4>你的简历挂钩点</h4>
                      <p>{item.resume_hook ?? "当前还没有读到与你简历强绑定的证据点。"}</p>
                    </div>
                  </div>

                  <footer className="answer-footer">
                    <span>{item.context_summary}</span>
                    <strong>置信度 {Math.round(item.confidence * 100)}%</strong>
                  </footer>
                </article>
              ))
            )}
          </div>
        </section>
      </main>
    </div>
  );
}

function formatTime() {
  return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function sleep(durationMs: number) {
  return new Promise((resolve) => window.setTimeout(resolve, durationMs));
}

async function fetchWithTimeout(
  input: RequestInfo | URL,
  init: RequestInit,
  timeoutMs: number,
  timeoutMessage: string,
) {
  const controller = new AbortController();
  const timerId = window.setTimeout(() => controller.abort(), timeoutMs);

  try {
    return await fetch(input, {
      ...init,
      signal: init.signal ?? controller.signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error(timeoutMessage);
    }
    throw error;
  } finally {
    window.clearTimeout(timerId);
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

function shouldGenerateAnswerCard(text: string, forceAnswer: boolean) {
  if (forceAnswer) {
    return true;
  }

  const normalized = text.trim();
  if (normalized.length < 8) {
    return false;
  }
  if (/^(谢谢|好的|嗯嗯|可以|行|对|然后|继续|没了)[。！!？?]?$/u.test(normalized)) {
    return false;
  }
  if (/[？?]$/u.test(normalized)) {
    return true;
  }

  return /(为什么|怎么|如何|区别|说一下|讲一下|解释|介绍|比较|展开|实现|原理|设计|选择|选型|优化|处理|解决|怎么看|能不能|可不可以|有没有|是否|再说|详细说)/u.test(
    normalized,
  );
}

function findReusableAnswerCard(items: AnswerFeedItem[], text: string) {
  const lastItem = items.length ? items[items.length - 1] : null;
  if (!lastItem) {
    return null;
  }

  const ageMs = Date.now() - (lastItem.createdAtMs ?? 0);
  if (ageMs > ANSWER_CARD_REUSE_WINDOW_MS) {
    return null;
  }

  const previous = normalizeCompareText(lastItem.prompt);
  const current = normalizeCompareText(text);
  if (!previous || !current) {
    return null;
  }

  const overlap =
    previous.includes(current) ||
    current.includes(previous) ||
    (!lastItem.deep_answer.ready && current.length >= Math.max(6, previous.length / 2));

  return overlap ? lastItem : null;
}

function normalizeCompareText(text: string) {
  return text.replace(/[\s，。！？?、,.!;；:“”"'（）()]/gu, "");
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

function clampAnswerFeedItems(items: AnswerFeedItem[]) {
  return items.length > MAX_ANSWER_FEED_ITEMS ? items.slice(-MAX_ANSWER_FEED_ITEMS) : items;
}
