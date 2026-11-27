"""CBP Bond 文件合规自动审查测试（指导文件 + rules/rule_def.csv）。"""

from datetime import date, timedelta
from io import BytesIO

import pytest

from autoemail.compliance import (
    RULES_PATH,
    classify_document,
    extract_dates,
    extract_eins,
    load_rule_defs,
    review_case,
    review_files,
)
from autoemail.channels.base import NormalizedAttachment, NormalizedMessage
from autoemail.models import ComplianceRun, Draft
from autoemail.pipeline import process_message


# ---- 规则表加载与校验 ----

def test_rule_defs_load_and_validate():
    rules, version = load_rule_defs()
    assert len(rules) >= 86
    assert version == "1"
    assert rules["D001"].level == "BLOCKER"
    assert rules["D007"].level == "INFO"


def test_rule_defs_reject_duplicate_rule_id(tmp_path):
    lines = RULES_PATH.read_text(encoding="utf-8-sig").splitlines()
    bad = tmp_path / "rule_def.csv"
    bad.write_text("\n".join([lines[0], lines[1], lines[1]]), encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        load_rule_defs(bad)


def test_rule_defs_reject_bad_level(tmp_path):
    lines = RULES_PATH.read_text(encoding="utf-8-sig").splitlines()
    lines[1] = lines[1].replace("BLOCKER", "FATAL")
    bad = tmp_path / "rule_def.csv"
    bad.write_text("\n".join(lines), encoding="utf-8")
    with pytest.raises(ValueError, match="level 非法"):
        load_rule_defs(bad)


# ---- 抽取器 ----

def test_classify_document():
    assert classify_document("cp575.pdf", "EMPLOYER IDENTIFICATION NUMBER") == "EIN_LETTER"
    assert classify_document("passport.pdf", "PASSPORT U.S. passport no. 12345") == "ID"
    assert classify_document("photo.pdf", "here is a photo of our product catalog") == "OTHER"


def test_extract_dates_marks_ambiguous_us_format():
    by_raw = {raw: (parsed, amb) for raw, parsed, amb in extract_dates("on 03/04/2026 and 2026-05-01")}
    assert by_raw["2026-05-01"][1] is False
    assert by_raw["03/04/2026"][1] is True  # 月日均 ≤12 且不等，无法排除日/月互换


def test_extract_eins():
    assert extract_eins("EIN: 12-3456789 (IRS)") == {"12-3456789"}


# ---- 规则判定 ----

def test_review_reject_on_expired_id(session, world):
    world["commission"].driver_license_expiry = "2020-01-01"
    session.commit()
    run = review_case(session, world["case"], doc_texts={})
    ids = {item["rule_id"] for item in run.triggered}
    assert "I002" in ids
    assert run.overall == "REJECT"
    assert run.blocker_count >= 1


def test_review_review_on_near_expiry_id(session, world):
    soon = (date.today() + timedelta(days=60)).isoformat()
    world["commission"].driver_license_expiry = soon
    session.commit()
    run = review_case(session, world["case"], doc_texts={})
    ids = {item["rule_id"] for item in run.triggered}
    assert "I003" in ids
    assert "I002" not in ids


def test_review_reject_on_ein_cross_file_mismatch(session, world):
    assert world["commission"].ein == "12-3456789"
    text = "CBP Form 301\nApplicant EIN: 98-7654321"
    run = review_case(session, world["case"], doc_texts={"form301.pdf": text})
    ids = {item["rule_id"] for item in run.triggered}
    assert "X002" in ids
    assert run.overall == "REJECT"


def test_quote_letter_does_not_trigger_bond_rules(session, world):
    """报价信不是表格：保函参数规则不得误触发（B002 尤其不能把年费当保额）。"""
    text = (
        "A1 Surety is pleased to offer a continuous customs bond "
        "for an annual fee of $500."
    )
    run = review_case(session, world["case"], doc_texts={"quote.pdf": text})
    ids = {item["rule_id"] for item in run.triggered}
    assert "B001" not in ids
    assert "B002" not in ids


def test_review_reject_on_continuous_bond_below_minimum(session, world):
    text = (
        "Surety Bond Application\n"
        "Type: Continuous bond\n"
        "Bond amount: $30,000.00"
    )
    run = review_case(session, world["case"], doc_texts={"application.pdf": text})
    ids = {item["rule_id"] for item in run.triggered}
    assert "B002" in ids
    assert "B001" not in ids  # 类型已认定（CONTINUOUS）
    assert run.overall == "REJECT"


def test_review_lists_unverified_rules(session, world):
    """外部数据源规则（OFAC 等）未接入时显式列 unverified，不得静默算通过。"""
    run = review_case(session, world["case"], doc_texts={})
    assert "U004" in run.unverified
    assert "B006" in run.unverified


def test_disabled_rule_f004_is_skipped(session, world):
    """CSV enabled=0 的规则（F004 日期歧义）不触发、不进 unverified。"""
    rules, _v = load_rule_defs()
    assert rules["F004"].enabled is False
    run = review_case(session, world["case"], doc_texts={"cert.pdf": "filed on 10/05/2026"})
    ids = {item["rule_id"] for item in run.triggered}
    assert "F004" not in ids
    assert "F004" not in run.unverified


# ---- 管线集成：收到附件即审查 ----

def _pdf_bytes(text: str) -> bytes:
    from reportlab.pdfgen import canvas

    buf = BytesIO()
    c = canvas.Canvas(buf)
    for i, line in enumerate(text.splitlines()):
        c.drawString(72, 750 - i * 14, line)
    c.save()
    return buf.getvalue()


def test_pipeline_runs_compliance_on_inbound_pdf(session, world):
    pdf = _pdf_bytes("CBP Form 301\nApplicant EIN: 98-7654321")
    message = NormalizedMessage(
        channel="eml",
        channel_msg_id="compliance-1",
        from_addr="toni@a1surety.com",
        subject="Re: [BOND-2026-0142] Application form",
        body_text="Please find the completed application attached.",
        attachments=[
            NormalizedAttachment(
                filename="application.pdf",
                mime="application/pdf",
                content=pdf,
                sha256="hash-compliance-1",
            )
        ],
        dedupe_hash="compliance-hash-1",
    )
    result = process_message(session, message)
    assert result.case_id == world["case"].id
    run = (
        session.query(ComplianceRun)
        .filter_by(case_id=world["case"].id)
        .order_by(ComplianceRun.id.desc())
        .first()
    )
    assert run is not None
    ids = {item["rule_id"] for item in run.triggered}
    assert "X002" in ids
    assert run.overall == "REJECT"


def test_pipeline_skips_compliance_without_attachments(session, world):
    message = NormalizedMessage(
        channel="eml",
        channel_msg_id="compliance-2",
        from_addr="toni@a1surety.com",
        subject="Re: [BOND-2026-0142] Quote",
        body_text="We are pleased to quote the continuous customs bond for an annual fee of $500.",
        dedupe_hash="compliance-hash-2",
    )
    result = process_message(session, message)
    assert result.draft_id is not None
    runs = session.query(ComplianceRun).filter_by(case_id=world["case"].id).all()
    assert runs == []


# ---- 填表佐证（主动审查）：我方产物发送前过规则 ----

def _blank_form_pdf(tmp_path, name="blank.pdf") -> str:
    """生成一张'空白 onboarding 表'：含 EIN 字样（会被分类为 EIN_LETTER）
    但不含委托公司名——正是体检发现的空表场景。"""
    from reportlab.pdfgen import canvas

    buf = BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 750, "APPLICATION FOR CONTINUOUS CUSTOMS BOND")
    c.drawString(72, 730, "Please complete all fields, sign, and return.")
    c.drawString(72, 710, "Importer name *")
    c.drawString(72, 690, "Federal ID # (EIN / IRS No.) *")
    c.save()
    p = tmp_path / name
    p.write_bytes(buf.getvalue())
    return str(p)


def test_review_files_fill_origin_flags_blank_form(session, world, tmp_path):
    path = _blank_form_pdf(tmp_path)
    run = review_files(session, world["case"], [path], origin="fill")
    assert run is not None
    assert run.origin == "fill"
    ids = {item["rule_id"] for item in run.triggered}
    assert "P005" in ids  # 空白表：公司名未出现在表格文本里


def test_review_files_returns_none_without_files(session, world):
    assert review_files(session, world["case"], [], origin="fill") is None


def test_draft_fill_appends_compliance_notices(session, world, tmp_path, monkeypatch):
    """自动填表产物发送前：合规佐证写入草稿 notices，供人工发送前参考。"""
    pdf_path = _blank_form_pdf(tmp_path, name="filled.pdf")
    monkeypatch.setattr(
        "autoemail.formfill.auto_fill_for_case",
        lambda *a, **k: {"output_path": pdf_path},
    )
    message = NormalizedMessage(
        channel="eml",
        channel_msg_id="fill-review-1",
        from_addr="toni@a1surety.com",
        subject="Re: [BOND-2026-0142] Application",
        body_text="Please return the thoroughly completed and signed documents.",
        attachments=[
            NormalizedAttachment(
                filename="application.pdf",
                mime="application/pdf",
                content=_pdf_bytes("sample application"),
                sha256="hash-fill-review",
            )
        ],
        dedupe_hash="fill-review-hash-1",
    )
    result = process_message(session, message)
    draft = session.get(Draft, result.draft_id)
    assert draft is not None
    assert any("填表合规佐证" in n for n in (draft.notices or []))
    run = (
        session.query(ComplianceRun)
        .filter_by(case_id=world["case"].id, origin="fill")
        .order_by(ComplianceRun.id.desc())
        .first()
    )
    assert run is not None
