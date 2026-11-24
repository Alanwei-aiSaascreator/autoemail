from pathlib import Path

from autoemail.channels.eml import EmlDirectoryChannel
from autoemail.db import Base
from autoemail.ingest.threading import store_message
from autoemail.models import Invoice, Thread
from autoemail.pipeline import process_message
from autoemail.channels.base import NormalizedMessage

FIXTURES = Path(__file__).parent / "fixtures"


def test_invoice_is_stored_and_payment_notification_attempted(session, world, monkeypatch):
    message = EmlDirectoryChannel(FIXTURES).fetch_since()[1]
    sent = []

    def fake_notify(webhook, content):
        sent.append((webhook, content))
        return True

    monkeypatch.setattr("autoemail.pipeline.notify_wecom", fake_notify)
    result = process_message(session, message, webhook="https://wecom.test/hook")

    assert result.case_id == world["case"].id
    assert result.intent.label == "invoice"
    assert result.payment_notified is True
    assert session.query(Invoice).count() == 1
    invoice = session.query(Invoice).one()
    assert invoice.payment_method == "zelle"
    assert world["case"].state == "invoiced"
    assert len(sent) == 1
    assert "发送至微信群安排支付" in sent[0][1] or "付款通知" in sent[0][1]


def test_unassigned_message_does_not_notify(session, world, monkeypatch):
    message = NormalizedMessage(
        channel="eml",
        channel_msg_id="unassigned-1",
        from_addr="unknown@broker.test",
        subject="Invoice for another importer",
        body_text="Invoice total: $600, please remit payment.",
        dedupe_hash="unassigned-hash",
    )
    called = []
    monkeypatch.setattr("autoemail.pipeline.notify_wecom", lambda *args, **kwargs: called.append(args) or True)
    result = process_message(session, message, webhook="https://wecom.test/hook")
    assert result.case_id is None
    assert result.ignored_reason == "unassigned"
    assert called == []
