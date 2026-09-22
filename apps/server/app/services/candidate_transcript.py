"""Relay native transcription items without application turn timers or batching."""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any, Protocol


class CandidateRuntime(Protocol):
    async def update_candidate_transcript(
        self, turn_id: str, text: str, status: str, *, delta: str = "",
    ) -> None: ...

    async def append_candidate_context(self, text: str) -> None: ...


@dataclass
class _Item:
    turn_id: str
    text: str = ""
    status: str = "streaming"


class CandidateTranscriptRelay:
    """One item per provider turn; send every delta without a batching timer.

    The FIFO worker only isolates socket backpressure from transcript collection.
    Native VAD starts reserve history positions; finals can arrive out of order.
    """

    def __init__(self, runtime: CandidateRuntime) -> None:
        self.runtime = runtime
        self._namespace = uuid.uuid4().hex
        self._items: dict[str, _Item] = {}
        self._seen: set[str] = set()
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._closing: asyncio.Task[None] | None = None
        self._failure: BaseException | None = None
        self._closed = False

    async def handle(self, event: dict[str, Any]) -> None:
        self._ensure_writable()
        event_id = str(event.get("event_id") or "")
        if event_id and event_id in self._seen:
            return
        if event_id:
            self._seen.add(event_id)
        kind, item_id = event.get("type"), str(event.get("item_id") or "")
        if kind in {"input_audio_buffer.speech_started", "input_audio_buffer.committed"}:
            await self.start(item_id)
        elif kind == "conversation.item.input_audio_transcription.delta":
            await self.add(str(event.get("delta") or ""), item_id, event_id=event_id)
        elif kind == "conversation.item.input_audio_transcription.completed":
            await self.complete(str(event.get("transcript") or ""), item_id)
        # speech_stopped is not transcript completion. The server commits the
        # turn; keep accepting deltas until the native completed event.

    async def start(self, item_id: str) -> _Item:
        self._ensure_writable()
        if not item_id:
            raise ValueError("Candidate transcription event is missing its native item_id.")
        if item_id not in self._items:
            self._items[item_id] = _Item(f"candidate:{self._namespace}:{item_id}")
            await self.runtime.update_candidate_transcript(self._items[item_id].turn_id, "", "streaming")
        return self._items[item_id]

    async def add(self, delta: str, item_id: str, *, event_id: str = "") -> None:
        item = await self.start(item_id)
        if not delta or item.status != "streaming":
            return
        item.text += delta
        await self.runtime.update_candidate_transcript(item.turn_id, item.text, "streaming", delta=delta)
        self._enqueue(item, delta=delta, event_id=event_id or uuid.uuid4().hex)

    async def complete(self, transcript: str, item_id: str) -> None:
        item = await self.start(item_id)
        if item.status == "completed" and item.text == transcript:
            return
        item.text, item.status = transcript, "completed"
        await self.runtime.update_candidate_transcript(item.turn_id, item.text, item.status)
        self._enqueue(item, text=transcript,
            meaning="Final ASR for this same turn; supersedes provisional text or earlier ASR. Recognition correction is not a new candidate choice.")

    def _enqueue(self, item: _Item, **content: str) -> None:
        # JSON preserves whitespace, including a delta containing only a space.
        # Never repeat the growing partial text.
        self._queue.put_nowait(json.dumps({"source": "candidate", "turn_id": item.turn_id,
            "status": item.status, **content}, ensure_ascii=False))
        if self._worker is None:
            self._worker = asyncio.create_task(self._forward())
            self._worker.add_done_callback(self._record_failure)

    async def _forward(self) -> None:
        while True:
            text = await self._queue.get()
            try:
                if text is None:
                    return
                await self.runtime.append_candidate_context(text)
            finally:
                self._queue.task_done()

    async def close(self) -> None:
        if self._closing is None:
            self._closed = True
            self._closing = asyncio.create_task(self._finish())
        await asyncio.shield(self._closing)

    async def _finish(self) -> None:
        for item in self._items.values():
            if item.status == "streaming":
                item.status = "interrupted"
                await self.runtime.update_candidate_transcript(item.turn_id, item.text, item.status)
                if item.text:
                    self._enqueue(item, text=item.text,
                        meaning="Transcription connection ended before a final result; this text remains provisional, not a completed utterance.")
        if self._worker is not None:
            self._queue.put_nowait(None)
            await self._worker

    def _record_failure(self, task: asyncio.Task[None]) -> None:
        self._failure = asyncio.CancelledError() if task.cancelled() else task.exception()

    def _ensure_writable(self) -> None:
        if self._closed:
            raise RuntimeError("Candidate transcription is closed.")
        if self._failure is not None:
            raise RuntimeError("Candidate transcript forwarding failed.") from None
