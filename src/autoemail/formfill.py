"""确定性填表：路由命中已知格式后，按配置映射从委托档案取值填表。

设计（对应"字段映射配置化"）：
- 每个格式在 form_mappings.json 里登记 acroform 字段指纹与字段映射规则；
- 映射规则三种写法：
  - "archive_field"：直接取委托档案列；
  - {"template": "{field1} ... {field2}"}：模板拼接，花括号引用档案列；
  - {"const": value}：固定值（复选框传 Yes）；
  - {"today": "%m/%d/%Y"}：填表当天日期。
- 档案取不到值的字段跳过并单独报告，绝不编造；映射里没有的表单字段
  列入 unmapped_fields 供人工补规则。
"""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from .config import settings
from .router import MAPPINGS_PATH, extract_field_names, route_form

SENSITIVE_COLUMNS = {"mailbox_password", "legal_name_norm"}


def load_mapping(format_id: str, path=None) -> dict[str, Any] | None:
    """取某格式的字段映射配置；没有登记映射返回 None。"""
    source = path or MAPPINGS_PATH
    if not source.exists():
        return None
    data = json.loads(source.read_text(encoding="utf-8"))
    for item in data.get("formats", []):
        if item.get("format_id") == format_id:
            return item.get("field_mapping") or {}
    return None


def load_signature_anchor(format_id: str, path=None) -> dict[str, Any] | None:
    """取某格式的签名区锚点配置（签名区是留白手写区、非 acroform 字段时用）。"""
    source = path or MAPPINGS_PATH
    if not source.exists():
        return None
    data = json.loads(source.read_text(encoding="utf-8"))
    for item in data.get("formats", []):
        if item.get("format_id") == format_id:
            return item.get("signature_anchor")
    return None


def get_commission_data(commission_name: str) -> dict:
    """按公司名查委托档案（模糊匹配），敏感字段不返回。找不到返回 {"error": ...}。"""
    from .db import SessionLocal
    from .models import Commission
    from .textutil import normalize_text

    norm = normalize_text(commission_name)
    with SessionLocal() as session:
        query = session.query(Commission)
        commission = query.filter(Commission.legal_name_norm == norm).first()
        if commission is None:
            commission = query.filter(
                Commission.legal_name_norm.like(f"%{norm}%")
            ).first()
        if commission is None:
            return {"error": f"找不到委托公司：{commission_name}"}
        return {
            col.name: getattr(commission, col.name)
            for col in Commission.__table__.columns
            if col.name not in SENSITIVE_COLUMNS
        }


def resolve_value(spec: Any, data: dict) -> tuple[Any, str | None]:
    """按映射规则算出一个字段值。返回 (值, 跳过原因)；跳过原因为 None 表示成功。

    支持规则：档案列名 / {"template"} / {"const"} / {"today"} / {"client"}。
    {"client": "b001"} 从客户需求.pdf 的字段取值（data 中以 client_b001 前缀注入）。
    值为 None/空字符串一律跳过，不编造。
    """
    if isinstance(spec, dict):
        if "const" in spec:
            return spec["const"], None
        if "today" in spec:
            return datetime.now().strftime(spec["today"]), None
        if "client" in spec:
            value = data.get(f"client_{spec['client']}")
            if value is None or value == "":
                return None, f"客户需求字段 {spec['client']} 为空"
            return value, None
        if "template" in spec:
            try:
                value = spec["template"].format_map(data)
            except KeyError as exc:
                return None, f"档案缺少字段 {exc}"
            if not value.strip():
                return None, "模板拼出空值"
            return value, None
        return None, f"无法识别的映射规则 {spec}"
    # 普通字符串：档案列名
    value = data.get(spec)
    if value is None or value == "":
        return None, f"档案字段 {spec} 为空"
    return value, None


def extract_client_intake(pdf_bytes: bytes) -> dict[str, str]:
    """从客户需求采集表（client_bond_application_v1）提取客户已填的字段值。

    返回 {字段名(如 b001/d019): 值}；空值字段不返回。
    """
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    fields = reader.get_fields() or {}
    result: dict[str, str] = {}
    for name, field in fields.items():
        value = field.get("/V")
        if value is None:
            continue
        text = str(value).strip()
        if text and text != "/Off" and text.lower() not in {"none", "n/a"}:
            result[name] = text
    return result


def store_intake(session, commission_id: int, pdf_bytes: bytes, filename: str | None = None) -> dict[str, str]:
    """解析客户需求采集表并存储结构化字段（委托专属，upsert 覆盖）。

    填表从这里取数，不再依赖飞书实时拉取；委托结束后随原始文件删除。
    """
    from .models import CommissionIntake

    fields = extract_client_intake(pdf_bytes)
    existing = session.scalar(
        select(CommissionIntake).where(CommissionIntake.commission_id == commission_id)
    )
    if existing is None:
        session.add(CommissionIntake(commission_id=commission_id, fields=fields, source_filename=filename))
    else:
        existing.fields = fields
        existing.source_filename = filename
    session.flush()
    return fields


def get_intake_fields(session, commission_id: int) -> dict[str, str]:
    """读委托的结构化客户需求字段；无则返回空 dict。"""
    from .models import CommissionIntake

    intake = session.scalar(
        select(CommissionIntake).where(CommissionIntake.commission_id == commission_id)
    )
    return dict(intake.fields) if intake else {}


def purge_commission_data(session, commission_id: int) -> dict:
    """委托结束后彻底清理：结构化客户需求字段 + 原始文件（章程/税号/证件）+ 签名图样。

    返回删除数量统计，供审计留痕。
    """
    from .models import CommissionDocument, CommissionIntake, SignatureAsset

    deleted = {"intake": 0, "documents": 0, "signature": 0}
    intake = session.scalar(
        select(CommissionIntake).where(CommissionIntake.commission_id == commission_id)
    )
    if intake is not None:
        session.delete(intake)
        deleted["intake"] = 1
    docs = session.scalars(
        select(CommissionDocument).where(CommissionDocument.commission_id == commission_id)
    ).all()
    for doc in docs:
        session.delete(doc)
        deleted["documents"] += 1
    sig = session.scalar(
        select(SignatureAsset).where(SignatureAsset.commission_id == commission_id)
    )
    if sig is not None:
        session.delete(sig)
        deleted["signature"] = 1
    session.flush()
    return deleted


def build_field_values(
    mapping: dict[str, Any], data: dict
) -> tuple[dict[str, Any], dict[str, str]]:
    """按映射规则生成 {PDF字段: 值}；返回 (field_values, skipped)。

    skipped: {PDF字段: 跳过原因}，供报告展示人工补数据。
    """
    field_values: dict[str, Any] = {}
    skipped: dict[str, str] = {}
    for pdf_field, spec in mapping.items():
        value, skip_reason = resolve_value(spec, data)
        if skip_reason is None:
            field_values[pdf_field] = value
        else:
            skipped[pdf_field] = skip_reason
    return field_values, skipped


def auto_fill_form(
    pdf_bytes: bytes,
    commission_name: str,
    output_path: str,
    client_intake_pdf: bytes | None = None,
) -> dict:
    """一步填表：路由 → 读档案 → 按配置映射生成字段值 → 调 fill_pdf_form。

    client_intake_pdf：可选，客户需求采集表（client_bond_application_v1）PDF，
    用于补充档案里没有的 case 字段（商品描述、原产国、货值、税费等）。

    返回操作报告（不回传 PDF 内容）：
    - routed / form_type / form_format：路由结果
    - filled / unknown_fields / total_fields：填表结果（来自 fill_pdf_form）
    - skipped：档案缺值跳过的字段及原因
    - unmapped_fields：表单里存在但映射未覆盖的字段（不含 undefined 机器名）
    - error：任一环节失败的原因
    """
    from . import toolkit

    route = route_form(pdf_bytes)
    if not route.get("matched"):
        return {
            "error": "表格路由未命中已知类型，需人工标注后再登记模板",
            "routed": False,
            "route": route,
        }
    format_id = route.get("format")
    if not format_id:
        return {
            "error": f"类型 {route.get('type')} 命中但没有匹配到已登记格式，需人工标注格式指纹",
            "routed": True,
            "route": route,
        }
    if route.get("role") == "client_fill_template":
        return {
            "routed": True,
            "form_type": route.get("type"),
            "form_format": format_id,
            "no_fill": True,
            "reason": "我方提供给客户的填写模板（client_fill_template），由客户自填，系统不代填",
        }
    mapping = load_mapping(format_id)
    if mapping is None:
        return {
            "error": f"格式 {format_id} 尚未配置字段映射，请先在 form_mappings.json 登记",
            "routed": True,
            "route": route,
        }

    data = get_commission_data(commission_name)
    if data.get("error"):
        return {"error": data["error"], "routed": True, "route": route}

    signature_png = _load_signature_png_by_name(commission_name)
    if client_intake_pdf is not None:
        client_data = extract_client_intake(client_intake_pdf)
    else:
        client_data = _load_intake_by_id(data.get("id"))
    report = _fill_core(
        pdf_bytes, format_id, data, output_path,
        signature_png=signature_png, client_data=client_data,
    )
    report.update({"routed": True, "form_type": route.get("type")})
    return report


# 批量填表产物使用的短名 -> 格式 id（历史命名：{legal_name}_{short}.pdf）
SHORT_FORMAT_IDS = {
    "poa": "asonic_poa_v1",
    "bond": "asonic_bond_app_v1",
    "cbp5106": "asonic_cbp5106_v1",
}


def stamp_signature_on_existing(legal_name: str, short: str) -> dict:
    """对已填好的 {legal_name}_{short}.pdf 补盖签名并覆盖原文件。

    用于「先填表、后上传签名」的时序补救：文字字段已填好，只按签名锚点把
    客户签名图样盖到署名处。无签名图样 / 无锚点配置时返回 stamped=False。
    short ∈ {poa, bond, cbp5106}。
    """
    from .signature import stamp_signature_at_anchor, stamp_signature_at_rects

    fmt = SHORT_FORMAT_IDS.get(short)
    if fmt is None:
        return {"stamped": False, "file": None, "reason": f"未知短名：{short}"}
    path = Path(settings.filled_forms_dir) / f"{legal_name}_{short}.pdf"
    if not path.exists():
        return {"stamped": False, "file": path.name, "reason": "文件不存在"}
    png = _load_signature_png_by_name(legal_name)
    if not png:
        return {"stamped": False, "file": path.name, "reason": "无客户签名图样"}
    anchor = load_signature_anchor(fmt)
    if anchor is None:
        return {"stamped": False, "file": path.name, "reason": f"格式 {fmt} 无签名锚点配置"}
    pdf_bytes = path.read_bytes()
    if "rect" in anchor:
        page = int(anchor.get("page", 0))
        rect = tuple(float(v) for v in anchor["rect"])
        path.write_bytes(stamp_signature_at_rects(pdf_bytes, png, [(page, rect)]))
        return {"stamped": True, "file": path.name, "method": f"rect page={page}"}
    field = anchor.get("field")
    if field:
        gap = float(anchor.get("gap", 5))
        height = float(anchor.get("height", 24))
        new_bytes, ok = stamp_signature_at_anchor(pdf_bytes, png, field, gap=gap, height=height)
        path.write_bytes(new_bytes)
        return {"stamped": ok, "file": path.name, "method": f"anchor field={field}", "ok": ok}
    return {"stamped": False, "file": path.name, "reason": "签名锚点配置无法识别"}


def _load_intake_by_id(commission_id: int | None) -> dict[str, str] | None:
    """按委托 id 从库读结构化客户需求字段；无记录返回 None。"""
    if commission_id is None:
        return None
    from .db import SessionLocal

    with SessionLocal() as session:
        fields = get_intake_fields(session, commission_id)
        return fields or None


def _load_signature_png_by_name(commission_name: str) -> bytes | None:
    """按公司名取客户签名图样原始内容；未提供返回 None。"""
    from .db import SessionLocal
    from .models import Commission
    from .signature import get_signature_asset
    from .textutil import normalize_text

    norm = normalize_text(commission_name)
    with SessionLocal() as session:
        commission = session.query(Commission).filter(
            Commission.legal_name_norm == norm
        ).first() or session.query(Commission).filter(
            Commission.legal_name_norm.like(f"%{norm}%")
        ).first()
        if commission is None:
            return None
        asset = get_signature_asset(session, commission.id)
        return asset.content if asset else None


def _commission_data(commission) -> dict:  # noqa: ANN001
    """把 Commission 对象转成填表用档案字典，敏感字段不返回。"""
    from .models import Commission

    return {
        col.name: getattr(commission, col.name)
        for col in Commission.__table__.columns
        if col.name not in SENSITIVE_COLUMNS
    }


def _fill_core(
    pdf_bytes: bytes,
    format_id: str,
    data: dict,
    output_path: str,
    signature_png: bytes | None = None,
    client_data: dict[str, str] | None = None,
) -> dict:
    """按格式映射从档案数据填表并落盘，返回填表报告。

    signature_png：客户本人提供的签名图样（PNG）。映射里标 {"signature": true}
    的署名域在文字填充后盖章；无素材时留空并单独报告。
    client_data：客户需求采集表提取的字段值，以 client_ 前缀注入 data，供
    {"client": "b001"} 映射规则或 {client_d019} 模板引用。
    """
    from . import toolkit
    from .signature import stamp_signature

    mapping = load_mapping(format_id)
    if mapping is None:
        return {"error": f"格式 {format_id} 尚未配置字段映射，请先在 form_mappings.json 登记"}

    merged = dict(data)
    if client_data:
        merged.update({f"client_{k}": v for k, v in client_data.items()})

    text_mapping: dict[str, Any] = {}
    signature_fields: list[str] = []
    for pdf_field, spec in mapping.items():
        if isinstance(spec, dict) and spec.get("signature"):
            signature_fields.append(pdf_field)
        else:
            text_mapping[pdf_field] = spec

    field_values, skipped = build_field_values(text_mapping, merged)
    fill_result = toolkit._fill_pdf_form(
        pdf_bytes=pdf_bytes, field_values=field_values, output_path=output_path
    )

    # 署名域：有客户签名素材则盖章，否则留空待客户签署
    signature_report: dict[str, str] = {}
    stamped_bytes = fill_result.output_path
    if signature_fields:
        if signature_png:
            stamped = Path(output_path).read_bytes()
            new_bytes, missing = stamp_signature(stamped, signature_png, signature_fields)
            Path(output_path).write_bytes(new_bytes)
            for field in signature_fields:
                signature_report[field] = (
                    "stamped" if field not in missing else "字段坐标未定位到，已留空"
                )
        else:
            for field in signature_fields:
                signature_report[field] = "客户签名图样未提供，留空待客户签署"
            skipped.update({f: "需要客户本人签名" for f in signature_fields})
        stamped_bytes = output_path

    # 签名区（留白手写区、非 acroform 字段）：按 signature_anchor 配置把客户签名盖章
    # 支持两种定位：{"field": 锚点字段, "gap", "height"}（签名区在锚点上方）；
    # 或 {"page", "rect"}（签名区直接给定坐标，用于签名框在静态文本旁的情况）。
    anchor = load_signature_anchor(format_id)
    if anchor is not None:
        if signature_png:
            stamped = Path(output_path).read_bytes()
            rect = anchor.get("rect")
            field = anchor.get("field")
            if rect is not None:
                from .signature import stamp_signature_at_rects

                page = int(anchor.get("page", 0))
                new_bytes = stamp_signature_at_rects(
                    stamped, signature_png, [(page, tuple(float(v) for v in rect))]
                )
                Path(output_path).write_bytes(new_bytes)
                signature_report[f"page{page}:{rect}"] = "stamped（签名区盖章）"
            elif field:
                from .signature import stamp_signature_at_anchor

                gap = float(anchor.get("gap", 5))
                height = float(anchor.get("height", 24))
                new_bytes, ok = stamp_signature_at_anchor(
                    stamped, signature_png, field, gap=gap, height=height
                )
                if ok:
                    Path(output_path).write_bytes(new_bytes)
                    signature_report[field] = "stamped（签名区盖章）"
                else:
                    signature_report[field] = "签名区坐标未定位，留空待客户签署"
        else:
            key = str(anchor.get("field") or anchor.get("rect") or "签名区")
            signature_report[key] = "客户签名图样未提供，留空待客户签署"
            skipped[key] = "需要客户本人签名"
        stamped_bytes = output_path

    # 表单里存在但映射没覆盖的字段（排除 undefined 机器名），供人工补规则
    listed = toolkit._list_pdf_fields(pdf_bytes=pdf_bytes)
    form_fields = set(listed.get("named_fields", []))
    return {
        "form_format": format_id,
        "output_path": stamped_bytes,
        "filled": fill_result.filled,
        "unknown_fields": fill_result.unknown_fields,
        "total_fields": fill_result.total_fields,
        "skipped": skipped,
        "unmapped_fields": sorted(form_fields - set(mapping)),
        "signature_fields": signature_report,
    }


def fill_flat_pdf(pdf_bytes: bytes, fields: list[dict], output_path: str) -> str:
    """平铺表填表：没有 acroform 字段的 PDF，按坐标在填写区画文字 / 勾选标记。

    fields 每项：{"x", "y", "text"}（画文字）或 {"x", "y", "check": true}（画 X 勾选）。
    坐标 x/y 为 PDF 坐标（左下角原点，单位 point）。返回落盘路径。
    复用 reportlab 画覆盖页 + pypdf merge 的机制（与签名盖章同源）。
    """
    import io

    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen import canvas

    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter(clone_from=reader)
    mb = writer.pages[0].mediabox
    width, height = float(mb.width), float(mb.height)

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height))
    for f in fields:
        x, y = float(f["x"]), float(f["y"])
        if f.get("check"):
            c.setFont("Helvetica-Bold", float(f.get("size", 12)))
            c.drawString(x, y, "X")
        else:
            c.setFont(f.get("font", "Helvetica"), float(f.get("size", 10)))
            c.drawString(x, y, str(f.get("text", "")))
    c.save()
    buf.seek(0)
    overlay = PdfReader(buf).pages[0]
    writer.pages[0].merge_page(overlay)

    out = io.BytesIO()
    writer.write(out)
    Path(output_path).write_bytes(out.getvalue())
    return output_path


# ---- 平铺表（无 acroform 字段）坐标填表 ----
# 平铺表签名：文本关键词 → 坐标字段生成器。坐标 x/y 为 PDF 坐标（左下角原点）。
def _bond_exchange_flat_fields(commission, intake: dict) -> list[dict]:
    """The Bond Exchange 版 C.A. Shea 海关保函申请表（平铺版，无表单字段）。

    坐标已按样本 PDF 实测校准：文字坐落在填写线上，勾选框 X 落在 9x9 框内。
    """
    ein = (commission.ein or "").replace("-", "")
    fields: list[dict] = [
        {"x": 20, "y": 724, "text": commission.legal_name, "size": 10},
        {"x": 505, "y": 714, "text": ein, "size": 10},
        {"x": 20, "y": 700, "text": commission.addr_line1 or "", "size": 9},
        {"x": 20, "y": 653, "text": commission.addr_city or "", "size": 10},
        {"x": 267, "y": 653, "text": commission.addr_state or "", "size": 10},
        {"x": 376, "y": 653, "text": commission.addr_zip or "", "size": 10},
        {"x": 90, "y": 535, "text": intake.get("a004") or "", "size": 10},
        # 勾选框：X 基线 y = 792 - 框top - 10
        {"x": 121, "y": 568, "check": True},   # Requested Bond Type - Continuous
        {"x": 23, "y": 553, "check": True},    # Activity 1 - Importer
        {"x": 35, "y": 487, "check": True},    # Type of Business - Importer
    ]
    return fields


_FLAT_FORM_DETECTORS: dict[str, tuple[tuple[str, ...], Any]] = {
    # 标识名 -> (文本关键词全部命中, 字段生成器)
    "bond_exchange_flat_v1": (
        ("bond exchange", "customs bond application"),
        _bond_exchange_flat_fields,
    ),
}


def _detect_flat_form(pdf_bytes: bytes) -> str | None:
    """识别无 acroform 字段的平铺表：文本命中已知平铺表关键词返回其标识名。"""
    from .ingest.pdf import extract_pdf_text

    text = (extract_pdf_text(pdf_bytes) or "").lower()
    if not text:
        return None
    for name, (keywords, _fn) in _FLAT_FORM_DETECTORS.items():
        if all(kw in text for kw in keywords):
            return name
    return None


def _fill_flat_form(session, case, flat_name: str, pdf_bytes: bytes, *, message_id: int | None = None) -> dict | None:
    """填已知平铺表：按坐标画文字/勾选落盘并记审计。失败返回 None（不阻断主链路）。"""
    from . import audit
    from .models import Commission

    commission = session.get(Commission, case.commission_id)
    if commission is None:
        return None
    generator = _FLAT_FORM_DETECTORS[flat_name][1]
    intake = get_intake_fields(session, commission.id) or {}
    fields = generator(commission, intake)

    out_dir = Path(settings.filled_forms_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = str(out_dir / f"{case.case_no}_{flat_name}.pdf")
    try:
        fill_flat_pdf(pdf_bytes, fields, output_path)
    except Exception as exc:  # noqa: BLE001
        audit.record(
            session,
            action="form.auto_fill",
            case_id=case.id,
            message_id=message_id,
            decision="skipped",
            reasons=[f"平铺表 {flat_name} 填表失败：{exc}"],
        )
        return None
    audit.record(
        session,
        action="form.auto_fill",
        case_id=case.id,
        message_id=message_id,
        decision="filled",
        reasons=[f"平铺表 {flat_name} 坐标填表 {len(fields)} 项"],
    )
    return {"output_path": output_path, "form_format": flat_name, "filled": len(fields)}


def auto_fill_for_case(session, case, attachment, *, message_id: int | None = None, provider=None) -> dict | None:  # noqa: ANN001
    """case 级填表入口：代理商发来的表格附件若命中已登记格式，按档案自动填好落盘。

    - 客户自填模板（client_fill_template）一律不代填；
    - 指纹未命中 → 类型分流：全新类型登记储备提示人工（不阻断草稿），
      已知类型的新格式由 LLM 克服后直接填；
    - 未命中格式或填表失败返回 None（不阻断主链路，仅留审计记录）；
    - 成功返回填表报告，output_path 指向 filled_forms_dir 下的产物。
    """
    from . import audit
    from .models import Commission

    pdf_bytes = attachment.content
    route = route_form(pdf_bytes)
    if not route.get("matched") or not route.get("format"):
        # 平铺表（无 acroform 字段）：先按已知平铺表坐标填，否则走未命中分流
        flat_name = _detect_flat_form(pdf_bytes)
        if flat_name is not None:
            return _fill_flat_form(session, case, flat_name, pdf_bytes, message_id=message_id)
        return _handle_unrouted_form(
            session, case, attachment, pdf_bytes, route,
            message_id=message_id, provider=provider,
        )
    if route.get("role") == "client_fill_template":
        return None

    commission = session.get(Commission, case.commission_id)
    if commission is None:
        return None

    out_dir = Path(settings.filled_forms_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = str(out_dir / f"{case.case_no}_{route['format']}.pdf")

    from .signature import get_signature_asset

    asset = get_signature_asset(session, commission.id)
    report = _fill_core(
        pdf_bytes,
        route["format"],
        _commission_data(commission),
        output_path,
        signature_png=asset.content if asset else None,
        client_data=get_intake_fields(session, commission.id) or None,
    )
    audit.record(
        session,
        action="form.auto_fill",
        case_id=case.id,
        message_id=message_id,
        decision="filled" if report.get("output_path") else "skipped",
        reasons=[
            f"attachment={attachment.filename}",
            f"format={route.get('format')}",
            report.get("error") or f"filled={len(report.get('filled') or [])}",
        ],
    )
    return report if report.get("output_path") else None


# ---- 类型 ≠ 格式：LLM 类型判定 / 新格式 LLM 克服 / 新类型储备 ----

def known_type_ids() -> set[str]:
    """已知类型全集：form_mappings.json 登记过的 type_id + 类型定义表。"""
    from .router import FORM_TYPES, FORMAT_LIBRARY

    return {fmt.type_id for fmt in FORMAT_LIBRARY} | set(FORM_TYPES)


def classify_form_type(pdf_bytes: bytes, *, provider=None) -> dict | None:
    """LLM 读表格字段名与文本，判断它属于哪种类型、是否是从未见过的类型。

    类型 ≠ 格式：财务表换版式仍是财务表（格式问题，LLM 克服映射）；
    出现我们没遇到过的字段结构才是新类型（需要启动转化流程）。
    LLM 不可用或校验失败返回 None。
    """
    from .ingest.pdf import extract_pdf_text
    from .llm import HttpxOpenAICompatibleProvider
    from .llm_schemas import LLMFormTypeOutput
    from .router import FORM_TYPES

    if provider is None:
        provider = HttpxOpenAICompatibleProvider()
    if not getattr(provider, "configured", True):
        return None
    field_names = sorted(extract_field_names(pdf_bytes))
    text = extract_pdf_text(pdf_bytes)
    if not field_names and not text:
        return None
    known_lines = "\n".join(
        f"- {tid}: {FORM_TYPES.get(tid, '已在格式库登记过的类型')}"
        for tid in sorted(known_type_ids())
    )
    field_lines = "\n".join(field_names[:120]) or "（无 acroform 字段）"
    prompt = (
        "判断下面这份表格属于哪种类型。注意区分两件事：\n"
        "1) 类型（type）：表格的业务种类，如海关保函申请表、个人财务报表；\n"
        "2) 格式（format）：同一类型的不同版式/字段命名，格式不同不算新类型。\n"
        "只有当表格的业务类型不在已知类型清单里（含我们没遇到过的字段结构）时，"
        "才判 is_new_type=true，并给一个 snake_case 的建议 type_id。\n\n"
        f"已知类型清单：\n{known_lines}\n\n"
        f"表格的 acroform 字段名：\n{field_lines}\n\n"
        f"表格文本摘录：\n{(text or '')[:1500]}"
    )
    result = provider.complete_json(
        system_prompt="你是表单类型判定助手，根据字段与文本判断表格类型，只输出 JSON。",
        user_prompt=prompt,
        schema=LLMFormTypeOutput,
        task="form_type_classify",
    )
    if result.error is not None or result.parsed is None:
        return None
    out = result.parsed
    return {
        "is_form": out.is_form,
        "type_id": out.type_id,
        "type_name": out.type_name,
        "is_new_type": out.is_new_type,
        "reason": out.reason,
    }


def _stable_format_id(type_id: str, field_names: list[str], suffix: str) -> str:
    digest = hashlib.sha1("\n".join(field_names).encode()).hexdigest()[:8]
    return f"{type_id}_{suffix}_{digest}"


def _load_mappings(source: Path) -> dict:
    if source.exists():
        return json.loads(source.read_text(encoding="utf-8"))
    return {"formats": []}


def _append_format_entry(entry: dict, *, mappings_path: Path | None = None) -> None:
    """把格式条目写进 form_mappings.json 并同步内存路由库。"""
    from .router import FORMAT_LIBRARY, FormFormat

    source = mappings_path or MAPPINGS_PATH
    data = _load_mappings(source)
    by_id = {item.get("format_id"): item for item in data.get("formats", [])}
    existing = by_id.get(entry["format_id"])
    if existing is not None:
        existing.update(entry)
    else:
        data.setdefault("formats", []).append(entry)
    source.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if not any(f.format_id == entry["format_id"] for f in FORMAT_LIBRARY):
        FORMAT_LIBRARY.append(
            FormFormat(
                format_id=entry["format_id"],
                type_id=entry["type_id"],
                field_names=frozenset(entry.get("field_names", [])),
                page_count=entry.get("page_count", 0),
                field_count=len(entry.get("field_names", [])),
                role=entry.get("role", "agent_inbound"),
            )
        )


def llm_overcome_format(
    pdf_bytes: bytes,
    type_id: str,
    *,
    provider=None,
    mappings_path: Path | None = None,
) -> str | None:
    """已知类型的新格式：LLM 读字段名生成字段映射，登记为新格式条目（格式克服）。

    映射落盘后立即可用于填表（含人工检查 unmapped/skipped 的线索）；
    同一字段集合的表格到达不会重复登记。失败返回 None。
    """
    from .llm import HttpxOpenAICompatibleProvider
    from .llm_schemas import LLMFieldMappingOutput
    from .models import Commission

    if provider is None:
        provider = HttpxOpenAICompatibleProvider()
    if not getattr(provider, "configured", True):
        return None
    field_names = sorted(extract_field_names(pdf_bytes))
    if not field_names:
        return None
    format_id = _stable_format_id(type_id, field_names, "llm")
    source = mappings_path or MAPPINGS_PATH
    data = _load_mappings(source)
    if any(item.get("format_id") == format_id for item in data.get("formats", [])):
        return format_id

    archive_columns = sorted(
        c.name for c in Commission.__table__.columns
        if c.name not in SENSITIVE_COLUMNS
    )
    rules_doc = (
        "映射值支持三种写法：\n"
        '- 档案列名字符串：直接取档案值，如 "legal_name"；\n'
        '- {"template": "{addr_line1}, {addr_city}"}：模板拼接，花括号引用档案列'
        '（客户需求字段以 client_ 前缀引用，如 {client_d001}）；\n'
        '- {"const": "Yes"}：固定值（复选框通常填 Yes 或 X）；\n'
        '- {"today": "%m/%d/%Y"}：填表当天日期。\n'
        "没有把握的字段不要编造，直接不要出现在映射里。"
    )
    prompt = (
        f"表格类型：{type_id}。这是一份该类型下的新格式表格（版式/字段命名不同），\n"
        "请为每个 acroform 字段给出取值映射，供自动填表使用。\n\n"
        f"{rules_doc}\n\n"
        f"可用档案列：\n{', '.join(archive_columns)}\n\n"
        f"表格 acroform 字段名：\n" + "\n".join(field_names[:200])
    )
    result = provider.complete_json(
        system_prompt="你是表单字段映射助手，把表格字段对应到档案数据，只输出 JSON。",
        user_prompt=prompt,
        schema=LLMFieldMappingOutput,
        task="form_type_mapping",
    )
    if result.error is not None or result.parsed is None:
        return None
    valid_fields = set(field_names)
    mapping = {
        k: v for k, v in result.parsed.field_mapping.items() if k in valid_fields
    }
    if not mapping:
        return None
    try:
        from pypdf import PdfReader
        import io

        page_count = len(PdfReader(io.BytesIO(pdf_bytes)).pages)
    except Exception:  # noqa: BLE001
        page_count = 0
    _append_format_entry(
        {
            "format_id": format_id,
            "type_id": type_id,
            "field_names": field_names,
            "page_count": page_count,
            "role": "agent_inbound",
            "field_mapping": mapping,
            "note": (
                "llm_generated：LLM 为已知类型的新格式自动生成的字段映射，"
                "以填表报告的 skipped/unmapped_fields 为线索人工校对"
            ),
        },
        mappings_path=mappings_path,
    )
    return format_id


def register_skeleton_format(
    type_id: str,
    field_names: list[str],
    *,
    mappings_path: Path | None = None,
) -> str:
    """新类型计入储备：字段清单登记为格式骨架（映射留空待人工补充）。"""
    fields = sorted(f for f in field_names if f)
    format_id = _stable_format_id(type_id, fields, "manual")
    _append_format_entry(
        {
            "format_id": format_id,
            "type_id": type_id,
            "field_names": fields,
            "page_count": 0,
            "role": "agent_inbound",
            "field_mapping": {},
            "note": "储备骨架：新类型计入储备后登记的格式指纹，field_mapping 待人工补充",
        },
        mappings_path=mappings_path,
    )
    return format_id


def _handle_unrouted_form(
    session,
    case,
    attachment,
    pdf_bytes: bytes,
    route: dict,
    *,
    message_id: int | None = None,
    provider=None,
) -> dict | None:
    """指纹未命中的表格分流：新类型 → 储备登记提示人工；已知类型新格式 → LLM 克服后填。"""
    from . import audit
    from .models import Commission, FormTypeRegistry

    if not settings.llm_enabled:
        return None
    judgment = classify_form_type(pdf_bytes, provider=provider)
    if judgment is None or not judgment.get("is_form"):
        audit.record(
            session,
            action="form.type_classify",
            case_id=case.id,
            message_id=message_id,
            decision="not_form_or_failed",
            reasons=[f"attachment={attachment.filename}"],
        )
        return None

    if not judgment.get("is_new_type") and judgment.get("type_id") in known_type_ids():
        # 已知类型的新格式：LLM 克服映射，直接按新格式填
        format_id = llm_overcome_format(pdf_bytes, judgment["type_id"], provider=provider)
        if format_id is None:
            audit.record(
                session,
                action="form.llm_format_overcome",
                case_id=case.id,
                message_id=message_id,
                decision="failed",
                reasons=[
                    f"attachment={attachment.filename}",
                    f"type_id={judgment['type_id']}",
                    "LLM 映射生成失败",
                ],
            )
            return None
        commission = session.get(Commission, case.commission_id)
        if commission is None:
            return None
        out_dir = Path(settings.filled_forms_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(out_dir / f"{case.case_no}_{format_id}.pdf")
        from .signature import get_signature_asset

        asset = get_signature_asset(session, commission.id)
        report = _fill_core(
            pdf_bytes,
            format_id,
            _commission_data(commission),
            output_path,
            signature_png=asset.content if asset else None,
            client_data=get_intake_fields(session, commission.id) or None,
        )
        report.update(
            {
                "routed": True,
                "form_type": judgment["type_id"],
                "form_format": format_id,
                "via": "llm_format_overcome",
            }
        )
        audit.record(
            session,
            action="form.auto_fill",
            case_id=case.id,
            message_id=message_id,
            decision="filled" if report.get("output_path") else "skipped",
            reasons=[
                f"attachment={attachment.filename}",
                f"format={format_id}（llm_format_overcome）",
                report.get("error") or f"filled={len(report.get('filled') or [])}",
            ],
        )
        return report if report.get("output_path") else None

    # 全新类型：登记储备（同类型全局去重），case 详情页提示人工"计入储备"
    type_id = judgment["type_id"]
    existing = session.scalar(
        select(FormTypeRegistry).where(FormTypeRegistry.type_id == type_id)
    )
    if existing is None:
        session.add(
            FormTypeRegistry(
                type_id=type_id,
                name=judgment.get("type_name") or type_id,
                status="pending",
                source_case_id=case.id,
                source_attachment_id=getattr(attachment, "id", None),
                source_filename=attachment.filename,
                sample_fields=sorted(extract_field_names(pdf_bytes)),
                reason=judgment.get("reason"),
            )
        )
        session.flush()
    audit.record(
        session,
        action="form.new_type_detected",
        case_id=case.id,
        message_id=message_id,
        decision="registry_pending",
        reasons=[
            f"attachment={attachment.filename}",
            f"type_id={type_id}",
            judgment.get("reason") or "",
        ],
    )
    return None
