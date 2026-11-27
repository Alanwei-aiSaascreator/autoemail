"""DSN 退信识别与全局拉黑。

对方邮箱拒绝接收（如 Exchange DSN 5.7.129 "you don't have permission to send
to it"）时：该地址进全局黑名单（所有委托均不再发送）、冻结线程、暂停跟催，
并推群通知人工更换联系方式。识别宁缺勿滥：必须正文含投递失败描述，且发件人
为系统退信地址 / 主题为退信 / 正文含 5.x.x DSN 代码三者之一，防止误拉黑。
"""

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit
from .channels.base import NormalizedMessage
from .followups import cancel_followups_for_case
from .models import Case, EmailBlocklist, Thread

# 正文投递失败描述（必要条件）
FAILURE_PHRASES = (
    "delivery has failed",
    "couldn't be delivered",
    "could not be delivered",
    "wasn't delivered",
    "was not delivered",
    "delivery status notification (failure)",
    "undeliverable",
)

# 发件人为系统退信地址（充分条件之一）
SYSTEM_SENDERS = ("mailer-daemon", "postmaster")

DSN_CODE = re.compile(r"\b5\.\d{1,3}\.\d{1,3}\b")
EMAIL_ADDR = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
MAILTO = re.compile(r"mailto:([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})", re.IGNORECASE)

# 系统地址与常见no-reply不进黑名单（黑名单对象必须是收件目标本人）
NON_RECIPIENT_LOCAL = ("mailer-daemon", "postmaster", "no-reply", "noreply", "donotreply")


@dataclass
class BounceInfo:
    reason: str
    dsn_code: str | None = None
    recipients: list[str] = field(default_factory=list)


def detect_bounce(message: NormalizedMessage) -> BounceInfo | None:
    """识别退信并提取被拒收的收件地址；非退信返回 None。"""
    from_addr = (message.from_addr or "").lower()
    subject = (message.subject or "").lower()
    body = message.body_text or ""
    body_lower = body.lower()

    if not any(phrase in body_lower for phrase in FAILURE_PHRASES):
        return None
    system_sender = any(marker in from_addr for marker in SYSTEM_SENDERS)
    subject_signal = any(
        kw in subject
        for kw in ("undeliverable", "undelivered", "delivery status notification", "delivery has failed")
    )
    dsn = DSN_CODE.search(body)
    if not (system_sender or subject_signal or dsn):
        return None

    recipients: list[str] = []
    # 优先取正文 mailto: 链接（DSN 标准格式），退回正文全部邮箱地址
    for match in MAILTO.finditer(body):
        addr = match.group(1).lower()
        if addr not in recipients:
            recipients.append(addr)
    if not recipients:
        for match in EMAIL_ADDR.finditer(body):
            addr = match.group(0).lower()
            if addr == from_addr:
                continue
            local = addr.split("@")[0]
            if any(marker in local for marker in NON_RECIPIENT_LOCAL):
                continue
            if addr not in recipients:
                recipients.append(addr)
    if not recipients:
        return None
    return BounceInfo(reason="DSN 退信：对方邮箱拒收", dsn_code=dsn.group(0) if dsn else None, recipients=recipients)


def record_bounce(session: Session, thread: Thread, bounce: BounceInfo, *, message_id: int | None = None, case: Case | None = None) -> EmailBlocklist:
    """落黑名单 + 冻结线程 + 暂停跟催（审计与通知由调用方负责）。返回首个黑名单记录。"""
    first_row: EmailBlocklist | None = None
    for addr in bounce.recipients:
        row = session.scalar(select(EmailBlocklist).where(EmailBlocklist.email == addr))
        if row is None:
            row = EmailBlocklist(
                email=addr,
                reason=f"{bounce.reason}" + (f"（{bounce.dsn_code}）" if bounce.dsn_code else ""),
                source_message_id=message_id,
            )
            session.add(row)
            audit.record(
                session,
                action="outbound.blocklist.add",
                thread_id=thread.id,
                message_id=message_id,
                case_id=case.id if case else None,
                decision=addr,
                reasons=[row.reason, "该邮箱拒收，全局停止发件（含其他委托）"],
            )
        if first_row is None:
            first_row = row
    if not thread.frozen:
        thread.frozen = True
        thread.freeze_reason = "DSN 退信：对方邮箱拒收，已停止自动发件"
    if case is not None:
        case.followup_paused = True
        case.followup_pause_reason = "对方邮箱拒收（退信），需人工更换联系方式"
        cancel_followups_for_case(session, case.id, reason="对方邮箱拒收（退信）")
    session.flush()
    assert first_row is not None
    return first_row
