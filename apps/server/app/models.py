from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


Speaker = Literal["interviewer", "candidate"]
AnswerScope = Literal["general", "innovation_ai", "canvasbot", "discordbot"]


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
