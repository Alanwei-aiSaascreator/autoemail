"""RFC822 解析：把原始邮件字节变成 NormalizedMessage。

eml 回放与 IMAP 拉取共用这一份实现，避免两条路径行为不一致。
"""

import hashlib
import re
from datetime import datetime
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

from ..channels.base import NormalizedAttachment, NormalizedMessage

SENSITIVE_DOC_TYPES = frozenset({"driver_license", "passport", "state_id", "ssn_card"})

# 文件名关键词 → 材料类型。宁可归到 unknown 交人工，不要瞎猜。
_DOC_TYPE_RULES = (
    ("driver_license", ("driver", "license", "驾照")),
    ("passport", ("passport", "护照")),
    ("state_id", ("state id", "州身份证")),
    ("poa", ("poa", "power of attorney", "授权书")),
    ("invoice", ("invoice", "发票")),
    ("articles", ("article", "articles of incorporation", "章程")),
    # application 必须在 bond 之前：否则 "Continuous Bond Application" 会被 bond 误吞
    ("application", ("application", "申请表", "form")),
    ("bond", ("bond",)),
    ("shipment", ("shipment", "bol", "packing list", "运单")),
)


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        if tag in ("p", "div", "br", "tr", "li"):
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._chunks.append(data)

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "".join(self._chunks)).strip()


def html_to_text(html: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(html)
    return parser.text()


def infer_doc_type(filename: str) -> str:
    lowered = filename.lower()
    for doc_type, keywords in _DOC_TYPE_RULES:
        if any(keyword in lowered for keyword in keywords):
            return doc_type
    return "unknown"


def _clean_msg_id(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().strip("<>") or None


def _split_ids(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip("<>") for part in value.split() if part.strip("<>")]


def parse_email_bytes(raw: bytes, channel: str = "eml") -> NormalizedMessage:
    msg = BytesParser(policy=policy.default).parsebytes(raw)

    body_text = ""
    body_html: str | None = None
    attachments: list[NormalizedAttachment] = []

    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()

        if filename:
            payload = part.get_payload(decode=True) or b""
            attachments.append(
                NormalizedAttachment(
                    filename=filename,
                    mime=part.get_content_type(),
                    content=payload,
                    sha256=hashlib.sha256(payload).hexdigest(),
                    doc_type=infer_doc_type(filename),
                )
            )
            continue

        if disposition == "attachment":
            continue

        if part.get_content_type() == "text/plain" and not body_text:
            body_text = part.get_content()
        elif part.get_content_type() == "text/html" and body_html is None:
            body_html = part.get_content()

    if not body_text and body_html:
        body_text = html_to_text(body_html)

    from_addr = parseaddr(msg.get("From", ""))[1].lower()
    to_addrs = [addr.lower() for _, addr in _address_pairs(msg.get_all("To", []))]
    cc_addrs = [addr.lower() for _, addr in _address_pairs(msg.get_all("Cc", []))]

    message_id = _clean_msg_id(msg.get("Message-ID"))
    dedupe_hash = (
        hashlib.sha256(message_id.encode()).hexdigest()
        if message_id
        else hashlib.sha256(raw).hexdigest()
    )

    sent_at: datetime | None = None
    try:
        sent_at = parsedate_to_datetime(msg.get("Date", ""))
    except (TypeError, ValueError):
        sent_at = None

    return NormalizedMessage(
        channel=channel,
        channel_msg_id=message_id,
        from_addr=from_addr,
        subject=str(msg.get("Subject", "") or ""),
        body_text=body_text,
        sent_at=sent_at,
        in_reply_to=_clean_msg_id(msg.get("In-Reply-To")),
        references=_split_ids(msg.get("References")),
        to_addrs=to_addrs,
        cc_addrs=cc_addrs,
        body_html=body_html,
        attachments=attachments,
        dedupe_hash=dedupe_hash,
    )


def _address_pairs(values: list[str]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for value in values:
        for chunk in value.split(","):
            name, addr = parseaddr(chunk)
            if addr:
                pairs.append((name, addr.lower()))
    return pairs
