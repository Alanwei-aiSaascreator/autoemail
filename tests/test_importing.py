from pathlib import Path

from autoemail.channels.eml import EmlDirectoryChannel
from autoemail.importing import import_eml, process_batch
from autoemail.models import Message

FIXTURES = Path(__file__).parent / "fixtures"


def test_process_batch_handles_unsorted_input(session, world):
    messages = EmlDirectoryChannel(FIXTURES).fetch_since()

    summary = process_batch(session, list(reversed(messages)))
    again = process_batch(session, messages)

    assert summary["processed"] == 2
    assert again["duplicates"] == 2


def test_import_processes_all_messages_without_auto_send(session, world):
    world["case"].auto_mode = "on"
    session.commit()

    summary = import_eml(session, FIXTURES)

    assert summary["total"] == 2
    assert summary["processed"] == 2
    assert summary["duplicates"] == 0
    assert summary["drafts"] == 1
    assert summary["intents"] == {"quote_reply": 1, "invoice": 1}
    assert summary["auto_sent"] == 0
    assert session.query(Message).filter(Message.direction == "out").count() == 0


def test_reimport_is_idempotent(session, world):
    import_eml(session, FIXTURES)

    summary = import_eml(session, FIXTURES)

    assert summary["duplicates"] == 2
    assert summary["processed"] == 0
