"""邮件正文降噪：剥离引用历史、签名、免责声明，保留业务正文。"""
import html
import re

QUOTE_PATTERNS = (
    r"^-{2,}\s*Original( Message)?\s*-{2,}$",
    r"^-{2,}\s*Forwarded message\s*-{2,}$",
    r"^-{2,}\s*原始邮件\s*-{2,}$",
    r"^原始邮件\s*$",
    r"^On\s.{10,300}\bwrote:\s*$",
    r"^在.{4,200}写道[:：]\s*$",
    r"^发件人[:：]",
    r"^发件时间[:：]",
    r"^收件人[:：]",
    r"^主题[:：]",
    r"^From:\s",
    r"^Sent from my .+$",
)

DISCLAIMER_PATTERNS = (
    r"This (e-?mail|message) and any attachments.*",
    r"CONFIDENTIALITY NOTICE.*",
    r"本(邮件|电子邮件)及(其)?附件.*",
    r"此(邮件|电子邮件)及(其)?附件.*",
)

SIGNATURE_PATTERN = re.compile(r"^(-{2,}\s*|_{10,})$")


def clean_body(text: str | None, *, limit: int = 6000) -> str:
    """去掉引用块、签名和免责声明，压缩空行并截断。"""
    if not text:
        return ""
    # QQ/HTML 客户端常用 HTML 实体（&nbsp;/&gt; 等）编码正文，先还原再识别引用头
    text = html.unescape(text).replace("\xa0", " ")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(">"):
            break
        if any(re.match(pattern, stripped, re.I) for pattern in QUOTE_PATTERNS):
            break
        if SIGNATURE_PATTERN.match(stripped):
            break
        kept.append(line)

    body = "\n".join(kept)
    for pattern in DISCLAIMER_PATTERNS:
        match = re.search(pattern, body, re.I | re.M)
        if match:
            body = body[: match.start()]
            break
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body[:limit]


def recent_history(messages, *, per_message: int = 6000, total: int = 12000) -> list[dict]:
    """从最新往回取，总量不超过 total，输出仍按时间正序。"""
    picked: list[dict] = []
    used = 0
    for item in reversed(messages):
        body = clean_body(item.body_text, limit=per_message)
        cost = len(body) + len(item.subject or "")
        if picked and used + cost > total:
            break
        picked.append({"direction": item.direction, "subject": item.subject, "body": body})
        used += cost
    return list(reversed(picked))
