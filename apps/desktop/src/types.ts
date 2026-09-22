export type Speaker = "interviewer" | "candidate";

export type SessionPhase = "idle" | "starting" | "live" | "stopping";

export type ChannelPhase =
  | "idle"
  | "connecting"
  | "ready"
  | "listening"
  | "muted"
  | "interrupted"
  | "reconnecting"
  | "error";

export type AnswerStatus = "streaming" | "completed" | "interrupted" | "error";

export type DeviceStatus = "offline" | "initializing" | "ready" | "error";

export interface InterviewSession {
  interview_id: string;
  session_token: string;
  capture_token?: string;
}

export interface ChannelState {
  phase: ChannelPhase;
  message: string;
}

export interface TranscriptState {
  final: string;
  partial: string;
}

export interface TranscriptTurn {
  turn_id: string;
  speaker: Speaker;
  text: string;
  question_id?: string;
  kind?: string;
  created_at?: string;
  status?: "streaming" | "completed" | "interrupted";
}

export interface QuestionRecord {
  question_id: string;
  text: string;
  turn_id?: string;
  created_at?: string;
}

export type ManualTextKind = "question" | "correction" | "candidate_context";
export type QuickAnswerAction = "answer" | "shorten" | "expand" | "rephrase" | "deep";
export type OperationStatus = "sent" | "accepted" | "running" | "completed" | "failed" | "cancelled";

export interface OperationRecord {
  operation_id: string;
  kind: string;
  status: OperationStatus;
  action?: string;
  detail?: string;
  question_id?: string;
  response_id?: string;
  created_at?: string;
}

export interface ChannelHealth {
  phase?: string;
  message?: string;
  detail?: string;
}

export interface AnswerRecord {
  responseId: string;
  questionId?: string;
  question?: string;
  text: string;
  status: AnswerStatus;
  createdAt: string;
  detail?: string;
  intermediate?: boolean;
}

export interface AnswerStore {
  order: string[];
  byId: Record<string, AnswerRecord>;
}

export interface ServerEvent {
  documents_count?: number;
  characters_count?: number;
  realtime_protocol?: string;
  workspace?: CodeWorkspace;
  screens?: CapturedScreen[];
  type?: string;
  speaker?: Speaker;
  response_id?: string;
  intermediate?: boolean;
  request_id?: string;
  question?: string;
  delta?: string;
  text?: string;
  status?: string;
  detail?: string;
  error?: string;
  message?: string;
  created_at?: string;
  active?: boolean;
  hold_answers?: boolean;
  current_question_id?: string;
  question_id?: string;
  turn_id?: string;
  kind?: string;
  action?: string;
  tool?: string;
  operation_id?: string;
  operations?: OperationRecord[];
  metrics?: Record<string, unknown>;
  questions?: QuestionRecord[];
  channel_details?: Partial<Record<Speaker, ChannelHealth>>;
  channels?: Partial<Record<Speaker, boolean>>;
  turns?: Array<Partial<TranscriptTurn>>;
}

export interface CodeProposal {
  proposal_id: string;
  base_revision: number;
  base_code: string;
  code: string;
  language: string;
  explanation: string;
  context_changed: boolean;
  code_changed: boolean;
}

export interface CodeWorkspace {
  document_id: string;
  revision: number;
  context_version: number;
  code: string;
  language: string;
  can_undo: boolean;
  run_id: string;
  proposal: CodeProposal | null;
  reveal_id?: string;
  last_change?: { base_code: string; code: string; language: string; explanation: string; revision: number } | null;
}

export interface CapturedScreen {
  request_id: string;
  question_id: string;
  captured_at: string;
  source_id: string;
}
