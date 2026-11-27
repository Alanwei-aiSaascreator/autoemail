from pathlib import Path

from autoemail.channels.base import NormalizedMessage
from autoemail.channels.eml import EmlDirectoryChannel
from autoemail.drafts import edit_draft, generate_draft, list_drafts, reject_draft
from autoemail.cli import main
from autoemail.models import AdcvdAttestation, Draft, DRAFT_PENDING, DRAFT_REJECTED
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
    # AD/CVD 不再"另行提供"，按运营口径在正文中直接回答
    assert "AD/CVD" in draft.body
    # 回答依据客户签名需求表生成时，必须同步落 IOR 备案记录供 R06 闸门核验
    attested = session.query(AdcvdAttestation).filter_by(case_id=world["case"].id).first()
    assert attested is not None
    assert "intake form" in attested.statement


def test_form_request_separates_enclosed_from_to_follow(session, world):
    """BondExchange 场景：对方要求 application + 财务报表 + 股权 + AD/CVD 确认。

    回复必须分两段：能随邮件附回的已填表格 vs 无法自动生成、需另行提供的材料，
    并且 material_keys 落入 controlled_fields 供后续（如重新处理）使用。
    """
    message = NormalizedMessage(
        channel="eml",
        channel_msg_id="material-3",
        from_addr="toni@a1surety.com",
        subject="Re: [BOND-2026-0142] Application",
        body_text=(
            "Hi, in order to issue the bond we need the following:\n"
            "Complete and return the attached application.\n"
            "Please provide the 2026 income statement and balance sheet.\n"
            "Ownership breakdown of the company.\n"
            "Confirmation on whether the goods are subject to antidumping or countervailing duties."
        ),
        dedupe_hash="material-hash-3",
    )
    result = process_message(session, message)
    draft = session.get(Draft, result.draft_id)
    assert result.intent.label == "form_request"
    assert draft.controlled_fields["material_keys"] == [
        "application",
        "financial_statement",
        "ownership",
        "ad_cvd",
    ]
    # 无附件时申请表无法自动填：正文如实说明「无法自动填」，不谎称已附上
    assert "could not be auto-filled" in draft.body
    # 财务报表自动生成附上；股权与 AD/CVD 按标准口径正文直答
    assert "Business financial statement" in draft.body
    assert "100%" in draft.body
    assert "AD/CVD" in draft.body
    # 置信度分级提示：无论高低都必须标出来供人工协同
    assert any("意图置信度" in n for n in (draft.notices or []))


def test_form_request_falls_back_to_agent_form_profile(session, world):
    """正文提取不到清单时，命中 AgentFormProfile 知识库（交流对象画像）。"""
    from autoemail.models import AgentFormProfile

    agent = world["agent"]
    session.add(
        AgentFormProfile(
            agent_id=agent.id,
            name=agent.name,
            materials=["application", "financial_statement"],
        )
    )
    session.commit()
    message = NormalizedMessage(
        channel="eml",
        channel_msg_id="material-4",
        from_addr="toni@a1surety.com",
        subject="Re: [BOND-2026-0142] Application",
        body_text="Please return the thoroughly completed and signed documents.",
        dedupe_hash="material-hash-4",
    )
    result = process_message(session, message)
    draft = session.get(Draft, result.draft_id)
    assert result.intent.label == "form_request"
    assert draft.controlled_fields["material_keys"] == ["application", "financial_statement"]
    assert draft.controlled_fields["material_source"] == "agent_form_profile"
    # 财务报表按运营口径自动生成并随回复附上
    assert "Business financial statement" in draft.body
    assert any("business_financial_statement" in (a or "") for a in (draft.attachments or []))


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


def test_quote_reply_adcvd_statement_auto_attests(session, world):
    """根修回归：任何意图的正文含 AD/CVD 实质声明都自动备案，R06 不再误拦。

    此前只在 form_request 分支落备案，quote_reply 的贸易概况段同样会含
    AD/CVD 声明（BOND-2026-0504 草稿 219 被 R06 拦截），现检测与备案同源。
    """
    from autoemail.models import CommissionIntake

    session.add(
        CommissionIntake(
            commission_id=world["commission"].id,
            fields={
                "a007": "No",
                "a006": "2026-11-17",
                "a012": "Replace",
                "d001": "Industrial Seals",
                "d015": "China",
                "d017": "4 times",
                "d019": "150000",
                "d021": "52500",
            },
        )
    )
    session.commit()
    message = NormalizedMessage(
        channel="eml",
        channel_msg_id="quote-adcvd-2",
        from_addr="toni@a1surety.com",
        subject="Re: [BOND-2026-0142] Quote",
        body_text=(
            "We are pleased to quote the continuous customs bond for an annual "
            "fee of $500. Please also confirm whether the goods are subject to "
            "antidumping or countervailing duties."
        ),
        dedupe_hash="quote-adcvd-hash-2",
    )
    result = process_message(session, message)
    draft = session.get(Draft, result.draft_id)
    assert draft is not None
    assert "not subject to" in draft.body
    attested = session.query(AdcvdAttestation).filter_by(case_id=world["case"].id).first()
    assert attested is not None
    assert "R06" not in (draft.rule_hits or [])
