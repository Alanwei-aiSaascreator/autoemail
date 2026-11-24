"""渠道抽象：业务层只认 NormalizedMessage，渠道差异到此为止。

一期只实现 eml 目录回放与 IMAP/SMTP；Graph / Gmail 后补，业务层零改动。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass
class NormalizedAttachment:
    filename: str
    mime: str | None
    content: bytes
    sha256: str
    doc_type: str | None = None
    expiry_date: object | None = None


@dataclass
class NormalizedMessage:
    channel: str
    channel_msg_id: str | None
    from_addr: str
    subject: str
    body_text: str
    sent_at: datetime | None = None
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    to_addrs: list[str] = field(default_factory=list)
    cc_addrs: list[str] = field(default_factory=list)
    body_html: str | None = None
    attachments: list[NormalizedAttachment] = field(default_factory=list)
    dedupe_hash: str = ""


@dataclass
class OutboundMessage:
    to_addrs: list[str]
    subject: str
    body_text: str
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    # 随邮件发送的本地文件路径（自动填表产物等）
    attachments: list[str] = field(default_factory=list)


@dataclass
class SendResult:
    ok: bool
    channel_msg_id: str | None = None
    error: str | None = None
    auth_failed: bool = False


class MessageChannel(Protocol):
    """收发通道协议。"""

    name: str

    def fetch_since(self, cursor: str | None = None) -> list[NormalizedMessage]:
        ...

    def send(self, outbound: OutboundMessage) -> SendResult:
        ...
