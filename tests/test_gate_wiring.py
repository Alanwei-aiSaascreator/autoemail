from datetime import datetime, timezone
from pathlib import Path

from autoemail import audit
from autoemail.channels.base import SendResult
from autoemail.channels.eml import EmlDirectoryChannel
from autoemail.drafts import approve_draft
from autoemail.models import (
    AdcvdAttestation,
    Agent,
    AgentPolicy,
    AuditEvent,
    Case,
    Commission,
    Draft,
    DRAFT_BLOCKED,
    DRAFT_SENT,
)
from autoemail.outbound import send_draft
from autoemail.pipeline import process_message
from autoemail.textutil import normalize_text

FIXTURES = Path(__file__).parent / "fixtures"


class FakeChannel:
    name = "fake"

    def __init__(self):
        self.calls = []

    def send(self, outbound):
        self.calls.append(outbound)
        return SendResult(ok=True, channel_msg_id="wired-1")


def _draft(session, world):
    result = process_message(session, EmlDirectoryChannel(FIXTURES).fetch_since()[0])
    return session.get(Draft, result.draft_id)


def _other_active_case(session, world):
    agent = Agent(name="BorderBuddy", email_domains=["borderbuddy.com"])
    session.add(agent)
    session.flush()
    commission = Commission(
        legal_name="Blue Harbor Trading Inc",
        legal_name_norm=normalize_text("Blue Harbor Trading Inc"),
        entity_type="Corp",
        ein="98-7654321",
    )
    session.add(commission)
    session.flush()
    other = Case(
        case_no="BOND-2026-0300",
        commission_id=commission.id,
        agent_id=agent.id,
        state="quoted",
    )
    session.add(other)
    session.commit()
    return other


def test_r04_blocks_other_commission_ein(session, world):
    _other_active_case(session, world)
    draft = _draft(session, world)
    draft.body += "\nPlease also confirm EIN 98-7654321."
    session.commit()
    channel = FakeChannel()

    result = approve_draft(session, draft.id, reviewer="ops", channel=channel)

    assert result.draft.status == DRAFT_BLOCKED
    assert "R04" in result.draft.rule_hits
    assert channel.calls == []


def test_r06_blocks_adcvd_statement_without_attestation(session, world):
    draft = _draft(session, world)
    draft.body += "\nWe confirm the goods are not subject to AD/CVD."
    session.commit()
    channel = FakeChannel()

    result = approve_draft(session, draft.id, reviewer="ops", channel=channel)

    assert result.draft.status == DRAFT_BLOCKED
    assert "R06" in result.draft.rule_hits
    assert channel.calls == []


def test_r06_allows_adcvd_statement_with_attestation(session, world):
    session.add(
        AdcvdAttestation(
            case_id=world["case"].id,
            hts_code="9403.60.8081",
            country_of_origin="Vietnam",
            statement="Not subject to AD/CVD per broker review.",
            attested_by="ops@sunrise.test",
        )
    )
    session.commit()
    draft = _draft(session, world)
    draft.body += "\nWe confirm the goods are not subject to AD/CVD."
    session.commit()
    channel = FakeChannel()

    result = approve_draft(session, draft.id, reviewer="ops", channel=channel)

    assert result.draft.status == DRAFT_SENT
    assert "R06" not in result.draft.rule_hits
    assert len(channel.calls) == 1


def test_r06_ignores_adcvd_topic_mention(session, world):
    """单纯提及 AD/CVD（如询价模板里 'AD/CVD schedule if applicable'）不是声明，不触发 R06。"""
    draft = _draft(session, world)
    draft.body += "\n(our client application covers ... AD/CVD schedule if applicable)"
    session.commit()
    channel = FakeChannel()

    result = approve_draft(session, draft.id, reviewer="ops", channel=channel)

    assert result.draft.status == DRAFT_SENT
    assert "R06" not in result.draft.rule_hits
    assert len(channel.calls) == 1


def test_r08_blocks_broker_conflict(session, world):
    policy = session.query(AgentPolicy).filter(AgentPolicy.agent_id == world["agent"].id).one()
    policy.requires_broker_for_customs = True
    world["commission"].customs_broker = "Other Customs Broker"
    session.commit()
    draft = _draft(session, world)
    channel = FakeChannel()

    result = approve_draft(session, draft.id, reviewer="ops", channel=channel)

    assert result.draft.status == DRAFT_BLOCKED
    assert "R08" in result.draft.rule_hits
    assert channel.calls == []


def test_r10_stops_when_thread_reaches_auto_send_limit(session, world):
    from autoemail.policy.gate import MAX_AUTO_SENDS_PER_THREAD_24H

    draft = _draft(session, world)
    world["case"].auto_mode = "on"
    session.commit()
    for _ in range(MAX_AUTO_SENDS_PER_THREAD_24H):
        audit.record(
            session,
            action="draft.sent",
            case_id=world["case"].id,
            thread_id=draft.thread_id,
            actor="agent",
            decision=DRAFT_SENT,
            commit=False,
        )
    session.commit()
    channel = FakeChannel()

    result = send_draft(
        session,
        draft,
        channel=channel,
        actor="agent",
        manual_approval=False,
        assignment_auto_eligible=True,
    )

    assert result.draft.status == DRAFT_BLOCKED
    assert "R10" in result.draft.rule_hits
    assert channel.calls == []


def test_r10_does_not_block_manual_approval(session, world):
    draft = _draft(session, world)
    audit.record(
        session,
        action="draft.sent",
        case_id=world["case"].id,
        thread_id=draft.thread_id,
        actor="agent",
        decision=DRAFT_SENT,
    )
    channel = FakeChannel()

    result = approve_draft(session, draft.id, reviewer="ops", channel=channel)

    assert result.draft.status == DRAFT_SENT
    assert len(channel.calls) == 1
