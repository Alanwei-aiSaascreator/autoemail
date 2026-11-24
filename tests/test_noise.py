from types import SimpleNamespace

from autoemail.intent import classify_message
from autoemail.noise import clean_body, recent_history
from helpers import make_message


def test_strips_english_quoted_reply():
    text = (
        "Hi Toni,\n\nPlease send the invoice and payment instructions.\n\n"
        "On Fri, Nov 6, 2026 at 10:56 PM Brittany <brittany@tavrigen.com> wrote:\n"
        "> Hi Toni,\n> sorry I missed your call.\n"
    )

    cleaned = clean_body(text)

    assert "Please send the invoice" in cleaned
    assert "wrote:" not in cleaned
    assert "sorry I missed" not in cleaned


def test_strips_chinese_quoted_reply():
    text = "请确认报价。\n\n在 2026年11月6日 10:56，Brittany 写道：\n> 之前的报价是 750 美元"

    cleaned = clean_body(text)

    assert "请确认报价" in cleaned
    assert "之前的报价" not in cleaned


def test_strips_signature_block():
    text = "The quoted premium is $750.\n\nBest regards,\nBrittany\n-- \nBrittany Nicole Barnes\nTavrigen LLC"

    cleaned = clean_body(text)

    assert "quoted premium is $750" in cleaned
    assert "Tavrigen LLC" not in cleaned


def test_strips_confidentiality_notice():
    text = "Goods are not subject to AD/CVD orders.\n\nThis email and any attachments are confidential."

    cleaned = clean_body(text)

    assert "not subject to AD/CVD" in cleaned
    assert "confidential" not in cleaned


def test_recent_history_respects_total_budget():
    messages = [
        SimpleNamespace(direction="in", subject="s", body_text="x" * 100)
        for _ in range(5)
    ]

    history = recent_history(messages, per_message=1000, total=250)

    assert len(history) == 2
    assert history[-1]["body"] == "x" * 100


def test_strips_qq_html_quoted_block():
    # QQ 客户端回复会把历史原文用 HTML 实体编码，且引用头是「原始邮件/发件人：」格式
    body = (
        "Thanks for your information! there is the bond document.\r\n\r\n"
        "         原始邮件\r\n         \r\n       \r\n"
        "发件人：心有些暖 <2656277828@qq.com&gt;\r\n"
        "发件时间：2026年11月18日 18:22\r\n"
        "收件人：contact <contact@weaveorasocial.com&gt;\r\n"
        "主题：Re: [BOND-2026-0143] Application documents\r\n\r\n"
        "知道了\r\n\r\n"
        "---Original---\r\n"
        "From:&nbsp;\"contact\"<contact@weaveorasocial.com&gt;\r\n"
        "-&nbsp;Commercial&nbsp;Invoice\r\n"
        "Best&nbsp;regards,\r\nRuiguang&nbsp;Chen"
    )

    cleaned = clean_body(body)

    assert "there is the bond document" in cleaned
    assert "Commercial" not in cleaned
    assert "发件人" not in cleaned


def test_quoted_invoice_does_not_drive_intent():
    body = (
        "Thanks, received.\n\n"
        "On Fri, Nov 6, 2026 at 10:56 PM Toni <toni@a1surety.com> wrote:\n"
        "> Invoice 1042 amount $750 due 2026-12-01, please wire transfer\n"
    )

    result = classify_message(make_message(subject="Re: bond", body=body))

    assert result.label != "invoice"


def test_marketing_payment_email_is_not_invoice():
    body = (
        "When you paid at EezyImport, you saved your payment details with Link. "
        "Link is a digital wallet that safely stores your payment details."
    )

    result = classify_message(
        make_message(subject="All your recurring payments, finally in one place", body=body)
    )

    assert result.label != "invoice"


def test_zero_amount_does_not_trigger_invoice():
    body = "Meet Link, the smarter wallet you used at EezyImport. Save $0 on your next payment."

    result = classify_message(make_message(subject="Meet Link", body=body))

    assert result.label != "invoice"
