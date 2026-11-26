from pathlib import Path

from autoemail.channels.base import NormalizedMessage
from autoemail.channels.eml import EmlDirectoryChannel
from autoemail.drafts import edit_draft, generate_draft, list_drafts, reject_draft
from autoemail.cli import main
from autoemail.models import Draft, DRAFT_PENDING, DRAFT_REJECTED
from autoemail.pipeline import process_message
from autoemail.models import Message, Thread
from autoemail.ingest.threading import store_message
from autoemail.intent import classify_message
from autoemail.ingest.assignment import resolve

FIXTURES = Path(__file__).parent / "fixtures"


def _quote_message():
    return EmlDirectoryChannel(FIXTURES).fetch_since()[0]


def test_quote_message_creates_pending_draft(session, world):
    result = process_message(session, _quote_message())

    assert result.intent.label == "quote_reply"
    assert result.draft_id is not None
    assert result.draft_status == DRAFT_PENDING
    draft = session.get(Draft, result.draft_id)
    assert draft is not None
    assert draft.source_message_id == result.message_id
    assert draft.subject.startswith("Re: [BOND-2026-0142]")
    assert "750" in draft.body
    assert draft.controlled_fields["commission_name"] == "Sunrise Home Goods LLC"


def test_same_source_and_intent_reuses_draft(session, world):
    first = process_message(session, _quote_message())
    second = process_message(session, _quote_message())
    assert first.draft_id == second.draft_id
    assert session.query(Draft).count() == 1


def test_material_draft_uses_agent_policy(session, world):
    message = NormalizedMessage(
        channel="eml",
        channel_msg_id="material-1",
        from_addr="toni@a1surety.com",
        subject="Re: [BOND-2026-0142] Application",
        body_text="Please return the thoroughly completed and signed documents.",
        dedupe_hash="material-hash",
    )
    result = process_message(session, message)
    draft = session.get(Draft, result.draft_id)
    assert result.intent.label == "form_request"
    assert "POA" in draft.body
    assert draft.controlled_fields["requested_materials"]


def test_material_draft_uses_extracted_checklist(session, world):
    """正文明确列出材料清单时，回复应优先用正文提取的材料，而非 policy 默认 POA。"""
    message = NormalizedMessage(
        channel="eml",
        channel_msg_id="material-2",
        from_addr="toni@a1surety.com",
        subject="Re: [BOND-2026-0142] Application",
        body_text=(
            "In order to proceed, the forms and other information we would need are:\n"
            "- Customs Bond application thoroughly completed and signed.\n"
            "- Current business financial statement (balance sheet and income statement).\n"
            "- Please confirm if the goods are subject to any anti-dumping or countervailing duties."
        ),
        dedupe_hash="material-hash-2",
    )
    result = process_message(session, message)
    draft = session.get(Draft, result.draft_id)
    assert result.intent.label == "form_request"
    assert "Completed bond application form" in draft.body
    assert "Business financial statement" in draft.body
    assert "anti-dumping" in draft.body


def test_invoice_does_not_create_email_draft(session, world, monkeypatch):
    monkeypatch.setattr("autoemail.pipeline.notify_wecom", lambda *args, **kwargs: True)
    invoice = EmlDirectoryChannel(FIXTURES).fetch_since()[1]
    result = process_message(session, invoice, webhook="https://wecom.test/hook")
    assert result.intent.label == "invoice"
    assert result.draft_id is None
    assert session.query(Draft).count() == 0


def test_edit_increments_version_and_returns_to_pending(session, world):
    result = process_message(session, _quote_message())
    draft = edit_draft(
        session,
        result.draft_id,
        reviewer="reviewer@example.com",
        body="Updated body",
        to_addrs=["toni@a1surety.com"],
    )
    assert draft.version == 2
    assert draft.status == DRAFT_PENDING
    assert draft.body == "Updated body"
    assert "before" in draft.edited_diff


def test_reject_records_reason(session, world):
    result = process_message(session, _quote_message())
    draft = reject_draft(session, result.draft_id, reviewer="reviewer", reason="报价信息需要补充")
    assert draft.status == DRAFT_REJECTED
    assert draft.reviewer == "reviewer"
    assert draft.reviewed_at is not None


def test_queue_filters_by_status_and_intent(session, world):
    result = process_message(session, _quote_message())
    assert [draft.id for draft in list_drafts(session)] == [result.draft_id]
    assert list_drafts(session, intent="form_request") == []
