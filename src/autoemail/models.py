"""数据模型（对应技术方案 V0.2 §3）。

设计要点：
- 业务最小单元是 case（委托 × 代理商），不是 commission；
- 敏感证件是否可进模型由 attachment.model_visible 控制（§9）；
- 所有出站消息必须过 send_ledger 抢占，防重复发送。
"""

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

TERMINAL_STATES = ("closed", "lost")
INVOICE_UNPAID = "unpaid"
INVOICE_PAID = "paid"
DRAFT_PENDING = "pending"
DRAFT_BLOCKED = "blocked"
DRAFT_SENDING = "sending"
DRAFT_SENT = "sent"
DRAFT_REJECTED = "rejected"
DRAFT_FAILED = "failed"
DRAFT_REVIEWABLE_STATES = (DRAFT_PENDING, DRAFT_BLOCKED, DRAFT_FAILED)
FOLLOWUP_PENDING = "pending"
FOLLOWUP_RUNNING = "running"
FOLLOWUP_DONE = "done"
FOLLOWUP_CANCELLED = "cancelled"
FOLLOWUP_FAILED = "failed"
FOLLOWUP_EXHAUSTED = "exhausted"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Commission(Base):
    """委托公司档案：填表数据的唯一来源。"""

    __tablename__ = "commission"

    id: Mapped[int] = mapped_column(primary_key=True)
    legal_name: Mapped[str] = mapped_column(String(200))
    legal_name_norm: Mapped[str] = mapped_column(String(200), index=True)
    entity_type: Mapped[str] = mapped_column(String(40))
    ein: Mapped[str | None] = mapped_column(String(20), index=True)
    ior: Mapped[str | None] = mapped_column(String(40), index=True)
    state_of_inc: Mapped[str | None] = mapped_column(String(8))
    addr_line1: Mapped[str | None] = mapped_column(String(200))
    addr_city: Mapped[str | None] = mapped_column(String(100))
    addr_state: Mapped[str | None] = mapped_column(String(8))
    addr_zip: Mapped[str | None] = mapped_column(String(16))
    signer_name: Mapped[str | None] = mapped_column(String(120))
    signer_title: Mapped[str | None] = mapped_column(String(120))
    customs_broker: Mapped[str | None] = mapped_column(String(120))
    mailbox_email: Mapped[str | None] = mapped_column(String(200))
    mailbox_password: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(40))
    # 公司补充信息（来自州信息申报表 / 客户文件）
    state_file_no: Mapped[str | None] = mapped_column(String(40))
    business_type: Mapped[str | None] = mapped_column(String(200))
    dba: Mapped[str | None] = mapped_column(String(200))
    # 法人/签字人个人信息（来自 SSN.txt / 驾照识别）
    principal_full_name: Mapped[str | None] = mapped_column(String(120))
    principal_dob: Mapped[str | None] = mapped_column(String(16))
    principal_sex: Mapped[str | None] = mapped_column(String(8))
    personal_address: Mapped[str | None] = mapped_column(String(300))
    ssn: Mapped[str | None] = mapped_column(String(16))
    driver_license_no: Mapped[str | None] = mapped_column(String(40))
    driver_license_state: Mapped[str | None] = mapped_column(String(8))
    driver_license_expiry: Mapped[str | None] = mapped_column(String(16))
    operator: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Agent(Base):
    """代理商档案主体。"""

    __tablename__ = "agent"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    contact_name: Mapped[str | None] = mapped_column(String(120))
    email_domains: Mapped[list] = mapped_column(JSON, default=list)
    email_addresses: Mapped[list] = mapped_column(JSON, default=list)
    channel_type: Mapped[str] = mapped_column(String(20), default="email")
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentPolicy(Base):
    """PRD 第四章"流程特征与注意点"的结构化落点。"""

    __tablename__ = "agent_policy"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agent.id"), unique=True)
    material_checklist: Mapped[list] = mapped_column(JSON, default=list)
    extra_fees: Mapped[list] = mapped_column(JSON, default=list)
    binding_clauses: Mapped[str | None] = mapped_column(Text)
    # after_cbp_approval（A1 模式）/ after_quote（其他家）
    invoice_timing: Mapped[str] = mapped_column(String(24), default="after_quote")
    # acroform_pdf / excel / docx_tpl / flat_pdf / web_portal / drive_upload
    submit_channel: Mapped[str] = mapped_column(String(24), default="acroform_pdf")
    requires_broker_for_customs: Mapped[bool] = mapped_column(Boolean, default=False)
    bond_copy_commitment: Mapped[bool] = mapped_column(Boolean, default=True)
    asks_adcvd: Mapped[bool] = mapped_column(Boolean, default=False)
    payment_methods: Mapped[list] = mapped_column(JSON, default=list)


class Case(Base):
    """状态机实例：一个委托在一个代理商处的完整生命周期。"""

    __tablename__ = "case"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_no: Mapped[str] = mapped_column(String(24), unique=True)
    commission_id: Mapped[int] = mapped_column(ForeignKey("commission.id"), index=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agent.id"), index=True)
    state: Mapped[str] = mapped_column(String(24), default="init")
    state_entered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    owner_user: Mapped[str | None] = mapped_column(String(120))
    # off（全人工）/ dry_run（只出草稿）/ on（可自动发送）
    auto_mode: Mapped[str] = mapped_column(String(16), default="off")
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_outbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    waiting_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_followup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    followup_count: Mapped[int] = mapped_column(default=0)
    followup_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    followup_pause_reason: Mapped[str | None] = mapped_column(String(200))
    poa_flags: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        # 同一委托同一代理商不允许两个活跃 case
        Index(
            "uq_case_active",
            "commission_id",
            "agent_id",
            unique=True,
            sqlite_where=text("state NOT IN ('closed','lost')"),
            postgresql_where=text("state NOT IN ('closed','lost')"),
        ),
    )


class FollowUpTask(Base):
    __tablename__ = "followup_task"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("case.id"), index=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey("thread.id"), index=True)
    task_type: Mapped[str] = mapped_column(String(32), default="followup")
    status: Mapped[str] = mapped_column(String(16), default=FOLLOWUP_PENDING, index=True)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=2)
    last_error: Mapped[str | None] = mapped_column(Text)
    trigger_state: Mapped[str] = mapped_column(String(24))
    trigger_message_id: Mapped[int | None] = mapped_column(ForeignKey("message.id"))
    draft_id: Mapped[int | None] = mapped_column(ForeignKey("draft.id"), index=True)
    sequence_no: Mapped[int] = mapped_column(default=1)
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Thread(Base):
    __tablename__ = "thread"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int | None] = mapped_column(ForeignKey("case.id"), index=True)
    channel: Mapped[str] = mapped_column(String(20), default="email")
    subject_norm: Mapped[str] = mapped_column(String(400))
    participants: Mapped[list] = mapped_column(JSON, default=list)
    frozen: Mapped[bool] = mapped_column(Boolean, default=False)
    freeze_reason: Mapped[str | None] = mapped_column(String(200))
    first_msg_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_msg_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Message(Base):
    __tablename__ = "message"

    id: Mapped[int] = mapped_column(primary_key=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey("thread.id"), index=True)
    direction: Mapped[str] = mapped_column(String(8))  # in / out
    channel: Mapped[str] = mapped_column(String(20), default="email")
    channel_msg_id: Mapped[str | None] = mapped_column(String(400), index=True)
    in_reply_to: Mapped[str | None] = mapped_column(String(400))
    references: Mapped[list] = mapped_column(JSON, default=list)
    from_addr: Mapped[str] = mapped_column(String(320))
    to_addrs: Mapped[list] = mapped_column(JSON, default=list)
    cc_addrs: Mapped[list] = mapped_column(JSON, default=list)
    subject: Mapped[str] = mapped_column(String(600))
    body_text: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dedupe_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("thread_id", "dedupe_hash", name="uq_message_dedupe"),)


class Attachment(Base):
    __tablename__ = "attachment"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("message.id"), index=True)
    filename: Mapped[str] = mapped_column(String(400))
    mime: Mapped[str | None] = mapped_column(String(120))
    size: Mapped[int] = mapped_column(default=0)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    storage_key: Mapped[str | None] = mapped_column(String(400))
    doc_type: Mapped[str | None] = mapped_column(String(40))
    expiry_date: Mapped[date | None] = mapped_column(Date)
    model_visible: Mapped[bool] = mapped_column(Boolean, default=True)


class CommissionDocument(Base):
    """委托方文件：公司章程、税号文件、法人证件。询价前必须齐全。"""

    __tablename__ = "commission_document"

    id: Mapped[int] = mapped_column(primary_key=True)
    commission_id: Mapped[int] = mapped_column(ForeignKey("commission.id"), index=True)
    # articles（公司章程）/ ein（税号文件）/ id（法人证件）
    doc_type: Mapped[str] = mapped_column(String(24), index=True)
    filename: Mapped[str] = mapped_column(String(400))
    mime: Mapped[str | None] = mapped_column(String(120))
    size: Mapped[int] = mapped_column(default=0)
    sha256: Mapped[str] = mapped_column(String(64))
    content: Mapped[bytes] = mapped_column(LargeBinary)
    uploaded_by: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("commission_id", "doc_type", name="uq_commission_document_type"),
    )


class SignatureAsset(Base):
    """客户本人提供的签名图样：客户自己的电子签名素材 + 授权依据留痕。

    只接受客户本人主动提供的签名图片，插入表单前必须有 authorization_note。
    """

    __tablename__ = "signature_asset"

    id: Mapped[int] = mapped_column(primary_key=True)
    commission_id: Mapped[int] = mapped_column(ForeignKey("commission.id"), index=True)
    filename: Mapped[str] = mapped_column(String(400))
    mime: Mapped[str | None] = mapped_column(String(120))
    size: Mapped[int] = mapped_column(default=0)
    sha256: Mapped[str] = mapped_column(String(64))
    content: Mapped[bytes] = mapped_column(LargeBinary)
    # 授权依据必须留痕：客户微信/邮件原话、日期、授权范围等
    authorization_note: Mapped[str] = mapped_column(Text)
    uploaded_by: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CommissionIntake(Base):
    """客户需求采集表（client_bond_application_v1）解析后的结构化字段。

    委托专属、隔离存储；填表从这里取数（不依赖飞书实时拉取）。
    委托结束（closed/lost）后随原始文件一并删除，不留存。
    """

    __tablename__ = "commission_intake"

    id: Mapped[int] = mapped_column(primary_key=True)
    commission_id: Mapped[int] = mapped_column(ForeignKey("commission.id"), index=True)
    # 解析出的字段值 {字段名(如 b001/d019): 值}
    fields: Mapped[dict] = mapped_column(JSON, default=dict)
    source_filename: Mapped[str | None] = mapped_column(String(400))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("commission_id", name="uq_commission_intake_commission"),
    )


class CommissionFile(Base):
    """委托专属原始文件库：保存每个委托全部资料文件的原本。

    与 CommissionDocument（询价必需 3 类）区分：这里保存所有资料文件（SSN、
    驾照正反面、人像、地址查验、房产、银行、官网、邮箱/电话等），按语义分类，
    供"代理要什么文件/信息"时检索并作为原始附件发出。委托结束后随其他数据删除。
    """

    __tablename__ = "commission_file"

    id: Mapped[int] = mapped_column(primary_key=True)
    commission_id: Mapped[int] = mapped_column(ForeignKey("commission.id"), index=True)
    # 语义分类：articles/ein/ssn/driver_license_front/driver_license_back/photo/
    # address_verification/street_view/property_info/bank_info/website/email/phone/
    # statement_of_information/filing_acknowledgment/client_application 等
    category: Mapped[str] = mapped_column(String(40), index=True)
    filename: Mapped[str] = mapped_column(String(400))
    mime: Mapped[str | None] = mapped_column(String(120))
    size: Mapped[int] = mapped_column(default=0)
    sha256: Mapped[str] = mapped_column(String(64))
    content: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("commission_id", "filename", name="uq_commission_file_name"),
    )


class CommissionImport(Base):
    """飞书链接导入委托的留痕：来源链接、解析出的字段与缺口。

    一个新客户 = 一份飞书资料文档；粘贴链接导入后在这里记录
    导入了哪些字段、还缺哪些，委托列表页按此展示数据来源。
    """

    __tablename__ = "commission_import"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 解析失败（连公司名都没有）时允许无关联委托，仅留错误记录
    commission_id: Mapped[int | None] = mapped_column(ForeignKey("commission.id"), index=True)
    source_url: Mapped[str] = mapped_column(String(600))
    # docx / doc / wiki / sheet / base
    doc_kind: Mapped[str] = mapped_column(String(16), default="docx")
    doc_title: Mapped[str | None] = mapped_column(String(300))
    # 提取出的全部字段（含未入库的），便于人工核对
    fields: Mapped[dict] = mapped_column(JSON, default=dict)
    filled: Mapped[list] = mapped_column(JSON, default=list)
    missing: Mapped[list] = mapped_column(JSON, default=list)
    # llm / rules（字段提取方式）
    via: Mapped[str] = mapped_column(String(16), default="rules")
    # ok / failed
    status: Mapped[str] = mapped_column(String(16), default="ok", index=True)
    error: Mapped[str | None] = mapped_column(Text)
    imported_by: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CommissionKnowledge(Base):
    """公司专属知识库：飞书导入材料解析出的正文文本，按公司隔离。

    与 CommissionFile（原始文件字节）互补：这里存"解析后的文字"，
    供代理问询时检索、填表取数、LLM 起草引用。重复导入同一来源覆盖。
    """

    __tablename__ = "commission_knowledge"

    id: Mapped[int] = mapped_column(primary_key=True)
    commission_id: Mapped[int] = mapped_column(ForeignKey("commission.id"), index=True)
    # 来源文件名（在线文档用文档名）
    source: Mapped[str] = mapped_column(String(400))
    # 语义分类（同 CommissionFile.category；在线文档为 online_doc）
    category: Mapped[str] = mapped_column(String(40), index=True)
    text: Mapped[str] = mapped_column(Text)
    # text（txt/pdf/docx 直读）/ vision（多模态识别）
    via: Mapped[str] = mapped_column(String(16), default="text")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("commission_id", "source", name="uq_commission_knowledge_source"),
    )


class Quote(Base):
    __tablename__ = "quote"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("case.id"), index=True)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("message.id"))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    term_months: Mapped[int | None] = mapped_column()
    extras: Mapped[list] = mapped_column(JSON, default=list)
    total_first_year: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    vs_history_ratio: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Invoice(Base):
    __tablename__ = "invoice"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("case.id"), index=True)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("message.id"))
    # 代理商发票号；正文或 PDF 里提取，用于同案重复来票去重
    invoice_no: Mapped[str | None] = mapped_column(String(64), index=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    due_date: Mapped[date | None] = mapped_column(Date)
    # wire / ach / card / zelle / unknown（方式由 broker 决定）
    payment_method: Mapped[str] = mapped_column(String(16), default="unknown")
    remit_detail: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default=INVOICE_UNPAID, index=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_method: Mapped[str | None] = mapped_column(String(16))
    paid_reference: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Draft(Base):
    __tablename__ = "draft"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("case.id"), index=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey("thread.id"), index=True)
    source_message_id: Mapped[int | None] = mapped_column(ForeignKey("message.id"), index=True)
    intent: Mapped[str] = mapped_column(String(40))
    subject: Mapped[str] = mapped_column(String(600))
    to_addrs: Mapped[list] = mapped_column(JSON, default=list)
    cc_addrs: Mapped[list] = mapped_column(JSON, default=list)
    body: Mapped[str] = mapped_column(Text)
    # 随草稿一起发送的本地文件路径（自动填表产物等）
    attachments: Mapped[list] = mapped_column(JSON, default=list)
    template_id: Mapped[str | None] = mapped_column(String(80))
    controlled_fields: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default=DRAFT_PENDING, index=True)
    rule_hits: Mapped[list] = mapped_column(JSON, default=list)
    notices: Mapped[list] = mapped_column(JSON, default=list)
    model_id: Mapped[str | None] = mapped_column(String(80))
    input_hash: Mapped[str | None] = mapped_column(String(64))
    reviewer: Mapped[str | None] = mapped_column(String(120))
    edited_diff: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(default=1)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    send_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("source_message_id", "intent", name="uq_draft_source_intent"),
    )


class AdcvdAttestation(Base):
    """AD/CVD 声明的人工签署记录：Broker 审、IOR 担责（V0.2 §7.3）。"""

    __tablename__ = "adcvd_attestation"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("case.id"), index=True)
    hts_code: Mapped[str] = mapped_column(String(40))
    country_of_origin: Mapped[str] = mapped_column(String(80))
    statement: Mapped[str] = mapped_column(Text)
    attested_by: Mapped[str] = mapped_column(String(120))
    attested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FormTypeRegistry(Base):
    """新类型表单储备库（类型 ≠ 格式：格式差异由 LLM 克服，类型才是储备单元）。

    LLM 判定来信表格属于从未见过的类型（含我们没遇到过的字段）时，
    在对应 case 详情页提示；用户点击"计入储备"后转为 converted，
    该类型的字段清单沉淀进 form_mappings.json 等人工补充映射。
    """

    __tablename__ = "form_type_registry"

    id: Mapped[int] = mapped_column(primary_key=True)
    type_id: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    # pending（待人工确认）/ converted（已计入储备）
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    source_case_id: Mapped[int | None] = mapped_column(ForeignKey("case.id"), index=True)
    source_attachment_id: Mapped[int | None] = mapped_column(ForeignKey("attachment.id"))
    source_filename: Mapped[str | None] = mapped_column(String(400))
    # 样本 acroform 字段名列表（新类型的字段证据）
    sample_fields: Mapped[list] = mapped_column(JSON, default=list)
    reason: Mapped[str | None] = mapped_column(Text)
    converted_by: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    converted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEvent(Base):
    """每条出站动作都要能回答"为什么发了出去"。"""

    __tablename__ = "audit_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(120), default="system")
    action: Mapped[str] = mapped_column(String(60), index=True)
    case_id: Mapped[int | None] = mapped_column(ForeignKey("case.id"), index=True)
    thread_id: Mapped[int | None] = mapped_column(ForeignKey("thread.id"))
    message_id: Mapped[int | None] = mapped_column(ForeignKey("message.id"))
    draft_id: Mapped[int | None] = mapped_column(ForeignKey("draft.id"), index=True)
    rule_ids: Mapped[list] = mapped_column(JSON, default=list)
    model_id: Mapped[str | None] = mapped_column(String(80))
    input_hash: Mapped[str | None] = mapped_column(String(64))
    decision: Mapped[str | None] = mapped_column(String(24))
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    latency_ms: Mapped[int | None] = mapped_column()


class SendLedger(Base):
    """出站幂等表：唯一约束即抢占。"""

    __tablename__ = "send_ledger"

    id: Mapped[int] = mapped_column(primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True)
    draft_id: Mapped[int | None] = mapped_column(ForeignKey("draft.id"))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    channel_msg_id: Mapped[str | None] = mapped_column(String(400))
    result: Mapped[str | None] = mapped_column(String(40))
