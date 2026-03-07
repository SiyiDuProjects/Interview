from __future__ import annotations

from dataclasses import dataclass

from app.models import AnswerVariant, CandidateContext, CoachRequest, TranscriptTurn
from app.services.knowledge_base import KEYWORD_TO_TOPIC, TOPIC_LIBRARY


FOLLOW_UP_MARKERS = [
    "为什么",
    "怎么",
    "如何",
    "区别",
    "展开",
    "具体",
    "why",
    "how",
    "difference",
    "tradeoff",
    "compare",
    "specific",
    "follow up",
]


@dataclass(frozen=True)
class CoachingPlan:
    topic: str
    question_type: str
    detected_follow_up: bool
    fast_answer: AnswerVariant
    local_deep_answer: AnswerVariant
    follow_up_angles: list[str]
    resume_hook: str | None
    context_summary: str
    confidence: float


def _detect_topic(turn: TranscriptTurn, history: list[TranscriptTurn]) -> str:
    text = turn.text.lower()
    for keyword, topic_key in KEYWORD_TO_TOPIC.items():
        if keyword in text:
            return topic_key

    for previous_turn in reversed(history):
        prev_text = previous_turn.text.lower()
        for keyword, topic_key in KEYWORD_TO_TOPIC.items():
            if keyword in prev_text:
                return topic_key

    return "project_experience"


def _detect_question_type(turn: TranscriptTurn) -> str:
    text = turn.text.lower()
    if any(token in text for token in ["项目", "负责", "难点", "project", "ownership", "responsible", "challenge"]):
        return "项目题"
    if any(token in text for token in ["算法", "复杂度", "algorithm", "complexity", "leetcode"]):
        return "算法题"
    if any(token in text for token in ["自我介绍", "介绍一下", "introduce yourself", "self intro", "background"]):
        return "行为题"
    return "基础题"


def _is_follow_up(turn: TranscriptTurn, history: list[TranscriptTurn]) -> bool:
    if not history:
        return False
    text = turn.text.lower()
    return any(marker in text for marker in FOLLOW_UP_MARKERS)


def _resume_hook(topic: str, context: CandidateContext) -> str | None:
    if not context.resume and not context.job_description and not context.custom_notes:
        return None

    role = context.target_role.strip() or "目标岗位"
    if topic == "SQL 索引":
        return f"把回答和你在 {role} 相关的 SQL 优化经历挂钩。"
    if topic == "Redis 持久化":
        return f"补一句和 {role} 相关的缓存可靠性取舍。"
    return f"把回答落到你简历里最匹配 {role} 的项目经历上。"


def _context_summary(context: CandidateContext) -> str:
    parts: list[str] = []
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
    return " | ".join(parts) if parts else "未加载候选人上下文"


def _build_answers(topic_key: str, detected_follow_up: bool) -> tuple[AnswerVariant, AnswerVariant, list[str], float]:
    template = TOPIC_LIBRARY[topic_key]

    fast_short_answer = template.short_answer
    fast_points = template.talking_points[:2]
    deep_short_answer = (
        f"针对 {template.topic}，先给定义，再讲实现原理、取舍和使用场景。"
    )
    deep_points = list(template.talking_points)

    if detected_follow_up:
        fast_short_answer = "这是追问，先正面回答追问点，再补一层更深的技术细节。"
        fast_points = [
            f"保持在当前主题 {template.topic} 上继续展开。",
            "不要从头重讲，直接接着上一句往下补。",
        ]
        deep_short_answer = (
            f"这个追问仍然围绕 {template.topic}，继续往机制、对比或取舍深入，并给一个例子。"
        )
        deep_points.insert(0, "以上一句回答为上下文，只补缺失的关键细节。")

    return (
        AnswerVariant(
            label="快速回答",
            short_answer=fast_short_answer,
            talking_points=fast_points,
            source="本地知识库",
            ready=True,
        ),
        AnswerVariant(
            label="详细回答",
            short_answer=deep_short_answer,
            talking_points=deep_points,
            source="本地知识库",
            ready=True,
        ),
        list(template.follow_up_angles),
        0.82 if topic_key != "project_experience" else 0.68,
    )


def build_coaching_plan(request: CoachRequest) -> CoachingPlan:
    topic_key = _detect_topic(request.turn, request.history)
    template = TOPIC_LIBRARY[topic_key]
    detected_follow_up = _is_follow_up(request.turn, request.history)
    question_type = _detect_question_type(request.turn)
    fast_answer, local_deep_answer, follow_up_angles, confidence = _build_answers(topic_key, detected_follow_up)

    return CoachingPlan(
        topic=template.topic,
        question_type=question_type,
        detected_follow_up=detected_follow_up,
        fast_answer=fast_answer,
        local_deep_answer=local_deep_answer,
        follow_up_angles=follow_up_angles,
        resume_hook=_resume_hook(template.topic, request.context),
        context_summary=_context_summary(request.context),
        confidence=confidence,
    )


def build_pending_deep_answer() -> AnswerVariant:
    return AnswerVariant(
        label="详细回答",
        short_answer="正在后台生成更完整的回答...",
        talking_points=[
            "先用上面的快速回答应对。",
            "更完整的细节会很快继续流入当前卡片。",
        ],
        source="OpenAI 后台任务",
        ready=False,
    )
