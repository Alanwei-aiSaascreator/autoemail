from autoemail.channels.base import SendResult
from autoemail.drafts import approve_draft
from autoemail.models import AuditEvent, Draft, Message, SendLedger, DRAFT_BLOCKED, DRAFT_FAILED, DRAFT_SENT
from autoemail.pipeline import process_message
from pathlib import Path
from autoemail.channels.eml import EmlDirectoryChannel

FIXTURES = Path(__file__).parent / "fixtures"


class FakeChannel:
    name = "fake"

    def __init__(self, result=None):
        self.calls = []
        self.result = result or SendResult(ok=True, channel_msg_id="outbound-1")

    def send(self, outbound):
        self.calls.append(outbound)
        return self.result


def _create_draft(session, world):
    result = process_message(session, EmlDirectoryChannel(FIXTURES).fetch_since()[0])
    return session.get(Draft, result.draft_id)


def test_approve_rechecks_gate_and_sends(session, world):
    draft = _create_draft(session, world)
    channel = FakeChannel()
    result = approve_draft(session, draft.id, reviewer="reviewer", channel=channel)

    assert result.send_result.ok is True
    assert result.draft.status == DRAFT_SENT
    assert len(channel.calls) == 1
    assert session.query(SendLedger).count() == 1
    outbound = session.query(Message).filter(Message.direction == "out").one()
    assert outbound.subject == draft.subject
    assert session.query(AuditEvent).filter(AuditEvent.draft_id == draft.id).count() >= 2


def test_approval_cannot_send_to_untrusted_recipient(session, world):
    draft = _create_draft(session, world)
    draft.to_addrs = ["attacker@evil.test"]
    session.commit()
    channel = FakeChannel()
    result = approve_draft(session, draft.id, reviewer="reviewer", channel=channel)
    assert result.draft.status == DRAFT_BLOCKED
    assert channel.calls == []
    assert session.query(SendLedger).count() == 0


def test_send_failure_is_retryable(session, world):
    draft = _create_draft(session, world)
    channel = FakeChannel(SendResult(ok=False, error="smtp unavailable"))
    result = approve_draft(session, draft.id, reviewer="reviewer", channel=channel)
    assert result.draft.status == DRAFT_FAILED
    assert result.draft.send_error == "smtp unavailable"
    assert len(channel.calls) == 1
    ledger = session.query(SendLedger).one()
    assert ledger.result == "failed"


def test_second_approval_of_sent_draft_is_rejected(session, world):
    draft = _create_draft(session, world)
    channel = FakeChannel()
    approve_draft(session, draft.id, reviewer="reviewer", channel=channel)
    try:
        approve_draft(session, draft.id, reviewer="reviewer", channel=channel)
    except ValueError as exc:
        assert "不允许批准" in str(exc)
    else:
        raise AssertionError("已发送草稿不应允许再次批准")
    assert len(channel.calls) == 1
