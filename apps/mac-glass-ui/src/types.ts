export type Speaker = "interviewer" | "candidate";

export interface TranscriptTurn {
  speaker: Speaker;
  text: string;
  timestamp?: string;
}

export interface CandidateContext {
  name: string;
  target_role: string;
  resume: string;
  job_description: string;
  custom_notes: string;
}

export interface RealtimeAnswer {
  id: string;
  text: string;
  status: "pending" | "streaming" | "done" | "error";
  timestamp: string;
  detail?: string;
}

export interface RealtimeMessage {
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
