"""历史邮件批量导入：按时间顺序回放，默认不触发自动发送。"""
from pathlib import Path

from sqlalchemy.orm import Session

from .channels.eml import EmlDirectoryChannel
from .pipeline import process_message


def _sort_key(message):
    sent_at = message.sent_at
    return (sent_at.timestamp() if sent_at else float("-inf"), message.channel_msg_id or "")


def import_eml(session: Session, path: str | Path, *, auto_send: bool = False) -> dict:
    """导入目录或单个 .eml，返回统计结果。

    默认 auto_send=False：历史回放只生成草稿，不会真的发信。
    """
    return process_batch(session, EmlDirectoryChannel(path).fetch_since(), auto_send=auto_send)


def process_batch(session: Session, messages, *, auto_send: bool = False) -> dict:
    """按时间顺序处理一批邮件，供 eml 回放与 IMAP 收件共用。"""
    messages = sorted(messages, key=_sort_key)
    summary = {
        "total": len(messages),
        "processed": 0,
        "duplicates": 0,
        "unassigned": 0,
        "drafts": 0,
        "auto_sent": 0,
        "intents": {},
    }
    for message in messages:
        result = process_message(session, message, auto_send=auto_send)
        if result.ignored_reason == "duplicate":
            summary["duplicates"] += 1
            continue
        summary["intents"][result.intent.label] = (
            summary["intents"].get(result.intent.label, 0) + 1
        )
        if result.case_id is None:
            summary["unassigned"] += 1
            continue
        summary["processed"] += 1
        if result.draft_id is not None:
            summary["drafts"] += 1
        if result.draft_status == "sent":
            summary["auto_sent"] += 1
    return summary
