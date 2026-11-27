"""CBP Bond 文件合规自动审查（指导文件 + rules/rule_def.csv 落地）。

硬约束（来自指导文件）：
- 规则唯一来源是包内 rules/rule_def.csv，引擎按 CSV 逐条加载校验后执行；
  禁止把单条规则写成代码里的 if/else 分支散落各处（执行器按 rule_id 注册表驱动）。
- 大模型不参与下"合规/不合规"结论；本模块为纯确定性引擎（字段抽取用正则/锚点）。
- 输入不可用（无文本层、字段缺失）显式落为 unverified，不许静默视为通过。
- 聚合判定：任一 BLOCKER 触发 → REJECT；有 WARNING 或 unverified → REVIEW；
  全部通过 → PASS。

一期覆盖规则（输入可得且可确定性判定）：D001/D008/P005/P006/B001/B002/B004/
F004/F005/I002/I003/S005/S006/X002；其余规则在报告中列回 unverified（输入
依赖外部数据源：OFAC/Circular 570/SOS 等，后续接入外部库后启用）。
"""

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from .models import Case, Commission, ComplianceRun

RULES_PATH = Path(__file__).parent / "rules" / "rule_def.csv"

BLOCKER = "BLOCKER"
WARNING = "WARNING"
INFO = "INFO"
PASS = "PASS"
REVIEW = "REVIEW"
REJECT = "REJECT"

# 数据库证件 doc_type → 规则表文件类型
DB_DOC_TYPE_MAP = {
    "articles": "REG_CERT",
    "ein": "EIN_LETTER",
    "id": "ID",
    "poa": "POA",
    "resolution": "BOARD_RESOLUTION",
    "financial": "FINANCIAL",
}

# 美国本土主体的必备文件集（19 CFR 113.13 基线）
REQUIRED_DOCS_US = ("REG_CERT", "EIN_LETTER", "ID")

DOC_TYPE_KEYWORDS = {
    "REG_CERT": ("certificate of formation", "articles of organization", "articles of incorporation", "certificate of incorporation", "good standing", "secretary of state"),
    "EIN_LETTER": ("employer identification number", "ein", "cp 575", "cp575", "147c"),
    "ID": ("driver license", "driver license", "drivers license", "driver's license", "passport", "identification card"),
    "POA": ("power of attorney", "customs power of attorney"),
    "FORM301": ("cbp form 301", "form 301", "continuation sheet"),
    # 不收裸词 "surety"：报价信常出现担保公司名（如 A1 Surety），会误分类
    "SURETY_APP": ("bond application", "indemnity agreement", "surety bond application", "application for surety bond"),
    "BOARD_RESOLUTION": ("board resolution", "resolution of the", "corporate resolution"),
    "FINANCIAL": ("balance sheet", "income statement", "financial statement"),
    "AD_CVD_STMT": ("antidumping", "anti-dumping", "countervailing"),
    "APOSTILLE": ("apostille", "apostille convention", "hague convention"),
}

EIN_RE = re.compile(r"\b\d{2}-\d{7}\b")
AMOUNT_RE = re.compile(r"\$[\d,]+(?:\.\d{1,2})?")
HS_RE = re.compile(r"\b\d{4}\.\d{2}(?:\.\d{2,4})?\b")
ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
US_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
POBOX_RE = re.compile(r"p\.?\s*o\.?\s*box", re.IGNORECASE)
EXPIRY_CONTEXT_RE = re.compile(r"expir\w*|valid\s+until|valid\s+through|effective\s+through", re.IGNORECASE)
CONTINUOUS_BOND_RE = re.compile(r"continuous\s+(?:customs\s+)?bond", re.IGNORECASE)
SINGLE_BOND_RE = re.compile(r"single\s+(?:transaction|entry)\s+bond", re.IGNORECASE)
# 保额必须取自明确语境（"bond amount"/"penal sum"），防止把报价的年费金额误当保额
BOND_AMOUNT_CONTEXT_RE = re.compile(
    r"bond\s+amount|amount\s+of\s+(?:the\s+)?bond|penal\s+sum|bond\s+in\s+the\s+amount",
    re.IGNORECASE,
)

# 委托人 DB 资料 → 审查上下文字段
DOC_SIZE_LIMIT_BYTES = 20 * 1024 * 1024
ID_EXPIRY_WARN_DAYS = 90
POA_EXPIRY_WARN_DAYS = 30


@dataclass
class RuleDef:
    rule_id: str
    group_id: str
    group_name: str
    level: str
    object_path: str
    message: str
    fix_hint: str
    legal_basis: str
    enabled: bool = True


@dataclass
class DocItem:
    filename: str
    doc_type: str
    text: str
    size: int


@dataclass
class ReviewContext:
    commission: Commission | None
    docs: list[DocItem] = field(default_factory=list)
    today: date = field(default_factory=lambda: datetime.now(timezone.utc).date())


def load_rule_defs(path: Path = RULES_PATH) -> tuple[dict[str, RuleDef], str]:
    """加载并校验规则表：rule_id 唯一、level 合法；失败拒绝启动。返回 (规则表, 版本)。"""
    rules: dict[str, RuleDef] = {}
    version = "0"
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for lineno, row in enumerate(csv.DictReader(fh), start=2):
            rule_id = (row.get("rule_id") or "").strip()
            level = (row.get("level") or "").strip()
            if not rule_id:
                raise ValueError(f"规则表第 {lineno} 行缺 rule_id")
            if rule_id in rules:
                raise ValueError(f"规则表 rule_id 重复：{rule_id}（第 {lineno} 行）")
            if level not in {BLOCKER, WARNING, INFO}:
                raise ValueError(f"规则表 {rule_id} level 非法：{level}（第 {lineno} 行）")
            version = (row.get("version") or version).strip()
            rules[rule_id] = RuleDef(
                rule_id=rule_id,
                group_id=(row.get("group_id") or "").strip(),
                group_name=(row.get("group_name") or "").strip(),
                level=level,
                object_path=(row.get("object_path") or "").strip(),
                message=(row.get("message") or "").strip(),
                fix_hint=(row.get("fix_hint") or "").strip(),
                legal_basis=(row.get("legal_basis") or "").strip(),
                enabled=(row.get("enabled") or "1").strip() == "1",
            )
    if not rules:
        raise ValueError("规则表为空")
    return rules, version


# ---- 抽取器（确定性） ----

def _norm_name(value: str | None) -> str:
    text = re.sub(r"[^A-Za-z0-9 ]+", " ", value or "")
    return re.sub(r"\s+", " ", text).strip().upper()


def classify_document(filename: str, text: str) -> str:
    """文件名 + 内容关键词投票分类；无命中归 OTHER。"""
    haystack = f"{filename}\n{text[:4000]}".lower()
    best_type, best_votes = "OTHER", 0
    for doc_type, keywords in DOC_TYPE_KEYWORDS.items():
        votes = sum(1 for kw in keywords if kw in haystack)
        if votes > best_votes:
            best_type, best_votes = doc_type, votes
    return best_type


def _parse_iso(m: re.Match) -> tuple[date | None, bool]:
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), False
    except ValueError:
        return None, False


def _parse_us(m: re.Match) -> tuple[date | None, bool]:
    month, day = int(m.group(1)), int(m.group(2))
    try:
        return date(int(m.group(3)), month, day), month <= 12 and day <= 12 and month != day
    except ValueError:
        return None, False


def extract_dates(text: str) -> list[tuple[str, date | None, bool]]:
    """提取 (原文, 解析日期, 是否歧义)。歧义 = 美式 M/D/YYYY 且月日均 ≤12 且不等
    （如 03/04/2026 无法排除日/月互换），对应 F004。"""
    results: list[tuple[str, date | None, bool]] = []
    for m in ISO_DATE_RE.finditer(text):
        parsed, ambiguous = _parse_iso(m)
        results.append((m.group(0), parsed, ambiguous))
    for m in US_DATE_RE.finditer(text):
        parsed, ambiguous = _parse_us(m)
        results.append((m.group(0), parsed, ambiguous))
    return results


def extract_eins(text: str) -> set[str]:
    return set(EIN_RE.findall(text or ""))


def extract_amounts(text: str) -> list[float]:
    values = []
    for raw in AMOUNT_RE.findall(text or ""):
        try:
            values.append(float(raw.replace("$", "").replace(",", "")))
        except ValueError:
            continue
    return values


def extract_hs_codes(text: str) -> list[str]:
    return [m.group(0) for m in HS_RE.finditer(text or "")]


def _dates_near(text: str, pattern: re.Pattern) -> list[date]:
    """同一行内命中关键语境（如 expire）的日期。"""
    found: list[date] = []
    for line in (text or "").splitlines():
        if pattern.search(line):
            for _, parsed, _amb in extract_dates(line):
                if parsed is not None:
                    found.append(parsed)
    return found


def _doc_texts(docs: list[DocItem], doc_type: str) -> list[str]:
    return [d.text for d in docs if d.doc_type == doc_type and d.text.strip()]


# ---- 执行器注册表：rule_id → 判定函数 ----
# 返回 (triggered: bool | None, evidence: dict)；None 表示输入不可用（unverified）。

def _rule_d001(ctx: ReviewContext) -> tuple[bool | None, dict]:
    present = {d.doc_type for d in ctx.docs}
    required = set(REQUIRED_DOCS_US)
    missing = sorted(required - present)
    # 完全不含必备类型（如只收到一封普通报价信）时无法判定"缺件"，
    # 落 unverified；部分缺失（收到过 ID 但缺注册证）才构成 D001 触发
    if not ctx.docs or not (present & required):
        return None, {}
    return bool(missing), {"missing": missing, "present": sorted(present)}


def _rule_d008(ctx: ReviewContext) -> tuple[bool | None, dict]:
    oversized = [d.filename for d in ctx.docs if d.size > DOC_SIZE_LIMIT_BYTES]
    return bool(oversized), {"files": oversized, "limit_bytes": DOC_SIZE_LIMIT_BYTES}


def _rule_p005(ctx: ReviewContext) -> tuple[bool | None, dict]:
    name = _norm_name(ctx.commission.legal_name if ctx.commission else None)
    if not name:
        return None, {}
    texts = _doc_texts(ctx.docs, "REG_CERT") + _doc_texts(ctx.docs, "EIN_LETTER")
    if not texts:
        return None, {}
    mismatched = [t for t in texts if name not in _norm_name(t)]
    return bool(mismatched), {"legal_name": ctx.commission.legal_name, "files_checked": len(texts)}


def _rule_p006(ctx: ReviewContext) -> tuple[bool | None, dict]:
    if ctx.commission is None:
        return None, {}
    addr = f"{ctx.commission.addr_line1 or ''}"
    if not addr.strip():
        return None, {}
    return bool(POBOX_RE.search(addr)), {"address": addr}


def _rule_b001(ctx: ReviewContext) -> tuple[bool | None, dict]:
    texts = _doc_texts(ctx.docs, "FORM301") + _doc_texts(ctx.docs, "SURETY_APP")
    if not texts:
        return None, {}
    all_text = "\n".join(texts)
    recognized = bool(CONTINUOUS_BOND_RE.search(all_text) or SINGLE_BOND_RE.search(all_text))
    return not recognized, {"recognized_types": ["CONTINUOUS" if CONTINUOUS_BOND_RE.search(all_text) else "SINGLE"] if recognized else []}


def _rule_b002(ctx: ReviewContext) -> tuple[bool | None, dict]:
    texts = _doc_texts(ctx.docs, "FORM301") + _doc_texts(ctx.docs, "SURETY_APP")
    if not texts:
        return None, {}
    all_text = "\n".join(texts)
    if not CONTINUOUS_BOND_RE.search(all_text):
        return None, {}
    # 只认保额语境行里的金额，避免把报价年费（如 $500/年）误判为保额
    amounts: list[float] = []
    for line in all_text.splitlines():
        if BOND_AMOUNT_CONTEXT_RE.search(line):
            amounts.extend(extract_amounts(line))
    if not amounts:
        return None, {}
    bond_amount = max(amounts)
    return bond_amount < 50000, {"amount": bond_amount, "minimum": 50000}


def _rule_b004(ctx: ReviewContext) -> tuple[bool | None, dict]:
    texts = _doc_texts(ctx.docs, "FORM301") + _doc_texts(ctx.docs, "SURETY_APP")
    if not texts:
        return None, {}
    all_text = "\n".join(texts)
    # 只取到期语境（expiration/expiry/valid until）行的日期，避免把签署日等误判为已过期
    end_dates = _dates_near(all_text, EXPIRY_CONTEXT_RE)
    if not end_dates:
        return None, {}
    today = ctx.today
    expired = [d.isoformat() for d in end_dates if d < today]
    return bool(expired), {"expired_dates": expired, "today": today.isoformat()}


def _rule_f004(ctx: ReviewContext) -> tuple[bool | None, dict]:
    ambiguous = [raw for raw, _d, amb in extract_dates("\n".join(d.text for d in ctx.docs)) if amb]
    if not ctx.docs:
        return None, {}
    return bool(ambiguous), {"dates": ambiguous}


def _rule_f005(ctx: ReviewContext) -> tuple[bool | None, dict]:
    codes = extract_hs_codes("\n".join(d.text for d in ctx.docs))
    if not ctx.docs:
        return None, {}
    bad = []
    for code in codes:
        digits = code.replace(".", "")
        if len(digits) not in {6, 8, 10} or not digits.isdigit():
            bad.append(code)
    return bool(bad), {"invalid": bad, "all": codes}


def _expiry_rule(
    ctx: ReviewContext, doc_types: tuple[str, ...], warn_days: int | None, *, trigger_on_missing: bool
) -> tuple[bool | None, dict]:
    """I002/I003（证件）与 S005/S006（POA）共用：到期日缺失/过期/临期。

    trigger_on_missing=True（硬规则）：抽不到到期日 → 触发"缺失"分支；
    False（临期告警）：抽不到到期日 → 不触发，由对应硬规则负责。
    """
    texts = "\n".join(t for dt in doc_types for t in _doc_texts(ctx.docs, dt))
    db_expiry = None
    if ctx.commission is not None and "ID" in doc_types:
        raw = (ctx.commission.driver_license_expiry or "").strip()
        if raw:
            try:
                db_expiry = date.fromisoformat(raw)
            except ValueError:
                db_expiry = None
    candidates = _dates_near(texts, EXPIRY_CONTEXT_RE)
    if db_expiry is not None:
        candidates.append(db_expiry)
    if db_expiry is None and not texts.strip():
        return None, {}
    if not candidates:
        return trigger_on_missing, {"expiry_found": False, "sources": list(doc_types)}
    today = ctx.today
    expired = [d.isoformat() for d in candidates if d < today]
    if expired and trigger_on_missing:
        return True, {"expired": expired}
    if warn_days is not None:
        near = [d.isoformat() for d in candidates if 0 <= (d - today).days <= warn_days]
        return bool(near), {"near_expiry": near, "warn_days": warn_days}
    return False, {"expiries": sorted({d.isoformat() for d in candidates})}


def _rule_i002(ctx: ReviewContext) -> tuple[bool | None, dict]:
    # 证件过期或到期日缺失均触发（I002：expiry_date 缺失 或 < today）
    return _expiry_rule(ctx, ("ID",), None, trigger_on_missing=True)


def _rule_i003(ctx: ReviewContext) -> tuple[bool | None, dict]:
    # 临期告警：无到期数据时不触发（I002 已负责缺失）
    triggered, ev = _expiry_rule(ctx, ("ID",), ID_EXPIRY_WARN_DAYS, trigger_on_missing=False)
    return triggered or False, ev


def _rule_s005(ctx: ReviewContext) -> tuple[bool | None, dict]:
    return _expiry_rule(ctx, ("POA",), None, trigger_on_missing=True)


def _rule_s006(ctx: ReviewContext) -> tuple[bool | None, dict]:
    triggered, ev = _expiry_rule(ctx, ("POA",), POA_EXPIRY_WARN_DAYS, trigger_on_missing=False)
    return triggered or False, ev


def _rule_x002(ctx: ReviewContext) -> tuple[bool | None, dict]:
    eins: set[str] = set()
    for d in ctx.docs:
        eins |= extract_eins(d.text)
    db_ein = (ctx.commission.ein or "").strip() if ctx.commission else ""
    if db_ein:
        eins.add(db_ein)
    if len(eins) <= 1:
        return None, {}
    return True, {"ein_values": sorted(eins)}


EXECUTORS: dict[str, Callable[[ReviewContext], tuple[bool | None, dict]]] = {
    "D001": _rule_d001,
    "D008": _rule_d008,
    "P005": _rule_p005,
    "P006": _rule_p006,
    "B001": _rule_b001,
    "B002": _rule_b002,
    "B004": _rule_b004,
    "F004": _rule_f004,
    "F005": _rule_f005,
    "I002": _rule_i002,
    "I003": _rule_i003,
    "S005": _rule_s005,
    "S006": _rule_s006,
    "X002": _rule_x002,
}


def build_docs_for_commission(
    session: Session,
    commission_id: int,
    doc_texts: dict[str, str] | None = None,
    doc_sizes: dict[str, int] | None = None,
) -> list[DocItem]:
    """组装委托文件包：数据库证件档案（CommissionDocument）+ 入站附件文本。"""
    from .models import CommissionDocument

    docs: list[DocItem] = []
    for row in session.query(CommissionDocument).filter_by(commission_id=commission_id).all():
        text = ""
        if (row.filename or "").lower().endswith((".pdf", ".txt", ".html")):
            try:
                if (row.filename or "").lower().endswith(".pdf"):
                    from .ingest.pdf import extract_pdf_text

                    text = extract_pdf_text(row.content or b"") or ""
                else:
                    text = (row.content or b"").decode("utf-8", errors="ignore")
            except Exception:  # noqa: BLE001  解析失败按无文本层处理，走 unverified
                text = ""
        docs.append(DocItem(
            filename=row.filename or f"{row.doc_type}.pdf",
            doc_type=DB_DOC_TYPE_MAP.get(row.doc_type, classify_document(row.filename or "", text)),
            text=text,
            size=len(row.content or b""),
        ))
    for filename, text in (doc_texts or {}).items():
        docs.append(DocItem(
            filename=filename,
            doc_type=classify_document(filename, text),
            text=text,
            size=(doc_sizes or {}).get(filename, 0),
        ))
    return docs


def evaluate_documents(
    commission: Commission | None,
    docs: list[DocItem],
) -> dict:
    """纯判定（不落库）：返回 {overall, counts, triggered, unverified, version}。

    review_case（管线落库）与委托库批量体检（临时盘点）共用同一份判定。
    """
    rules, version = load_rule_defs()
    today = datetime.now(timezone.utc).date()
    ctx = ReviewContext(commission=commission, docs=docs, today=today)
    triggered: list[dict] = []
    unverified: list[str] = []
    counts = {BLOCKER: 0, WARNING: 0, INFO: 0}
    for rule_id, rule in rules.items():
        if not rule.enabled:
            continue  # CSV enabled=0 的规则（如 F004 日期歧义）整体跳过，不算 unverified
        executor = EXECUTORS.get(rule_id)
        if executor is None:
            unverified.append(rule_id)
            continue
        try:
            hit, evidence = executor(ctx)
        except Exception as exc:  # noqa: BLE001  单条规则异常 → unverified，不中断整轮
            unverified.append(rule_id)
            evidence = {"error": str(exc)}
            hit = None
        if hit is None:
            unverified.append(rule_id)
            continue
        if hit:
            counts[rule.level] += 1
            triggered.append({
                "rule_id": rule.rule_id,
                "level": rule.level,
                "error_code": None,
                "message": rule.message,
                "fix_hint": rule.fix_hint,
                "legal_basis": rule.legal_basis,
                "evidence": evidence,
            })
        elif rule.level == INFO:
            counts[INFO] += 1
    if counts[BLOCKER]:
        overall = REJECT
    elif counts[WARNING] or unverified:
        overall = REVIEW
    else:
        overall = PASS
    return {
        "overall": overall,
        "counts": counts,
        "triggered": triggered,
        "unverified": sorted(set(unverified)),
        "version": version,
    }


def review_case(
    session: Session,
    case: Case,
    *,
    doc_texts: dict[str, str] | None = None,
    doc_sizes: dict[str, int] | None = None,
) -> ComplianceRun:
    """对 case 当前文件包跑全量规则审查，落 ComplianceRun 并返回。"""
    commission = session.get(Commission, case.commission_id)
    docs = build_docs_for_commission(session, case.commission_id, doc_texts, doc_sizes)
    result = evaluate_documents(commission, docs)
    run = ComplianceRun(
        case_id=case.id,
        rule_set_version=result["version"],
        overall=result["overall"],
        blocker_count=result["counts"][BLOCKER],
        warning_count=result["counts"][WARNING],
        info_count=result["counts"][INFO],
        triggered=result["triggered"],
        unverified=result["unverified"],
    )
    session.add(run)
    session.flush()
    return run


def blockers_of(run: ComplianceRun) -> list[dict]:
    return [item for item in (run.triggered or []) if item.get("level") == BLOCKER]
