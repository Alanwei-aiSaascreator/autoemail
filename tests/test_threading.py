from autoemail.ingest.threading import store_message
from autoemail.models import Message, Thread
from helpers import make_message


def test_duplicate_message_is_dropped(session):
    msg = make_message()
    assert store_message(session, msg) is not None
    assert store_message(session, msg) is None


def test_reply_reuses_thread(session):
    first = make_message(dedupe="h1", message_id="m1@a1")
    stored = store_message(session, first)

    reply = make_message(dedupe="h2", message_id="m2@a1", in_reply_to="m1@a1")
    reply_stored = store_message(session, reply)

    assert reply_stored.thread_id == stored.thread_id
    assert session.query(Thread).count() == 1
    assert session.query(Message).count() == 2


def test_new_subject_creates_new_thread(session):
    store_message(session, make_message(dedupe="h1", message_id="m1@a1", subject="Bond A"))
    store_message(session, make_message(dedupe="h2", message_id="m2@a1", subject="Bond B"))
    assert session.query(Thread).count() == 2
