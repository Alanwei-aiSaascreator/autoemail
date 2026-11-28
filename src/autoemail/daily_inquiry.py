"""每日随机询价调度。

规则（2026-11 需求更新）：
- 对文件齐全的委托公司，每天随机抽取一批代理商发出询价（默认 20 家）
- 同一委托对同一代理商永不重复询价（以该组合是否存在 case 为准，含已关闭）
- 多个委托同日分配代理商时全局去重，尽量避免命中同一家（少量重叠可接受）
- 委托一旦出现已付款案件，停止该委托的每日询价
- 每天只跑一次；守护进程每轮轮询前检查，手动可用 CLI 触发
"""

from __future__ import annotations

import logging
import random
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit
from .channels.base import MessageChannel
from .channels.imap import channel_for_commission
from .config import settings
from .documents import missing_documents
from .initiate import missing_commission_fields, start_case
from .models import Agent, AuditEvent, Case, Commission

LOGGER = logging.getLogger(__name__)

BATCH_ACTION = "daily_inquiry.batch"
TERMINAL_STATES = ("closed", "lost", "paid")


def _already_ran_today(session: Session) -> bool:
    today = date.today().isoformat()
    return (
        session.scalar(
            select(AuditEvent.id).where(
                AuditEvent.action == BATCH_ACTION,
                AuditEvent.decision == today,
                # -dry-run-no-send 后缀是误配置警告，不算"今日已执行"
                ~AuditEvent.decision.like("%-dry-run-no-send"),
            )
        )
        is not None
    )


def _ready_commissions(session: Session) -> list[Commission]:
    """文件齐全且尚未付款的活跃委托。"""
    ready: list[Commission] = []
    for commission in session.scalars(
        select(Commission).where(Commission.status == "active")
    ):
        if missing_commission_fields(commission):
            continue
        if missing_documents(session, commission.id):
            continue
        paid = session.scalar(
            select(Case.id).where(
                Case.commission_id == commission.id, Case.state == "paid"
            )
        )
        if paid is not None:
            continue
        ready.append(commission)
    return ready


def _inquired_agent_ids(session: Session, commission_id: int) -> set[int]:
    """该委托询价过的所有代理商（含已关闭 case，永不重复）。"""
    return set(
        session.scalars(
            select(Case.agent_id).where(Case.commission_id == commission_id)
        )
    )


def run_daily_batch(
    session: Session,
    *,
    channel: MessageChannel | None = None,
    batch_size: int | None = None,
    actor: str = "agent",
    force: bool = False,
) -> dict:
    """执行（或跳过）当日批量询价。返回汇总 dict。

    channel=None 时按 dry_run 建立 case 与待审草稿，不发送；
    传入 ImapSmtpChannel 则直接发送询价邮件。
    """
    size = batch_size or settings.daily_inquiry_batch_size
    if not force and _already_ran_today(session):
        return {"ran": False, "reason": "今日已执行过批量询价"}

    # channel=None 意味着本批只能生成草稿不能真实发出。守护路径（actor=agent）
    # 属于误配置（watch 忘带 --allow-auto-send）：记一条显式警告审计，且**不写**
    # "今日已执行"的常规审计，不占当天名额，正式批次开启后仍会照常执行。
    # CLI 手动 dry_run（actor=cli）不受影响，属刻意演练。
    dry_run_warning = channel is None and actor == "agent"
    if dry_run_warning:
        audit.record(
            session,
            action=BATCH_ACTION,
            actor=actor,
            decision=f"{date.today().isoformat()}-dry-run-no-send",
            reasons=["警告：守护批次未启用发送渠道，仅生成草稿未真实发出（检查 watch 是否带 --allow-auto-send）"],
        )
        session.commit()
        LOGGER.warning("每日询价批次以 dry_run 运行，未真实发送——检查守护进程启动参数")

    commissions = _ready_commissions(session)
    if not commissions:
        audit.record(
            session,
            action=BATCH_ACTION,
            actor=actor,
            decision=date.today().isoformat(),
            reasons=["今日无可询价委托（无文件齐全且未付款的委托）"],
        )
        session.commit()
        return {"ran": True, "commissions": [], "sent": 0, "planned": 0, "dry_run_no_send": dry_run_warning}

    pool = [a for a in session.scalars(select(Agent)) if a.email_addresses]
    random.shuffle(pool)

    # 发送前预检：逐个确认委托邮箱 SMTP 登录可用，错误密码直接剔除，杜绝连续认证失败触发 fail2ban
    verified_ids: set[int] = set()
    precheck_failures: list[str] = []
    if channel is not None:
        for commission in commissions:
            ch = channel_for_commission(commission) or channel
            ok, err = ch.verify()
            if ok:
                verified_ids.add(commission.id)
            else:
                precheck_failures.append(f"{commission.legal_name}：{err}")
                LOGGER.warning("预检失败，跳过委托 %s：%s", commission.legal_name, err)

    taken: set[int] = set()  # 今日已被其他委托占用，避免多委托扎堆同一代理商
    tripped: set[int] = set()  # 发送中认证失败熔断的委托，跳过其后续所有代理商
    summary: list[dict] = []
    sent = planned = failed = 0
    for commission in commissions:
        if channel is not None and commission.id not in verified_ids:
            continue
        inquired = _inquired_agent_ids(session, commission.id)
        picked = []
        for agent in pool:
            if len(picked) >= size:
                break
            if agent.id in inquired or agent.id in taken:
                continue
            picked.append(agent)
        detail = {"commission": commission.legal_name, "ok": 0, "failed": []}
        for agent in picked:
            if commission.id in tripped:
                break
            taken.add(agent.id)
            auto_mode = "on" if channel is not None else "dry_run"
            try:
                result = start_case(
                    session,
                    commission_id=commission.id,
                    agent_id=agent.id,
                    agent_email=agent.email_addresses[0],
                    auto_mode=auto_mode,
                    channel=channel,
                )
            except Exception as exc:  # noqa: BLE001  单家失败不中断整批
                failed += 1
                detail["failed"].append(f"{agent.name}: {exc}")
                session.rollback()
                continue
            if channel is not None:
                if result.send_result is not None and result.send_result.auth_failed:
                    tripped.add(commission.id)
                    detail["failed"].append(f"{agent.name}: 认证失败，熔断该委托后续发送")
                    LOGGER.warning(
                        "委托 %s 邮箱认证失败，熔断跳过后续：%s",
                        commission.legal_name,
                        result.send_result.error,
                    )
                    continue
                sent += 1
            else:
                planned += 1
            detail["ok"] += 1
            # 逐家提交：中途失败进度保留，重试时按已询过跳过，不会重发
            session.commit()
        summary.append(detail)

    reasons = [f"{item['commission']}：成功 {item['ok']} 家" for item in summary]
    reasons += [
        f"{item['commission']} 失败 {len(item['failed'])}：{msg}"
        for item in summary
        for msg in item["failed"][:3]
    ]
    if precheck_failures:
        reasons.append("预检未通过：" + "；".join(precheck_failures))
    # dry_run 误配置时不写"今日已执行"审计：今天正式批次仍可正常触发。
    # CLI 手动 dry_run（actor=cli）保持原行为，写审计防重复演练。
    if not dry_run_warning:
        audit.record(
            session,
            action=BATCH_ACTION,
            actor=actor,
            decision=date.today().isoformat(),
            reasons=reasons or ["无新增"],
        )
    session.commit()
    return {
        "ran": True,
        "commissions": summary,
        "sent": sent,
        "planned": planned,
        "failed": failed,
        "dry_run_no_send": dry_run_warning,
    }


def maybe_run_daily_batch(
    session: Session,
    *,
    channel: MessageChannel | None = None,
) -> dict | None:
    """守护进程每轮调用：开关开启且今天没跑过才执行，异常记入返回值不中断守护。

    允许发送（channel 非 None）时受自动发送时间窗约束：纽约时间 09:00 前不启动批次。
    """
    if not settings.daily_inquiry_enabled:
        return None
    if channel is not None:
        from .policy.window import auto_send_window_open

        window_open, window_desc = auto_send_window_open()
        if not window_open:
            return {
                "ran": False,
                "reason": f"未到自动发送时间窗（{window_desc}），今日询价批次稍后执行",
            }
    try:
        return run_daily_batch(session, channel=channel)
    except Exception as exc:  # noqa: BLE001  调度失败不影响收件轮询
        session.rollback()
        return {"ran": False, "error": f"{type(exc).__name__}: {exc}"}
