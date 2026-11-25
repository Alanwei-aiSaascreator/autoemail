"""重新处理入站邮件：分类器修复后，让误处理的入站邮件重跑管线。

一次重跑会：作废旧草稿 → 回滚 case 状态 → 清理旧意图产生的 Quote/Invoice →
用原始 .eml（或退回 DB 字段）重建 NormalizedMessage → 复用入站管线重新出意图与草稿。
全程强制 auto_send=False（绝不自动发送）、notify=False（不重复推送群通知）。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit
from .channels.base import NormalizedMessage
from .ingest.parse import parse_email_bytes
from .models import (
    AuditEvent,
    Case,
    Commission,
    Draft,
    DRAFT_BLOCKED,
    DRAFT_FAILED,
    DRAFT_PENDING,
    DRAFT_REJECTED,
    DRAFT_SENT,
    INVOICE_PAID,
    Invoice,
    Message,
    Quote,
    Thread,
    utcnow,
)

# 重跑前允许作废的草稿状态（与人工可拒绝的草稿状态一致；sent/rejected 不动）
_REVERTABLE_DRAFT_STATES = (DRAFT_PENDING, DRAFT_BLOCKED, DRAFT_FAILED)


def _attach_case_fillings(session: Session, case: Case, draft: Draft) -> list[str]:
    """把该 case 的填表产物（filled_forms 里 case_no 或公司名前缀的 PDF）附到草稿。

    用于重新处理历史邮件：原始 .eml 未存档时拿不到空白表格，但填表产物已存在，
    直接附上已填好（含签名）的产物，让草稿可以随回复发出。返回附上的文件路径列表。
    """
    from .config import settings

    filled_dir = Path(settings.filled_forms_dir)
    if not filled_dir.is_dir():
        return []
    commission = session.get(Commission, case.commission_id)
    patterns = [f"{case.case_no}_*"]
    if commission is not None and commission.legal_name:
        patterns.append(f"{commission.legal_name}_*")
    attached: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for item in sorted(filled_dir.glob(pattern)):
            if not item.is_file() or item.name in seen:
                continue
            seen.add(item.name)
            attached.append(str(item))
    if not attached:
        return []
    existing = list(draft.attachments or [])
    for p in attached:
        if p not in existing:
            existing.append(p)
    draft.attachments = existing
    return attached


def rebuild_message(session: Session, stored: Message) -> tuple[NormalizedMessage, str]:
    """重建 NormalizedMessage：优先解析原始 .eml；文件不可用时退回 DB 字段（无附件）。

    返回 (消息, 原始来源)：原始来源为 "eml" 或 "db"。
    """
    raw_path = (stored.raw_path or "").strip()
    if raw_path:
        try:
            raw = Path(raw_path).read_bytes()
        except OSError:
            raw = b""
        if raw:
            return parse_email_bytes(raw, channel=stored.channel or "eml"), "eml"
    message = NormalizedMessage(
        channel=stored.channel or "email",
        channel_msg_id=stored.channel_msg_id,
        from_addr=stored.from_addr,
        subject=stored.subject,
        body_text=stored.body_text,
        sent_at=stored.sent_at,
        in_reply_to=stored.in_reply_to,
        references=list(stored.references or []),
        to_addrs=list(stored.to_addrs or []),
        cc_addrs=list(stored.cc_addrs or []),
        dedupe_hash=stored.dedupe_hash,
    )
    return message, "db"


def _latest_intent_label(session: Session, message_id: int) -> str:
    """取该消息最近一次分类出的意图（以审计为准，退回旧草稿的意图）。"""
    event = session.scalar(
        select(AuditEvent)
        .where(AuditEvent.action == "intent.classify", AuditEvent.message_id == message_id)
        .order_by(AuditEvent.ts.desc(), AuditEvent.id.desc())
        .limit(1)
    )
    if event is not None and event.decision:
        return event.decision
    draft = session.scalar(
        select(Draft)
        .where(Draft.source_message_id == message_id)
        .order_by(Draft.id.desc())
        .limit(1)
    )
    return draft.intent if draft is not None else "unknown"


def _old_state_from_reasons(reasons: list[str]) -> str | None:
    """从 case.state_changed 审计原因「{旧} → {新}: {理由}」解析出旧状态。"""
    for reason in reasons or []:
        head = reason.split(":", 1)[0]
        if " → " in head:
            return head.split(" → ", 1)[0].strip()
    return None


def _reject_old_drafts(session: Session, message_id: int, *, actor: str) -> list[int]:
    """把该消息产生的待审草稿作废（不发信），返回被作废的草稿 id 列表。"""
    drafts = session.scalars(
        select(Draft).where(
            Draft.source_message_id == message_id,
            Draft.status.in_(_REVERTABLE_DRAFT_STATES),
        )
    ).all()
    rejected_ids: list[int] = []
    for draft in drafts:
        draft.status = DRAFT_REJECTED
        draft.reviewer = actor
        draft.reviewed_at = utcnow()
        draft.updated_at = utcnow()
        session.flush()
        audit.record(
            session,
            action="draft.rejected",
            actor=actor,
            case_id=draft.case_id,
            thread_id=draft.thread_id,
            message_id=message_id,
            draft_id=draft.id,
            decision=DRAFT_REJECTED,
            reasons=["重新处理：原草稿作废"],
        )
        rejected_ids.append(draft.id)
    return rejected_ids


def _revert_case_state(session: Session, case: Case, message_id: int, *, actor: str) -> tuple[bool, str]:
    """回滚该消息推进过的 case 状态；状态已被后续邮件推进则跳过。

    返回 (是否回滚, 说明)。回滚是状态机之外的纠正动作，直接改 state 不走
    transition_case 的合法迁移校验（如 bond_received → quoted_out 本不合法）。
    """
    event = session.scalar(
        select(AuditEvent)
        .where(AuditEvent.action == "case.state_changed", AuditEvent.message_id == message_id)
        .order_by(AuditEvent.ts.desc(), AuditEvent.id.desc())
        .limit(1)
    )
    if event is None or not event.decision:
        return False, "该邮件未引起状态迁移，无需回滚"
    if case.state != event.decision:
        return False, f"状态已被后续邮件推进（当前 {case.state}），跳过回滚"
    old_state = _old_state_from_reasons(event.reasons or [])
    if old_state is None or old_state == case.state:
        return False, "审计原因中无法解析旧状态，跳过回滚"
    previous = case.state
    case.state = old_state
    case.state_entered_at = utcnow()
    session.flush()
    audit.record(
        session,
        action="case.state_reverted",
        actor=actor,
        case_id=case.id,
        thread_id=event.thread_id,
        message_id=message_id,
        decision=old_state,
        reasons=[f"{previous} → {old_state}: 重新处理回滚"],
    )
    return True, f"{previous} → {old_state}"


def _remove_intent_data(session: Session, message_id: int, *, actor: str) -> dict[str, int]:
    """清理旧意图产生的 Quote / Invoice（已付款发票在上游已拦截，不会走到这里）。"""
    removed = {"quote": 0, "invoice": 0}
    for quote in session.scalars(select(Quote).where(Quote.message_id == message_id)).all():
        session.delete(quote)
        removed["quote"] += 1
    if removed["quote"]:
        session.flush()
        audit.record(
            session,
            action="quote.removed",
            actor=actor,
            message_id=message_id,
            decision=str(removed["quote"]),
            reasons=["重新处理：删除该邮件产生的报价记录"],
        )
    for invoice in session.scalars(select(Invoice).where(Invoice.message_id == message_id)).all():
        session.delete(invoice)
        removed["invoice"] += 1
    if removed["invoice"]:
        session.flush()
        audit.record(
            session,
            action="invoice.removed",
            actor=actor,
            message_id=message_id,
            decision=str(removed["invoice"]),
            reasons=["重新处理：删除该邮件产生的发票记录"],
        )
    return removed


def reprocess_message(session: Session, message_id: int, *, actor: str = "system") -> dict:
    """重新处理一封入站邮件，返回摘要字典。

    前置校验（不满足直接 raise ValueError）：
    - 只能重新处理 direction=in 的消息；
    - 已发出过回复（存在 sent 草稿）的消息不能重跑；
    - 关联发票已付款的消息不能重跑。
    """
    stored = session.get(Message, message_id)
    if stored is None:
        raise ValueError(f"消息不存在：{message_id}")
    if stored.direction != "in":
        raise ValueError("只能重新处理入站邮件")

    sent_draft_id = session.scalar(
        select(Draft.id).where(
            Draft.source_message_id == message_id, Draft.status == DRAFT_SENT
        )
    )
    if sent_draft_id is not None:
        raise ValueError("该邮件已发出过回复，不能重新处理")

    paid_invoice_id = session.scalar(
        select(Invoice.id).where(
            Invoice.message_id == message_id, Invoice.status == INVOICE_PAID
        )
    )
    if paid_invoice_id is not None:
        raise ValueError("该邮件产生的发票已付款，不能重新处理")

    thread = session.get(Thread, stored.thread_id)
    case = session.get(Case, thread.case_id) if thread and thread.case_id else None
    old_intent = _latest_intent_label(session, message_id)
    notes: list[str] = []

    # 1) 作废旧草稿
    rejected_ids = _reject_old_drafts(session, message_id, actor=actor)
    session.commit()

    # 2) 回滚 case 状态
    state_reverted = False
    if case is not None:
        state_reverted, state_note = _revert_case_state(session, case, message_id, actor=actor)
        notes.append(f"状态回滚：{state_note}")
        session.commit()

    # 3) 清理旧意图产生的数据
    removed = {"quote": 0, "invoice": 0}
    if case is not None:
        removed = _remove_intent_data(session, message_id, actor=actor)
        session.commit()

    # 4) 重建 NormalizedMessage 并重跑管线（不自动发送、不推群）
    message, raw_source = rebuild_message(session, stored)
    if raw_source == "db":
        notes.append("附件不可用：原始 .eml 未存档，按数据库字段重建")

    from .pipeline import _process_stored  # 局部导入，避免导入环

    result = _process_stored(
        session,
        stored,
        message,
        llm_provider=None,
        retriever=None,
        auto_send=False,
        notify=False,
    )
    session.commit()

    # 重新处理后，若新草稿是 form_request，附上该 case 已有的填表产物（含签名的表格）
    if result.draft_id and result.intent.label == "form_request" and case is not None:
        draft = session.get(Draft, result.draft_id)
        if draft is not None:
            attached = _attach_case_fillings(session, case, draft)
            if attached:
                draft.body = draft.body.rstrip("\n") + (
                    "\n\nPlease find attached the completed application form(s) "
                    "and supporting documents.\n"
                )
                draft.updated_at = utcnow()
                session.flush()
                audit.record(
                    session,
                    action="draft.attachments_added",
                    actor=actor,
                    case_id=case.id,
                    thread_id=stored.thread_id,
                    message_id=message_id,
                    draft_id=draft.id,
                    decision=str(len(attached)),
                    reasons=[f"重新处理：自动附上填表产物 {attached}"],
                )
            session.commit()

    new_intent = result.intent.label
    audit.record(
        session,
        action="message.reprocessed",
        actor=actor,
        case_id=case.id if case is not None else None,
        thread_id=stored.thread_id,
        message_id=message_id,
        draft_id=result.draft_id,
        decision=new_intent,
        reasons=[f"{old_intent} → {new_intent}", f"raw_source: {raw_source}", *notes],
    )
    session.commit()

    return {
        "message_id": message_id,
        "case_id": case.id if case is not None else None,
        "old_intent": old_intent,
        "new_intent": new_intent,
        "raw_source": raw_source,
        "state_reverted": state_reverted,
        "rejected_draft_ids": rejected_ids,
        "removed_quotes": removed["quote"],
        "removed_invoices": removed["invoice"],
        "draft_id": result.draft_id,
        "draft_status": result.draft_status,
        "notes": notes,
    }
