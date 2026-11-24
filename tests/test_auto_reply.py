from pathlib import Path

from autoemail.channels.eml import EmlDirectoryChannel
from autoemail.channels.base import SendResult
from autoemail.models import Case, Draft, Message
from autoemail.pipeline import process_message

FIXTURES = Path(__file__).parent / "fixtures"


class FakeChannel:
    name = "fake"

    def __init__(self):
        self.calls = []

    def send(self, outbound):
        self.calls.append(outbound)
        return SendResult(ok=True, channel_msg_id="auto-reply-1")


def test_auto_reply_requires_on_mode(session, world, monkeypatch):
    channel = FakeChannel()
    monkeypatch.setattr("autoemail.pipeline.send_draft", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("off mode 不应发送")))
    message = EmlDirectoryChannel(FIXTURES).fetch_since()[0]
    result = process_message(session, message)
    assert result.draft_status == "pending"
    assert channel.calls == []


def test_auto_reply_pipeline_can_be_enabled_without_payment_change(session, world):
    world["case"].auto_mode = "on"
    session.commit()
    message = EmlDirectoryChannel(FIXTURES).fetch_since()[0]
    result = process_message(session, message)
    assert result.intent.label == "quote_reply"
    assert result.draft_id is not None
