from autoemail.channels.base import NormalizedMessage
from autoemail.config import settings
from autoemail.llm import FakeLLMProvider
from autoemail.llm_schemas import LLMDraftSuggestion
from autoemail.models import Draft, Message
from autoemail.pipeline import process_message
from autoemail.rag import InMemoryRetriever, KnowledgeDocument


def quote_message() -> NormalizedMessage:
    return NormalizedMessage(
        channel="eml",
        channel_msg_id="llm-draft-1",
        from_addr="toni@a1surety.com",
        subject="Re: [BOND-2026-0142] Bond quote",
        body_text="Our annual premium is $750. Please confirm the next steps.",
        dedupe_hash="llm-draft-hash-1",
    )


def suggestion(body: str = "Hi Toni,\n\nThank you for the quote. We will review it.\n\nBest,\nBond Operations") -> LLMDraftSuggestion:
    return LLMDraftSuggestion(subject="Re: [BOND-2026-0142] Bond quote", body=body)


def test_llm_draft_used_when_enabled(session, world, monkeypatch):
    monkeypatch.setattr(settings, "llm_draft_enabled", True)
    provider = FakeLLMProvider(suggestion())

    result = process_message(session, quote_message(), llm_provider=provider)

    draft = session.get(Draft, result.draft_id)
    assert draft.controlled_fields["content_source"] == "llm_suggestion"
    assert draft.model_id == "fake-model"
    assert draft.input_hash
    assert draft.body.startswith("Hi Toni,")
    assert result.draft_status == "pending"


def test_llm_draft_disabled_keeps_template(session, world, monkeypatch):
    monkeypatch.setattr(settings, "llm_draft_enabled", False)
    provider = FakeLLMProvider(suggestion())

    result = process_message(session, quote_message(), llm_provider=provider)

    draft = session.get(Draft, result.draft_id)
    assert draft.controlled_fields["content_source"] == "deterministic_template"
    assert draft.model_id is None
    # 意图分类可能调用 LLM（新主判定流程），但草稿生成绝不调用
    assert all(call["task"] != "draft_suggestion" for call in provider.calls)


def test_llm_draft_adcvd_claim_falls_back_to_template(session, world, monkeypatch):
    monkeypatch.setattr(settings, "llm_draft_enabled", True)
    provider = FakeLLMProvider(
        suggestion("Hi Toni,\n\nThis product is not subject to AD/CVD duties.\n\nBest,\nBond Operations")
    )

    result = process_message(session, quote_message(), llm_provider=provider)

    draft = session.get(Draft, result.draft_id)
    assert draft.controlled_fields["content_source"] == "deterministic_template"
    assert draft.model_id is None


def test_llm_draft_foreign_domain_falls_back_to_template(session, world, monkeypatch):
    monkeypatch.setattr(settings, "llm_draft_enabled", True)
    provider = FakeLLMProvider(
        suggestion("Hi Toni,\n\nPlease contact ops@evil-example.com for details.\n\nBest,\nBond Operations")
    )

    result = process_message(session, quote_message(), llm_provider=provider)

    draft = session.get(Draft, result.draft_id)
    assert draft.controlled_fields["content_source"] == "deterministic_template"
    assert draft.model_id is None


def test_llm_error_falls_back_to_template(session, world, monkeypatch):
    monkeypatch.setattr(settings, "llm_draft_enabled", True)
    provider = FakeLLMProvider(error="timeout")

    result = process_message(session, quote_message(), llm_provider=provider)

    draft = session.get(Draft, result.draft_id)
    assert draft.controlled_fields["content_source"] == "deterministic_template"
    assert draft.model_id is None


def test_llm_draft_is_never_auto_sent(session, world, monkeypatch):
    monkeypatch.setattr(settings, "llm_draft_enabled", True)
    world["case"].auto_mode = "on"
    session.commit()
    provider = FakeLLMProvider(suggestion())

    result = process_message(session, quote_message(), llm_provider=provider)

    assert result.draft_status == "pending"
    assert session.query(Message).filter(Message.direction == "out").count() == 0


def test_rag_references_recorded_on_draft(session, world, monkeypatch):
    monkeypatch.setattr(settings, "llm_draft_enabled", True)
    monkeypatch.setattr(settings, "rag_enabled", True)
    retriever = InMemoryRetriever(
        [
            KnowledgeDocument(
                doc_id="doc-premium",
                title="Premium handling",
                text="annual premium quote confirmation process",
                source="playbook",
            )
        ]
    )
    provider = FakeLLMProvider(suggestion())

    result = process_message(
        session, quote_message(), llm_provider=provider, retriever=retriever
    )

    draft = session.get(Draft, result.draft_id)
    assert draft.controlled_fields["content_source"] == "llm_suggestion"
    assert draft.controlled_fields["rag_doc_ids"] == ["doc-premium"]
    draft_call = next(call for call in provider.calls if call["task"] == "draft_suggestion")
    assert "doc-premium" in draft_call["user"]
