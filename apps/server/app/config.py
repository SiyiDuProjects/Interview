from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_fast_model: str = "gpt-5-mini"
    openai_model: str = "gpt-4.1-mini"
    openai_timeout_seconds: float = 12.0
    openai_transcription_model: str = "gpt-4o-mini-transcribe"
    openai_transcription_timeout_seconds: float = 20.0
    deepgram_api_key: str = ""
    deepgram_ws_url: str = "wss://api.deepgram.com/v1/listen"
    deepgram_model: str = "nova-3"
    deepgram_language: str = "zh-CN"
    deepgram_language_en: str = "en-US"
    deepgram_interim_results: bool = True
    deepgram_endpointing_ms: int = 400
    deepgram_punctuate: bool = True
    deepgram_smart_format: bool = True
    xfyun_rtasr_app_id: str = ""
    xfyun_rtasr_access_key_id: str = ""
    xfyun_rtasr_access_key_secret: str = ""
    xfyun_rtasr_ws_url: str = "wss://office-api-ast-dx.iflyaisol.com/ast/communicate/v1"
    xfyun_rtasr_lang: str = "autodialect"
    xfyun_rtasr_punc: int = 1
    xfyun_rtasr_pd: str = "tech"
    xfyun_rtasr_vad_mdn: int = 2
    xfyun_rtasr_eng_lang_type: int = 2
    xfyun_rtasr_role_type: int = 0
    transcription_provider: str = "deepgram"
    transcription_model_size: str = ""
    transcription_device: str = "auto"
    transcription_compute_type: str = "auto"
    transcription_language: str = ""


def _load_dotenv() -> None:
    cwd = Path.cwd().resolve()
    candidates = [cwd / ".env", cwd.parent / ".env", cwd.parent.parent / ".env"]
    for candidate in candidates:
        if not candidate.exists():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())
        break


def get_settings() -> Settings:
    _load_dotenv()
    timeout_value = os.getenv("OPENAI_TIMEOUT_SECONDS", "12")
    transcription_timeout_value = os.getenv("OPENAI_TRANSCRIPTION_TIMEOUT_SECONDS", "20")
    try:
        timeout_seconds = float(timeout_value)
    except ValueError:
        timeout_seconds = 12.0
    try:
        transcription_timeout_seconds = float(transcription_timeout_value)
    except ValueError:
        transcription_timeout_seconds = 20.0
    deepgram_endpointing_raw = os.getenv("DEEPGRAM_ENDPOINTING_MS", "400")
    try:
        deepgram_endpointing_ms = int(deepgram_endpointing_raw)
    except ValueError:
        deepgram_endpointing_ms = 400

    def parse_bool(name: str, default: bool) -> bool:
        raw_value = os.getenv(name)
        if raw_value is None:
            return default
        return raw_value.strip().lower() in {"1", "true", "yes", "on"}

    def env_first(*names: str, default: str = "") -> str:
        for name in names:
            value = os.getenv(name)
            if value is not None and value.strip():
                return value.strip()
        return default

    def env_int_first(*names: str, default: int) -> int:
        for name in names:
            value = os.getenv(name)
            if value is None or not value.strip():
                continue
            try:
                return int(value.strip())
            except ValueError:
                continue
        return default

    return Settings(
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        openai_fast_model=os.getenv("OPENAI_FAST_MODEL", "gpt-5-mini").strip() or "gpt-5-mini",
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini",
        openai_timeout_seconds=timeout_seconds,
        openai_transcription_model=os.getenv("OPENAI_TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe").strip()
        or "gpt-4o-mini-transcribe",
        openai_transcription_timeout_seconds=transcription_timeout_seconds,
        deepgram_api_key=os.getenv("DEEPGRAM_API_KEY", "").strip(),
        deepgram_ws_url=os.getenv("DEEPGRAM_WS_URL", "wss://api.deepgram.com/v1/listen").strip()
        or "wss://api.deepgram.com/v1/listen",
        deepgram_model=os.getenv("DEEPGRAM_MODEL", "nova-3").strip() or "nova-3",
        deepgram_language=os.getenv("DEEPGRAM_LANGUAGE", "zh-CN").strip(),
        deepgram_language_en=os.getenv("DEEPGRAM_LANGUAGE_EN", "en-US").strip() or "en-US",
        deepgram_interim_results=parse_bool("DEEPGRAM_INTERIM_RESULTS", True),
        deepgram_endpointing_ms=deepgram_endpointing_ms,
        deepgram_punctuate=parse_bool("DEEPGRAM_PUNCTUATE", True),
        deepgram_smart_format=parse_bool("DEEPGRAM_SMART_FORMAT", True),
        xfyun_rtasr_app_id=env_first("XFYUN_ASR_LLM_APP_ID", "XFYUN_RTASR_APP_ID"),
        xfyun_rtasr_access_key_id=env_first("XFYUN_ASR_LLM_ACCESS_KEY_ID", "XFYUN_RTASR_ACCESS_KEY_ID"),
        xfyun_rtasr_access_key_secret=env_first(
            "XFYUN_ASR_LLM_ACCESS_KEY_SECRET",
            "XFYUN_RTASR_ACCESS_KEY_SECRET",
        ),
        xfyun_rtasr_ws_url=env_first(
            "XFYUN_ASR_LLM_WS_URL",
            "XFYUN_RTASR_WS_URL",
            default="wss://office-api-ast-dx.iflyaisol.com/ast/communicate/v1",
        ),
        xfyun_rtasr_lang=env_first("XFYUN_ASR_LLM_LANG", "XFYUN_RTASR_LANG", default="autodialect"),
        xfyun_rtasr_punc=env_int_first("XFYUN_ASR_LLM_PUNC", "XFYUN_RTASR_PUNC", default=1),
        xfyun_rtasr_pd=env_first("XFYUN_ASR_LLM_PD", "XFYUN_RTASR_PD", default="tech"),
        xfyun_rtasr_vad_mdn=env_int_first("XFYUN_ASR_LLM_VAD_MDN", "XFYUN_RTASR_VAD_MDN", default=2),
        xfyun_rtasr_eng_lang_type=env_int_first(
            "XFYUN_ASR_LLM_ENG_LANG_TYPE",
            "XFYUN_RTASR_ENG_LANG_TYPE",
            default=2,
        ),
        xfyun_rtasr_role_type=env_int_first("XFYUN_ASR_LLM_ROLE_TYPE", "XFYUN_RTASR_ROLE_TYPE", default=0),
        transcription_provider=os.getenv("TRANSCRIPTION_PROVIDER", "deepgram").strip() or "deepgram",
        transcription_model_size=os.getenv("TRANSCRIPTION_MODEL_SIZE", "small").strip(),
        transcription_device=os.getenv("TRANSCRIPTION_DEVICE", "auto").strip() or "auto",
        transcription_compute_type=os.getenv("TRANSCRIPTION_COMPUTE_TYPE", "auto").strip() or "auto",
        transcription_language=os.getenv("TRANSCRIPTION_LANGUAGE", "zh").strip(),
    )
