from pathlib import Path

from autoemail.channels.base import SendResult
from autoemail.channels.eml import EmlDirectoryChannel
from autoemail.config import settings
from autoemail.models import Case, Commission, Draft, DRAFT_PENDING, DRAFT_REJECTED, DRAFT_SENT
from autoemail.pipeline import process_message
from autoemail.config import settings
from autoemail.web import (
    perform_approve,
    perform_connection_test,
    perform_create_commission,
    perform_edit,
    perform_invoice_paid,
    perform_reject,
    perform_set_mode,
    perform_settings,
    perform_start_inquiry,
    perform_upload_signature,
    render_case,
    render_cases,
    render_commission_signature,
    render_dashboard,
    render_draft,
    render_drafts,
    render_inquiry,
    render_llm,
    render_settings,
    render_stats,
)

FIXTURES = Path(__file__).parent / "fixtures"


class FakeChannel:
    name = "fake"

    def __init__(self):
        self.calls = []

    def send(self, outbound):
        self.calls.append(outbound)
        return SendResult(ok=True, channel_msg_id="web-out-1")


def _seed(session, world):
    return process_message(session, EmlDirectoryChannel(FIXTURES).fetch_since()[0])


def test_create_commission_requires_all_fields(session):
    kind, message = perform_create_commission(
        session, {"legal_name": "New Importer LLC", "reviewer": "ops"}
    )

    assert kind == "error"
    assert "EIN" in message
    assert session.query(Commission).count() == 0


def test_create_commission_then_one_click_inquiry(session, world):
    world["case"].state = "closed"
    session.commit()
    form = {
        "legal_name": "New Importer LLC",
        "entity_type": "LLC",
        "ein": "98-7654321",
        "state_of_inc": "CA",
        "addr_line1": "1 Main St",
        "addr_city": "Los Angeles",
        "addr_state": "CA",
        "addr_zip": "90001",
        "signer_name": "Li Lei",
        "signer_title": "Manager",
        "reviewer": "ops",
    }

    kind, message = perform_create_commission(session, form)
    assert kind == "ok"
    commission = session.query(Commission).filter(
        Commission.legal_name == "New Importer LLC"
    ).one()

    inquiry_form = {
        "commission_id": str(commission.id),
        "agent_id": str(world["agent"].id),
        "agent_email": "toni@a1surety.com",
        "reviewer": "ops",
        "confirm": "yes",
    }
    kind, message, _ = perform_start_inquiry(session, inquiry_form)
    assert kind == "error"
    assert "委托文件不完整" in message
    assert "公司章程" in message

    from autoemail.documents import save_document

    for doc_type in ("articles", "ein", "id"):
        save_document(
            session,
            commission=commission,
            doc_type=doc_type,
            filename=f"{doc_type}.pdf",
            mime="application/pdf",
            content=b"pdf-bytes",
            actor="ops",
        )

    kind, message, _ = perform_start_inquiry(session, inquiry_form)
    assert kind == "ok"
    assert "询价草稿" in message


def test_inquiry_page_shows_complete_commission(session, world):
    page = render_inquiry(session)

    assert "发起询价" in page
    assert 'action="/inquiry"' in page
    assert "可发起询价" in page
    assert "Sunrise Home Goods LLC" in page


def test_one_click_inquiry_requires_complete_documents(session, world):
    world["case"].state = "closed"
    world["commission"].addr_zip = None
    session.commit()
    form = {
        "commission_id": str(world["commission"].id),
        "agent_id": str(world["agent"].id),
        "agent_email": "toni@a1surety.com",
        "reviewer": "ops",
        "confirm": "yes",
    }

    kind, message, case_id = perform_start_inquiry(session, form)

    assert kind == "error"
    assert "邮编" in message
    assert case_id is None
    assert session.query(Case).count() == 1


def test_one_click_inquiry_creates_review_draft(session, world):
    world["case"].state = "closed"
    session.commit()
    form = {
        "commission_id": str(world["commission"].id),
        "agent_id": str(world["agent"].id),
        "agent_email": "toni@a1surety.com",
        "reviewer": "ops",
        "confirm": "yes",
    }

    kind, message, case_id = perform_start_inquiry(session, form)

    assert kind == "ok"
    assert case_id is not None
    assert "询价草稿" in message
    draft = session.query(Draft).filter(Draft.intent == "initial_inquiry").one()
    assert draft.case_id == case_id
    assert draft.status == DRAFT_PENDING
    assert session.get(Case, case_id).state == "init"


def test_dashboard_renders_counts_and_pending_drafts(session, world):
    result = _seed(session, world)

    page = render_dashboard(session)

    assert "Bond 询价台" in page
    assert "待审草稿" in page
    assert "进行中 Case" in page
    assert "/cases/" in page
    assert "发起询价" in page


def test_case_pages_render(session, world):
    _seed(session, world)

    listing = render_cases(session)
    commission_listing = render_cases(session, "active", commission_id=world["commission"].id)
    detail = render_case(session, world["case"].id)

    assert "Sunrise Home Goods LLC" in listing
    assert "BOND-2026-0142" in commission_listing
    assert detail is not None
    assert "Sunrise Home Goods LLC" in detail
    assert "邮件" in detail


def test_draft_pages_render_body_and_fields(session, world):
    result = _seed(session, world)

    listing = render_drafts(session)
    detail = render_draft(session, result.draft_id)

    assert f"#{result.draft_id}" in listing
    assert detail is not None
    assert "受控字段" in detail
    assert "deterministic_template" in detail
    assert "quote for Sunrise Home Goods LLC" in detail


def test_upload_signature_stores_asset(session, world):
    from autoemail.models import SignatureAsset

    kind, message = perform_upload_signature(
        session,
        world["commission"].id,
        {"reviewer": "ops", "authorization_note": "客户微信授权本次申请"},
        {"signature": ("sig.png", b"fakepng-bytes")},
    )
    assert kind == "ok"
    asset = session.query(SignatureAsset).one()
    assert asset.commission_id == world["commission"].id
    assert asset.filename == "sig.png"
    assert asset.authorization_note == "客户微信授权本次申请"


def test_upload_signature_defaults_note(session, world):
    from autoemail.models import SignatureAsset

    kind, message = perform_upload_signature(
        session,
        world["commission"].id,
        {"reviewer": "ops", "authorization_note": ""},
        {"signature": ("sig.png", b"fakepng")},
    )
    assert kind == "ok"
    asset = session.query(SignatureAsset).one()
    assert "客户本人提供" in asset.authorization_note


def test_upload_signature_rejects_non_image(session, world):
    kind, message = perform_upload_signature(
        session,
        world["commission"].id,
        {"reviewer": "ops", "authorization_note": "客户授权"},
        {"signature": ("sig.pdf", b"%PDF-1.4")},
    )
    assert kind == "error"
    assert "图片" in message


def test_signature_page_renders(session, world):
    page = render_commission_signature(session, world["commission"].id)
    assert page is not None
    assert "上传客户手写签名" in page
    assert "Sunrise Home Goods LLC" in page
    assert 'name="signature"' in page


def test_llm_page_renders(session, world):
    _seed(session, world)

    page = render_llm(session)

    assert "LLM 统计" in page
    assert "影子对比分布" in page


def test_missing_draft_and_case_return_none(session):
    assert render_draft(session, 999) is None
    assert render_case(session, 999) is None


def test_draft_list_filters_by_status(session, world):
    result = _seed(session, world)
    link = f'href="/drafts/{result.draft_id}"'

    assert link in render_drafts(session, "pending")
    assert link not in render_drafts(session, "sent")


def test_case_list_filters_by_group(session, world):
    world["case"].state = "closed"
    session.commit()

    assert "Sunrise Home Goods LLC" not in render_cases(session, "active")
    assert "Sunrise Home Goods LLC" in render_cases(session, "closed")


def test_settings_page_hides_secret(monkeypatch):
    monkeypatch.setattr(settings, "imap_host", "imap.qq.com")
    monkeypatch.setattr(settings, "imap_user", "bot@qq.com")
    monkeypatch.setattr(settings, "imap_password", "secret-code")

    page = render_settings()

    assert 'action="/settings"' in page
    assert 'type="password"' in page
    assert "secret-code" not in page
    assert "已配置" in page


def test_perform_settings_writes_env(monkeypatch, tmp_path, session):
    env = tmp_path / ".env"
    env.write_text("# comment\nAUTOEMAIL_IMAP_HOST=old-host\n", encoding="utf-8")
    monkeypatch.setattr(settings, "imap_password", "")

    kind, message = perform_settings(
        session,
        {
            "reviewer": "ops",
            "imap_host": "imap.qq.com",
            "imap_user": "bot@qq.com",
            "imap_password": "code-123",
            "smtp_port": "587",
        },
        env_path=str(env),
    )

    text = env.read_text(encoding="utf-8")
    assert kind == "ok"
    assert "code-123" not in message
    assert "# comment" in text
    assert "AUTOEMAIL_IMAP_HOST=imap.qq.com" in text
    assert "AUTOEMAIL_IMAP_PASSWORD=code-123" in text
    assert settings.imap_host == "imap.qq.com"
    assert settings.imap_password == "code-123"


def test_blank_password_keeps_existing(monkeypatch, tmp_path, session):
    env = tmp_path / ".env"
    env.write_text("AUTOEMAIL_IMAP_PASSWORD=existing\n", encoding="utf-8")
    monkeypatch.setattr(settings, "imap_password", "existing")

    perform_settings(
        session,
        {"reviewer": "ops", "imap_host": "imap.qq.com", "smtp_port": "587"},
        env_path=str(env),
    )

    assert "AUTOEMAIL_IMAP_PASSWORD=existing" in env.read_text(encoding="utf-8")
    assert settings.imap_password == "existing"


def test_invalid_smtp_port_is_rejected(tmp_path, session):
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")

    kind, message = perform_settings(
        session,
        {"reviewer": "ops", "smtp_port": "99999"},
        env_path=str(env),
    )

    assert kind == "error"
    assert "端口" in message


def test_case_page_has_auto_mode_form(session, world):
    page = render_case(session, world["case"].id)

    assert f"/cases/{world['case'].id}/mode" in page
    assert "自动模式" in page
    assert "dry_run" in page


def test_set_mode_requires_reviewer_and_confirmation(session, world):
    kind, _ = perform_set_mode(session, world["case"].id, {"reviewer": "", "mode": "off"})
    assert kind == "error"

    kind, message = perform_set_mode(
        session, world["case"].id, {"reviewer": "ops", "mode": "on"}
    )
    assert kind == "error"
    assert "确认" in message
    assert world["case"].auto_mode == "off"


def test_set_mode_changes_and_audits(session, world):
    from autoemail.models import AuditEvent

    kind, message = perform_set_mode(
        session,
        world["case"].id,
        {"reviewer": "ops", "mode": "on", "confirm": "yes"},
    )

    assert kind == "ok"
    assert message.endswith("on")
    assert world["case"].auto_mode == "on"
    event = session.query(AuditEvent).filter(
        AuditEvent.action == "case.auto_mode_changed"
    ).one()
    assert event.decision == "on"


def test_pending_draft_page_has_review_forms(session, world):
    result = _seed(session, world)

    page = render_draft(session, result.draft_id)

    assert f"/drafts/{result.draft_id}/edit" in page
    assert f"/drafts/{result.draft_id}/approve" in page
    assert f"/drafts/{result.draft_id}/reject" in page
    assert "批准并发送" in page


def test_edit_action_updates_draft(session, world):
    result = _seed(session, world)

    kind, message = perform_edit(
        session,
        result.draft_id,
        {
            "reviewer": "ops@example.com",
            "subject": "Re: [BOND-2026-0142] Updated",
            "to": "toni@a1surety.com",
            "cc": "",
            "body": "Updated body",
        },
    )

    draft = session.get(Draft, result.draft_id)
    assert kind == "ok"
    assert "v2" in message
    assert draft.status == DRAFT_PENDING
    assert draft.body == "Updated body"
    assert draft.subject.startswith("Re: [BOND-2026-0142] Updated")


def test_edit_requires_reviewer_and_recipient(session, world):
    result = _seed(session, world)

    kind, _ = perform_edit(session, result.draft_id, {"reviewer": "", "to": "toni@a1surety.com"})
    assert kind == "error"

    kind, _ = perform_edit(session, result.draft_id, {"reviewer": "ops", "to": ""})
    assert kind == "error"


def test_reject_action(session, world):
    result = _seed(session, world)

    kind, _ = perform_reject(
        session, result.draft_id, {"reviewer": "ops", "reason": "金额需要确认"}
    )

    draft = session.get(Draft, result.draft_id)
    assert kind == "ok"
    assert draft.status == DRAFT_REJECTED


def test_approve_requires_confirmation(session, world):
    result = _seed(session, world)

    kind, message = perform_approve(session, result.draft_id, {"reviewer": "ops"})

    assert kind == "error"
    assert "确认" in message
    assert session.get(Draft, result.draft_id).status == DRAFT_PENDING


def test_approve_without_smtp_is_blocked(session, world, monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "")
    monkeypatch.setattr(settings, "smtp_user", "")
    monkeypatch.setattr(settings, "smtp_password", "")
    result = _seed(session, world)

    kind, message = perform_approve(
        session, result.draft_id, {"reviewer": "ops", "confirm": "yes"}
    )

    assert kind == "error"
    assert "SMTP" in message
    assert session.get(Draft, result.draft_id).status == DRAFT_PENDING


def test_approve_with_channel_sends(session, world):
    result = _seed(session, world)
    channel = FakeChannel()

    kind, message = perform_approve(
        session,
        result.draft_id,
        {"reviewer": "ops", "confirm": "yes"},
        channel=channel,
    )

    draft = session.get(Draft, result.draft_id)
    assert kind == "ok"
    assert draft.status == DRAFT_SENT
    assert len(channel.calls) == 1
    assert f"/drafts/{result.draft_id}/approve" not in render_draft(session, result.draft_id)


def test_stats_renders_state_distribution(session, world):
    _seed(session, world)

    page = render_stats(session)

    assert "检测中台" in page
    assert "Case 状态总览" in page
    assert "今日发出询价" in page
    assert "今日动作" in page
    assert "待办横切" in page
    assert "A1 Surety" in page


def test_stats_zero_data_still_renders(session):
    page = render_stats(session)

    assert "检测中台" in page
    assert "没有进行中的 case" in page
    assert "暂无代理商数据" in page
