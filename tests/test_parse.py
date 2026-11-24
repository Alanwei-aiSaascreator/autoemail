from pathlib import Path

from autoemail.channels.eml import EmlDirectoryChannel
from autoemail.ingest.parse import infer_doc_type, parse_email_bytes

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_plain_reply():
    raw = (FIXTURES / "case1_quote_reply.eml").read_bytes()
    msg = parse_email_bytes(raw)

    assert msg.from_addr == "toni@a1surety.com"
    assert "BOND-2026-0142" in msg.subject
    assert "750" in msg.body_text
    assert msg.channel_msg_id == "a1-reply-0001@a1surety.com"
    assert msg.in_reply_to == "outbound-0001@example.com"
    assert len(msg.dedupe_hash) == 64


def test_parse_multipart_attachment():
    raw = (FIXTURES / "case2_invoice.eml").read_bytes()
    msg = parse_email_bytes(raw)

    assert "750.00" in msg.body_text
    assert len(msg.attachments) == 1
    attachment = msg.attachments[0]
    assert attachment.filename == "invoice-1042.pdf"
    assert attachment.doc_type == "invoice"
    assert attachment.content.startswith(b"%PDF")


def test_channel_reads_directory():
    channel = EmlDirectoryChannel(FIXTURES)
    messages = channel.fetch_since()
    assert len(messages) >= 2
    assert {m.channel for m in messages} == {"eml"}


def test_doc_type_inference():
    assert infer_doc_type("POA_Sunrise_signed.pdf") == "poa"
    assert infer_doc_type("drivers-license.jpg") == "driver_license"
    assert infer_doc_type("random.bin") == "unknown"


def test_doc_type_bond_application_is_application_not_bond():
    assert infer_doc_type("ECC - Continuous Bond Application + Guide (EN) 2026.pdf") == "application"
    assert infer_doc_type("Customs Bond.pdf") == "bond"
