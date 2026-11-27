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


# ---- 回复全量计划：LLM 决策，框架执行（事实全部来自 DB/需求表，模型不写事实） ----

class PlanInlineAnswer(BaseModel):
    """对方提出的一个具体问题 → 该用哪个确定性应答器回答（LLM 只选类别不写内容）。"""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=500)
    answer_key: Literal[
        "ownership",        # 股权结构 → 法人 100% 持股口径
        "ad_cvd",           # 反倾销/反补贴 → 需求表 a007 + HTS 联网核对
        "trade_profile",    # 贸易概况（货品/原产地/频次/口岸/货值/税费）
        "request_quote",    # 请对方给出报价与时效
        "effective_date",   # 期望生效日期（需求表 a006）
        "existing_bond",    # 存量/在途保函情况（需求表 a012）
        "documents_ready",  # 三件套（章程/EIN 信/证件）已备妥可随时提交
        "acknowledge",      # 已知悉/收到，无实质回答
    ]


class PlanAttachment(BaseModel):
    """一个附件的处置决定。能否真填由路由器/格式库判定，模型只决定动不动它。"""

    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=400)
    disposition: Literal[
        "fill_sign_return",  # 我方代填代签后随回复附回
        "keep_original",     # 原样附回（如对方要求提交原件）
        "ignore",            # 无需处理（宣传材料/无关文件）
    ]
    note: str = Field(default="", max_length=300)


class ReplyPlanOutput(BaseModel):
    """一封来信的全量回复计划。所有枚举之外的选择都会被校验拒绝。"""

    model_config = ConfigDict(extra="forbid")

    intent: Literal["quote_reply", "form_request", "bond_delivered", "unclear"]
    confidence: confloat(ge=0, le=1)
    materials: list[MaterialKey] = Field(default_factory=list, max_length=8)
    inline_answers: list[PlanInlineAnswer] = Field(default_factory=list, max_length=12)
    attachments: list[PlanAttachment] = Field(default_factory=list, max_length=10)
    # 案件进度建议：none 表示不迁状态；其余必须命中状态机白名单才执行
    state_hint: Literal[
        "none", "quoted", "materials_requested", "quoted_out",
        "bond_received", "invoiced", "blocked", "closed", "lost",
    ] = "none"
    reasoning: str = Field(default="", max_length=800)
