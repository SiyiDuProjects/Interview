from __future__ import annotations

import threading

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from app.models import AnswerVariant, CandidateContext, CoachRequest, CoachResponse, DetailJobStatus
from app.services.interview_coach import build_coaching_plan, build_pending_deep_answer
from app.services.deepgram_realtime import DeepgramRealtimeError, proxy_live_transcription
from app.services.openai_service import (
    detail_pipeline_enabled,
    get_detail_job_status,
    resolve_fast_answers,
    start_detail_job,
    stream_detail_events,
)
from app.services.openai_realtime import OpenAIRealtimeError, proxy_realtime_interview
from app.services.xfyun_realtime import XfyunRealtimeError, proxy_xfyun_live_transcription
from app.services.transcription_service import (
    TranscriptionError,
    get_transcription_source,
    transcribe_audio_chunk,
    warmup_transcription_model,
)
from app.config import get_settings


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


@app.on_event("startup")
def warmup_local_services() -> None:
    worker = threading.Thread(target=_warmup_transcription_worker, daemon=True)
    worker.start()


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
    }


def _warmup_transcription_worker() -> None:
    try:
        source = warmup_transcription_model()
        print(f"[server] transcription warmup ready: {source}", flush=True)
    except Exception as exc:
        print(f"[server] transcription warmup failed: {exc}", flush=True)


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


@app.post("/api/transcribe/chunk")
async def transcribe_chunk(
    file: UploadFile = File(...),
    speaker: str = Form(...),
) -> dict[str, str]:
    if speaker not in {"interviewer", "candidate"}:
        raise HTTPException(status_code=400, detail="speaker must be interviewer or candidate")

    audio_bytes = await file.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="audio chunk is empty")

    try:
        text = await transcribe_audio_chunk(
            audio_bytes=audio_bytes,
            filename=file.filename or "chunk.webm",
            content_type=file.content_type or "audio/webm",
            speaker=speaker,
        )
    except TranscriptionError as exc:
        print(
            f"[server] transcription error speaker={speaker} bytes={len(audio_bytes)} detail={exc}",
            flush=True,
        )
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    print(
        f"[server] transcription ok speaker={speaker} bytes={len(audio_bytes)} text={text!r}",
        flush=True,
    )

    return {
        "speaker": speaker,
        "text": text,
        "source": get_transcription_source(),
    }


@app.websocket("/ws/transcribe/{speaker}")
async def transcribe_stream(websocket: WebSocket, speaker: str) -> None:
    if speaker not in {"interviewer", "candidate"}:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    locale = websocket.query_params.get("locale", "zh").lower()
    try:
        if locale.startswith("zh"):
            await proxy_xfyun_live_transcription(websocket, speaker)
        else:
            await proxy_live_transcription(websocket, speaker, "en")
    except WebSocketDisconnect:
        return
    except XfyunRealtimeError as exc:
        await websocket.send_json({"type": "error", "speaker": speaker, "detail": str(exc)})
        await websocket.close(code=1011)
    except DeepgramRealtimeError as exc:
        await websocket.send_json({"type": "error", "speaker": speaker, "detail": str(exc)})
        await websocket.close(code=1011)
    except Exception as exc:
        await websocket.send_json({"type": "error", "speaker": speaker, "detail": str(exc)})
        await websocket.close(code=1011)


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


@app.post("/api/coach/respond", response_model=CoachResponse)
def coach_respond(payload: CoachRequest) -> CoachResponse:
    plan = build_coaching_plan(payload)
    fast_answer, fast_answer_alternatives = resolve_fast_answers(payload, plan)
    deep_answer = AnswerVariant(
        label="详细回答",
        short_answer="未检测到可用的大模型配置，当前无法生成详细回答。",
        talking_points=[
            "请检查 OPENAI_API_KEY 是否已配置。",
            "确认后端能访问 OpenAI 接口后再重试。",
        ],
        source="AI 不可用",
        ready=True,
    )
    detail_job_id = None

    if detail_pipeline_enabled(payload):
        detail_job_id = start_detail_job(payload, plan)
        deep_answer = build_pending_deep_answer()

    return CoachResponse(
        topic=plan.topic,
        question_type=plan.question_type,
        detected_follow_up=plan.detected_follow_up,
        fast_answer=fast_answer,
        fast_answer_alternatives=fast_answer_alternatives,
        deep_answer=deep_answer,
        follow_up_angles=plan.follow_up_angles,
        resume_hook=plan.resume_hook,
        context_summary=plan.context_summary,
        confidence=plan.confidence,
        detail_job_id=detail_job_id,
    )


@app.get("/api/coach/detail/{job_id}", response_model=DetailJobStatus)
def coach_detail(job_id: str) -> DetailJobStatus:
    return get_detail_job_status(job_id)


@app.get("/api/coach/detail-stream/{job_id}")
def coach_detail_stream(job_id: str) -> StreamingResponse:
    return StreamingResponse(
        stream_detail_events(job_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
