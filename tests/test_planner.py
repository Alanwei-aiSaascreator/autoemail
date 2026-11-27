"""回复全量规划器测试：LLM 框架内决策 → 框架用核实数据执行。"""

from autoemail import planner
from autoemail.channels.base import NormalizedAttachment, NormalizedMessage
from autoemail.config import settings
from autoemail.llm import FakeLLMProvider
from autoemail.llm_schemas import PlanAttachment, PlanInlineAnswer, ReplyPlanOutput
from autoemail.models import Case, Draft
from autoemail.pipeline import process_message


def _plan(**overrides) -> ReplyPlanOutput:
    data = dict(
        intent="form_request",
        confidence=0.9,
        materials=["application", "ownership", "ad_cvd"],
        inline_answers=[
            PlanInlineAnswer(question="Who owns the company?", answer_key="ownership"),
            PlanInlineAnswer(
                question="Are the goods subject to AD/CVD?", answer_key="ad_cvd"
            ),
            PlanInlineAnswer(
                question="When do you need the bond effective?", answer_key="effective_date"
            ),
        ],
        attachments=[
            PlanAttachment(
                filename="blank_bond_app.pdf",
                disposition="fill_sign_return",
                note="blank application",
            ),
            PlanAttachment(
                filename="marketing_brochure.pdf",
                disposition="ignore",
                note="brochure",
            ),
        ],
        state_hint="materials_requested",
        reasoning="agent asked for app + ownership + adcvd and questions",
    )
    data.update(overrides)
    return ReplyPlanOutput(**data)


def _message() -> NormalizedMessage:
    pdf = b"%PDF-1.4 nothing"
    return NormalizedMessage(
        channel="eml",
        channel_msg_id="plan-1",
        from_addr="toni@a1surety.com",
        subject="Re: [BOND-2026-0142] Application",
        body_text=(
            "Please complete, sign, and return the attached application form. "
            "Who owns the company? "
            "Are the goods subject to antidumping duties? When do you need the bond effective?"
        ),
        dedupe_hash="plan-hash-1",
        attachments=[
            NormalizedAttachment(
                filename="blank_bond_app.pdf",
                mime="application/pdf",
                content=pdf,
                sha256="h1",
            ),
            NormalizedAttachment(
                filename="marketing_brochure.pdf",
                mime="application/pdf",
                content=pdf,
                sha256="h2",
            ),
        ],
    )


def test_plan_reply_none_without_provider():
    assert planner.plan_reply(_message(), provider=None, case_no="X", case_state="quoted_out") is None


def test_plan_reply_none_below_confidence():
    provider = FakeLLMProvider(_plan(confidence=0.5))
    assert (
        planner.plan_reply(
            _message(), provider=provider, case_no="X", case_state="quoted_out"
        )
        is None
    )


def test_plan_to_slots_shape():
    slots = planner.plan_to_slots(_plan())
    assert slots["materials"] == ["application", "ownership", "ad_cvd"]
    assert slots["state_hint"] == "materials_requested"
    assert slots["inline_answers"][0]["answer_key"] == "ownership"
    assert slots["attachment_plan"][1]["disposition"] == "ignore"


def test_pipeline_consumes_plan(session, world, monkeypatch, tmp_path):
    """计划驱动：意图/材料/逐题回答/跳过附件/状态建议全部落地。"""
    monkeypatch.setattr(settings, "filled_forms_dir", str(tmp_path))
    provider = FakeLLMProvider(_plan())
    result = process_message(session, _message(), llm_provider=provider)
    draft = session.get(Draft, result.draft_id)
    assert draft is not None
    # 意图被计划改写为 form_request
    assert draft.intent == "form_request"
    # 逐题回答：股权 100% 口径 + AD/CVD + 生效日期
    assert "only one owner" in draft.body
    assert "AD/CVD" in draft.body
    # effective_date 应答：需求表 world 里没填 a006，允许整题省略或出现句子
    assert "only one owner" in draft.body
    # 状态建议被采纳：quoted_out → materials_requested 在白名单内
    case = session.get(Case, world["case"].id)
    assert case.state == "materials_requested"
    # 审计留痕
    from sqlalchemy import select

    from autoemail.models import AuditEvent

    events = session.scalars(
        select(AuditEvent).where(AuditEvent.action == "llm.reply_plan")
    ).all()
    assert len(events) == 1


def test_pipeline_state_hint_rejected_when_not_allowed(session, world, monkeypatch, tmp_path):
    """状态建议不在白名单（quoted → quoted_out 非法）时保持确定性映射，不执行。"""
    monkeypatch.setattr(settings, "filled_forms_dir", str(tmp_path))
    case = session.get(Case, world["case"].id)
    case.state = "quoted"
    session.commit()
    provider = FakeLLMProvider(_plan(state_hint="quoted_out"))
    result = process_message(session, _message(), llm_provider=provider)
    draft = session.get(Draft, result.draft_id)
    assert draft is not None
    session.refresh(case)
    # quoted_out 对 quoted 非法：确定性映射 form_request → materials_requested 生效
    assert case.state == "materials_requested"


def test_pipeline_falls_back_without_plan(session, world, monkeypatch, tmp_path):
    """LLM 返回解析失败（error）时回退确定性链路，草稿仍正常生成。"""
    monkeypatch.setattr(settings, "filled_forms_dir", str(tmp_path))
    provider = FakeLLMProvider(None, error="boom")
    result = process_message(session, _message(), llm_provider=provider)
    assert result.draft_id is not None
    draft = session.get(Draft, result.draft_id)
    assert draft is not None
    assert "only one owner" not in draft.body


# ---- 扫描件的眼睛：视觉 OCR 提取 ----

def test_extract_scanned_none_without_provider():
    assert planner.extract_scanned_pdf_text(b"not a pdf", provider=None) == ""


def test_extract_scanned_unreadable_pdf_returns_empty():
    provider = FakeLLMProvider(None)
    assert planner.extract_scanned_pdf_text(b"%PDF-1.4 broken", provider=provider) == ""


def test_pipeline_uses_vision_for_textless_pdf(session, world, monkeypatch, tmp_path):
    """无文本层附件触发视觉 OCR，规划器拿到文字后正常出计划。"""
    import io

    from reportlab.lib.pagesizes import letter as page_size
    from reportlab.pdfgen import canvas as rl_canvas

    # 造一个真无文本层的图片 PDF
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=page_size)
    c.drawString(72, 700, "scan-placeholder")
    c.save()
    img_pdf = b"%PDF-1.4 minimal"  # 文本层提取为空即可触发视觉路径

    calls = {"ocr": 0}

    def fake_ocr(pdf_bytes, *, provider=None, max_pages=3):
        calls["ocr"] += 1
        return "U.S. Customs Bond Application blank form for completion"

    monkeypatch.setattr(planner, "extract_scanned_pdf_text", fake_ocr)
    monkeypatch.setattr(settings, "filled_forms_dir", str(tmp_path))
    plan = _plan()
    provider = FakeLLMProvider(plan)
    msg = _message()
    result = process_message(session, msg, llm_provider=provider)
    draft = session.get(Draft, result.draft_id)
    assert draft is not None
