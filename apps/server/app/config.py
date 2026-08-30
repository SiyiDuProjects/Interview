from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_realtime_model: str = "gpt-realtime-2.1"
    openai_realtime_transcription_model: str = "gpt-realtime-whisper"
    openai_realtime_transcription_language: str = ""
    openai_realtime_reasoning_effort: str = "low"
    openai_code_model: str = "gpt-5.6-sol"
    openai_code_reasoning_effort: str = "high"
    openai_code_timeout_seconds: float = 45.0
    interview_access_token: str = ""
    interview_session_ttl_seconds: int = 3600
    interview_context_dir: str = ""
    interview_screenshot_max_bytes: int = 5 * 1024 * 1024
    interview_allowed_origins: tuple[str, ...] = (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    )


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
    code_timeout_seconds = _float_env("OPENAI_CODE_TIMEOUT_SECONDS", 45.0, minimum=1.0)
    interview_session_ttl_seconds = int(
        _float_env("INTERVIEW_SESSION_TTL_SECONDS", 3600.0, minimum=60.0, maximum=86400.0)
    )
    interview_screenshot_max_bytes = int(
        _float_env(
            "INTERVIEW_SCREENSHOT_MAX_BYTES",
            float(5 * 1024 * 1024),
            minimum=1024.0,
            maximum=float(20 * 1024 * 1024),
        )
    )

    return Settings(
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        openai_base_url=_validated_openai_base_url(),
        openai_realtime_model=os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1").strip()
        or "gpt-realtime-2.1",
        openai_realtime_transcription_model=os.getenv(
            "OPENAI_REALTIME_TRANSCRIPTION_MODEL",
            "gpt-realtime-whisper",
        ).strip()
        or "gpt-realtime-whisper",
        openai_realtime_transcription_language=os.getenv("OPENAI_REALTIME_TRANSCRIPTION_LANGUAGE", "").strip(),
        openai_realtime_reasoning_effort=os.getenv("OPENAI_REALTIME_REASONING_EFFORT", "low").strip() or "low",
        openai_code_model=os.getenv("OPENAI_CODE_MODEL", "gpt-5.6-sol").strip() or "gpt-5.6-sol",
        openai_code_reasoning_effort=os.getenv("OPENAI_CODE_REASONING_EFFORT", "high").strip() or "high",
        openai_code_timeout_seconds=code_timeout_seconds,
        interview_access_token=os.getenv("INTERVIEW_ACCESS_TOKEN", "").strip(),
        interview_session_ttl_seconds=interview_session_ttl_seconds,
        interview_context_dir=os.getenv("INTERVIEW_CONTEXT_DIR", "").strip(),
        interview_screenshot_max_bytes=interview_screenshot_max_bytes,
        interview_allowed_origins=_csv_env(
            "INTERVIEW_ALLOWED_ORIGINS",
            ("http://localhost:5173", "http://127.0.0.1:5173"),
        ),
    )


def _float_env(name: str, default: float, *, minimum: float, maximum: float | None = None) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _csv_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    values = tuple(value.strip().rstrip("/") for value in os.getenv(name, "").split(",") if value.strip())
    return values or default


def _validated_openai_base_url() -> str:
    value = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.username or parsed.password:
        raise ValueError("OPENAI_BASE_URL must not contain user information.")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("OPENAI_BASE_URL must be an HTTP(S) URL.")
    if parsed.scheme == "http" and parsed.hostname.lower() not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Remote OPENAI_BASE_URL must use HTTPS.")
    return value
