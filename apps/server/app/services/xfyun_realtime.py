from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlencode

import websockets
from fastapi import WebSocket
from websockets.asyncio.client import ClientConnection

from app.config import get_settings


class XfyunRealtimeError(RuntimeError):
    pass


async def proxy_xfyun_live_transcription(websocket: WebSocket, speaker: str) -> None:
    settings = get_settings()
    if not settings.xfyun_rtasr_app_id or not settings.xfyun_rtasr_access_key_id or not settings.xfyun_rtasr_access_key_secret:
        raise XfyunRealtimeError("未配置讯飞实时转写凭据。")

    session_id = str(uuid.uuid4())
    async with websockets.connect(
        build_xfyun_ws_url(session_id),
        ping_interval=10,
        ping_timeout=20,
        max_size=None,
    ) as upstream:
        await websocket.send_json(
            {
                "type": "ready",
                "speaker": speaker,
                "source": f"xfyun-asr-llm:{settings.xfyun_rtasr_lang}",
            }
        )

        forward_client = asyncio.create_task(_forward_client_audio(websocket, upstream, session_id))
        forward_upstream = asyncio.create_task(_forward_xfyun_events(websocket, upstream, speaker))

        done, pending = await asyncio.wait(
            {forward_client, forward_upstream},
            return_when=asyncio.FIRST_EXCEPTION,
        )

        for task in pending:
            task.cancel()

        for task in done:
            exception = task.exception()
            if exception is not None:
                raise exception


def build_xfyun_ws_url(session_id: str) -> str:
    settings = get_settings()
    utc_string = _beijing_now().strftime("%Y-%m-%dT%H:%M:%S%z")
    params = {
        "accessKeyId": settings.xfyun_rtasr_access_key_id,
        "appId": settings.xfyun_rtasr_app_id,
        "audio_encode": "pcm_s16le",
        "eng_lang_type": str(settings.xfyun_rtasr_eng_lang_type),
        "eng_vad_mdn": str(settings.xfyun_rtasr_vad_mdn),
        "lang": settings.xfyun_rtasr_lang,
        "punc": str(settings.xfyun_rtasr_punc),
        "samplerate": "16000",
        "utc": utc_string,
        "uuid": session_id,
    }
    if settings.xfyun_rtasr_pd:
        params["pd"] = settings.xfyun_rtasr_pd
    if settings.xfyun_rtasr_role_type:
        params["role_type"] = str(settings.xfyun_rtasr_role_type)

    base_string = urlencode(sorted(params.items()), quote_via=quote)
    signature = base64.b64encode(
        hmac.new(
            settings.xfyun_rtasr_access_key_secret.encode("utf-8"),
            base_string.encode("utf-8"),
            hashlib.sha1,
        ).digest()
    ).decode("utf-8")
    return f"{settings.xfyun_rtasr_ws_url}?{base_string}&signature={quote(signature, safe='')}"


async def _forward_client_audio(websocket: WebSocket, upstream: ClientConnection, session_id: str) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            await _send_end(upstream, session_id)
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

        if payload.get("type") in {"finalize", "close"}:
            await _send_end(upstream, session_id)
            if payload.get("type") == "close":
                await upstream.close()
                return


async def _forward_xfyun_events(websocket: WebSocket, upstream: ClientConnection, speaker: str) -> None:
    async for raw_message in upstream:
        if isinstance(raw_message, bytes):
            continue

        try:
            payload = json.loads(raw_message)
        except json.JSONDecodeError:
            continue

        event = _translate_xfyun_event(payload, speaker)
        if event is None:
            continue
        await websocket.send_json(event)


async def _send_end(upstream: ClientConnection, session_id: str) -> None:
    try:
        await upstream.send(json.dumps({"end": True, "sessionId": session_id}))
    except Exception:
        return


def _translate_xfyun_event(payload: dict[str, Any], speaker: str) -> dict[str, Any] | None:
    action = payload.get("action") or payload.get("msg_type")
    if action == "error":
        raise XfyunRealtimeError(str(payload.get("desc") or "讯飞实时转写失败"))

    if action in {"started", "result"}:
        data = payload.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                data = {}
        if not isinstance(data, dict):
            return None

        transcript = _extract_xfyun_transcript(data)
        if not transcript:
            return None

        st = (((data.get("cn") or {}).get("st")) or {})
        result_type = str(st.get("type", "1"))
        is_last = bool(data.get("ls", False))
        is_final = result_type == "0"

        return {
            "type": "transcript",
            "speaker": speaker,
            "text": transcript,
            "is_final": is_final,
            "speech_final": is_final or is_last,
        }

    return None


def _extract_xfyun_transcript(data: dict[str, Any]) -> str:
    words: list[str] = []
    rt_items = ((((data.get("cn") or {}).get("st")) or {}).get("rt")) or []
    for rt_item in rt_items:
        for ws_item in rt_item.get("ws", []):
            for cw_item in ws_item.get("cw", []):
                word = str(cw_item.get("w", "")).strip()
                if word:
                    words.append(word)
    return _join_xfyun_tokens(words).strip()


def _join_xfyun_tokens(words: list[str]) -> str:
    if not words:
        return ""

    merged = words[0]
    for word in words[1:]:
        if _should_insert_space(merged, word):
            merged = f"{merged} {word}"
        else:
            merged = f"{merged}{word}"
    return merged


def _should_insert_space(left: str, right: str) -> bool:
    left_text = left.rstrip()
    right_text = right.lstrip()
    if not left_text or not right_text:
        return False

    return bool(_is_ascii_word_char(left_text[-1]) and _is_ascii_word_char(right_text[0]))


def _is_ascii_word_char(char: str) -> bool:
    return ("a" <= char.lower() <= "z") or ("0" <= char <= "9")


def _beijing_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))
