from __future__ import annotations

import asyncio
import base64
import json
import uuid
from typing import Any
from urllib.parse import urlencode

import httpx
import websockets
from fastapi import WebSocket
from pydantic import ValidationError
from websockets.asyncio.client import ClientConnection

from app.config import get_settings
from app.models import AnswerScope, CandidateContext
from app.services.realtime_context import (
    RealtimeContextConfig,
    build_realtime_instructions,
    lookup_candidate_context,
)


class OpenAIRealtimeError(RuntimeError):
    pass


AUDIO_INPUT_FORMAT: dict[str, Any] = {"type": "audio/pcm", "rate": 24000}
TEXT_OUTPUT_MODALITIES = ["text"]


class RealtimeInterviewHub:
    def __init__(self) -> None:
        self.main_upstream: ClientConnection | None = None
        self.main_context = CandidateContext()
        self.main_scope: AnswerScope = "general"
        self.main_label = ""
        self.pending_context: list[str] = []
        self.latest_screen_image_url = ""
        self.latest_screen_summary = ""
        self.latest_code_context = ""
        self.lock = asyncio.Lock()

    async def reset_main(self) -> None:
        async with self.lock:
            upstream = self.main_upstream
            self.main_upstream = None
            self.pending_context = []
        if upstream is not None:
            await _safe_close(upstream)

    async def ensure_main(self, config: RealtimeContextConfig) -> ClientConnection:
        async with self.lock:
            self.main_context = config.context
            self.main_scope = config.answer_scope
            self.main_label = config.project_context_label
            if self.main_upstream is None:
                self.main_upstream = await _connect_openai_realtime()
                await _send_session_update(
                    self.main_upstream,
                    config=config,
                    create_response=True,
                    transcription=False,
                )
                for context_text in self.pending_context:
                    await _send_context_item(self.main_upstream, context_text)
                self.pending_context = []
            else:
                await _send_session_update(
                    self.main_upstream,
                    config=config,
                    create_response=True,
                    transcription=False,
                )
            return self.main_upstream

    async def update_config(self, config: RealtimeContextConfig) -> None:
        upstream = await self.ensure_main(config)
        await _send_session_update(upstream, config=config, create_response=True, transcription=False)

    async def append_context(self, text: str) -> None:
        normalized = text.strip()
        if not normalized:
            return
        async with self.lock:
            upstream = self.main_upstream
            if upstream is None:
                self.pending_context.append(normalized)
                return
            await _send_context_item(upstream, normalized)

    async def update_screen_context(self, image_url: str, summary: str) -> None:
        async with self.lock:
            self.latest_screen_image_url = image_url
            self.latest_screen_summary = summary

    async def update_code_context(self, text: str) -> None:
        normalized = text.strip()
        if not normalized:
            return
        async with self.lock:
            self.latest_code_context = normalized[-8000:]

    async def current_visual_code_context(self) -> tuple[str, str, str]:
        async with self.lock:
            return self.latest_screen_image_url, self.latest_screen_summary, self.latest_code_context

    def current_context(self) -> tuple[CandidateContext, AnswerScope]:
        return self.main_context, self.main_scope


_hub = RealtimeInterviewHub()


async def proxy_realtime_interview(websocket: WebSocket, speaker: str) -> None:
    await websocket.accept()
    if speaker not in {"interviewer", "candidate"}:
        await websocket.close(code=1008)
        return
    if not get_settings().openai_api_key:
        await websocket.send_json({"type": "error", "detail": "未配置 OPENAI_API_KEY，无法启动 OpenAI Realtime。"})
        await websocket.close(code=1011)
        return

    try:
        start_payload = await _receive_start(websocket)
        config = _parse_config(start_payload)
        if speaker == "interviewer":
            await _proxy_interviewer(websocket, config)
        else:
            await _proxy_candidate(websocket, config)
    except Exception as exc:
        try:
            await websocket.send_json({"type": "error", "detail": str(exc)})
        finally:
            await websocket.close(code=1011)


async def _proxy_interviewer(websocket: WebSocket, config: RealtimeContextConfig) -> None:
    settings = get_settings()
    answer_upstream = await _hub.ensure_main(config)
    transcription_upstream = await _connect_openai_realtime(intent="transcription")
    await _send_transcription_session_update(transcription_upstream)
    await websocket.send_json(
        {
            "type": "ready",
            "speaker": "interviewer",
            "source": f"{settings.openai_realtime_transcription_model}->{settings.openai_realtime_model}",
        }
    )

    pending_screen_requests: dict[str, asyncio.Future[str]] = {}
    client_task = asyncio.create_task(
        _forward_client_controls(
            websocket,
            answer_upstream,
            "interviewer",
            pending_screen_requests,
            transcription_upstream=transcription_upstream,
        )
    )
    answer_task = asyncio.create_task(_forward_main_events(websocket, answer_upstream, pending_screen_requests))
    transcription_task = asyncio.create_task(
        _forward_interviewer_transcription_events(websocket, transcription_upstream)
    )

    done, pending = await asyncio.wait({client_task, answer_task, transcription_task}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await _safe_close(transcription_upstream)
    for task in done:
        if task.cancelled():
            continue
        exception = task.exception()
        if exception is not None:
            raise exception


async def _proxy_candidate(websocket: WebSocket, config: RealtimeContextConfig) -> None:
    settings = get_settings()
    await _hub.update_config(config)
    upstream = await _connect_openai_realtime(intent="transcription")
    await _send_transcription_session_update(upstream)
    await websocket.send_json(
        {"type": "ready", "speaker": "candidate", "source": f"{settings.openai_realtime_transcription_model}:context"}
    )

    client_task = asyncio.create_task(_forward_client_controls(websocket, upstream, "candidate", {}))
    upstream_task = asyncio.create_task(_forward_candidate_events(websocket, upstream))

    done, pending = await asyncio.wait({client_task, upstream_task}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for task in done:
        if task.cancelled():
            continue
        exception = task.exception()
        if exception is not None:
            raise exception
    await _safe_close(upstream)


async def _forward_client_controls(
    websocket: WebSocket,
    upstream: ClientConnection,
    speaker: str,
    pending_screen_requests: dict[str, asyncio.Future[str]],
    *,
    transcription_upstream: ClientConnection | None = None,
) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            if speaker == "interviewer":
                await _hub.reset_main()
                if transcription_upstream is not None:
                    await _safe_close(transcription_upstream)
            else:
                await _safe_close(upstream)
            return

        binary_payload = message.get("bytes")
        if binary_payload is not None:
            await _send_audio_append(upstream, binary_payload)
            if transcription_upstream is not None:
                await _send_audio_append(transcription_upstream, binary_payload)
            continue

        text_payload = message.get("text")
        if not text_payload:
            continue
        payload = json.loads(text_payload)
        payload_type = payload.get("type")

        if payload_type == "close":
            if speaker == "interviewer":
                await _hub.reset_main()
                if transcription_upstream is not None:
                    await _safe_close(transcription_upstream)
            else:
                await _safe_close(upstream)
            return
        if payload_type == "screen_snapshot":
            request_id = str(payload.get("request_id") or "")
            future = pending_screen_requests.pop(request_id, None)
            if future is None or future.done():
                continue
            error = str(payload.get("error") or "").strip()
            image_url = str(payload.get("image_url") or "").strip()
            if error:
                future.set_exception(OpenAIRealtimeError(error))
            elif image_url:
                future.set_result(image_url)
            else:
                future.set_exception(OpenAIRealtimeError("Screen snapshot did not include image data."))
            continue
        if payload_type == "stop":
            await _send_json(upstream, {"type": "input_audio_buffer.clear"})
            if transcription_upstream is not None:
                await _send_json(transcription_upstream, {"type": "input_audio_buffer.clear"})
            continue
        if payload_type == "manual_text":
            text = str(payload.get("text", "")).strip()
            if not text:
                continue
            role_text = "候选人刚才补充的上下文" if speaker == "candidate" else "面试官文字问题"
            if speaker == "interviewer":
                await websocket.send_json({"type": "answer_status", "status": "pending", "text": "已收到文字问题，正在请求 Realtime..."})
                await _send_user_text(upstream, f"{role_text}: {text}", create_response=True)
            if speaker == "interviewer":
                await websocket.send_json({"type": "answer_status", "status": "pending", "text": "已发送 response.create，等待模型返回..."})
            if speaker == "candidate":
                await _hub.append_context(f"候选人补充: {text}")
            continue
        if payload_type == "update_scope":
            await _hub.update_config(_parse_config(payload))
            continue
        if payload_type == "screenshot":
            image_url = str(payload.get("image_url", "")).strip()
            prompt = str(payload.get("prompt", "这是当前面试题、白板或代码截图，请结合它回答。")).strip()
            create_response = bool(payload.get("create_response", False))
            if image_url:
                await _hub.update_screen_context(image_url, prompt)
                await _send_image_item(upstream, image_url=image_url, prompt=prompt, create_response=create_response)
            continue


async def _forward_main_events(
    websocket: WebSocket,
    upstream: ClientConnection,
    pending_screen_requests: dict[str, asyncio.Future[str]] | None = None,
) -> None:
    completed_text_responses: set[str] = set()
    pending_screen_requests = pending_screen_requests if pending_screen_requests is not None else {}
    async for raw_message in upstream:
        if isinstance(raw_message, bytes):
            continue
        payload = json.loads(raw_message)
        event_type = payload.get("type")
        response_id = _event_response_id(payload)

        if event_type in {"response.output_text.delta", "response.text.delta"}:
            await websocket.send_json({"type": "answer_delta", "delta": payload.get("delta", "")})
        elif event_type in {"response.output_text.done", "response.text.done"}:
            if response_id:
                completed_text_responses.add(response_id)
            await websocket.send_json({"type": "answer_done", "text": payload.get("text", "")})
        elif event_type == "response.content_part.done":
            text = _extract_content_part_text(payload.get("part"))
            if text and response_id not in completed_text_responses:
                if response_id:
                    completed_text_responses.add(response_id)
                await websocket.send_json({"type": "answer_done", "text": text})
        elif event_type == "response.output_item.done":
            text = _extract_response_item_text(payload.get("item"))
            if text and response_id not in completed_text_responses:
                if response_id:
                    completed_text_responses.add(response_id)
                await websocket.send_json({"type": "answer_done", "text": text})
        elif event_type == "response.created":
            await websocket.send_json({"type": "answer_status", "status": "pending", "text": "Realtime 已创建回复..."})
        elif event_type == "response.in_progress":
            await websocket.send_json({"type": "answer_status", "status": "pending", "text": "Realtime 正在生成..."})
        elif event_type == "response.done":
            response = payload.get("response") or {}
            status = response.get("status") if isinstance(response, dict) else ""
            status_detail = ""
            if isinstance(response, dict):
                status_detail = _response_status_detail(response)
                text = _extract_response_text(response)
                if text and response_id not in completed_text_responses:
                    if response_id:
                        completed_text_responses.add(response_id)
                    await websocket.send_json({"type": "answer_done", "text": text})
            if response_id not in completed_text_responses or status_detail != "completed":
                await websocket.send_json({"type": "response_done", "detail": status_detail or str(status or "")})
        elif event_type == "response.function_call_arguments.done":
            await _handle_tool_call(websocket, upstream, payload, pending_screen_requests)
        elif event_type == "conversation.item.input_audio_transcription.completed":
            transcript = str(payload.get("transcript", "")).strip()
            if transcript:
                await websocket.send_json({"type": "transcript", "speaker": "interviewer", "text": transcript, "is_final": True})
        elif event_type == "error":
            await websocket.send_json({"type": "error", "detail": _extract_error(payload)})


async def _forward_candidate_events(websocket: WebSocket, upstream: ClientConnection) -> None:
    async for raw_message in upstream:
        if isinstance(raw_message, bytes):
            continue
        payload = json.loads(raw_message)
        event_type = payload.get("type")
        if event_type == "conversation.item.input_audio_transcription.delta":
            delta = str(payload.get("delta", "")).strip()
            if delta:
                await websocket.send_json({"type": "transcript", "speaker": "candidate", "text": delta, "is_final": False})
        elif event_type == "conversation.item.input_audio_transcription.completed":
            transcript = str(payload.get("transcript", "")).strip()
            if not transcript:
                continue
            await _hub.append_context(f"候选人刚才说: {transcript}")
            await websocket.send_json(
                {"type": "transcript", "speaker": "candidate", "text": transcript, "is_final": True, "speech_final": True}
            )
        elif event_type == "error":
            await websocket.send_json({"type": "error", "detail": _extract_error(payload)})


async def _forward_interviewer_transcription_events(
    websocket: WebSocket,
    transcription_upstream: ClientConnection,
) -> None:
    async for raw_message in transcription_upstream:
        if isinstance(raw_message, bytes):
            continue
        payload = json.loads(raw_message)
        event_type = payload.get("type")
        if event_type == "conversation.item.input_audio_transcription.delta":
            delta = str(payload.get("delta", "")).strip()
            if delta:
                await websocket.send_json({"type": "transcript", "speaker": "interviewer", "text": delta, "is_final": False})
        elif event_type == "conversation.item.input_audio_transcription.completed":
            transcript = str(payload.get("transcript", "")).strip()
            if not transcript:
                continue
            await websocket.send_json(
                {"type": "transcript", "speaker": "interviewer", "text": transcript, "is_final": True, "speech_final": True}
            )
        elif event_type == "error":
            await websocket.send_json({"type": "error", "detail": _extract_error(payload)})


async def _handle_tool_call(
    websocket: WebSocket,
    upstream: ClientConnection,
    payload: dict[str, Any],
    pending_screen_requests: dict[str, asyncio.Future[str]] | None = None,
) -> None:
    call_id = str(payload.get("call_id") or payload.get("item_id") or "")
    name = str(payload.get("name") or "")
    if not call_id:
        return
    try:
        arguments = json.loads(str(payload.get("arguments") or "{}"))
    except json.JSONDecodeError:
        arguments = {}

    if name == "lookup_candidate_context":
        await websocket.send_json({"type": "answer_status", "status": "pending", "text": "正在查找项目/简历上下文..."})
        await _handle_lookup_tool_call(upstream, call_id, arguments)
        return

    if name == "capture_current_screen":
        await websocket.send_json({"type": "answer_status", "status": "pending", "text": "正在读取当前屏幕上下文..."})
        await _handle_screen_capture_tool_call(
            websocket,
            upstream,
            call_id,
            arguments,
            pending_screen_requests if pending_screen_requests is not None else {},
        )
        return

    if name == "solve_code_question":
        await websocket.send_json({"type": "answer_status", "status": "pending", "text": "正在用深度模型分析代码题..."})
        await _handle_code_tool_call(websocket, upstream, call_id, arguments)
        return


async def _handle_lookup_tool_call(upstream: ClientConnection, call_id: str, arguments: dict[str, Any]) -> None:
    context, current_scope = _hub.current_context()
    query = str(arguments.get("query") or "")
    scope = arguments.get("scope") if arguments.get("scope") in {"general", "innovation_ai", "canvasbot", "discordbot"} else current_scope
    result = lookup_candidate_context(query, scope, context)
    await _send_tool_output(upstream, call_id, result or "没有找到相关候选人背景。")
    await _send_response_create(upstream)


async def _handle_screen_capture_tool_call(
    websocket: WebSocket,
    upstream: ClientConnection,
    call_id: str,
    arguments: dict[str, Any],
    pending_screen_requests: dict[str, asyncio.Future[str]],
) -> None:
    request_id = str(uuid.uuid4())
    future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    pending_screen_requests[request_id] = future
    reason = str(arguments.get("reason") or "需要读取当前屏幕上的题目、白板或代码。").strip()
    await websocket.send_json({"type": "screen_capture_request", "request_id": request_id, "reason": reason})

    try:
        image_url = await asyncio.wait_for(future, timeout=12.0)
    except Exception as exc:
        pending_screen_requests.pop(request_id, None)
        await _send_tool_output(upstream, call_id, f"屏幕截图失败: {exc}")
        await _send_response_create(upstream)
        return

    summary = reason or "当前屏幕截图已作为后续代码题/白板题上下文。"
    await _hub.update_screen_context(image_url, summary)
    await _send_image_item(upstream, image_url=image_url, prompt=summary, create_response=False)
    await _send_tool_output(
        upstream,
        call_id,
        "已获取当前屏幕截图，并写入后续回答上下文。如果当前问题是代码、算法、SQL、debug 或代码 follow-up，请继续调用 solve_code_question。",
    )
    await _send_response_create(upstream)


async def _handle_code_tool_call(
    websocket: WebSocket,
    upstream: ClientConnection,
    call_id: str,
    arguments: dict[str, Any],
) -> None:
    try:
        answer = await _solve_code_question(arguments)
    except Exception as exc:
        await websocket.send_json(
            {
                "type": "answer_status",
                "status": "pending",
                "text": "深度模型暂时失败，Realtime 会先用已有上下文回答。",
                "detail": str(exc),
            }
        )
        await _send_tool_output(upstream, call_id, f"深度代码工具失败: {exc}")
        await _send_response_create(upstream)
        return

    await _hub.update_code_context(answer)
    await _send_tool_output(upstream, call_id, answer)
    await _send_response_create(upstream)


async def _receive_start(websocket: WebSocket) -> dict[str, Any]:
    message = await websocket.receive_text()
    payload = json.loads(message)
    if payload.get("type") != "start":
        raise OpenAIRealtimeError("Realtime socket must start with a start control message.")
    return payload


def _parse_config(payload: dict[str, Any]) -> RealtimeContextConfig:
    context_payload = payload.get("context") or {}
    try:
        context = CandidateContext.model_validate(context_payload)
    except ValidationError:
        context = CandidateContext()
    scope = payload.get("answer_scope") if payload.get("answer_scope") in {"general", "innovation_ai", "canvasbot", "discordbot"} else "general"
    return RealtimeContextConfig(
        context=context,
        answer_scope=scope,
        project_context_label=str(payload.get("project_context_label") or ""),
    )


async def _connect_openai_realtime(model: str | None = None, *, intent: str | None = None) -> ClientConnection:
    settings = get_settings()
    query_params = {"model": model or settings.openai_realtime_model}
    if intent:
        query_params = {"intent": intent}
    query = urlencode(query_params)
    base_url = settings.openai_base_url
    if base_url.startswith("https://"):
        ws_base = f"wss://{base_url[len('https://'):]}"
    elif base_url.startswith("http://"):
        ws_base = f"ws://{base_url[len('http://'):]}"
    else:
        ws_base = base_url
    return await websockets.connect(
        f"{ws_base}/realtime?{query}",
        additional_headers={
            "Authorization": f"Bearer {settings.openai_api_key}",
        },
        ping_interval=10,
        ping_timeout=20,
        max_size=None,
    )


async def _send_transcription_session_update(upstream: ClientConnection) -> None:
    settings = get_settings()
    transcription: dict[str, Any] = {"model": settings.openai_realtime_transcription_model}
    if settings.transcription_language:
        transcription["language"] = settings.transcription_language
    await _send_json(
        upstream,
        {
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {
                    "input": {
                        "format": AUDIO_INPUT_FORMAT,
                        "transcription": transcription,
                    }
                },
            },
        },
    )


async def _send_session_update(
    upstream: ClientConnection,
    *,
    config: RealtimeContextConfig,
    create_response: bool,
    transcription: bool,
) -> None:
    settings = get_settings()
    turn_detection: dict[str, Any] = {
        "type": "semantic_vad",
        "eagerness": "medium",
        "create_response": create_response,
        "interrupt_response": True,
    }
    audio_input: dict[str, Any] = {
        "format": AUDIO_INPUT_FORMAT,
        "turn_detection": turn_detection,
    }
    if transcription:
        audio_input_transcription: dict[str, Any] = {"model": settings.openai_realtime_transcription_model}
        if settings.transcription_language:
            audio_input_transcription["language"] = settings.transcription_language
        audio_input["transcription"] = audio_input_transcription
    session: dict[str, Any] = {
        "type": "realtime",
        "model": settings.openai_realtime_model,
        "output_modalities": TEXT_OUTPUT_MODALITIES,
        "instructions": build_realtime_instructions(config),
        "audio": {"input": audio_input},
        "reasoning": {"effort": settings.openai_realtime_reasoning_effort},
        "tools": [_lookup_tool_schema(), _screen_capture_tool_schema(), _solve_code_tool_schema()],
        "tool_choice": "auto",
    }

    await _send_json(
        upstream,
        {
            "type": "session.update",
            "session": session,
        },
    )


def _lookup_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "name": "lookup_candidate_context",
        "description": "查找候选人的简历、岗位要求、补充备注和所有项目背景。根据问题自动选择最相关事实，用于回答面试问题。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "面试官问题或背景关键词。"},
                "scope": {
                    "type": "string",
                    "description": "可选。默认 general 会自动搜索所有项目背景。",
                    "enum": ["general", "innovation_ai", "canvasbot", "discordbot"],
                },
            },
            "required": ["query"],
        },
    }


def _screen_capture_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "name": "capture_current_screen",
        "description": (
            "当问题依赖候选人当前屏幕、在线 IDE、白板、截图题或刚刚写的代码时调用。"
            "不要用于普通口述概念题。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "简短说明为什么需要读取屏幕，例如要看刚刚写的代码或白板题面。",
                }
            },
            "required": ["reason"],
        },
    }


def _solve_code_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "name": "solve_code_question",
        "description": (
            "用于真实编程/算法/复杂 SQL/debug/代码 follow-up。"
            "普通技术概念题、项目经历题和行为面试题不要调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "面试官当前完整问题，包含题目要求或追问。",
                },
                "problem_type": {
                    "type": "string",
                    "description": "可选分类，例如 algorithm、sql、debug、concurrency、follow_up。",
                },
                "visible_code_or_context": {
                    "type": "string",
                    "description": "如果 Realtime 已经从对话中知道题面、代码或关键上下文，写在这里。",
                },
            },
            "required": ["question"],
        },
    }


async def _send_audio_append(upstream: ClientConnection, audio_bytes: bytes) -> None:
    await _send_json(
        upstream,
        {
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(audio_bytes).decode("ascii"),
        },
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
    await _send_json(upstream, {"type": "response.create", "response": {"output_modalities": TEXT_OUTPUT_MODALITIES}})


async def _send_context_item(upstream: ClientConnection, text: str) -> None:
    await _send_user_text(upstream, f"候选人上下文更新: {text}", create_response=False)


async def _send_tool_output(upstream: ClientConnection, call_id: str, output: str) -> None:
    await _send_json(
        upstream,
        {
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": output,
            },
        },
    )


async def _send_image_item(upstream: ClientConnection, *, image_url: str, prompt: str, create_response: bool) -> None:
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


async def _solve_code_question(arguments: dict[str, Any]) -> str:
    settings = get_settings()
    if not settings.openai_api_key:
        raise OpenAIRealtimeError("OPENAI_API_KEY is not configured.")

    question = str(arguments.get("question") or "").strip()
    if not question:
        question = "请根据当前面试上下文解答代码题。"
    problem_type = str(arguments.get("problem_type") or "coding").strip()
    visible_context = str(arguments.get("visible_code_or_context") or "").strip()
    context, scope = _hub.current_context()
    screen_image_url, screen_summary, latest_code_context = await _hub.current_visual_code_context()

    prompt = _build_code_question_prompt(
        question=question,
        problem_type=problem_type,
        visible_context=visible_context,
        latest_code_context=latest_code_context,
        screen_summary=screen_summary,
        candidate_context=context,
        scope=scope,
    )
    content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    if screen_image_url:
        content.append({"type": "input_image", "image_url": screen_image_url, "detail": "auto"})

    payload: dict[str, Any] = {
        "model": settings.openai_code_model,
        "instructions": _code_solver_instructions(),
        "input": [{"role": "user", "content": content}],
        "text": {"verbosity": "medium"},
        "max_output_tokens": 2200,
    }
    if settings.openai_code_model.startswith("gpt-5"):
        payload["reasoning"] = {"effort": settings.openai_code_reasoning_effort}

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
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

    if data.get("status") == "incomplete":
        raise OpenAIRealtimeError(f"Code model response incomplete: {data.get('incomplete_details')}")
    answer = _extract_openai_response_text(data)
    if not answer:
        raise OpenAIRealtimeError("Code model response did not include output text.")
    return answer.strip()


def _code_solver_instructions() -> str:
    return (
        "你是候选人的代码题深度解题助手。输出必须是简体中文，像面试现场候选人可以参考着说和写。"
        "不要写免责声明，不要说你是 AI。"
        "优先保证正确性，再保证简洁。"
        "如果需要代码，给核心实现；如果题目信息不足，先列出一个合理假设再解。"
    )


def _build_code_question_prompt(
    *,
    question: str,
    problem_type: str,
    visible_context: str,
    latest_code_context: str,
    screen_summary: str,
    candidate_context: CandidateContext,
    scope: AnswerScope,
) -> str:
    return (
        "请解答当前技术面试代码/算法/SQL/debug 问题。\n"
        "输出结构固定为：\n"
        "1. 现场先说：用 2 到 4 句给候选人马上能说出口的思路。\n"
        "2. 核心实现：给必要代码或伪代码；如果是 SQL，给 SQL；如果是 debug，指出问题和修法。\n"
        "3. 复杂度/边界：说明时间空间复杂度、关键边界条件和 follow-up 风险。\n"
        "不要输出 Markdown 表格。不要长篇铺垫。\n"
        f"问题类型: {problem_type}\n"
        f"当前问题: {question}\n"
        f"Realtime 已知代码/题面上下文: {visible_context or '无'}\n"
        f"最近代码题上下文: {latest_code_context or '无'}\n"
        f"最近屏幕上下文: {screen_summary or '无'}\n"
        f"候选人目标岗位: {candidate_context.target_role or '未填写'}\n"
        f"候选人简历摘要: {_clip(candidate_context.resume, 1200) or '未填写'}\n"
        f"补充偏好: {_clip(candidate_context.custom_notes, 500) or '未填写'}\n"
        f"当前项目范围: {scope}\n"
    )


async def _send_json(upstream: ClientConnection, payload: dict[str, Any]) -> None:
    await upstream.send(json.dumps(payload, ensure_ascii=False))


async def _safe_close(upstream: ClientConnection) -> None:
    try:
        await upstream.close()
    except Exception:
        return


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
        reason = status_details.get("reason") or status_details.get("error")
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


def _extract_response_text(response: dict[str, Any]) -> str:
    output = response.get("output")
    if not isinstance(output, list):
        return ""
    parts: list[str] = []
    for item in output:
        text = _extract_response_item_text(item)
        if text:
            parts.append(text)
    return "\n\n".join(parts).strip()


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
    parts: list[str] = []
    for part in content:
        text = _extract_content_part_text(part)
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _extract_content_part_text(part: Any) -> str:
    if not isinstance(part, dict):
        return ""
    for key in ("text", "transcript"):
        value = part.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
