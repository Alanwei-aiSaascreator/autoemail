"""每日随机询价调度测试。"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from autoemail.daily_inquiry import run_daily_batch
from autoemail.invoices import mark_paid
from autoemail.models import Agent, Case, Commission, CommissionDocument, Invoice
from autoemail.textutil import normalize_text


def _make_ready_commission(session, name: str) -> Commission:
    commission = Commission(
        legal_name=name,
        legal_name_norm=normalize_text(name),
        entity_type="LLC",
        ein="12-3456789",
        state_of_inc="DE",
        addr_line1="1200 N Market St",
        addr_city="Wilmington",
        addr_state="DE",
        addr_zip="19801",
        signer_name="Wei Zhang",
        signer_title="Manager",
    )
    session.add(commission)
    session.flush()
    for doc_type in ("articles", "ein", "id"):
        session.add(
            CommissionDocument(
                commission_id=commission.id,
                doc_type=doc_type,
                filename=f"{doc_type}.pdf",
                size=4,
                sha256=f"hash-{name}-{doc_type}",
                content=b"test",
            )
        )
    session.flush()
    return commission


def _make_agents(session, count: int, prefix: str = "Broker") -> list[Agent]:
    agents = []
    for i in range(count):
        agent = Agent(
            name=f"{prefix} {i:03d}",
            email_addresses=[f"{prefix.lower()}{i:03d}@example.com"],
            email_domains=["example.com"],
        )
        session.add(agent)
        agents.append(agent)
    session.flush()
    return agents


def _pairs(session, commission_id: int) -> set[int]:
    return set(
        session.scalars(select(Case.agent_id).where(Case.commission_id == commission_id))
    )


def test_batch_plans_twenty_and_never_repeats(session):
    _make_agents(session, 25)
    commission = _make_ready_commission(session, "Alpha LLC")

    first = run_daily_batch(session, batch_size=20)
    assert first["ran"] is True
    assert first["planned"] == 20
    assert len(_pairs(session, commission.id)) == 20

    # 同一天重复触发：跳过
    second = run_daily_batch(session, batch_size=20)
    assert second["ran"] is False

    # 强制再跑：不会重复已询过的，只补选剩余未询过的 5 家
    third = run_daily_batch(session, batch_size=20, force=True)
    assert third["ran"] is True
    assert third["planned"] == 5
    assert len(_pairs(session, commission.id)) == 25

    # 全部询过之后，再强制跑也不再新增
    fourth = run_daily_batch(session, batch_size=20, force=True)
    assert fourth["planned"] == 0
    assert len(_pairs(session, commission.id)) == 25


def test_batch_avoids_same_agent_across_commissions(session):
    _make_agents(session, 30)
    alpha = _make_ready_commission(session, "Alpha LLC")
    beta = _make_ready_commission(session, "Beta LLC")

    summary = run_daily_batch(session, batch_size=20)

    alpha_agents = _pairs(session, alpha.id)
    beta_agents = _pairs(session, beta.id)
    assert len(alpha_agents) == 20
    assert len(beta_agents) == 10  # 池子只剩 30-20=10 家，不与 Alpha 重叠
    assert alpha_agents.isdisjoint(beta_agents)
    assert summary["planned"] == 30


def test_batch_skips_commission_already_paid(session):
    _make_agents(session, 5)
    commission = _make_ready_commission(session, "Paid LLC")
    paid_case = Case(
        case_no="BOND-2026-9001",
        commission_id=commission.id,
        agent_id=_make_agents(session, 1, "Existing")[0].id,
        state="paid",
    )
    session.add(paid_case)
    session.commit()

    summary = run_daily_batch(session, batch_size=20, force=True)
    assert summary["commissions"] == []


def test_batch_skips_closed_pair_but_uses_rest(session):
    agents = _make_agents(session, 5)
    commission = _make_ready_commission(session, "Gamma LLC")
    closed = Case(
        case_no="BOND-2026-9002",
        commission_id=commission.id,
        agent_id=agents[0].id,
        state="closed",
    )
    session.add(closed)
    session.commit()

    summary = run_daily_batch(session, batch_size=3, force=True)
    picked = _pairs(session, commission.id)
    # 已关闭的那家不会被重复询价，只新选 3 家
    assert agents[0].id in picked  # 来自已关闭 case，不是新询价
    picked_new = picked - {agents[0].id}
    assert len(picked_new) == 3
    assert agents[0].id not in picked_new


def test_mark_paid_closes_sibling_cases(session):
    commission = _make_ready_commission(session, "Delta LLC")
    agents = _make_agents(session, 2)
    winner = Case(
        case_no="BOND-2026-9010",
        commission_id=commission.id,
        agent_id=agents[0].id,
        state="invoiced",
    )
    loser = Case(
        case_no="BOND-2026-9011",
        commission_id=commission.id,
        agent_id=agents[1].id,
        state="quoted",
    )
    session.add_all([winner, loser])
    session.flush()
    invoice = Invoice(
        case_id=winner.id,
        invoice_no="5001",
        amount="500.00",
        currency="USD",
        payment_method="wire",
        status="unpaid",
    )
    session.add(invoice)
    session.commit()

    assert mark_paid(session, invoice, method="wire", reference="WT-1") is True
    session.refresh(winner)
    session.refresh(loser)
    assert winner.state == "paid"
    assert loser.state == "closed"


def test_batch_with_no_ready_commissions_records_audit(session):
    _make_agents(session, 3)
    summary = run_daily_batch(session, batch_size=20)
    assert summary["ran"] is True
    assert summary["commissions"] == []
    assert session.scalars(select(Case.id)).first() is None
