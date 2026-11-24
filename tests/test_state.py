from datetime import datetime, timezone

from autoemail.models import Case
from autoemail.state import state_for_intent, transition_case


def test_state_for_intent():
    assert state_for_intent("quote_reply") == "quoted"
    assert state_for_intent("form_request") == "materials_requested"
    assert state_for_intent("bond_delivered") == "bond_received"
    assert state_for_intent("unclear") is None


def test_transition_updates_state_time(session, world):
    case = world["case"]
    old_time = case.state_entered_at
    changed = transition_case(session, case, "quoted", reason="收到报价")
    assert changed is True
    assert case.state == "quoted"
    assert case.state_entered_at != old_time


def test_terminal_case_does_not_transition(session, world):
    case = world["case"]
    case.state = "closed"
    session.commit()
    assert transition_case(session, case, "quoted", reason="不应推进") is False
    assert case.state == "closed"
