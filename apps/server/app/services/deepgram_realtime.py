from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlencode

import websockets
from fastapi import WebSocket
from websockets.asyncio.client import ClientConnection

from app.config import get_settings


class DeepgramRealtimeError(RuntimeError):
    pass


async def proxy_live_transcription(websocket: WebSocket, speaker: str, locale: str = "zh") -> None:
    settings = get_settings()
    if not settings.deepgram_api_key:
        raise DeepgramRealtimeError("未配置 DEEPGRAM_API_KEY，无法启动实时转写。")

    async with websockets.connect(
        build_deepgram_ws_url(locale),
        additional_headers={"Authorization": f"Token {settings.deepgram_api_key}"},
        ping_interval=10,
        ping_timeout=20,
        max_size=None,
    ) as upstream:
        await websocket.send_json(
            {
                "type": "ready",
                "speaker": speaker,
                "source": f"deepgram-live:{settings.deepgram_model}",
            }
        )

        forward_client = asyncio.create_task(_forward_client_audio(websocket, upstream))
        forward_upstream = asyncio.create_task(_forward_deepgram_events(websocket, upstream, speaker))
        keepalive = asyncio.create_task(_send_keepalive(upstream))

        done, pending = await asyncio.wait(
            {forward_client, forward_upstream, keepalive},
            return_when=asyncio.FIRST_EXCEPTION,
        )

        for task in pending:
            task.cancel()

        for task in done:
            exception = task.exception()
            if exception is not None:
                raise exception


def build_deepgram_ws_url(locale: str = "zh") -> str:
    settings = get_settings()
    query = {
        "model": settings.deepgram_model,
        "encoding": "linear16",
        "sample_rate": "16000",
        "channels": "1",
        "interim_results": str(settings.deepgram_interim_results).lower(),
        "endpointing": str(settings.deepgram_endpointing_ms),
        "punctuate": str(settings.deepgram_punctuate).lower(),
        "smart_format": str(settings.deepgram_smart_format).lower(),
        "language": settings.deepgram_language_en if locale.startswith("en") else settings.deepgram_language,
    }
    return f"{settings.deepgram_ws_url}?{urlencode(query)}"


async def _forward_client_audio(websocket: WebSocket, upstream: ClientConnection) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            await _safe_send_json(upstream, {"type": "CloseStream"})
            return

        binary_payload = message.get("bytes")
        text_payload = message.get("text")
        if binary_payload is not None:
            await upstream.send(binary_payload)
            continue

        if not text_payload:
            continue

        try:
            payload = json.loads(text_payload)
        except json.JSONDecodeError:
            continue

        payload_type = payload.get("type")
        if payload_type == "finalize":
            await _safe_send_json(upstream, {"type": "Finalize"})
        elif payload_type == "close":
            await _safe_send_json(upstream, {"type": "CloseStream"})
            return


async def _forward_deepgram_events(websocket: WebSocket, upstream: ClientConnection, speaker: str) -> None:
    async for raw_message in upstream:
        if isinstance(raw_message, bytes):
            continue

        try:
            payload = json.loads(raw_message)
        except json.JSONDecodeError:
            continue

        event = _translate_deepgram_event(payload, speaker)
        if event is None:
            continue
        await websocket.send_json(event)


async def _send_keepalive(upstream: ClientConnection) -> None:
    while True:
        await asyncio.sleep(5)
        await _safe_send_json(upstream, {"type": "KeepAlive"})


async def _safe_send_json(upstream: ClientConnection, payload: dict[str, Any]) -> None:
    try:
        await upstream.send(json.dumps(payload))
    except Exception:
        return


def _translate_deepgram_event(payload: dict[str, Any], speaker: str) -> dict[str, Any] | None:
    event_type = payload.get("type")
    if event_type == "Results":
        channel = payload.get("channel", {})
        alternatives = channel.get("alternatives", [])
        if not alternatives:
            return None

        transcript = str(alternatives[0].get("transcript", "")).strip()
        if not transcript:
            return None

        return {
            "type": "transcript",
            "speaker": speaker,
            "text": transcript,
            "is_final": bool(payload.get("is_final", False)),
            "speech_final": bool(payload.get("speech_final", False)),
        }

    if event_type == "UtteranceEnd":
        return {
            "type": "utterance_end",
            "speaker": speaker,
        }

    return None
