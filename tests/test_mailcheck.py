from autoemail.config import settings
from autoemail.mailcheck import check_imap, check_smtp


def test_check_reports_missing_config(monkeypatch):
    monkeypatch.setattr(settings, "imap_host", "")
    monkeypatch.setattr(settings, "smtp_host", "")

    imap = check_imap()
    smtp = check_smtp()

    assert imap == {"ok": False, "error": "IMAP 未配置"}
    assert smtp == {"ok": False, "error": "SMTP 未配置"}


def test_check_uses_configured_credentials(monkeypatch):
    monkeypatch.setattr(settings, "imap_host", "imap.example.com")
    monkeypatch.setattr(settings, "imap_user", "bot@example.com")
    monkeypatch.setattr(settings, "imap_password", "secret")

    # 不发起真实网络请求：把 IMAP4_SSL 替换成必然失败的桩
    import autoemail.mailcheck as module

    class Failing:
        def __init__(self, host):
            raise OSError("connection refused")

    monkeypatch.setattr(module.imaplib, "IMAP4_SSL", Failing)
    result = check_imap()

    assert result["ok"] is False
    assert "connection refused" in result["error"]
