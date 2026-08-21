from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import json
import secrets
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
import websockets
from fastapi import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import ClientConnection

from app.config import get_settings
from app.models import ConnectionRole, Speaker
from app.services.context_store import ContextStore
from app.services.realtime_context import build_realtime_instructions


class OpenAIRealtimeError(RuntimeError):
    pass


AUDIO_INPUT_FORMAT: dict[str, Any] = {"type": "audio/pcm", "rate": 24000}
TEXT_OUTPUT_MODALITIES = ["text"]
ALLOWED_SCREENSHOT_MIME_TYPES = {"image/png", "image/jpeg", "image/webp"}
MAX_PENDING_CANDIDATE_CONTEXT = 50
MAX_RECENT_DIALOGUE = 40
AUTHENTICATION_TIMEOUT_SECONDS = 5.0
CLIENT_SEND_TIMEOUT_SECONDS = 2.0
CLIENT_SNAPSHOT_TIMEOUT_SECONDS = 5.0
MAX_AUDIO_FRAME_BYTES = 256 * 1024
MAX_MANUAL_TEXT_CHARS = 12_000


class InterviewRuntime:
    """All mutable state and both OpenAI sessions for one interview."""

    def __init__(
        self,
        *,
        interview_id: str,
        session_token: str,
        capture_token: str,
        expires_at: datetime,
        context_store: ContextStore | None = None,
    ) -> None:
        self.interview_id = interview_id
        self.session_token = session_token
        self.capture_token = capture_token
        self.expires_at = expires_at
        self.context_store = context_store or ContextStore()

        self.main_upstream: ClientConnection | None = None
        self.candidate_upstream: ClientConnection | None = None
        self._main_reader_task: asyncio.Task[None] | None = None
        self._candidate_reader_task: asyncio.Task[None] | None = None
        self._upstream_lock = asyncio.Lock()
        self._event_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._answer_lock = asyncio.Lock()
        self._capture_clients: dict[Speaker, WebSocket] = {}
        self._capture_ready: set[Speaker] = set()
        self._ui_clients: dict[str, WebSocket] = {}
        self._ready_ui_clients: set[str] = set()
        self._closed = False
        self.active = False

        self.pending_candidate_context: deque[str] = deque(maxlen=MAX_PENDING_CANDIDATE_CONTEXT)
        self.recent_dialogue: deque[dict[str, str]] = deque(maxlen=MAX_RECENT_DIALOGUE)
        self.pending_screen_requests: dict[str, asyncio.Future[str]] = {}
        self.latest_screen_image_url = ""
        self.latest_screen_summary = ""
        self.latest_retrieval: list[dict[str, str | int]] = []

        self.active_response_id = ""
        self.response_buffers: dict[str, str] = {}
        self.response_order: list[str] = []
        self.response_status: dict[str, Literal["streaming", "completed", "interrupted", "error"]] = {}
        self.response_details: dict[str, str] = {}
        self.started_responses: set[str] = set()
        self.terminal_responses: set[str] = set()

    @property
    def closed(self) -> bool:
        return self._closed

    def token_matches(self, token: str) -> bool:
        return bool(token) and hmac.compare_digest(self.session_token, token)

    def capture_token_matches(self, token: str) -> bool:
        return bool(token) and hmac.compare_digest(self.capture_token, token)

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.active or self._capture_clients:
            return False
        return self.expires_at <= (now or datetime.now(timezone.utc))

    async def serve(self, websocket: WebSocket, role: ConnectionRole) -> None:
        await websocket.accept()
        if not await self._authenticate(websocket, role):
            return
        if role == "client":
            await self._serve_ui_client(websocket)
        else:
            await self._serve_capture_client(websocket, role)

    async def _serve_ui_client(self, websocket: WebSocket) -> None:
        client_id = secrets.token_urlsafe(12)
        registered = False
        try:
            async with asyncio.timeout(CLIENT_SNAPSHOT_TIMEOUT_SECONDS):
                async with self._event_lock:
                    if self._closed or self.is_expired():
                        await _send_websocket_json(
                            websocket, {"type": "error", "detail": "Interview session expired."}
                        )
                        await websocket.close(code=1008)
                        return
                    self._ui_clients[client_id] = websocket
                    registered = True
                    await _send_websocket_json(
                        websocket,
                        {
                            "type": "session_ready",
                            "speaker": "client",
                            "source": get_settings().openai_realtime_model,
                            "interview_id": self.interview_id,
                        },
                    )
                    await _send_websocket_json(websocket, self._device_status_payload())
                    await _send_websocket_json(websocket, self._interview_state_payload())
                    async with self._state_lock:
                        turns = list(self.recent_dialogue)
                    await _send_websocket_json(
                        websocket, {"type": "transcript_snapshot", "turns": turns}
                    )
                    await self._send_answer_snapshots_locked(websocket)
                    self._ready_ui_clients.add(client_id)

            await _forward_ui_controls(self, websocket)
        finally:
            if registered:
                async with self._event_lock:
                    if self._ui_clients.get(client_id) is websocket:
                        self._ui_clients.pop(client_id, None)
                        self._ready_ui_clients.discard(client_id)

    async def _serve_capture_client(self, websocket: WebSocket, speaker: Speaker) -> None:
        registered = False
        try:
            async with self._event_lock:
                if self._closed or self.is_expired():
                    await _send_websocket_json(
                        websocket, {"type": "error", "detail": "Interview session expired."}
                    )
                    await websocket.close(code=1008)
                    return
                if speaker in self._capture_clients:
                    await _send_websocket_json(
                        websocket, {"type": "error", "detail": f"{speaker} is already connected."}
                    )
                    await websocket.close(code=1008)
                    return
                self._capture_clients[speaker] = websocket
                registered = True
                source = (
                    get_settings().openai_realtime_model
                    if speaker == "interviewer"
                    else f"{get_settings().openai_realtime_transcription_model}:context"
                )
                await _send_websocket_json(
                    websocket,
                    {
                        "type": "session_ready",
                        "speaker": speaker,
                        "source": source,
                        "interview_id": self.interview_id,
                    },
                )
                await self._broadcast_clients_locked(self._device_status_payload())

            await _forward_capture_controls(self, websocket, speaker)
        finally:
            if registered:
                async with self._event_lock:
                    if self._capture_clients.get(speaker) is websocket:
                        self._capture_clients.pop(speaker, None)
                        self._capture_ready.discard(speaker)
                        await self._broadcast_clients_locked(self._device_status_payload())

    async def _authenticate(self, websocket: WebSocket, role: ConnectionRole) -> bool:
        try:
            payload = await asyncio.wait_for(
                websocket.receive_json(),
                timeout=AUTHENTICATION_TIMEOUT_SECONDS,
            )
        except (asyncio.TimeoutError, json.JSONDecodeError, TypeError, WebSocketDisconnect):
            await _close_websocket(websocket, code=1008)
            return False
        except Exception:
            await _close_websocket(websocket, code=1008)
            return False
        if not isinstance(payload, dict) or payload.get("type") != "authenticate":
            await _close_websocket(websocket, code=1008)
            return False
        token = str(payload.get("token") or "")
        authenticated = self.token_matches(token) if role == "client" else self.capture_token_matches(token)
        if not authenticated:
            await _close_websocket(websocket, code=1008)
            return False
        return True

    async def _send_answer_snapshots_locked(self, websocket: WebSocket) -> None:
        async with self._answer_lock:
            for response_id in self.response_order:
                payload: dict[str, Any] = {
                    "type": "answer_snapshot",
                    "response_id": response_id,
                    "text": self.response_buffers.get(response_id, ""),
                    "status": self.response_status.get(response_id, "streaming"),
                }
                detail = self.response_details.get(response_id, "")
                if detail:
                    payload["detail"] = detail
                await _send_websocket_json(websocket, payload)

    async def public_state(self) -> dict[str, Any]:
        async with self._event_lock:
            device = self._device_status_payload()
            interview = self._interview_state_payload()
            return {
                "interview_id": self.interview_id,
                "session_token": self.session_token,
                "expires_at": self.expires_at.isoformat().replace("+00:00", "Z"),
                "device_status": {"status": device["status"], "channels": device["channels"]},
                "interview_state": {"active": interview["active"]},
            }

    def _device_status_payload(self) -> dict[str, Any]:
        connected = set(self._capture_clients)
        channels = {
            "interviewer": "interviewer" in self._capture_ready,
            "candidate": "candidate" in self._capture_ready,
        }
        if all(channels.values()):
            status = "ready"
        elif connected:
            status = "initializing"
        else:
            status = "offline"
        return {"type": "device_status", "status": status, "channels": channels}

    def _interview_state_payload(self) -> dict[str, Any]:
        return {"type": "interview_state", "active": self.active}

    async def broadcast_to_clients(self, payload: dict[str, Any]) -> None:
        async with self._event_lock:
            await self._broadcast_clients_locked(payload)

    async def _broadcast_clients_locked(self, payload: dict[str, Any]) -> None:
        peers = [
            (client_id, self._ui_clients.get(client_id))
            for client_id in tuple(self._ready_ui_clients)
        ]

        async def send_one(client_id: str, websocket: WebSocket | None) -> str | None:
            if websocket is None:
                return client_id
            try:
                await _send_websocket_json(websocket, payload)
                return None
            except Exception:
                return client_id

        failed = [
            client_id
            for client_id in await asyncio.gather(*(send_one(*peer) for peer in peers))
            if client_id is not None
        ]
        for client_id in failed:
            self._ui_clients.pop(client_id, None)
            self._ready_ui_clients.discard(client_id)

    async def send_to_ui_client(self, websocket: WebSocket, payload: dict[str, Any]) -> bool:
        async with self._event_lock:
            if websocket not in self._ui_clients.values():
                return False
            try:
                await _send_websocket_json(websocket, payload)
                return True
            except Exception:
                for client_id, registered in tuple(self._ui_clients.items()):
                    if registered is websocket:
                        self._ui_clients.pop(client_id, None)
                        self._ready_ui_clients.discard(client_id)
                return False

    async def send_to_capture(self, speaker: Speaker, payload: dict[str, Any]) -> bool:
        async with self._event_lock:
            return await self._send_to_capture_locked(speaker, payload)

    async def _send_to_capture_locked(self, speaker: Speaker, payload: dict[str, Any]) -> bool:
        websocket = self._capture_clients.get(speaker)
        if websocket is None:
            return False
        try:
            await _send_websocket_json(websocket, payload)
            return True
        except Exception:
            if self._capture_clients.get(speaker) is websocket:
                self._capture_clients.pop(speaker, None)
                self._capture_ready.discard(speaker)
                await self._broadcast_clients_locked(self._device_status_payload())
            return False

    async def mark_capture_ready(self, speaker: Speaker, websocket: WebSocket) -> None:
        async with self._event_lock:
            if self._capture_clients.get(speaker) is not websocket:
                return
            self._capture_ready.add(speaker)
            if self.active:
                await self._send_to_capture_locked(speaker, {"type": "capture_start"})
            await self._broadcast_clients_locked(self._device_status_payload())

    async def start_interview(self, requester: WebSocket) -> None:
        async with self._event_lock:
            if requester not in self._ui_clients.values():
                return
            if self._capture_ready != {"interviewer", "candidate"}:
                await _send_websocket_json(
                    requester, {"type": "error", "detail": "Capture device is not ready."}
                )
                return
            if not self.active:
                interviewer_started = await self._send_to_capture_locked(
                    "interviewer", {"type": "capture_start"}
                )
                candidate_started = await self._send_to_capture_locked(
                    "candidate", {"type": "capture_start"}
                )
                if not interviewer_started or not candidate_started:
                    if interviewer_started:
                        await self._send_to_capture_locked("interviewer", {"type": "capture_stop"})
                    if candidate_started:
                        await self._send_to_capture_locked("candidate", {"type": "capture_stop"})
                    self.active = False
                    await _send_websocket_json(
                        requester, {"type": "error", "detail": "Capture device disconnected."}
                    )
                    await self._broadcast_clients_locked(self._interview_state_payload())
                    return
                self.active = True
            await self._broadcast_clients_locked(self._interview_state_payload())

    async def emit_transcript_delta(self, speaker: Speaker, delta: str) -> None:
        if not delta:
            return
        async with self._event_lock:
            await self._broadcast_clients_locked(
                {"type": "transcript_delta", "speaker": speaker, "delta": delta}
            )

    async def emit_transcript_final(self, speaker: Speaker, text: str) -> None:
        normalized = text.strip()
        if not normalized:
            return
        async with self._event_lock:
            async with self._state_lock:
                self.recent_dialogue.append({"speaker": speaker, "text": normalized[-3000:]})
            await self._broadcast_clients_locked(
                {"type": "transcript_final", "speaker": speaker, "text": normalized}
            )

    async def ensure_main(self) -> ClientConnection:
        async with self._upstream_lock:
            self._ensure_open()
            if self.main_upstream is None:
                upstream = await _connect_openai_realtime(kind="main")
                try:
                    await _send_session_update(upstream)
                    while self.pending_candidate_context:
                        await _send_context_item(upstream, self.pending_candidate_context.popleft())
                except Exception:
                    await _safe_close(upstream)
                    raise
                self.main_upstream = upstream
                self._main_reader_task = asyncio.create_task(self._run_main_reader(upstream))
            return self.main_upstream

    async def ensure_candidate(self) -> ClientConnection:
        async with self._upstream_lock:
            self._ensure_open()
            if self.candidate_upstream is None:
                upstream = await _connect_openai_realtime(kind="candidate")
                try:
                    await _send_transcription_session_update(upstream)
                except Exception:
                    await _safe_close(upstream)
                    raise
                self.candidate_upstream = upstream
                self._candidate_reader_task = asyncio.create_task(self._run_candidate_reader(upstream))
            return self.candidate_upstream

    async def append_candidate_context(self, text: str) -> None:
        normalized = text.strip()
        if not normalized:
            return
        async with self._upstream_lock:
            if self._closed:
                return
            upstream = self.main_upstream
            if upstream is None:
                self.pending_candidate_context.append(normalized)
                return
            await _send_context_item(upstream, normalized)

    async def remember_dialogue(self, speaker: Speaker, text: str) -> None:
        normalized = text.strip()
        if not normalized:
            return
        async with self._state_lock:
            self.recent_dialogue.append({"speaker": speaker, "text": normalized[-3000:]})

    async def update_screen(self, image_url: str, summary: str) -> None:
        async with self._state_lock:
            self.latest_screen_image_url = image_url
            self.latest_screen_summary = summary[-1000:]

    async def update_retrieval(self, matches: list[dict[str, str | int]]) -> None:
        async with self._state_lock:
            self.latest_retrieval = matches

    async def analysis_context(self) -> tuple[list[dict[str, str]], str, str, list[dict[str, str | int]]]:
        async with self._state_lock:
            return (
                list(self.recent_dialogue),
                self.latest_screen_image_url,
                self.latest_screen_summary,
                list(self.latest_retrieval),
            )

    async def close(self, *, websocket_code: int = 1000) -> None:
        async with self._upstream_lock:
            if self._closed:
                return
            self._closed = True
            upstreams = [self.main_upstream, self.candidate_upstream]
            self.main_upstream = None
            self.candidate_upstream = None
            reader_tasks = [self._main_reader_task, self._candidate_reader_task]
            self._main_reader_task = None
            self._candidate_reader_task = None
            futures = list(self.pending_screen_requests.values())
            self.pending_screen_requests.clear()
            self.pending_candidate_context.clear()

        async with self._event_lock:
            await self._broadcast_clients_locked({"type": "session_ended"})
            capture_clients = list(self._capture_clients.values())
            for websocket in capture_clients:
                try:
                    await _send_websocket_json(websocket, {"type": "session_ended"})
                except Exception:
                    pass
            clients = list(self._ui_clients.values()) + capture_clients
            self._ui_clients.clear()
            self._ready_ui_clients.clear()
            self._capture_clients.clear()
            self._capture_ready.clear()
            self.active = False
            async with self._answer_lock:
                self.response_buffers.clear()
                self.response_order.clear()
                self.response_status.clear()
                self.response_details.clear()
                self.started_responses.clear()
                self.terminal_responses.clear()

        for future in futures:
            if not future.done():
                future.set_exception(OpenAIRealtimeError("Interview session closed."))
        for task in reader_tasks:
            if task is not None:
                task.cancel()
        for upstream in upstreams:
            if upstream is not None:
                await _safe_close(upstream)
        for task in reader_tasks:
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
        for websocket in clients:
            try:
                await websocket.close(code=websocket_code)
            except Exception:
                pass

    async def _run_main_reader(self, upstream: ClientConnection) -> None:
        try:
            await _forward_main_events(self, upstream)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closed:
                await self.broadcast_to_clients({"type": "error", "detail": str(exc)})
        finally:
            await self._release_upstream("main", upstream)

    async def _run_candidate_reader(self, upstream: ClientConnection) -> None:
        try:
            await _forward_candidate_events(self, upstream)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closed:
                await self.broadcast_to_clients({"type": "error", "detail": str(exc)})
        finally:
            await self._release_upstream("candidate", upstream)

    async def _release_upstream(self, kind: Literal["main", "candidate"], upstream: ClientConnection) -> None:
        async with self._upstream_lock:
            if kind == "main" and self.main_upstream is upstream:
                self.main_upstream = None
                self._main_reader_task = None
            elif kind == "candidate" and self.candidate_upstream is upstream:
                self.candidate_upstream = None
                self._candidate_reader_task = None
        await _safe_close(upstream)

    def _ensure_open(self) -> None:
        if self._closed or self.is_expired():
            raise OpenAIRealtimeError("Interview session expired.")


class InterviewRegistry:
    def __init__(self) -> None:
        self._current: InterviewRuntime | None = None
        self._lock = asyncio.Lock()

    async def create(self) -> InterviewRuntime:
        settings = get_settings()
        now = datetime.now(timezone.utc)
        expired: InterviewRuntime | None = None
        async with self._lock:
            if self._current is not None and not self._current.closed and not self._current.is_expired(now):
                return self._current
            expired = self._current
            runtime = InterviewRuntime(
                interview_id=str(uuid.uuid4()),
                session_token=secrets.token_urlsafe(32),
                capture_token=secrets.token_urlsafe(32),
                expires_at=now + timedelta(seconds=settings.interview_session_ttl_seconds),
            )
            self._current = runtime
        if expired is not None:
            await expired.close(websocket_code=1008)
        return runtime

    async def get(self, interview_id: str) -> InterviewRuntime | None:
        runtime = await self.current()
        if runtime is None or runtime.interview_id != interview_id:
            return None
        return runtime

    async def current(self) -> InterviewRuntime | None:
        expired: InterviewRuntime | None = None
        async with self._lock:
            runtime = self._current
            if runtime is not None and (runtime.closed or runtime.is_expired()):
                expired = runtime
                self._current = None
                runtime = None
        if expired is not None:
            await expired.close(websocket_code=1008)
        return runtime

    async def delete(self, interview_id: str) -> InterviewRuntime | None:
        async with self._lock:
            runtime = self._current
            if runtime is None or runtime.interview_id != interview_id:
                return None
            self._current = None
        if runtime is not None:
            await runtime.close()
        return runtime

    async def clear(self) -> None:
        async with self._lock:
            runtime = self._current
            self._current = None
        if runtime is not None:
            await runtime.close()


_registry = InterviewRegistry()


def get_interview_registry() -> InterviewRegistry:
    return _registry


async def _forward_capture_controls(
    runtime: InterviewRuntime,
    websocket: WebSocket,
    speaker: Speaker,
) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return

        binary_payload = message.get("bytes")
        if binary_payload is not None:
            if len(binary_payload) > MAX_AUDIO_FRAME_BYTES:
                await _send_websocket_json(
                    websocket, {"type": "error", "detail": "Audio frame is too large."}
                )
                continue
            if runtime.active and binary_payload:
                upstream = await (
                    runtime.ensure_main() if speaker == "interviewer" else runtime.ensure_candidate()
                )
                await _send_audio_append(upstream, binary_payload)
            continue

        text_payload = message.get("text")
        if not text_payload:
            continue
        try:
            payload = json.loads(text_payload)
        except json.JSONDecodeError:
            await _send_websocket_json(
                websocket, {"type": "error", "detail": "Invalid JSON control message."}
            )
            continue
        payload_type = payload.get("type")

        if payload_type == "close":
            return
        if payload_type == "capture_ready":
            await runtime.mark_capture_ready(speaker, websocket)
            continue
        if payload_type == "screen_snapshot" and speaker == "interviewer":
            await _resolve_screen_snapshot(runtime, payload)
            continue
        await _send_websocket_json(
            websocket, {"type": "error", "detail": "Unsupported capture control message."}
        )


async def _forward_ui_controls(runtime: InterviewRuntime, websocket: WebSocket) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        if message.get("bytes") is not None:
            await runtime.send_to_ui_client(
                websocket, {"type": "error", "detail": "UI clients cannot send audio."}
            )
            continue
        text_payload = message.get("text")
        if not text_payload:
            continue
        try:
            payload = json.loads(text_payload)
        except json.JSONDecodeError:
            await runtime.send_to_ui_client(
                websocket, {"type": "error", "detail": "Invalid JSON control message."}
            )
            continue
        payload_type = payload.get("type")
        if payload_type == "close":
            return
        if payload_type == "start_interview":
            await runtime.start_interview(websocket)
            continue
        if payload_type == "manual_text":
            if not runtime.active:
                await runtime.send_to_ui_client(
                    websocket, {"type": "error", "detail": "Interview is not active."}
                )
                continue
            text = str(payload.get("text") or "").strip()
            if not text:
                continue
            if len(text) > MAX_MANUAL_TEXT_CHARS:
                await runtime.send_to_ui_client(
                    websocket, {"type": "error", "detail": "Manual text is too long."}
                )
                continue
            await runtime.emit_transcript_final("interviewer", text)
            upstream = await runtime.ensure_main()
            await _send_user_text(upstream, f"[Interviewer] {text}", create_response=True)
            continue
        if payload_type == "request_screen_capture":
            if not runtime.active:
                await runtime.send_to_ui_client(
                    websocket, {"type": "error", "detail": "Interview is not active."}
                )
                continue
            task = asyncio.create_task(_capture_screen_for_ui(runtime, websocket))
            task.add_done_callback(_consume_task_result)
            continue
        await runtime.send_to_ui_client(
            websocket, {"type": "error", "detail": "Unsupported UI control message."}
        )


def _consume_task_result(task: asyncio.Task[None]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


async def _resolve_screen_snapshot(runtime: InterviewRuntime, payload: dict[str, Any]) -> None:
    request_id = str(payload.get("request_id") or "")
    future = runtime.pending_screen_requests.get(request_id)
    if future is None or future.done():
        return
    error = str(payload.get("error") or "").strip()
    if error:
        future.set_exception(OpenAIRealtimeError(error))
        return
    try:
        image_url = _validate_image_data_url(str(payload.get("image_data") or ""))
    except OpenAIRealtimeError as exc:
        future.set_exception(exc)
    else:
        future.set_result(image_url)


async def _forward_main_events(runtime: InterviewRuntime, upstream: ClientConnection) -> None:
    async for raw_message in upstream:
        if isinstance(raw_message, bytes):
            continue
        payload = json.loads(raw_message)
        event_type = str(payload.get("type") or "")

        if event_type == "response.created":
            response_id = _event_response_id(payload)
            if response_id:
                await _begin_response(runtime, response_id)
            continue

        if event_type == "response.output_text.delta":
            response_id = _resolved_response_id(runtime, payload)
            delta = str(payload.get("delta") or "")
            if response_id and delta:
                await _emit_answer_delta(runtime, response_id, delta)
            continue

        if event_type == "response.output_text.done":
            response_id = _resolved_response_id(runtime, payload)
            text = str(payload.get("text") or "").strip()
            if response_id and text:
                await _set_answer_text(runtime, response_id, text)
            continue

        if event_type == "response.content_part.done":
            response_id = _resolved_response_id(runtime, payload)
            text = _extract_content_part_text(payload.get("part"))
            if response_id and text:
                await _set_answer_text(runtime, response_id, text)
            continue

        if event_type == "response.output_item.done":
            response_id = _resolved_response_id(runtime, payload)
            text = _extract_response_item_text(payload.get("item"))
            if response_id and text:
                await _set_answer_text(runtime, response_id, text)
            continue

        if event_type == "response.done":
            await _emit_response_terminal(runtime, payload)
            continue

        if event_type in {"response.cancelled", "response.canceled"}:
            response_id = _resolved_response_id(runtime, payload)
            if response_id:
                await _emit_terminal(
                    runtime,
                    response_id=response_id,
                    event_type="answer_interrupted",
                    text=None,
                    detail="cancelled",
                )
            continue

        if event_type == "response.function_call_arguments.done":
            await _handle_tool_call(runtime, upstream, payload)
            continue

        if event_type == "conversation.item.input_audio_transcription.delta":
            delta = str(payload.get("delta") or "")
            if delta:
                await runtime.emit_transcript_delta("interviewer", delta)
            continue

        if event_type == "conversation.item.input_audio_transcription.completed":
            transcript = str(payload.get("transcript") or "").strip()
            if transcript:
                await runtime.emit_transcript_final("interviewer", transcript)
            continue

        if event_type == "error":
            response_id = _resolved_response_id(runtime, payload)
            detail = _extract_error(payload)
            if response_id:
                await _emit_terminal(
                    runtime,
                    response_id=response_id,
                    event_type="answer_error",
                    text=None,
                    detail=detail,
                )
            else:
                await runtime.broadcast_to_clients({"type": "error", "detail": detail})


async def _forward_candidate_events(runtime: InterviewRuntime, upstream: ClientConnection) -> None:
    accumulator = _CandidateTranscriptAccumulator(runtime)
    try:
        async for raw_message in upstream:
            if isinstance(raw_message, bytes):
                continue
            payload = json.loads(raw_message)
            event_type = payload.get("type")
            if event_type == "conversation.item.input_audio_transcription.delta":
                await accumulator.add(str(payload.get("delta") or ""))
            elif event_type == "conversation.item.input_audio_transcription.completed":
                await accumulator.complete(str(payload.get("transcript") or ""))
            elif event_type == "error":
                await runtime.broadcast_to_clients({"type": "error", "detail": _extract_error(payload)})
    finally:
        await accumulator.close()


class _CandidateTranscriptAccumulator:
    """Finalize transcription-only deltas after a short quiet period.

    The transcription intent does not support server VAD and does not reliably
    emit `completed` without an explicit client commit, so delta silence is the
    local turn boundary. A later API `completed` event is still accepted and
    de-duplicated.
    """

    def __init__(self, runtime: InterviewRuntime, *, quiet_seconds: float = 1.0) -> None:
        self.runtime = runtime
        self.quiet_seconds = quiet_seconds
        self.buffer = ""
        self._timer: asyncio.Task[None] | None = None
        self._recent_finals: deque[str] = deque(maxlen=8)
        self._final_lock = asyncio.Lock()

    async def add(self, delta: str) -> None:
        if not delta:
            return
        self.buffer += delta
        await self.runtime.emit_transcript_delta("candidate", delta)
        self._cancel_timer()
        self._timer = asyncio.create_task(self._flush_after_quiet())

    async def complete(self, transcript: str) -> None:
        text = transcript.strip() or self.buffer.strip()
        self.buffer = ""
        await self._emit_final(text)
        self._cancel_timer()

    async def close(self) -> None:
        text = self.buffer.strip()
        self.buffer = ""
        await self._emit_final(text)
        self._cancel_timer()

    async def _flush_after_quiet(self) -> None:
        try:
            await asyncio.sleep(self.quiet_seconds)
            text = self.buffer.strip()
            self.buffer = ""
            await self._emit_final(text)
        except asyncio.CancelledError:
            return
        finally:
            if self._timer is asyncio.current_task():
                self._timer = None

    async def _emit_final(self, text: str) -> None:
        normalized = text.strip()
        if not normalized:
            return
        async with self._final_lock:
            if normalized in self._recent_finals:
                return
            self._recent_finals.append(normalized)
            await self.runtime.emit_transcript_final("candidate", normalized)
            await self.runtime.append_candidate_context(f"[Candidate context; do not answer] {normalized}")

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None


async def _begin_response(runtime: InterviewRuntime, response_id: str) -> None:
    async with runtime._event_lock:
        async with runtime._answer_lock:
            runtime.active_response_id = response_id
            runtime.response_buffers.setdefault(response_id, "")
            await _emit_answer_started_locked(runtime, response_id)


async def _emit_answer_started_locked(runtime: InterviewRuntime, response_id: str) -> None:
    if response_id in runtime.started_responses:
        return
    runtime.started_responses.add(response_id)
    runtime.response_order.append(response_id)
    runtime.response_buffers.setdefault(response_id, "")
    runtime.response_status[response_id] = "streaming"
    await runtime._broadcast_clients_locked({"type": "answer_started", "response_id": response_id})


async def _emit_answer_delta(runtime: InterviewRuntime, response_id: str, delta: str) -> None:
    async with runtime._event_lock:
        async with runtime._answer_lock:
            if response_id in runtime.terminal_responses:
                return
            await _emit_answer_started_locked(runtime, response_id)
            runtime.response_buffers[response_id] = runtime.response_buffers.get(response_id, "") + delta
            await runtime._broadcast_clients_locked(
                {"type": "answer_delta", "response_id": response_id, "delta": delta}
            )


async def _set_answer_text(runtime: InterviewRuntime, response_id: str, text: str) -> None:
    async with runtime._event_lock:
        async with runtime._answer_lock:
            if response_id in runtime.terminal_responses:
                return
            await _emit_answer_started_locked(runtime, response_id)
            runtime.response_buffers[response_id] = text


async def _emit_response_terminal(runtime: InterviewRuntime, payload: dict[str, Any]) -> None:
    response = payload.get("response")
    if not isinstance(response, dict):
        return
    response_id = _event_response_id(payload) or runtime.active_response_id
    if not response_id:
        return
    extracted_text = _extract_response_text(response)
    text = extracted_text or None
    status = str(response.get("status") or "")
    detail = _response_status_detail(response)
    if status == "completed":
        event_type = "answer_completed"
    elif status in {"cancelled", "canceled"} or detail.startswith(("cancelled", "canceled")):
        event_type = "answer_interrupted"
    else:
        event_type = "answer_error"
    await _emit_terminal(
        runtime,
        response_id=response_id,
        event_type=event_type,
        text=text,
        detail=detail,
    )


async def _emit_terminal(
    runtime: InterviewRuntime,
    *,
    response_id: str,
    event_type: Literal["answer_completed", "answer_interrupted", "answer_error"],
    text: str | None,
    detail: str,
) -> None:
    async with runtime._event_lock:
        async with runtime._answer_lock:
            if response_id in runtime.terminal_responses:
                return
            await _emit_answer_started_locked(runtime, response_id)
            final_text = runtime.response_buffers.get(response_id, "") if text is None else text
            runtime.response_buffers[response_id] = final_text
            runtime.terminal_responses.add(response_id)
            status_by_event: dict[str, Literal["completed", "interrupted", "error"]] = {
                "answer_completed": "completed",
                "answer_interrupted": "interrupted",
                "answer_error": "error",
            }
            runtime.response_status[response_id] = status_by_event[event_type]
            if event_type != "answer_completed" or detail not in {"", "completed"}:
                runtime.response_details[response_id] = detail
            else:
                runtime.response_details.pop(response_id, None)
            payload: dict[str, Any] = {"type": event_type, "response_id": response_id, "text": final_text}
            if event_type != "answer_completed" or detail not in {"", "completed"}:
                payload["detail"] = detail
            await runtime._broadcast_clients_locked(payload)
            if runtime.active_response_id == response_id:
                runtime.active_response_id = ""


async def _handle_tool_call(
    runtime: InterviewRuntime,
    upstream: ClientConnection,
    payload: dict[str, Any],
) -> None:
    call_id = str(payload.get("call_id") or payload.get("item_id") or "")
    name = str(payload.get("name") or "")
    if not call_id:
        return
    try:
        arguments = json.loads(str(payload.get("arguments") or "{}"))
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object.")
    except (json.JSONDecodeError, ValueError) as exc:
        await _send_tool_failure(runtime, upstream, call_id, name, str(exc))
        return

    if name == "search_context":
        query = str(arguments.get("query") or "").strip()
        if not query:
            await _send_tool_failure(runtime, upstream, call_id, name, "query is required")
            return
        matches = [
            match.as_dict()
            for match in runtime.context_store.search(query)
        ]
        await runtime.update_retrieval(matches)
        await _send_tool_result(upstream, call_id, {"ok": True, "matches": matches})
        return

    if name == "capture_current_screen":
        if arguments:
            await _send_tool_failure(runtime, upstream, call_id, name, "This tool takes no arguments.")
            return
        await _capture_current_screen(runtime, upstream, call_id)
        return

    if name == "analyze_problem":
        question = str(arguments.get("question") or "").strip()
        if not question:
            await _send_tool_failure(runtime, upstream, call_id, name, "question is required")
            return
        try:
            analysis = await _analyze_problem(runtime, question)
        except Exception as exc:
            await _send_tool_failure(runtime, upstream, call_id, name, str(exc))
            return
        await _send_tool_result(upstream, call_id, {"ok": True, "analysis": analysis})
        return

    await _send_tool_failure(runtime, upstream, call_id, name, f"Unknown tool: {name}")


async def _send_tool_failure(
    runtime: InterviewRuntime,
    upstream: ClientConnection,
    call_id: str,
    name: str,
    detail: str,
) -> None:
    bounded = detail.strip()[:500] or "Tool call failed."
    await _send_tool_result(upstream, call_id, {"ok": False, "error": bounded})
    await runtime.broadcast_to_clients(
        {"type": "tool_error", "tool": name or "unknown", "detail": bounded}
    )


async def _capture_current_screen(
    runtime: InterviewRuntime,
    upstream: ClientConnection,
    call_id: str,
) -> None:
    try:
        request_id, image_url = await _request_current_screen(
            runtime,
            reason="Capture the current question, whiteboard, or code screen.",
        )
    except Exception as exc:
        await _send_tool_failure(
            runtime,
            upstream,
            call_id,
            "capture_current_screen",
            f"Screen capture failed: {exc}",
        )
        return

    summary = "Current interview question, whiteboard, or code screen."
    await runtime.update_screen(image_url, summary)
    await _send_image_item(upstream, image_url=image_url, prompt=summary, create_response=False)
    await _send_tool_result(upstream, call_id, {"ok": True, "request_id": request_id})


async def _capture_screen_for_ui(runtime: InterviewRuntime, websocket: WebSocket) -> None:
    try:
        _, image_url = await _request_current_screen(
            runtime,
            reason="Capture the current question, whiteboard, or code screen.",
        )
        summary = "Current interview question, whiteboard, or code screen."
        await runtime.update_screen(image_url, summary)
        upstream = await runtime.ensure_main()
        await _send_image_item(upstream, image_url=image_url, prompt=summary, create_response=True)
    except Exception as exc:
        detail = str(exc).strip()[:500] or "Screen capture could not be processed."
        await runtime.send_to_ui_client(websocket, {"type": "error", "detail": detail})


async def _request_current_screen(
    runtime: InterviewRuntime,
    *,
    reason: str,
) -> tuple[str, str]:
    request_id = f"{runtime.interview_id}:{uuid.uuid4()}"
    future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    runtime.pending_screen_requests[request_id] = future
    sent = await runtime.send_to_capture(
        "interviewer",
        {"type": "screen_capture_request", "request_id": request_id, "reason": reason},
    )
    if not sent:
        runtime.pending_screen_requests.pop(request_id, None)
        raise OpenAIRealtimeError("Interviewer capture device is not connected.")
    try:
        image_url = await asyncio.wait_for(future, timeout=12.0)
        return request_id, _validate_image_data_url(image_url)
    finally:
        runtime.pending_screen_requests.pop(request_id, None)


async def _analyze_problem(runtime: InterviewRuntime, question: str) -> str:
    settings = get_settings()
    if not settings.openai_api_key:
        raise OpenAIRealtimeError("OPENAI_API_KEY is not configured.")

    fresh_matches = [
        match.as_dict()
        for match in runtime.context_store.search(question)
    ]
    if fresh_matches:
        await runtime.update_retrieval(fresh_matches)
    dialogue, screen_image_url, screen_summary, latest_retrieval = await runtime.analysis_context()
    input_text = _build_analysis_input(
        question=question,
        dialogue=dialogue,
        retrieval=fresh_matches or latest_retrieval,
        screen_summary=screen_summary,
    )
    content: list[dict[str, Any]] = [{"type": "input_text", "text": input_text}]
    if screen_image_url:
        content.append({"type": "input_image", "image_url": screen_image_url, "detail": "auto"})

    request: dict[str, Any] = {
        "model": settings.openai_code_model,
        "instructions": "Analyze accurately and return a concise, natural answer the candidate can use directly. Follow the conversation's language.",
        "input": [{"role": "user", "content": content}],
        "store": False,
        "max_output_tokens": 2200,
    }
    if settings.openai_code_model.startswith("gpt-5"):
        request["reasoning"] = {"effort": settings.openai_code_reasoning_effort}

    timeout = httpx.Timeout(
        connect=10.0,
        read=settings.openai_code_timeout_seconds,
        write=20.0,
        pool=10.0,
    )
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{settings.openai_base_url}/responses",
            headers={
                "Authorization": f"Bearer {settings.openai_api_key}",
                "Content-Type": "application/json",
            },
            json=request,
        )
        response.raise_for_status()
        data = response.json()

    if data.get("status") == "incomplete":
        raise OpenAIRealtimeError(f"Analysis response incomplete: {data.get('incomplete_details')}")
    answer = _extract_openai_response_text(data)
    if not answer:
        raise OpenAIRealtimeError("Analysis response did not include output text.")
    return answer.strip()


def _build_analysis_input(
    *,
    question: str,
    dialogue: list[dict[str, str]],
    retrieval: list[dict[str, str | int]],
    screen_summary: str,
) -> str:
    recent = "\n".join(f"{turn['speaker']}: {turn['text']}" for turn in dialogue[-16:]) or "无"
    retrieved = "\n\n".join(
        f"[{match.get('source', '')}] {match.get('text', '')}" for match in retrieval[:6]
    ) or "无"
    return (
        f"当前问题：{question}\n\n"
        f"最近对话：\n{_clip(recent, 8000)}\n\n"
        f"检索资料：\n{_clip(retrieved, 7000)}\n\n"
        f"截图说明：{_clip(screen_summary, 1000) or '无'}"
    )


async def _connect_openai_realtime(*, kind: Literal["main", "candidate"]) -> ClientConnection:
    settings = get_settings()
    if not settings.openai_api_key:
        raise OpenAIRealtimeError("OPENAI_API_KEY is not configured.")
    query_params = {"model": settings.openai_realtime_model} if kind == "main" else {"intent": "transcription"}
    query = urlencode(query_params)
    base_url = settings.openai_base_url
    if base_url.startswith("https://"):
        ws_base = f"wss://{base_url[len('https://') :]}"
    elif base_url.startswith("http://"):
        ws_base = f"ws://{base_url[len('http://') :]}"
    else:
        ws_base = base_url
    return await websockets.connect(
        f"{ws_base}/realtime?{query}",
        additional_headers={"Authorization": f"Bearer {settings.openai_api_key}"},
        ping_interval=10,
        ping_timeout=20,
        max_size=None,
    )


async def _send_session_update(upstream: ClientConnection) -> None:
    settings = get_settings()
    transcription: dict[str, Any] = {"model": settings.openai_realtime_transcription_model}
    if settings.openai_realtime_transcription_language:
        transcription["language"] = settings.openai_realtime_transcription_language
    session: dict[str, Any] = {
        "type": "realtime",
        "model": settings.openai_realtime_model,
        "output_modalities": TEXT_OUTPUT_MODALITIES,
        "instructions": build_realtime_instructions(),
        "audio": {
            "input": {
                "format": AUDIO_INPUT_FORMAT,
                "transcription": transcription,
                "turn_detection": {
                    "type": "semantic_vad",
                    "eagerness": "medium",
                    "create_response": True,
                    "interrupt_response": True,
                },
            }
        },
        "reasoning": {"effort": settings.openai_realtime_reasoning_effort},
        "tools": [_search_context_tool_schema(), _capture_screen_tool_schema(), _analyze_problem_tool_schema()],
        "tool_choice": "auto",
    }
    await _send_json(upstream, {"type": "session.update", "session": session})


async def _send_transcription_session_update(upstream: ClientConnection) -> None:
    settings = get_settings()
    transcription: dict[str, Any] = {"model": settings.openai_realtime_transcription_model}
    if settings.openai_realtime_transcription_language:
        transcription["language"] = settings.openai_realtime_transcription_language
    await _send_json(
        upstream,
        {
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {"input": {"format": AUDIO_INPUT_FORMAT, "transcription": transcription}},
            },
        },
    )


def _search_context_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "name": "search_context",
        "description": "Search the candidate resume, job context, notes, and background files.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    }


def _capture_screen_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "name": "capture_current_screen",
        "description": "Capture the current question, whiteboard, IDE, or code on screen.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    }


def _analyze_problem_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "name": "analyze_problem",
        "description": "Deeply analyze a difficult code, SQL, debugging, or system-design problem.",
        "parameters": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
            "additionalProperties": False,
        },
    }


async def _send_audio_append(upstream: ClientConnection, audio_bytes: bytes) -> None:
    await _send_json(
        upstream,
        {"type": "input_audio_buffer.append", "audio": base64.b64encode(audio_bytes).decode("ascii")},
    )


async def _send_user_text(upstream: ClientConnection, text: str, *, create_response: bool) -> None:
    await _send_json(
        upstream,
        {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        },
    )
    if create_response:
        await _send_response_create(upstream)


async def _send_response_create(upstream: ClientConnection) -> None:
    await _send_json(
        upstream,
        {"type": "response.create", "response": {"output_modalities": TEXT_OUTPUT_MODALITIES}},
    )


async def _send_context_item(upstream: ClientConnection, text: str) -> None:
    await _send_user_text(upstream, text, create_response=False)


async def _send_tool_result(upstream: ClientConnection, call_id: str, result: dict[str, Any]) -> None:
    await _send_json(
        upstream,
        {
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(result, ensure_ascii=False),
            },
        },
    )
    await _send_response_create(upstream)


async def _send_image_item(
    upstream: ClientConnection,
    *,
    image_url: str,
    prompt: str,
    create_response: bool,
) -> None:
    await _send_json(
        upstream,
        {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": image_url, "detail": "auto"},
                ],
            },
        },
    )
    if create_response:
        await _send_response_create(upstream)


def _validate_image_data_url(image_url: str, *, max_bytes: int | None = None) -> str:
    if not image_url.startswith("data:") or ";base64," not in image_url:
        raise OpenAIRealtimeError("Screenshot must be a base64 data URL.")
    header, encoded = image_url.split(",", 1)
    mime_type = header[5:].split(";", 1)[0].lower()
    if mime_type not in ALLOWED_SCREENSHOT_MIME_TYPES:
        raise OpenAIRealtimeError("Screenshot MIME type must be PNG, JPEG, or WebP.")
    size_limit = max_bytes if max_bytes is not None else get_settings().interview_screenshot_max_bytes
    estimated_size = (len(encoded) * 3) // 4
    if estimated_size > size_limit:
        raise OpenAIRealtimeError("Screenshot exceeds the configured size limit.")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise OpenAIRealtimeError("Screenshot base64 is invalid.") from exc
    if not decoded or len(decoded) > size_limit:
        raise OpenAIRealtimeError("Screenshot is empty or too large.")
    valid_signature = (
        mime_type == "image/png" and decoded.startswith(b"\x89PNG\r\n\x1a\n")
        or mime_type == "image/jpeg" and decoded.startswith(b"\xff\xd8\xff")
        or mime_type == "image/webp" and len(decoded) >= 12 and decoded[:4] == b"RIFF" and decoded[8:12] == b"WEBP"
    )
    if not valid_signature:
        raise OpenAIRealtimeError("Screenshot bytes do not match the declared MIME type.")
    return image_url


async def _send_json(upstream: ClientConnection, payload: dict[str, Any]) -> None:
    await upstream.send(json.dumps(payload, ensure_ascii=False))


async def _safe_close(upstream: ClientConnection) -> None:
    try:
        await upstream.close()
    except Exception:
        pass


async def _send_websocket_json(websocket: WebSocket, payload: dict[str, Any]) -> None:
    await asyncio.wait_for(websocket.send_json(payload), timeout=CLIENT_SEND_TIMEOUT_SECONDS)


async def _close_websocket(websocket: WebSocket, *, code: int) -> None:
    try:
        await websocket.close(code=code)
    except Exception:
        pass


def _extract_error(payload: dict[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error or "OpenAI Realtime error")


def _response_status_detail(response: dict[str, Any]) -> str:
    status = str(response.get("status") or "")
    if status in {"", "completed"}:
        return "completed"
    status_details = response.get("status_details") or response.get("incomplete_details")
    if isinstance(status_details, dict):
        reason = status_details.get("reason") or status_details.get("error") or status_details.get("type")
        if reason:
            return f"{status}: {reason}"
    return status


def _event_response_id(payload: dict[str, Any]) -> str:
    response_id = payload.get("response_id")
    if isinstance(response_id, str):
        return response_id
    response = payload.get("response")
    if isinstance(response, dict) and isinstance(response.get("id"), str):
        return str(response["id"])
    return ""


def _resolved_response_id(runtime: InterviewRuntime, payload: dict[str, Any]) -> str:
    return _event_response_id(payload) or runtime.active_response_id


def _extract_response_text(response: dict[str, Any]) -> str:
    output = response.get("output")
    if not isinstance(output, list):
        return ""
    parts = [_extract_response_item_text(item) for item in output]
    return "\n\n".join(part for part in parts if part).strip()


def _extract_openai_response_text(payload: dict[str, Any]) -> str:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    return _extract_response_text(payload)


def _extract_response_item_text(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    parts = [_extract_content_part_text(part) for part in content]
    return "\n".join(part for part in parts if part).strip()


def _extract_content_part_text(part: Any) -> str:
    if not isinstance(part, dict):
        return ""
    for key in ("text", "transcript"):
        value = part.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _clip(text: str, limit: int) -> str:
    normalized = text.strip()
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit]}…"
