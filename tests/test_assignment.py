from autoemail.ingest.assignment import resolve
from autoemail.ingest.threading import store_message
from autoemail.models import Case, Commission, Thread
from autoemail.textutil import normalize_text
from helpers import make_message


def _resolve(session, msg):
    stored = store_message(session, msg)
    thread = session.get(Thread, stored.thread_id)
    return resolve(session, thread, msg)


def test_case_token_binds_and_allows_auto(session, world):
    msg = make_message(subject="Re: [BOND-2026-0142] Inquiry", body="Yes we can write it")
    assignment = _resolve(session, msg)
    assert assignment.method == "case_token"
    assert assignment.case_id == world["case"].id
    assert assignment.auto_eligible is True


def test_ein_binds_and_allows_auto(session, world):
    msg = make_message(body="Our file shows EIN 12-3456789 for this importer.")
    assignment = _resolve(session, msg)
    assert assignment.method == "ein_exact"
    assert assignment.case_id == world["case"].id
    assert assignment.auto_eligible is True


def test_conflicting_names_are_unassigned(session, world):
    other_commission = Commission(
        legal_name="Maple Trading LLC",
        legal_name_norm=normalize_text("Maple Trading LLC"),
        entity_type="LLC",
    )
    session.add(other_commission)
    session.flush()
    session.add(
        Case(
            case_no="BOND-2026-0143",
            commission_id=other_commission.id,
            agent_id=world["agent"].id,
            state="quoted_out",
        )
    )
    session.commit()

    msg = make_message(body="We handle both Sunrise Home Goods LLC and Maple Trading LLC.")
    assignment = _resolve(session, msg)
    assert assignment.case_id is None
    assert assignment.method == "unassigned"
    assert assignment.auto_eligible is False


def test_single_active_case_binds_but_no_auto(session, world):
    msg = make_message(body="Please advise on next steps.")
    assignment = _resolve(session, msg)
    assert assignment.method == "agent_single_active"
    assert assignment.case_id == world["case"].id
    assert assignment.auto_eligible is False


def test_thread_inherit(session, world):
    first = make_message(dedupe="h1", message_id="m1@a1", body="Please advise")
    stored = store_message(session, first)
    thread = session.get(Thread, stored.thread_id)
    thread.case_id = world["case"].id
    session.commit()

    reply = make_message(
        dedupe="h2",
        message_id="m2@a1",
        in_reply_to="m1@a1",
        body="following up",
    )
    assignment = _resolve(session, reply)
    assert assignment.method == "thread_inherit"
    assert assignment.auto_eligible is True
