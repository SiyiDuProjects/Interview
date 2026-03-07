from __future__ import annotations

import json
import threading
import time
import urllib.request
import uuid
from typing import Any, Iterator

from app.config import get_settings
from app.models import AnswerVariant, CoachRequest, DetailJobStatus
from app.services.interview_coach import CoachingPlan


FAST_MODEL_FALLBACKS = ["gpt-4.1-mini", "gpt-4.1-nano", "gpt-4o-mini"]
READY_DETAIL_JOB_TTL_SECONDS = 600
MAX_DETAIL_JOB_AGE_SECONDS = 3600
MAX_DETAIL_JOB_COUNT = 128

_detail_jobs: dict[str, DetailJobStatus] = {}
_detail_job_conditions: dict[str, threading.Condition] = {}
_detail_job_updated_at: dict[str, float] = {}
_detail_jobs_lock = threading.Lock()


def detail_pipeline_enabled(request: CoachRequest) -> bool:
    settings = get_settings()
    return bool(settings.openai_api_key)


def resolve_fast_answer(request: CoachRequest, plan: CoachingPlan) -> AnswerVariant:
    settings = get_settings()
    if not settings.openai_api_key:
        return _build_unavailable_answer(
            label="快速回答",
            short_answer="未检测到可用的大模型配置，当前无法生成快速回答。",
            source="AI 不可用",
        )

    try:
        return _generate_openai_fast_answer(request, plan)
    except Exception as exc:
        return _build_failure_answer(
            label="快速回答",
            short_answer="AI 快速回答生成失败，请稍后重试。",
            source="AI 失败",
            error=str(exc),
        )


def start_detail_job(request: CoachRequest, plan: CoachingPlan) -> str:
    _cleanup_detail_jobs()
    job_id = str(uuid.uuid4())
    with _detail_jobs_lock:
        _detail_jobs[job_id] = DetailJobStatus(job_id=job_id, ready=False, version=0)
        _detail_job_conditions[job_id] = threading.Condition()
        _detail_job_updated_at[job_id] = time.time()

    worker = threading.Thread(
        target=_run_detail_job,
        args=(job_id, request, plan),
        daemon=True,
    )
    worker.start()
    return job_id


def get_detail_job_status(job_id: str) -> DetailJobStatus:
    _cleanup_detail_jobs()
    with _detail_jobs_lock:
        return _detail_jobs.get(job_id, DetailJobStatus(job_id=job_id, ready=False, error="Job not found"))


def stream_detail_events(job_id: str) -> Iterator[str]:
    last_version = -1
    while True:
        _cleanup_detail_jobs()
        with _detail_jobs_lock:
            condition = _detail_job_conditions.get(job_id)
            if condition is None:
                status = DetailJobStatus(job_id=job_id, ready=True, error="Job not found", version=0)
                yield _encode_sse(status)
                return

        with condition:
            condition.wait_for(lambda: get_detail_job_status(job_id).version != last_version, timeout=20)

        status = get_detail_job_status(job_id)
        if status.version == last_version:
            yield ": keep-alive\n\n"
            if status.ready:
                return
            continue

        last_version = status.version
        yield _encode_sse(status)
        if status.ready:
            return


def _encode_sse(status: DetailJobStatus) -> str:
    return f"data: {json.dumps(status.model_dump(), ensure_ascii=False)}\n\n"


def _set_detail_job_status(job_id: str, *, ready: bool, answer: AnswerVariant | None = None, error: str | None = None) -> None:
    _cleanup_detail_jobs()
    with _detail_jobs_lock:
        current = _detail_jobs.get(job_id)
        if current is None:
            return
        status = DetailJobStatus(
            job_id=job_id,
            ready=ready,
            version=current.version + 1,
            answer=answer,
            error=error,
        )
        _detail_jobs[job_id] = status
        condition = _detail_job_conditions.setdefault(job_id, threading.Condition())
        _detail_job_updated_at[job_id] = time.time()

    with condition:
        condition.notify_all()


def _run_detail_job(job_id: str, request: CoachRequest, plan: CoachingPlan) -> None:
    try:
        answer = _generate_openai_detail_answer(request, plan)
        _publish_streamed_answer(job_id, answer)
    except Exception as exc:
        fallback = _build_failure_answer(
            label="详细回答",
            short_answer="AI 详细回答生成失败，请稍后重试。",
            source="AI 失败",
            error=str(exc),
        )
        _set_detail_job_status(job_id, ready=True, answer=fallback, error=str(exc))


def _publish_streamed_answer(job_id: str, answer: AnswerVariant) -> None:
    words = answer.short_answer.split()
    checkpoints = [max(1, len(words) // 3), max(1, (2 * len(words)) // 3), len(words)]
    seen = set()
    for checkpoint in checkpoints:
        if checkpoint in seen:
            continue
        seen.add(checkpoint)
        partial = AnswerVariant(
            label=answer.label,
            short_answer=" ".join(words[:checkpoint]),
            talking_points=[],
            source=answer.source,
            ready=False,
        )
        _set_detail_job_status(job_id, ready=False, answer=partial)
        time.sleep(0.12)

    partial_points: list[str] = []
    for point in answer.talking_points:
        partial_points.append(point)
        partial = AnswerVariant(
            label=answer.label,
            short_answer=answer.short_answer,
            talking_points=list(partial_points),
            source=answer.source,
            ready=False,
        )
        _set_detail_job_status(job_id, ready=False, answer=partial)
        time.sleep(0.08)

    final_answer = AnswerVariant(
        label=answer.label,
        short_answer=answer.short_answer,
        talking_points=answer.talking_points,
        source=answer.source,
        ready=True,
    )
    _set_detail_job_status(job_id, ready=True, answer=final_answer)


def _generate_openai_fast_answer(request: CoachRequest, plan: CoachingPlan) -> AnswerVariant:
    settings = get_settings()
    prompt = _build_fast_prompt(request, plan)
    candidate_models = [settings.openai_fast_model, settings.openai_model, *FAST_MODEL_FALLBACKS]
    tried: list[str] = []
    last_error: Exception | None = None

    for model in candidate_models:
        if not model or model in tried:
            continue
        tried.append(model)
        try:
            return _request_openai_structured_answer(
                model=model,
                prompt=prompt,
                base_url=settings.openai_base_url,
                api_key=settings.openai_api_key,
                timeout_seconds=settings.openai_timeout_seconds,
                label="快速回答",
                schema_name="fast_answer",
                min_items=2,
                max_items=3,
                max_output_tokens=360,
            )
        except Exception as exc:
            last_error = exc
            continue

    if last_error is None:
        raise ValueError("OpenAI fast generation failed without an explicit error")
    raise last_error


def _generate_openai_detail_answer(request: CoachRequest, plan: CoachingPlan) -> AnswerVariant:
    settings = get_settings()
    prompt = _build_detail_prompt(request, plan)
    candidate_models = [settings.openai_model, *FAST_MODEL_FALLBACKS]
    tried: list[str] = []
    last_error: Exception | None = None

    for model in candidate_models:
        if model in tried:
            continue
        tried.append(model)
        try:
            return _request_openai_structured_answer(
                model=model,
                prompt=prompt,
                base_url=settings.openai_base_url,
                api_key=settings.openai_api_key,
                timeout_seconds=settings.openai_timeout_seconds,
                label="详细回答",
                schema_name="detail_answer",
                min_items=3,
                max_items=5,
                max_output_tokens=500,
            )
        except Exception as exc:
            last_error = exc
            continue

    if last_error is None:
        raise ValueError("OpenAI detail generation failed without an explicit error")
    raise last_error


def _request_openai_structured_answer(
    *,
    model: str,
    prompt: str,
    base_url: str,
    api_key: str,
    timeout_seconds: float,
    label: str,
    schema_name: str,
    min_items: int,
    max_items: int,
    max_output_tokens: int,
) -> AnswerVariant:
    verbosity = "low" if model.startswith("gpt-5") else "medium"
    payload = {
        "model": model,
        "input": prompt,
        "text": {
            "verbosity": verbosity,
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "schema": {
                    "type": "object",
                    "properties": {
                        "short_answer": {"type": "string"},
                        "talking_points": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": min_items,
                            "maxItems": max_items,
                        },
                    },
                    "required": ["short_answer", "talking_points"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        },
        "max_output_tokens": max_output_tokens,
    }
    if model.startswith("gpt-5"):
        payload["reasoning"] = {"effort": "low"}
        payload["max_output_tokens"] = 1200

    http_request = urllib.request.Request(
        url=f"{base_url}/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(http_request, timeout=timeout_seconds) as response:
        data = json.loads(response.read().decode("utf-8"))

    if data.get("status") == "incomplete":
        raise ValueError(f"OpenAI response incomplete: {data.get('incomplete_details')}")

    output_text = _extract_output_text(data)
    parsed = _parse_detail_payload(output_text)
    return AnswerVariant(
        label=label,
        short_answer=parsed["short_answer"],
        talking_points=parsed["talking_points"][:max_items],
        source=f"OpenAI {model}",
        ready=True,
    )


def _build_fast_prompt(request: CoachRequest, plan: CoachingPlan) -> str:
    history_lines = [f"{turn.speaker}: {turn.text}" for turn in request.history[-4:]]
    history_block = "\n".join(history_lines) if history_lines else "无历史对话"
    return (
        "你是中文模拟面试助手。\n"
        "无论问题、上下文或简历内容是中文还是英文，输出都必须只使用简体中文。\n"
        "不要输出英文句子，不要中英混写，technical term 也优先翻成中文；只有在必须保留的专有名词场景下才保留英文缩写。\n"
        "返回 JSON，包含 short_answer 和 talking_points。\n"
        "short_answer 必须适合候选人直接口头复述，控制在 80 字以内；talking_points 给 2 到 3 条，且每条都必须是简体中文。\n"
        f"问题: {request.turn.text}\n"
        f"主题: {plan.topic}\n"
        f"问题类型: {plan.question_type}\n"
        f"是否追问: {plan.detected_follow_up}\n"
        f"最近上下文:\n{history_block}\n"
    )


def _build_detail_prompt(request: CoachRequest, plan: CoachingPlan) -> str:
    history_lines = [f"{turn.speaker}: {turn.text}" for turn in request.history[-8:]]
    history_block = "\n".join(history_lines) if history_lines else "无历史对话"
    resume_hook = plan.resume_hook or "无简历挂钩点"
    return (
        "你是中文模拟面试助手。\n"
        "无论问题、上下文或简历内容是中文还是英文，输出都必须只使用简体中文。\n"
        "不要输出英文句子，不要中英混写，technical term 也优先翻成中文；只有在必须保留的专有名词场景下才保留英文缩写。\n"
        "返回 JSON，包含 short_answer 和 talking_points。\n"
        "short_answer 要写成候选人可以直接口头表达的完整简体中文回答；talking_points 给 3 到 5 条，覆盖原理、取舍、场景，且每条都必须是简体中文。\n"
        f"问题: {request.turn.text}\n"
        f"主题: {plan.topic}\n"
        f"问题类型: {plan.question_type}\n"
        f"是否追问: {plan.detected_follow_up}\n"
        f"快速回答: {plan.fast_answer.short_answer}\n"
        f"可能追问: {', '.join(plan.follow_up_angles)}\n"
        f"简历挂钩点: {resume_hook}\n"
        f"最近对话:\n{history_block}\n"
    )


def _extract_output_text(payload: Any) -> str:
    text_value = payload.get("output_text") if isinstance(payload, dict) else None
    if isinstance(text_value, str) and text_value.strip():
        return text_value

    for item in payload.get("output", []) if isinstance(payload, dict) else []:
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and isinstance(content.get("text"), str):
                return content["text"]

    matches: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            text = node.get("text")
            if isinstance(text, str) and text.strip().startswith("{"):
                matches.append(text)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    if not matches:
        raise ValueError("OpenAI response did not include output text")
    return max(matches, key=len)


def _parse_detail_payload(output_text: str) -> dict[str, Any]:
    parsed = json.loads(output_text)
    short_answer = str(parsed.get("short_answer", "")).strip()
    talking_points = parsed.get("talking_points", [])
    if not short_answer:
        raise ValueError("Detailed answer missing short_answer")
    if not isinstance(talking_points, list) or not talking_points:
        raise ValueError("Detailed answer missing talking_points")
    return {
        "short_answer": short_answer,
        "talking_points": [str(item).strip() for item in talking_points if str(item).strip()][:5],
    }


def _build_unavailable_answer(*, label: str, short_answer: str, source: str) -> AnswerVariant:
    return AnswerVariant(
        label=label,
        short_answer=short_answer,
        talking_points=[
            "请先配置可用的 OPENAI_API_KEY。",
            "确认后端网络访问正常后再重试。",
        ],
        source=source,
        ready=True,
    )


def _build_failure_answer(*, label: str, short_answer: str, source: str, error: str) -> AnswerVariant:
    return AnswerVariant(
        label=label,
        short_answer=short_answer,
        talking_points=[
            "稍后可以直接重试当前问题。",
            f"错误信息: {error[:160]}",
        ],
        source=source,
        ready=True,
    )


def _cleanup_detail_jobs(now: float | None = None) -> None:
    current_time = now if now is not None else time.time()
    stale_job_ids: set[str] = set()

    with _detail_jobs_lock:
        for job_id, status in _detail_jobs.items():
            updated_at = _detail_job_updated_at.get(job_id, current_time)
            age_seconds = current_time - updated_at
            if age_seconds > MAX_DETAIL_JOB_AGE_SECONDS:
                stale_job_ids.add(job_id)
                continue
            if status.ready and age_seconds > READY_DETAIL_JOB_TTL_SECONDS:
                stale_job_ids.add(job_id)

        ready_jobs = sorted(
            (
                (_detail_job_updated_at.get(job_id, current_time), job_id)
                for job_id, status in _detail_jobs.items()
                if status.ready and job_id not in stale_job_ids
            )
        )
        overflow = max(0, len(_detail_jobs) - len(stale_job_ids) - MAX_DETAIL_JOB_COUNT)
        for _, job_id in ready_jobs[:overflow]:
            stale_job_ids.add(job_id)

        conditions_to_notify = [
            _detail_job_conditions.pop(job_id, None)
            for job_id in stale_job_ids
            if _detail_jobs.pop(job_id, None) is not None
        ]
        for job_id in stale_job_ids:
            _detail_job_updated_at.pop(job_id, None)

    for condition in conditions_to_notify:
        if condition is None:
            continue
        with condition:
            condition.notify_all()
