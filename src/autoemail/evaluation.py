"""评测集导出与回归：用历史邮件建立可重复跑的分类/归属基线。"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .channels.base import NormalizedMessage
from .intent import classify_message
from .models import AuditEvent, Case, Message, Thread


def _normalized(message: Message) -> NormalizedMessage:
    return NormalizedMessage(
        channel=message.channel,
        channel_msg_id=message.channel_msg_id,
        from_addr=message.from_addr,
        subject=message.subject,
        body_text=message.body_text,
        sent_at=message.sent_at,
        dedupe_hash=message.dedupe_hash,
    )


def _case_no(session: Session, message: Message) -> str | None:
    thread = session.get(Thread, message.thread_id)
    if thread is None or thread.case_id is None:
        return None
    case = session.get(Case, thread.case_id)
    return case.case_no if case else None


def _assignment_method(session: Session, message_id: int) -> str | None:
    return session.scalar(
        select(AuditEvent.decision)
        .where(AuditEvent.action == "assignment.resolve", AuditEvent.message_id == message_id)
        .order_by(AuditEvent.id.desc())
    )


def export_labels(session: Session, path: str | Path) -> int:
    """把所有入站邮件导出为 JSONL 标注文件，predicted 已填好，人工只改 expected。"""
    messages = session.scalars(
        select(Message).where(Message.direction == "in").order_by(Message.id)
    ).all()
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    count = 0
    with target.open("w", encoding="utf-8") as handle:
        for message in messages:
            intent = classify_message(_normalized(message)).label
            case_no = _case_no(session, message)
            method = _assignment_method(session, message.id)
            record = {
                "message_key": message.channel_msg_id or message.dedupe_hash,
                "subject": message.subject,
                "from": message.from_addr,
                "predicted": {"intent": intent, "case_no": case_no, "method": method},
                "expected": {"intent": intent, "case_no": case_no, "method": method},
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1
    return count


def _accuracy(correct: int, labeled: int) -> float | None:
    return round(correct / labeled, 4) if labeled else None


def evaluate_labels(session: Session, path: str | Path) -> dict:
    """按标注文件重新计算当前分类与归属，输出准确率和不一致明细。"""
    records = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))

    stats = {field: {"labeled": 0, "correct": 0} for field in ("intent", "case_no", "method")}
    mismatches: list[dict] = []
    missing = 0

    for record in records:
        key = record.get("message_key")
        message = session.scalar(
            select(Message).where(
                (Message.channel_msg_id == key) | (Message.dedupe_hash == key)
            )
        )
        if message is None:
            missing += 1
            continue

        expected = record.get("expected") or {}
        actual = {
            "intent": classify_message(_normalized(message)).label,
            "case_no": _case_no(session, message),
            "method": _assignment_method(session, message.id),
        }
        for field, value in expected.items():
            if field not in stats or value is None:
                continue
            stats[field]["labeled"] += 1
            if actual[field] == value:
                stats[field]["correct"] += 1
            else:
                mismatches.append(
                    {
                        "message_key": key,
                        "field": field,
                        "expected": value,
                        "actual": actual[field],
                        "subject": message.subject,
                    }
                )

    metrics = {
        field: {**values, "accuracy": _accuracy(values["correct"], values["labeled"])}
        for field, values in stats.items()
    }
    return {
        "total": len(records),
        "evaluated": len(records) - missing,
        "missing": missing,
        "metrics": metrics,
        "mismatches": mismatches[:20],
    }
