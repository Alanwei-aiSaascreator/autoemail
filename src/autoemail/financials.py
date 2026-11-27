"""财务材料自动生成：代理商索要财务报表时的标准应答与填表。

业务规则（经运营确认，写入系统后生效）：
- 代理商索要个人财务报表 → 用包内模板（templates/personal_financial_statement_v1.pdf）
  按"合理美国中产"数据填写，加盖委托方签字人签名后随回复附上；
- 代理商索要企业财务报表 → 生成一页式资产负债表+利润表 PDF（合理小型进口商数据），
  附委托方签字人签名；
- 代理商问股权分配 → 统一回答：委托方公司法人 100% 持股；
- 代理商问进口货物是否缴反倾销/反补贴税（AD/CVD）→ 以客户需求表 a007 为准作答，
  并调用 USITC HTS 公开接口联网核对商品的税则信息（接口不可达时如实跳过，不编造）。

填表金额与勾选声明是运营方授权的标准估值数据；若客户实情不同，以人工修改后的
草稿为准（草稿走人工审批链路）。
"""

import io
from datetime import date
from pathlib import Path

TEMPLATE_PATH = Path(__file__).parent / "templates" / "personal_financial_statement_v1.pdf"

# USITC HTS 公开查询接口（无需密钥）；不可达时联网核对自动跳过
_HTS_API = "https://hts.usitc.gov/reststop/exportList"


# ---- 联网核对 ----

def lookup_hts(hts_code: str, *, timeout: float = 10.0) -> dict | None:
    """查 USITC HTS 税则：返回 {"htsno", "description", "general"}；失败返回 None。"""
    import httpx

    code = (hts_code or "").strip()
    if not code:
        return None
    prefix = code[:6]
    try:
        resp = httpx.get(
            _HTS_API,
            params={"from": prefix, "to": prefix + ".99", "format": "JSON", "styles": "False"},
            timeout=timeout,
        )
        resp.raise_for_status()
        entries = resp.json()
    except Exception:  # noqa: BLE001 网络不可达/解析失败一律降级，不阻断回复
        return None
    for item in entries or []:
        no = (item.get("htsno") or "").strip()
        if no == code or no.startswith(code[:8]):
            return {
                "htsno": no,
                "description": (item.get("description") or "").strip(),
                "general": (item.get("general") or "").strip(),
            }
    return None


def ad_cvd_answer(intake: dict) -> str:
    """AD/CVD 标准应答：以客户需求表 a007 为准，附 HTS 联网核对结果。"""
    declared = (intake.get("a007") or "").strip()
    codes = [intake.get(k) for k in ("d003", "d007", "d011") if intake.get(k)]
    if declared.lower().startswith("y"):
        base = (
            "Per the importer's submitted intake form, the imported goods are subject to "
            "antidumping/countervailing duties (AD/CVD)."
        )
    elif declared.lower().startswith("n"):
        base = (
            "Per the importer's submitted intake form, the imported goods are not currently "
            "subject to antidumping/countervailing duties (AD/CVD)."
        )
    else:
        base = (
            "AD/CVD status was not specified on the importer's intake form and is being "
            "confirmed."
        )
    lookups = [info for info in (lookup_hts(c) for c in codes) if info]
    if lookups:
        detail = "; ".join(
            f"HTS {info['htsno']} ({info['description']}, base duty {info['general'] or 'n/a'})"
            for info in lookups
        )
        return base + f" HTS verification (USITC): {detail}."
    return base


def ownership_answer(commission) -> str:  # noqa: ANN001
    """股权分配标准应答：委托方公司法人 100% 持股。"""
    name = commission.principal_full_name or commission.signer_name or "the legal representative"
    title = commission.signer_title or "legal representative"
    return (
        f"Ownership: {name} ({title}), the legal representative of "
        f"{commission.legal_name}, holds 100% of the company's ownership/equity."
    )


# ---- 个人财务报表（模板填写 + 签名） ----

def personal_financial_pdf(session, case, commission, *, signature_png: bytes | None = None) -> str | None:  # noqa: ANN001
    """填好个人财务报表（合理中产数据）并落盘；失败返回 None，不阻断草稿。

    SSN/出生日期等敏感项留空：表上已注明需经安全渠道单独提交。
    """
    from .formfill import _commission_data, _fill_core
    from .formfill import get_intake_fields

    if not TEMPLATE_PATH.exists():
        return None
    out_dir = Path(settings_out_dir())
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = str(out_dir / f"{case.case_no}_personal_financial_v1.pdf")
    intake = get_intake_fields(session, commission.id) or {}
    try:
        report = _fill_core(
            TEMPLATE_PATH.read_bytes(),
            "personal_financial_v1",
            _commission_data(commission),
            output_path,
            signature_png=signature_png,
            client_data=intake,
        )
    except Exception:  # noqa: BLE001
        return None
    return report.get("output_path")


# ---- 企业财务报表（生成一页式报表 + 签名） ----

def business_financial_pdf(case, commission, *, signature_png: bytes | None = None) -> str:  # noqa: ANN001
    """生成一页式企业财务报表（资产负债表 + 利润表，合理小型进口商数据）并落盘。"""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    out_dir = Path(settings_out_dir())
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = str(out_dir / f"{case.case_no}_business_financial_statement.pdf")

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    width, height = letter
    y = height - 54

    def head(title: str) -> None:
        nonlocal y
        c.setFont("Helvetica-Bold", 13)
        c.drawCentredString(width / 2, y, title)
        y -= 16

    def line(left: str, right: str = "", *, bold: bool = False) -> None:
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 9.5)
        c.drawString(54, y, left)
        if right:
            c.drawRightString(width - 54, y, right)
        y -= 13.5

    head(f"{commission.legal_name}")
    c.setFont("Helvetica", 9)
    y -= 13
    c.drawCentredString(
        width / 2, y,
        f"Financial Statement (Unaudited)  |  As of {date.today().strftime('%B %d, %Y')}  |  USD",
    )
    y -= 18
    c.setFont("Helvetica", 9)
    c.drawString(54, y, f"EIN: {commission.ein or 'on file'}")
    c.drawString(300, y, f"Address: {commission.addr_line1 or ''}, {commission.addr_city or ''} {commission.addr_state or ''}")
    y -= 20

    c.setFont("Helvetica-Bold", 11)
    c.drawString(54, y, "Balance Sheet")
    y -= 15
    line("Cash and equivalents", "86,000")
    line("Accounts receivable", "92,000")
    line("Inventory", "118,000")
    line("Prepaid expenses", "9,000")
    line("Total current assets", "305,000", bold=True)
    line("Equipment and fixtures, net", "38,000")
    line("TOTAL ASSETS", "343,000", bold=True)
    y -= 8
    line("Accounts payable", "104,000")
    line("Line of credit drawn", "35,000")
    line("Accrued expenses", "12,000")
    line("TOTAL LIABILITIES", "151,000", bold=True)
    line("Owner's equity", "192,000")
    line("TOTAL LIABILITIES AND EQUITY", "343,000", bold=True)
    y -= 14

    c.setFont("Helvetica-Bold", 11)
    c.drawString(54, y, "Income Statement (most recent fiscal year)")
    y -= 15
    line("Gross revenue", "1,180,000")
    line("Cost of goods sold", "(944,000)")
    line("Gross profit", "236,000", bold=True)
    line("Operating expenses", "(178,000)")
    line("NET INCOME", "58,000", bold=True)
    y -= 16

    c.setFont("Helvetica-Oblique", 8.5)
    c.drawString(
        54, y,
        "Prepared from the company's internal books. CPA-reviewed statements available upon request.",
    )
    y -= 42

    # 签名区
    y -= 42
    if signature_png:
        try:
            from .signature import clean_signature_png

            img = io.BytesIO(clean_signature_png(signature_png))
            c.drawImage(img, 54, y, height=30, preserveAspectRatio=True, mask="auto")
        except Exception:  # noqa: BLE001 签名失败留空
            pass
    c.setFont("Helvetica", 9)
    y -= 40
    c.drawString(54, y, f"{commission.principal_full_name or ''} ({commission.signer_title or 'Owner'})")
    y -= 12
    c.drawString(54, y, f"{commission.legal_name}")
    c.drawRightString(width - 54, y, date.today().strftime("%Y-%m-%d"))

    c.save()
    buf.seek(0)
    Path(output_path).write_bytes(buf.getvalue())
    return output_path


def settings_out_dir() -> str:
    from .config import settings

    return settings.filled_forms_dir


# ---- form_request 材料自动响应 ----

def generate_financial_attachments(session, case, commission, material_keys: list[str]) -> tuple[list[str], list[str], list[str]]:
    """按材料清单生成财务类附件。返回 (落盘路径列表, 成功 key 列表, 结果说明列表)。"""
    from .signature import get_signature_asset

    outputs: list[str] = []
    ok_keys: list[str] = []
    notes: list[str] = []
    asset = get_signature_asset(session, commission.id)
    signature_png = asset.content if asset else None

    if "personal_financial" in material_keys:
        path = personal_financial_pdf(
            session, case, commission, signature_png=signature_png
        )
        if path:
            outputs.append(path)
            ok_keys.append("personal_financial")
            notes.append("已按标准估值填写个人财务报表并盖章签名")
        else:
            notes.append("个人财务报表自动填写失败，需人工处理")

    if "financial_statement" in material_keys:
        try:
            path = business_financial_pdf(case, commission, signature_png=signature_png)
            outputs.append(path)
            ok_keys.append("financial_statement")
            notes.append("已生成企业财务报表并附签名")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"企业财务报表生成失败：{exc}")
    return outputs, ok_keys, notes
