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
    r"how\s+to\s+pay|"
    r"pay(?:ing)?\s+(?:your|our|the)?\s*(?:invoices?|bills?)\s+by\s+the\s+due\s+date|"
    r"send\s+(?:a\s+)?(?:wire|ach)\s+to|"
    r"payment\s+instructions?|"
    r"payment\s+methods?|"
    r"to\s+make\s+payments?\s+click",
    re.IGNORECASE,
)

# "向你收款"的动作语句：真发票/付款通知一定有这类表述，且后面跟着账单细节。
# 报价（Our service is $100）、条款（pay your invoices by the due date）、
# 防诈骗提示（do not send wire to accounts not on our invoice）都不命中。
_INVOICE_DEMAND_RE = re.compile(
    r"invoice\s+(?:is\s+)?attached|attached\s+(?:is\s+|please\s+find\s+)?(?:our\s+|the\s+)?invoice|"
    r"amount\s+due[:\s]|balance\s+due[:\s]|total\s+due[:\s]|"
    r"(?:please\s+)?remit\s+(?:the\s+)?(?:payment|amount|payment\s+of)|"
    r"pay(?:ment)?\s+(?:is\s+)?due\s+(?:by|on|within)|"
    r"pay\s+(?:this\s+|the\s+|your\s+)?invoice\b|"
    r"remittance\s+information|"
    r"应付金额|请付款|请查收发票",
    re.IGNORECASE,
)
# 金额 + "total/amount due" 的组合信号（兜底：有些发票正文极简，只有一句 Total due: $x）
_TOTAL_DUE_RE = re.compile(
    r"\b(?:total|amount|balance)\s+(?:amount\s+)?due\b", re.IGNORECASE
)

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


# 「Bond 已送达」的强信号：仅在正文明确表示"已签发/交付 Bond"时才判 bond_delivered。
# 仅凭附件名含 bond（可能是空白申请表）不足以判，避免误判。
_BOND_DELIVERED_BODY_RE = re.compile(
    r"executed\s+(?:continuous\s+)?bond|"
    r"bond\s+(?:has\s+been\s+|was\s+)?issued|issued\s+(?:continuous\s+)?bond|"
    r"bond\s+number\s*[:#]?\s*\d|"
    r"bond\s+copy\s+(?:enclosed|attached|for\s+your\s+records)",
    re.IGNORECASE,
)


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
    """从邮件正文和附件中提取付款信息；没有付款信号则返回 None。

    判"这是一封发票/付款通知"需要结构性证据：真发票至少占其一——
    发票附件、正文发出"向你收款"的动作（amount due / please remit /
    pay by / invoice attached 等）、或独立的到期日。代理商签名与条款里的
    "pay your invoices by the due date"、"we accept payment via PayCargo"、
    "send wire/ACH to the account on our invoice" 只是付款方式说明，
    绝不能凭这些词把报价信判成发票。
    """
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
    if not has_invoice_attachment:
        # 无发票附件时，"向你收款"的动作语句才算发票信号（invoice attached /
        # amount due / please remit payment / pay the invoice 等）。
        # 单纯出现 payment/wire/zelle/invoice 字样（报价、条款、防诈骗提示）不算。
        demand_hit = _INVOICE_DEMAND_RE.search(signal_text)
        if not demand_hit:
            return None
        strong_signal = demand_hit is not None
        weak_signal = amount is not None and due_date is not None
        if not (strong_signal or weak_signal):
            return None
    else:
        strong_signal = True
        weak_signal = amount is not None and due_date is not None

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


# 报价/材料邮件里对方提出的具体问题 → 可确定性回答的 key（与 drafts 应答器一一对应）。
# 提问形态多变（"can you please help me with the following" + 列表、直接疑问句），
# 用关键词而非问号判定；漏识别的后果是对方问题悬空无人应答。
_QUESTION_RULES: list[tuple[str, re.Pattern]] = [
    # beneficial ownership / US status of owners（股权归属 + 股东美国身份）
    ("ownership", re.compile(
        r"beneficial\s+owners?|owners?hip\s+(?:of|structure|breakdown|percentage)|"
        r"us\s+status\s+of\s+(?:its\s+|their\s+)?owners?|who\s+(?:are\s+)?(?:the\s+)?owners?",
        re.IGNORECASE,
    )),
    # 进口什么商品（what kind of articles / commodity / goods）
    ("trade_profile", re.compile(
        r"what\s+(?:kind\s+of\s+)?(?:articles|commodity|commodities|goods|products?)|"
        r"commodity\s+you\s+are\s+importing|articles\s+does\s+\w+\s+import|"
        r"what\s+(?:are\s+you|do\s+you)\s+import",
        re.IGNORECASE,
    )),
    # 是否曾在其他经纪/担保人投保过 continuous bond
    ("existing_bond", re.compile(
        r"previously\s+applied\s+for|existing\s+(?:continuous\s+)?bond|"
        r"(?:current|prior)\s+(?:bond|surety)|other\s+broker\s+or\s+surety|"
        r"already\s+(?:have|had)\s+(?:a\s+)?bond",
        re.IGNORECASE,
    )),
    # AD/CVD 确认
    ("ad_cvd", re.compile(
        r"anti[- ]?dumping|countervailing|ad/?cvd",
        re.IGNORECASE,
    )),
    # 生效日期
    ("effective_date", re.compile(
        r"(?:requested\s+)?(?:effective|start)\s+date|when\s+(?:would|should)\s+the\s+bond",
        re.IGNORECASE,
    )),
]


def _extract_questions(body_text: str) -> list[str]:
    """从正文提取对方提出的问题类别（去重、保序）。"""
    text = body_text or ""
    keys: list[str] = []
    for key, pattern in _QUESTION_RULES:
        if pattern.search(text) and key not in keys:
            keys.append(key)
    return keys


def _find_quote_amount(text: str) -> tuple[Decimal | None, str]:
    """报价邮件取"首年总支出"：逐行识别保费/服务费并求和。

    代理商报价常拆成多项（服务费 $100 + 保费 $600），只取第一项会把 $100
    当成全部报价，金额与历史均价对比全失真。规则：
    - premium/fee/quote/price/报价/价格 所在行都是报价项，金额全部累加；
    - "X for one year / X for two years" 是期限选项：只计一年档，两年档不计
      （首年现金支出口径，与 policy/money.py 的 total_first_year 一致）；
    - 附件名/主题行（Invoice 1042、bond face value）不算报价项。
    """
    total = Decimal("0")
    currency = "USD"
    hit = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or len(stripped) > 400:
            continue
        lowered = stripped.lower()
        if not any(
            word in lowered
            for word in (
                "premium", "fee", "price", "quote", "cost", "charge",
                "service", "application:", "bond:", "报价", "价格", "费用", "服务费",
            )
        ):
            continue
        # 该行的 Bond 面额（如 "$50,000 bond"）不是收费项：面额通常远大于保费
        matches = [
            match
            for match in AMOUNT_RE.finditer(stripped)
            if match.group("symbol") or match.group("currency")
        ]
        if not matches:
            continue
        # 期限选项行："X for one year, Y for two years" → 只取一年档
        term_matches = [
            match
            for match in matches
            if _term_of_match(stripped, match) == 1
        ]
        picked = term_matches if term_matches else matches
        # "two years / 2 year term" 单独出现时整行不计（非首年支出）
        if not term_matches and re.search(r"\btwo\s+years?\b|\b2\s+years?\b", lowered):
            continue
        amount, line_currency = _sum_amounts(picked)
        if amount is not None:
            total += amount
            if line_currency:
                currency = line_currency
            hit = True
    if hit and total > 0:
        return total, currency
    return _find_amount(text)


def _term_of_match(line: str, match: re.Match[str]) -> int | None:
    """金额后的期限标注（"X for one year"）；非期限选项返回 None。"""
    tail = line[match.end() : match.end() + 24].lower()
    if re.match(r"\s*(?:for\s+)?(?:one|1)\s+years?", tail):
        return 1
    if re.match(r"\s*(?:for\s+)?(?:two|2)\s+years?", tail):
        return 2
    return None


def _sum_amounts(matches: list[re.Match[str]]) -> tuple[Decimal | None, str]:
    """累加一行里的多个金额项；返回 (总额, 首个币种)。"""
    total = Decimal("0")
    currency = "USD"
    for match in matches:
        amount, cur = _parse_amount(match)
        if amount > 0:
            total += amount
            if not currency or currency == "USD":
                currency = cur
    return (total if total > 0 else None), currency


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
    if "bond" in attachment_types and _BOND_DELIVERED_BODY_RE.search(f"{message.subject}\n{message.body_text}"):
        return IntentResult("bond_delivered", 0.9, {"bond_ref": message.subject}, evidence=["附件识别为 Bond 且正文有已签发信号"])
    if attachment_types & {"application", "poa", "articles"}:
        return IntentResult("form_request", 0.85, {"attachments": sorted(attachment_types)}, evidence=["附件包含申请材料"])
    if any(word in message.body_text.lower() for word in ("please complete", "application form", "申请表")):
        return IntentResult("form_request", 0.8, evidence=["正文请求填写申请表"])
    if any(word in f"{message.subject}\n{message.body_text}".lower() for word in ("quote", "premium", "price", "报价", "价格", "fee", "cost", "charge", "service for", "non-refundable")):
        amount, currency = _find_quote_amount(f"{message.subject}\n{message.body_text}")
        return IntentResult(
            "quote_reply",
            0.8 if amount is not None else 0.65,
            {
                "amount": amount,
                "currency": currency,
                # 对方提出的问题：quote_reply 也必须逐题作答（如 Robert 邮件的
                # 股权/进口商品/历史投保三问），草稿层据此确定性应答
                "questions": _extract_questions(message.body_text),
            },
            evidence=["正文命中报价关键词"],
        )
    return IntentResult("unclear", 0.35, evidence=["确定性规则未识别意图"])
