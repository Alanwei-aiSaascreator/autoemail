"""财务材料自动响应测试：个人/企业财务报表生成、股权与 AD/CVD 标准口径。"""

import pdfplumber
import pytest
from pypdf import PdfReader

from autoemail import financials
from autoemail.models import Commission, SignatureAsset
from autoemail.textutil import normalize_text


def _commission(session):
    commission = Commission(
        legal_name="Sunrise Home Goods LLC",
        legal_name_norm=normalize_text("Sunrise Home Goods LLC"),
        entity_type="LLC",
        ein="12-3456789",
        addr_line1="1200 N Market St",
        addr_city="Wilmington",
        addr_state="DE",
        addr_zip="19801",
        principal_full_name="Wei Zhang",
        signer_title="Manager",
    )
    session.add(commission)
    session.flush()
    return commission


def test_ownership_answer_100_percent_legal_rep(session):
    commission = _commission(session)
    answer = financials.ownership_answer(commission)
    assert "Wei Zhang" in answer
    assert "100%" in answer
    assert "Sunrise Home Goods LLC" in answer
    # 正面回答"是否有多位股东"：明确只有一位 owner
    assert "only one owner" in answer
    assert "no other owners" in answer


def test_ad_cvd_answer_follows_intake(session, monkeypatch):
    monkeypatch.setattr(financials, "lookup_hts", lambda code: None)
    no_answer = financials.ad_cvd_answer({"a007": "No"})
    assert no_answer.startswith("No.")
    assert "are not subject" in no_answer
    yes_answer = financials.ad_cvd_answer({"a007": "Yes"})
    assert yes_answer.startswith("Yes.")
    assert "are subject to" in yes_answer


def test_ad_cvd_answer_includes_hts_verification(session, monkeypatch):
    monkeypatch.setattr(
        financials,
        "lookup_hts",
        lambda code: {"htsno": code, "description": "Vacuum flasks", "general": "7.2%"},
    )
    answer = financials.ad_cvd_answer({"a007": "No", "d003": "9617.00.19.00"})
    assert "USITC" in answer and "9617.00.19.00" in answer


def test_lookup_hts_returns_none_on_network_failure(monkeypatch):
    import httpx

    def raise_timeout(*args, **kwargs):
        raise httpx.ConnectTimeout("offline")

    monkeypatch.setattr(httpx, "get", raise_timeout)
    assert financials.lookup_hts("9617.00.19.00") is None


def test_lookup_hts_matches_nearest_heading(monkeypatch):
    """10 位统计码在公开税则库只到 8 位：应命中公共前缀最长的标题条目。"""
    import httpx

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {"htsno": "9617.00", "description": "Vacuum flasks", "general": ""},
                {"htsno": "9617.00.10.00", "description": "Not exceeding 1 liter", "general": "7.2%"},
                {"htsno": "9617.00.30.00", "description": "1-2 liters", "general": "6.9%"},
            ]

    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured["params"] = params
        return FakeResp()

    monkeypatch.setattr(httpx, "get", fake_get)
    info = financials.lookup_hts("9617.00.19.00")
    # 查询区间用标题段（9617.00），不是截断错误的前缀
    assert captured["params"]["from"] == "9617.00"
    assert info is not None
    assert info["htsno"] == "9617.00.10.00"
    assert info["general"] == "7.2%"


def test_business_financial_pdf_generation(session, world, tmp_path, monkeypatch):
    monkeypatch.setattr(financials, "settings_out_dir", lambda: str(tmp_path))
    path = financials.business_financial_pdf(world["case"], world["commission"])
    text = pdfplumber.open(path).pages[0].extract_text()
    assert "Sunrise Home Goods LLC" in text
    assert "TOTAL ASSETS" in text and "NET INCOME" in text


def test_personal_financial_pdf_filled_and_signed(session, world, tmp_path, monkeypatch):
    import io

    from PIL import Image

    monkeypatch.setattr(financials, "settings_out_dir", lambda: str(tmp_path))
    commission = world["commission"]
    commission.principal_full_name = "Wei Zhang"
    session.flush()
    buf = io.BytesIO()
    Image.new("RGBA", (80, 24), (10, 10, 10, 255)).save(buf, "PNG")
    session.add(
        SignatureAsset(
            commission_id=world["commission"].id,
            filename="sig.png",
            sha256="hash-sig",
            authorization_note="client upload",
            content=buf.getvalue(),
        )
    )
    session.flush()
    paths, ok_keys, _notes = financials.generate_financial_attachments(
        session, world["case"], world["commission"], ["personal_financial"]
    )
    assert ok_keys == ["personal_financial"]
    reader = PdfReader(paths[0])
    values = {k: f.get("/V") for k, f in (reader.get_fields() or {}).items()}
    assert values["p002"] == "Wei Zhang"
    assert values["p003"] == "Sunrise Home Goods LLC"
    assert values["as13"] == "987,000"
    assert values["li12"] == "653,700"
    assert values["q011"] == "Wei Zhang"
    # SSN/出生日期留空：需经安全渠道单独提交，系统绝不编造
    assert values.get("p012") in (None, "")
    assert values.get("p007") in (None, "")


def test_generate_financial_attachments_failure_is_reported(session, world, tmp_path, monkeypatch):
    monkeypatch.setattr(financials, "settings_out_dir", lambda: str(tmp_path))
    monkeypatch.setattr(financials, "TEMPLATE_PATH", tmp_path / "missing.pdf")
    _paths, ok_keys, notes = financials.generate_financial_attachments(
        session, world["case"], world["commission"], ["personal_financial"]
    )
    assert ok_keys == []
    assert any("人工处理" in n for n in notes)
