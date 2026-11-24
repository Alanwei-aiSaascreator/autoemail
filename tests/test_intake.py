"""客户需求采集表结构化存储生命周期：存 / 读 / 委托结束清理。"""

from autoemail import formfill
from autoemail.models import CommissionIntake
from autoemail.state import transition_case


def test_store_and_get_intake(session, world, monkeypatch):
    monkeypatch.setattr(
        formfill, "extract_client_intake",
        lambda pdf: {"b001": "ACME LLC", "d019": "150000", "d015": "China"},
    )
    fields = formfill.store_intake(session, world["commission"].id, b"fake", filename="intake.pdf")
    assert fields["b001"] == "ACME LLC"
    assert fields["d019"] == "150000"

    got = formfill.get_intake_fields(session, world["commission"].id)
    assert got == {"b001": "ACME LLC", "d019": "150000", "d015": "China"}


def test_store_intake_upserts(session, world, monkeypatch):
    monkeypatch.setattr(formfill, "extract_client_intake", lambda pdf: {"b001": "V1"})
    formfill.store_intake(session, world["commission"].id, b"fake")
    session.commit()
    # 第二次覆盖
    monkeypatch.setattr(formfill, "extract_client_intake", lambda pdf: {"b001": "V2", "d019": "9"})
    formfill.store_intake(session, world["commission"].id, b"fake2")
    session.commit()
    assert session.query(CommissionIntake).count() == 1
    assert formfill.get_intake_fields(session, world["commission"].id) == {"b001": "V2", "d019": "9"}


def test_get_intake_empty_when_none(session, world):
    assert formfill.get_intake_fields(session, world["commission"].id) == {}


def test_purge_commission_data_removes_all(session, world, monkeypatch):
    monkeypatch.setattr(formfill, "extract_client_intake", lambda pdf: {"b001": "ACME"})
    formfill.store_intake(session, world["commission"].id, b"fake")
    session.commit()

    deleted = formfill.purge_commission_data(session, world["commission"].id)
    session.commit()

    assert deleted["intake"] == 1
    assert deleted["documents"] == 3  # world fixture 建了 articles/ein/id 三个文档
    assert formfill.get_intake_fields(session, world["commission"].id) == {}


def test_transition_to_closed_purges_when_no_active_case(session, world, monkeypatch):
    monkeypatch.setattr(formfill, "extract_client_intake", lambda pdf: {"b001": "ACME"})
    formfill.store_intake(session, world["commission"].id, b"fake")
    session.commit()

    transition_case(session, world["case"], "closed", reason="测试关闭")
    session.commit()

    assert formfill.get_intake_fields(session, world["commission"].id) == {}
    assert session.query(CommissionIntake).count() == 0


def test_transition_does_not_purge_when_other_case_active(session, world, monkeypatch):
    from autoemail.models import Agent, Case

    monkeypatch.setattr(formfill, "extract_client_intake", lambda pdf: {"b001": "ACME"})
    formfill.store_intake(session, world["commission"].id, b"fake")
    # 再建一个活跃 case（另一个代理商，绕过 uq_case_active 约束）
    agent2 = Agent(name="Second Surety", email_addresses=["x@second.com"])
    session.add(agent2)
    session.flush()
    session.add(
        Case(
            case_no="BOND-2026-0999",
            commission_id=world["commission"].id,
            agent_id=agent2.id,
            state="init",
        )
    )
    session.commit()

    transition_case(session, world["case"], "closed", reason="测试关闭")
    session.commit()

    # 还有活跃 case，不清理
    assert formfill.get_intake_fields(session, world["commission"].id) == {"b001": "ACME"}
    assert session.query(CommissionIntake).count() == 1
