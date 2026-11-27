"""邮件人味层：让每封发出的邮件都有自然的个性和语气变化。

设计原则：
- 确定性生成、不依赖 LLM（离线环境也要有效果），以"邮件内容 + 日期 + case 号"
  做种子，同一封邮件永远得到同一个变体（重跑/审计一致性），不同邮件之间自然分散；
- 只动"寒暄/语气词/开场结尾"，绝不动事实（金额、公司名、材料清单、法律表述）；
- 变体池都是真实商务邮件里常见的高频写法，不追求花哨，追求"不像模板"。
"""

from __future__ import annotations

import hashlib
import random

from .models import Case, Commission

# 通用寒暄（询价/回信开头），带轻微语气差异
_GREETINGS = (
    "Hello {name},",
    "Hi {name},",
    "Hi {name}, I hope your week is going well.",
    "Hello {name}, I hope this finds you well.",
    "Good morning {name},",
    "Good afternoon {name},",
    "Hi {name}, hope all is well on your end.",
)

# 开场句（感谢/引入），按意图分组
_OPENERS_INQUIRY = (
    "{company} is looking to purchase a customs bond and we'd love to get a quote from your agency.",
    "We're reaching out on behalf of {company} — we're in the market for a customs bond and would appreciate a quote from you.",
    "I'm writing to ask whether your agency could quote a customs bond for {company}.",
    "{company} is shopping for a customs bond at the moment, and your agency came up on our list. Could you send us a quote?",
)

_OPENERS_QUOTE_REPLY = (
    "Thank you for the quote for {company}.",
    "Thanks for getting back to us with the quote for {company}.",
    "Thank you for sending over the quote for {company} — much appreciated.",
    "Thanks for the quote for {company}; we've reviewed it.",
)

_OPENERS_QUOTE_MISSING = (
    "Thank you for your message regarding the continuous importer bond for {company}.",
    "Thanks for your note about the continuous importer bond for {company}.",
    "Thanks for following up on the bond for {company}.",
)

_OPENERS_FORM_REQUEST = (
    "Thank you for your message regarding {company}.",
    "Thanks for your email and the details for {company}.",
    "Thanks for laying out what you need for {company}.",
    "Got it — thanks for the checklist for {company}.",
)

_OPENERS_BOND_DELIVERED = (
    "Thank you for sending the bond copy for {company}.",
    "We've received the bond copy for {company} — thank you.",
    "Thanks for forwarding the bond documents for {company}.",
)

# 结尾前的一句轻收口
_CLOSERS = (
    "Please let us know if you need anything else from our side.",
    "Feel free to reach out if anything else comes up.",
    "Happy to provide anything else you may need.",
    "Let us know if you have any questions in the meantime.",
    "",
    "",
)

_SIGNOFFS = (
    "Best regards,",
    "Best,",
    "Regards,",
    "Kind regards,",
    "Many thanks,",
    "Thanks,",
)


def _pick(options: tuple[str, ...], seed: str) -> str:
    digest = hashlib.sha256(seed.encode()).digest()
    return options[digest[0] % len(options)]


def _pick_skip_empty(options: tuple[str, ...], seed: str) -> str:
    value = _pick(options, seed)
    return value


def human_seed(case_no: str, *, channel_msg_id: str = "", sent_at: str = "") -> str:
    return f"{case_no}|{channel_msg_id}|{sent_at}"


def greeting(name: str, seed: str) -> str:
    """生成寒暄行。name 为空时用通用称呼。"""
    if not name or not name.strip():
        base = _pick(("Hello,", "Hi there,", "Good morning,", "Hello team,"), seed)
        return base
    return _pick(_GREETINGS, seed + "|g").format(name=name.strip().split()[0])


def opener_inquiry(company: str, seed: str) -> str:
    return _pick(_OPENERS_INQUIRY, seed + "|oi").format(company=company)


def opener_quote_reply(company: str, seed: str) -> str:
    return _pick(_OPENERS_QUOTE_REPLY, seed + "|oq").format(company=company)


def opener_quote_missing(company: str, seed: str) -> str:
    return _pick(_OPENERS_QUOTE_MISSING, seed + "|om").format(company=company)


def opener_form_request(company: str, seed: str) -> str:
    return _pick(_OPENERS_FORM_REQUEST, seed + "|of").format(company=company)


def opener_bond_delivered(company: str, seed: str) -> str:
    return _pick(_OPENERS_BOND_DELIVERED, seed + "|ob").format(company=company)


def closer(seed: str) -> str:
    """收口句；可能为空（不是每封邮件都需要）。"""
    return _pick(_CLOSERS, seed + "|c")


def signoff(seed: str) -> str:
    return _pick(_SIGNOFFS, seed + "|s")


def humanize_block(
    text: str,
    *,
    intent: str,
    case_no: str,
    channel_msg_id: str = "",
) -> str:
    """对已经拼好的正文做"语气重排"：替换开头寒暄与结尾署名前。

    输入正文约定：第一行是寒暄（Hello/Hi ...），末尾是 "Best regards,\nName"。
    本函数只替换这两处，中间业务内容原样保留。
    """
    lines = text.split("\n")
    if not lines:
        return text
    seed = human_seed(case_no, channel_msg_id=channel_msg_id)
    # 1) 替换寒暄行
    first = lines[0]
    if first.rstrip().endswith((",", ":")) and len(first) < 80:
        # 尝试提取称呼
        parts = first.rstrip(",:").split(" ", 1)
        name = parts[1] if len(parts) > 1 else ""
        # 去掉可能带上的语气尾巴
        name = name.split(" I hope")[0].split(" hope")[0].strip()
        lines[0] = greeting(name, seed)
    # 2) 替换结尾署名引导（Best regards, / Thank you, / Regards,）
    for idx in range(len(lines) - 1, max(len(lines) - 5, -1), -1):
        stripped = lines[idx].strip()
        if stripped in ("Best regards,", "Thank you,", "Regards,", "Thanks,", "Kind regards,", "Many thanks,"):
            lines[idx] = signoff(seed)
            # 署名前插入收口句（若本封邮件抽到非空 closer）
            tail = closer(seed)
            if tail:
                lines.insert(idx, tail)
                lines.insert(idx + 1, "")
            break
    return "\n".join(lines)
