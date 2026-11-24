"""工具注册表：把原子能力（LLM / 程序）注册成可被 function calling 调用的工具。

每个工具是「无状态纯函数」：输入 → 结构化输出，便于后续让 LLM 编排。
"""

import base64
from dataclasses import dataclass
import io
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel
from pypdf import PdfReader, PdfWriter

from .ingest.pdf import extract_pdf_text
from .llm import HttpxOpenAICompatibleProvider
from .vision import (
    DriverLicenseInfo,
    extract_driver_license,
    extract_id_card,
    extract_ssn_card,
)


# ---- 结构化输出 schema ----

class PersonalIdentity(BaseModel):
    full_name: str | None = None
    address: str | None = None
    ssn: str | None = None
    date_of_birth: str | None = None


class CompanyFiling(BaseModel):
    state_file_no: str | None = None
    business_type: str | None = None
    manager_name: str | None = None
    dba: str | None = None


# ---- 原子能力实现 ----

def _parse_ssn_text(text: str) -> PersonalIdentity | None:
    """从 SSN 文本提取个人身份信息（格式不统一，用文本 LLM 结构化）。"""
    provider = HttpxOpenAICompatibleProvider()
    result = provider.complete_json(
        system_prompt="你是档案录入助手，从客户提供的文本中提取个人身份信息。取不到的字段填 null。",
        user_prompt=(
            "从以下文本提取 full_name(个人英文全名)、address(个人住址)、"
            "ssn(社会安全号，去掉 SSN: 前缀只留数字)、"
            "date_of_birth(出生日期 YYYY-MM-DD)：\n\n" + text[:4000]
        ),
        schema=PersonalIdentity,
        task="parse_ssn_text",
    )
    if result.error is not None or result.parsed is None:
        return None
    return result.parsed


def _parse_statement_of_info(pdf_bytes: bytes) -> CompanyFiling | None:
    """从加州州务卿信息申报表（Statement of Information）PDF 提取公司登记信息。"""
    text = extract_pdf_text(pdf_bytes)
    if not text:
        return None
    provider = HttpxOpenAICompatibleProvider()
    result = provider.complete_json(
        system_prompt="你是档案录入助手，从加州州务卿信息申报表文本中提取公司登记信息。取不到的字段填 null。",
        user_prompt=(
            "从以下文本提取 state_file_no(州登记编号，形如 B2026… 或 File No)、"
            "business_type(业务类型，如 Cross-border e-commerce)、"
            "manager_name(经理/法人 Member/Manager 的英文全名)、"
            "dba(商号或别名，如无填 null)：\n\n" + text[:8000]
        ),
        schema=CompanyFiling,
        task="parse_statement_of_info",
    )
    if result.error is not None or result.parsed is None:
        return None
    return result.parsed


# ---- 填表 ----

_TRUE_TOKENS = {"yes", "true", "1", "on", "x", "√", "勾选", "勾"}


def _load_pdf(pdf_bytes: str | bytes | None, pdf_path: str | None = None) -> bytes:
    """工具参数里的文件内容：base64 字符串 / bytes / 本地路径，三选一。"""
    if pdf_bytes is not None:
        if isinstance(pdf_bytes, bytes):
            return pdf_bytes
        return base64.b64decode(pdf_bytes)
    if pdf_path:
        return Path(pdf_path).read_bytes()
    raise ValueError("必须提供 pdf_bytes（base64）或 pdf_path 之一")


class FillPdfFormResult(BaseModel):
    filled: list[str] = []
    unknown_fields: list[str] = []
    total_fields: int = 0
    output_path: str | None = None
    pdf_base64: str | None = None  # 仅当未指定 output_path 时返回（编程调用用，勿回传 LLM）


def _to_bytes(data: str | bytes) -> bytes:
    """兼容旧调用：base64 字符串或 bytes。"""
    if isinstance(data, bytes):
        return data
    return base64.b64decode(data)


def _resolve_checkbox_value(field: Any, value: Any) -> Any:
    """复选框字段：把 Yes/true 等肯定词解析成该字段的 on 状态值。"""
    if not isinstance(value, str) or value.strip().lower() not in _TRUE_TOKENS:
        return value
    states = field.get("/_States_") if field is not None else None
    if states:
        # 状态列表里 off 可能排在前面，取第一个非 off 状态作为勾选值
        off_tokens = {"/off", "/no", "/false", "/unset", "off", "no", "false"}
        for state in states:
            name = state[0] if isinstance(state, (list, tuple)) else state
            if str(name).lower() not in off_tokens:
                return name
    return "/Yes"


def _fill_pdf_form(
    pdf_bytes: str | bytes | None = None,
    field_values: dict[str, Any] | None = None,
    output_path: str | None = None,
    pdf_path: str | None = None,
) -> FillPdfFormResult:
    """填充 PDF acroform 表单。field_values 为 {字段名: 值}，值写入所有页。

    pdf_bytes（base64/bytes）或 pdf_path 二选一。
    填好的 PDF 写入 output_path（编排场景，避免 base64 回传 LLM）；
    未指定 output_path 时在结果里返回 pdf_base64（编程调用）。
    """
    field_values = field_values or {}
    reader = PdfReader(io.BytesIO(_load_pdf(pdf_bytes, pdf_path)))
    fields = reader.get_fields() or {}
    known_fill: dict[str, Any] = {}
    unknown: list[str] = []
    for name, value in field_values.items():
        field = fields.get(name)
        if field is None:
            unknown.append(name)
        else:
            known_fill[name] = _resolve_checkbox_value(field, value)

    writer = PdfWriter(clone_from=reader)
    if known_fill:
        # None = 所有页；auto_regenerate=False 跳过外观重绘，读表软件会自行渲染
        writer.update_page_form_field_values(None, known_fill, auto_regenerate=False)
    buf = io.BytesIO()
    writer.write(buf)
    data = buf.getvalue()

    result = FillPdfFormResult(
        filled=sorted(known_fill),
        unknown_fields=unknown,
        total_fields=len(fields),
    )
    if output_path:
        Path(output_path).write_bytes(data)
        result.output_path = str(output_path)
    else:
        result.pdf_base64 = base64.b64encode(data).decode()
    return result


# ---- 表格路由 / 字段列表 ----

def _route_form(pdf_bytes: str | bytes | None = None, pdf_path: str | None = None) -> dict:
    """路由一个表格 PDF：类型级 LLM rerank + 格式级结构指纹（见 router.py）。"""
    from .router import route_form

    return route_form(_load_pdf(pdf_bytes, pdf_path))


def _list_pdf_fields(pdf_bytes: str | bytes | None = None, pdf_path: str | None = None) -> dict:
    """列出 PDF 的 acroform 字段名（过滤掉 undefined 类机器名），填表前用。"""
    reader = PdfReader(io.BytesIO(_load_pdf(pdf_bytes, pdf_path)))
    fields = reader.get_fields() or {}
    names = sorted(k for k in fields if k and not k.lower().startswith("undefined"))
    return {"total": len(fields), "named_fields": names}


# ---- 客户档案取值 ----

def _get_commission_fields(
    commission_name: str, field_names: list[str] | None = None
) -> dict:
    """按公司名查委托方档案，返回可用的填表字段值。敏感字段（邮箱密码）不返回。"""
    from .formfill import get_commission_data

    data = get_commission_data(commission_name)
    if field_names and not data.get("error"):
        data = {k: data.get(k) for k in field_names}
    return data


def _auto_fill_form(
    commission_name: str,
    output_path: str,
    pdf_bytes: str | bytes | None = None,
    pdf_path: str | None = None,
) -> dict:
    """确定性一步填表：路由命中已知格式后按配置映射自动填，返回操作报告。"""
    from .formfill import auto_fill_form

    return auto_fill_form(
        _load_pdf(pdf_bytes, pdf_path), commission_name, output_path
    )


# ---- 工具定义 ----

@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    func: Callable[..., Any]


TOOL_REGISTRY: dict[str, Tool] = {
    "parse_ssn_text": Tool(
        name="parse_ssn_text",
        description="从 SSN 文本中提取个人姓名、住址、社会安全号、出生日期",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string", "description": "SSN 文本内容"}},
            "required": ["text"],
        },
        func=_parse_ssn_text,
    ),
    "parse_statement_of_info": Tool(
        name="parse_statement_of_info",
        description="从州信息申报表 PDF 提取州登记编号、业务类型、法人姓名、商号",
        parameters={
            "type": "object",
            "properties": {"pdf_bytes": {"type": "string", "description": "PDF 文件字节（base64）"}},
            "required": ["pdf_bytes"],
        },
        func=_parse_statement_of_info,
    ),
    "extract_driver_license": Tool(
        name="extract_driver_license",
        description="识别美国驾照图片，提取姓名、出生日期、驾照号、地址、有效期、性别、签发州",
        parameters={
            "type": "object",
            "properties": {
                "image_bytes": {"type": "string", "description": "图片字节（base64）"},
                "mime": {"type": "string", "description": "图片 MIME 类型"},
            },
            "required": ["image_bytes"],
        },
        func=extract_driver_license,
    ),
    "extract_ssn_card": Tool(
        name="extract_ssn_card",
        description="识别美国社会安全卡（SSN Card）图片，提取姓名与社会安全号",
        parameters={
            "type": "object",
            "properties": {
                "image_bytes": {"type": "string", "description": "图片字节（base64）"},
                "mime": {"type": "string", "description": "图片 MIME 类型"},
            },
            "required": ["image_bytes"],
        },
        func=extract_ssn_card,
    ),
    "extract_id_card": Tool(
        name="extract_id_card",
        description="识别中国居民身份证图片，提取姓名、身份证号、出生日期、性别、住址、民族",
        parameters={
            "type": "object",
            "properties": {
                "image_bytes": {"type": "string", "description": "图片字节（base64）"},
                "mime": {"type": "string", "description": "图片 MIME 类型"},
            },
            "required": ["image_bytes"],
        },
        func=extract_id_card,
    ),
    "fill_pdf_form": Tool(
        name="fill_pdf_form",
        description=(
            "填充 PDF 表单（acroform）：按 {字段名: 值} 写入，填好的 PDF 保存到 output_path，"
            "返回填充报告（已填字段、表单中不存在的字段）。复选框字段传 Yes 即可。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "pdf_bytes": {"type": "string", "description": "待填 PDF 文件字节（base64），与 pdf_path 二选一"},
                "pdf_path": {"type": "string", "description": "待填 PDF 的本地路径，与 pdf_bytes 二选一"},
                "field_values": {
                    "type": "object",
                    "description": "字段名到值的映射，复选框传 Yes/No",
                },
                "output_path": {"type": "string", "description": "填好的 PDF 保存路径"},
            },
            "required": ["field_values", "output_path"],
        },
        func=_fill_pdf_form,
    ),
    "route_form": Tool(
        name="route_form",
        description=(
            "判断一份表格 PDF 属于哪种已知类型（个人财务报表/海关保函申请表/信用授权书）"
            "和哪种已知格式；都不命中则说明是新类型或新格式。填表前先用它路由。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "pdf_bytes": {"type": "string", "description": "PDF 文件字节（base64），与 pdf_path 二选一"},
                "pdf_path": {"type": "string", "description": "PDF 本地路径，与 pdf_bytes 二选一"},
            },
        },
        func=_route_form,
    ),
    "list_pdf_fields": Tool(
        name="list_pdf_fields",
        description="列出 PDF 表单的全部字段名（过滤机器生成的 undefined 名），用于构造填表映射",
        parameters={
            "type": "object",
            "properties": {
                "pdf_bytes": {"type": "string", "description": "PDF 文件字节（base64），与 pdf_path 二选一"},
                "pdf_path": {"type": "string", "description": "PDF 本地路径，与 pdf_bytes 二选一"},
            },
        },
        func=_list_pdf_fields,
    ),
    "get_commission_fields": Tool(
        name="get_commission_fields",
        description=(
            "按公司名查委托方档案，返回公司登记信息与法人个人信息"
            "（公司名、EIN、地址、注册州、州登记号、签字人、SSN、驾照等），供填表取值"
        ),
        parameters={
            "type": "object",
            "properties": {
                "commission_name": {"type": "string", "description": "委托公司名称（支持模糊匹配）"},
                "field_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "只取这些字段（可选，不传返回全部）",
                },
            },
            "required": ["commission_name"],
        },
        func=_get_commission_fields,
    ),
    "auto_fill_form": Tool(
        name="auto_fill_form",
        description=(
            "确定性一步填表：先路由表格，命中已登记格式后按配置映射从委托档案自动取值填充，"
            "返回操作报告（已填字段、档案缺值跳过的字段、映射未覆盖的字段）。"
            "仅对已登记格式（当前支持海关保函申请表 customs_bond_v1）可用；"
            "未命中格式时改用 route_form + list_pdf_fields + get_commission_fields + fill_pdf_form 组合。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "commission_name": {"type": "string", "description": "委托公司名称（支持模糊匹配）"},
                "pdf_bytes": {"type": "string", "description": "待填 PDF 文件字节（base64），与 pdf_path 二选一"},
                "pdf_path": {"type": "string", "description": "待填 PDF 的本地路径，与 pdf_bytes 二选一"},
                "output_path": {"type": "string", "description": "填好的 PDF 保存路径"},
            },
            "required": ["commission_name", "output_path"],
        },
        func=_auto_fill_form,
    ),
}


def tool_schemas() -> list[dict]:
    """导出 OpenAI function calling 格式的工具定义列表。"""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        for tool in TOOL_REGISTRY.values()
    ]


def invoke_tool(name: str, **kwargs: Any) -> Any:
    """按名称调用工具。"""
    tool = TOOL_REGISTRY.get(name)
    if tool is None:
        raise ValueError(f"未知工具：{name}")
    return tool.func(**kwargs)
