from datetime import date
from datetime import date
from decimal import Decimal

from autoemail.channels.base import NormalizedMessage
from autoemail.intent import classify_message, extract_payment_info
from autoemail.channels.base import NormalizedAttachment


def message(body: str, subject: str = "Invoice 1042", attachments=None):
    return NormalizedMessage(
        channel="eml",
        channel_msg_id="m1",
        from_addr="toni@a1surety.com",
        subject=subject,
        body_text=body,
        attachments=attachments or [],
        dedupe_hash="intent-hash",
    )


def test_extracts_invoice_payment_details():
    result = extract_payment_info(
        message(
            "Amount due: $750.00\nDue date: 12/01/2026\nPayment method: Zelle to billing@a1surety.com"
        )
    )
    assert result is not None
    assert result.amount == Decimal("750.00")
    assert result.currency == "USD"
    assert result.due_date == date(2026, 12, 1)
    assert result.payment_method == "zelle"
    assert result.confidence >= 0.9


def test_invoice_attachment_is_payment_signal():
    attachment = NormalizedAttachment(
        filename="invoice-100.pdf", mime="application/pdf", content=b"pdf", sha256="abc", doc_type="invoice"
    )
    result = classify_message(message("Please find attached.", attachments=[attachment]))
    assert result.label == "invoice"
    assert result.payment is not None
    assert result.confidence >= 0.6


def test_non_payment_email_is_not_invoice():
    result = classify_message(message("Yes, we can write the continuous bond for $750." , subject="Bond quote"))
    assert result.label == "quote_reply"
    assert result.payment is None


def test_unknown_payment_method_is_preserved():
    result = extract_payment_info(message("Invoice total: $600. Please remit payment."))
    assert result is not None
    assert result.payment_method == "unknown"
    assert result.amount == Decimal("600")


def _a(filename, doc_type):
    return NormalizedAttachment(
        filename=filename, mime="application/pdf", content=b"%PDF", sha256="x", doc_type=doc_type
    )


def test_bond_application_attachments_are_form_request_not_invoice():
    """代理商发来申请表格（POA/CBP5106/Bond Application），签名含 'accept payment via'，
    应识别为 form_request 而非 invoice。"""
    body = (
        "Our annual bond fee is $650 per year.\n"
        "We will need the following when getting a new POA:\n"
        "1. POA signed with a pen\n"
        "2. CBP form 5106 completed (skip section 3)\n"
        "3. Photo ID of signor\n"
        "4. Business Registration Document\n\n"
        "We accept payment via PayCargo at https://app.paycargo.com/..."
    )
    result = classify_message(
        message(
            body,
            subject="RE: [BOND-2026-0252] Inquiry - Continuous Customs Bond",
            attachments=[
                _a("ECC - Power of Attorney + Guide (EN) 2026.pdf", "poa"),
                _a("ECC - CBP Form 5106 + Guide (EN) 2026.pdf", "application"),
                _a("ECC - Continuous Bond Application + Guide (EN) 2026.pdf", "application"),
            ],
        )
    )
    assert result.label == "form_request"
    assert result.payment is None


def test_blank_forms_with_request_body_are_form_request_not_bond_delivered():
    """Acora 案例（BOND-2026-0192）回归：代理回复附空白表格（附件名含
    "Customs Bond Application"，历史 doc_type 误存为 bond），正文列明要我们
    填写的材料清单——必须判 form_request，绝不能判 bond_delivered。"""
    body = (
        "Thank you for reaching out about this bond. We so appreciate the opportunity "
        "to work with you.\n\n"
        "In order to proceed, the forms and other information we would need are:\n"
        "- Customs Bond application thoroughly completed and signed.\n"
        "- Breakdown of ownership percentage for the company\n"
        "- Personal Financial Statement for all owners and spouses (if married)\n"
        "- Current business financial statement (balance sheet and income statement)\n"
        "- Please confirm if the goods being imported are subject to any "
        "anti-dumping and/or countervailing duties."
    )
    result = classify_message(
        message(
            body,
            subject="RE: [BOND-2026-0192] Inquiry - Continuous Customs Bond",
            attachments=[
                _a("CA Shea.Customs Bond Application.pdf", "bond"),
                _a("Personal Financial Form.xlsx", "application"),
            ],
        )
    )
    assert result.label == "form_request"


def test_form_request_extracts_material_checklist():
    """form_request 应识别出代理商要求的具体材料清单（申请表/财务/股权/AD-CVD/个人财务）。"""
    body = (
        "In order to proceed, the forms and other information we would need are:\n"
        "- Customs Bond application thoroughly completed and signed.\n"
        "- Breakdown of ownership percentage for the company\n"
        "- Personal Financial Statement for all owners and spouses\n"
        "- Current business financial statement (balance sheet and income statement)\n"
        "- Please confirm if the goods are subject to any anti-dumping and/or countervailing duties."
    )
    result = classify_message(
        message(
            body,
            subject="RE: [BOND-2026-0192] Inquiry - Continuous Customs Bond",
            attachments=[_a("CA Shea.Customs Bond Application.pdf", "bond")],
        )
    )
    assert result.label == "form_request"
    materials = result.slots.get("materials", [])
    assert "application" in materials
    assert "personal_financial" in materials
    assert "financial_statement" in materials
    assert "ownership" in materials
    assert "ad_cvd" in materials


def test_complete_and_return_attached_application_is_form_request():
    """BondExchange 案例（BOND-2026-0646）回归：祈使句 "Complete and return the
    attached application"（无 please）+ 附件名含 bond，必须判 form_request 而非
    bond_delivered。"""
    body = (
        "Thank you for your request for a $50k Continuous Customs Import Bond. "
        "To proceed with your quote please provide the following information:\n"
        "Complete and return the attached application\n"
        "2026 income statement and balance sheet\n"
        "Advise on ownership breakdown if more than one\n"
        "Are the goods imported by the principal subject to antidumping or countervailing duties?"
    )
    result = classify_message(
        message(
            body,
            subject="Re: [BOND-2026-0646] Inquiry - Continuous Customs Bond",
            attachments=[_a("Bond Application.pdf", "bond")],
        )
    )
    assert result.label == "form_request"
    materials = result.slots.get("materials", [])
    assert "application" in materials
    assert "financial_statement" in materials
    assert "ownership" in materials
    assert "ad_cvd" in materials


def test_bond_attachment_without_executed_signal_not_bond_delivered():
    """附件 doc_type=bond 但正文无「已签发/交付」信号 → 不判 bond_delivered。

    这是误判根因修复：空白申请表文件名常含 bond（如 "Customs Bond.pdf"），
    只有正文明确表示 executed/issued bond 才算「Bond 已送达」。
    """
    result = classify_message(
        message(
            "Please see the attached document.",
            subject="Re: [BOND-2026-0646] bond application",
            attachments=[_a("Continuous Bond.pdf", "bond")],
        )
    )
    assert result.label != "bond_delivered"


def test_delivered_bond_without_request_body_still_bond_delivered():
    """真正交付 Bond 的邮件（无填表要求正文）仍应判 bond_delivered。"""
    result = classify_message(
        message(
            "Please find attached the executed continuous bond for your records.",
            subject="Bond copy for Sunrise Home Goods",
            attachments=[_a("Continuous_Bond_0427.pdf", "bond")],
        )
    )
    assert result.label == "bond_delivered"


def test_need_attached_application_is_form_request_even_with_bond_attachment():
    """Surety One 案例（BOND-2026-0214）回归：正文 "we will need the attached
    application"（含重复 will need 的笔误）+ 空白 bond.pdf 附件——必须判
    form_request，不能因为附件名含 bond 就判 bond_delivered。"""
    body = (
        "Thank you for your interest in our bonding company's support of this "
        "need, Chanrathana.\n\n"
        "In order to quote you we will need will need the attached application "
        "and the items at the bottom of page three. ALL required items listed "
        "on the application must be submitted or the application will be declined."
    )
    result = classify_message(
        message(
            body,
            subject="Re: [BOND-2026-0214] Inquiry - Continuous Customs Bond",
            attachments=[
                _a("bond.pdf", "bond"),
                _a("cbp5106.pdf", "application"),
                _a("poa.pdf", "poa"),
            ],
        )
    )
    assert result.label == "form_request"


def test_accept_payment_via_is_not_payment_signal():
    """签名里的付款方式说明不应触发发票判定。"""
    result = extract_payment_info(
        message(
            "Our annual bond fee is $650 per year.\n\nWe accept payment via PayCargo.",
            subject="RE: Inquiry",
        )
    )
    assert result is None


def test_signature_paycargo_link_is_not_invoice():
    """BOND-2026-1737 回归：代理反问"你从哪拿到我们的信息"，签名里有
    "To make payments click below: PayCargo 链接"——正文无任何收款动作，
    绝不能判成 invoice 并伪造一张 $5000 发票。"""
    body = (
        "Hello\n\n"
        "Please let us know where you obtained our information\n\n"
        "Thank You\n\n"
        "Gerald R.\n"
        "J.J. BOLL ASSOCIATES, INC.\n"
        "To make payments click below:\n"
        "https://shared.outlook.inky.com/link?domain=paycargo\n"
        "** J.J. BOLL ASSOCIATES A WELL-ESTABLISHED C-TPAT CERTIFIED COMPANY **"
    )
    result = extract_payment_info(message(body, subject="Re: [BOND-2026-1737] Inquiry"))
    assert result is None
    assert classify_message(message(body, subject="Re: [BOND-2026-1737] Inquiry")).label == "unclear"


def test_advisory_pay_your_invoice_by_due_date_is_not_invoice():
    """BOND-2026-1280 回归：代理报价信（$100 服务费 + $600 连续保证金），
    页脚 ADVISORIES 里有 "Pay your Invoice by the Due Date" 条款——
    这是报价，不是发票。"""
    body = (
        "Hello Cynthia,\n\n"
        "Our service for applying for a continuous bond application: $100.00 (non-refundable).\n"
        "Continuous Bond: $600 for one year, $900 for two years.\n\n"
        "ADVISORIES:\n"
        "- Pay your Invoice by the Due Date: It is very important that you pay our "
        "invoices by the due date. Unless otherwise explicitly agreed in writing...\n"
        "- Checks as a Payment Method: Starting June 01, 2022, CBS will no longer "
        "accept checks as a payment method.\n"
    )
    result = extract_payment_info(message(body, subject="Re: [BOND-2026-1280] Inquiry"))
    assert result is None
    assert classify_message(message(body, subject="Re: [BOND-2026-1280] Inquiry")).label == "quote_reply"


def test_wire_fraud_warning_in_signature_is_not_invoice():
    """BOND-2026-0752 回归：代理要求补充商品信息，签名防诈骗提示里有
    "do not send wire/ACH to any account number other than what we have
    listed directly on our invoice ... sending payment"——不是发票。"""
    body = (
        "Dear Amanuel,\n\n"
        "Please advise the commodity you are considering importing.\n"
        "Does the USA Importer have any USA employees or a USA presence?\n\n"
        "Sincerely,\n"
        "Bill Marston\n"
        "-Please do not send wire/ ACH to any account number other than what we have "
        "listed directly on our invoice. We will never send an email asking to send "
        "a wire to a new account. Please call to verify when sending payment-"
    )
    result = extract_payment_info(message(body, subject="RE: [BOND-2026-0752] Inquiry"))
    assert result is None
    assert classify_message(message(body, subject="RE: [BOND-2026-0752] Inquiry")).label == "unclear"


def test_premium_quote_with_payment_expectation_is_quote_reply():
    """BOND-2026-0713 回归：代理报价 "$50,000 bond premium is $500 + $75 filing
    fee + $250 setup fee ... We expect payment before filing with USCBP"——
    "we expect payment" 是付款预期不是收款动作，这是报价信。"""
    body = (
        "Good Morning,\n\n"
        "Thank you for your interest in our company. Just to be clear, we are a "
        "Customs Broker & a Freight Forwarding Company but we can assist you in "
        "obtaining your continuous bond. Before we can proceed,\n"
        "Can you please send us the following information for verification purposes: "
        "Articles of Formation, IRS EIN letter (CP575), W-9, principal ID, and the "
        "list of officers in your organization. To provide this service, we require "
        "a Power of Attorney authorizing us to file for your bond. The premium for a "
        "$50,000.00 bond is $500.00, plus a $75.00 filing fee and a $250.00 new "
        "customer setup fee. Approval from the Surety Company will probably take "
        "7-10 days once we submit the application. Once approved, we will file the "
        "bond with USCBP, usually within 24 hrs. We expect payment before filing "
        "with USCBP. Let us know if you have any questions.\n"
    )
    result = extract_payment_info(message(body, subject="Re: [BOND-2026-0713] Inquiry"))
    assert result is None
    assert classify_message(message(body, subject="Re: [BOND-2026-0713] Inquiry")).label in (
        "quote_reply",
        "form_request",
    )


def test_real_invoice_body_still_detected():
    """真发票正文（amount due / remit）仍必须识别为 invoice，不因收紧规则而漏判。"""
    result = extract_payment_info(
        message(
            "Please find attached our invoice.\nAmount due: $750.00\n"
            "Due date: 12/01/2026\nPlease remit payment via Zelle."
        )
    )
    assert result is not None
    assert result.amount == Decimal("750.00")


def test_total_due_with_amount_is_invoice():
    """极简发票正文只有 "Total due: $x" 一句也能兜底命中。"""
    result = extract_payment_info(message("Total due: $600.00", subject="Invoice"))
    assert result is not None
    assert result.amount == Decimal("600.00")
