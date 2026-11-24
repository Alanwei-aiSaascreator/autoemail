import hashlib
from datetime import date
from decimal import Decimal
from pathlib import Path

from autoemail.channels.base import NormalizedAttachment, NormalizedMessage
from autoemail.ingest.pdf import extract_invoice_pdf_text, extract_pdf_text
from autoemail.intent import classify_message
from autoemail.invoices import mark_paid
from autoemail.models import AuditEvent, Case, INVOICE_PAID, Invoice
from autoemail.pipeline import process_message
from autoemail.web import perform_invoice_paid, render_case

FIXTURES = Path(__file__).parent / "fixtures"
PDF = (FIXTURES / "invoice-1042.pdf").read_bytes()


def _attachment() -> NormalizedAttachment:
    return NormalizedAttachment(
        filename="invoice-1042.pdf",
        mime="application/pdf",
        content=PDF,
        sha256=hashlib.sha256(PDF).hexdigest(),
        doc_type="invoice",
    )


def _message(name: str, body: str = "Please find attached invoice 1042.") -> NormalizedMessage:
    return NormalizedMessage(
        channel="eml",
        channel_msg_id=name,
        from_addr="toni@a1surety.com",
        subject="Re: [BOND-2026-0142] Invoice 1042",
        body_text=body,
        attachments=[_attachment()],
        dedupe_hash=name,
    )


def test_extract_pdf_text_reads_text_layer():
    text = extract_pdf_text(PDF)
    assert "Invoice No: 1042" in text
    assert "1,250.00" in text


def test_broken_or_scanned_pdf_returns_empty():
    assert extract_pdf_text(b"%PDF-1.4\n") == ""
    assert extract_pdf_text(b"") == ""
    assert extract_invoice_pdf_text([]) == ""


def test_pdf_only_invoice_fills_amount_and_due_date():
    result = classify_message(_message("pdf-only", body="Please find attached."))

    assert result.label == "invoice"
    assert result.payment is not None
    assert result.payment.amount == Decimal("1250.00")
    assert result.payment.currency == "USD"
    assert result.payment.due_date == date(2026, 12, 1)
    assert result.payment.payment_method == "wire"
    assert result.payment.invoice_no == "1042"


def test_pipeline_stores_invoice_number_and_dedupes_resend(session, world, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "autoemail.pipeline.notify_wecom",
        lambda webhook, content: sent.append(content) or True,
    )

    first = process_message(session, _message("invoice-first"), webhook="https://wecom.test/hook")
    second = process_message(session, _message("invoice-resend"), webhook="https://wecom.test/hook")

    invoice = session.query(Invoice).one()
    assert first.payment_notified is True
    assert second.payment_notified is False
    assert invoice.invoice_no == "1042"
    assert invoice.amount == Decimal("1250.00")
    assert invoice.status == "unpaid"
    assert len(sent) == 1
    assert "1042" in sent[0]


def test_mark_paid_moves_invoice_and_case(session, world, monkeypatch):
    monkeypatch.setattr("autoemail.pipeline.notify_wecom", lambda webhook, content: True)
    process_message(session, _message("invoice-paid"), webhook="https://wecom.test/hook")
    invoice = session.query(Invoice).one()

    assert mark_paid(session, invoice, method="wire", reference="WT-20261118", actor="ops") is True
    session.refresh(invoice)
    case = session.get(Case, invoice.case_id)

    assert invoice.status == INVOICE_PAID
    assert invoice.paid_method == "wire"
    assert invoice.paid_reference == "WT-20261118"
    assert invoice.paid_at is not None
    assert case.state == "paid"
    event = session.query(AuditEvent).filter(AuditEvent.action == "invoice.marked_paid").one()
    assert event.decision == "wire"

    assert mark_paid(session, invoice, method="wire", reference="WT-20261118", actor="ops") is False


def test_case_page_shows_payment_backfill_form(session, world, monkeypatch):
    monkeypatch.setattr("autoemail.pipeline.notify_wecom", lambda webhook, content: True)
    process_message(session, _message("invoice-web"), webhook="https://wecom.test/hook")

    page = render_case(session, world["case"].id)

    assert f"/cases/{world['case'].id}/invoice-paid" in page
    assert "1042" in page
    assert "回填已付款" in page


def test_perform_invoice_paid_validates_and_updates(session, world, monkeypatch):
    monkeypatch.setattr("autoemail.pipeline.notify_wecom", lambda webhook, content: True)
    process_message(session, _message("invoice-web-pay"), webhook="https://wecom.test/hook")
    invoice = session.query(Invoice).one()

    kind, _ = perform_invoice_paid(
        session, world["case"].id, {"invoice_id": str(invoice.id), "method": "wire", "reference": ""}
    )
    assert kind == "error"

    kind, message = perform_invoice_paid(
        session,
        world["case"].id,
        {
            "invoice_id": str(invoice.id),
            "method": "zelle",
            "reference": "ZL-88",
            "reviewer": "ops",
            "confirm": "yes",
        },
    )

    session.refresh(invoice)
    assert kind == "ok"
    assert "paid" in message
    assert invoice.status == INVOICE_PAID
    assert world["case"].state == "paid"
