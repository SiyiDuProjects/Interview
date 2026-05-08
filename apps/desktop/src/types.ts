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
