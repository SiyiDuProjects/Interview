from __future__ import annotations

import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any


def observed_at() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class InterviewHistory:
    """One ordered, session-owned record of received context, with no trimming.

    Answer entries reference the existing append-only answer store. Transcript
    entries are shared with the transcript view; they are not a second copy.
    """

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self.by_id: dict[str, dict[str, Any]] = {}
        self.turns: deque[dict[str, Any]] = deque()

    def add_turn(
        self,
        speaker: str,
        text: str,
        *,
        turn_id: str = "",
        question_id: str = "",
        corrects_turn_id: str = "",
    ) -> dict[str, Any]:
        turn_id = turn_id or f"turn-{uuid.uuid4()}"
        existing = self.by_id.get(turn_id)
        if existing is not None:
            if text:
                existing["text"] = text
            return existing
        entry: dict[str, Any] = {
            "kind": "transcript",
            "turn_id": turn_id,
            "speaker": speaker,
            "text": text,
            "question_id": question_id or (turn_id if speaker == "interviewer" else ""),
            "created_at": observed_at(),
        }
        if corrects_turn_id:
            entry["corrects_turn_id"] = corrects_turn_id
        self.entries.append(entry)
        self.by_id[turn_id] = entry
        self.turns.append(entry)
        return entry

    def add_answer(self, response_id: str, question_id: str) -> None:
        key = f"answer:{response_id}"
        if key not in self.by_id:
            entry = {"kind": "answer", "response_id": response_id, "question_id": question_id, "created_at": observed_at()}
            self.by_id[key] = entry
            self.entries.append(entry)

    def add_analysis(self, analysis_id: str, question_id: str, question: str, text: str) -> None:
        key = f"analysis:{analysis_id}"
        if key not in self.by_id:
            entry = {
                "kind": "analysis", "analysis_id": analysis_id, "question_id": question_id,
                "question": question, "text": text, "created_at": observed_at(),
            }
            self.by_id[key] = entry
            self.entries.append(entry)

    def add_screen(
        self, request_id: str, image_url: str, summary: str, *, question_id: str,
        source_id: str = "", captured_at: str = "",
    ) -> dict[str, Any]:
        key = f"screen:{request_id}"
        if key in self.by_id:
            return self.by_id[key]
        entry = {
            "kind": "screen", "request_id": request_id, "question_id": question_id,
            "image_url": image_url, "summary": summary, "source_id": source_id,
            "captured_at": captured_at or observed_at(), "created_at": observed_at(),
        }
        self.by_id[key] = entry
        self.entries.append(entry)
        return entry

    def snapshot(self, answers: dict[str, str], statuses: dict[str, str]) -> tuple[list[dict[str, Any]], list[str]]:
        records: list[dict[str, Any]] = []
        images: list[str] = []
        for entry in self.entries:
            record = dict(entry)
            if record["kind"] == "answer":
                response_id = record["response_id"]
                record["text"] = answers.get(response_id, "")
                record["status"] = statuses.get(response_id, "streaming")
                record["meaning"] = "Assistant draft; not evidence that the candidate said or adopted it."
            elif record["kind"] == "screen":
                images.append(record.pop("image_url"))
                record["image_number"] = len(images)
            elif record.get("speaker") == "candidate":
                record["meaning"] = "Candidate context; microphone capture does not prove transmission to the meeting."
            records.append(record)
        return records, images

    def transcript_snapshot(self) -> list[dict[str, Any]]:
        return [{key: value for key, value in turn.items() if key != "kind"}
                for turn in self.turns if turn["text"] or turn.get("status") == "streaming"]

    def questions(self) -> list[dict[str, str]]:
        questions: dict[str, dict[str, str]] = {}
        for entry in self.entries:
            if (entry.get("speaker") == "interviewer" or entry["kind"] == "screen_question") and entry.get("text"):
                questions[entry["question_id"]] = {key: entry[key] for key in ("question_id", "turn_id", "text", "created_at") if key in entry}
        return list(questions.values())
