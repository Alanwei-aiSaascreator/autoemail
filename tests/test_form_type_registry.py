"""新类型表单识别与储备：类型 ≠ 格式。

- 全新类型（没见过的字段结构）→ FormTypeRegistry 登记 pending，case 详情页提示"计入储备"；
- 已知类型的新格式（财务表换版式还是财务表）→ LLM 生成映射沉淀为新格式条目，直接填表；
- 计入储备 → 字段清单落为格式骨架（映射留空待人工补充），类型转 converted。
"""

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from autoemail import formfill
from autoemail.channels.base import NormalizedAttachment
from autoemail.llm import LLMResponse, _hash_input
from autoemail.llm_schemas import LLMFieldMappingOutput, LLMFormTypeOutput
from autoemail.models import AuditEvent, Case, Commission, FormTypeRegistry
from autoemail.router import FORMAT_LIBRARY, route_form
from autoemail import web

FIXTURES = Path(__file__).parent / "fixtures"
AGENT_BLANK = FIXTURES / "bond_application_blank.pdf"


class DualProvider:
    """按 task 名分发响应的假 LLM：form_type_classify / form_type_mapping。"""

    configured = True
    model_id = "fake-model"

    def __init__(self, responses: dict[str, object]):
        self.responses = responses
        self.calls: list[str] = []

    def complete_json(self, *, system_prompt, user_prompt, schema, task):
        self.calls.append(task)
        response = self.responses.get(task)
        if response is None:
            return LLMResponse(self.model_id, None, error="未配置该任务的响应")
        parsed = schema.model_validate(response)
        return LLMResponse(
            self.model_id, parsed, parsed.model_dump_json(),
            _hash_input(system_prompt, user_prompt, task), latency_ms=1,
        )


def _attachment() -> NormalizedAttachment:
    return NormalizedAttachment(
        filename="mystery_form.pdf",
        mime="application/pdf",
        content=AGENT_BLANK.read_bytes(),
        sha256="hash-mystery",
    )


def _no_route(pdf_bytes):
    return {"type": None, "format": None, "scores": {}, "matched": False}


@pytest.fixture()
def _llm_on(monkeypatch):
    monkeypatch.setattr("autoemail.config.settings.llm_enabled", True)


@pytest.fixture()
def _isolated_mappings(monkeypatch, tmp_path):
    """把 form_mappings.json 指到临时文件（种子为空库），并隔离内存格式库。"""
    path = tmp_path / "form_mappings.json"
    path.write_text(json.dumps({"formats": []}), encoding="utf-8")
    monkeypatch.setattr(formfill, "MAPPINGS_PATH", path)
    monkeypatch.setattr("autoemail.router.FORMAT_LIBRARY", list(FORMAT_LIBRARY))
    return path


# ---- 全新类型 → 登记 pending 储备 ----

def test_new_type_registers_pending(session, world, monkeypatch, tmp_path, _llm_on, _isolated_mappings):
    monkeypatch.setattr("autoemail.config.settings.filled_forms_dir", str(tmp_path))
    monkeypatch.setattr(formfill, "route_form", _no_route)
    provider = DualProvider({
        "form_type_classify": {
            "is_form": True,
            "type_id": "usda_permit_application",
            "type_name": "USDA 许可申请表",
            "is_new_type": True,
            "reason": "含 USDA 许可字段结构，不在已知类型清单里",
        },
    })

    report = formfill.auto_fill_for_case(
        session, world["case"], _attachment(), provider=provider
    )

    assert report is None  # 新类型不填表
    row = session.scalar(select(FormTypeRegistry))
    assert row is not None
    assert row.type_id == "usda_permit_application"
    assert row.status == "pending"
    assert row.source_case_id == world["case"].id
    assert row.source_filename == "mystery_form.pdf"
    assert "Company Name" in (row.sample_fields or [])  # 真实夹具的 acroform 字段
    events = session.scalars(
        select(AuditEvent).where(AuditEvent.action == "form.new_type_detected")
    ).all()
    assert len(events) == 1 and events[0].decision == "registry_pending"


def test_new_type_dedupe_same_type_id(session, world, monkeypatch, tmp_path, _llm_on, _isolated_mappings):
    monkeypatch.setattr("autoemail.config.settings.filled_forms_dir", str(tmp_path))
    monkeypatch.setattr(formfill, "route_form", _no_route)
    provider = DualProvider({
        "form_type_classify": {
            "is_form": True,
            "type_id": "usda_permit_application",
            "type_name": "USDA 许可申请表",
            "is_new_type": True,
            "reason": "x",
        },
    })
    for _ in range(2):
        formfill.auto_fill_for_case(session, world["case"], _attachment(), provider=provider)
    rows = session.scalars(select(FormTypeRegistry)).all()
    assert len(rows) == 1


def test_llm_disabled_no_registry_no_llm_call(session, world, monkeypatch, tmp_path, _isolated_mappings):
    monkeypatch.setattr("autoemail.config.settings.filled_forms_dir", str(tmp_path))
    monkeypatch.setattr(formfill, "route_form", _no_route)

    def _boom(*args, **kwargs):
        raise AssertionError("LLM 关闭时不应调用模型")

    provider = DualProvider({})
    monkeypatch.setattr(formfill, "classify_form_type", _boom)
    report = formfill.auto_fill_for_case(session, world["case"], _attachment(), provider=provider)
    assert report is None
    assert session.scalar(select(FormTypeRegistry)) is None


# ---- 已知类型的新格式 → LLM 克服映射，直接填表 ----

def test_known_type_new_format_llm_overcome(session, world, monkeypatch, tmp_path, _llm_on, _isolated_mappings):
    monkeypatch.setattr("autoemail.config.settings.filled_forms_dir", str(tmp_path))
    monkeypatch.setattr(formfill, "route_form", _no_route)

    class FakeFillResult:
        output_path = "filled.pdf"
        filled = ["Company Name", "TIN"]
        unknown_fields: list = []
        total_fields = 2

    monkeypatch.setattr("autoemail.toolkit._fill_pdf_form", lambda **kwargs: FakeFillResult())
    monkeypatch.setattr(
        "autoemail.toolkit._list_pdf_fields",
        lambda **kwargs: {"named_fields": ["Company Name", "TIN"]},
    )
    provider = DualProvider({
        "form_type_classify": {
            "is_form": True,
            "type_id": "customs_bond_application",
            "type_name": "海关保函申请表",
            "is_new_type": False,
            "reason": "保函申请表的另一版式，属格式差异",
        },
        "form_type_mapping": {
            "field_mapping": {
                "Company Name": "legal_name",
                "TIN": "ein",
                "Not A Real Field": "legal_name",  # 非法键应被过滤
            },
        },
    })

    report = formfill.auto_fill_for_case(
        session, world["case"], _attachment(), provider=provider
    )

    assert report is not None and report.get("output_path")
    assert report["via"] == "llm_format_overcome"
    assert report["form_type"] == "customs_bond_application"
    # 新格式条目落盘且映射合法
    data = json.loads(_isolated_mappings.read_text(encoding="utf-8"))
    entry = next(
        item for item in data["formats"] if item["format_id"] == report["form_format"]
    )
    assert entry["field_mapping"] == {"Company Name": "legal_name", "TIN": "ein"}
    assert "llm_generated" in entry["note"]
    # 同一字段集合重复到达 → 直接复用已登记格式，不再调 LLM
    provider2 = DualProvider({})
    format_id2 = formfill.llm_overcome_format(
        AGENT_BLANK.read_bytes(), "customs_bond_application", provider=provider2
    )
    assert format_id2 == report["form_format"]
    assert provider2.calls == []


# ---- 计入储备：骨架格式 + 状态转换 + web 动作 ----

def test_register_skeleton_format(_isolated_mappings):
    format_id = formfill.register_skeleton_format(
        "usda_permit_application", ["Z B", "Applicant Name", ""]
    )
    data = json.loads(_isolated_mappings.read_text(encoding="utf-8"))
    entry = next(item for item in data["formats"] if item["format_id"] == format_id)
    assert entry["field_names"] == ["Applicant Name", "Z B"]
    assert entry["field_mapping"] == {}
    assert "储备骨架" in entry["note"]


def test_perform_convert_form_type(session, world, monkeypatch, _llm_on, _isolated_mappings):
    row = FormTypeRegistry(
        type_id="usda_permit_application",
        name="USDA 许可申请表",
        status="pending",
        source_case_id=world["case"].id,
        source_filename="mystery.pdf",
        sample_fields=["Applicant Name", "Permit No"],
        reason="新类型",
    )
    session.add(row)
    session.commit()

    kind, message = web.perform_convert_form_type(
        session, world["case"].id, {"reviewer": "ops", "registry_id": str(row.id)}
    )
    assert kind == "ok"
    session.refresh(row)
    assert row.status == "converted"
    assert row.converted_by == "ops"
    assert row.converted_at is not None
    # 骨架格式已沉淀
    data = json.loads(_isolated_mappings.read_text(encoding="utf-8"))
    assert any(item["type_id"] == "usda_permit_application" for item in data["formats"])
    events = session.scalars(
        select(AuditEvent).where(AuditEvent.action == "form.new_type_converted")
    ).all()
    assert len(events) == 1

    # 已处理过不可重复处理
    kind2, _ = web.perform_convert_form_type(
        session, world["case"].id, {"reviewer": "ops", "registry_id": str(row.id)}
    )
    assert kind2 == "error"


def test_render_case_shows_pending_new_type_alert(session, world):
    session.add(FormTypeRegistry(
        type_id="usda_permit_application",
        name="USDA 许可申请表",
        status="pending",
        source_case_id=world["case"].id,
        source_filename="mystery.pdf",
        sample_fields=["Applicant Name"],
        reason="含未见字段",
    ))
    session.commit()
    html = web.render_case(session, world["case"].id)
    assert "检测到新类型表单" in html
    assert "计入储备" in html
    assert "USDA 许可申请表" in html

    # converted 后不再出提示卡，但分区里保留记录
    row = session.scalar(select(FormTypeRegistry))
    row.status = "converted"
    session.commit()
    html2 = web.render_case(session, world["case"].id)
    assert "检测到新类型表单" not in html2
    assert "USDA 许可申请表" in html2
