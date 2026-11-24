"""归属解析：把来信绑定到 case（V0.2 §4）。

铁律：任何"猜测"都不得触发自动发送。只有 L1–L3 与 L4 允许 auto_eligible。
"""

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..channels.base import NormalizedMessage
from ..models import Agent, Case, Commission, Thread, TERMINAL_STATES
from ..textutil import normalize_text

CASE_TOKEN_RE = re.compile(r"\[(BOND-\d{4}-\d{4})\]", re.IGNORECASE)
EIN_RE = re.compile(r"\b\d{2}-\d{7}\b")

FUZZY_THRESHOLD = 88.0
AMBIGUITY_MARGIN = 0.15

AUTO_ELIGIBLE_METHODS = frozenset(
    {"thread_inherit", "case_token", "ein_exact", "legal_name_exact"}
)


@dataclass
class Assignment:
    case_id: int | None
    method: str
    confidence: float
    auto_eligible: bool
    evidence: list[str] = field(default_factory=list)
    reason: str | None = None


def active_cases(
    session: Session, *, commission_id: int | None = None, agent_id: int | None = None
) -> list[Case]:
    stmt = select(Case).where(Case.state.notin_(TERMINAL_STATES))
    if commission_id is not None:
        stmt = stmt.where(Case.commission_id == commission_id)
    if agent_id is not None:
        stmt = stmt.where(Case.agent_id == agent_id)
    return list(session.scalars(stmt).all())


def resolve(session: Session, thread: Thread, msg: NormalizedMessage) -> Assignment:
    text = f"{msg.subject}\n{msg.body_text}"

    # L1 线程继承
    if thread.case_id:
        return Assignment(
            case_id=thread.case_id,
            method="thread_inherit",
            confidence=1.0,
            auto_eligible=True,
            evidence=["thread 已绑定 case"],
        )

    # L2 主题令牌（我方外发时注入）
    token = CASE_TOKEN_RE.search(text)
    if token:
        case = session.scalar(select(Case).where(Case.case_no == token.group(1).upper()))
        if case:
            return Assignment(
                case_id=case.id,
                method="case_token",
                confidence=1.0,
                auto_eligible=True,
                evidence=[f"命中 {case.case_no}"],
            )

    candidates: dict[int, tuple[str, float, str]] = {}

    # L3 EIN / IOR 精确命中
    for value in sorted(set(EIN_RE.findall(text))):
        commissions = session.scalars(
            select(Commission).where(or_(Commission.ein == value, Commission.ior == value))
        ).all()
        for commission in commissions:
            for case in active_cases(session, commission_id=commission.id):
                candidates[case.id] = ("ein_exact", 1.0, f"EIN/IOR {value}")

    # L4 / L5 公司名
    if not candidates:
        text_norm = normalize_text(text)
        for commission in session.scalars(select(Commission)).all():
            name_norm = commission.legal_name_norm or ""
            if not name_norm:
                continue
            matched = 0.0
            if name_norm in text_norm:
                matched = 0.95
            else:
                ratio = fuzz.partial_ratio(name_norm, text_norm)
                if ratio >= FUZZY_THRESHOLD:
                    matched = 0.85
            if not matched:
                continue
            method = "legal_name_exact" if matched >= 0.95 else "name_fuzzy"
            for case in active_cases(session, commission_id=commission.id):
                current = candidates.get(case.id)
                if not current or matched > current[1]:
                    candidates[case.id] = (method, matched, f"公司名 {commission.legal_name}")

    if candidates:
        ranked = sorted(candidates.items(), key=lambda item: -item[1][1])
        top_case_id, (method, score, evidence) = ranked[0]
        if len(ranked) > 1 and score - ranked[1][1][1] < AMBIGUITY_MARGIN:
            return Assignment(
                case_id=None,
                method="unassigned",
                confidence=0.0,
                auto_eligible=False,
                evidence=[evidence, ranked[1][1][2]],
                reason="多个候选分数接近，需人工指定",
            )
        return Assignment(
            case_id=top_case_id,
            method=method,
            confidence=score,
            auto_eligible=method in AUTO_ELIGIBLE_METHODS,
            evidence=[evidence],
        )

    # L6 该代理名下仅一个活跃 case
    agent = find_agent(session, msg.from_addr)
    if agent:
        cases = active_cases(session, agent_id=agent.id)
        if len(cases) == 1:
            return Assignment(
                case_id=cases[0].id,
                method="agent_single_active",
                confidence=0.80,
                auto_eligible=False,
                evidence=[f"{agent.name} 名下仅 1 个活跃 case"],
            )
        if len(cases) > 1:
            return Assignment(
                case_id=None,
                method="unassigned",
                confidence=0.0,
                auto_eligible=False,
                evidence=[f"{agent.name} 名下有 {len(cases)} 个活跃 case"],
                reason="无法确定是哪一个委托",
            )

    return Assignment(
        case_id=None,
        method="unassigned",
        confidence=0.0,
        auto_eligible=False,
        reason="无任何匹配",
    )


def find_agent(session: Session, from_addr: str) -> Agent | None:
    addr = (from_addr or "").lower()
    domain = addr.split("@")[-1] if "@" in addr else ""
    for agent in session.scalars(select(Agent)).all():
        if addr in {a.lower() for a in (agent.email_addresses or [])}:
            return agent
        if domain and domain in {d.lower() for d in (agent.email_domains or [])}:
            return agent
    return None
