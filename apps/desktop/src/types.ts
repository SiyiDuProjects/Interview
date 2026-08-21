export type Speaker = "interviewer" | "candidate";

export type SessionPhase = "idle" | "starting" | "live" | "stopping";

export type ChannelPhase =
  | "idle"
  | "connecting"
  | "listening"
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

export interface AnswerRecord {
  responseId: string;
  question?: string;
  text: string;
  status: AnswerStatus;
  createdAt: string;
  detail?: string;
}

export interface AnswerStore {
  order: string[];
  byId: Record<string, AnswerRecord>;
}

export interface ServerEvent {
  type?: string;
  speaker?: Speaker;
  response_id?: string;
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
  channels?: Partial<Record<Speaker, boolean>>;
  turns?: Array<{ speaker?: Speaker; text?: string }>;
}
