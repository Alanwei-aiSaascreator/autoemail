"""一期入站处理管线。

当前重点是付款信息：入站邮件归属成功后，识别 Invoice/付款信息，写入 Invoice，
并推送企业微信群。付款执行仍由人工完成。
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit
from .bounces import detect_bounce, record_bounce
from .channels.base import MessageChannel, NormalizedMessage
from .channels.imap import ImapSmtpChannel
from .config import settings
from .ingest.assignment import Assignment, resolve
from .ingest.threading import store_message
from .intent import IntentResult, classify_message
from .intent_llm import classify_with_llm
from .llm import HttpxOpenAICompatibleProvider
from .models import Agent, Case, Commission, Draft, Message, Thread
from .drafts import generate_draft
from .invoices import record_invoice
from .notify.feishu import (
    build_bounce_text,
    build_payment_text,
    build_quote_text,
    notify as notify_feishu,
)
from .notify.wecom import (
    build_bounce_markdown,
    build_payment_markdown,
    build_quote_markdown,
    notify as notify_wecom,
)
from .state import can_transition, state_for_intent, transition_case
from .followups import cancel_followups_for_case
from .outbound import send_draft
from .quotes import record_quote


@dataclass
class ProcessResult:
    message_id: int | None
    thread_id: int | None
    case_id: int | None
    assignment: Assignment
    intent: IntentResult
    payment_notified: bool = False
    ignored_reason: str | None = None
    draft_id: int | None = None
    draft_status: str | None = None


def _case_context(session: Session, case: Case) -> tuple[Commission, Agent]:
    commission = session.get(Commission, case.commission_id)
    agent = session.get(Agent, case.agent_id)
    if commission is None or agent is None:
        raise RuntimeError(f"case {case.case_no} 关联档案不存在")
    return commission, agent


def _review_payment_notice(text: str) -> tuple[bool, str]:
    """用 LLM 审核付款通知内容，拦截明显错误。返回 (是否放行, 原因)。

    拦截情形：付款方式「未识别/unknown」、收款人/汇款要素残缺或明显异常、金额缺失或可疑。
    LLM 不可用时降级为放行，不阻塞付款通知主链路。
    """
    from pydantic import BaseModel

    from .llm import HttpxOpenAICompatibleProvider

    class Review(BaseModel):
        approve: bool
        reason: str

    provider = HttpxOpenAICompatibleProvider()
    result = provider.complete_json(
        system_prompt="你是付款通知审核助手，判断机器人推送到工作群的付款通知是否应放行。",
        user_prompt=(
            "判断下面这条付款通知是否应推送。存在以下任一问题即拦截(approve=false)："
            "1) 付款方式为「未识别」；2) 收款人/汇款要素残缺或明显异常（如不完整人名、可疑链接）；"
            "3) 金额缺失或明显不合理。否则放行(approve=true)。\n\n"
            f"通知内容：\n{text}"
        ),
        schema=Review,
        task="payment_notice_review",
    )
    if result.error is not None or result.parsed is None:
        return True, ""
    return result.parsed.approve, result.parsed.reason


def process_message(
    session: Session,
    message: NormalizedMessage,
    *,
    webhook: str | None = None,
    attachment_url: str | None = None,
    llm_provider: object | None = None,
    retriever: object | None = None,
    auto_send: bool = True,
) -> ProcessResult:
    stored = store_message(session, message)
    if stored is None:
        stored = session.scalar(select(Message).where(Message.dedupe_hash == message.dedupe_hash))
        if stored is None:
            return ProcessResult(
                message_id=None,
                thread_id=None,
                case_id=None,
                assignment=Assignment(None, "duplicate", 0.0, False),
                intent=IntentResult("unclear", 0.0),
                ignored_reason="duplicate",
            )
        thread = session.get(Thread, stored.thread_id)
        case = session.get(Case, thread.case_id) if thread and thread.case_id else None
        if thread and case:
            existing_intent = classify_message(message)
            existing_draft = session.scalar(
                select(Draft).where(
                    Draft.source_message_id == stored.id,
                    Draft.intent == existing_intent.label,
                )
            )
            return ProcessResult(
                stored.id, thread.id, case.id,
                Assignment(case.id, "duplicate", 1.0, False),
                existing_intent,
                draft_id=existing_draft.id if existing_draft else None,
                draft_status=existing_draft.status if existing_draft else None,
                ignored_reason="duplicate",
            )
        return ProcessResult(stored.id, stored.thread_id, None, Assignment(None, "duplicate", 0.0, False), IntentResult("unclear", 0.0), ignored_reason="duplicate")

    return _process_stored(
        session,
        stored,
        message,
        webhook=webhook,
        attachment_url=attachment_url,
        llm_provider=llm_provider,
        retriever=retriever,
        auto_send=auto_send,
        notify=True,
    )


def _process_stored(
    session: Session,
    stored: Message,
    message: NormalizedMessage,
    *,
    webhook: str | None = None,
    attachment_url: str | None = None,
    llm_provider: object | None = None,
    retriever: object | None = None,
    auto_send: bool = True,
    notify: bool = True,
) -> ProcessResult:
    """store_message 去重之后的处理逻辑：归属 → 意图 → 状态迁移 → 草稿/付款。

    process_message 与重新处理（reprocess.reprocess_message）共用同一份实现；
    notify=False 时不推送群通知（避免重复打扰），auto_send=False 时强制不自动发送。
    """
    thread = session.get(Thread, stored.thread_id)
    if thread is None:
        raise RuntimeError(f"message {stored.id} 的 thread 不存在")

    assignment = resolve(session, thread, message)
    audit.record(
        session,
        action="assignment.resolve",
        thread_id=thread.id,
        message_id=stored.id,
        case_id=assignment.case_id,
        decision=assignment.method,
        reasons=assignment.evidence + ([assignment.reason] if assignment.reason else []),
    )

    # DSN 退信（对方邮箱拒收）优先于一切归属/意图处理：拉黑 + 冻结 + 停跟催 + 推群
    bounce = detect_bounce(message)
    if bounce is not None:
        bounce_case = session.get(Case, assignment.case_id) if assignment.case_id else None
        record_bounce(session, thread, bounce, message_id=stored.id, case=bounce_case)
        session.commit()
        commission_name, agent_name, case_no = "未知", "未知", "未知"
        if bounce_case is not None:
            commission, agent = _case_context(session, bounce_case)
            commission_name, agent_name, case_no = commission.legal_name, agent.name, bounce_case.case_no
        notified = notify_wecom(
            settings.wecom_webhook,
            build_bounce_markdown(
                commission_name=commission_name,
                agent_name=agent_name,
                case_no=case_no,
                blocked_emails=bounce.recipients,
                dsn_code=bounce.dsn_code,
            ),
        ) or notify_feishu(
            settings.feishu_webhook,
            build_bounce_text(
                commission_name=commission_name,
                agent_name=agent_name,
                case_no=case_no,
                blocked_emails=bounce.recipients,
                dsn_code=bounce.dsn_code,
            ),
        )
        audit.record(
            session,
            action="bounce.handled",
            thread_id=thread.id,
            message_id=stored.id,
            case_id=assignment.case_id,
            decision="blocked" if notified else "notified_failed",
            reasons=[
                f"recipients={bounce.recipients}",
                f"dsn={bounce.dsn_code or 'n/a'}",
                "已全局拉黑并冻结线程，停止一切自动发件",
            ],
        )
        session.commit()
        return ProcessResult(
            stored.id,
            thread.id,
            assignment.case_id,
            assignment,
            IntentResult("unclear", 0.0),
            ignored_reason="bounce",
        )

    if assignment.case_id is None:
        return ProcessResult(stored.id, thread.id, None, assignment, IntentResult("unclear", 0.0), ignored_reason="unassigned")

    thread.case_id = assignment.case_id
    session.commit()
    deterministic_intent = classify_message(message)
    explicit_provider = llm_provider is not None
    active_provider = llm_provider
    if active_provider is None and settings.llm_enabled:
        active_provider = HttpxOpenAICompatibleProvider()
    llm_allowed = explicit_provider or settings.llm_enabled or settings.llm_intent_fallback_enabled
    provider_ready = active_provider is not None and getattr(active_provider, "configured", True)
    intent = deterministic_intent
    if (
        active_provider is not None
        and llm_allowed
        and provider_ready
        and deterministic_intent.payment is None
    ):
        # LLM 读全文（含附件清单）优先判定；确定性关键词分类只做兜底。
        # 付款类（invoice）由确定性解析直接处理，不经 LLM。
        llm_intent = classify_with_llm(message, provider=active_provider)
        if llm_intent is not None:
            before = deterministic_intent.label
            intent = llm_intent
            audit.record(
                session,
                action="llm.intent.classify",
                thread_id=thread.id,
                message_id=stored.id,
                case_id=assignment.case_id,
                decision=intent.label,
                reasons=[
                    f"deterministic={before}",
                    f"llm={intent.label}",
                    f"agree={before == intent.label}",
                ],
            )
    case = session.get(Case, assignment.case_id)
    if case is None:
        raise RuntimeError(f"case id {assignment.case_id} 不存在")

    # 回复全量规划：LLM 读全文+附件文本，在框架枚举内决策意图/材料/正文回答/
    # 附件处置/状态迁移；低置信或不可用时为 None，走确定性链路。
    reply_plan = None
    if active_provider is not None and llm_allowed and provider_ready and deterministic_intent.payment is None:
        from .ingest.pdf import extract_pdf_text
        from .planner import plan_reply

        pdf_texts: dict[str, str] = {}
        for att in message.attachments or []:
            if not (att.filename or "").lower().endswith(".pdf"):
                continue
            text = extract_pdf_text(att.content) or ""
            # 扫描件/照片件（无文本层）→ 视觉模型 OCR 提取后再规划
            if len(text.strip()) < 40:
                from .planner import extract_scanned_pdf_text

                ocr = extract_scanned_pdf_text(att.content, provider=active_provider)
                if len(ocr.strip()) > len(text.strip()):
                    text = ocr
            pdf_texts[att.filename] = text
        reply_plan = plan_reply(
            message,
            provider=active_provider,
            case_no=case.case_no,
            case_state=case.state,
            pdf_texts=pdf_texts,
        )
        if reply_plan is not None:
            from .planner import plan_to_slots

            intent.slots.update(plan_to_slots(reply_plan))
            if reply_plan.intent != "unclear":
                intent.label = reply_plan.intent
                intent.confidence = max(intent.confidence, reply_plan.confidence)
                intent.evidence = ["planner", *intent.evidence]
            elif reply_plan.inline_answers and deterministic_intent.label == "unclear":
                # 框架规则：对方提出了我们能确定性回答的问题 → 必须回信，
                # 不允许规划器以 unclear 逃逸（否则信不回、问题悬空）。
                intent.label = "form_request"
                intent.confidence = max(intent.confidence, reply_plan.confidence)
                intent.evidence = ["planner+inline_answers", *intent.evidence]
            audit.record(
                session,
                action="llm.reply_plan",
                thread_id=thread.id,
                message_id=stored.id,
                case_id=case.id,
                decision=reply_plan.intent,
                reasons=[
                    f"state_hint={reply_plan.state_hint}",
                    f"materials={reply_plan.materials}",
                    f"answers={len(reply_plan.inline_answers)}",
                    f"attachments={[(a.filename, a.disposition) for a in reply_plan.attachments]}",
                ],
            )

    audit.record(
        session,
        action="intent.classify",
        thread_id=thread.id,
        message_id=stored.id,
        case_id=case.id,
        decision=intent.label,
        reasons=intent.evidence,
    )
    case.last_inbound_at = message.sent_at or datetime.now(timezone.utc)
    target_state = state_for_intent(intent.label)
    # LLM 规划的状态建议：仅当命中状态机白名单时采纳，否则保持确定性映射
    plan_hint = str(intent.slots.get("state_hint") or "none")
    if plan_hint != "none" and plan_hint != target_state and can_transition(case.state, plan_hint):
        target_state = plan_hint
    if target_state:
        transition_case(session, case, target_state, reason=f"收到代理商 {intent.label} 回复", message_id=stored.id)
        cancel_followups_for_case(session, case.id, reason="收到代理商新回复")
    session.commit()

    if intent.label == "quote_reply" and intent.slots.get("amount") is not None:
        record_quote(
            session,
            case=case,
            message=stored,
            amount=intent.slots["amount"],
            currency=str(intent.slots.get("currency") or "USD"),
        )
        session.commit()
        # 报价即推群：人工横向对比各代理商价格（不议价，低者得，付款人工执行）
        commission, agent = _case_context(session, case)
        if notify:
            notify_wecom(
                settings.wecom_webhook,
                build_quote_markdown(
                    commission_name=commission.legal_name,
                    agent_name=agent.name,
                    case_no=case.case_no,
                    amount=intent.slots["amount"],
                    currency=str(intent.slots.get("currency") or "USD"),
                ),
            )
            notify_feishu(
                settings.feishu_webhook,
                build_quote_text(
                    commission_name=commission.legal_name,
                    agent_name=agent.name,
                    case_no=case.case_no,
                    amount=intent.slots["amount"],
                    currency=str(intent.slots.get("currency") or "USD"),
                ),
            )

    if intent.payment is None:
        draft_result = generate_draft(
            session,
            case=case,
            thread=thread,
            source_message=stored,
            source=message,
            intent=intent,
            assignment=assignment,
            llm_provider=active_provider if settings.llm_draft_enabled else None,
            retriever=retriever,
        )
        auto_result = None
        llm_body = draft_result.draft is not None and draft_result.draft.model_id is not None
        if (
            draft_result.draft
            and auto_send
            and case.auto_mode == "on"
            and intent.label in {"quote_reply", "form_request", "bond_delivered"}
            and not llm_body
        ):
            auto_result = send_draft(session, draft_result.draft, channel=ImapSmtpChannel(), actor="agent", manual_approval=False, assignment_auto_eligible=assignment.auto_eligible)
        final_draft = auto_result.draft if auto_result else draft_result.draft
        return ProcessResult(
            stored.id,
            thread.id,
            case.id,
            assignment,
            intent,
            ignored_reason=draft_result.ignored_reason,
            draft_id=final_draft.id if final_draft else None,
            draft_status=final_draft.status if final_draft else None,
        )

    payment = intent.payment
    record = record_invoice(session, case=case, message=stored, payment=payment)
    invoice = record.invoice
    transition_case(session, case, "invoiced", reason="收到代理商付款信息", message_id=stored.id)
    session.commit()

    if not record.created:
        # 重发同一张发票：不重复推群，避免同一笔款被安排两次
        return ProcessResult(stored.id, thread.id, case.id, assignment, intent, False)

    if not notify:
        # 重新处理：只重建数据，不重复推群安排付款
        return ProcessResult(stored.id, thread.id, case.id, assignment, intent, False)

    commission, agent = _case_context(session, case)
    feishu_text = build_payment_text(
        commission_name=commission.legal_name,
        agent_name=agent.name,
        amount=invoice.amount or "未识别",
        currency=invoice.currency,
        due_date=invoice.due_date,
        payment_method=invoice.payment_method,
        remit_detail=invoice.remit_detail,
        invoice_no=invoice.invoice_no,
        attachment_url=attachment_url,
    )
    # AI 审核：付款通知内容明显错误（付款方式未识别/收款人残缺/金额可疑）时拦截，不推群
    approve, reject_reason = _review_payment_notice(feishu_text)
    if not approve:
        audit.record(
            session,
            action="payment.notice_rejected",
            thread_id=thread.id,
            message_id=stored.id,
            case_id=case.id,
            decision="rejected",
            reasons=[f"AI 审核拦截付款通知：{reject_reason}"],
        )
        return ProcessResult(stored.id, thread.id, case.id, assignment, intent, False)

    notified_wecom = notify_wecom(
        webhook if webhook is not None else settings.wecom_webhook,
        build_payment_markdown(
            commission_name=commission.legal_name,
            agent_name=agent.name,
            amount=invoice.amount or "未识别",
            currency=invoice.currency,
            due_date=invoice.due_date,
            payment_method=invoice.payment_method,
            remit_detail=invoice.remit_detail,
            invoice_no=invoice.invoice_no,
            attachment_url=attachment_url,
        ),
    )
    notified_feishu = notify_feishu(settings.feishu_webhook, feishu_text)
    notified = notified_wecom or notified_feishu
    audit.record(
        session,
        action="payment.notify",
        thread_id=thread.id,
        message_id=stored.id,
        case_id=case.id,
        decision="sent" if notified else "failed",
        reasons=["检测到付款信息，推送通知安排支付"],
    )
    return ProcessResult(stored.id, thread.id, case.id, assignment, intent, notified)
