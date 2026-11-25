"""自动发送时间窗测试：纽约时间 09:00 起才允许自动发信（人工批准不受限）。"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from autoemail.channels.base import SendResult
from autoemail.models import DRAFT_BLOCKED, Draft
from autoemail.outbound import send_draft
from autoemail.policy.gate import AUTO_SEND, DRAFT, GateContext, evaluate
from autoemail.policy.window import auto_send_window_open

NY = ZoneInfo("America/New_York")


class FakeChannel:
    name = "fake"

    def __init__(self):
        self.calls = []

    def send(self, outbound):
        self.calls.append(outbound)
        return SendResult(ok=True, channel_msg_id="window-1")


def _turn_auto_on(session, world):
    world["case"].auto_mode = "on"
    session.commit()


# ---- 时间窗判定 ----

def test_window_before_9am_ny_closed():
    opened, _ = auto_send_window_open(datetime(2026, 1, 15, 8, 59, tzinfo=NY))
    assert opened is False


def test_window_at_9am_ny_open():
    opened, desc = auto_send_window_open(datetime(2026, 1, 15, 9, 0, tzinfo=NY))
    assert opened is True
    assert "09:00" in desc and "纽约" in desc


def test_window_converts_from_utc_est():
    # 1 月为东部标准时间（UTC-5）：UTC 14:00 = 纽约 09:00
    assert auto_send_window_open(datetime(2026, 1, 15, 13, 59, tzinfo=ZoneInfo("UTC")))[0] is False
    assert auto_send_window_open(datetime(2026, 1, 15, 14, 0, tzinfo=ZoneInfo("UTC")))[0] is True


def test_window_respects_dst():
    # 7 月为夏令时（UTC-4）：UTC 13:00 = 纽约 09:00
    assert auto_send_window_open(datetime(2026, 7, 15, 12, 59, tzinfo=ZoneInfo("UTC")))[0] is False
    assert auto_send_window_open(datetime(2026, 7, 15, 13, 0, tzinfo=ZoneInfo("UTC")))[0] is True


# ---- 闸门规则 R11 ----

def _ctx(**overrides):
    base = dict(
        case_state="quoted_out",
        auto_mode="on",
        recipients=["toni@a1surety.com"],
        thread_participants=["toni@a1surety.com"],
        agent_domains=["a1surety.com"],
        body_text="normal reply",
        state_allows_auto=True,
        ledger_claimed=True,
    )
    base.update(overrides)
    return GateContext(**base)


def test_gate_allows_when_window_open():
    assert evaluate(_ctx()).action == AUTO_SEND


def test_gate_blocks_auto_send_before_window():
    decision = evaluate(_ctx(auto_send_window_open=False, auto_send_window_desc="纽约时间每天 09:00 起"))
    assert decision.action == DRAFT
    assert decision.rule_ids == ["R11"]
    assert "09:00" in decision.reasons[0]


def test_manual_approval_ignores_window():
    decision = evaluate(
        _ctx(
            auto_send_window_open=False,
            auto_send_window_desc="纽约时间每天 09:00 起",
            manual_approval=True,
        )
    )
    assert decision.action == AUTO_SEND


# ---- 端到端：自动发送被时间窗拦下 / 窗开后正常发出 ----

def _create_draft_pending(session, world):
    """与 test_outbound._create_draft 相同的入站消息，但禁止流水线自动发送，
    由用例自己控制 send_draft 的时机与渠道（FakeChannel）。"""
    from autoemail.channels.eml import EmlDirectoryChannel
    from autoemail.pipeline import process_message

    fixtures = Path(__file__).parent / "fixtures"
    result = process_message(
        session, EmlDirectoryChannel(fixtures).fetch_since()[0], auto_send=False
    )
    return session.get(Draft, result.draft_id)


def test_send_draft_auto_blocked_before_window(session, world, monkeypatch):
    _turn_auto_on(session, world)
    monkeypatch.setattr(
        "autoemail.outbound.auto_send_window_open",
        lambda: (False, "纽约时间每天 09:00 起"),
    )
    draft = _create_draft_pending(session, world)
    channel = FakeChannel()
    send_draft(session, draft, channel=channel, actor="agent", manual_approval=False)
    assert channel.calls == []
    assert draft.status == DRAFT_BLOCKED
    assert "R11" in draft.rule_hits


def test_send_draft_auto_sends_after_window(session, world, monkeypatch):
    _turn_auto_on(session, world)
    monkeypatch.setattr(
        "autoemail.outbound.auto_send_window_open",
        lambda: (True, "纽约时间每天 09:00 起"),
    )
    draft = _create_draft_pending(session, world)
    channel = FakeChannel()
    send_draft(session, draft, channel=channel, actor="agent", manual_approval=False)
    assert len(channel.calls) == 1
    assert draft.status == "sent"


# ---- 每日询价批次同样受时间窗约束 ----

def test_daily_inquiry_batch_waits_for_window(session, world, monkeypatch):
    monkeypatch.setattr(
        "autoemail.policy.window.auto_send_window_open",
        lambda: (False, "纽约时间每天 09:00 起"),
    )
    # .env 可能关掉了每日询价（开发设备），测试显式打开
    monkeypatch.setattr("autoemail.daily_inquiry.settings.daily_inquiry_enabled", True)
    from autoemail.daily_inquiry import maybe_run_daily_batch

    summary = maybe_run_daily_batch(session, channel=FakeChannel())
    assert summary == {
        "ran": False,
        "reason": "未到自动发送时间窗（纽约时间每天 09:00 起），今日询价批次稍后执行",
    }


def test_daily_inquiry_batch_runs_after_window(session, world, monkeypatch):
    monkeypatch.setattr(
        "autoemail.policy.window.auto_send_window_open",
        lambda: (True, "纽约时间每天 09:00 起"),
    )
    monkeypatch.setattr("autoemail.daily_inquiry.settings.daily_inquiry_enabled", True)
    from autoemail.daily_inquiry import maybe_run_daily_batch

    summary = maybe_run_daily_batch(session, channel=FakeChannel())
    assert summary is not None and summary.get("ran") is not None
