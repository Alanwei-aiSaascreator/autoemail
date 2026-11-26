"""命令行入口：建库、灌演示数据、回放 eml。

M0 只做"能跑通"，审核台 API 在 M1 接入。
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from . import audit
from .channels.eml import EmlDirectoryChannel
from .channels.imap import ImapSmtpChannel, inbound_channels
from .config import settings
from .db import get_session, init_db
from .daily_inquiry import maybe_run_daily_batch, run_daily_batch
from .drafts import approve_draft, edit_draft, get_draft, list_drafts, reject_draft
from .evaluation import evaluate_labels, export_labels
from .ingest.assignment import resolve
from .ingest.threading import store_message
from .importing import import_eml, process_batch
from .initiate import start_case
from .invoices import mark_paid
from .mailcheck import check_imap, check_smtp
from .followups import scan_followups
from .models import Agent, AgentPolicy, Case, Commission, Draft, Invoice, Thread
from .pipeline import process_message
from .state import set_auto_mode
from .textutil import normalize_text

DEMO_AGENTS = [
    {
        "name": "A1 Surety",
        "contact_name": "Toni Vrkic",
        "domains": ["a1surety.com"],
        "policy": {
            "invoice_timing": "after_cbp_approval",
            "submit_channel": "acroform_pdf",
            "asks_adcvd": True,
            "extra_fees": [],
            "binding_clauses": "要求 Commercial Invoice + 货物照片，并确认 AD/CVD 适用性。",
        },
    },
    {
        "name": "BorderBuddy",
        "contact_name": "Brittney",
        "domains": ["borderbuddy.com"],
        "policy": {
            "invoice_timing": "after_quote",
            "submit_channel": "acroform_pdf",
            "extra_fees": [],
            "binding_clauses": "POA 需盖章签字并补中间名；要求在途 shipment 细节。",
        },
    },
    {
        "name": "Aldenbridge",
        "contact_name": "Nathaniel J. Vicens",
        "domains": ["aldenbridge.com"],
        "policy": {
            "invoice_timing": "after_quote",
            "submit_channel": "acroform_pdf",
            "extra_fees": [{"label": "CF5106 注册费", "amount": "55"}],
            "requires_broker_for_customs": True,
            "binding_clauses": "必须以其为报关行，否则撤销 Continuous Bond。",
        },
    },
]


def cmd_init_db(_: argparse.Namespace) -> int:
    init_db()
    print("数据库已初始化")
    return 0


def cmd_seed_demo(_: argparse.Namespace) -> int:
    init_db()
    session = get_session()
    try:
        if session.query(Agent).count():
            print("已存在数据，跳过 seed")
            return 0

        agents = {}
        for item in DEMO_AGENTS:
            agent = Agent(
                name=item["name"],
                contact_name=item["contact_name"],
                email_domains=item["domains"],
                email_addresses=[],
            )
            session.add(agent)
            session.flush()
            policy = item["policy"]
            session.add(
                AgentPolicy(
                    agent_id=agent.id,
                    material_checklist=["申请表", "POA", "Articles", "Commercial Invoice"],
                    extra_fees=policy.get("extra_fees", []),
                    binding_clauses=policy.get("binding_clauses"),
                    invoice_timing=policy["invoice_timing"],
                    submit_channel=policy["submit_channel"],
                    asks_adcvd=policy.get("asks_adcvd", False),
                    requires_broker_for_customs=policy.get(
                        "requires_broker_for_customs", False
                    ),
                )
            )
            agents[agent.name] = agent

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
            customs_broker=None,
        )
        session.add(commission)
        session.flush()

        session.add(
            Case(
                case_no="BOND-2026-0142",
                commission_id=commission.id,
                agent_id=agents["A1 Surety"].id,
                state="quoted_out",
                auto_mode="dry_run",
            )
        )
        session.commit()
        print(f"演示数据已写入：3 个代理商 / 1 个委托 / case BOND-2026-0142")
        return 0
    finally:
        session.close()


def cmd_ingest(args: argparse.Namespace) -> int:
    init_db()
    session = get_session()
    channel = EmlDirectoryChannel(args.path)
    try:
        messages = channel.fetch_since()
        if not messages:
            print("没有找到 .eml 文件")
            return 1

        results = []
        for msg in messages:
            result = process_message(session, msg, webhook=args.webhook)
            results.append(
                {
                    "subject": msg.subject,
                    "from": msg.from_addr,
                    "attachments": [a.filename for a in msg.attachments],
                    "case_id": result.case_id,
                    "method": result.assignment.method,
                    "confidence": result.assignment.confidence,
                    "auto_eligible": result.assignment.auto_eligible,
                    "intent": result.intent.label,
                    "intent_confidence": result.intent.confidence,
                    "payment_method": result.intent.payment.payment_method if result.intent.payment else None,
                    "payment_notified": result.payment_notified,
                    "draft_id": result.draft_id,
                    "draft_status": result.draft_status,
                    "ignored_reason": result.ignored_reason,
                }
            )
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        session.close()


def cmd_followup(args: argparse.Namespace) -> int:
    init_db()
    session = get_session()
    try:
        results = scan_followups(session, run=args.followup_command == "run")
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        session.close()


def cmd_initiate(args: argparse.Namespace) -> int:
    init_db()
    session = get_session()
    try:
        result = start_case(
            session,
            commission_id=args.commission_id,
            agent_id=args.agent_id,
            agent_email=args.agent_email,
            auto_mode=args.auto_mode,
            channel=ImapSmtpChannel() if args.auto_mode == "on" else None,
        )
        payload = {
            "case_no": result.case.case_no,
            "case_id": result.case.id,
            "agent_id": result.case.agent_id,
            "state": result.case.state,
            "thread_id": result.thread.id,
            "message_id": result.message.id,
            "decision": result.decision,
            "draft_id": result.draft.id if result.draft else None,
            "draft_status": result.draft.status if result.draft else None,
            "send_result": result.send_result.__dict__ if result.send_result else None,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        session.close()


def _draft_dict(draft: Draft) -> dict:
    return {
        "id": draft.id,
        "case_id": draft.case_id,
        "thread_id": draft.thread_id,
        "source_message_id": draft.source_message_id,
        "intent": draft.intent,
        "status": draft.status,
        "version": draft.version,
        "subject": draft.subject,
        "to_addrs": draft.to_addrs,
        "cc_addrs": draft.cc_addrs,
        "body": draft.body,
        "controlled_fields": draft.controlled_fields,
        "rule_hits": draft.rule_hits,
        "notices": draft.notices,
        "reviewer": draft.reviewer,
        "created_at": draft.created_at,
        "updated_at": draft.updated_at,
        "reviewed_at": draft.reviewed_at,
        "sent_at": draft.sent_at,
        "send_error": draft.send_error,
    }


def cmd_draft_list(args: argparse.Namespace) -> int:
    init_db()
    session = get_session()
    try:
        drafts = list_drafts(
            session,
            status=args.status,
            case_id=args.case_id,
            intent=args.intent,
            limit=args.limit,
            offset=args.offset,
        )
        print(json.dumps([_draft_dict(draft) for draft in drafts], ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        session.close()


def cmd_draft_show(args: argparse.Namespace) -> int:
    init_db()
    session = get_session()
    try:
        print(json.dumps(_draft_dict(get_draft(session, args.draft_id)), ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        session.close()


def _parse_addresses(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [item.strip().lower() for item in value.split(",") if item.strip()]


def cmd_draft_edit(args: argparse.Namespace) -> int:
    init_db()
    session = get_session()
    try:
        draft = edit_draft(
            session,
            args.draft_id,
            reviewer=args.reviewer,
            subject=args.subject,
            to_addrs=_parse_addresses(args.to),
            cc_addrs=_parse_addresses(args.cc),
            body=args.body,
        )
        print(json.dumps(_draft_dict(draft), ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        session.close()


def cmd_draft_reject(args: argparse.Namespace) -> int:
    init_db()
    session = get_session()
    try:
        draft = reject_draft(session, args.draft_id, reviewer=args.reviewer, reason=args.reason)
        print(json.dumps(_draft_dict(draft), ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        session.close()


def cmd_draft_approve(args: argparse.Namespace) -> int:
    init_db()
    session = get_session()
    try:
        result = approve_draft(
            session,
            args.draft_id,
            reviewer=args.reviewer,
            channel=ImapSmtpChannel(),
        )
        payload = {
            "draft": _draft_dict(result.draft),
            "decision": result.decision,
            "reasons": result.reasons,
            "send_result": result.send_result.__dict__ if result.send_result else None,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0 if result.draft.status == "sent" else 2
    finally:
        session.close()


def cmd_llm_stats(_: argparse.Namespace) -> int:
    init_db()
    session = get_session()
    try:
        print(json.dumps(audit.llm_stats(session), ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        session.close()


def cmd_mail_poll(args: argparse.Namespace) -> int:
    if not (settings.imap_host and settings.imap_user and settings.imap_password):
        print(json.dumps({"ok": False, "error": "IMAP 未配置"}, ensure_ascii=False, indent=2))
        return 1
    init_db()
    session = get_session()
    try:
        messages = ImapSmtpChannel().fetch_since()
        summary = process_batch(session, messages, auto_send=args.allow_auto_send)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    finally:
        session.close()


def _watch_logger(log_path: str | None) -> logging.Logger:
    logger = logging.getLogger("autoemail.watch")
    logger.setLevel(logging.INFO)
    if log_path:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def cmd_mail_watch(args: argparse.Namespace) -> int:
    """守护模式：持续轮询收件箱并自动处理回复。

    单次轮询失败（认证失败、网络抖动等）不退出，记日志后继续，等凭据恢复后自动重连。
    用户只需完成「传文件 + 选代理商 + 发起询价」，之后代理商任何回复都会被
    拉取、识别、过闸门并（在 auto_mode=on 且闸门通过时）自动回复。
    """
    log = _watch_logger(getattr(args, "log", None))
    if not (settings.imap_host and settings.imap_user and settings.imap_password):
        log.error("IMAP 未配置，无法启动守护")
        print(json.dumps({"ok": False, "error": "IMAP 未配置"}, ensure_ascii=False, indent=2))
        return 1
    init_db()
    interval = max(args.interval, 5)
    startup = {
        "watching": True,
        "interval_seconds": interval,
        "allow_auto_send": args.allow_auto_send,
    }
    log.info("守护启动 %s", json.dumps(startup, ensure_ascii=False))
    print(json.dumps(startup, ensure_ascii=False, indent=2))
    while True:
        try:
            session = get_session()
            try:
                # 每日随机询价：每天第一轮轮询时触发（当天已跑过则跳过）
                batch_summary = maybe_run_daily_batch(
                    session, channel=ImapSmtpChannel() if args.allow_auto_send else None
                )
                if batch_summary is not None:
                    log.info("每日询价批次 %s", json.dumps(batch_summary, ensure_ascii=False))
                    if batch_summary.get("ran") or batch_summary.get("error"):
                        print(json.dumps(batch_summary, ensure_ascii=False, indent=2))
                # 轮询全局收件箱 + 各委托自有邮箱，收到的回复都走同一处理管线
                processed_total = 0
                for mail_channel in inbound_channels(session):
                    try:
                        messages = mail_channel.fetch_since()
                    except Exception as exc:  # noqa: BLE001 单个邮箱失败不影响其他
                        log.warning("邮箱 %s 轮询失败：%s", mail_channel.user, exc)
                        continue
                    summary = process_batch(session, messages, auto_send=args.allow_auto_send)
                    processed_total += summary.get("processed", 0)
                    if summary.get("processed"):
                        summary["mailbox"] = mail_channel.user
                        payload = json.dumps(summary, ensure_ascii=False)
                        log.info("已处理 %s", payload)
                        print(json.dumps(summary, ensure_ascii=False, indent=2))
                if processed_total:
                    log.info("本轮共处理 %s 封", processed_total)
            finally:
                session.close()
        except KeyboardInterrupt:
            log.info("守护已停止")
            print(json.dumps({"watching": False, "reason": "已停止"}, ensure_ascii=False))
            return 0
        except Exception as exc:  # noqa: BLE001  认证失败/网络抖动，等待恢复后重试
            log.warning("轮询失败（等待重试）：%s", exc)
        time.sleep(interval)


def cmd_inquiry_batch(args: argparse.Namespace) -> int:
    """手动触发每日随机批量询价。"""
    init_db()
    session = get_session()
    try:
        summary = run_daily_batch(
            session,
            channel=ImapSmtpChannel() if args.send else None,
            batch_size=args.size,
            actor="cli",
            force=args.force,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if summary.get("ran") else 1
    finally:
        session.close()


def cmd_mail_check(args: argparse.Namespace) -> int:
    result = {"imap": check_imap(), "smtp": check_smtp()}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["imap"]["ok"] or result["smtp"]["ok"] else 1


def cmd_import(args: argparse.Namespace) -> int:
    init_db()
    session = get_session()
    try:
        summary = import_eml(session, args.path, auto_send=args.allow_auto_send)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    finally:
        session.close()


def cmd_eval_export(args: argparse.Namespace) -> int:
    init_db()
    session = get_session()
    try:
        count = export_labels(session, args.output)
        print(json.dumps({"exported": count, "path": args.output}, ensure_ascii=False, indent=2))
        return 0
    finally:
        session.close()


def cmd_eval_run(args: argparse.Namespace) -> int:
    init_db()
    session = get_session()
    try:
        report = evaluate_labels(session, args.path)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    finally:
        session.close()


def cmd_case_set_mode(args: argparse.Namespace) -> int:
    init_db()
    session = get_session()
    try:
        case = session.get(Case, args.case_id)
        if case is None:
            print(f"Case 不存在：{args.case_id}")
            return 1
        changed = set_auto_mode(session, case, args.mode, actor=args.actor)
        print(
            json.dumps(
                {
                    "case_id": case.id,
                    "case_no": case.case_no,
                    "auto_mode": case.auto_mode,
                    "changed": changed,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    finally:
        session.close()


def _invoice_dict(invoice: Invoice) -> dict:
    return {
        "id": invoice.id,
        "case_id": invoice.case_id,
        "invoice_no": invoice.invoice_no,
        "amount": str(invoice.amount) if invoice.amount is not None else None,
        "currency": invoice.currency,
        "due_date": invoice.due_date,
        "payment_method": invoice.payment_method,
        "status": invoice.status,
        "paid_at": invoice.paid_at,
        "paid_method": invoice.paid_method,
        "paid_reference": invoice.paid_reference,
    }


def cmd_signature_upload(args: argparse.Namespace) -> int:
    """上传客户本人提供的签名图样（必须附授权依据）。"""
    from .signature import save_signature_asset

    path = Path(args.file)
    if not path.exists():
        print(f"文件不存在：{path}")
        return 1
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(path.suffix.lower())
    if mime is None:
        print("仅支持 png/jpg/webp 图片")
        return 1
    init_db()
    session = get_session()
    try:
        commission = (
            session.query(Commission)
            .filter(Commission.legal_name_norm == normalize_text(args.commission))
            .first()
        ) or (
            session.query(Commission)
            .filter(Commission.legal_name_norm.like(f"%{normalize_text(args.commission)}%"))
            .first()
        )
        if commission is None:
            print(f"找不到委托公司：{args.commission}")
            return 1
        try:
            asset = save_signature_asset(
                session,
                commission=commission,
                filename=path.name,
                mime=mime,
                content=path.read_bytes(),
                actor=args.actor,
                authorization_note=args.note,
            )
        except ValueError as exc:
            print(f"上传被拒绝：{exc}")
            return 1
        session.commit()
        print(
            json.dumps(
                {
                    "commission": commission.legal_name,
                    "filename": asset.filename,
                    "sha256": asset.sha256,
                    "authorization_note": asset.authorization_note,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    finally:
        session.close()


def cmd_signature_list(_: argparse.Namespace) -> int:
    """查看各委托已存的客户签名图样。"""
    from .models import SignatureAsset

    init_db()
    session = get_session()
    try:
        rows = []
        for asset in session.query(SignatureAsset).all():
            commission = session.get(Commission, asset.commission_id)
            rows.append(
                {
                    "commission": commission.legal_name if commission else asset.commission_id,
                    "filename": asset.filename,
                    "sha256": asset.sha256,
                    "uploaded_by": asset.uploaded_by,
                    "authorization_note": asset.authorization_note,
                    "created_at": str(asset.created_at),
                }
            )
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    finally:
        session.close()


def cmd_signature_stamp(args: argparse.Namespace) -> int:
    """对已填表格补盖签名：覆盖 filled_forms 里 {legal_name}_{poa|bond|cbp5106}.pdf。"""
    from pathlib import Path as _Path

    import re

    from .formfill import stamp_signature_on_existing

    init_db()
    out_dir = _Path(settings.filled_forms_dir)
    if not out_dir.is_dir():
        print(f"填表产物目录不存在：{out_dir}")
        return 1
    pattern = re.compile(r"^(.*)_(poa|bond|cbp5106)\.pdf$", re.IGNORECASE)
    targets: dict[str, list[str]] = {}
    for f in out_dir.glob("*.pdf"):
        m = pattern.match(f.name)
        if m:
            targets.setdefault(m.group(1), []).append(m.group(2).lower())

    if args.commission:
        names = [n for n in targets if args.commission.lower() in n.lower()]
        if not names:
            print(f"填表产物里找不到委托：{args.commission}")
            return 1
    else:
        names = sorted(targets)

    results = []
    for name in names:
        for short in sorted(set(targets[name])):
            r = stamp_signature_on_existing(name, short)
            r["legal_name"] = name
            results.append(r)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    stamped = sum(1 for r in results if r.get("stamped"))
    print(f"补签完成：{stamped}/{len(results)} 份已盖章")
    return 0


def cmd_agent_add_domain(args: argparse.Namespace) -> int:
    """给代理商补域名：解除「收件域名不属于代理商档案」的拦截。"""
    from .models import Agent, Case

    init_db()
    session = get_session()
    try:
        if args.case:
            case = session.query(Case).filter(Case.case_no == args.case).first()
            if case is None:
                print(f"找不到 case：{args.case}")
                return 1
            agent = session.get(Agent, case.agent_id)
        else:
            agent = session.query(Agent).filter(Agent.name.like(f"%{args.agent}%")).first()
        if agent is None:
            print("找不到代理商")
            return 1
        doms = list(agent.email_domains or [])
        if args.domain in doms:
            print(f"{agent.name}（#{agent.id}）已有域名 {args.domain}")
            return 0
        doms.append(args.domain)
        agent.email_domains = doms
        session.commit()
        print(f"已给 {agent.name}（#{agent.id}）补域名 {args.domain} -> {doms}")
        return 0
    finally:
        session.close()


def cmd_intake_store(args: argparse.Namespace) -> int:
    """上传客户需求采集表 PDF，解析后存结构化字段（委托专属）。"""
    from .formfill import store_intake

    path = Path(args.file)
    if not path.exists():
        print(f"文件不存在：{path}")
        return 1
    if path.suffix.lower() != ".pdf":
        print("仅支持 PDF（客户需求采集表）")
        return 1
    init_db()
    session = get_session()
    try:
        commission = (
            session.query(Commission)
            .filter(Commission.legal_name_norm == normalize_text(args.commission))
            .first()
        ) or (
            session.query(Commission)
            .filter(Commission.legal_name_norm.like(f"%{normalize_text(args.commission)}%"))
            .first()
        )
        if commission is None:
            print(f"找不到委托公司：{args.commission}")
            return 1
        fields = store_intake(
            session, commission.id, path.read_bytes(), filename=path.name
        )
        session.commit()
        print(
            json.dumps(
                {
                    "commission": commission.legal_name,
                    "filename": path.name,
                    "fields_extracted": len(fields),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    finally:
        session.close()


def cmd_intake_list(args: argparse.Namespace) -> int:
    """查看各委托已存的结构化客户需求字段。"""
    from .models import CommissionIntake

    init_db()
    session = get_session()
    try:
        rows = []
        for intake in session.query(CommissionIntake).all():
            commission = session.get(Commission, intake.commission_id)
            rows.append(
                {
                    "commission": commission.legal_name if commission else intake.commission_id,
                    "fields": len(intake.fields or {}),
                    "source_filename": intake.source_filename,
                    "created_at": str(intake.created_at),
                }
            )
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    finally:
        session.close()


def cmd_invoice_list(args: argparse.Namespace) -> int:
    init_db()
    session = get_session()
    try:
        statement = select(Invoice).order_by(Invoice.id.desc())
        if args.status:
            statement = statement.where(Invoice.status == args.status)
        if args.case_id is not None:
            statement = statement.where(Invoice.case_id == args.case_id)
        invoices = session.scalars(statement.limit(args.limit)).all()
        print(
            json.dumps(
                [_invoice_dict(item) for item in invoices],
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0
    finally:
        session.close()


def cmd_invoice_pay(args: argparse.Namespace) -> int:
    init_db()
    session = get_session()
    try:
        invoice = session.get(Invoice, args.invoice_id)
        if invoice is None:
            print(f"Invoice 不存在：{args.invoice_id}")
            return 1
        paid_at = None
        if args.paid_at:
            try:
                paid_at = datetime.fromisoformat(args.paid_at)
            except ValueError:
                print(f"paid_at 需为 ISO 时间，例如 2026-11-18T10:00:00：{args.paid_at}")
                return 1
        changed = mark_paid(
            session,
            invoice,
            method=args.method,
            reference=args.reference,
            actor=args.reviewer,
            paid_at=paid_at,
        )
        case = session.get(Case, invoice.case_id)
        print(
            json.dumps(
                {
                    "invoice": _invoice_dict(invoice),
                    "case_state": case.state if case else None,
                    "changed": changed,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0
    finally:
        session.close()


def cmd_web(args: argparse.Namespace) -> int:
    init_db()
    from .web import serve

    serve(port=args.port, open_browser=not args.no_open, host=args.host)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autoemail", description="Bond 邮件自动化 Agent")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="建表").set_defaults(func=cmd_init_db)
    sub.add_parser("seed-demo", help="写入演示数据").set_defaults(func=cmd_seed_demo)

    initiate = sub.add_parser("initiate", help="主动向 Bond 代理商发起询价")
    initiate.add_argument("--commission-id", type=int, required=True)
    initiate.add_argument("--agent-id", type=int, required=True)
    initiate.add_argument("--agent-email", default=None)
    initiate.add_argument("--auto-mode", choices=["off", "dry_run", "on"], default="dry_run")
    initiate.set_defaults(func=cmd_initiate)

    batch = sub.add_parser("inquiry-batch", help="每日随机批量询价（文件齐全的委托每天随机 N 家）")
    batch.add_argument("--send", action="store_true", help="直接发送询价邮件（默认 dry_run 只出草稿）")
    batch.add_argument("--force", action="store_true", help="忽略今日已执行记录，再跑一次")
    batch.add_argument("--size", type=int, default=None, help="每家委托的代理商数量（默认取配置）")
    batch.set_defaults(func=cmd_inquiry_batch)

    followup = sub.add_parser("followup", help="扫描或执行到期催办任务")
    followup_sub = followup.add_subparsers(dest="followup_command", required=True)
    followup_scan = followup_sub.add_parser("scan", help="扫描到期任务")
    followup_scan.set_defaults(func=cmd_followup)
    followup_run = followup_sub.add_parser("run", help="执行到期任务并生成催办草稿")
    followup_run.set_defaults(func=cmd_followup)

    ingest = sub.add_parser("ingest", help="回放 .eml 文件或目录")
    ingest.add_argument("path", help=".eml 文件或目录")
    ingest.add_argument("--webhook", default=None, help="企业微信群机器人 Webhook；不传则读取配置")
    ingest.set_defaults(func=cmd_ingest)

    mail = sub.add_parser("mail", help="邮箱连接自检")
    mail_sub = mail.add_subparsers(dest="mail_command", required=True)
    mail_check = mail_sub.add_parser("check", help="验证 IMAP / SMTP 登录，不收取也不发送")
    mail_check.set_defaults(func=cmd_mail_check)
    mail_poll = mail_sub.add_parser("poll", help="拉取未读邮件并处理（默认不自动发送）")
    mail_poll.add_argument(
        "--allow-auto-send",
        action="store_true",
        help="允许对 on 模式的 case 自动发送；首次接入建议不要开启",
    )
    mail_poll.set_defaults(func=cmd_mail_poll)

    mail_watch = mail_sub.add_parser("watch", help="守护模式：持续轮询收件箱并自动处理回复")
    mail_watch.add_argument("--interval", type=int, default=60, help="轮询间隔（秒，最小 5）")
    mail_watch.add_argument(
        "--allow-auto-send",
        action="store_true",
        help="允许对 on 模式的 case 自动回复；不开启则只生成审核草稿",
    )
    mail_watch.add_argument("--log", default=None, help="日志文件路径；不传则只输出到终端")
    mail_watch.set_defaults(func=cmd_mail_watch)

    importer = sub.add_parser("import", help="批量导入历史 .eml（默认不自动发送）")
    importer.add_argument("path", help=".eml 文件或目录")
    importer.add_argument(
        "--allow-auto-send",
        action="store_true",
        help="允许对 on 模式的 case 自动发送；历史回放不建议开启",
    )
    importer.set_defaults(func=cmd_import)

    drafts = sub.add_parser("draft", help="审核邮件草稿")
    draft_sub = drafts.add_subparsers(dest="draft_command", required=True)

    draft_list = draft_sub.add_parser("list", help="查询审核队列")
    draft_list.add_argument("--status", default="pending")
    draft_list.add_argument("--case-id", type=int, default=None)
    draft_list.add_argument("--intent", default=None)
    draft_list.add_argument("--limit", type=int, default=50)
    draft_list.add_argument("--offset", type=int, default=0)
    draft_list.set_defaults(func=cmd_draft_list)

    draft_show = draft_sub.add_parser("show", help="查看草稿")
    draft_show.add_argument("draft_id", type=int)
    draft_show.set_defaults(func=cmd_draft_show)

    draft_edit = draft_sub.add_parser("edit", help="编辑草稿")
    draft_edit.add_argument("draft_id", type=int)
    draft_edit.add_argument("--reviewer", required=True)
    draft_edit.add_argument("--subject")
    draft_edit.add_argument("--to", help="逗号分隔的收件人")
    draft_edit.add_argument("--cc", help="逗号分隔的抄送人")
    draft_edit.add_argument("--body")
    draft_edit.set_defaults(func=cmd_draft_edit)

    draft_reject = draft_sub.add_parser("reject", help="拒绝草稿")
    draft_reject.add_argument("draft_id", type=int)
    draft_reject.add_argument("--reviewer", required=True)
    draft_reject.add_argument("--reason", required=True)
    draft_reject.set_defaults(func=cmd_draft_reject)

    draft_approve = draft_sub.add_parser("approve", help="批准并尝试发送草稿")
    draft_approve.add_argument("draft_id", type=int)
    draft_approve.add_argument("--reviewer", required=True)
    draft_approve.set_defaults(func=cmd_draft_approve)

    invoices = sub.add_parser("invoice", help="查看 Invoice 与回填付款")
    invoice_sub = invoices.add_subparsers(dest="invoice_command", required=True)

    invoice_list = invoice_sub.add_parser("list", help="查询 Invoice")
    invoice_list.add_argument("--status", choices=["unpaid", "paid"], default=None)
    invoice_list.add_argument("--case-id", type=int, default=None)
    invoice_list.add_argument("--limit", type=int, default=50)
    invoice_list.set_defaults(func=cmd_invoice_list)

    invoice_pay = invoice_sub.add_parser("pay", help="回填付款方式与流水号，并把 case 推进到 paid")
    invoice_pay.add_argument("invoice_id", type=int)
    invoice_pay.add_argument("--method", choices=["wire", "ach", "card", "zelle"], required=True)
    invoice_pay.add_argument("--reference", required=True, help="付款流水号或凭证号")
    invoice_pay.add_argument("--reviewer", required=True)
    invoice_pay.add_argument("--paid-at", default=None, help="ISO 时间；不传则取当前时间")
    invoice_pay.set_defaults(func=cmd_invoice_pay)

    llm = sub.add_parser("llm", help="LLM 运行统计")
    llm_sub = llm.add_subparsers(dest="llm_command", required=True)
    llm_stats = llm_sub.add_parser("stats", help="影子模式一致率与兜底/草稿统计")
    llm_stats.set_defaults(func=cmd_llm_stats)

    web = sub.add_parser("web", help="启动本地网页审核台")
    web.add_argument("--port", type=int, default=8787)
    web.add_argument("--host", default=settings.web_host, help="监听地址，默认 127.0.0.1")
    web.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    web.set_defaults(func=cmd_web)

    evaluation = sub.add_parser("eval", help="评测集导出与回归")
    eval_sub = evaluation.add_subparsers(dest="eval_command", required=True)
    eval_export = eval_sub.add_parser("export", help="导出 JSONL 标注文件")
    eval_export.add_argument("output", help="输出文件路径，如 labels.jsonl")
    eval_export.set_defaults(func=cmd_eval_export)
    eval_run = eval_sub.add_parser("run", help="按标注文件计算准确率")
    eval_run.add_argument("path", help="标注文件路径")
    eval_run.set_defaults(func=cmd_eval_run)

    case = sub.add_parser("case", help="Case 管理")
    case_sub = case.add_subparsers(dest="case_command", required=True)
    case_mode = case_sub.add_parser("set-mode", help="设置自动模式")
    case_mode.add_argument("case_id", type=int)
    case_mode.add_argument("--mode", choices=["off", "dry_run", "on"], required=True)
    case_mode.add_argument("--actor", default="cli")
    case_mode.set_defaults(func=cmd_case_set_mode)

    signature = sub.add_parser("signature", help="客户签名图样（客户本人提供，系统不代造）")
    signature_sub = signature.add_subparsers(dest="signature_command", required=True)
    signature_up = signature_sub.add_parser("upload", help="上传客户签名图样（必须附授权依据）")
    signature_up.add_argument("--commission", required=True, help="委托公司名称")
    signature_up.add_argument("--file", required=True, help="签名图片路径 png/jpg/webp")
    signature_up.add_argument(
        "--note", required=True, help="授权依据（客户微信/邮件原话、日期、授权范围），必须留痕"
    )
    signature_up.add_argument("--actor", default="cli")
    signature_up.set_defaults(func=cmd_signature_upload)
    signature_list = signature_sub.add_parser("list", help="查看各委托已存的签名图样")
    signature_list.set_defaults(func=cmd_signature_list)
    signature_stamp = signature_sub.add_parser("stamp", help="对已填表格补盖签名（poa/bond/cbp5106）")
    signature_stamp.add_argument("--commission", default=None, help="仅处理指定委托（按名称模糊匹配）")
    signature_stamp.set_defaults(func=cmd_signature_stamp)

    agent_cmd = sub.add_parser("agent", help="代理商档案管理")
    agent_sub = agent_cmd.add_subparsers(dest="agent_command", required=True)
    agent_domain = agent_sub.add_parser("add-domain", help="给代理商补域名（解除收件域名拦截）")
    agent_domain.add_argument("--case", default=None, help="按 case 编号定位代理商")
    agent_domain.add_argument("--agent", default=None, help="按代理商名称模糊匹配")
    agent_domain.add_argument("--domain", required=True, help="要补的域名")
    agent_domain.set_defaults(func=cmd_agent_add_domain)

    intake = sub.add_parser("intake", help="客户需求采集表：解析并存储结构化字段（委托专属）")
    intake_sub = intake.add_subparsers(dest="intake_command", required=True)
    intake_store = intake_sub.add_parser("store", help="上传客户需求采集表 PDF 并解析存储")
    intake_store.add_argument("--commission", required=True, help="委托公司名称")
    intake_store.add_argument("--file", required=True, help="客户需求采集表 PDF 路径")
    intake_store.set_defaults(func=cmd_intake_store)
    intake_list = intake_sub.add_parser("list", help="查看各委托已存的结构化字段")
    intake_list.set_defaults(func=cmd_intake_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
