"""补发脚本（2026-11-28）：凌晨批次因 watch 缺 --allow-auto-send 只建了草稿未发信。

处理方式：
1. 找出今天 700 个 dry_run 询价 case，逐个按真实发送重试（同一 thread 的 planned
   消息已存在，start_case 会因活跃 case 冲突拒绝）——因此不走 start_case，改为
   直接对既有 case 执行"补发送"：复用原正文、抢占台账、发送、置 quoted_out。
2. 成功后写审计 decision=今天（防止今晚自动批次再次空跑）。
"""

import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from datetime import date

from sqlalchemy import select

from autoemail import audit
from autoemail.channels.base import OutboundMessage
from autoemail.channels.imap import channel_for_commission
from autoemail.db import get_session
from autoemail.models import Case, Commission, Draft, Message, SendLedger, Thread
from autoemail.state import transition_case
from autoemail.followups import schedule_followup
from autoemail.initiate import utcnow

INTERVAL_SECONDS = 3


def main() -> None:
    session = get_session()
    today = date.today().isoformat()
    dry_cases = session.scalars(
        select(Case).where(Case.auto_mode == "dry_run", Case.state == "init")
    ).all()
    print(f"待补发 dry_run 询价 case 数: {len(dry_cases)}", flush=True)

    sent = failed = 0
    for case in dry_cases:
        commission = session.get(Commission, case.commission_id)
        thread = session.scalars(select(Thread).where(Thread.case_id == case.id)).first()
        planned = session.scalars(
            select(Message).where(
                Message.thread_id == thread.id, Message.direction == "out"
            )
        ).first()
        draft = session.scalars(
            select(Draft).where(Draft.case_id == case.id, Draft.intent == "initial_inquiry")
        ).first()
        if commission is None or thread is None or planned is None or draft is None:
            print(f"SKIP case={case.case_no} 数据不完整", flush=True)
            continue
        recipient = thread.participants[0] if thread.participants else None
        if not recipient:
            print(f"SKIP case={case.case_no} 无收件地址", flush=True)
            continue
        channel = channel_for_commission(commission)
        if channel is None or not (channel.verify()[0]):
            print(f"FAIL case={case.case_no} SMTP 预检未通过", flush=True)
            failed += 1
            continue
        dedupe_key = f"initial-inquiry|{case.id}|{recipient}"
        ledger = SendLedger(dedupe_key=dedupe_key, result="claimed")
        session.add(ledger)
        try:
            session.flush()
        except Exception:
            session.rollback()
            print(f"SKIP case={case.case_no} 台账已存在（可能已发）", flush=True)
            continue
        result = channel.send(
            OutboundMessage(to_addrs=[recipient], subject=planned.subject, body_text=planned.body_text)
        )
        if not getattr(result, "ok", False):
            ledger.result = "failed"
            session.commit()
            failed += 1
            print(f"FAIL case={case.case_no} {getattr(result, 'error', '')}", flush=True)
            continue
        planned.channel_msg_id = result.channel_msg_id
        planned.sent_at = utcnow()
        ledger.result = "sent"
        ledger.channel_msg_id = result.channel_msg_id
        transition_case(session, case, "quoted_out", reason="补发送成功", message_id=planned.id)
        case.last_outbound_at = planned.sent_at
        case.waiting_since = planned.sent_at
        if draft is not None:
            draft.status = "sent"
        schedule_followup(session, case, thread, trigger_state="quoted_out", trigger_message_id=planned.id)
        audit.record(
            session,
            action="case.initiated",
            case_id=case.id,
            thread_id=thread.id,
            message_id=planned.id,
            decision="sent",
            reasons=["dry_run 询价补发送成功"],
        )
        session.commit()
        sent += 1
        print(f"SENT case={case.case_no} -> {recipient} ({sent})", flush=True)
        time.sleep(INTERVAL_SECONDS)

    audit.record(
        session,
        action="daily_inquiry.batch",
        actor="agent",
        decision=today,
        reasons=[f"补发送：成功 {sent} 封，失败 {failed} 封（凌晨批次因 watch 缺发送参数未真实发出）"],
    )
    session.commit()
    print(f"ALL_DONE sent={sent} failed={failed}", flush=True)


if __name__ == "__main__":
    main()
