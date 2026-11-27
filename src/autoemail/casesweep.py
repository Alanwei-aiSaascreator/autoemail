"""Case 清扫：比价出局的探测 case 自动落 lost，避免"进行中"无限堆积。

背景：每日随机询价每家委托每天找 20 家新代理比价，一个代理一个 case，
日积月累"进行中"会堆上千个——其中绝大多数早已出局或无回音，但系统之前
没有任何机制把它们送进终态（closed/lost），导致 Case 列表全堆在"进行中"。

三条规则（全部走状态机白名单 transition_case，逐案留审计）：
1. 中标收敛：同委托任一 case 进入 paid → 其余还在早期阶段（init/quoted_out/
   quoted/materials_requested）的 case 全部 lost（比价落选）。
   blocked（人工接管/退信冻结）与实质推进中的（bond_received/invoiced）不动。
2. 比价出局超时：quoted_out 停留超过 QUOTED_OUT_STALE_DAYS 天 → lost。
3. 询价无回音：init 停留超过 INIT_STALE_DAYS 天 → lost。

每日只跑一次（watch 每轮轮询前检查，与每日询价批次同一节流模式）。
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit
from .models import Case, TERMINAL_STATES
from .state import transition_case

SWEEP_ACTION = "case.sweep"
QUOTED_OUT_STALE_DAYS = 7
INIT_STALE_DAYS = 14
# 中标收敛时允许直接关闭的早期状态（实质推进中的不动）
CONCEDE_EARLY_STATES = ("init", "quoted_out", "quoted", "materials_requested")
# 超时清扫覆盖的状态 → 天数
STALE_RULES = {
    "quoted_out": QUOTED_OUT_STALE_DAYS,
    "init": INIT_STALE_DAYS,
}


def run_case_sweep(session: Session, *, actor: str = "agent") -> dict:
    """执行一轮清扫，返回汇总（供审计与日志）。幂等：已是终态的 case 不会重复处理。"""
    now = datetime.now(timezone.utc)
    counts = {"conceded": 0, "stale": 0}
    details: list[str] = []

    # 规则 1：中标收敛——有 paid 的委托，其余早期 case 全部落选关闭
    paid_commission_ids = set(
        session.scalars(
            select(Case.commission_id).where(Case.state == "paid")
        ).all()
    )
    if paid_commission_ids:
        conceding = session.scalars(
            select(Case).where(
                Case.commission_id.in_(paid_commission_ids),
                Case.state.in_(CONCEDE_EARLY_STATES),
            )
        ).all()
        for case in conceding:
            if transition_case(session, case, "lost", reason="同委托其他 case 已付款中标，比价落选关闭"):
                counts["conceded"] += 1
                details.append(f"{case.case_no}:paid 收敛")
                audit.record(
                    session,
                    action=SWEEP_ACTION,
                    case_id=case.id,
                    actor=actor,
                    decision="lost",
                    reasons=["同委托其他 case 已付款中标（paid），比价落选自动关闭"],
                )

    # 规则 2/3：按状态超时清扫
    for state, days in STALE_RULES.items():
        threshold = now - timedelta(days=days)
        stale = session.scalars(
            select(Case).where(
                Case.state == state,
                Case.state_entered_at.is_not(None),
                Case.state_entered_at < threshold,
            )
        ).all()
        for case in stale:
            reason = (
                f"比价出局超过 {days} 天无后续，自动关闭"
                if state == "quoted_out"
                else f"询价发出超过 {days} 天无回音，自动关闭"
            )
            if transition_case(session, case, "lost", reason=reason):
                counts["stale"] += 1
                details.append(f"{case.case_no}:{state} 超时")
                audit.record(
                    session,
                    action=SWEEP_ACTION,
                    case_id=case.id,
                    actor=actor,
                    decision="lost",
                    reasons=[reason],
                )

    session.commit()
    return {"ran": True, **counts, "total": counts["conceded"] + counts["stale"], "details": details}


def maybe_run_case_sweep(session: Session, *, actor: str = "agent") -> dict | None:
    """每日只跑一次；当天已跑过返回 None。与每日询价批次同一节流模式。"""
    today = datetime.now(timezone.utc).date().isoformat()
    done = session.execute(
        select(audit.AuditEvent.id).where(
            audit.AuditEvent.action == SWEEP_ACTION + ".batch",
            audit.AuditEvent.decision == today,
        ).limit(1)
    ).first()
    if done:
        return None
    summary = run_case_sweep(session, actor=actor)
    audit.record(
        session,
        action=SWEEP_ACTION + ".batch",
        actor=actor,
        decision=today,
        reasons=[
            f"conceded={summary['conceded']}",
            f"stale={summary['stale']}",
            f"total={summary['total']}",
        ],
    )
    session.commit()
    return summary
