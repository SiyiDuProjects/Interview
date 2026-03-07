export type Speaker = "interviewer" | "candidate";
export type GenerationMode = "hybrid" | "api_only";

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

export interface AnswerVariant {
  label: string;
  short_answer: string;
  talking_points: string[];
  source: string;
  ready: boolean;
}

export interface CoachResponse {
  topic: string;
  question_type: string;
  detected_follow_up: boolean;
  fast_answer: AnswerVariant;
  deep_answer: AnswerVariant;
  follow_up_angles: string[];
  resume_hook?: string | null;
  context_summary: string;
  confidence: number;
  detail_job_id?: string | null;
}

export interface DetailJobStatus {
  job_id: string;
  ready: boolean;
  version: number;
  answer?: AnswerVariant | null;
  error?: string | null;
}

export interface AnswerFeedItem extends CoachResponse {
  id: string;
  prompt: string;
  timestamp: string;
  createdAtMs?: number;
}
