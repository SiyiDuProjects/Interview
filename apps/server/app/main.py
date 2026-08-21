from __future__ import annotations

import hmac
import hashlib
import time
import ipaddress
from pathlib import Path
from typing import cast

from fastapi import Cookie, FastAPI, Header, HTTPException, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.models import BrowserLogin, ConnectionRole
from app.services.openai_realtime import OpenAIRealtimeError, get_interview_registry


API_VERSION = "0.5.0"
REALTIME_PROTOCOL_VERSION = "realtime-interview-v4"
BROWSER_COOKIE_NAME = "interview_browser_session"
BROWSER_COOKIE_TTL_SECONDS = 3600

app = FastAPI(title="Interview Copilot API", version=API_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(get_settings().interview_allowed_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    settings = get_settings()
    return {
        "status": "ok",
        "version": API_VERSION,
        "realtime_protocol": REALTIME_PROTOCOL_VERSION,
        "realtime_model": settings.openai_realtime_model,
        "realtime_transcription_model": settings.openai_realtime_transcription_model,
        "realtime_reasoning_effort": settings.openai_realtime_reasoning_effort,
        "code_model": settings.openai_code_model,
    }


@app.post("/api/interviews", status_code=status.HTTP_201_CREATED)
async def create_interview(
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    configured_token = get_settings().interview_access_token
    _require_configured_or_loopback(request, configured_token)
    if configured_token and not _token_matches(_bearer_token(authorization), configured_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid interview access token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    response.headers["Cache-Control"] = "no-store"
    runtime = await get_interview_registry().create()
    return {
        "interview_id": runtime.interview_id,
        "session_token": runtime.session_token,
        "capture_token": runtime.capture_token,
        "expires_at": runtime.expires_at.isoformat().replace("+00:00", "Z"),
    }


@app.post("/api/browser/login")
async def browser_login(payload: BrowserLogin, request: Request, response: Response) -> dict[str, bool]:
    configured_token = get_settings().interview_access_token
    _require_configured_or_loopback(request, configured_token)
    if configured_token and not _token_matches(payload.access_token, configured_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid access token.")
    if configured_token:
        response.set_cookie(
            key=BROWSER_COOKIE_NAME,
            value=_make_browser_cookie(configured_token),
            max_age=BROWSER_COOKIE_TTL_SECONDS,
            httponly=True,
            secure=_request_uses_https(request),
            samesite="strict",
            path="/",
        )
    response.headers["Cache-Control"] = "no-store"
    return {"ok": True}


@app.get("/api/interviews/current", response_model=None)
async def current_interview(
    request: Request,
    response: Response,
    browser_session: str | None = Cookie(default=None, alias=BROWSER_COOKIE_NAME),
) -> dict[str, object] | Response:
    configured_token = get_settings().interview_access_token
    _require_configured_or_loopback(request, configured_token)
    if configured_token and not _valid_browser_cookie(browser_session or "", configured_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Browser login required.")
    response.headers["Cache-Control"] = "no-store"
    runtime = await get_interview_registry().current()
    if runtime is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"Cache-Control": "no-store"})
    return await runtime.public_state()


@app.delete("/api/interviews/{interview_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_interview(
    interview_id: str,
    authorization: str | None = Header(default=None),
) -> Response:
    registry = get_interview_registry()
    runtime = await registry.get(interview_id)
    if runtime is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interview session not found.")
    if not runtime.token_matches(_bearer_token(authorization)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid interview session token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    await registry.delete(interview_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.websocket("/ws/interviews/{interview_id}/{speaker}")
async def interview_stream(websocket: WebSocket, interview_id: str, speaker: str) -> None:
    if speaker not in {"interviewer", "candidate", "client"}:
        await websocket.close(code=1008)
        return
    if not _websocket_origin_allowed(websocket):
        await websocket.close(code=1008)
        return
    runtime = await get_interview_registry().get(interview_id)
    if runtime is None:
        await websocket.close(code=1008)
        return
    try:
        await runtime.serve(websocket, cast(ConnectionRole, speaker))
    except WebSocketDisconnect:
        return
    except OpenAIRealtimeError as exc:
        await _send_socket_error(websocket, str(exc))
    except Exception as exc:
        await _send_socket_error(websocket, str(exc))


async def _send_socket_error(websocket: WebSocket, detail: str) -> None:
    try:
        await websocket.send_json({"type": "error", "detail": detail})
    except Exception:
        pass
    try:
        await websocket.close(code=1011)
    except Exception:
        pass


def _bearer_token(authorization: str | None) -> str:
    if not authorization:
        return ""
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return ""
    return token.strip()


def _token_matches(provided: str, expected: str) -> bool:
    return bool(provided) and hmac.compare_digest(provided, expected)


def _make_browser_cookie(secret: str, *, issued_at: int | None = None) -> str:
    issued = int(time.time()) if issued_at is None else issued_at
    signature = hmac.new(
        hashlib.sha256(secret.encode("utf-8")).digest(),
        f"browser:{issued}".encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"{issued}.{signature}"


def _valid_browser_cookie(value: str, secret: str, *, now: int | None = None) -> bool:
    issued_text, separator, signature = value.partition(".")
    if not separator or not issued_text.isdigit() or not signature:
        return False
    issued = int(issued_text)
    current = int(time.time()) if now is None else now
    if issued > current + 60 or current - issued > BROWSER_COOKIE_TTL_SECONDS:
        return False
    expected = _make_browser_cookie(secret, issued_at=issued)
    return hmac.compare_digest(value, expected)


def _request_uses_https(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    return request.url.scheme == "https" or forwarded == "https" or not _is_loopback_request(request)


def _require_configured_or_loopback(request: Request, configured_token: str) -> None:
    if not configured_token and not _is_loopback_request(request):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="INTERVIEW_ACCESS_TOKEN is required for remote access.",
        )


def _is_loopback_request(request: Request) -> bool:
    host = (request.url.hostname or "").strip("[]").lower()
    client_host = (request.client.host if request.client else "").strip("[]").lower()
    return _is_loopback_host(host) and _is_loopback_host(client_host)


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _websocket_origin_allowed(websocket: WebSocket) -> bool:
    raw_origin = (websocket.headers.get("origin") or "").strip()
    if not raw_origin or raw_origin in {"null", "file:", "file://"}:
        return True
    origin = raw_origin.rstrip("/")
    if origin in get_settings().interview_allowed_origins:
        return True
    forwarded_proto = websocket.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    scheme = forwarded_proto or ("https" if websocket.url.scheme == "wss" else "http")
    host = (
        websocket.headers.get("x-forwarded-host", "").split(",", 1)[0].strip()
        or websocket.headers.get("host", "").strip()
    )
    return bool(host) and origin == f"{scheme}://{host}".rstrip("/")


def _mount_web_app() -> None:
    server_root = Path(__file__).resolve().parents[1]
    candidates = (server_root / "web", server_root.parent / "desktop" / "dist")
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            app.mount("/", StaticFiles(directory=candidate, html=True), name="web")
            return


@app.api_route(
    "/api/{unmatched_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def unmatched_api(unmatched_path: str) -> None:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API route not found.")


@app.websocket("/ws/{unmatched_path:path}")
async def unmatched_websocket(websocket: WebSocket, unmatched_path: str) -> None:
    await websocket.close(code=1008)


_mount_web_app()
