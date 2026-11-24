from autoemail.channels.base import SendResult
from autoemail.initiate import start_case
from autoemail.models import (
    Case,
    CommissionDocument,
    Draft,
    Message,
    SendLedger,
    Thread,
    DRAFT_PENDING,
    DRAFT_SENT,
)


class FakeChannel:
    name = "fake"

    def __init__(self, result=None):
        self.calls = []
        self.result = result or SendResult(ok=True, channel_msg_id="initial-out-1")

    def send(self, outbound):
        self.calls.append(outbound)
        return self.result


def _close_seed_case(session, world):
    world["case"].state = "closed"
    session.commit()


def test_start_case_dry_run_creates_inquiry_draft(session, world):
    _close_seed_case(session, world)
    result = start_case(
        session,
        commission_id=world["commission"].id,
        agent_id=world["agent"].id,
        agent_email="toni@a1surety.com",
        auto_mode="dry_run",
    )

    assert result.case.case_no.startswith("BOND-")
    assert result.case.state == "init"
    assert result.thread.case_id == result.case.id
    assert result.message.direction == "out"
    assert "[BOND-" in result.message.subject
    assert result.draft is not None
    assert result.draft.status == DRAFT_PENDING
    assert result.draft.intent == "initial_inquiry"
    assert result.draft.controlled_fields["bond_amount"] == "$50,000"


def test_start_case_rejects_incomplete_commission(session, world):
    _close_seed_case(session, world)
    world["commission"].signer_name = None
    session.commit()
    try:
        start_case(
            session,
            commission_id=world["commission"].id,
            agent_id=world["agent"].id,
            agent_email="toni@a1surety.com",
        )
    except ValueError as exc:
        assert "资料不完整" in str(exc)
        assert "签署人" in str(exc)
    else:
        raise AssertionError("委托资料不完整时不应发起询价")


def test_start_case_rejects_missing_commission_documents(session, world):
    _close_seed_case(session, world)
    document = session.query(CommissionDocument).filter_by(
        commission_id=world["commission"].id, doc_type="id"
    ).one()
    session.delete(document)
    session.commit()
    try:
        start_case(
            session,
            commission_id=world["commission"].id,
            agent_id=world["agent"].id,
            agent_email="toni@a1surety.com",
        )
    except ValueError as exc:
        assert "委托文件不完整" in str(exc)
        assert "法人证件" in str(exc)
    else:
        raise AssertionError("委托文件不齐时不应发起询价")


def test_start_case_rejects_untrusted_agent_email(session, world):
    _close_seed_case(session, world)
    try:
        start_case(
            session,
            commission_id=world["commission"].id,
            agent_id=world["agent"].id,
            agent_email="person@evil.test",
        )
    except ValueError as exc:
        assert "域名" in str(exc)
    else:
        raise AssertionError("不可信代理商邮箱不应被接受")


def test_start_case_on_sends_and_moves_to_quoted_out(session, world):
    _close_seed_case(session, world)
    channel = FakeChannel()
    result = start_case(
        session,
        commission_id=world["commission"].id,
        agent_id=world["agent"].id,
        agent_email="toni@a1surety.com",
        auto_mode="on",
        channel=channel,
    )

    assert result.draft is None
    assert result.send_result.ok is True
    assert result.case.state == "quoted_out"
    assert result.message.sent_at is not None
    assert result.message.channel_msg_id == "initial-out-1"
    assert len(channel.calls) == 1
    assert session.query(SendLedger).count() == 1


def test_start_case_failed_send_blocks_case(session, world):
    _close_seed_case(session, world)
    channel = FakeChannel(SendResult(ok=False, error="smtp unavailable"))
    result = start_case(
        session,
        commission_id=world["commission"].id,
        agent_id=world["agent"].id,
        agent_email="toni@a1surety.com",
        auto_mode="on",
        channel=channel,
    )
    assert result.case.state == "blocked"
    assert result.send_result.ok is False
    assert session.query(SendLedger).one().result == "failed"
