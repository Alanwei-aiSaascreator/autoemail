"""邮箱连接自检：只登录，不收取、不发送。"""
import imaplib
import smtplib

from .config import settings


def check_imap() -> dict:
    if not (settings.imap_host and settings.imap_user and settings.imap_password):
        return {"ok": False, "error": "IMAP 未配置"}
    try:
        with imaplib.IMAP4_SSL(settings.imap_host) as client:
            client.login(settings.imap_user, settings.imap_password)
            status, data = client.select("INBOX", readonly=True)
            total = int(data[0]) if status == "OK" and data and data[0] else None
            unseen = None
            status, found = client.search(None, "UNSEEN")
            if status == "OK" and found and found[0]:
                unseen = len(found[0].split())
        return {
            "ok": True,
            "host": settings.imap_host,
            "user": settings.imap_user,
            "inbox_messages": total,
            "unseen": unseen,
        }
    except (imaplib.IMAP4.error, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def check_smtp() -> dict:
    if not (settings.smtp_host and settings.smtp_user and settings.smtp_password):
        return {"ok": False, "error": "SMTP 未配置"}
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as client:
            client.ehlo()
            client.starttls()
            client.ehlo()
            client.login(settings.smtp_user, settings.smtp_password)
        return {"ok": True, "host": settings.smtp_host, "port": settings.smtp_port, "user": settings.smtp_user}
    except (smtplib.SMTPException, OSError) as exc:
        return {"ok": False, "error": str(exc)}
