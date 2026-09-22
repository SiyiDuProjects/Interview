from __future__ import annotations

import uuid
from typing import Any


async def run_ui_operation(runtime: Any, websocket: Any, payload: dict[str, Any], operation_id: str) -> None:
    # Lazy import keeps the protocol handlers separate without a runtime cycle.
    from app.services.openai_realtime import (
        OpenAIRealtimeError, MAX_MANUAL_TEXT_CHARS, QUICK_ANSWER_ACTIONS,
        _send_user_text, _request_current_screen, _record_screen,
    )
    if runtime.closed or not runtime.active:
        raise OpenAIRealtimeError("Interview is not active.")
    kind = payload["type"]
    action = str(payload.get("action") or "")
    if kind == "code_action":
        from app.services.code_workspace import run_code_operation
        await run_code_operation(runtime, payload, operation_id)
        return
    if kind == "request_screen_capture" and payload.get("collect_only") is True:
        # Capture is independent of answer generation and speech turn revisions.
        await runtime.operation_status(operation_id, "running", detail="正在收集截图。")
        runtime.metrics["tool_calls"] += 1
        try:
            request_id, image_url = await _request_current_screen(runtime, reason="Collect another page of the question or current code; do not answer yet.")
        except Exception:
            runtime.metrics["tool_failures"] += 1
            raise
        if runtime.closed or not runtime.active:
            return
        # Record first: a transient model outage must not lose a captured page.
        await _record_screen(runtime, None, request_id, image_url, str(payload.get("question_id") or runtime.current_question_id))
        runtime.collected_screens.append(request_id)
        await runtime.broadcast_to_clients(runtime.screen_collection_state())
        await runtime.operation_status(operation_id, "completed", detail=f"已收集 {len(runtime.collected_screens)} 张，可继续截图或开始解题。")
        return
    if kind in {"clear_screens", "answer_screens"}:
        selected = payload.get("request_ids")
        if (not isinstance(selected, list) or not selected
                or any(not isinstance(item, str) or item not in runtime.collected_screens for item in selected)
                or len(set(selected)) != len(selected)):
            raise OpenAIRealtimeError("截图选择已改变，请核对后重试。")
        if kind == "clear_screens":
            runtime.collected_screens = [item for item in runtime.collected_screens if item not in selected]
            await runtime.broadcast_to_clients(runtime.screen_collection_state())
            await runtime.operation_status(operation_id, "completed", detail="已结束本组截图；历史材料仍保留。")
            return
        # Each click freezes its page set; later captures remain in the tray.
        question_id = f"screen-question-{uuid.uuid4()}"
        from app.services.realtime_history import observed_at
        selection = {"kind": "screen_question", "question_id": question_id, "request_ids": list(selected),
                     "text": f"截图题目（{len(selected)} 张）", "created_at": observed_at(),
                     "meaning": "UI-selected screenshot question; label is not an interviewer transcript."}
        runtime.history.entries.append(selection)
        runtime.history.by_id[question_id] = selection
        revision = await runtime.invalidate_work(question_id=question_id, except_operation=operation_id)
        await runtime.broadcast_to_clients(runtime.question_state())
        upstream = await runtime.ensure_main()
        from app.services.openai_realtime import _send_image_item
        for page_number, request_id in enumerate(selected, 1):
            entry = runtime.history.by_id[f"screen:{request_id}"]
            if not runtime.work_is_current(revision, upstream):
                await runtime.operation_status(operation_id, "cancelled", detail="有新问题，请重试截图解题。")
                return
            await _send_image_item(upstream, image_url=entry["image_url"], prompt=f"Selected question page {page_number}/{len(selected)}; request_id={request_id}; use the pages together.")
        started = await runtime.request_response(
            revision=revision, question_id=question_id, operation_id=operation_id, allow_held=True,
            instructions="Answer the problem in the explicitly selected screenshot pages together. Use the complete interview context and latest corrections. Ask for missing pages if incomplete.",
            target_context={"question_id": question_id, "selected_screenshot_ids": selected},
        )
        if not started:
            await runtime.operation_status(operation_id, "cancelled", detail="请在当前发言结束后重试，截图已保留。")
        return
    if kind == "set_answer_hold":
        if not isinstance(payload.get("hold"), bool):
            raise OpenAIRealtimeError("hold must be a boolean.")
        runtime.hold_answers = payload["hold"]
        revision = await runtime.invalidate_work(except_operation=operation_id)
        if runtime.hold_answers and runtime.main_upstream is not None:
            await runtime.cancel_response(runtime.main_upstream)
        await runtime.broadcast_to_clients(runtime.question_state())
        if not runtime.hold_answers and runtime.current_question_id:
            if await runtime.request_response(revision=revision, question_id=runtime.current_question_id, operation_id=operation_id):
                return
        elif not runtime.hold_answers and runtime.main_upstream is not None:
            from app.services.live_session import append_context
            await append_context(runtime.main_upstream, "Automatic answers are enabled. Answer new interviewer questions normally.", instruction=True)
        await runtime.operation_status(operation_id, "completed", detail="Answers paused; context collection continues." if runtime.hold_answers else "Automatic answers resumed.")
        return
    text = str(payload.get("text") or "").strip()
    manual_kind = payload.get("kind", "question")
    if kind == "manual_text" and (not text or len(text) > MAX_MANUAL_TEXT_CHARS or manual_kind not in {"question", "correction", "candidate_context"}):
        raise OpenAIRealtimeError("Invalid manual text or input kind.")
    if kind == "quick_answer" and action not in {*QUICK_ANSWER_ACTIONS, "deep"}:
        raise OpenAIRealtimeError("Unknown answer action.")
    if kind == "manual_text" and manual_kind == "candidate_context":
        await runtime.emit_transcript_final("candidate", text)
        await runtime.append_candidate_context(f"[Candidate supplied context; do not answer] {text}")
        await runtime.operation_status(operation_id, "completed", detail="Candidate context added.")
        return
    response_id = str(payload.get("response_id") or "")
    question_id = str(payload.get("question_id") or "")
    if response_id:
        if response_id not in runtime.response_buffers or not runtime.response_buffers[response_id]:
            raise OpenAIRealtimeError("The selected answer is unavailable.")
        selected_question = runtime._response_metadata.get(response_id, {}).get("question_id", "")
        if question_id and selected_question and question_id != selected_question:
            raise OpenAIRealtimeError("The answer and question selection do not match.")
        question_id = selected_question or question_id
    question = runtime.question_text(question_id)
    question_id = question_id or runtime.current_question_id
    if kind == "quick_answer" and not question and not response_id:
        raise OpenAIRealtimeError("请先输入问题，或等待面试官提问后再试。")
    if kind == "manual_text":
        if manual_kind == "question":
            question_id = f"question-{uuid.uuid4()}"
        elif not question_id:
            raise OpenAIRealtimeError("Select the question to correct.")
        question = text
    revision = await runtime.invalidate_work(question_id=question_id, except_operation=operation_id)
    await runtime.operation_status(operation_id, "running", question_id=question_id)
    if kind == "manual_text":
        corrects_id = str(payload.get("turn_id") or "") if manual_kind == "correction" else ""
        if manual_kind == "correction" and not corrects_id:
            corrects_id = next((turn["turn_id"] for turn in reversed(runtime.history.turns)
                               if turn.get("question_id") == question_id and turn["speaker"] == "interviewer"), "")
        if corrects_id and corrects_id not in runtime.history.by_id:
            raise OpenAIRealtimeError("The selected transcript is unavailable.")
        # Collection remains available while answer generation is paused.
        await runtime.emit_transcript_final("interviewer", text, question_id=question_id, corrects_turn_id=corrects_id)
        upstream = await runtime.ensure_main()
        label = f"Correction to question {question_id}; replaces the misheard wording" if manual_kind == "correction" else f"Interviewer question {question_id}"
        await _send_user_text(upstream, f"[{label}] {text}")
        if runtime.hold_answers:
            await runtime.operation_status(operation_id, "completed", detail="Question context saved; answers remain paused until the current speech ends or you resume.")
            return
    async with runtime._response_lock:
        upstream = await runtime.ensure_main() if kind == "request_screen_capture" and runtime.hold_answers else await runtime.response_slot(revision)
        if upstream is None:
            await runtime.operation_status(operation_id, "cancelled", detail="Answers are paused or a newer question is in progress.")
            return
    if kind == "request_screen_capture":
        await runtime.operation_status(operation_id, "running", detail="Capturing the selected screen.")
        runtime.metrics["tool_calls"] += 1
        try:
            request_id, image_url = await _request_current_screen(runtime, reason="Capture the selected question, whiteboard, or code screen.")
        except Exception:
            runtime.metrics["tool_failures"] += 1
            raise
        if not runtime.work_is_current(revision, upstream):
            await runtime.operation_status(operation_id, "cancelled", detail="A newer question superseded this capture.")
            return
        await _record_screen(runtime, upstream, request_id, image_url, question_id)
        if runtime.hold_answers:
            await runtime.operation_status(operation_id, "completed", detail="Screen context saved; answers remain paused.")
            return
    instructions = QUICK_ANSWER_ACTIONS.get(action, "Answer the selected question using all current context and the latest corrections.")
    target_context = {"operation_id": operation_id, "question_id": question_id, "question": question} if kind == "quick_answer" else None
    if response_id:
        assert target_context is not None
        target_context.update(response_id=response_id, assistant_draft=runtime.response_buffers[response_id])
    if not await runtime.request_response(revision=revision, question_id=question_id, instructions=instructions, operation_id=operation_id, target_context=target_context):
        await runtime.operation_status(operation_id, "cancelled", detail="A newer question or pause superseded this action.")
