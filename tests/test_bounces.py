"""DSN 退信识别与全局拉黑测试。

场景：Exchange DSN 5.7.129（you don't have permission to send to it）等退信
表明对方邮箱拒收。系统必须识别退信 → 地址进全局黑名单（所有委托均不再发送）
→ 冻结线程 + 暂停跟催 → 推群通知人工。
"""

from autoemail.bounces import detect_bounce
from autoemail.channels.base import NormalizedMessage, SendResult
from autoemail.models import DRAFT_BLOCKED, Draft, EmailBlocklist, Thread
from autoemail.outbound import send_draft
from autoemail.pipeline import process_message
from autoemail.policy.gate import DRAFT, GateContext, evaluate

DSN_BODY = (
    "Delivery has failed to these recipients or groups:\n"
    "\n"
    "robert.cristiano@everestre.com<mailto:robert.cristiano@everestre.com>\n"
    "Your message couldn't be delivered to the recipient because you don't "
    "have permission to send to it.\n"
    "\n"
    "Ask the recipient's email admin to add you to the accept list for the recipient.\n"
    "\n"
    "For more information, see DSN 5.7.129 Errors in Exchange Online and Office 365."
)


class FakeChannel:
    name = "fake"

    def __init__(self):
        self.calls = []

    def send(self, outbound):
        self.calls.append(outbound)
        return SendResult(ok=True, channel_msg_id="bounce-1")


def _dsn_message(dedupe_hash: str) -> NormalizedMessage:
    return NormalizedMessage(
        channel="email",
        channel_msg_id=f"dsn-{dedupe_hash}",
        from_addr="postmaster@outlook.com",
        subject="Undeliverable: Re: [BOND-2026-0142] Application",
        body_text=DSN_BODY,
        dedupe_hash=dedupe_hash,
    )


# ---- 识别 ----

def test_detect_bounce_dsn_57129():
    info = detect_bounce(_dsn_message("detect-1"))
    assert info is not None
    assert info.recipients == ["robert.cristiano@everestre.com"]
    assert info.dsn_code == "5.7.129"


def test_detect_bounce_ignores_normal_reply():
    message = NormalizedMessage(
        channel="email",
        channel_msg_id="normal-1",
        from_addr="toni@a1surety.com",
        subject="Re: [BOND-2026-0142] Application",
        body_text=(
            "We are pleased to quote the continuous customs bond. "
            "Please confirm whether the goods are subject to antidumping duties."
        ),
        dedupe_hash="normal-detect-1",
    )
    assert detect_bounce(message) is None


def test_detect_bounce_requires_failure_phrase():
    # 有 DSN 代码但没有投递失败描述（如成功回执含 2.x.x）不触发
    message = NormalizedMessage(
        channel="email",
        channel_msg_id="receipt-1",
        from_addr="postmaster@outlook.com",
        subject="Delivered: Re: [BOND-2026-0142] Application",
        body_text="Your message was delivered. DSN 5.7.0 reference archived.",
        dedupe_hash="receipt-hash-1",
    )
    assert detect_bounce(message) is None


# ---- 管线：拉黑 + 冻结 + 停跟催 + 推群 ----

def test_bounce_blacklists_and_freezes(session, world, monkeypatch):
    notified = []
    monkeypatch.setattr(
        "autoemail.pipeline.notify_wecom",
        lambda webhook, content: notified.append(content) or True,
    )
    monkeypatch.setattr("autoemail.pipeline.notify_feishu", lambda webhook, content: True)

    result = process_message(session, _dsn_message("bounce-pipe-1"))

    assert result.ignored_reason == "bounce"
    assert result.draft_id is None
    row = (
        session.query(EmailBlocklist)
        .filter_by(email="robert.cristiano@everestre.com")
        .one()
    )
    assert "5.7.129" in row.reason
    thread = session.get(Thread, result.thread_id)
    assert thread.frozen is True
    assert world["case"].followup_paused is True
    assert notified and "robert.cristiano@everestre.com" in notified[0]


# ---- 闸门 R13：黑名单地址一律不发（含其他委托、含人工批准） ----

def test_gate_r13_blocks_blocked_recipient():
    ctx = GateContext(
        case_state="quoted_out",
        auto_mode="on",
        recipients=["robert.cristiano@everestre.com"],
        thread_participants=["robert.cristiano@everestre.com"],
        agent_domains=["everestre.com"],
        body_text="normal",
        state_allows_auto=True,
        ledger_claimed=True,
        manual_approval=True,
        blocked_recipients=["robert.cristiano@everestre.com"],
    )
    decision = evaluate(ctx)
    assert decision.action == DRAFT or decision.action == "block"
    assert decision.rule_ids == ["R13"]


def test_send_draft_blocked_for_blacklisted_address(session, world, monkeypatch):
    session.add(EmailBlocklist(email="toni@a1surety.com", reason="DSN 5.7.129 拒收"))
    session.commit()
    world["case"].auto_mode = "on"
    monkeypatch.setattr("autoemail.outbound.auto_send_window_open", lambda: (True, "窗口开"))

    from pathlib import Path

    from autoemail.channels.eml import EmlDirectoryChannel

    fixtures = Path(__file__).parent / "fixtures"
    result = process_message(
        session, EmlDirectoryChannel(fixtures).fetch_since()[0], auto_send=False
    )
    draft = session.get(Draft, result.draft_id)
    channel = FakeChannel()

    # 自动发送被拦
    send_draft(session, draft, channel=channel, actor="agent", manual_approval=False)
    assert channel.calls == []
    assert draft.status == DRAFT_BLOCKED
    assert "R13" in draft.rule_hits

    # 人工批准同样被拦：拒收地址不允许再次发送，直到人工移出黑名单
    send_draft(session, draft, channel=channel, actor="human", manual_approval=True)
    assert channel.calls == []
    assert "R13" in draft.rule_hits
