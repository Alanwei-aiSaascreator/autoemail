"""代理商回复等待与催办任务服务。"""
from datetime import datetime, timedelta, timezone
import hashlib
import json

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import audit
from .models import (
    Case, Thread, Message, Draft, FollowUpTask,
    FOLLOWUP_PENDING, FOLLOWUP_RUNNING, FOLLOWUP_DONE, FOLLOWUP_CANCELLED,
    FOLLOWUP_FAILED, FOLLOWUP_EXHAUSTED, DRAFT_PENDING,
)

DEFAULT_DELAY = timedelta(days=3)
DEFAULT_MAX_ATTEMPTS = 2


def utcnow():
    return datetime.now(timezone.utc)


def schedule_followup(session: Session, case: Case, thread: Thread, *, trigger_state: str, trigger_message_id: int | None = None, due_at: datetime | None = None, sequence_no: int | None = None) -> FollowUpTask | None:
    sequence = sequence_no or case.followup_count + 1
    key = hashlib.sha256(f"{case.id}|{thread.id}|{trigger_state}|{sequence}".encode()).hexdigest()
    existing = session.scalar(select(FollowUpTask).where(FollowUpTask.idempotency_key == key))
    if existing:
        return existing
    task = FollowUpTask(
        case_id=case.id,
        thread_id=thread.id,
        task_type="followup",
        status=FOLLOWUP_PENDING,
        due_at=due_at or utcnow() + DEFAULT_DELAY,
        max_attempts=DEFAULT_MAX_ATTEMPTS,
        trigger_state=trigger_state,
        trigger_message_id=trigger_message_id,
        sequence_no=sequence,
        idempotency_key=key,
    )
    session.add(task)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return session.scalar(select(FollowUpTask).where(FollowUpTask.idempotency_key == key))
    audit.record(session, action="followup.scheduled", case_id=case.id, thread_id=thread.id, message_id=trigger_message_id, decision=task.status, reasons=[f"到期时间：{task.due_at.isoformat()}"])
    return task


def cancel_followups_for_case(session: Session, case_id: int, *, reason: str) -> int:
    tasks = session.scalars(select(FollowUpTask).where(FollowUpTask.case_id == case_id, FollowUpTask.status.in_([FOLLOWUP_PENDING, FOLLOWUP_RUNNING]))).all()
    for task in tasks:
        task.status = FOLLOWUP_CANCELLED
        task.completed_at = utcnow()
        audit.record(session, action="followup.cancelled", case_id=case_id, thread_id=task.thread_id, decision=FOLLOWUP_CANCELLED, reasons=[reason])
    session.commit()
    return len(tasks)


def list_due_followups(session: Session, *, now: datetime | None = None) -> list[FollowUpTask]:
    now = now or utcnow()
    return list(session.scalars(select(FollowUpTask).where(FollowUpTask.status == FOLLOWUP_PENDING, FollowUpTask.due_at <= now).order_by(FollowUpTask.due_at, FollowUpTask.id)).all())


def claim_followup(session: Session, task_id: int) -> FollowUpTask | None:
    result = session.execute(update(FollowUpTask).where(FollowUpTask.id == task_id, FollowUpTask.status == FOLLOWUP_PENDING).values(status=FOLLOWUP_RUNNING, attempt_count=FollowUpTask.attempt_count + 1))
    if result.rowcount != 1:
        session.rollback()
        return None
    session.commit()
    return session.get(FollowUpTask, task_id)


def run_followup(session: Session, task: FollowUpTask) -> dict:
    case = session.get(Case, task.case_id)
    thread = session.get(Thread, task.thread_id)
    if case is None or thread is None:
        task.status = FOLLOWUP_FAILED
        task.last_error = "关联 case/thread 不存在"
        session.commit()
        return {"task_id": task.id, "action": "failed", "reason": task.last_error}
    if case.state in ("closed", "lost") or thread.frozen or case.auto_mode == "off":
        task.status = FOLLOWUP_CANCELLED
        task.completed_at = utcnow()
        session.commit()
        return {"task_id": task.id, "action": "skipped", "reason": "case 已终止、线程冻结或自动模式关闭"}
    latest_inbound = session.scalar(select(Message).where(Message.thread_id == thread.id, Message.direction == "in").order_by(Message.sent_at.desc(), Message.id.desc()))
    if latest_inbound and task.trigger_message_id and latest_inbound.id > task.trigger_message_id:
        task.status = FOLLOWUP_CANCELLED
        task.completed_at = utcnow()
        session.commit()
        return {"task_id": task.id, "action": "skipped", "reason": "代理商已有新回复"}
    existing = session.scalar(select(Draft).where(Draft.case_id == case.id, Draft.intent == "followup", Draft.status.in_(["pending", "blocked", "failed"])))
    if existing:
        task.status = FOLLOWUP_DONE
        task.completed_at = utcnow()
        session.commit()
        return {"task_id": task.id, "action": "skipped", "draft_id": existing.id, "reason": "已有待处理催办草稿"}
    latest_outbound = session.scalar(select(Message).where(Message.thread_id == thread.id, Message.direction == "out", Message.sent_at.is_not(None)).order_by(Message.sent_at.desc(), Message.id.desc()))
    if latest_outbound is None:
        task.status = FOLLOWUP_CANCELLED
        task.completed_at = utcnow()
        session.commit()
        return {"task_id": task.id, "action": "skipped", "reason": "没有实际发送成功的出站邮件"}
    body = "Hello,\n\nFollowing up on our request for the Continuous Customs Bond. Please let us know the current status and any outstanding items.\n\nBest regards,\nBond Operations\n"
    draft = Draft(case_id=case.id, thread_id=thread.id, source_message_id=latest_outbound.id, intent="followup", subject=f"Re: {latest_outbound.subject}", to_addrs=latest_outbound.to_addrs, cc_addrs=[], body=body, template_id="followup_v1", controlled_fields={"reason": "等待代理商回复", "attempt": task.attempt_count}, status=DRAFT_PENDING, rule_hits=[], notices=["催办草稿，需人工审核"], input_hash=hashlib.sha256(f"{task.id}|{task.attempt_count}".encode()).hexdigest())
    session.add(draft)
    session.flush()
    task.draft_id = draft.id
    task.status = "awaiting_review"
    task.completed_at = None
    case.followup_count = task.attempt_count
    case.last_followup_at = utcnow()
    session.commit()
    audit.record(session, action="followup.draft_created", case_id=case.id, thread_id=thread.id, draft_id=draft.id, decision=DRAFT_PENDING, reasons=["代理商回复超时，生成催办草稿"])
    return {"task_id": task.id, "action": "draft_created", "draft_id": draft.id}


def scan_followups(session: Session, *, now: datetime | None = None, run: bool = False) -> list[dict]:
    results = []
    for task in list_due_followups(session, now=now):
        claimed = claim_followup(session, task.id)
        if not claimed:
            continue
        results.append(run_followup(session, claimed) if run else {"task_id": claimed.id, "action": "due"})
    return results
