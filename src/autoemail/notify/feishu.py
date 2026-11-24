"""飞书群机器人通知（付款信息推送）。

飞书自定义机器人不支持 markdown，用纯文本 text 消息；推送内容不得包含证件信息。
"""

import hashlib
import logging
import time
from pathlib import Path

import httpx

# 同样内容 10 分钟内只推一次，防止上游重试/重复识别导致刷屏
DEDUPE_WINDOW_SECONDS = 600
_recent: dict[str, float] = {}

_log = logging.getLogger("autoemail.feishu")
if not _log.handlers:
    try:
        Path("logs").mkdir(exist_ok=True)
        _handler = logging.FileHandler(Path("logs") / "feishu.log", encoding="utf-8")
        _handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        _log.addHandler(_handler)
        _log.setLevel(logging.INFO)
    except OSError:
        pass  # 无 logs 目录时只留内存去重，不落日志

PAYMENT_LABELS = {
    "wire": "电汇",
    "ach": "ACH",
    "card": "银行卡",
    "zelle": "Zelle",
    "unknown": "未识别",
}

_EXTRA_HINT = {"card": "银行卡支付注意限额与手续费", "zelle": "Zelle 注意单笔限额"}


def build_payment_text(
    *,
    commission_name: str,
    agent_name: str,
    amount: object,
    currency: str,
    due_date: object | None,
    payment_method: str,
    remit_detail: dict | None = None,
    invoice_no: str | None = None,
    attachment_url: str | None = None,
) -> str:
    method = (payment_method or "unknown").lower()
    lines = [
        "【Bond 付款通知】",
        f"委托公司：{commission_name}",
        f"代理商：{agent_name}",
        f"金额：{amount} {currency}",
        f"付款方式：{PAYMENT_LABELS.get(method, method)}",
        f"到期日：{due_date or '未注明'}",
    ]
    if invoice_no:
        lines.append(f"发票号：{invoice_no}")
    if remit_detail:
        detail = "；".join(f"{k}={v}" for k, v in remit_detail.items() if v)
        if detail:
            lines.append(f"汇款要素：{detail}")
    if attachment_url:
        lines.append(f"附件：{attachment_url}")
    hint = _EXTRA_HINT.get(method)
    if hint:
        lines.append(f"提示：{hint}")
    lines.append("")
    lines.append("请安排支付，付款后回填付款方式与流水号。")
    return "\n".join(lines)


def build_quote_text(
    *,
    commission_name: str,
    agent_name: str,
    case_no: str,
    amount: object,
    currency: str,
) -> str:
    """报价推群文案：人工横向对比，安排后续付款。"""
    lines = [
        "【Bond 报价通知】",
        f"委托公司：{commission_name}",
        f"代理商：{agent_name}",
        f"案件：{case_no}",
        f"报价：{amount} {currency}",
        "",
        "不议价，横向对比各家报价，低者得；付款由人工安排。",
    ]
    return "\n".join(lines)


def notify(webhook: str, content: str, *, retries: int = 3) -> bool:
    """推送失败重试 3 次；仍失败返回 False，由调用方降级为站内告警。

    同样内容在 10 分钟内只推一次；每次调用写入 logs/feishu.log 留痕。
    """
    if not webhook:
        return False
    key = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    now = time.time()
    last = _recent.get(key, 0.0)
    if now - last < DEDUPE_WINDOW_SECONDS:
        _log.info("dedupe-skip 窗口内重复内容，未推送")
        return True  # 视为已通知，调用方不要按失败处理
    payload = {"msg_type": "text", "content": {"text": content}}
    for attempt in range(retries):
        try:
            response = httpx.post(webhook, json=payload, timeout=10)
            # 飞书返回 code == 0 表示成功
            if response.status_code == 200 and response.json().get("code") == 0:
                _recent[key] = now
                _log.info("sent ok head=%r", content[:40])
                return True
            _log.warning("attempt %d rejected status=%s body=%s", attempt + 1,
                         response.status_code, response.text[:200])
        except (httpx.HTTPError, ValueError) as exc:
            _log.warning("attempt %d error %s", attempt + 1, exc)
        time.sleep(2**attempt)
    return False
