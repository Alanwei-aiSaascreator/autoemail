"""表格路由：类型级 LLM rerank + 格式级结构指纹匹配。

两级设计：
- 类型级（粗）：用 LLM 语义 rerank，把表格分到已知类型（个人财务 / 海关保函 / 信用授权）
- 格式级（细）：用 acroform 字段名集合的 Jaccard 相似度匹配到具体格式
- 分数都低 → 判定新类型/新格式，走人工标注后注册进模板库
"""

import io
import json
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

from .ingest.pdf import extract_pdf_text
from .llm import HttpxOpenAICompatibleProvider


# ---- 类型定义（LLM 语义 rerank 用） ----

FORM_TYPES: dict[str, str] = {
    "personal_financial_statement": "个人财务报表，含个人姓名、SSN、年龄、配偶、住址、资产与负债明细",
    "customs_bond_application": "海关保函申请表，含公司名、EIN、地址、注册州、签字人、商品、保额",
    "credit_authorization": "信用报告与背景调查授权书，含个人姓名、SSN、住址、签字",
}


class RerankOut(BaseModel):
    scores: dict[str, float]
    best: str


# ---- 格式指纹（结构匹配用） ----

@dataclass
class FormFormat:
    format_id: str
    type_id: str
    field_names: frozenset[str] = field(default_factory=frozenset)
    page_count: int = 0
    field_count: int = 0
    # agent_inbound：代理商发来、我方要填；client_fill_template：我方出给客户填，系统不代填
    role: str = "agent_inbound"


FORMAT_LIBRARY: list[FormFormat] = []

# 格式指纹与字段映射统一落在配置文件里：新格式人工标注后登记进来，不改代码
MAPPINGS_PATH = Path(__file__).parent / "form_mappings.json"


def load_formats(path: Path | None = None) -> list[FormFormat]:
    """从配置文件加载格式指纹（含字段映射配置，供 formfill 读取）。"""
    source = path or MAPPINGS_PATH
    if not source.exists():
        return []
    data = json.loads(source.read_text(encoding="utf-8"))
    loaded: list[FormFormat] = []
    for item in data.get("formats", []):
        loaded.append(
            FormFormat(
                format_id=item["format_id"],
                type_id=item["type_id"],
                field_names=frozenset(item.get("field_names", [])),
                page_count=item.get("page_count", 0),
                field_count=len(item.get("field_names", [])),
                role=item.get("role", "agent_inbound"),
            )
        )
    return loaded


def save_formats(path: Path | None = None) -> None:
    """把 FORMAT_LIBRARY 写回配置文件（保留已有映射配置）。"""
    source = path or MAPPINGS_PATH
    existing = {}
    if source.exists():
        existing = json.loads(source.read_text(encoding="utf-8"))
    by_id = {
        item.get("format_id"): item for item in existing.get("formats", [])
    }
    for fmt in FORMAT_LIBRARY:
        item = by_id.setdefault(fmt.format_id, {})
        item.update(
            {
                "format_id": fmt.format_id,
                "type_id": fmt.type_id,
                "field_names": sorted(fmt.field_names),
                "page_count": fmt.page_count,
                "role": fmt.role,
            }
        )
    source.write_text(
        json.dumps({"formats": list(by_id.values())}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


FORMAT_LIBRARY.extend(load_formats())


def extract_field_names(pdf_bytes: bytes) -> frozenset[str]:
    """提取 PDF 的 acroform 字段名集合（作为格式指纹）。"""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(pdf_bytes))
        fields = reader.get_fields()
        return frozenset(fields.keys()) if fields else frozenset()
    except Exception:  # noqa: BLE001
        return frozenset()


def register_format(
    format_id: str,
    type_id: str,
    pdf_bytes: bytes,
    *,
    role: str = "agent_inbound",
    persist: bool = True,
) -> FormFormat:
    """从样本 PDF 注册一个格式指纹，默认持久化进配置文件。"""
    field_names = extract_field_names(pdf_bytes)
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(pdf_bytes))
        page_count = len(reader.pages)
    except Exception:  # noqa: BLE001
        page_count = 0
    fmt = FormFormat(
        format_id=format_id,
        type_id=type_id,
        field_names=field_names,
        page_count=page_count,
        field_count=len(field_names),
        role=role,
    )
    FORMAT_LIBRARY.append(fmt)
    if persist:
        save_formats()
    return fmt


# ---- 路由 ----

def rerank_types(text: str) -> dict[str, float]:
    """类型级 rerank：LLM 对每个已知类型打分。"""
    provider = HttpxOpenAICompatibleProvider()
    lines = "\n".join(f"{k}: {v}" for k, v in FORM_TYPES.items())
    prompt = (
        "你是表格分类助手。以下是已知表格类型：\n"
        f"{lines}\n\n"
        "给定一份新表格的文本，对每个类型给出匹配分（0~1，越高越匹配）。\n"
        "只返回一个 JSON：{\"scores\": {\"<类型id>\": 分数}, \"best\": \"<最匹配类型id>\"}\n\n"
        f"新表格文本：\n{text[:1500]}"
    )
    result = provider.complete_json(
        system_prompt="你是表格分类助手，根据表格字段内容判断类型匹配度。",
        user_prompt=prompt,
        schema=RerankOut,
        task="rerank_types",
    )
    if result.error is not None or result.parsed is None:
        return {}
    return result.parsed.scores


def match_format(input_fields: frozenset[str], formats: list[FormFormat]) -> str | None:
    """格式级匹配：用 acroform 字段名集合的 Jaccard 相似度，选最高分。"""
    best_id = None
    best_score = 0.0
    for fmt in formats:
        if not fmt.field_names or not input_fields:
            continue
        inter = len(input_fields & fmt.field_names)
        union = len(input_fields | fmt.field_names)
        jaccard = inter / union if union else 0.0
        if jaccard > best_score:
            best_score = jaccard
            best_id = fmt.format_id
    return best_id if best_score >= 0.5 else None


def route_form(
    pdf_bytes: bytes,
    *,
    type_threshold: float = 0.5,
) -> dict:
    """路由一个表格 PDF：返回类型、格式、角色、分数与是否命中。

    先做全库指纹短路匹配（确定性、零 LLM 成本）；未命中再走
    类型级 LLM rerank + 类型内格式指纹的两级识别。
    """
    text = extract_pdf_text(pdf_bytes)
    input_fields = extract_field_names(pdf_bytes)

    if input_fields:
        format_id = match_format(input_fields, list(FORMAT_LIBRARY))
        if format_id:
            fmt = next(f for f in FORMAT_LIBRARY if f.format_id == format_id)
            return {
                "type": fmt.type_id,
                "format": format_id,
                "role": fmt.role,
                "scores": {},
                "matched": True,
                "via": "fingerprint",
            }

    if not text:
        return {"type": None, "format": None, "scores": {}, "matched": False}

    type_scores = rerank_types(text)
    if not type_scores:
        return {"type": None, "format": None, "scores": {}, "matched": False}

    best_type = max(type_scores, key=type_scores.get)
    best_score = type_scores[best_type]

    if best_score < type_threshold:
        return {
            "type": best_type,
            "format": None,
            "scores": type_scores,
            "matched": False,
            "new_type": True,
        }

    formats = [f for f in FORMAT_LIBRARY if f.type_id == best_type]
    format_id = match_format(input_fields, formats) if input_fields else None
    fmt = next((f for f in FORMAT_LIBRARY if f.format_id == format_id), None)

    return {
        "type": best_type,
        "format": format_id,
        "role": fmt.role if fmt else None,
        "scores": type_scores,
        "matched": True,
    }
