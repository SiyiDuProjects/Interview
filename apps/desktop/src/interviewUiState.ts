import type {
  AnswerRecord,
  AnswerStatus,
  AnswerStore,
  ChannelHealth,
  ChannelState,
  DeviceStatus,
  OperationRecord,
  OperationStatus,
  QuickAnswerAction,
  ServerEvent,
  Speaker,
  TranscriptState,
  TranscriptTurn,
} from "./types";

const TERMINAL_OPERATIONS = new Set<OperationStatus>(["completed", "failed", "cancelled"]);

export function mergeTranscriptTurn(turns: TranscriptTurn[], turn: TranscriptTurn): TranscriptTurn[] {
  const previous = turns.find((item) => item.turn_id === turn.turn_id);
  if (!previous) return [...turns, turn];
  if (previous.status && previous.status !== "streaming" && turn.status === "streaming") return turns;
  return turns.map((item) => item.turn_id === turn.turn_id ? { ...item, ...turn } : item);
}

export function transcriptForSpeaker(turns: TranscriptTurn[], speaker: Speaker): TranscriptState {
  let final = "";
  const partial: string[] = [];
  for (const turn of turns) {
    if (turn.speaker !== speaker) continue;
    if (turn.status === "streaming") partial.push(turn.text);
    else if (turn.text) final = turn.text;
  }
  return { final, partial: partial.filter(Boolean).join(" ") };
}

export function manualDraftKey(mode: string, questionId?: string) {
  return ["misheard", "conditions", "return"].includes(mode) ? `${mode}:${questionId || "none"}` : mode;
}

export function operationIsPending(operation: OperationRecord) {
  return !TERMINAL_OPERATIONS.has(operation.status);
}

export function mergeOperation(
  operations: OperationRecord[],
  incoming: OperationRecord,
): OperationRecord[] {
  const existing = operations.find((operation) => operation.operation_id === incoming.operation_id);
  if (existing && !operationIsPending(existing) && operationIsPending(incoming)) return operations;
  if (!existing) return [...operations, incoming];
  return operations.map((operation) => operation.operation_id === incoming.operation_id
    ? { ...operation, ...incoming }
    : operation);
}

export function answerActionPayload(
  action: QuickAnswerAction,
  operationId: string,
  answer: AnswerRecord | undefined,
  questionId: string | undefined,
) {
  if (!["answer", "deep"].includes(action) && !answer?.responseId) {
    throw new Error("请先选择要修改的回答。");
  }
  return {
    type: "quick_answer",
    action,
    operation_id: operationId,
    ...(answer?.responseId ? { response_id: answer.responseId } : {}),
    ...((answer?.questionId || questionId) ? { question_id: answer?.questionId || questionId } : {}),
  };
}

export function reconcileReadingAnswer(selectedId: string | null, answerIds: string[]) {
  if (selectedId && answerIds.includes(selectedId)) return selectedId;
  return answerIds[answerIds.length - 1] ?? null;
}

export function visibleAnswerOrder(answers: AnswerStore) {
  return answers.order.filter((id) => answers.byId[id] && answers.byId[id].intermediate !== true);
}

export function reconcileReadingSelection(
  selectedId: string | null,
  visibleIds: string[],
  rawIds: string[],
) {
  const anchor = selectedId && rawIds.includes(selectedId) ? selectedId : rawIds[rawIds.length - 1] ?? null;
  if (!anchor || visibleIds.includes(anchor)) return anchor;
  // Keep a hidden tool phase as a temporary bookmark until its later answer
  // arrives. Selecting the older display fallback would pin the reader there.
  const anchorIndex = rawIds.indexOf(anchor);
  for (let index = visibleIds.length - 1; index >= 0; index -= 1) {
    if (rawIds.indexOf(visibleIds[index]) > anchorIndex) return visibleIds[index];
  }
  return anchor;
}

export function mergeAnswerEvent(
  current: AnswerRecord,
  payload: ServerEvent,
  status: AnswerStatus,
  replaceText: boolean,
): AnswerRecord {
  const withMetadata = typeof payload.intermediate === "boolean" && payload.intermediate !== current.intermediate
    ? { ...current, intermediate: payload.intermediate } : current;
  // A late classification must still hide a tool preamble whose text has
  // already reached a terminal state. Completed text itself stays immutable.
  if (!replaceText && current.status !== "streaming") return withMetadata;
  return {
    ...withMetadata,
    question: payload.question ?? current.question,
    questionId: payload.question_id ?? current.questionId,
    text: payload.type === "answer_delta"
      ? `${current.text}${payload.delta ?? payload.text ?? ""}`
      : replaceText ? (payload.text ?? "") : (payload.text ?? current.text),
    status,
    createdAt: payload.created_at ?? current.createdAt,
    detail: payload.detail ?? payload.error ?? payload.message ?? current.detail,
  };
}

export function newerAnswerCount(selectedId: string | null, answerIds: string[]) {
  if (!selectedId) return 0;
  const index = answerIds.indexOf(selectedId);
  return index < 0 ? 0 : answerIds.length - index - 1;
}

export function applyChannelHealth(
  ready: boolean,
  status: DeviceStatus,
  active: boolean,
  health?: ChannelHealth,
): ChannelState {
  const phase = health?.phase;
  const message = health?.detail || health?.message;
  if (phase === "muted") return { phase, message: message || "音轨已静音，暂未收到声音" };
  if (phase === "error" || phase === "interrupted") {
    return { phase, message: message || "采集已中断，请恢复这路音频" };
  }
  if (phase === "reconnecting" || phase === "connecting") {
    return { phase, message: message || "正在连接采集设备" };
  }
  if (ready) return { phase: active ? "listening" : "ready", message: message || (active ? "采集中" : "已就绪") };
  return status === "initializing"
    ? { phase: "connecting", message: message || "初始化中" }
    : { phase: "idle", message: message || "采集设备离线" };
}

export function listeningStatus({
  connected,
  reconnecting,
  active,
  deviceStatus,
  channels,
  held,
  answering,
}: {
  connected: boolean;
  reconnecting: boolean;
  active: boolean;
  deviceStatus: DeviceStatus;
  channels: Record<Speaker, ChannelState>;
  held: boolean;
  answering: boolean;
}) {
  if (!connected) return {
    label: reconnecting ? "重新连接中" : "等待连接",
    detail: active ? "同步已中断，采集和回答状态等待恢复。" : "连接后才能确认采集状态。",
    live: false,
  };
  if (!active) return {
    label: deviceStatus === "ready" ? "准备就绪" : "设备未就绪",
    detail: deviceStatus === "ready" ? "开始后才会发送音频。" : "请查看两路音频状态。",
    live: false,
  };
  if (deviceStatus !== "ready" || Object.values(channels).some((channel) => channel.phase !== "listening" && channel.phase !== "ready")) {
    return { label: "音频需检查", detail: "至少一路采集尚未就绪，请查看设备详情。", live: false };
  }
  if (held) return { label: "只听不答", detail: "继续接收和转写音频，自动回答已暂停。", live: true };
  return { label: answering ? "回答中" : "聆听中", detail: "两路音频已连接。", live: true };
}

export function operationLabel(operation: OperationRecord) {
  const name = operation.action === "deep" ? "深入分析"
    : operation.kind === "code_action" ? "代码"
    : operation.kind === "answer_screens" ? "截图解题"
    : operation.kind === "clear_screens" ? "新一组截图"
    : operation.kind === "request_screen_capture" ? "截图"
    : operation.kind === "set_answer_hold" ? "回答状态"
    : operation.kind === "manual_text" ? "更新上下文"
    : "回答请求";
  const state: Record<OperationStatus, string> = {
    sent: "已发送，等待确认",
    accepted: "已接收",
    running: "处理中",
    completed: "已完成",
    failed: "未完成",
    cancelled: "已取消",
  };
  return `${name} · ${state[operation.status]}`;
}

export function safeAnswerLink(url: string | undefined) {
  if (!url || !/^https?:\/\//i.test(url)) return "";
  try {
    const parsed = new URL(url);
    return parsed.username || parsed.password ? "" : parsed.href;
  } catch { return ""; }
}
