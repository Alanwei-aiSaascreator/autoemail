"""Case 清扫测试：比价出局的探测 case 自动落 lost，不再无限堆在"进行中"。"""

from datetime import datetime, timedelta, timezone

from autoemail.casesweep import maybe_run_case_sweep, run_case_sweep
from autoemail.models import Agent, Case, Commission
from autoemail.textutil import normalize_text


def _make_case(session, commission, case_no, state, *, entered_days_ago=0):
    agent = Agent(name=f"Agent-{case_no}")
    session.add(agent)
    session.flush()
    case = Case(
        case_no=case_no,
        commission_id=commission.id,
        agent_id=agent.id,
        state=state,
        state_entered_at=datetime.now(timezone.utc) - timedelta(days=entered_days_ago),
    )
    session.add(case)
    return case


def _make_commission(session, name):
    commission = Commission(
        legal_name=name,
        legal_name_norm=normalize_text(name),
        entity_type="LLC",
    )
    session.add(commission)
    session.flush()
    return commission


def test_sweep_closes_stale_quoted_out_and_init(session):
    commission = _make_commission(session, "Sweep Corp LLC")
    loser = _make_case(session, commission, "BOND-SWEEP-0001", "quoted_out", entered_days_ago=8)
    silent = _make_case(session, commission, "BOND-SWEEP-0002", "init", entered_days_ago=15)
    fresh = _make_case(session, commission, "BOND-SWEEP-0003", "quoted_out", entered_days_ago=1)
    session.commit()

    summary = run_case_sweep(session)

    assert summary["total"] == 2
    session.refresh(loser)
    session.refresh(silent)
    session.refresh(fresh)
    assert loser.state == "lost"
    assert silent.state == "lost"
    assert fresh.state == "quoted_out"  # 未超时不动


def test_sweep_concedes_losers_when_one_case_paid(session):
    commission = _make_commission(session, "Converge Corp LLC")
    winner = _make_case(session, commission, "BOND-SWEEP-0101", "paid")
    loser_a = _make_case(session, commission, "BOND-SWEEP-0102", "quoted_out", entered_days_ago=1)
    loser_b = _make_case(session, commission, "BOND-SWEEP-0103", "init", entered_days_ago=1)
    progressing = _make_case(session, commission, "BOND-SWEEP-0104", "invoiced")
    session.commit()

    summary = run_case_sweep(session)

    session.refresh(winner)
    session.refresh(loser_a)
    session.refresh(loser_b)
    session.refresh(progressing)
    assert winner.state == "paid"
    assert loser_a.state == "lost"
    assert loser_b.state == "lost"
    assert progressing.state == "invoiced"  # 实质推进中的不动
    assert summary["conceded"] == 2


def test_sweep_respects_terminal_and_blocked_states(session):
    commission = _make_commission(session, "Guard Corp LLC")
    frozen = _make_case(session, commission, "BOND-SWEEP-0201", "blocked", entered_days_ago=30)
    closed = _make_case(session, commission, "BOND-SWEEP-0202", "closed")
    stale_quoted = _make_case(session, commission, "BOND-SWEEP-0203", "quoted_out", entered_days_ago=30)
    session.commit()

    summary = run_case_sweep(session)

    session.refresh(frozen)
    session.refresh(closed)
    session.refresh(stale_quoted)
    assert frozen.state == "blocked"  # 人工接管/冻结的 case 清扫不碰
    assert closed.state == "closed"  # 终态不重复处理
    assert stale_quoted.state == "lost"
    assert summary["total"] == 1


def test_maybe_run_sweep_runs_once_per_day(session):
    commission = _make_commission(session, "Throttle Corp LLC")
    _make_case(session, commission, "BOND-SWEEP-0301", "quoted_out", entered_days_ago=10)
    session.commit()

    first = maybe_run_case_sweep(session)
    assert first is not None and first["total"] == 1
    # 当天再跑：已被当日批记录节流，返回 None
    second = maybe_run_case_sweep(session)
    assert second is None
