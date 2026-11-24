"""报价入库与金额判定：把邮件里的报价变成可审计的 Quote 记录。"""
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import AgentPolicy, Case, Message, Quote
from .policy.money import AUTO_ACCEPT, MANUAL, QuoteEval, evaluate_quote


def _extra_amounts(policy: AgentPolicy | None) -> list[Decimal]:
    amounts: list[Decimal] = []
    for item in (policy.extra_fees or []) if policy else []:
        raw = item.get("amount") if isinstance(item, dict) else item
        if raw is None:
            continue
        try:
            amounts.append(Decimal(str(raw)))
        except (ArithmeticError, ValueError):
            continue
    return amounts


def _history_avg(session: Session, case: Case) -> Decimal | None:
    """同一委托历史报价的首年合计均值，用于异常价提示。"""
    values = session.scalars(
        select(Quote.total_first_year)
        .join(Case, Quote.case_id == Case.id)
        .where(Case.commission_id == case.commission_id, Quote.case_id != case.id)
    ).all()
    if not values:
        return None
    return sum(values) / len(values)


def record_quote(
    session: Session,
    *,
    case: Case,
    message: Message,
    amount: object,
    currency: str = "USD",
) -> Quote:
    """记录一封报价邮件；同一消息只入库一次。"""
    existing = session.scalar(select(Quote).where(Quote.message_id == message.id))
    if existing is not None:
        return existing

    policy = session.scalar(select(AgentPolicy).where(AgentPolicy.agent_id == case.agent_id))
    extras = _extra_amounts(policy)
    result = evaluate_quote(
        amount,
        currency=currency,
        extras=extras,
        history_avg=_history_avg(session, case),
        approval_line=settings.approval_line_usd,
        anomaly_ratio=settings.anomaly_ratio,
    )
    quote = Quote(
        case_id=case.id,
        message_id=message.id,
        amount=Decimal(str(amount)),
        currency=(currency or "USD").upper(),
        extras=[str(item) for item in extras],
        total_first_year=result.total_first_year,
        vs_history_ratio=result.vs_history_ratio,
    )
    session.add(quote)
    session.flush()
    return quote


def quote_eval_for_case(session: Session, case: Case) -> QuoteEval | None:
    """取该 case 最新一条报价，转成 Gate 可用的判定结果。"""
    quote = session.scalar(
        select(Quote)
        .where(Quote.case_id == case.id)
        .order_by(Quote.created_at.desc(), Quote.id.desc())
    )
    if quote is None:
        return None

    notices: list[str] = []
    if quote.currency != "USD":
        notices.append(f"报价币种为 {quote.currency}，需人工折算后判定")
    action = (
        AUTO_ACCEPT
        if quote.currency == "USD" and quote.total_first_year <= settings.approval_line_usd
        else MANUAL
    )
    if (
        quote.vs_history_ratio is not None
        and quote.vs_history_ratio > settings.anomaly_ratio
    ):
        notices.append(
            f"异常价提示：首年合计 {quote.total_first_year} 为历史均价的 "
            f"{quote.vs_history_ratio} 倍（仅提示，不改变动作）"
        )
    if action == MANUAL:
        notices.append(
            f"首年合计 {quote.total_first_year} USD 超过审批线 "
            f"{settings.approval_line_usd}，自动发送需人工审批"
        )
    return QuoteEval(
        total_first_year=quote.total_first_year,
        currency=quote.currency,
        action=action,
        notices=notices,
        vs_history_ratio=quote.vs_history_ratio,
    )
