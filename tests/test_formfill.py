"""字段映射配置化填表测试：映射解析、配置一致性与 auto_fill_form 编排。"""

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from autoemail import formfill
from autoemail.channels.base import NormalizedAttachment, NormalizedMessage
from autoemail.formfill import build_field_values, load_mapping, resolve_value
from autoemail.models import AuditEvent, Draft
from autoemail.router import MAPPINGS_PATH, FORMAT_LIBRARY, route_form

FIXTURES = Path(__file__).parent / "fixtures"
AGENT_BLANK = FIXTURES / "bond_application_blank.pdf"
CLIENT_TEMPLATE = FIXTURES / "client_bond_application.pdf"


# ---- 映射规则解析 ----

DATA = {
    "legal_name": "Sunrise Home Goods LLC",
    "ein": "12-3456789",
    "addr_line1": "1200 N Market St",
    "addr_city": "Wilmington",
    "addr_state": "DE",
    "addr_zip": "19801",
    "mailbox_email": "sunrise@example.com",
    "principal_full_name": "Wei Zhang",
    "signer_title": "Manager",
}


def test_resolve_direct_column():
    value, skip = resolve_value("ein", DATA)
    assert skip is None
    assert value == "12-3456789"


def test_resolve_missing_column_skipped():
    value, skip = resolve_value("dba", DATA)
    assert value is None
    assert "dba" in skip


def test_resolve_const_and_today():
    assert resolve_value({"const": "Yes"}, DATA) == ("Yes", None)
    value, skip = resolve_value({"today": "%m/%d/%Y"}, DATA)
    assert skip is None
    assert len(value.split("/")) == 3


def test_resolve_template_joins_archive_fields():
    spec = {"template": "{addr_line1}, {addr_city}, {addr_state} {addr_zip}"}
    value, skip = resolve_value(spec, DATA)
    assert skip is None
    assert value == "1200 N Market St, Wilmington, DE 19801"


def test_resolve_template_missing_field_skips():
    value, skip = resolve_value({"template": "{legal_name} {ein}"}, {"legal_name": "X LLC"})
    assert value is None
    assert "ein" in skip


def test_resolve_unknown_rule_skipped():
    value, skip = resolve_value({"regex": "x"}, DATA)
    assert value is None and skip


def test_resolve_client_rule():
    data = {**DATA, "client_b001": "ACME LLC", "client_d019": "150000"}
    value, skip = resolve_value({"client": "b001"}, data)
    assert skip is None
    assert value == "ACME LLC"
    value2, _ = resolve_value({"client": "d019"}, data)
    assert value2 == "150000"


def test_resolve_client_rule_missing_skipped():
    value, skip = resolve_value({"client": "d019"}, DATA)
    assert value is None
    assert "d019" in skip


def test_resolve_template_references_client_field():
    data = {**DATA, "client_d001": "Industrial Seals", "client_d002": "Rubber Gaskets"}
    value, skip = resolve_value({"template": "{client_d001}: {client_d002}"}, data)
    assert skip is None
    assert value == "Industrial Seals: Rubber Gaskets"


def test_build_field_values_splits_filled_and_skipped():
    mapping = {
        "Company Name": "legal_name",
        "TIN": "ein",
        "DBAs": "dba",
        "New Bond Application": {"const": "Yes"},
        "Print Name and Title": {"template": "{principal_full_name}, {signer_title}"},
    }
    values, skipped = build_field_values(mapping, DATA)
    assert values == {
        "Company Name": "Sunrise Home Goods LLC",
        "TIN": "12-3456789",
        "New Bond Application": "Yes",
        "Print Name and Title": "Wei Zhang, Manager",
    }
    assert set(skipped) == {"DBAs"}


# ---- 配置一致性 ----

def test_mapping_config_loadable_and_consistent():
    assert MAPPINGS_PATH.exists()
    config = json.loads(MAPPINGS_PATH.read_text(encoding="utf-8"))
    formats = config.get("formats", [])
    assert formats, "配置文件必须至少登记一个格式"

    from autoemail.models import Commission

    archive_columns = {c.name for c in Commission.__table__.columns}
    for item in formats:
        assert item["format_id"] and item["type_id"]
        assert len(item.get("field_names", [])) > 0, "格式指纹不能为空"
        for pdf_field, spec in item.get("field_mapping", {}).items():
            assert pdf_field, "映射键不能为空"
            if isinstance(spec, str):
                assert spec in archive_columns, f"{pdf_field} 引用的档案列 {spec} 不存在"
            elif isinstance(spec, dict):
                assert set(spec) & {"const", "today", "template", "client"}, f"{pdf_field} 规则不合法: {spec}"
                if "template" in spec:
                    for placeholder in _placeholders(spec["template"]):
                        # client_ 前缀占位符来自客户需求采集表，非档案列
                        if placeholder.startswith("client_"):
                            continue
                        assert placeholder in archive_columns, (
                            f"{pdf_field} 模板引用的档案列 {placeholder} 不存在"
                        )


def _placeholders(template: str) -> list[str]:
    import string

    return [fn for _, fn, _, _ in string.Formatter().parse(template) if fn]


def test_router_loads_formats_from_config():
    ids = {fmt.format_id for fmt in FORMAT_LIBRARY}
    assert "customs_bond_v1" in ids


def test_load_mapping():
    mapping = load_mapping("customs_bond_v1")
    assert mapping is not None
    assert "Company Name" in mapping
    assert load_mapping("no_such_format") is None


# ---- auto_fill_form 编排（monkeypatch 路由与档案，不依赖 LLM 与真实库） ----

@pytest.fixture()
def _patched_flow(monkeypatch):
    monkeypatch.setattr(
        formfill,
        "route_form",
        lambda pdf_bytes: {
            "type": "customs_bond_application",
            "format": "customs_bond_v1",
            "scores": {},
            "matched": True,
        },
    )
    mapping = load_mapping("customs_bond_v1")
    monkeypatch.setattr(formfill, "load_mapping", lambda format_id, path=None: mapping)


def test_auto_fill_form_route_miss(tmp_path, monkeypatch):
    monkeypatch.setattr(
        formfill, "route_form", lambda pdf_bytes: {"matched": False}
    )
    report = formfill.auto_fill_form(b"%PDF-fake", "Sunrise", str(tmp_path / "out.pdf"))
    assert report["routed"] is False and report["error"]


def test_auto_fill_form_no_format(tmp_path, monkeypatch):
    monkeypatch.setattr(
        formfill,
        "route_form",
        lambda pdf_bytes: {"type": "personal_financial_statement", "format": None, "matched": True},
    )
    report = formfill.auto_fill_form(b"%PDF-fake", "Sunrise", str(tmp_path / "out.pdf"))
    assert report["routed"] is True
    assert "未配置" in report["error"] or "标注" in report["error"]


def test_auto_fill_form_end_to_end(tmp_path, monkeypatch, _patched_flow):
    """路由命中 + 档案齐全 → 报告含已填字段；未映射字段单独列出。"""
    commission = dict(DATA)
    monkeypatch.setattr(formfill, "get_commission_data", lambda name: commission)

    # 伪造 fill 与字段列表：只验证编排逻辑，不依赖真实 PDF
    class FakeFillResult:
        output_path = "out.pdf"
        filled = ["Company Name", "TIN"]
        unknown_fields = []
        total_fields = 3

    monkeypatch.setattr(
        "autoemail.toolkit._fill_pdf_form", lambda **kwargs: FakeFillResult()
    )
    monkeypatch.setattr(
        "autoemail.toolkit._list_pdf_fields",
        lambda **kwargs: {"named_fields": ["Company Name", "TIN", "Bond Amount Requested"]},
    )

    report = formfill.auto_fill_form(b"%PDF-fake", "Sunrise", "out.pdf")
    assert report["routed"] is True
    assert report["form_format"] == "customs_bond_v1"
    assert report["filled"] == ["Company Name", "TIN"]
    assert "Bond Amount Requested" in report["unmapped_fields"]


def test_auto_fill_form_commission_not_found(tmp_path, monkeypatch, _patched_flow):
    monkeypatch.setattr(
        formfill, "get_commission_data", lambda name: {"error": "找不到委托公司：Ghost"}
    )
    report = formfill.auto_fill_form(b"%PDF-fake", "Ghost", str(tmp_path / "o.pdf"))
    assert report["error"] == "找不到委托公司：Ghost"


# ---- role 支持：指纹短路路由 + 客户模板不代填 ----

def test_route_form_fingerprint_short_circuit(monkeypatch):
    """代理商保函表（空白夹具）直接命中指纹，不经过 LLM rerank。"""

    def _no_llm(*args, **kwargs):
        raise AssertionError("指纹命中后不应调用 LLM rerank")

    monkeypatch.setattr("autoemail.router.rerank_types", _no_llm)
    route = route_form(AGENT_BLANK.read_bytes())
    assert route["matched"] is True
    assert route["via"] == "fingerprint"
    assert route["type"] == "customs_bond_application"
    assert route["format"] == "customs_bond_v1"
    assert route["role"] == "agent_inbound"


def test_client_template_routes_but_never_filled(tmp_path, monkeypatch):
    """客户版需求采集表：路由可识别（client_fill_template），但系统绝不代填。"""

    def _no_fill(**kwargs):
        raise AssertionError("客户自填模板不允许被填充")

    monkeypatch.setattr("autoemail.toolkit._fill_pdf_form", _no_fill)
    route = route_form(CLIENT_TEMPLATE.read_bytes())
    assert route["matched"] is True
    assert route["role"] == "client_fill_template"
    assert route["format"] == "client_bond_application_v1"

    report = formfill.auto_fill_form(
        CLIENT_TEMPLATE.read_bytes(), "Sunrise Home Goods LLC", str(tmp_path / "x.pdf")
    )
    assert report["routed"] is True
    assert report["no_fill"] is True
    assert not (tmp_path / "x.pdf").exists()


def test_auto_fill_form_full_real_fixture(tmp_path, monkeypatch):
    """空白代理商保函表 + 档案数据 → 落盘 PDF 可读回正确值（纯确定性，无 LLM）。"""
    monkeypatch.setattr(formfill, "get_commission_data", lambda name: dict(DATA))
    out = tmp_path / "filled.pdf"
    report = formfill.auto_fill_form(AGENT_BLANK.read_bytes(), "Sunrise", str(out))

    assert report.get("error") is None
    assert out.exists()
    assert "Company Name" in report["filled"]
    assert report["skipped"]["DBAs"] == "档案字段 dba 为空"
    assert not report["unmapped_fields"] or "Company Name" not in report["unmapped_fields"]

    from pypdf import PdfReader

    fields = PdfReader(str(out)).get_fields()
    assert fields["Company Name"].get("/V") == "Sunrise Home Goods LLC"
    # 修复点：TIN/SSN/CBP 栏 = 公司名 + EIN
    assert fields["Name TINSSNCBP"].get("/V") == "Sunrise Home Goods LLC 12-3456789"
    assert fields["New Bond Application"].get("/V") == "/On"
    assert fields["Address City State Zip"].get("/V") == (
        "1200 N Market St, Wilmington, DE 19801"
    )


# ---- 流水线接入：form_request 附件自动填表随草稿送审 ----

def _form_request_message(attachment: NormalizedAttachment | None) -> NormalizedMessage:
    return NormalizedMessage(
        channel="eml",
        channel_msg_id="form-pipeline-1",
        from_addr="toni@a1surety.com",
        subject="Re: [BOND-2026-0142] Application",
        body_text="Please complete the application form.",
        attachments=[attachment] if attachment else [],
        dedupe_hash="form-pipeline-hash",
    )


def test_form_request_attachment_auto_fills_into_draft(session, world, monkeypatch, tmp_path):
    monkeypatch.setattr("autoemail.config.settings.filled_forms_dir", str(tmp_path))
    attachment = NormalizedAttachment(
        filename="bond_application.pdf",
        mime="application/pdf",
        content=AGENT_BLANK.read_bytes(),
        sha256="hash-agent-form",
    )
    from autoemail.pipeline import process_message

    result = process_message(session, _form_request_message(attachment))
    assert result.intent.label == "form_request"
    draft = session.get(Draft, result.draft_id)
    # 1 份填表产物 + 公司章程 + 法人证件（Photo ID）两份支持文件原件
    assert len(draft.attachments) == 3
    produced = Path(draft.attachments[0])
    assert produced.exists()
    assert produced.name.startswith("BOND-2026-0142_customs_bond_v1")
    assert [Path(p).name for p in draft.attachments[1:]] == [
        "BOND-2026-0142_supporting_articles.pdf",
        "BOND-2026-0142_supporting_id.pdf",
    ]
    assert "attached" in draft.body
    assert any("已自动填表并附上" in n for n in draft.notices)
    assert any("已附上支持文件原件" in n for n in draft.notices)

    events = session.scalars(
        select(AuditEvent).where(AuditEvent.action == "form.auto_fill")
    ).all()
    assert len(events) == 1 and events[0].decision == "filled"


def test_form_request_client_template_not_filled(session, world, monkeypatch, tmp_path):
    monkeypatch.setattr("autoemail.config.settings.filled_forms_dir", str(tmp_path))
    attachment = NormalizedAttachment(
        filename="Customs_Bond_Client_Application_Fillable.pdf",
        mime="application/pdf",
        content=CLIENT_TEMPLATE.read_bytes(),
        sha256="hash-client-form",
    )
    from autoemail.pipeline import process_message

    result = process_message(session, _form_request_message(attachment))
    draft = session.get(Draft, result.draft_id)
    assert draft.attachments == []
    assert "attached" not in draft.body
    assert list(tmp_path.iterdir()) == []


# ---- 平铺表：配置驱动、逐格填写、条件勾选、多页渲染 ----


def _flat_data(**overrides):
    data = {
        **DATA,
        "client_a004": "50,000",
        "client_a006": "2026-11-22",
        "client_a007": "No",
        "client_b005": "B20260442134",
        "client_b011": "12345678901",
        "client_d001": "Household kitchen storage products",
    }
    data.update(overrides)
    return data


def _flat_template():
    return formfill.load_flat_templates()["bond_exchange_flat_v1"]


def test_flat_template_builds_bond_exchange_layout():
    fields, skipped = formfill.build_flat_fields(_flat_template(), _flat_data())
    assert skipped == {}
    texts = [f["text"] for f in fields if f.get("text")]
    checks = [f for f in fields if f.get("check")]
    clears = [f for f in fields if f.get("clear")]

    assert "Sunrise Home Goods LLC" in texts
    assert "Household kitchen storage products" in texts
    assert "B20260442134" not in texts
    fein = [f for f in fields if f.get("y") == 684]
    assert [f["text"] for f in fein] == list("123456789")
    assert len(fein) == 9
    assert not [f for f in fields if f.get("y") == 644], "有 FEIN 时 Customs 栏必须留空"
    assert len(checks) == 2 and all(f.get("rect") for f in checks)
    assert clears == [{"page": 0, "clear": [445.0, 489.0, 452.5, 495.5]}]

    city = next(f for f in fields if f.get("text") == "Wilmington")
    state = next(f for f in fields if f.get("text") == "DE")
    zip_ = next(f for f in fields if f.get("text") == "19801")
    assert 17.9 < city["x"] < 229.1
    assert 229.1 < state["x"] < 264.8
    assert 264.8 < zip_["x"] < 370.4
    assert city["y"] == state["y"] == zip_["y"] == 634


def test_flat_template_conditional_ad_cvd_check():
    fields, _ = formfill.build_flat_fields(_flat_template(), _flat_data(client_a007="Yes"))
    checks = [f for f in fields if f.get("check")]
    assert len(checks) == 3
    assert checks[-1]["rect"] == [368.6, 526.7, 373.8, 531.8]
    assert checks[-1]["size"] == 5.5


def test_flat_template_uses_cbp_number_only_without_fein():
    data = _flat_data()
    data.pop("ein")
    fields, skipped = formfill.build_flat_fields(_flat_template(), data)
    customs = [f for f in fields if f.get("y") == 644]
    assert [f["text"] for f in customs] == list("12345678901")
    assert all(f["align"] == "center" for f in customs)
    assert all(f["x"] != 492 for f in customs), "第 7 格是原表印刷横线，不能覆盖"
    assert "fein" in skipped


def test_flat_template_rejects_wrong_char_count_without_partial_fill():
    fields, skipped = formfill.build_flat_fields(_flat_template(), _flat_data(ein="123"))
    assert "需要 9 位" in skipped["fein"]
    assert not [f for f in fields if f.get("y") == 684]


def _pdf_with_pages(sizes: list[tuple[int, int]], text: str = "") -> bytes:
    import io

    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for width, height in sizes:
        c.setPageSize((width, height))
        if text:
            c.drawString(30, height - 40, text)
        c.showPage()
    c.save()
    return buf.getvalue()


def test_flat_detection_requires_matching_page_geometry():
    content = _pdf_with_pages([(612, 792)], "The Bond Exchange / Customs Bond Application")
    assert formfill._detect_flat_form(content) == "bond_exchange_flat_v1"
    wrong_size = _pdf_with_pages([(600, 792)], "The Bond Exchange / Customs Bond Application")
    assert formfill._detect_flat_form(wrong_size) is None


def test_flat_renderer_supports_multiple_pages_and_rejects_bad_page(tmp_path):
    from pypdf import PdfReader

    source = _pdf_with_pages([(612, 792), (612, 792)])
    output = tmp_path / "filled.pdf"
    formfill.fill_flat_pdf(
        source,
        [
            {"page": 1, "x": 50, "y": 700, "text": "SECOND PAGE"},
            {"page": 1, "rect": [50, 650, 60, 660], "check": True, "size": 10},
            {"page": 0, "clear": [50, 600, 60, 610]},
        ],
        str(output),
    )
    reader = PdfReader(output)
    assert "SECOND PAGE" not in (reader.pages[0].extract_text() or "")
    second_text = reader.pages[1].extract_text() or ""
    assert "SECOND PAGE" in second_text and "X" in second_text

    with pytest.raises(ValueError, match="页码越界"):
        formfill.fill_flat_pdf(
            source, [{"page": 2, "x": 1, "y": 1, "text": "bad"}], str(output)
        )


def test_save_formats_preserves_flat_templates(tmp_path, monkeypatch):
    from autoemail import router

    path = tmp_path / "form_mappings.json"
    flat = {"format_id": "flat_v1", "keywords": ["example"], "fields": []}
    path.write_text(json.dumps({"formats": [], "flat_forms": [flat]}), encoding="utf-8")
    monkeypatch.setattr(router, "FORMAT_LIBRARY", [])
    router.save_formats(path)
    assert json.loads(path.read_text(encoding="utf-8"))["flat_forms"] == [flat]
