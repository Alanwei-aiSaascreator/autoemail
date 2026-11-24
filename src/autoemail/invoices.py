"""Invoice 落库与付款回填：收票 → 推群 → 人工标记已付。

同一 case 下按发票号去重：代理商重发同一张发票时不重复建单、不重复推群。
付款由人工执行，系统只负责回填方式、流水号，并把 case 推进到 paid。
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit
from .followups import cancel_followups_for_case
from .intent import PaymentInfo
from .models import INVOICE_PAID, INVOICE_UNPAID, Case, Invoice, Message
from .state import can_transition, transition_case


@dataclass
class InvoiceRecord:
    invoice: Invoice
    created: bool


def _fill_missing(invoice: Invoice, payment: PaymentInfo) -> None:
    """重发发票只补齐空缺字段，不覆盖人工已核对的数据。"""
    if invoice.amount is None and payment.amount is not None:
        invoice.amount = payment.amount
        invoice.currency = payment.currency or invoice.currency
    if invoice.due_date is None and payment.due_date is not None:
        invoice.due_date = payment.due_date
    if invoice.payment_method == "unknown" and payment.payment_method != "unknown":
        invoice.payment_method = payment.payment_method
    if not invoice.remit_detail and payment.remit_detail:
        invoice.remit_detail = dict(payment.remit_detail)


def record_invoice(
    session: Session,
    *,
    case: Case,
    message: Message,
    payment: PaymentInfo,
) -> InvoiceRecord:
    existing = None
    if payment.invoice_no:
        existing = session.scalar(
            select(Invoice).where(
                Invoice.case_id == case.id,
                Invoice.invoice_no == payment.invoice_no,
            )
        )
    if existing is not None:
        _fill_missing(existing, payment)
        audit.record(
            session,
            action="invoice.duplicate",
            case_id=case.id,
            message_id=message.id,
            decision=existing.status,
            reasons=[f"发票号 {existing.invoice_no} 已存在，仅补齐空缺字段"],
        )
        return InvoiceRecord(existing, False)

    invoice = Invoice(
        case_id=case.id,
        message_id=message.id,
        invoice_no=payment.invoice_no,
        amount=payment.amount,
        currency=payment.currency,
        due_date=payment.due_date,
        payment_method=payment.payment_method,
        remit_detail=dict(payment.remit_detail),
        status=INVOICE_UNPAID,
    )
    session.add(invoice)
    session.flush()
    audit.record(
        session,
        action="invoice.recorded",
        case_id=case.id,
        message_id=message.id,
        decision=str(payment.amount) if payment.amount is not None else "unknown",
        reasons=[
            f"发票号 {payment.invoice_no}" if payment.invoice_no else "未识别发票号",
            f"付款方式 {payment.payment_method}",
        ],
    )
    return InvoiceRecord(invoice, True)


def _close_sibling_cases(session: Session, paid_case: Case) -> int:
    """同委托的其余进行中案件直接关闭，不发信（付款后收敛）。"""
    if paid_case is None:
        return 0
    siblings = session.scalars(
        select(Case).where(
            Case.commission_id == paid_case.commission_id,
            Case.id != paid_case.id,
            Case.state.notin_(("closed", "lost", "paid")),
        )
    ).all()
    closed = 0
    for sibling in siblings:
        if not can_transition(sibling.state, "closed"):
            continue
        transition_case(
            session,
            sibling,
            "closed",
            reason=f"同委托案件 {paid_case.case_no} 已付款，本案件不再推进（不发信）",
        )
        cancel_followups_for_case(session, sibling.id, reason="同委托已付款")
        audit.record(
            session,
            action="case.closed_sibling",
            case_id=sibling.id,
            decision="closed",
            reasons=[f"委托案件 {paid_case.case_no} 已付款，自动关闭"],
        )
        closed += 1
    return closed


def mark_paid(
    session: Session,
    invoice: Invoice,
    *,
    method: str,
    reference: str,
    actor: str = "human",
    paid_at: datetime | None = None,
) -> bool:
    """回填付款信息；重复回填返回 False。"""
    if invoice.status == INVOICE_PAID:
        return False
    invoice.status = INVOICE_PAID
    invoice.paid_at = paid_at or datetime.now(timezone.utc)
    invoice.paid_method = method
    invoice.paid_reference = reference

    case = session.get(Case, invoice.case_id)
    if case is not None and can_transition(case.state, "paid"):
        transition_case(
            session,
            case,
            "paid",
            reason=f"Invoice #{invoice.id} 已付款（{method} / {reference}）",
        )
    audit.record(
        session,
        action="invoice.marked_paid",
        actor=actor,
        case_id=invoice.case_id,
        decision=method,
        reasons=[f"invoice_id={invoice.id}", f"reference={reference}"],
    )
    _close_sibling_cases(session, case)
    return True
