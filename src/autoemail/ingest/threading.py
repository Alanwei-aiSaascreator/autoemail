"""线程还原与消息入库。"""

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..channels.base import NormalizedMessage
from ..config import settings
from ..models import Attachment, Message, Thread
from ..textutil import normalize_subject


def raw_archive_path(dedupe_hash: str) -> Path:
    """原始邮件落盘的约定路径：{raw_mail_dir}/{dedupe_hash}.eml。"""
    return Path(settings.raw_mail_dir) / f"{dedupe_hash}.eml"


def resolve_thread(session: Session, msg: NormalizedMessage) -> Thread:
    """按 In-Reply-To / References 找父消息所在线程，找不到则新建。

    这里只做线程还原，不做归属判定——归属在 assignment 里单独算。
    """
    parent_ids = [msg.in_reply_to] if msg.in_reply_to else []
    parent_ids.extend(msg.references)

    if parent_ids:
        parent = session.scalar(
            select(Message).where(Message.channel_msg_id.in_(parent_ids))
        )
        if parent:
            thread = session.get(Thread, parent.thread_id)
            if thread:
                return thread

    thread = Thread(
        channel=msg.channel,
        subject_norm=normalize_subject(msg.subject),
        participants=[],
        first_msg_at=msg.sent_at,
        last_msg_at=msg.sent_at,
    )
    session.add(thread)
    session.flush()
    return thread


def store_message(session: Session, msg: NormalizedMessage) -> Message | None:
    """入库并去重。返回 None 表示该消息此前已处理过。"""
    if session.scalar(select(Message.id).where(Message.dedupe_hash == msg.dedupe_hash)):
        return None

    thread = resolve_thread(session, msg)

    # IMAP 入站路径会先把原始 .eml 按去重哈希落盘，这里按约定路径回填 raw_path
    archived = raw_archive_path(msg.dedupe_hash)
    raw_path = str(archived) if archived.is_file() else ""

    message = Message(
        thread_id=thread.id,
        direction="in",
        channel=msg.channel,
        channel_msg_id=msg.channel_msg_id,
        in_reply_to=msg.in_reply_to,
        references=msg.references,
        from_addr=msg.from_addr,
        to_addrs=msg.to_addrs,
        cc_addrs=msg.cc_addrs,
        subject=msg.subject,
        body_text=msg.body_text,
        sent_at=msg.sent_at,
        raw_path=raw_path,
        dedupe_hash=msg.dedupe_hash,
    )
    session.add(message)
    session.flush()

    for item in msg.attachments:
        session.add(
            Attachment(
                message_id=message.id,
                filename=item.filename,
                mime=item.mime,
                size=len(item.content),
                sha256=item.sha256,
                doc_type=item.doc_type,
                expiry_date=item.expiry_date,
                # V0.2 §9：业务已批准敏感证件可进模型；开关在 context_builder 统一裁决
                model_visible=True,
            )
        )

    participants = set(thread.participants or [])
    participants.add(msg.from_addr)
    participants.update(msg.to_addrs)
    thread.participants = sorted(participants)
    if thread.first_msg_at is None:
        thread.first_msg_at = msg.sent_at
    thread.last_msg_at = msg.sent_at or thread.last_msg_at

    session.commit()
    return message
