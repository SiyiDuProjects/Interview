from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_realtime_model: str = "gpt-realtime-2"
    openai_realtime_transcription_model: str = "gpt-realtime-whisper"
    openai_realtime_transcription_language: str = "zh"
    openai_realtime_reasoning_effort: str = "low"
    openai_code_model: str = "gpt-5.5"
    openai_code_reasoning_effort: str = "high"
    openai_code_timeout_seconds: float = 45.0


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
    code_timeout_value = os.getenv("OPENAI_CODE_TIMEOUT_SECONDS", "45")
    try:
        code_timeout_seconds = float(code_timeout_value)
    except ValueError:
        code_timeout_seconds = 45.0

    return Settings(
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        openai_realtime_model=os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2").strip() or "gpt-realtime-2",
        openai_realtime_transcription_model=os.getenv(
            "OPENAI_REALTIME_TRANSCRIPTION_MODEL",
            "gpt-realtime-whisper",
        ).strip()
        or "gpt-realtime-whisper",
        openai_realtime_transcription_language=os.getenv("OPENAI_REALTIME_TRANSCRIPTION_LANGUAGE", "zh").strip()
        or "zh",
        openai_realtime_reasoning_effort=os.getenv("OPENAI_REALTIME_REASONING_EFFORT", "low").strip() or "low",
        openai_code_model=os.getenv("OPENAI_CODE_MODEL", "gpt-5.5").strip() or "gpt-5.5",
        openai_code_reasoning_effort=os.getenv("OPENAI_CODE_REASONING_EFFORT", "high").strip() or "high",
        openai_code_timeout_seconds=code_timeout_seconds,
    )
