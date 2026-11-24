"""Case 状态迁移与代理商回复映射。"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit
from .models import Case, TERMINAL_STATES

STATE_BY_INTENT = {
    "quote_reply": "quoted",
    "form_request": "materials_requested",
    "bond_delivered": "bond_received",
    "invoice": "invoiced",
}

ALLOWED_TRANSITIONS = {
    "init": frozenset({"quoted_out", "blocked", "closed", "lost"}),
    "quoted_out": frozenset({"quoted", "materials_requested", "bond_received", "invoiced", "blocked", "closed", "lost"}),
    "quoted": frozenset({"materials_requested", "bond_received", "invoiced", "blocked", "closed", "lost"}),
    "materials_requested": frozenset({"quoted", "bond_received", "invoiced", "blocked", "closed", "lost"}),
    "bond_received": frozenset({"invoiced", "closed", "lost"}),
    "invoiced": frozenset({"paid", "closed", "lost"}),
    "paid": frozenset({"closed", "lost"}),
    "blocked": frozenset({"init", "quoted_out", "quoted", "materials_requested", "closed", "lost"}),
}

AUTO_REPLY_INTENTS = frozenset({"quote_reply", "form_request", "bond_delivered"})

AUTO_MODES = ("off", "dry_run", "on")


def set_auto_mode(session: Session, case: Case, mode: str, *, actor: str = "system") -> bool:
    """切换 case 自动模式并留痕；返回是否实际发生变化。"""
    if mode not in AUTO_MODES:
        raise ValueError(f"未知自动模式：{mode}")
    if case.auto_mode == mode:
        return False
    before = case.auto_mode
    case.auto_mode = mode
    session.commit()
    audit.record(
        session,
        action="case.auto_mode_changed",
        case_id=case.id,
        actor=actor,
        decision=mode,
        reasons=[f"{before} → {mode}"],
    )
    return True


def utcnow():
    return datetime.now(timezone.utc)


def can_transition(old_state: str, new_state: str) -> bool:
    return old_state != new_state and new_state in ALLOWED_TRANSITIONS.get(old_state, frozenset())


def state_allows_auto_reply(case_state: str, intent: str) -> bool:
    return case_state not in TERMINAL_STATES and intent in AUTO_REPLY_INTENTS


def transition_case(session: Session, case: Case, new_state: str, *, reason: str, message_id: int | None = None) -> bool:
    if case.state == new_state:
        return False
    if case.state in TERMINAL_STATES:
        return False
    if not can_transition(case.state, new_state):
        raise ValueError(f"非法 Case 状态迁移：{case.state} → {new_state}")
    old_state = case.state
    case.state = new_state
    case.state_entered_at = utcnow()
    session.flush()
    audit.record(session, action="case.state_changed", case_id=case.id, message_id=message_id, decision=new_state, reasons=[f"{old_state} → {new_state}: {reason}"])
    if new_state in TERMINAL_STATES:
        _cleanup_commission_if_done(session, case.commission_id)
    return True


def _cleanup_commission_if_done(session: Session, commission_id: int) -> None:
    """委托所有 case 都结束（closed/lost）后，清理该委托的结构化客户需求字段与原始文件。"""
    active = session.scalar(
        select(Case.id).where(
            Case.commission_id == commission_id,
            Case.state.notin_(TERMINAL_STATES),
        )
    )
    if active is not None:
        return  # 还有其他活跃 case，暂不清理
    from .formfill import purge_commission_data

    deleted = purge_commission_data(session, commission_id)
    audit.record(
        session,
        action="commission.data_purged",
        decision=f"deleted_{sum(deleted.values())}",
        reasons=[
            f"委托 {commission_id} 所有 case 已结束，清理结构化客户需求字段与原始文件：{deleted}"
        ],
    )


def state_for_intent(intent: str) -> str | None:
    return STATE_BY_INTENT.get(intent)
