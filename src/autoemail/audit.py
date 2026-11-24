"""审计留痕：每条动作都要能回答"为什么"。"""

from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuditEvent

LLM_ACTIONS = (
    "llm.intent.shadow",
    "llm.intent.fallback",
    "llm.intent.classify",
    "llm.draft.completed",
    "llm.draft.rejected",
)


def _reason_value(reasons: list[str], prefix: str) -> str | None:
    for reason in reasons:
        if reason.startswith(prefix):
            return reason[len(prefix) :]
    return None


def llm_stats(session: Session) -> dict:
    """汇总影子模式一致率、意图兜底分布和草稿建议结果。"""
    events = session.scalars(select(AuditEvent).where(AuditEvent.action.in_(LLM_ACTIONS))).all()
    shadow_total = 0
    shadow_agree = 0
    pairs: Counter[tuple[str, str]] = Counter()
    fallback: Counter[str] = Counter()
    draft: Counter[str] = Counter()

    for event in events:
        reasons = event.reasons or []
        if event.action in ("llm.intent.shadow", "llm.intent.classify"):
            deterministic = _reason_value(reasons, "deterministic=")
            llm_label = _reason_value(reasons, "llm=")
            if deterministic is None or llm_label is None:
                continue
            shadow_total += 1
            if deterministic == llm_label:
                shadow_agree += 1
            pairs[(deterministic, llm_label)] += 1
            if event.action == "llm.intent.classify":
                fallback[event.decision or "unknown"] += 1
        elif event.action == "llm.intent.fallback":
            fallback[event.decision or "unknown"] += 1
        else:
            draft[event.decision or "unknown"] += 1

    return {
        "shadow": {
            "total": shadow_total,
            "agree": shadow_agree,
            "disagree": shadow_total - shadow_agree,
            "agree_rate": round(shadow_agree / shadow_total, 4) if shadow_total else None,
            "pairs": [
                {
                    "deterministic": deterministic,
                    "llm": llm_label,
                    "count": count,
                    "agree": deterministic == llm_label,
                }
                for (deterministic, llm_label), count in pairs.most_common()
            ],
        },
        "fallback": {"total": sum(fallback.values()), "labels": dict(fallback)},
        "draft": {"total": sum(draft.values()), "decisions": dict(draft)},
    }


def record(
    session: Session,
    *,
    action: str,
    actor: str = "system",
    case_id: int | None = None,
    thread_id: int | None = None,
    message_id: int | None = None,
    draft_id: int | None = None,
    rule_ids: list[str] | None = None,
    decision: str | None = None,
    reasons: list[str] | None = None,
    model_id: str | None = None,
    input_hash: str | None = None,
    latency_ms: int | None = None,
    commit: bool = True,
) -> AuditEvent:
    event = AuditEvent(
        actor=actor,
        action=action,
        case_id=case_id,
        thread_id=thread_id,
        message_id=message_id,
        draft_id=draft_id,
        rule_ids=rule_ids or [],
        decision=decision,
        reasons=reasons or [],
        model_id=model_id,
        input_hash=input_hash,
        latency_ms=latency_ms,
    )
    session.add(event)
    if commit:
        session.commit()
    return event
