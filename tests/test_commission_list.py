"""委托列表 + 飞书链接导入 + 自动询价开关。

- 新客户 = 一份飞书资料文档；粘贴链接自动拉正文、提字段、建档留痕；
- 委托列表页统计所有委托的资料/文件/询价数据；
- 设置页开关控制每日自动询价（daily_inquiry）。
"""

import pytest
from sqlalchemy import select

from autoemail import feishu_import
from autoemail.config import settings
from autoemail.llm import LLMResponse, _hash_input
from autoemail.llm_schemas import LLMCommissionExtractOutput
from autoemail.models import AuditEvent, Commission, CommissionImport
from autoemail import web

DOC_TEXT = """
客户资料：Northwind Trading LLC
Legal Name: Northwind Trading LLC
Entity Type: Corporation
EIN: 98-7654321
State of Formation: CA
Street Address: 888 Harbor Blvd
City: Los Angeles
State: CA
Zip: 90001
Authorized Signer: Li Na
Title: President
Phone: +1 213 555 0188
Email: lina@northwind.example
"""


class ExtractProvider:
    """按 task=commission_extract 返回固定结果的假 LLM。"""

    configured = True
    model_id = "fake-model"

    def __init__(self, response: dict | None = None, error: str | None = None):
        self.response = response
        self.error = error
        self.calls: list[str] = []

    def complete_json(self, *, system_prompt, user_prompt, schema, task):
        self.calls.append(task)
        if self.error:
            return LLMResponse(self.model_id, None, error=self.error)
        parsed = schema.model_validate(self.response)
        return LLMResponse(
            self.model_id, parsed, parsed.model_dump_json(),
            _hash_input(system_prompt, user_prompt, task), latency_ms=1,
        )


def _fetch_ok(monkeypatch, text: str = DOC_TEXT):
    monkeypatch.setattr(
        feishu_import, "fetch_lark_doc_text",
        lambda url: {"kind": "docx", "token": "tok123", "title": "客户资料", "text": text},
    )


# ---- 链接解析与字段提取 ----

def test_parse_lark_url_kinds():
    base = "https://example.feishu.cn"
    assert feishu_import.parse_lark_url(f"{base}/docx/AbC123") == ("docx", "AbC123")
    assert feishu_import.parse_lark_url(f"{base}/wiki/XyZ789") == ("wiki", "XyZ789")
    assert feishu_import.parse_lark_url(f"{base}/sheets/sheet1") == ("sheet", "sheet1")
    assert feishu_import.parse_lark_url(f"{base}/doc/old1") == ("doc", "old1")
    assert feishu_import.parse_lark_url(f"{base}/base/tbl1") == ("base", "tbl1")
    with pytest.raises(ValueError):
        feishu_import.parse_lark_url("not a link")
    with pytest.raises(ValueError):
        feishu_import.parse_lark_url("")


def test_rules_extraction_disambiguates_state():
    fields = feishu_import.extract_fields_by_rules(DOC_TEXT)
    assert fields["legal_name"] == "Northwind Trading LLC"
    assert fields["state_of_inc"] == "CA"  # State of Formation → 注册州
    assert fields["addr_line1"] == "888 Harbor Blvd"
    assert fields["addr_city"] == "Los Angeles"
    assert fields["addr_state"] == "CA"  # 裸 State → 地址州
    assert fields["ein"] == "98-7654321"
    assert fields["signer_name"] == "Li Na"
    assert fields["mailbox_email"] == "lina@northwind.example"


# ---- 导入建档 ----

def test_import_creates_commission_with_llm(session, monkeypatch):
    _fetch_ok(monkeypatch)
    provider = ExtractProvider({
        "legal_name": "Northwind Trading LLC",
        "entity_type": "Corporation",
        "ein": "98-7654321",
        "state_of_inc": "CA",
        "addr_line1": "888 Harbor Blvd",
        "addr_city": "Los Angeles",
        "addr_state": "CA",
        "addr_zip": "90001",
        "signer_name": "Li Na",
        "signer_title": "President",
    })

    record = feishu_import.import_commission_from_feishu(
        session, "https://example.feishu.cn/docx/tok123", reviewer="ops", provider=provider
    )

    assert record.status == "ok"
    assert record.via == "llm"
    commission = session.get(Commission, record.commission_id)
    assert commission.legal_name == "Northwind Trading LLC"
    assert commission.state_of_inc == "CA"
    assert record.missing == []  # 询价必需字段全齐
    assert provider.calls == ["commission_extract"]
    events = session.scalars(
        select(AuditEvent).where(AuditEvent.action == "commission.feishu_import")
    ).all()
    assert len(events) == 1 and events[0].decision == "ok"


def test_import_rules_fallback_when_llm_off(session, monkeypatch):
    _fetch_ok(monkeypatch)

    record = feishu_import.import_commission_from_feishu(
        session, "https://example.feishu.cn/docx/tok123", reviewer="ops", provider=None
    )

    assert record.via == "rules"
    commission = session.get(Commission, record.commission_id)
    assert commission.ein == "98-7654321"


def test_import_llm_error_falls_back_to_rules(session, monkeypatch):
    _fetch_ok(monkeypatch)
    provider = ExtractProvider(error="模型超时")

    record = feishu_import.import_commission_from_feishu(
        session, "https://example.feishu.cn/docx/tok123", reviewer="ops", provider=provider
    )

    assert record.via == "rules"
    assert record.status == "ok"
    commission = session.get(Commission, record.commission_id)
    assert commission.legal_name == "Northwind Trading LLC"


def test_import_fills_blanks_without_overwrite(session, world, monkeypatch):
    _fetch_ok(monkeypatch)
    commission = world["commission"]  # 已有完整人工资料
    original_signer = commission.signer_name
    provider = ExtractProvider({
        "legal_name": commission.legal_name,
        "entity_type": "Corporation",  # 与现值不同 → 不得覆盖
        "ein": "00-0000000",
    })

    record = feishu_import.import_commission_from_feishu(
        session, "https://example.feishu.cn/docx/tok123", reviewer="ops", provider=provider
    )

    session.refresh(commission)
    assert record.commission_id == commission.id
    assert commission.entity_type != "Corporation"  # 不覆盖已有值
    assert commission.signer_name == original_signer


def test_import_failure_without_legal_name(session, monkeypatch):
    _fetch_ok(monkeypatch, text="只是一份没有公司名结构的文档")

    record = feishu_import.import_commission_from_feishu(
        session, "https://example.feishu.cn/docx/tok123", reviewer="ops", provider=None
    )

    assert record.status == "failed"
    assert "公司法定全名" in (record.error or "")
    assert record.commission_id is None
    assert session.scalar(select(Commission).where(Commission.id == record.commission_id)) is None


def test_import_requires_feishu_credentials(session, monkeypatch):
    monkeypatch.setattr(settings, "feishu_app_id", "")
    monkeypatch.setattr(settings, "feishu_app_secret", "")

    with pytest.raises(ValueError) as excinfo:
        feishu_import.import_commission_from_feishu(
            session, "https://example.feishu.cn/docx/tok123", reviewer="ops"
        )

    assert "设置" in str(excinfo.value)
    row = session.scalar(select(CommissionImport))
    assert row is not None and row.status == "failed"


# ---- 资料文件夹导入：整套文件下载入库，硬性三份自动补齐 ----

FOLDER_FILES = [
    "JA Tendo Foods——查重",  # 在线文档（走正文）
    "2026101434 JA Tendo Foods——信息申报.pdf",
    "JA Tendo Foods——EIN.pdf",
    "JA Tendo Foods——SSN.txt",
    "JA Tendo Foods——地址查验.png",
    "JA Tendo Foods——地址街景.png",
    "JA Tendo Foods——法人照正面.jpg",
    "JA Tendo Foods——法人照精面.jpg",
    "JA Tendo Foods——法人照片.jpg",
    "JA Tendo Foods——房产信息.txt",
    "JA Tendo Foods——公司章程.pdf",
    "JA Tendo Foods——官网地址.txt",
    "JA Tendo Foods——客户需求.pdf",
    "JA Tendo Foods——申请通知书Business Entity Filing Acknowledgment.pdf",
]


def test_parse_folder_link():
    url = "https://example.feishu.cn/drive/folder/fldcnABC123"
    assert feishu_import.parse_lark_url(url) == ("folder", "fldcnABC123")
    assert feishu_import.parse_lark_url("https://example.feishu.cn/folder/xyz") == ("folder", "xyz")


def test_classify_filenames():
    classify = feishu_import.classify_filename
    assert classify("JA Tendo Foods——公司章程.pdf") == "articles"
    assert classify("JA Tendo Foods——EIN.pdf") == "ein"
    assert classify("JA Tendo Foods——SSN.txt") == "ssn"
    assert classify("JA Tendo Foods——法人照正面.jpg") == "driver_license_front"
    assert classify("JA Tendo Foods——法人照精面.jpg") == "driver_license_back"
    assert classify("JA Tendo Foods——法人照片.jpg") == "photo"
    assert classify("JA Tendo Foods——地址查验.png") == "address_verification"
    assert classify("JA Tendo Foods——地址街景.png") == "street_view"
    assert classify("JA Tendo Foods——房产信息.txt") == "property_info"
    assert classify("JA Tendo Foods——官网地址.txt") == "website"
    assert classify("JA Tendo Foods——客户需求.pdf") == "client_application"
    assert classify("JA Tendo Foods——信息申报.pdf") == "statement_of_information"
    assert classify("JA Tendo Foods——申请通知书Filing Acknowledgment.pdf") == "filing_acknowledgment"
    assert classify("readme.md") == "other"


def test_folder_import_end_to_end(session, monkeypatch):
    """文件夹链接：全部文件入库 CommissionFile，硬性三份进 CommissionDocument。"""

    def fake_fetch(url):
        return {
            "kind": "folder",
            "token": "fldcnABC",
            "files": [
                {"name": name, "kind": "file", "content": f"bytes-{name}".encode()}
                for name in FOLDER_FILES
                if not name.endswith("查重")
            ],
            "texts": "【JA Tendo Foods——查重】\nLegal Name: JA Tendo Foods LLC\nState of Formation: WA",
            "doc_texts": [
                {"name": "JA Tendo Foods——查重", "text": "Legal Name: JA Tendo Foods LLC\nState of Formation: WA"}
            ],
        }

    monkeypatch.setattr(feishu_import, "fetch_lark_folder_files", fake_fetch)

    record = feishu_import.import_commission_from_feishu(
        session, "https://example.feishu.cn/drive/folder/fldcnABC", reviewer="ops"
    )

    assert record.status == "ok" and record.doc_kind == "folder"
    commission = session.get(Commission, record.commission_id)
    assert commission.legal_name == "JA Tendo Foods LLC"  # 查重文档正文里的 Legal Name
    assert commission.state_of_inc == "WA"  # State of Formation → 注册州

    from autoemail.models import CommissionDocument, CommissionFile

    files = session.scalars(select(CommissionFile).where(CommissionFile.commission_id == commission.id)).all()
    assert len(files) == len(FOLDER_FILES) - 1  # 在线文档不算文件材料
    docs = {doc.doc_type for doc in session.scalars(
        select(CommissionDocument).where(CommissionDocument.commission_id == commission.id)
    )}
    assert docs == {"articles", "ein", "id"}  # 硬性三份自动补齐
    # 法人证件 = 驾照正面（不是法人照片）
    id_doc = session.scalar(
        select(CommissionDocument).where(
            CommissionDocument.commission_id == commission.id,
            CommissionDocument.doc_type == "id",
        )
    )
    assert "正面" in id_doc.filename


def test_folder_import_routes_from_generic_entry(session, monkeypatch):
    """同一个输入框粘贴文件夹链接也走文件夹流程。"""
    monkeypatch.setattr(
        feishu_import, "fetch_lark_folder_files",
        lambda url: {
            "kind": "folder", "token": "t", "files": [],
            "texts": "Legal Name: Folder Flow Co\nEntity Type: LLC",
            "doc_texts": [{"name": "查重", "text": "Legal Name: Folder Flow Co\nEntity Type: LLC"}],
        },
    )

    record = feishu_import.import_commission_from_feishu(
        session, "https://example.feishu.cn/drive/folder/tok", reviewer="ops"
    )

    assert record.status == "ok"
    commission = session.get(Commission, record.commission_id)
    assert commission.legal_name == "Folder Flow Co"


# ---- 公司专属知识库：材料正文解析入库 ----

def test_folder_import_builds_knowledge_base(session, monkeypatch):
    """txt 直接解码、PDF 走 extract_pdf_text，全部进 CommissionKnowledge；
    客户需求 PDF 另走既有 intake 解析（CommissionIntake）。"""
    import autoemail.formfill as formfill
    from autoemail.models import CommissionIntake, CommissionKnowledge

    def fake_extract_file_text(name: str, content: bytes) -> str:
        if name.endswith(".txt"):
            return content.decode("utf-8", errors="replace")
        if name.endswith(".pdf"):
            return "CUSTOMS BOND CLIENT APPLICATION\nLegal Name: JA Tendo Foods LLC"
        return ""  # 图片无文字

    monkeypatch.setattr(feishu_import, "_extract_file_text", fake_extract_file_text)
    monkeypatch.setattr(
        formfill, "extract_client_intake",
        lambda pdf_bytes: {"b001_legal_name": "JA Tendo Foods LLC"},
    )
    monkeypatch.setattr(
        feishu_import, "fetch_lark_folder_files",
        lambda url: {
            "kind": "folder", "token": "t", "doc_texts": [], "texts": "",
            "files": [
                {"name": "JA Tendo Foods——客户需求.pdf", "kind": "file", "content": b"%PDF-fake"},
                {"name": "JA Tendo Foods——SSN.txt", "kind": "file", "content": "SSN: 123-45-6789".encode()},
                {"name": "JA Tendo Foods——地址查验.png", "kind": "file", "content": b"\x89PNG"},
            ],
        },
    )

    record = feishu_import.import_commission_from_feishu(
        session, "https://example.feishu.cn/drive/folder/t", reviewer="ops"
    )

    assert record.status == "ok"
    commission = session.get(Commission, record.commission_id)
    rows = session.scalars(
        select(CommissionKnowledge).where(CommissionKnowledge.commission_id == commission.id)
    ).all()
    sources = {row.source for row in rows}
    assert "JA Tendo Foods——客户需求.pdf" in sources
    assert "JA Tendo Foods——SSN.txt" in sources
    assert "JA Tendo Foods——地址查验.png" not in sources  # 图片正文为空不入库
    intake = session.scalar(
        select(CommissionIntake).where(CommissionIntake.commission_id == commission.id)
    )
    assert intake is not None and intake.fields  # 客户需求表已结构化


def test_knowledge_upsert_no_duplicate_on_reimport(session, monkeypatch):
    from autoemail.models import CommissionKnowledge

    payload = {
        "kind": "folder", "token": "t", "doc_texts": [],
        "texts": "",
        "files": [{"name": "Co A——官网地址.txt", "kind": "file", "content": "Website: https://a.example".encode()}],
    }
    monkeypatch.setattr(feishu_import, "fetch_lark_folder_files", lambda url: dict(payload))
    url = "https://example.feishu.cn/drive/folder/t"

    feishu_import.import_commission_from_feishu(session, url, reviewer="ops")
    payload["files"][0]["content"] = "Website: https://a-new.example".encode()
    feishu_import.import_commission_from_feishu(session, url, reviewer="ops")

    commission = session.scalar(select(Commission).where(Commission.legal_name == "Co A"))
    rows = session.scalars(
        select(CommissionKnowledge).where(CommissionKnowledge.commission_id == commission.id)
    ).all()
    assert len(rows) == 1  # 同来源覆盖，不重复
    assert "a-new.example" in rows[0].text


# ---- web 层：委托列表页 / 导入动作 / 设置开关 ----

def test_render_commissions_lists_stats_and_link(session, world, monkeypatch):
    monkeypatch.setattr(settings, "daily_inquiry_enabled", True)
    session.add(CommissionImport(
        commission_id=world["commission"].id,
        source_url="https://example.feishu.cn/docx/tok123",
        doc_kind="docx",
        doc_title="客户资料",
        fields={"legal_name": world["commission"].legal_name},
        filled=["ein"],
        missing=[],
        status="ok",
    ))
    session.commit()

    html = web.render_commissions(session)

    assert "委托列表" in html
    assert "粘贴飞书链接" in html
    assert 'action="/commissions/feishu"' in html
    assert world["commission"].legal_name in html
    assert "https://example.feishu.cn/docx/tok123" in html
    assert "自动询价" in html and "已开启" in html


def test_perform_import_feishu_via_web(session, monkeypatch):
    _fetch_ok(monkeypatch)

    kind, message = web.perform_import_feishu(
        session,
        {"feishu_url": "https://example.feishu.cn/docx/tok123", "reviewer": "ops"},
        provider=ExtractProvider({"legal_name": "Northwind Trading LLC"}),
    )

    assert kind == "ok"
    assert "已从飞书文档导入" in message
    commission = session.scalar(select(Commission).where(Commission.legal_name == "Northwind Trading LLC"))
    assert commission is not None

    kind2, message2 = web.perform_import_feishu(session, {"feishu_url": "x", "reviewer": ""})
    assert kind2 == "error" and "操作人" in message2


def test_settings_auto_inquiry_toggle_writes_env(monkeypatch, tmp_path, session):
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setattr(settings, "imap_password", "")
    monkeypatch.setattr(settings, "smtp_password", "")
    monkeypatch.setattr(settings, "feishu_app_secret", "")
    # perform_settings 会直接改全局 settings；先经 monkeypatch 登记以便用例后还原
    monkeypatch.setattr(settings, "daily_inquiry_enabled", True)
    monkeypatch.setattr(settings, "daily_inquiry_batch_size", 20)
    monkeypatch.setattr(settings, "feishu_app_id", "")

    form = {
        "reviewer": "ops",
        "smtp_port": "587",
        "daily_inquiry_enabled": "on",
        "daily_inquiry_batch_size": "7",
        "feishu_app_id": "cli_123",
        "feishu_app_secret": "sec-abc",
    }
    kind, _ = web.perform_settings(session, form, env_path=str(env))
    assert kind == "ok"
    assert settings.daily_inquiry_enabled is True
    assert settings.daily_inquiry_batch_size == 7
    assert settings.feishu_app_id == "cli_123"
    assert settings.feishu_app_secret == "sec-abc"
    text = env.read_text(encoding="utf-8")
    assert "AUTOEMAIL_DAILY_INQUIRY_ENABLED=true" in text
    assert "AUTOEMAIL_DAILY_INQUIRY_BATCH_SIZE=7" in text
    assert "AUTOEMAIL_FEISHU_APP_SECRET=sec-abc" in text

    # 不勾选 → 关闭；secret 留空保持不变
    form2 = {"reviewer": "ops", "smtp_port": "587", "daily_inquiry_batch_size": "20"}
    web.perform_settings(session, form2, env_path=str(env))
    assert settings.daily_inquiry_enabled is False
    assert settings.feishu_app_secret == "sec-abc"
    assert "AUTOEMAIL_DAILY_INQUIRY_ENABLED=false" in env.read_text(encoding="utf-8")


def test_settings_rejects_bad_batch_size(tmp_path, session):
    env = tmp_path / ".env"
    kind, message = web.perform_settings(
        session,
        {"reviewer": "ops", "smtp_port": "587", "daily_inquiry_batch_size": "abc"},
        env_path=str(env),
    )
    assert kind == "error" and "数字" in message

    kind2, message2 = web.perform_settings(
        session,
        {"reviewer": "ops", "smtp_port": "587", "daily_inquiry_batch_size": "999"},
        env_path=str(env),
    )
    assert kind2 == "error" and "1~200" in message2


def test_render_settings_shows_auto_inquiry_and_feishu(monkeypatch):
    monkeypatch.setattr(settings, "daily_inquiry_enabled", True)
    monkeypatch.setattr(settings, "daily_inquiry_batch_size", 12)

    page = web.render_settings()

    assert 'name="daily_inquiry_enabled"' in page and "checked" in page
    assert 'name="daily_inquiry_batch_size"' in page and 'value="12"' in page
    assert 'name="feishu_app_id"' in page
    assert 'name="feishu_app_secret"' in page
    assert "sec-abc" not in page
