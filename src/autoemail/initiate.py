"""Bond case 主动询价。

这是业务流程的起点：从委托档案创建一个独立 case，给指定代理商发起询价。
一期支持 dry_run/off 先生成审核草稿，只有 auto_mode=on 才通过闸门后发送。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import audit
from .channels.base import MessageChannel, OutboundMessage, SendResult
from .channels.imap import channel_for_commission
from .config import settings
from .documents import missing_documents
from .models import (
    Agent,
    Case,
    Commission,
    Draft,
    Message,
    SendLedger,
    Thread,
    DRAFT_BLOCKED,
    DRAFT_PENDING,
    DRAFT_SENT,
)
from .policy.gate import AUTO_SEND, GateContext, evaluate
from .state import transition_case
from .followups import schedule_followup


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# 委托资料不完整就不询价：代理商报价、后续填表都依赖这些字段
REQUIRED_COMMISSION_FIELDS = (
    ("legal_name", "公司法定全名"),
    ("entity_type", "公司类型"),
    ("ein", "EIN"),
    ("state_of_inc", "注册州"),
    ("addr_line1", "地址"),
    ("addr_city", "城市"),
    ("addr_state", "州"),
    ("addr_zip", "邮编"),
    ("signer_name", "签署人"),
    ("signer_title", "签署人职务"),
)


def missing_commission_fields(commission: Commission) -> list[str]:
    return [
        label
        for field, label in REQUIRED_COMMISSION_FIELDS
        if not str(getattr(commission, field, None) or "").strip()
    ]


@dataclass
class InitiationResult:
    case: Case
    thread: Thread
    message: Message
    draft: Draft | None
    decision: str
    send_result: SendResult | None = None


def _next_case_no(session: Session) -> str:
    year = datetime.now().year
    prefix = f"BOND-{year}-"
    existing = session.scalars(select(Case.case_no).where(Case.case_no.like(f"{prefix}%"))).all()
    numbers = [int(value.rsplit("-", 1)[-1]) for value in existing if value.rsplit("-", 1)[-1].isdigit()]
    return f"{prefix}{max(numbers, default=0) + 1:04d}"


def _agent_address(agent: Agent, explicit: str | None) -> str:
    address = (explicit or "").strip().lower()
    if not address:
        addresses = agent.email_addresses or []
        if len(addresses) == 1:
            address = addresses[0].lower()
    if not address or "@" not in address:
        raise ValueError("主动询价需要代理商邮箱；请传 agent_email 或先维护 Agent.email_addresses")
    domains = {domain.lower() for domain in (agent.email_domains or [])}
    domain = address.rsplit("@", 1)[1]
    if domains and domain not in domains:
        raise ValueError(f"代理商邮箱域名不在档案内：{domain}")
    return address


def _inquiry(case_no: str, commission: Commission, recipient: str) -> tuple[str, str]:
    """询价正文按《客户需求》（CUSTOMS BOND CLIENT APPLICATION）问卷结构撰写。

    开场寒暄/引入句经人味层生成自然变体（每封不同，事实内容不变）。
    """
    from . import humanize

    subject = f"[{case_no}] Inquiry - Continuous Customs Bond"
    address = ", ".join(
        part
        for part in (
            commission.addr_line1,
            commission.addr_city,
            commission.addr_state,
            commission.addr_zip,
        )
        if part
    )
    signer = commission.signer_name or "Bond Operations"
    signer_title = commission.signer_title or "Member"
    seed = humanize.human_seed(case_no, channel_msg_id=recipient)
    body = (
        f"{humanize.greeting('', seed + recipient)}\n\n"
        f"{humanize.opener_inquiry(commission.legal_name, seed)} Details below per our client application:\n\n"
        "01 / BOND REQUEST\n"
        "- Service requested: New bond\n"
        "- Bond coverage: Continuous Customs Bond\n"
        "- Activity code: 1 - Importer\n"
        "- Requested bond limit: USD 50,000\n\n"
        "02 / LEGAL ENTITY & IMPORTER ID\n"
        f"- Exact legal entity: {commission.legal_name}\n"
        f"- Entity type: {commission.entity_type}\n"
        f"- State of formation: {commission.state_of_inc or 'N/A'}\n"
        f"- EIN: {commission.ein or 'N/A'}\n"
        f"- Principal address: {address or 'N/A'}\n\n"
        "03 / CONTACTS & AUTHORIZED SIGNER\n"
        f"- Authorized signer: {signer} ({signer_title})\n"
        f"- Contact email: {commission.mailbox_email or 'N/A'}\n"
        f"- Phone: {commission.phone or 'N/A'}\n\n"
        "Please reply with:\n"
        "1. Your price for this bond and the estimated processing time\n"
        "2. Your application requirements / intake checklist (entity & importer ID, "
        "contacts & owners, merchandise & trade profile, customs operations, bond "
        "history & compliance, and AD/CVD schedule if applicable)\n"
        "3. Documents you need from us - we have Articles of Formation, IRS EIN letter "
        "(CP575), W-9 and principal ID available\n\n"
        f"{humanize.closer(seed)}\n\n"
        f"{humanize.signoff(seed)}\n"
        f"{signer}\n"
        f"{commission.legal_name}\n"
    )
    body = "\n".join(line for line in body.split("\n") if line.strip() or True)
    # 去掉 closer 为空时留下的多余空行段
    while "\n\n\n" in body:
        body = body.replace("\n\n\n", "\n\n")
    return subject, body


def _planned_message(
    session: Session,
    *,
    thread: Thread,
    recipient: str,
    subject: str,
    body: str,
    from_addr: str | None = None,
) -> Message:
    message_id = f"planned-{uuid.uuid4().hex}@autoemail"
    dedupe = hashlib.sha256(f"planned|{thread.id}|{subject}|{recipient}".encode()).hexdigest()
    message = Message(
        thread_id=thread.id,
        direction="out",
        channel="email",
        channel_msg_id=message_id,
        from_addr=from_addr or settings.from_addr or settings.imap_user or "bond-operations@internal",
        to_addrs=[recipient],
        cc_addrs=[],
        subject=subject,
        body_text=body,
        sent_at=None,
        dedupe_hash=dedupe,
    )
    session.add(message)
    session.flush()
    return message


def start_case(
    session: Session,
    *,
    commission_id: int,
    agent_id: int,
    auto_mode: str = "dry_run",
    agent_email: str | None = None,
    channel: MessageChannel | None = None,
) -> InitiationResult:
    """为一个委托主动向一个代理商询价。

    dry_run/off：建立 case/thread/计划消息，并生成 pending 草稿，不发送。
    on：建立 case 后通过发送闸门发送；发送失败会保留 case 和 thread 供重试。
    """
    if auto_mode not in {"off", "dry_run", "on"}:
        raise ValueError("auto_mode 必须是 off、dry_run 或 on")
    commission = session.get(Commission, commission_id)
    agent = session.get(Agent, agent_id)
    if commission is None or agent is None:
        raise ValueError("委托公司或代理商不存在")
    missing = missing_commission_fields(commission)
    if missing:
        raise ValueError("委托资料不完整，缺少：" + "、".join(missing))
    missing_files = missing_documents(session, commission.id)
    if missing_files:
        raise ValueError("委托文件不完整，缺少：" + "、".join(missing_files))
    recipient = _agent_address(agent, agent_email)
    existing = session.scalar(
        select(Case).where(
            Case.commission_id == commission.id,
            Case.agent_id == agent.id,
            Case.state.notin_(("closed", "lost")),
        )
    )
    if existing is not None:
        raise ValueError(f"该委托与代理商已有活跃 case：{existing.case_no}")

    case = Case(
        case_no=_next_case_no(session),
        commission_id=commission.id,
        agent_id=agent.id,
        state="init",
        auto_mode=auto_mode,
    )
    session.add(case)
    session.flush()
    subject, body = _inquiry(case.case_no, commission, recipient)
    thread = Thread(
        case_id=case.id,
        channel="email",
        subject_norm=subject.lower(),
        participants=[recipient],
        first_msg_at=utcnow(),
        last_msg_at=utcnow(),
    )
    session.add(thread)
    session.flush()
    planned = _planned_message(
        session,
        thread=thread,
        recipient=recipient,
        subject=subject,
        body=body,
        from_addr=commission.mailbox_email,
    )

    ctx = GateContext(
        case_state=case.state,
        auto_mode=auto_mode,
        recipients=[recipient],
        thread_participants=[recipient],
        agent_domains=agent.email_domains or [],
        body_text=body,
        state_allows_auto=True,
        ledger_claimed=True,
    )
    decision = evaluate(ctx)
    draft = None
    send_result = None

    if auto_mode != "on" or decision.action != AUTO_SEND:
        draft = Draft(
            case_id=case.id,
            thread_id=thread.id,
            source_message_id=planned.id,
            intent="initial_inquiry",
            subject=subject,
            to_addrs=[recipient],
            cc_addrs=[],
            body=body,
            template_id="initial_inquiry_v1",
            controlled_fields={
                "case_no": case.case_no,
                "commission_name": commission.legal_name,
                "agent_name": agent.name,
                "bond_type": "Continuous Customs Bond",
                "bond_amount": "$50,000",
            },
            status=DRAFT_BLOCKED if decision.action == "block" else DRAFT_PENDING,
            rule_hits=decision.rule_ids,
            notices=decision.notices + decision.reasons,
            input_hash=hashlib.sha256(body.encode()).hexdigest(),
        )
        session.add(draft)
        session.commit()
        audit.record(
            session,
            action="case.initiated",
            case_id=case.id,
            thread_id=thread.id,
            message_id=planned.id,
            draft_id=draft.id,
            decision=draft.status,
            rule_ids=decision.rule_ids,
            reasons=["主动询价已生成审核草稿", *decision.reasons],
        )
        return InitiationResult(case, thread, planned, draft, decision.action)

    if channel is None:
        raise ValueError("auto_mode=on 必须提供邮件渠道")
    dedupe_key = hashlib.sha256(f"initial-inquiry|{case.id}|{recipient}".encode()).hexdigest()
    ledger = SendLedger(dedupe_key=dedupe_key, result="claimed")
    session.add(ledger)
    try:
        session.flush()
    except Exception:
        session.rollback()
        raise ValueError("主动询价已存在或发送台账抢占失败")

    send_result = (channel_for_commission(commission) or channel).send(
        OutboundMessage(to_addrs=[recipient], subject=subject, body_text=body)
    )
    if send_result.ok:
        planned.channel_msg_id = send_result.channel_msg_id
        planned.sent_at = utcnow()
        ledger.result = "sent"
        ledger.channel_msg_id = send_result.channel_msg_id
        transition_case(session, case, "quoted_out", reason="主动询价发送成功", message_id=planned.id)
        case.last_outbound_at = planned.sent_at
        case.waiting_since = planned.sent_at
        session.commit()
        schedule_followup(session, case, thread, trigger_state="quoted_out", trigger_message_id=planned.id)
        audit.record(
            session,
            action="case.initiated",
            case_id=case.id,
            thread_id=thread.id,
            message_id=planned.id,
            decision=DRAFT_SENT,
            reasons=["主动询价已发送"],
        )
    else:
        ledger.result = "failed"
        case.state = "blocked"
        session.commit()
        audit.record(
            session,
            action="case.initiation_failed",
            case_id=case.id,
            thread_id=thread.id,
            message_id=planned.id,
            decision="failed",
            reasons=[send_result.error or "主动询价发送失败"],
        )
    return InitiationResult(case, thread, planned, None, decision.action, send_result)
