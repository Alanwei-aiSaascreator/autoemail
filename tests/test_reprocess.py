"""重新处理入站邮件：分类器修复后，让误处理的入站邮件重跑管线。"""

import pytest
from sqlalchemy import select

from autoemail.intent import IntentResult
from autoemail.models import AuditEvent, Case, Draft, Invoice, Message
from autoemail.pipeline import process_message
from autoemail.reprocess import reprocess_message
from helpers import make_message


def _process_as(session, monkeypatch, label: str, dedupe: str):
    """以指定意图（模拟误判/修复后的分类器）处理一封测试邮件，返回处理结果。"""
    monkeypatch.setattr(
        "autoemail.pipeline.classify_message",
        lambda msg: IntentResult(label, 0.9, evidence=["forced"]),
    )
    message = make_message(
        dedupe=dedupe,
        subject="Please complete the bond application",
        body="Please fill out the attached application form and send it back to us.",
    )
    return process_message(session, message)


def test_reprocess_misclassified_message(session, world, monkeypatch, tmp_path):
    """误判 bond_delivered → 修复后重跑为 form_request：旧草稿作废、新草稿生成、状态回滚再推进。"""
    from autoemail.config import settings

    monkeypatch.setattr(settings, "filled_forms_dir", str(tmp_path))
    result = _process_as(session, monkeypatch, "bond_delivered", "reprocess-1")
    stored_id = result.message_id
    case = session.get(Case, world["case"].id)
    assert case.state == "bond_received"
    old_draft = session.get(Draft, result.draft_id)
    assert old_draft is not None
    assert old_draft.intent == "bond_delivered"

    # 分类器已修复：同一封邮件现在判定为 form_request
    monkeypatch.setattr(
        "autoemail.pipeline.classify_message",
        lambda msg: IntentResult("form_request", 0.9, evidence=["fixed"]),
    )
    summary = reprocess_message(session, stored_id, actor="tester")

    assert summary["old_intent"] == "bond_delivered"
    assert summary["new_intent"] == "form_request"
    assert summary["state_reverted"] is True
    assert summary["rejected_draft_ids"] == [old_draft.id]

    session.refresh(old_draft)
    assert old_draft.status == "rejected"
    assert old_draft.reviewer == "tester"

    # 新草稿按新意图生成；无附件 → 自动填表跳过，不产生填表 notice
    new_draft = session.scalar(
        select(Draft).where(
            Draft.source_message_id == stored_id, Draft.intent == "form_request"
        )
    )
    assert new_draft is not None
    assert new_draft.id != old_draft.id
    assert new_draft.status == summary["draft_status"]
    assert new_draft.status in {"pending", "blocked"}
    assert all("已自动填表" not in item for item in (new_draft.notices or []))

    # 状态先回滚到旧状态（quoted_out），再被新意图推进到 materials_requested
    assert case.state == "materials_requested"

    event = session.scalar(
        select(AuditEvent).where(AuditEvent.action == "message.reprocessed")
    )
    assert event is not None
    assert f"{summary['old_intent']} → {summary['new_intent']}" in event.reasons
    assert "raw_source: db" in event.reasons
    assert any("附件不可用" in reason for reason in event.reasons)


def test_reprocess_rejected_when_reply_already_sent(session, world, monkeypatch):
    """已发出过回复（存在 sent 草稿）的入站邮件默认不能重跑；force 放行补发更正回复。"""
    result = _process_as(session, monkeypatch, "bond_delivered", "reprocess-2")
    draft = session.get(Draft, result.draft_id)
    draft.status = "sent"
    session.commit()

    with pytest.raises(ValueError, match="已发出"):
        reprocess_message(session, result.message_id)

    # force=True：误判纠正场景下允许补发更正回复
    monkeypatch.setattr(
        "autoemail.pipeline.classify_message",
        lambda msg: IntentResult("form_request", 0.9, evidence=["fixed"]),
    )
    summary = reprocess_message(session, result.message_id, actor="tester", force=True)
    assert summary["new_intent"] == "form_request"
    assert any("强制重跑" in note for note in summary["notes"])
    new_draft = session.get(Draft, summary["draft_id"])
    assert new_draft is not None
    assert new_draft.status != "sent"


def test_reprocess_rejects_outbound_message(session, world, monkeypatch):
    """出站消息不允许重新处理。"""
    result = _process_as(session, monkeypatch, "bond_delivered", "reprocess-3")
    stored = session.get(Message, result.message_id)
    session.add(
        Message(
            thread_id=stored.thread_id,
            direction="out",
            channel="email",
            from_addr="ops@example.com",
            to_addrs=["toni@a1surety.com"],
            subject="Re: bond",
            body_text="outbound",
            dedupe_hash="reprocess-3-out",
        )
    )
    session.commit()
    outbound = session.scalar(select(Message).where(Message.dedupe_hash == "reprocess-3-out"))

    with pytest.raises(ValueError, match="只能重新处理入站邮件"):
        reprocess_message(session, outbound.id)


def test_reprocess_rejected_when_invoice_paid(session, world, monkeypatch):
    """旧意图产生且已付款的发票：重跑会破坏账务数据，直接拒绝。"""
    result = _process_as(session, monkeypatch, "bond_delivered", "reprocess-4")
    case = session.get(Case, world["case"].id)
    session.add(
        Invoice(
            case_id=case.id,
            message_id=result.message_id,
            amount=None,
            status="paid",
        )
    )
    session.commit()

    with pytest.raises(ValueError, match="已付款"):
        reprocess_message(session, result.message_id)

    # 拒绝发生在任何变更之前：旧草稿未被作废，状态也未回滚
    draft = session.get(Draft, result.draft_id)
    assert draft.status != "rejected"
    assert case.state == "bond_received"
    assert session.scalar(
        select(AuditEvent).where(AuditEvent.action == "message.reprocessed")
    ) is None


def test_reprocess_does_not_attach_stale_fillings(session, world, monkeypatch, tmp_path):
    """重跑只填原始 .eml 里的申请表，绝不附 case 名下历史批量三件套。

    三件套可能是别的代理商的格式（如 Express 的 Continuous Bond Application），
    拿它敷衍代理商正是之前「发错文件」的根因，必须彻底禁止。
    """
    from autoemail.config import settings

    monkeypatch.setattr(settings, "filled_forms_dir", str(tmp_path))
    (tmp_path / f"{world['case'].case_no}_bond.pdf").write_bytes(b"%PDF-1.4 bond")
    (tmp_path / "Sunrise Home Goods LLC_poa.pdf").write_bytes(b"%PDF-1.4 poa")

    result = _process_as(session, monkeypatch, "bond_delivered", "reprocess-attach")
    monkeypatch.setattr(
        "autoemail.pipeline.classify_message",
        lambda msg: IntentResult("form_request", 0.9, evidence=["fixed"]),
    )
    summary = reprocess_message(session, result.message_id, actor="tester")

    new_draft = session.get(Draft, summary["draft_id"])
    assert new_draft is not None
    assert new_draft.intent == "form_request"
    # 不再把历史三件套当正确产物附上
    assert new_draft.attachments == []
    assert "Please find attached" not in new_draft.body


def test_reprocess_skips_fillings_when_application_not_requested(session, world, monkeypatch, tmp_path):
    """材料清单不含 application 时，正文按清单分段列明待提供材料，绝不附填表产物。"""
    from autoemail.config import settings

    monkeypatch.setattr(settings, "filled_forms_dir", str(tmp_path))
    (tmp_path / f"{world['case'].case_no}_bond.pdf").write_bytes(b"%PDF-1.4 bond")
    (tmp_path / "Sunrise Home Goods LLC_poa.pdf").write_bytes(b"%PDF-1.4 poa")

    result = _process_as(session, monkeypatch, "bond_delivered", "reprocess-nofill")
    monkeypatch.setattr(
        "autoemail.pipeline.classify_message",
        lambda msg: IntentResult(
            "form_request",
            0.9,
            slots={"materials": ["financial_statement", "ownership"]},
            evidence=["fixed"],
        ),
    )
    summary = reprocess_message(session, result.message_id, actor="tester")

    new_draft = session.get(Draft, summary["draft_id"])
    assert new_draft is not None
    assert new_draft.intent == "form_request"
    # 企业财务报表按运营口径自动生成并附上
    assert new_draft.attachments == [
        str(tmp_path / f"{world['case'].case_no}_business_financial_statement.pdf")
    ]
    assert new_draft.controlled_fields["material_keys"] == ["financial_statement", "ownership"]
    # 股权按标准口径正文直答：法人 100% 持股
    assert "100%" in new_draft.body
    assert "Wei Zhang" in new_draft.body
