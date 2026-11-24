"""企业微信群机器人通知（V0.2 §12）。

付款信息一旦到达就推群，由人工安排支付；推送内容不得包含证件信息。
"""

import time

import httpx

PAYMENT_LABELS = {
    "wire": "电汇",
    "ach": "ACH",
    "card": "银行卡",
    "zelle": "Zelle",
    "unknown": "未识别",
}

_EXTRA_HINT = {"card": "银行卡支付注意限额与手续费", "zelle": "Zelle 注意单笔限额"}


def build_payment_markdown(
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
        "**Bond 付款通知**",
        f"> 委托公司：{commission_name}",
        f"> 代理商：{agent_name}",
        f"> 金额：{amount} {currency}",
        f"> 付款方式：{PAYMENT_LABELS.get(method, method)}",
        f"> 到期日：{due_date or '未注明'}",
    ]
    if invoice_no:
        lines.append(f"> 发票号：{invoice_no}")
    if remit_detail:
        detail = "；".join(f"{k}={v}" for k, v in remit_detail.items() if v)
        if detail:
            lines.append(f"> 汇款要素：{detail}")
    if attachment_url:
        lines.append(f"> 附件：[查看 Invoice]({attachment_url})")
    hint = _EXTRA_HINT.get(method)
    if hint:
        lines.append(f"> 提示：{hint}")
    lines.append("\n请安排支付，付款后回填付款方式与流水号。")
    return "\n".join(lines)


def build_quote_markdown(
    *,
    commission_name: str,
    agent_name: str,
    case_no: str,
    amount: object,
    currency: str,
) -> str:
    lines = [
        "**Bond 报价通知**",
        f"> 委托公司：{commission_name}",
        f"> 代理商：{agent_name}",
        f"> 案件：{case_no}",
        f"> 报价：{amount} {currency}",
        "\n不议价，横向对比各家报价，低者得；付款由人工安排。",
    ]
    return "\n".join(lines)


def notify(webhook: str, content: str, *, retries: int = 3) -> bool:
    """推送失败重试 3 次；仍失败返回 False，由调用方降级为站内告警。"""
    if not webhook:
        return False
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    for attempt in range(retries):
        try:
            response = httpx.post(webhook, json=payload, timeout=10)
            if response.status_code == 200 and response.json().get("errcode") == 0:
                return True
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(2**attempt)
    return False
