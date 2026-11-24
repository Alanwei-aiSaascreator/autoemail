from decimal import Decimal

from autoemail.channels.base import NormalizedMessage, SendResult
from autoemail.drafts import approve_draft
from autoemail.models import AgentPolicy, Case, Draft, Message, Quote, DRAFT_SENT
from autoemail.pipeline import process_message
from autoemail.quotes import quote_eval_for_case, record_quote


class FakeChannel:
    name = "fake"

    def __init__(self):
        self.calls = []

    def send(self, outbound):
        self.calls.append(outbound)
        return SendResult(ok=True, channel_msg_id="quote-out-1")


def _quote_message(amount: str, *, msg_id: str = "quote-1", hash_id: str = "quote-hash-1"):
    return NormalizedMessage(
        channel="eml",
        channel_msg_id=msg_id,
        from_addr="toni@a1surety.com",
        subject="Re: [BOND-2026-0142] Bond quote",
        body_text=f"Our annual premium is {amount}. Please confirm the next steps.",
        dedupe_hash=hash_id,
    )


def test_quote_under_line_is_recorded(session, world):
    result = process_message(session, _quote_message("$750"))

    quote = session.query(Quote).one()
    draft = session.get(Draft, result.draft_id)
    assert quote.total_first_year == Decimal("750.00")
    assert quote.currency == "USD"
    assert "R07" not in (draft.rule_hits or [])
    assert quote_eval_for_case(session, world["case"]).action == "auto_accept"


def test_quote_over_line_requires_manual_approval(session, world):
    world["case"].auto_mode = "dry_run"
    session.commit()
    result = process_message(session, _quote_message("$1,500"))

    quote = session.query(Quote).one()
    draft = session.get(Draft, result.draft_id)
    assert quote.total_first_year == Decimal("1500.00")
    assert "R07" in draft.rule_hits
    assert any("审批线" in notice for notice in draft.notices)


def test_agent_extra_fee_counts_toward_first_year_total(session, world):
    world["case"].auto_mode = "dry_run"
    policy = session.query(AgentPolicy).filter(AgentPolicy.agent_id == world["agent"].id).one()
    policy.extra_fees = [{"label": "CF5106 注册费", "amount": "55"}]
    session.commit()

    result = process_message(session, _quote_message("$950"))

    quote = session.query(Quote).one()
    draft = session.get(Draft, result.draft_id)
    assert quote.total_first_year == Decimal("1005.00")
    assert "R07" in draft.rule_hits


def test_manual_approval_can_send_above_line(session, world):
    result = process_message(session, _quote_message("$1,500"))
    draft = session.get(Draft, result.draft_id)
    channel = FakeChannel()

    approved = approve_draft(session, draft.id, reviewer="ops", channel=channel)

    assert approved.draft.status == DRAFT_SENT
    assert len(channel.calls) == 1


def test_anomaly_price_only_adds_notice(session, world):
    world["case"].auto_mode = "dry_run"
    other_case = Case(
        case_no="BOND-2026-0999",
        commission_id=world["commission"].id,
        agent_id=world["agent"].id,
        state="closed",
    )
    session.add(other_case)
    session.flush()
    session.add(
        Quote(
            case_id=other_case.id,
            amount=Decimal("500"),
            currency="USD",
            extras=[],
            total_first_year=Decimal("500"),
        )
    )
    session.commit()

    result = process_message(session, _quote_message("$1,500"))

    quote = session.query(Quote).filter(Quote.case_id == world["case"].id).one()
    draft = session.get(Draft, result.draft_id)
    assert quote.vs_history_ratio == Decimal("3.000")
    assert any("异常价提示" in notice for notice in draft.notices)
    assert "R07" in draft.rule_hits


def test_record_quote_is_idempotent_per_message(session, world):
    result = process_message(session, _quote_message("$750"))
    message_id = result.message_id
    message = session.get(Message, message_id)
    case = world["case"]

    first = record_quote(session, case=case, message=message, amount=Decimal("750"))
    second = record_quote(session, case=case, message=message, amount=Decimal("999"))
    session.commit()

    assert first.id == second.id
    assert session.query(Quote).count() == 1
