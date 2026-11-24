"""统一邮件出站服务：Gate、幂等、渠道发送和发送后状态落库。"""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import audit
from .channels.base import MessageChannel, OutboundMessage, SendResult
from .channels.imap import channel_for_commission
from .models import (
    AdcvdAttestation,
    Agent,
    AgentPolicy,
    AuditEvent,
    Case,
    Commission,
    Draft,
    Message,
    SendLedger,
    TERMINAL_STATES,
    Thread,
    DRAFT_BLOCKED,
    DRAFT_FAILED,
    DRAFT_SENT,
    DRAFT_SENDING,
)
from .policy.gate import AUTO_SEND, GateContext, evaluate
from .policy.window import auto_send_window_open
from .quotes import quote_eval_for_case
from .state import state_allows_auto_reply


def utcnow():
    return datetime.now(timezone.utc)


# 只匹配对商品 AD/CVD 状态的实质声明（如 "not subject to AD/CVD duties"），
# 单纯提及 AD/CVD（如 "AD/CVD schedule if applicable"）不构成声明，不触发 R06。
ADCVD_PATTERN = re.compile(
    r"(not\s+(currently\s+)?subject\s+to|subject\s+to)\s+(any\s+)?"
    r"(ad/?cvd|anti-?dumping|countervailing)",
    re.IGNORECASE,
)
REDLINE_WORDS = (
    "保证清关",
    "包清关",
    "保证不被查验",
    "guaranteed clearance",
    "guarantee customs clearance",
    "100% clearance",
)


@dataclass
class OutboundResult:
    draft: Draft
    decision: str
    send_result: SendResult | None = None
    reasons: list[str] | None = None


def resolve_reply_source(session: Session, draft: Draft, explicit: Message | None = None) -> Message | None:
    if explicit is not None:
        return explicit
    if draft.source_message_id is not None:
        return session.get(Message, draft.source_message_id)
    return session.scalar(
        select(Message).where(
            Message.thread_id == draft.thread_id,
            Message.direction == "out",
            Message.sent_at.is_not(None),
        ).order_by(Message.sent_at.desc(), Message.id.desc())
    )


def _claim(session: Session, draft: Draft) -> SendLedger | None:
    key = hashlib.sha256(f"{draft.id}|{draft.version}|{draft.subject}|{draft.to_addrs}|{draft.body}".encode()).hexdigest()
    ledger = SendLedger(dedupe_key=key, draft_id=draft.id, result="claimed")
    session.add(ledger)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return None
    return ledger


def _other_case_markers(session: Session, case: Case) -> list[str]:
    """其他活跃委托的编号、EIN、法定全名，用于 R04 串户检查。"""
    rows = session.execute(
        select(Case.case_no, Commission.ein, Commission.legal_name)
        .join(Commission, Case.commission_id == Commission.id)
        .where(
            Case.id != case.id,
            Case.state.notin_(TERMINAL_STATES),
            Case.commission_id != case.commission_id,
        )
    ).all()
    markers: set[str] = set()
    for case_no, ein, legal_name in rows:
        markers.add(case_no)
        if ein:
            markers.add(ein)
        if legal_name and len(legal_name) >= 4:
            markers.add(legal_name)
    return sorted(marker for marker in markers if marker)


def _adcvd_flags(session: Session, case: Case, body: str) -> tuple[bool, bool]:
    present = bool(ADCVD_PATTERN.search(body))
    attested = (
        session.scalar(
            select(AdcvdAttestation.id).where(AdcvdAttestation.case_id == case.id)
        )
        is not None
    )
    return present, attested


def _redline_hits(body: str) -> list[str]:
    lowered = body.lower()
    return [word for word in REDLINE_WORDS if word.lower() in lowered]


def _auto_send_counts(session: Session, case: Case, thread: Thread) -> tuple[int, int]:
    in_thread = session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.action == "draft.sent",
            AuditEvent.actor == "agent",
            AuditEvent.thread_id == thread.id,
            AuditEvent.ts >= utcnow() - timedelta(hours=24),
        )
    ) or 0
    to_agent = session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .join(Case, AuditEvent.case_id == Case.id)
        .where(
            AuditEvent.action == "draft.sent",
            AuditEvent.actor == "agent",
            Case.agent_id == case.agent_id,
            AuditEvent.ts >= utcnow() - timedelta(hours=1),
        )
    ) or 0
    return in_thread, to_agent


def _consecutive_failures(session: Session, case: Case) -> int:
    statuses = session.scalars(
        select(Draft.status)
        .where(Draft.case_id == case.id)
        .order_by(Draft.id.desc())
        .limit(5)
    ).all()
    count = 0
    for status in statuses:
        if status != DRAFT_FAILED:
            break
        count += 1
    return count


def send_draft(session: Session, draft: Draft, *, channel: MessageChannel, actor: str, manual_approval: bool = False, assignment_auto_eligible: bool = True, source_message: Message | None = None) -> OutboundResult:
    case = session.get(Case, draft.case_id)
    thread = session.get(Thread, draft.thread_id)
    source = resolve_reply_source(session, draft, source_message)
    if case is None or thread is None or source is None:
        raise ValueError("草稿缺少可用的 Case、Thread 或回复基准消息")
    agent = session.get(Agent, case.agent_id)
    agent_domains = list(agent.email_domains or []) if agent else []
    policy = session.scalar(
        select(AgentPolicy).where(AgentPolicy.agent_id == case.agent_id)
    )
    commission = session.get(Commission, case.commission_id)
    adcvd_present, adcvd_attested = _adcvd_flags(session, case, draft.body)
    window_open, window_desc = auto_send_window_open()
    sends_in_thread, sends_to_agent = _auto_send_counts(session, case, thread)
    context = GateContext(
        case_state=case.state,
        auto_mode=case.auto_mode,
        manual_approval=manual_approval,
        thread_frozen=thread.frozen,
        assignment_auto_eligible=assignment_auto_eligible,
        recipients=draft.to_addrs,
        thread_participants=thread.participants or [],
        agent_domains=agent_domains,
        body_text=draft.body,
        other_case_markers=_other_case_markers(session, case),
        adcvd_statement_present=adcvd_present,
        adcvd_attestation_present=adcvd_attested,
        redline_hits=_redline_hits(draft.body),
        quote=quote_eval_for_case(session, case),
        requires_broker_for_customs=bool(policy and policy.requires_broker_for_customs),
        commission_customs_broker=commission.customs_broker if commission else None,
        agent_name=agent.name if agent else "",
        state_allows_auto=manual_approval or state_allows_auto_reply(case.state, draft.intent),
        auto_sends_24h_in_thread=sends_in_thread,
        auto_sends_1h_to_agent=sends_to_agent,
        consecutive_failures=_consecutive_failures(session, case),
        ledger_claimed=True,
        auto_send_window_open=window_open,
        auto_send_window_desc=window_desc,
    )
    decision = evaluate(context)
    draft.rule_hits = decision.rule_ids
    draft.notices = decision.notices + decision.reasons
    if decision.action != AUTO_SEND:
        draft.status = DRAFT_BLOCKED
        session.commit()
        return OutboundResult(draft, decision.action, reasons=decision.reasons)
    ledger = _claim(session, draft)
    if ledger is None:
        draft.status = DRAFT_BLOCKED
        draft.notices = draft.notices + ["发送台账抢占失败，未发送"]
        session.commit()
        return OutboundResult(draft, "block", reasons=["发送台账抢占失败，未发送"])
    draft.status = DRAFT_SENDING
    session.commit()
    result = (channel_for_commission(commission) or channel).send(OutboundMessage(to_addrs=draft.to_addrs, subject=draft.subject, body_text=draft.body, in_reply_to=source.channel_msg_id, references=(source.references or []) + ([source.channel_msg_id] if source.channel_msg_id else []), attachments=list(draft.attachments or [])))
    if not result.ok:
        ledger.result = "failed"
        draft.status = DRAFT_FAILED
        draft.send_error = result.error
        session.commit()
        return OutboundResult(draft, DRAFT_FAILED, result, [result.error or "渠道发送失败"])
    now = utcnow()
    ledger.result = "sent"
    ledger.channel_msg_id = result.channel_msg_id
    draft.status = DRAFT_SENT
    draft.sent_at = now
    case.last_outbound_at = now
    case.waiting_since = now
    outbound = Message(thread_id=draft.thread_id, direction="out", channel=channel.name, channel_msg_id=result.channel_msg_id, in_reply_to=source.channel_msg_id, references=(source.references or []) + ([source.channel_msg_id] if source.channel_msg_id else []), from_addr="", to_addrs=draft.to_addrs, cc_addrs=draft.cc_addrs, subject=draft.subject, body_text=draft.body, sent_at=now, dedupe_hash=hashlib.sha256(f"out|{draft.id}|{draft.version}|{result.channel_msg_id}".encode()).hexdigest())
    session.add(outbound)
    session.commit()
    audit.record(session, action="draft.sent", case_id=case.id, thread_id=thread.id, message_id=source.id, draft_id=draft.id, actor=actor, decision=DRAFT_SENT, reasons=["通过发送闸门并发送"])
    return OutboundResult(draft, AUTO_SEND, result)
