from autoemail.channels.base import NormalizedMessage
from autoemail.config import settings
from autoemail.llm import FakeLLMProvider, HttpxOpenAICompatibleProvider
from autoemail.llm_schemas import LLMDraftSuggestion, LLMIntentOutput
from autoemail.models import AuditEvent, Case, Draft, Invoice, Message
from autoemail.pipeline import process_message


def make_unclear_message(case_token: str = "BOND-2026-0142") -> NormalizedMessage:
    return NormalizedMessage(
        channel="eml",
        channel_msg_id="llm-1",
        from_addr="toni@a1surety.com",
        subject=f"Re: [{case_token}] quick question",
        body_text="Just checking on the overall timeline, nothing else.",
        dedupe_hash="llm-hash-1",
    )


def test_llm_primary_classification_advances_state(session, world):
    """LLM 读全文优先判定：unclear 文本被 LLM 判为 form_request 时按 form_request 走。"""
    provider = FakeLLMProvider(LLMIntentOutput(label="form_request", confidence=0.9))
    result = process_message(session, make_unclear_message(), llm_provider=provider)

    assert result.intent.label == "form_request"
    assert result.intent.evidence[0] == "LLM"
    assert result.draft_status == "pending"
    assert session.query(Invoice).count() == 0
    # LLM 判定视同确定性结果，正常推进状态
    assert world["case"].state == "materials_requested"
    event = session.query(AuditEvent).filter(AuditEvent.action == "llm.intent.classify").one()
    assert event.decision == "form_request"
    assert "deterministic=unclear" in event.reasons


def test_llm_overrides_wrong_keyword_classification(session, world, monkeypatch):
    """Acora 案例回归：关键词把空白表格误判 bond_delivered，LLM 全文判定纠正。"""
    from autoemail.channels.base import NormalizedAttachment

    monkeypatch.setattr("autoemail.formfill.auto_fill_for_case", lambda *a, **k: None)
    message = NormalizedMessage(
        channel="eml",
        channel_msg_id="llm-acora",
        from_addr="kendyl@goldleafsurety.com",
        subject="Re: [BOND-2026-0142] Inquiry - Continuous Customs Bond",
        body_text=(
            "In order to proceed, the forms and other information we would need are:\n"
            "- Customs Bond application thoroughly completed and signed."
        ),
        attachments=[
            NormalizedAttachment(
                filename="CA Shea.Customs Bond Application.pdf",
                mime="application/pdf",
                content=b"",
                sha256="x",
                doc_type="bond",
            )
        ],
        dedupe_hash="llm-acora-hash",
    )
    provider = FakeLLMProvider(LLMIntentOutput(label="form_request", confidence=0.95))

    result = process_message(session, message, llm_provider=provider)

    assert result.intent.label == "form_request"
    assert world["case"].state == "materials_requested"


def test_invoice_intent_never_calls_llm(session, world):
    provider = FakeLLMProvider(LLMIntentOutput(label="quote_reply", confidence=0.99))
    message = NormalizedMessage(
        channel="eml",
        channel_msg_id="llm-inv",
        from_addr="toni@a1surety.com",
        subject="Re: [BOND-2026-0142] Invoice 2000",
        body_text="Invoice total: $500. Please remit payment.",
        dedupe_hash="llm-inv-hash",
    )
    result = process_message(session, message, llm_provider=provider)
    assert provider.calls == []
    assert result.intent.label == "invoice"
    assert session.query(Invoice).count() == 1


def test_llm_disagreement_recorded_with_both_labels(session, world):
    """LLM 与关键词判定不一致时，审计必须同时记录两者便于复盘。"""
    provider = FakeLLMProvider(LLMIntentOutput(label="form_request", confidence=0.9))
    message = NormalizedMessage(
        channel="eml",
        channel_msg_id="llm-disagree",
        from_addr="toni@a1surety.com",
        subject="Re: [BOND-2026-0142] Quote",
        body_text="Our annual premium is $750.",
        dedupe_hash="llm-disagree-hash",
    )

    result = process_message(session, message, llm_provider=provider)

    assert result.intent.label == "form_request"
    event = session.query(AuditEvent).filter(AuditEvent.action == "llm.intent.classify").one()
    assert event.decision == "form_request"
    assert "deterministic=quote_reply" in event.reasons
    assert "agree=False" in event.reasons


def test_unconfigured_provider_keeps_deterministic(session, world, monkeypatch):
    monkeypatch.setattr(settings, "llm_base_url", "")
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "llm_model", "")
    provider = HttpxOpenAICompatibleProvider(base_url="", api_key="", model="")
    message = NormalizedMessage(
        channel="eml",
        channel_msg_id="llm-unconfigured",
        from_addr="toni@a1surety.com",
        subject="Re: [BOND-2026-0142] Quote",
        body_text="Our annual premium is $750.",
        dedupe_hash="llm-unconfigured-hash",
    )

    result = process_message(session, message, llm_provider=provider)

    assert result.intent.label == "quote_reply"
    assert session.query(AuditEvent).filter(AuditEvent.action == "llm.intent.classify").count() == 0


def test_llm_draft_body_never_auto_sent_but_intent_can_advance(session, world, monkeypatch):
    """LLM 生成的正文仍需人工审核；但意图判定推进状态不受影响。"""
    monkeypatch.setattr(settings, "llm_draft_enabled", True)
    world["case"].auto_mode = "on"
    session.commit()

    class DualProvider:
        """意图任务返回 LLMIntentOutput，草稿任务返回 LLMDraftSuggestion。"""

        model_id = "fake-model"

        def __init__(self) -> None:
            self.calls: list[dict] = []
            self._intent = FakeLLMProvider(LLMIntentOutput(label="form_request", confidence=0.9))
            self._draft = FakeLLMProvider(
                LLMDraftSuggestion(
                    subject="Re: [BOND-2026-0142] quick question",
                    body="Hi Toni,\n\nThanks for the update.\n\nBest,\nBond Operations",
                )
            )

        def complete_json(self, *, system_prompt, user_prompt, schema, task):
            self.calls.append({"task": task, "system": system_prompt, "user": user_prompt})
            inner = self._draft if task == "draft_suggestion" else self._intent
            return inner.complete_json(
                system_prompt=system_prompt, user_prompt=user_prompt, schema=schema, task=task
            )

    provider = DualProvider()

    result = process_message(session, make_unclear_message(), llm_provider=provider)

    assert result.intent.label == "form_request"
    assert result.draft_status == "pending"
    assert session.query(Message).filter(Message.direction == "out").count() == 0
    # 意图仍按 LLM 判定推进状态（自动化由既有发送闸门统一把关）
    assert world["case"].state == "materials_requested"
