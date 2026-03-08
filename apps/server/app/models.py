from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


Speaker = Literal["interviewer", "candidate"]
GenerationMode = Literal["hybrid", "api_only"]
FastModelChoice = Literal["gpt-5-mini", "gpt-5-nano", "gpt-5-dual"]


class TranscriptTurn(BaseModel):
    speaker: Speaker
    text: str = Field(min_length=1)
    timestamp: str | None = None


class CandidateContext(BaseModel):
    name: str = ""
    target_role: str = ""
    resume: str = ""
    job_description: str = ""
    custom_notes: str = ""


class CoachRequest(BaseModel):
    turn: TranscriptTurn
    history: list[TranscriptTurn] = Field(default_factory=list)
    context: CandidateContext = Field(default_factory=CandidateContext)
    generation_mode: GenerationMode = "hybrid"
    fast_model: FastModelChoice | None = None


class AnswerVariant(BaseModel):
    label: str
    short_answer: str
    talking_points: list[str]
    source: str
    ready: bool = True
    elapsed_ms: int | None = None


class CoachResponse(BaseModel):
    topic: str
    question_type: str
    detected_follow_up: bool
    fast_answer: AnswerVariant
    fast_answer_alternatives: list[AnswerVariant] = Field(default_factory=list)
    deep_answer: AnswerVariant
    follow_up_angles: list[str]
    resume_hook: str | None = None
    context_summary: str
    confidence: float
    detail_job_id: str | None = None


class DetailJobStatus(BaseModel):
    job_id: str
    ready: bool
    version: int = 0
    answer: AnswerVariant | None = None
    error: str | None = None
