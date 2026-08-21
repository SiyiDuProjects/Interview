from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services.context_store import ContextStore
from app.services.openai_realtime import (
    InterviewRuntime,
    OpenAIRealtimeError,
    _CandidateTranscriptAccumulator,
    _analyze_problem,
    _capture_screen_for_ui,
    _forward_capture_controls,
    _forward_main_events,
    _forward_ui_controls,
    _handle_tool_call,
    _resolve_screen_snapshot,
    _send_response_create,
    _send_session_update,
    _send_transcription_session_update,
    _validate_image_data_url,
)


PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nminimal").decode("ascii")


class FakeUpstream:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False

    async def send(self, payload: str) -> None:
        self.messages.append(json.loads(payload))

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        item = await self.queue.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def close(self) -> None:
        self.closed = True
        self.queue.put_nowait(None)


class FakeEventStream:
    def __init__(self, events: list[dict]) -> None:
        self.events = [json.dumps(event) for event in events]

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if not self.events:
            raise StopAsyncIteration
        return self.events.pop(0)


class FakeSocket:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.messages.append(payload)


class FakeClientWebSocket(FakeSocket):
    def __init__(self, authentication: dict, controls: list[dict] | None = None) -> None:
        super().__init__()
        self.authentication = authentication
        self.controls = list(controls or [{"type": "websocket.disconnect"}])
        self.accepted = False
        self.closed_codes: list[int] = []
        self.receive_count = 0

    async def accept(self) -> None:
        self.accepted = True

    async def receive_json(self) -> dict:
        return self.authentication

    async def receive(self) -> dict:
        self.receive_count += 1
        return self.controls.pop(0)

    async def close(self, *, code: int) -> None:
        self.closed_codes.append(code)


class FailingSocket(FakeSocket):
    async def send_json(self, payload: dict) -> None:
        raise RuntimeError("client disconnected")


class StreamingClientWebSocket(FakeSocket):
    def __init__(self, authentication: dict) -> None:
        super().__init__()
        self.authentication = authentication
        self.controls: asyncio.Queue[dict] = asyncio.Queue()
        self.snapshot_sent = asyncio.Event()
        self.accepted = False
        self.closed_codes: list[int] = []

    async def accept(self) -> None:
        self.accepted = True

    async def receive_json(self) -> dict:
        return self.authentication

    async def receive(self) -> dict:
        return await self.controls.get()

    async def send_json(self, payload: dict) -> None:
        await super().send_json(payload)
        if payload.get("type") == "answer_snapshot":
            self.snapshot_sent.set()

    async def close(self, *, code: int) -> None:
        self.closed_codes.append(code)


class FakeHTTPResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"status": "completed", "output_text": "Use a bounded queue and backpressure."}


class FakeHTTPClient:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def post(self, url: str, **kwargs) -> FakeHTTPResponse:
        self.requests.append({"url": url, **kwargs})
        return FakeHTTPResponse()


def make_runtime(name: str = "one", *, context_store: ContextStore | None = None) -> InterviewRuntime:
    return InterviewRuntime(
        interview_id=name,
        session_token=f"token-{name}",
        capture_token=f"capture-{name}",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        context_store=context_store,
    )


def attach_ui(runtime: InterviewRuntime, socket: FakeSocket, name: str = "ui") -> None:
    runtime._ui_clients[name] = socket  # type: ignore[assignment]
    runtime._ready_ui_clients.add(name)


def attach_capture(
    runtime: InterviewRuntime,
    socket: FakeSocket,
    speaker: str = "interviewer",
) -> None:
    runtime._capture_clients[speaker] = socket  # type: ignore[index,assignment]


class RealtimeProtocolTests(unittest.TestCase):
    def test_main_session_uses_current_nested_schema_and_exact_tools(self) -> None:
        async def run() -> list[dict]:
            upstream = FakeUpstream()
            await _send_session_update(upstream)  # type: ignore[arg-type]
            await _send_response_create(upstream)  # type: ignore[arg-type]
            return upstream.messages

        with patch.dict(
            os.environ,
            {
                "OPENAI_REALTIME_MODEL": "gpt-realtime-2.1",
                "OPENAI_REALTIME_TRANSCRIPTION_MODEL": "gpt-realtime-whisper",
                "OPENAI_REALTIME_TRANSCRIPTION_LANGUAGE": "",
            },
        ):
            messages = asyncio.run(run())

        session = messages[0]["session"]
        self.assertEqual(session["type"], "realtime")
        self.assertEqual(session["model"], "gpt-realtime-2.1")
        self.assertEqual(session["output_modalities"], ["text"])
        self.assertNotIn("modalities", session)
        self.assertNotIn("input_audio_format", session)
        audio_input = session["audio"]["input"]
        self.assertEqual(audio_input["format"], {"type": "audio/pcm", "rate": 24000})
        self.assertEqual(audio_input["transcription"], {"model": "gpt-realtime-whisper"})
        self.assertTrue(audio_input["turn_detection"]["create_response"])
        self.assertTrue(audio_input["turn_detection"]["interrupt_response"])
        tools = session["tools"]
        self.assertEqual(
            [tool["name"] for tool in tools],
            ["search_context", "capture_current_screen", "analyze_problem"],
        )
        self.assertTrue(all(tool["parameters"]["additionalProperties"] is False for tool in tools))
        self.assertEqual(tools[1]["parameters"]["properties"], {})
        self.assertEqual(messages[1], {"type": "response.create", "response": {"output_modalities": ["text"]}})

    def test_candidate_transcription_has_no_turn_detection(self) -> None:
        upstream = FakeUpstream()
        with patch.dict(os.environ, {"OPENAI_REALTIME_TRANSCRIPTION_LANGUAGE": ""}):
            asyncio.run(_send_transcription_session_update(upstream))  # type: ignore[arg-type]
        session = upstream.messages[0]["session"]
        self.assertEqual(session["type"], "transcription")
        self.assertEqual(session["audio"]["input"]["format"], {"type": "audio/pcm", "rate": 24000})
        self.assertNotIn("turn_detection", session["audio"]["input"])
        self.assertNotIn("language", session["audio"]["input"]["transcription"])

    def test_runtime_opens_exactly_two_upstreams_and_updates_each_once(self) -> None:
        async def run() -> tuple[list[dict], list[dict], list[str]]:
            runtime = make_runtime()
            main = FakeUpstream()
            candidate = FakeUpstream()
            connect = AsyncMock(side_effect=[candidate, main])
            with patch("app.services.openai_realtime._connect_openai_realtime", new=connect):
                self.assertIs(await runtime.ensure_candidate(), await runtime.ensure_candidate())
                self.assertIsNone(runtime.main_upstream)
                self.assertIs(await runtime.ensure_main(), await runtime.ensure_main())
                kinds = [call.kwargs["kind"] for call in connect.await_args_list]
            await runtime.close()
            return main.messages, candidate.messages, kinds

        main_messages, candidate_messages, kinds = asyncio.run(run())
        self.assertEqual(kinds, ["candidate", "main"])
        self.assertEqual([item["type"] for item in main_messages], ["session.update"])
        self.assertEqual([item["type"] for item in candidate_messages], ["session.update"])

    def test_runtime_state_is_isolated(self) -> None:
        async def run() -> tuple[list[dict], list[dict]]:
            first = make_runtime("first")
            second = make_runtime("second")
            first.main_upstream = FakeUpstream()  # type: ignore[assignment]
            second.main_upstream = FakeUpstream()  # type: ignore[assignment]
            await first.append_candidate_context("first-only")
            await second.append_candidate_context("second-only")
            return first.main_upstream.messages, second.main_upstream.messages  # type: ignore[union-attr]

        first_messages, second_messages = asyncio.run(run())
        self.assertIn("first-only", first_messages[0]["item"]["content"][0]["text"])
        self.assertNotIn("second-only", first_messages[0]["item"]["content"][0]["text"])
        self.assertIn("second-only", second_messages[0]["item"]["content"][0]["text"])

    def test_idle_capture_audio_does_not_open_upstream(self) -> None:
        async def run() -> tuple[AsyncMock, AsyncMock]:
            runtime = make_runtime("idle-audio")
            interviewer = FakeClientWebSocket(
                {},
                controls=[
                    {"type": "websocket.receive", "bytes": b"audio"},
                    {"type": "websocket.disconnect"},
                ],
            )
            candidate = FakeClientWebSocket(
                {},
                controls=[
                    {"type": "websocket.receive", "bytes": b"audio"},
                    {"type": "websocket.disconnect"},
                ],
            )
            ensure_main = AsyncMock(return_value=FakeUpstream())
            ensure_candidate = AsyncMock(return_value=FakeUpstream())
            with (
                patch.object(runtime, "ensure_main", new=ensure_main),
                patch.object(runtime, "ensure_candidate", new=ensure_candidate),
            ):
                await _forward_capture_controls(
                    runtime, interviewer, "interviewer"  # type: ignore[arg-type]
                )
                await _forward_capture_controls(
                    runtime, candidate, "candidate"  # type: ignore[arg-type]
                )
            return ensure_main, ensure_candidate

        ensure_main, ensure_candidate = asyncio.run(run())
        ensure_main.assert_not_awaited()
        ensure_candidate.assert_not_awaited()

    def test_start_requires_both_capture_channels_ready_and_opens_no_upstream(self) -> None:
        async def run() -> tuple[InterviewRuntime, FakeSocket, FakeSocket, FakeSocket, AsyncMock, AsyncMock]:
            runtime = make_runtime("start-gate")
            ui = FakeSocket()
            interviewer = FakeSocket()
            candidate = FakeSocket()
            attach_ui(runtime, ui)
            ensure_main = AsyncMock(return_value=FakeUpstream())
            ensure_candidate = AsyncMock(return_value=FakeUpstream())
            with (
                patch.object(runtime, "ensure_main", new=ensure_main),
                patch.object(runtime, "ensure_candidate", new=ensure_candidate),
            ):
                await runtime.start_interview(ui)  # type: ignore[arg-type]
                attach_capture(runtime, interviewer, "interviewer")
                attach_capture(runtime, candidate, "candidate")
                await runtime.mark_capture_ready("interviewer", interviewer)  # type: ignore[arg-type]
                await runtime.start_interview(ui)  # type: ignore[arg-type]
                await runtime.mark_capture_ready("candidate", candidate)  # type: ignore[arg-type]
                await runtime.start_interview(ui)  # type: ignore[arg-type]
            return runtime, ui, interviewer, candidate, ensure_main, ensure_candidate

        runtime, ui, interviewer, candidate, ensure_main, ensure_candidate = asyncio.run(run())
        self.assertTrue(runtime.active)
        self.assertEqual(interviewer.messages[-1], {"type": "capture_start"})
        self.assertEqual(candidate.messages[-1], {"type": "capture_start"})
        self.assertEqual(
            len([message for message in ui.messages if message.get("detail") == "Capture device is not ready."]),
            2,
        )
        self.assertTrue(any(message == {"type": "interview_state", "active": True} for message in ui.messages))
        ensure_main.assert_not_awaited()
        ensure_candidate.assert_not_awaited()

    def test_two_ui_clients_receive_same_answer_and_one_failure_does_not_stop_other(self) -> None:
        async def run() -> tuple[list[dict], list[dict], InterviewRuntime]:
            runtime = make_runtime("fanout")
            first = FakeSocket()
            second = FakeSocket()
            attach_ui(runtime, FailingSocket(), "failed")
            attach_ui(runtime, first, "first")
            attach_ui(runtime, second, "second")
            await _forward_main_events(
                runtime,
                FakeEventStream(
                    [
                        {"type": "response.created", "response": {"id": "resp_1"}},
                        {"type": "response.output_text.delta", "response_id": "resp_1", "delta": "One"},
                        {
                            "type": "response.done",
                            "response": {
                                "id": "resp_1",
                                "status": "completed",
                                "output": [{"content": [{"type": "output_text", "text": "One"}]}],
                            },
                        },
                    ]
                ),  # type: ignore[arg-type]
            )
            runtime._ui_clients.pop("first")
            runtime._ready_ui_clients.discard("first")
            await _forward_main_events(
                runtime,
                FakeEventStream(
                    [
                        {"type": "response.created", "response": {"id": "resp_2"}},
                        {
                            "type": "response.done",
                            "response": {"id": "resp_2", "status": "completed", "output": []},
                        },
                    ]
                ),  # type: ignore[arg-type]
            )
            return first.messages, second.messages, runtime

        first_messages, second_messages, runtime = asyncio.run(run())
        self.assertEqual(
            [message["type"] for message in first_messages],
            ["answer_started", "answer_delta", "answer_completed"],
        )
        self.assertEqual(second_messages[:3], first_messages)
        self.assertEqual(
            [message["response_id"] for message in second_messages[-2:]],
            ["resp_2", "resp_2"],
        )
        self.assertNotIn("failed", runtime._ui_clients)
        self.assertIsNone(runtime.main_upstream)

    def test_official_response_sequence_emits_id_bearing_answer_events(self) -> None:
        async def run() -> list[dict]:
            runtime = make_runtime()
            socket = FakeSocket()
            attach_ui(runtime, socket)
            events = FakeEventStream(
                [
                    {"type": "response.created", "response": {"id": "resp_1"}},
                    {"type": "response.output_item.added", "response_id": "resp_1"},
                    {"type": "response.content_part.added", "response_id": "resp_1"},
                    {"type": "response.output_text.delta", "response_id": "resp_1", "delta": "Use "},
                    {"type": "response.output_text.done", "response_id": "resp_1", "text": "Use indexes."},
                    {"type": "response.content_part.done", "response_id": "resp_1", "part": {"text": "Use indexes."}},
                    {
                        "type": "response.output_item.done",
                        "response_id": "resp_1",
                        "item": {"content": [{"type": "output_text", "text": "Use indexes."}]},
                    },
                    {
                        "type": "response.done",
                        "response": {
                            "id": "resp_1",
                            "status": "completed",
                            "output": [{"content": [{"type": "output_text", "text": "Use indexes."}]}],
                        },
                    },
                ]
            )
            await _forward_main_events(runtime, events)  # type: ignore[arg-type]
            return socket.messages

        messages = asyncio.run(run())
        self.assertEqual(
            messages,
            [
                {"type": "answer_started", "response_id": "resp_1"},
                {"type": "answer_delta", "response_id": "resp_1", "delta": "Use "},
                {"type": "answer_completed", "response_id": "resp_1", "text": "Use indexes."},
            ],
        )

    def test_cancelled_response_preserves_partial_text(self) -> None:
        async def run() -> list[dict]:
            runtime = make_runtime()
            socket = FakeSocket()
            attach_ui(runtime, socket)
            await _forward_main_events(
                runtime,
                FakeEventStream(
                    [
                        {"type": "response.created", "response": {"id": "resp_cancel"}},
                        {"type": "response.output_text.delta", "response_id": "resp_cancel", "delta": "Partial"},
                        {
                            "type": "response.done",
                            "response": {
                                "id": "resp_cancel",
                                "status": "cancelled",
                                "status_details": {"reason": "turn_detected"},
                            },
                        },
                    ]
                ),  # type: ignore[arg-type]
            )
            return socket.messages

        messages = asyncio.run(run())
        self.assertEqual(messages[-1]["type"], "answer_interrupted")
        self.assertEqual(messages[-1]["response_id"], "resp_cancel")
        self.assertEqual(messages[-1]["text"], "Partial")

    def test_candidate_delta_debounce_finalizes_and_deduplicates_completed(self) -> None:
        async def run() -> tuple[list[dict], list[str]]:
            runtime = make_runtime()
            socket = FakeSocket()
            attach_ui(runtime, socket)
            accumulator = _CandidateTranscriptAccumulator(runtime, quiet_seconds=0.01)
            await accumulator.add("What")
            await accumulator.add(" is")
            await asyncio.sleep(0.03)
            await accumulator.complete("What is")
            await accumulator.close()
            return socket.messages, list(runtime.pending_candidate_context)

        messages, pending = asyncio.run(run())
        self.assertEqual(messages[0]["delta"], "What")
        self.assertEqual(messages[1]["delta"], " is")
        finals = [message for message in messages if message["type"] == "transcript_final"]
        self.assertEqual(finals, [{"type": "transcript_final", "speaker": "candidate", "text": "What is"}])
        self.assertEqual(len(pending), 1)
        self.assertIn("What is", pending[0])

    def test_invalid_first_frame_never_registers_client_or_connects_upstream(self) -> None:
        async def run() -> tuple[FakeClientWebSocket, AsyncMock, InterviewRuntime]:
            runtime = make_runtime("auth")
            websocket = FakeClientWebSocket({"type": "authenticate", "token": "wrong"})
            ensure_main = AsyncMock(return_value=object())
            with patch.object(runtime, "ensure_main", new=ensure_main):
                await runtime.serve(websocket, "client")
            return websocket, ensure_main, runtime

        websocket, ensure_main, runtime = asyncio.run(run())
        self.assertTrue(websocket.accepted)
        self.assertEqual(websocket.closed_codes, [1008])
        self.assertEqual(websocket.messages, [])
        ensure_main.assert_not_awaited()
        self.assertEqual(runtime._ui_clients, {})

    def test_reconnect_replays_partial_and_terminal_answer_snapshots(self) -> None:
        async def run() -> tuple[list[dict], list[dict], InterviewRuntime]:
            runtime = make_runtime("reconnect")
            await _forward_main_events(
                runtime,
                FakeEventStream(
                    [
                        {"type": "response.created", "response": {"id": "resp_partial"}},
                        {
                            "type": "response.output_text.delta",
                            "response_id": "resp_partial",
                            "delta": "Still streaming",
                        },
                        {"type": "response.created", "response": {"id": "resp_done"}},
                        {"type": "response.output_text.delta", "response_id": "resp_done", "delta": "Final"},
                        {
                            "type": "response.done",
                            "response": {
                                "id": "resp_done",
                                "status": "completed",
                                "output": [{"content": [{"type": "output_text", "text": "Final answer"}]}],
                            },
                        },
                        {"type": "response.created", "response": {"id": "resp_interrupted"}},
                        {
                            "type": "response.output_text.delta",
                            "response_id": "resp_interrupted",
                            "delta": "Interrupted partial",
                        },
                        {
                            "type": "response.done",
                            "response": {
                                "id": "resp_interrupted",
                                "status": "cancelled",
                                "status_details": {"reason": "turn_detected"},
                            },
                        },
                    ]
                ),  # type: ignore[arg-type]
            )
            first = FakeClientWebSocket(
                {"type": "authenticate", "token": runtime.session_token}
            )
            second = FakeClientWebSocket(
                {"type": "authenticate", "token": runtime.session_token}
            )
            ensure_main = AsyncMock(return_value=object())
            with patch.object(runtime, "ensure_main", new=ensure_main):
                await runtime.serve(first, "client")
                await runtime.serve(second, "client")
            return first.messages, second.messages, runtime

        first_messages, second_messages, runtime = asyncio.run(run())
        first_snapshots = [item for item in first_messages if item["type"] == "answer_snapshot"]
        second_snapshots = [item for item in second_messages if item["type"] == "answer_snapshot"]
        expected = [
            {
                "type": "answer_snapshot",
                "response_id": "resp_partial",
                "text": "Still streaming",
                "status": "streaming",
            },
            {
                "type": "answer_snapshot",
                "response_id": "resp_done",
                "text": "Final answer",
                "status": "completed",
            },
            {
                "type": "answer_snapshot",
                "response_id": "resp_interrupted",
                "text": "Interrupted partial",
                "status": "interrupted",
                "detail": "cancelled: turn_detected",
            },
        ]
        self.assertEqual(first_snapshots, expected)
        self.assertEqual(second_snapshots, expected)
        self.assertEqual(
            runtime.response_order,
            ["resp_partial", "resp_done", "resp_interrupted"],
        )

    def test_join_snapshot_is_followed_by_live_events_without_gap(self) -> None:
        async def run() -> list[dict]:
            runtime = make_runtime("join-live")
            await runtime.emit_transcript_final("interviewer", "Existing question")
            await _forward_main_events(
                runtime,
                FakeEventStream(
                    [
                        {"type": "response.created", "response": {"id": "old"}},
                        {
                            "type": "response.done",
                            "response": {
                                "id": "old",
                                "status": "completed",
                                "output": [{"content": [{"type": "output_text", "text": "Old answer"}]}],
                            },
                        },
                    ]
                ),  # type: ignore[arg-type]
            )
            websocket = StreamingClientWebSocket(
                {"type": "authenticate", "token": runtime.session_token}
            )
            serve_task = asyncio.create_task(runtime.serve(websocket, "client"))  # type: ignore[arg-type]
            await asyncio.wait_for(websocket.snapshot_sent.wait(), timeout=1.0)
            await _forward_main_events(
                runtime,
                FakeEventStream(
                    [
                        {"type": "response.created", "response": {"id": "live"}},
                        {"type": "response.output_text.delta", "response_id": "live", "delta": "Live"},
                    ]
                ),  # type: ignore[arg-type]
            )
            await websocket.controls.put({"type": "websocket.disconnect"})
            await serve_task
            return websocket.messages

        messages = asyncio.run(run())
        self.assertEqual(
            [message["type"] for message in messages[:5]],
            [
                "session_ready",
                "device_status",
                "interview_state",
                "transcript_snapshot",
                "answer_snapshot",
            ],
        )
        self.assertEqual(messages[3]["turns"], [{"speaker": "interviewer", "text": "Existing question"}])
        self.assertEqual([message["type"] for message in messages[5:]], ["answer_started", "answer_delta"])
        self.assertEqual(messages[5]["response_id"], "live")

    def test_terminal_state_survives_client_send_failure(self) -> None:
        async def run() -> InterviewRuntime:
            runtime = make_runtime("send-failure")
            attach_ui(runtime, FailingSocket())
            await _forward_main_events(
                runtime,
                FakeEventStream(
                    [
                        {"type": "response.created", "response": {"id": "resp_failed_send"}},
                        {
                            "type": "response.done",
                            "response": {
                                "id": "resp_failed_send",
                                "status": "completed",
                                "output": [{"content": [{"type": "output_text", "text": "Preserved"}]}],
                            },
                        },
                    ]
                ),  # type: ignore[arg-type]
            )
            return runtime

        runtime = asyncio.run(run())
        self.assertEqual(runtime.response_buffers["resp_failed_send"], "Preserved")
        self.assertEqual(runtime.response_status["resp_failed_send"], "completed")
        self.assertIn("resp_failed_send", runtime.terminal_responses)

    def test_idle_ui_rejects_audio_manual_text_and_screen_without_upstream(self) -> None:
        async def run() -> tuple[list[dict], AsyncMock]:
            runtime = make_runtime("idle-ui")
            websocket = FakeClientWebSocket(
                {},
                controls=[
                    {"type": "websocket.receive", "bytes": b"audio"},
                    {
                        "type": "websocket.receive",
                        "text": json.dumps({"type": "manual_text", "text": "question"}),
                    },
                    {
                        "type": "websocket.receive",
                        "text": json.dumps({"type": "request_screen_capture"}),
                    },
                    {"type": "websocket.disconnect"},
                ],
            )
            attach_ui(runtime, websocket)
            ensure_main = AsyncMock(return_value=FakeUpstream())
            with patch.object(runtime, "ensure_main", new=ensure_main):
                await _forward_ui_controls(runtime, websocket)  # type: ignore[arg-type]
            return websocket.messages, ensure_main

        messages, ensure_main = asyncio.run(run())
        self.assertEqual([message["type"] for message in messages], ["error", "error", "error"])
        self.assertEqual(messages[0]["detail"], "UI clients cannot send audio.")
        self.assertTrue(all(len(message["detail"]) <= 500 for message in messages))
        ensure_main.assert_not_awaited()

    def test_close_notifies_ui_and_capture_before_socket_shutdown(self) -> None:
        async def run() -> tuple[list[dict], list[dict]]:
            runtime = make_runtime("ended")
            ui = FakeSocket()
            capture = FakeSocket()
            attach_ui(runtime, ui)
            attach_capture(runtime, capture)
            await runtime.close()
            return ui.messages, capture.messages

        ui_messages, capture_messages = asyncio.run(run())
        self.assertEqual(ui_messages[-1], {"type": "session_ended"})
        self.assertEqual(capture_messages[-1], {"type": "session_ended"})

    def test_invalid_manual_screen_capture_reports_bounded_error_and_keeps_control_loop_alive(self) -> None:
        async def run() -> FakeClientWebSocket:
            runtime = make_runtime("bad-screen")
            websocket = FakeClientWebSocket(
                {},
                controls=[
                    {
                        "type": "websocket.receive",
                        "text": json.dumps(
                            {
                                "type": "screen_capture",
                                "image_data": "data:image/png;base64,bm90LXBuZw==",
                                "request_answer": True,
                            }
                        ),
                    },
                    {"type": "websocket.receive", "text": json.dumps({"type": "close"})},
                ],
            )
            await _forward_capture_controls(
                runtime,
                websocket,  # type: ignore[arg-type]
                "interviewer",
            )
            return websocket

        websocket = asyncio.run(run())
        self.assertEqual(websocket.receive_count, 2)
        self.assertEqual(websocket.messages[0]["type"], "error")
        self.assertLessEqual(len(websocket.messages[0]["detail"]), 500)


class RealtimeToolTests(unittest.TestCase):
    def test_tool_failure_is_returned_to_model_and_broadcast_to_all_ui_clients(self) -> None:
        async def run() -> tuple[list[dict], list[dict], list[dict]]:
            runtime = make_runtime("tool-error")
            first = FakeSocket()
            second = FakeSocket()
            attach_ui(runtime, first, "first")
            attach_ui(runtime, second, "second")
            upstream = FakeUpstream()
            await _handle_tool_call(
                runtime,
                upstream,  # type: ignore[arg-type]
                {"call_id": "bad", "name": "search_context", "arguments": "{}"},
            )
            return first.messages, second.messages, upstream.messages

        first, second, upstream = asyncio.run(run())
        self.assertEqual(first, second)
        self.assertEqual(first[0]["type"], "tool_error")
        self.assertFalse(json.loads(upstream[0]["item"]["output"])["ok"])

    def test_search_context_returns_scoped_snapshot(self) -> None:
        async def run(root: Path) -> tuple[dict, list[dict]]:
            runtime = make_runtime(context_store=ContextStore(root))
            upstream = FakeUpstream()
            await _handle_tool_call(
                runtime,
                upstream,  # type: ignore[arg-type]
                {
                    "call_id": "call_search",
                    "name": "search_context",
                    "arguments": json.dumps({"query": "Redis persistence"}),
                },
            )
            return json.loads(upstream.messages[0]["item"]["output"]), upstream.messages

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "profile.md").write_text("Built Redis AOF persistence tooling.", encoding="utf-8")
            result, messages = asyncio.run(run(root))
        self.assertTrue(result["ok"])
        self.assertEqual(result["matches"][0]["source"], "profile.md")
        self.assertEqual(messages[1], {"type": "response.create", "response": {"output_modalities": ["text"]}})

    def test_screen_tool_uses_runtime_scoped_request_and_validates_image(self) -> None:
        async def run() -> tuple[list[dict], list[dict]]:
            runtime = make_runtime("screen")
            socket = FakeSocket()
            attach_capture(runtime, socket)
            upstream = FakeUpstream()
            task = asyncio.create_task(
                _handle_tool_call(
                    runtime,
                    upstream,  # type: ignore[arg-type]
                    {"call_id": "call_screen", "name": "capture_current_screen", "arguments": "{}"},
                )
            )
            await asyncio.sleep(0)
            request_id = socket.messages[0]["request_id"]
            self.assertTrue(request_id.startswith("screen:"))
            await _resolve_screen_snapshot(
                runtime,
                {"request_id": request_id, "image_data": PNG_DATA_URL},
            )
            await task
            return socket.messages, upstream.messages

        socket_messages, upstream_messages = asyncio.run(run())
        self.assertEqual(socket_messages[0]["type"], "screen_capture_request")
        self.assertEqual(upstream_messages[0]["item"]["content"][1]["type"], "input_image")
        result = json.loads(upstream_messages[1]["item"]["output"])
        self.assertTrue(result["ok"])
        self.assertEqual(upstream_messages[2]["type"], "response.create")

    def test_ui_screen_request_routes_only_to_interviewer_capture_and_creates_answer(self) -> None:
        async def run() -> tuple[list[dict], list[dict]]:
            runtime = make_runtime("ui-screen")
            runtime.active = True
            capture = FakeSocket()
            ui = FakeSocket()
            attach_capture(runtime, capture)
            attach_ui(runtime, ui)
            upstream = FakeUpstream()
            runtime.main_upstream = upstream  # type: ignore[assignment]
            task = asyncio.create_task(
                _capture_screen_for_ui(runtime, ui)  # type: ignore[arg-type]
            )
            await asyncio.sleep(0)
            request = capture.messages[0]
            self.assertEqual(request["type"], "screen_capture_request")
            await _resolve_screen_snapshot(
                runtime,
                {"request_id": request["request_id"], "image_data": PNG_DATA_URL},
            )
            await task
            return capture.messages, upstream.messages

        capture_messages, upstream_messages = asyncio.run(run())
        self.assertEqual(len(capture_messages), 1)
        self.assertEqual(upstream_messages[0]["item"]["content"][1]["type"], "input_image")
        self.assertEqual(upstream_messages[1], {"type": "response.create", "response": {"output_modalities": ["text"]}})

    def test_analyze_problem_is_stateless_and_auto_includes_runtime_context(self) -> None:
        async def run(root: Path, client: FakeHTTPClient) -> str:
            runtime = make_runtime(context_store=ContextStore(root))
            await runtime.remember_dialogue("interviewer", "Design a job queue")
            await runtime.update_screen(PNG_DATA_URL, "queue diagram")
            with patch("app.services.openai_realtime.httpx.AsyncClient", return_value=client):
                return await _analyze_problem(runtime, "How should backpressure work?")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "background.txt").write_text("The system uses a bounded job queue with workers.", encoding="utf-8")
            client = FakeHTTPClient()
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
                answer = asyncio.run(run(root, client))

        self.assertIn("bounded queue", answer)
        request = client.requests[0]["json"]
        self.assertEqual(request["model"], "gpt-5.6-sol")
        self.assertIs(request["store"], False)
        content = request["input"][0]["content"]
        self.assertEqual(content[1]["type"], "input_image")
        self.assertIn("Design a job queue", content[0]["text"])
        self.assertIn("bounded job queue", content[0]["text"])

    def test_screenshot_rejects_mime_signature_mismatch_and_size(self) -> None:
        invalid = "data:image/png;base64," + base64.b64encode(b"not-png").decode("ascii")
        with self.assertRaises(OpenAIRealtimeError):
            _validate_image_data_url(invalid)
        with self.assertRaises(OpenAIRealtimeError):
            _validate_image_data_url(PNG_DATA_URL, max_bytes=4)


if __name__ == "__main__":
    unittest.main()
