from __future__ import annotations

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.models import CandidateContext
from app.services.openai_realtime import OpenAIRealtimeError, proxy_realtime_interview


API_VERSION = "0.3.1"
REALTIME_PROTOCOL_VERSION = "realtime-text-events-v2"

app = FastAPI(title="Interview Copilot API", version=API_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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


@app.post("/api/context/preview")
def preview_context(context: CandidateContext) -> dict[str, str]:
    parts = []
    if context.name:
        parts.append(f"候选人: {context.name}")
    if context.target_role:
        parts.append(f"目标岗位: {context.target_role}")
    if context.resume:
        parts.append("已加载简历")
    if context.job_description:
        parts.append("已加载岗位 JD")
    if context.custom_notes:
        parts.append("已加载补充笔记")
    return {"summary": " | ".join(parts) if parts else "未加载上下文"}


@app.websocket("/ws/realtime/interview/{speaker}")
async def realtime_interview_stream(websocket: WebSocket, speaker: str) -> None:
    try:
        await proxy_realtime_interview(websocket, speaker)
    except WebSocketDisconnect:
        return
    except OpenAIRealtimeError as exc:
        await websocket.send_json({"type": "error", "speaker": speaker, "detail": str(exc)})
        await websocket.close(code=1011)
    except Exception as exc:
        await websocket.send_json({"type": "error", "speaker": speaker, "detail": str(exc)})
        await websocket.close(code=1011)
