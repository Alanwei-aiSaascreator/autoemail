"""一期邮件意图识别：先用确定性规则完成高价值场景。

当前优先保证付款信息不漏掉；LLM 意图识别接口后续接入时，返回结果仍需符合本模块的结构。
"""

import re
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal

from .channels.base import NormalizedMessage
from .ingest.pdf import extract_invoice_pdf_text
from .llm import LLMProvider
from .llm_schemas import LLMIntentOutput
from .noise import clean_body

AMOUNT_RE = re.compile(
    r"(?P<symbol>\$|USD\s*)?(?P<amount>\d{1,3}(?:[,\s]\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)\s*(?P<currency>USD|CAD|US\s*DOLLARS?|DOLLARS?)?",
    re.IGNORECASE,
)
INVOICE_NO_RE = re.compile(
    r"invoice\s*(?:no\.?|number|#)?\s*[:#-]?\s*(\d[\w-]{1,30})",
    re.IGNORECASE,
)
DATE_RE = re.compile(
    r"\b(?P<month>\d{1,2})[/-](?P<day>\d{1,2})[/-](?P<year>\d{2,4})\b"
)
PAYMENT_TERMS = {
    "wire": (r"\bwire\b", r"\bwire transfer\b", r"\bbank wire\b", "电汇"),
    "ach": (r"\bach\b", r"\bautomated clearing\b", "银行转账"),
    "card": (r"\bcredit card\b", r"\bdebit card\b", r"\bcard payment\b", "银行卡"),
    "zelle": (r"\bzelle\b",),
}

# 付款方式说明（"we accept payment via PayCargo"、"payment method" 等）是代理商告知
# 付款渠道，不构成"要求付款"信号。此类语境需在判断付款信号前剔除，避免把报价/材料
# 邮件误判为发票。
_PAYMENT_METHOD_MENTION_RE = re.compile(
    r"accept(?:ed)?s?\s+payments?|"
    r"payments?\s+(?:via|through|using|method|link)|"
    r"how\s+to\s+pay",
    re.IGNORECASE,
)

# 代理商要求填表时，正文里列出的具体材料类型（用于后续按清单响应，
# 而不是一律套用标准三件套）。顺序即提取顺序。
_MATERIAL_RULES: list[tuple[str, re.Pattern]] = [
    ("application", re.compile(r"bond\s+application|application\s+form|\bapplication\b", re.IGNORECASE)),
    ("personal_financial", re.compile(r"personal\s+financial\s+statement", re.IGNORECASE)),
    ("financial_statement", re.compile(r"financial\s+statement|income\s+statement|balance\s+sheet", re.IGNORECASE)),
    ("ownership", re.compile(r"ownership\s+(?:breakdown|percentage)|\bownership\b", re.IGNORECASE)),
    ("ad_cvd", re.compile(r"anti[- ]?dumping|countervailing|ad/?cvd", re.IGNORECASE)),
]


def _extract_material_requirements(body_text: str) -> list[str]:
    """从正文提取代理商要求的材料清单（申请表/财务报表/股权结构/反倾销反补贴/个人财务报表）。"""
    text = body_text or ""
    materials: list[str] = []
    for key, pattern in _MATERIAL_RULES:
        if pattern.search(text) and key not in materials:
            materials.append(key)
    return materials


@dataclass
class PaymentInfo:
    amount: Decimal | None = None
    currency: str = "USD"
    due_date: date | None = None
    payment_method: str = "unknown"
    remit_detail: dict[str, str] = field(default_factory=dict)
    invoice_no: str | None = None
    confidence: float = 0.0


@dataclass
class IntentResult:
    label: str
    confidence: float
    slots: dict[str, object] = field(default_factory=dict)
    payment: PaymentInfo | None = None
    evidence: list[str] = field(default_factory=list)


def _parse_amount(match: re.Match[str]) -> tuple[Decimal, str]:
    raw = re.sub(r"[,\s]", "", match.group("amount"))
    currency = (match.group("currency") or "").upper().replace(" ", "")
    if currency in {"DOLLARS", "USDOLLARS"} or match.group("symbol") == "$":
        currency = "USD"
    return Decimal(raw), currency or "USD"


def _find_amount(text: str) -> tuple[Decimal | None, str]:
    matches = list(AMOUNT_RE.finditer(text))
    # 优先选择带 $ / 币种的金额，避免把主题中的 Invoice 1042 当成金额。
    prioritized = sorted(
        matches,
        key=lambda match: bool(match.group("symbol") or match.group("currency")),
        reverse=True,
    )
    for match in prioritized:
        start = max(0, match.start() - 24)
        context = text[start : match.end() + 16].lower()
        if match.group("symbol") or match.group("currency") or any(
            word in context for word in ("amount", "due", "invoice", "payment", "premium", "total", "price")
        ):
            # 邮件主题中的 Invoice 编号（如 Invoice 1042）不是付款金额。
            if "invoice" in context and not any(
                word in context for word in ("amount", "due", "payment", "total")
            ) and not match.group("symbol") and not match.group("currency"):
                continue
            return _parse_amount(match)
    return None, "USD"


def _find_payment_method(text: str) -> str:
    lowered = text.lower()
    matches = [
        method
        for method, terms in PAYMENT_TERMS.items()
        if any(
            re.search(term, lowered, re.IGNORECASE) if term.startswith(r"\b") else term.lower() in lowered
            for term in terms
        )
    ]
    return matches[0] if len(matches) == 1 else (matches[0] if matches else "unknown")


def _find_due_date(text: str) -> date | None:
    lowered = text.lower()
    for match in DATE_RE.finditer(text):
        context = lowered[max(0, match.start() - 18) : match.start()]
        if "due" not in context and "截止" not in context and "payment" not in context:
            continue
        year = int(match.group("year"))
        if year < 100:
            year += 2000
        try:
            return date(year, int(match.group("month")), int(match.group("day")))
        except ValueError:
            return None
    return None


def _extract_remit_detail(text: str) -> dict[str, str]:
    detail: dict[str, str] = {}
    patterns = {
        "recipient": r"(?:to|payable to|收款人)\s*[:：]?\s*([^\n,;]+)",
        "account_last4": r"(?:account|账号)\s*(?:ending|末四位)?\s*[:#-]?\s*(?:\*+)?(\d{4})\b",
        "routing_last4": r"(?:routing|路由)\s*(?:number)?\s*[:#-]?\s*(?:\*+)?(\d{4})\b",
        "payment_link": r"https?://[^\s>]+",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            detail[key] = match.group(1) if match.lastindex else match.group(0)
    return detail


def _find_invoice_no(text: str) -> str | None:
    match = INVOICE_NO_RE.search(text)
    return match.group(1).strip("-,.") if match else None


def extract_payment_info(message: NormalizedMessage) -> PaymentInfo | None:
    """从邮件正文和附件中提取付款信息；没有付款信号则返回 None。"""
    attachment_names = " ".join(a.filename for a in message.attachments)
    pdf_text = extract_invoice_pdf_text(message.attachments)
    text = f"{message.subject}\n{message.body_text}\n{attachment_names}"
    if pdf_text:
        # 正文常只写 "please find attached"；金额、到期日、发票号以 PDF 文本兜底
        text = f"{text}\n{pdf_text}"
    lowered = text.lower()
    # 剔除"付款方式说明"后再判断付款信号：签名里的 "we accept payment via PayCargo"
    # 不应把一封报价/材料邮件误判成发票。
    signal_text = _PAYMENT_METHOD_MENTION_RE.sub(" ", lowered)
    amount, currency = _find_amount(text)
    if amount is not None and amount <= 0:
        # $0 常见于营销文案（立省 $0、免费额度），不能当作应付金额
        amount = None
    due_date = _find_due_date(text)
    has_invoice_attachment = any(a.doc_type == "invoice" for a in message.attachments)
    strong_signal = any(
        term in signal_text
        for term in (
            "invoice", "amount due", "remit", "zelle", "wire transfer",
            "ach payment", "payment instructions", "付款", "发票",
        )
    )
    # 只有 "payment" 一个词不够：营销邮件也常出现，必须再带金额、到期日或发票附件
    weak_signal = any(term in signal_text for term in ("payment", "paid"))
    if not (strong_signal or has_invoice_attachment or (weak_signal and (amount is not None or due_date is not None))):
        return None

    method = _find_payment_method(text)
    detail = _extract_remit_detail(text)
    confidence = 0.55
    if amount is not None:
        confidence += 0.2
    if method != "unknown":
        confidence += 0.2
    if any(a.doc_type == "invoice" for a in message.attachments):
        confidence += 0.05

    return PaymentInfo(
        amount=amount,
        currency=currency,
        due_date=due_date,
        payment_method=method,
        remit_detail=detail,
        invoice_no=_find_invoice_no(text),
        confidence=min(confidence, 1.0),
    )


def _find_quote_amount(text: str) -> tuple[Decimal | None, str]:
    """报价邮件优先取 premium/price/quote 所在语句，避免把 Bond 面额当报价。"""
    for line in text.splitlines():
        lowered = line.lower()
        if any(word in lowered for word in ("premium", "annual price", "quote", "报价", "价格")):
            amount, currency = _find_amount(line)
            if amount is not None:
                return amount, currency
    return _find_amount(text)


def classify_message(message: NormalizedMessage) -> IntentResult:
    # 只看本次新增正文：引用旧邮件里的 invoice/报价不应决定本次意图
    body = clean_body(message.body_text, limit=20000)
    message = replace(message, body_text=body)
    payment = extract_payment_info(message)
    if payment is not None:
        evidence = ["正文或附件命中付款信号"]
        if payment.amount is not None:
            evidence.append(f"识别金额 {payment.amount} {payment.currency}")
        if payment.payment_method != "unknown":
            evidence.append(f"识别付款方式 {payment.payment_method}")
        return IntentResult(
            label="invoice",
            confidence=payment.confidence,
            slots={
                "amount": payment.amount,
                "currency": payment.currency,
                "due_date": payment.due_date,
                "payment_method": payment.payment_method,
            },
            payment=payment,
            evidence=evidence,
        )

    attachment_types = {a.doc_type for a in message.attachments}
    # 正文向我们要表格/材料（如 "forms we would need"、"completed and signed"）时，
    # 即使附件名含 bond 也不能判 bond_delivered：已签发的 Bond 不会附带填表要求。
    # 附件是空白申请表（application/form/5106 等）同理，优先判 form_request。
    _FORM_REQUEST_BODY_RE = re.compile(
        r"forms?\s+(?:and\s+\w+\s+information\s+)?we\s+(?:would|will)\s+need|"
        r"need\s+(?:will\s+need\s+)?(?:the\s+)?attached\s+(?:application|form|forms)|"
        r"(?:thoroughly\s+)?completed\s+and\s+signed|"
        r"(?:please\s+)?(?:complete\s+and\s+return|complete|fill(?:\s+out)?|return|submit)(?:\s+the)?\s+(?:attached\s+)?(?:application|form|bond application)|"
        r"fill\s+out\s+the\s+(?:application|form)|"
        r"申请表|请填写",
        re.IGNORECASE,
    )
    if _FORM_REQUEST_BODY_RE.search(message.body_text):
        return IntentResult(
            "form_request", 0.85, {
                "attachments": sorted(attachment_types),
                "materials": _extract_material_requirements(message.body_text),
            },
            evidence=["正文要求填写/提交表格材料"],
        )
    if "bond" in attachment_types:
        return IntentResult("bond_delivered", 0.9, {"bond_ref": message.subject}, evidence=["附件识别为 Bond"])
    if attachment_types & {"application", "poa", "articles"}:
        return IntentResult("form_request", 0.85, {"attachments": sorted(attachment_types)}, evidence=["附件包含申请材料"])
    if any(word in message.body_text.lower() for word in ("please complete", "application form", "申请表")):
        return IntentResult("form_request", 0.8, evidence=["正文请求填写申请表"])
    if any(word in f"{message.subject}\n{message.body_text}".lower() for word in ("quote", "premium", "price", "报价", "价格")):
        amount, currency = _find_quote_amount(f"{message.subject}\n{message.body_text}")
        return IntentResult(
            "quote_reply",
            0.8 if amount is not None else 0.65,
            {"amount": amount, "currency": currency},
            evidence=["正文命中报价关键词"],
        )
    return IntentResult("unclear", 0.35, evidence=["确定性规则未识别意图"])
