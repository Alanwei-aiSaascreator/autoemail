"""一次性补发脚本（2026-11-27）：把上午被中断的批次剩余量发完。

- 每家委托今日目标 70 封：上午已发的（Enpro 51 封）按差额补足，其余 9 家各发 70
- 复用与每日批次完全相同的机制：永不重复询价、跨委托当日去重、SMTP 预检、认证失败熔断、逐家提交
- 结束后写审计 decision=今天，今晚纽约 9 点的自动批次自动跳过，明日恢复正常节奏
"""

import json
import random
import sys
import time
from datetime import date

# Windows 控制台默认 cp1252/GBK，代理商名含全角字符会让 print 直接崩溃
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import func, select

from autoemail import audit
from autoemail.channels.imap import ImapSmtpChannel, channel_for_commission
from autoemail.daily_inquiry import (
    BATCH_ACTION,
    _inquired_agent_ids,
    _ready_commissions,
)
from autoemail.db import get_session
from autoemail.initiate import start_case
from autoemail.models import Agent, Case

TARGET = 70
TODAY = date.today().isoformat()
INTERVAL_SECONDS = 3  # 与日常批次相同的发送间隔


def main() -> None:
    session = get_session()
    channel = ImapSmtpChannel()
    commissions = _ready_commissions(session)
    print(f"READY {[c.legal_name for c in commissions]}", flush=True)

    # 发送前逐家 SMTP 预检（与每日批次一致）
    verified: dict[int, object] = {}
    for c in commissions:
        ch = channel_for_commission(c) or channel
        ok, err = ch.verify()
        if ok:
            verified[c.id] = ch
        print(f"PRECHECK {'OK' if ok else 'FAIL'} {c.legal_name} {err or ''}", flush=True)

    # 今日已联系过的代理商（跨委托去重，含上午已发的量）
    taken_today = set(
        session.scalars(select(Case.agent_id).where(func.date(Case.created_at) == TODAY))
    )
    pool = [a for a in session.scalars(select(Agent)) if a.email_addresses]
    random.shuffle(pool)
    print(f"TODAY_TAKEN {len(taken_today)} POOL {len(pool)}", flush=True)

    summary: list[str] = []
    tripped: set[int] = set()
    for c in commissions:
        if c.id not in verified:
            summary.append(f"{c.legal_name}: 预检失败跳过")
            continue
        inquired = _inquired_agent_ids(session, c.id)
        sent_today = session.scalar(
            select(func.count(Case.id)).where(
                Case.commission_id == c.id, func.date(Case.created_at) == TODAY
            )
        )
        remaining = TARGET - (sent_today or 0)
        if remaining <= 0:
            summary.append(f"{c.legal_name}: 今日已发 {sent_today}，无需补发")
            print(f"SKIP {c.legal_name} already {sent_today}", flush=True)
            continue
        picked = []
        for a in pool:
            if len(picked) >= remaining:
                break
            if a.id in inquired or a.id in taken_today:
                continue
            picked.append(a)
        if len(picked) < remaining:
            print(f"WARNING {c.legal_name}: 池不足，只能发 {len(picked)}/{remaining}", flush=True)
        ok_n = 0
        for a in picked:
            if c.id in tripped:
                break
            taken_today.add(a.id)
            try:
                result = start_case(
                    session,
                    commission_id=c.id,
                    agent_id=a.id,
                    agent_email=a.email_addresses[0],
                    auto_mode="on",
                    channel=channel,
                )
            except Exception as exc:  # noqa: BLE001 单家失败不中断整批
                session.rollback()
                print(f"FAIL {c.legal_name} -> {a.name}: {exc}", flush=True)
                continue
            if result.send_result is not None and result.send_result.auth_failed:
                tripped.add(c.id)
                print(f"AUTH_FAIL {c.legal_name} 熔断后续", flush=True)
                continue
            ok_n += 1
            print(f"SENT {c.legal_name} -> {a.name} ({ok_n}/{len(picked)})", flush=True)
            session.commit()
            time.sleep(INTERVAL_SECONDS)
        summary.append(f"{c.legal_name}: 补发成功 {ok_n} 家")
        print(f"DONE {c.legal_name} ok={ok_n}", flush=True)

    audit.record(
        session,
        action=BATCH_ACTION,
        actor="agent",
        decision=TODAY,
        reasons=summary or ["无新增"],
    )
    session.commit()
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)
    print("ALL_DONE", flush=True)


if __name__ == "__main__":
    main()
