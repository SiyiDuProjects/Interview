from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

from app.config import get_settings


class TranscriptionError(RuntimeError):
    pass


@dataclass(frozen=True)
class _TranscriptionRuntime:
    source: str


_runtime: _TranscriptionRuntime | None = None


def reset_model_cache_for_tests() -> None:
    global _runtime
    _runtime = None


def get_transcription_source() -> str:
    return _ensure_runtime().source


def warmup_transcription_model() -> str:
    return _ensure_runtime().source


async def transcribe_audio_chunk(
    *,
    audio_bytes: bytes,
    filename: str,
    content_type: str,
    speaker: str,
) -> str:
    settings = get_settings()
    if len(audio_bytes) < 2048:
        return ""

    if settings.transcription_provider == "deepgram":
        return await _transcribe_with_deepgram(
            audio_bytes=audio_bytes,
            filename=filename,
            content_type=content_type,
        )

    if settings.transcription_provider == "openai":
        return await _transcribe_with_openai(
            audio_bytes=audio_bytes,
            filename=filename,
            content_type=content_type,
        )

    raise TranscriptionError(f"不支持的转写 provider: {settings.transcription_provider}")


def _ensure_runtime() -> _TranscriptionRuntime:
    global _runtime
    if _runtime is not None:
        return _runtime

    settings = get_settings()
    if settings.transcription_provider == "deepgram":
        if not settings.deepgram_api_key:
            raise TranscriptionError("未配置 DEEPGRAM_API_KEY，无法初始化语音转写。")
        _runtime = _TranscriptionRuntime(source=f"deepgram-live:{settings.deepgram_model}")
        return _runtime

    if settings.transcription_provider == "openai":
        if not settings.openai_api_key:
            raise TranscriptionError("未配置 OPENAI_API_KEY，无法初始化语音转写。")
        _runtime = _TranscriptionRuntime(source=f"openai-stt:{settings.openai_transcription_model}")
        return _runtime

    raise TranscriptionError(f"不支持的转写 provider: {settings.transcription_provider}")


async def _transcribe_with_openai(*, audio_bytes: bytes, filename: str, content_type: str) -> str:
    settings = get_settings()
    if not settings.openai_api_key:
        raise TranscriptionError("未配置 OPENAI_API_KEY，无法调用语音转写。")

    data = {
        "model": settings.openai_transcription_model,
        "language": settings.transcription_language or "zh",
        "response_format": "json",
    }
    files = {
        "file": (filename or "chunk.webm", audio_bytes, content_type or "audio/webm"),
    }

    timeout = httpx.Timeout(
        connect=10.0,
        read=settings.openai_transcription_timeout_seconds,
        write=20.0,
        pool=10.0,
    )
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{settings.openai_base_url}/audio/transcriptions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                data=data,
                files=files,
            )
            response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise TranscriptionError("语音转写超时，请稍后重试。") from exc
    except httpx.HTTPStatusError as exc:
        detail = _extract_error_message(exc.response)
        raise TranscriptionError(f"语音转写失败：{detail}") from exc
    except httpx.HTTPError as exc:
        raise TranscriptionError("语音转写请求失败，请检查网络或 API 配置。") from exc

    text = _extract_text_payload(response)
    return _normalize_transcribed_text(text)


async def _transcribe_with_deepgram(*, audio_bytes: bytes, filename: str, content_type: str) -> str:
    settings = get_settings()
    if not settings.deepgram_api_key:
        raise TranscriptionError("未配置 DEEPGRAM_API_KEY，无法调用语音转写。")

    params = {
        "model": settings.deepgram_model,
        "punctuate": str(settings.deepgram_punctuate).lower(),
        "smart_format": str(settings.deepgram_smart_format).lower(),
    }
    if settings.deepgram_language:
        params["language"] = settings.deepgram_language

    timeout = httpx.Timeout(connect=10.0, read=25.0, write=20.0, pool=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                "https://api.deepgram.com/v1/listen",
                params=params,
                headers={
                    "Authorization": f"Token {settings.deepgram_api_key}",
                    "Content-Type": content_type or "audio/webm",
                },
                content=audio_bytes,
            )
            response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise TranscriptionError("语音转写超时，请稍后重试。") from exc
    except httpx.HTTPStatusError as exc:
        detail = _extract_error_message(exc.response)
        raise TranscriptionError(f"语音转写失败：{detail}") from exc
    except httpx.HTTPError as exc:
        raise TranscriptionError("语音转写请求失败，请检查网络或 API 配置。") from exc

    payload = response.json()
    text = (
        payload.get("results", {})
        .get("channels", [{}])[0]
        .get("alternatives", [{}])[0]
        .get("transcript", "")
    )
    return _normalize_transcribed_text(str(text))


def _extract_text_payload(response: httpx.Response) -> str:
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        payload = response.json()
    else:
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError:
            payload = {"text": response.text}

    text = payload.get("text", "") if isinstance(payload, dict) else ""
    if not isinstance(text, str):
        text = str(text)
    return text.strip()


def _extract_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except json.JSONDecodeError:
        text = response.text.strip()
        return text or f"HTTP {response.status_code}"

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()

    return f"HTTP {response.status_code}"


def _normalize_transcribed_text(text: str) -> str:
    return " ".join(text.split())
