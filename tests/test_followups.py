from datetime import timedelta

from autoemail.followups import cancel_followups_for_case, list_due_followups, run_followup, schedule_followup
from autoemail.models import FollowUpTask, FOLLOWUP_CANCELLED, FOLLOWUP_PENDING, Thread, Message
from autoemail.pipeline import process_message
from autoemail.channels.eml import EmlDirectoryChannel
from datetime import datetime, timezone


def make_thread(session, world):
    thread = Thread(case_id=world["case"].id, channel="email", subject_norm="bond", participants=["toni@a1surety.com"])
    session.add(thread)
    session.flush()
    return thread


def make_outbound(session, thread):
    message = Message(thread_id=thread.id, direction="out", channel="fake", channel_msg_id="out-1", from_addr="ops@example.com", to_addrs=["toni@a1surety.com"], cc_addrs=[], subject="Bond inquiry", body_text="Please reply", sent_at=datetime.now(timezone.utc), dedupe_hash="outbound-hash")
    session.add(message)
    session.commit()
    return message


def test_schedule_is_idempotent(session, world):
    thread = make_thread(session, world)
    first = schedule_followup(session, world["case"], thread, trigger_state="quoted_out", sequence_no=1)
    second = schedule_followup(session, world["case"], thread, trigger_state="quoted_out", sequence_no=1)
    assert first.id == second.id
    assert session.query(FollowUpTask).count() == 1


def test_cancel_followups(session, world):
    thread = make_thread(session, world)
    task = schedule_followup(session, world["case"], thread, trigger_state="quoted_out")
    assert cancel_followups_for_case(session, world["case"].id, reason="收到新回复") == 1
    assert session.get(FollowUpTask, task.id).status == FOLLOWUP_CANCELLED


def test_due_followup_creates_draft(session, world):
    thread = make_thread(session, world)
    make_outbound(session, thread)
    task = schedule_followup(session, world["case"], thread, trigger_state="quoted_out", due_at=__import__('datetime').datetime.now(__import__('datetime').timezone.utc) - timedelta(minutes=1))
    world["case"].auto_mode = "dry_run"
    session.commit()
    results = __import__('autoemail.followups', fromlist=['scan_followups']).scan_followups(session, run=True)
    assert results[0]["action"] == "draft_created"
    assert session.get(FollowUpTask, task.id).status == "awaiting_review"
