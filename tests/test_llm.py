from autoemail import audit
from autoemail.intent import IntentResult
from autoemail.intent_llm import classify_with_llm, classify_with_llm_fallback
from autoemail.llm import FakeLLMProvider
from autoemail.llm_schemas import LLMIntentOutput
from autoemail.channels.base import NormalizedAttachment, NormalizedMessage


def message():
    return NormalizedMessage(channel="eml", channel_msg_id="m", from_addr="a@broker.test", subject="Question", body_text="Can you clarify the process?", dedupe_hash="h")


def test_llm_reads_full_text_and_attachments_as_primary():
    """用户要求：意图判断必须读完全文（含附件清单），LLM 优先、关键词只做兜底。"""
    blank_form_email = NormalizedMessage(
        channel="eml",
        channel_msg_id="m2",
        from_addr="kendyl@goldleafsurety.test",
        subject="RE: [BOND-2026-0192] Inquiry - Continuous Customs Bond",
        body_text=(
            "In order to proceed, the forms and other information we would need are:\n"
            "- Customs Bond application thoroughly completed and signed.\n"
            "- Personal Financial Statement for all owners and spouses (if married)"
        ),
        attachments=[
            NormalizedAttachment(filename="CA Shea.Customs Bond Application.pdf", mime="application/pdf", content=b"", sha256="x", doc_type="bond"),
        ],
        dedupe_hash="h2",
    )
    provider = FakeLLMProvider(LLMIntentOutput(label="form_request", confidence=0.95, evidence=["body requests completed and signed forms"]))
    result = classify_with_llm(blank_form_email, provider=provider)
    assert result is not None
    assert result.label == "form_request"
    assert result.evidence[0] == "LLM"
    # prompt 必须包含全文与附件清单
    prompt = provider.calls[0]["user"]
    assert "Attachments:" in prompt
    assert "CA Shea.Customs Bond Application.pdf" in prompt
    assert "thoroughly completed and signed" in prompt


def test_llm_unavailable_returns_none_for_deterministic_fallback():
    provider = FakeLLMProvider(error="timeout")
    assert classify_with_llm(message(), provider=provider) is None
    assert classify_with_llm(message(), provider=None) is None


def test_llm_low_confidence_returns_none():
    provider = FakeLLMProvider(LLMIntentOutput(label="form_request", confidence=0.5))
    assert classify_with_llm(message(), provider=provider) is None


def test_deterministic_result_does_not_call_llm():
    provider = FakeLLMProvider(LLMIntentOutput(label="form_request", confidence=0.99))
    deterministic = IntentResult("quote_reply", 0.9)
    result = classify_with_llm_fallback(message(), deterministic=deterministic, provider=provider)
    assert result.label == "quote_reply"
    assert provider.calls == []


def test_llm_fallback_returns_structured_intent():
    provider = FakeLLMProvider(LLMIntentOutput(label="form_request", confidence=0.9, evidence=["asks for application"]))
    result = classify_with_llm_fallback(message(), deterministic=IntentResult("unclear", 0.35), provider=provider)
    assert result.label == "form_request"
    assert result.confidence == 0.9
    assert len(provider.calls) == 1


def test_llm_failure_falls_back_to_unclear():
    provider = FakeLLMProvider(error="timeout")
    result = classify_with_llm_fallback(message(), deterministic=IntentResult("unclear", 0.35), provider=provider)
    assert result.label == "unclear"


def test_no_provider_keeps_unclear():
    result = classify_with_llm_fallback(message(), deterministic=IntentResult("unclear", 0.35), provider=None)
    assert result.label == "unclear"


def test_llm_stats_summarizes_shadow_fallback_and_draft(session):
    audit.record(
        session,
        action="llm.intent.shadow",
        decision="quote_reply",
        reasons=["deterministic=quote_reply", "llm=quote_reply", "agree=True"],
        commit=False,
    )
    audit.record(
        session,
        action="llm.intent.shadow",
        decision="form_request",
        reasons=["deterministic=quote_reply", "llm=form_request", "agree=False"],
        commit=False,
    )
    audit.record(session, action="llm.intent.fallback", decision="form_request", commit=False)
    audit.record(session, action="llm.draft.completed", decision="llm_suggestion", commit=False)
    audit.record(session, action="llm.draft.rejected", decision="fallback_template", commit=False)
    session.commit()

    stats = audit.llm_stats(session)

    assert stats["shadow"]["total"] == 2
    assert stats["shadow"]["agree"] == 1
    assert stats["shadow"]["agree_rate"] == 0.5
    assert {(pair["deterministic"], pair["llm"], pair["count"]) for pair in stats["shadow"]["pairs"]} == {
        ("quote_reply", "quote_reply", 1),
        ("quote_reply", "form_request", 1),
    }
    assert stats["fallback"] == {"total": 1, "labels": {"form_request": 1}}
    assert stats["draft"] == {"total": 2, "decisions": {"llm_suggestion": 1, "fallback_template": 1}}


def test_llm_stats_empty_database(session):
    stats = audit.llm_stats(session)
    assert stats["shadow"]["total"] == 0
    assert stats["shadow"]["agree_rate"] is None
