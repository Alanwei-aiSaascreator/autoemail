"""LLM 受控结构化输出。模型只能返回这些字段。"""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, confloat


# 代理商要求的材料类型（机器可执行的 key，与 intent._MATERIAL_RULES 一致）
MaterialKey = Literal[
    "application",
    "personal_financial",
    "financial_statement",
    "ownership",
    "ad_cvd",
]


class LLMIntentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: Literal["quote_reply", "form_request", "bond_delivered", "unclear"]
    confidence: confloat(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list, max_length=8)
    needs_human_review: bool = True
    # 三层意图：form_request 时列出对方要求的具体材料（决定回复附什么、缺什么）
    materials: list[MaterialKey] = Field(default_factory=list, max_length=8)
    reasoning: str = Field(default="", max_length=500)


class LLMDraftSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=600)
    body: str = Field(min_length=1, max_length=12000)
    referenced_doc_ids: list[str] = Field(default_factory=list, max_length=10)
    uncertainty_notes: list[str] = Field(default_factory=list, max_length=8)


class LLMFormTypeOutput(BaseModel):
    """表单类型判定：新类型（含未见过的字段）≠ 已知类型的新格式（LLM 克服格式）。"""

    model_config = ConfigDict(extra="forbid")

    is_form: bool
    type_id: str = Field(min_length=1, max_length=80)
    type_name: str = Field(min_length=1, max_length=200)
    is_new_type: bool
    reason: str = Field(min_length=1, max_length=800)


class LLMCommissionExtractOutput(BaseModel):
    """飞书委托资料文档 → 委托档案字段。提取不到的字段留空字符串。"""

    model_config = ConfigDict(extra="forbid")

    legal_name: str = Field(default="", max_length=200)
    entity_type: str = Field(default="", max_length=40)
    ein: str = Field(default="", max_length=20)
    state_of_inc: str = Field(default="", max_length=8)
    addr_line1: str = Field(default="", max_length=200)
    addr_city: str = Field(default="", max_length=100)
    addr_state: str = Field(default="", max_length=8)
    addr_zip: str = Field(default="", max_length=16)
    signer_name: str = Field(default="", max_length=120)
    signer_title: str = Field(default="", max_length=120)
    phone: str = Field(default="", max_length=40)
    mailbox_email: str = Field(default="", max_length=200)
    notes: list[str] = Field(default_factory=list, max_length=10)


class LLMFieldMappingOutput(BaseModel):
    """已知类型的新格式：LLM 读字段名生成 field_mapping（格式克服）。"""

    model_config = ConfigDict(extra="forbid")

    # {PDF字段名: 档案列名 或 {"template": ...} / {"const": ...} / {"today": ...}}
    field_mapping: dict[str, str | dict[str, str]] = Field(default_factory=dict, max_length=200)
