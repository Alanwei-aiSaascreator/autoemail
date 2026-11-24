import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from autoemail import models  # noqa: F401  注册模型
from autoemail.config import settings
from autoemail.db import Base
from autoemail.models import Agent, AgentPolicy, Case, Commission, CommissionDocument
from autoemail.textutil import normalize_text


@pytest.fixture(autouse=True)
def _isolate_llm_settings(monkeypatch):
    """测试不读本地 .env 的 LLM 开关；需要时由用例自行 monkeypatch 打开。"""
    monkeypatch.setattr(settings, "llm_enabled", False)
    monkeypatch.setattr(settings, "llm_shadow_mode", False)
    monkeypatch.setattr(settings, "llm_intent_fallback_enabled", False)
    monkeypatch.setattr(settings, "llm_draft_enabled", False)
    monkeypatch.setattr(settings, "rag_enabled", False)
    # 隔离 LLM 凭据，避免付款通知 AI 审核等路径在测试里真调用外部模型
    monkeypatch.setattr(settings, "llm_base_url", "")
    monkeypatch.setattr(settings, "llm_api_key", "")


@pytest.fixture(autouse=True)
def _isolate_notify_webhooks(monkeypatch):
    """测试绝不触碰真实企业微信/飞书 webhook，防止向群里发垃圾消息。"""
    monkeypatch.setattr(settings, "wecom_webhook", "")
    monkeypatch.setattr(settings, "feishu_webhook", "")


@pytest.fixture(autouse=True)
def _isolate_mail_servers(monkeypatch):
    """测试绝不连接真实 IMAP/SMTP 服务器，防止向真实收件人发出邮件。

    需要邮件服务器配置的用例（如 test_web）在用例内自行显式设置假值。
    """
    monkeypatch.setattr(settings, "imap_host", "")
    monkeypatch.setattr(settings, "smtp_host", "")


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture()
def world(session):
    agent = Agent(
        name="A1 Surety",
        contact_name="Toni Vrkic",
        email_domains=["a1surety.com"],
        email_addresses=["toni@a1surety.com"],
    )
    session.add(agent)
    session.flush()
    session.add(AgentPolicy(agent_id=agent.id, invoice_timing="after_cbp_approval"))

    commission = Commission(
        legal_name="Sunrise Home Goods LLC",
        legal_name_norm=normalize_text("Sunrise Home Goods LLC"),
        entity_type="LLC",
        ein="12-3456789",
        ior="12-3456789",
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
                mime="application/pdf",
                size=4,
                sha256=f"hash-{doc_type}",
                content=b"test",
            )
        )

    case = Case(
        case_no="BOND-2026-0142",
        commission_id=commission.id,
        agent_id=agent.id,
        state="quoted_out",
    )
    session.add(case)
    session.commit()
    return {"agent": agent, "commission": commission, "case": case}
