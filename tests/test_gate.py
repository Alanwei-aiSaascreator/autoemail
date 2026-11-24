from autoemail.policy.gate import AUTO_SEND, BLOCK, DRAFT, GateContext, evaluate
from autoemail.policy.money import evaluate_quote


def _ctx(**overrides) -> GateContext:
    base = dict(
        case_state="quoted_out",
        auto_mode="on",
        recipients=["toni@a1surety.com"],
        thread_participants=["toni@a1surety.com", "ops@example.com"],
        agent_domains=["a1surety.com"],
        quote=evaluate_quote(750),
        agent_name="A1 Surety",
    )
    base.update(overrides)
    return GateContext(**base)


def test_happy_path_allows_auto_send():
    decision = evaluate(_ctx())
    assert decision.action == AUTO_SEND
    assert decision.rule_ids == []


def test_human_owned_blocks():
    assert evaluate(_ctx(human_owned=True)).action == BLOCK


def test_frozen_thread_blocks():
    assert evaluate(_ctx(thread_frozen=True)).action == BLOCK


def test_auto_mode_off_falls_back_to_draft():
    assert evaluate(_ctx(auto_mode="off")).action == DRAFT


def test_unreliable_assignment_falls_back_to_draft():
    assert evaluate(_ctx(assignment_auto_eligible=False)).action == DRAFT


def test_unknown_recipient_blocks():
    decision = evaluate(_ctx(recipients=["ops@example.com", "attacker@evil.com"]))
    assert decision.action == BLOCK
    assert decision.rule_ids == ["R03"]


def test_foreign_domain_blocks():
    decision = evaluate(
        _ctx(recipients=["toni@a1surety.com"], agent_domains=["a1surety.com", "other.com"])
    )
    assert decision.action == AUTO_SEND  # a1surety.com 合法

    decision = evaluate(_ctx(agent_domains=["borderbuddy.com"]))
    assert decision.action == BLOCK


def test_cross_case_leak_blocks():
    decision = evaluate(_ctx(other_case_markers=["Maple Trading LLC"]))
    assert decision.action == AUTO_SEND

    decision = evaluate(
        _ctx(
            body_text="Please see attached for Maple Trading LLC.",
            other_case_markers=["Maple Trading LLC"],
        )
    )
    assert decision.action == BLOCK
    assert decision.rule_ids == ["R04"]


def test_sensitive_attachment_requires_review():
    decision = evaluate(_ctx(attachment_doc_types=["passport"]))
    assert decision.action == DRAFT
    assert decision.rule_ids == ["R05"]


def test_adcvd_statement_without_attestation_falls_back():
    decision = evaluate(
        _ctx(adcvd_statement_present=True, adcvd_attestation_present=False)
    )
    assert decision.action == DRAFT
    assert decision.rule_ids == ["R06"]

    ok = evaluate(_ctx(adcvd_statement_present=True, adcvd_attestation_present=True))
    assert ok.action == AUTO_SEND


def test_quote_above_approval_line_goes_manual():
    decision = evaluate(_ctx(quote=evaluate_quote(1200)))
    assert decision.action == DRAFT
    assert decision.rule_ids == ["R07"]


def test_anomaly_notice_does_not_block():
    decision = evaluate(_ctx(quote=evaluate_quote(900, history_avg=500)))
    assert decision.action == AUTO_SEND
    assert any("异常价提示" in notice for notice in decision.notices)


def test_broker_conflict_blocks():
    decision = evaluate(
        _ctx(
            requires_broker_for_customs=True,
            commission_customs_broker="BorderBuddy",
            agent_name="Aldenbridge",
        )
    )
    assert decision.action == BLOCK
    assert decision.rule_ids == ["R08"]


def test_unknown_broker_falls_back_to_draft():
    decision = evaluate(
        _ctx(requires_broker_for_customs=True, commission_customs_broker=None)
    )
    assert decision.action == DRAFT
    assert decision.rule_ids == ["R08"]

    aligned = evaluate(
        _ctx(
            requires_broker_for_customs=True,
            commission_customs_broker="A1 Surety",
            agent_name="A1 Surety",
        )
    )
    assert aligned.action == AUTO_SEND


def test_state_machine_limit_falls_back():
    assert evaluate(_ctx(state_allows_auto=False)).action == DRAFT


def test_frequency_limits_fall_back():
    assert evaluate(_ctx(auto_sends_24h_in_thread=10)).action == DRAFT
    assert evaluate(_ctx(auto_sends_1h_to_agent=3)).action == DRAFT
    assert evaluate(_ctx(consecutive_failures=2)).action == DRAFT


def test_ledger_not_claimed_blocks():
    decision = evaluate(_ctx(ledger_claimed=False))
    assert decision.action == BLOCK
    assert decision.rule_ids == ["R12"]
