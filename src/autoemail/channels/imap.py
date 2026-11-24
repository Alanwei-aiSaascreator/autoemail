"""IMAP 收 + SMTP 发。邮箱平台未定，这是参考实现；Graph / Gmail 后续补同协议类。

注意：本类未纳入自动化测试（无可用邮件服务器），接入真实邮箱时需按平台实测。
"""

import imaplib
import logging
import smtplib
import threading
import time
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

from sqlalchemy import select

from ..config import settings
from ..ingest.parse import parse_email_bytes
from .base import NormalizedMessage, OutboundMessage, SendResult

LOGGER = logging.getLogger(__name__)

# 全局发送节流：保证任意时刻只有一封邮件在途，且相邻两封间隔不低于配置值。
# 用模块级状态（而非实例级）让多个渠道实例也能共享同一节奏，避免瞬时并发触发 fail2ban。
_throttle_lock = threading.Lock()
_last_send_at = 0.0


def _throttle() -> None:
    interval = settings.send_interval_seconds
    if interval <= 0:
        return
    global _last_send_at
    with _throttle_lock:
        now = time.monotonic()
        wait = interval - (now - _last_send_at)
        if wait > 0:
            time.sleep(wait)
        _last_send_at = time.monotonic()


class ImapSmtpChannel:
    name = "imap"

    def __init__(
        self,
        *,
        user: str | None = None,
        password: str | None = None,
        from_addr: str | None = None,
    ) -> None:
        self.host = settings.imap_host
        self.user = user or settings.imap_user
        self.password = password or settings.imap_password
        self.from_addr = from_addr or settings.from_addr or self.user

    def fetch_since(self, cursor: str | None = None) -> list[NormalizedMessage]:
        """拉取未读邮件。cursor 为上一次处理到的 UID。"""
        messages: list[NormalizedMessage] = []
        with imaplib.IMAP4_SSL(self.host) as client:
            client.login(self.user, self.password)
            client.select("INBOX")
            criteria = "UNSEEN" if not cursor else f"UID {int(cursor) + 1}:*"
            status, data = client.uid("SEARCH", None, criteria)
            if status != "OK":
                return messages
            for uid in data[0].split():
                status, payload = client.uid("FETCH", uid, "(RFC822)")
                if status != "OK":
                    continue
                raw = next((part[1] for part in payload if isinstance(part, tuple)), None)
                if raw:
                    messages.append(parse_email_bytes(raw, channel=self.name))
        return messages

    def verify(self) -> tuple[bool, str]:
        """SMTP 登录预检：批量发送前确认账号密码可用，避免错误密码进入循环触发 fail2ban。"""
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as client:
                client.starttls()
                client.login(self.user, self.password)
            return True, ""
        except smtplib.SMTPAuthenticationError as exc:
            return False, f"认证失败 {exc.smtp_code}: {exc.smtp_error}"
        except (smtplib.SMTPException, OSError) as exc:
            return False, str(exc)

    def send(self, outbound: OutboundMessage) -> SendResult:
        _throttle()
        msg = EmailMessage()
        msg["From"] = self.from_addr
        msg["To"] = ", ".join(outbound.to_addrs)
        msg["Subject"] = outbound.subject
        msg["Message-ID"] = make_msgid()
        if outbound.in_reply_to:
            msg["In-Reply-To"] = f"<{outbound.in_reply_to}>"
            msg["References"] = " ".join(f"<{ref}>" for ref in outbound.references)
        msg.set_content(outbound.body_text)

        for path in outbound.attachments or []:
            file = Path(path)
            suffix = file.suffix.lower().lstrip(".")
            if suffix == "pdf":
                maintype, subtype = "application", "pdf"
            elif suffix in {"jpg", "jpeg", "png", "gif", "webp"}:
                maintype, subtype = "image", suffix
            else:
                maintype, subtype = "application", "octet-stream"
            msg.add_attachment(
                file.read_bytes(), maintype=maintype, subtype=subtype, filename=file.name
            )

        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as client:
                client.starttls()
                client.login(self.user, self.password)
                client.send_message(msg)
        except smtplib.SMTPAuthenticationError as exc:
            return SendResult(
                ok=False, error=f"认证失败 {exc.smtp_code}: {exc.smtp_error}", auth_failed=True
            )
        except (smtplib.SMTPException, OSError) as exc:
            return SendResult(ok=False, error=str(exc))
        self._append_to_sent(msg)
        return SendResult(ok=True, channel_msg_id=msg["Message-ID"].strip("<>"))

    def _append_to_sent(self, msg: EmailMessage) -> None:
        """把已发邮件回写到发件箱的 Sent 文件夹（网页端可见）。失败不影响发送结果。"""
        try:
            with imaplib.IMAP4_SSL(self.host) as client:
                client.login(self.user, self.password)
                sent_folder = "Sent"
                status, folders = client.list()
                if status == "OK":
                    for entry in folders or []:
                        line = entry.decode("utf-8", "ignore") if isinstance(entry, bytes) else str(entry)
                        if "Sent" in line.split("/")[-1].strip('" ') or line.rstrip().endswith("Sent"):
                            parts = line.rsplit(" ", 1)
                            if len(parts) == 2:
                                sent_folder = parts[1].strip('"')
                            break
                status, _ = client.select(sent_folder)
                if status != "OK":
                    client.create(sent_folder)
                    client.select(sent_folder)
                client.append(sent_folder, "\\Seen", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        except Exception:  # noqa: BLE001 副本回写失败只记调试，不阻塞发信
            LOGGER.debug("Sent 副本回写失败：%s", self.user, exc_info=True)


def channel_for_commission(commission) -> "ImapSmtpChannel | None":  # noqa: ANN001
    """委托配置了自有邮箱时返回专属渠道，否则 None（用全局渠道）。"""
    if commission is None:
        return None
    if not commission.mailbox_email or not commission.mailbox_password:
        return None
    if commission.mailbox_email == settings.imap_user:
        return None
    return ImapSmtpChannel(
        user=commission.mailbox_email,
        password=commission.mailbox_password,
        from_addr=commission.mailbox_email,
    )


def inbound_channels(session) -> "list[ImapSmtpChannel]":  # noqa: ANN001
    """全局收件箱 + 所有配置了自有邮箱的委托收件箱。"""
    from ..models import Commission  # 局部导入避免循环依赖

    channels = [ImapSmtpChannel()]
    seen = {settings.imap_user}
    for commission in session.scalars(
        select(Commission).where(Commission.mailbox_email.is_not(None))
    ):
        if commission.mailbox_email in seen or not commission.mailbox_password:
            continue
        seen.add(commission.mailbox_email)
        channels.append(
            ImapSmtpChannel(user=commission.mailbox_email, password=commission.mailbox_password)
        )
    return channels
