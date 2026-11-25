"""客户签名图样链路测试：入库留痕、图样清理、坐标盖章、填表集成。

合规前提：签名素材一律视为客户本人提供（save 强制授权依据），系统不生成签名。
"""

import io
from pathlib import Path

import pytest

from autoemail import formfill
from autoemail.config import settings
from autoemail.formfill import _fill_core
from autoemail.models import AuditEvent, Commission
from autoemail.signature import (
    clean_signature_png,
    get_signature_asset,
    locate_signature_rects,
    save_signature_asset,
    stamp_signature,
)

FIXTURES = Path(__file__).parent / "fixtures"
AGENT_BLANK = FIXTURES / "bond_application_blank.pdf"


def _signature_png() -> bytes:
    """用 Pillow 画一个白底黑字签名样的测试图（替代真实客户素材）。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (600, 200), "white")
    draw = ImageDraw.Draw(img)
    draw.line([(40, 150), (200, 60), (360, 140), (560, 70)], fill="black", width=6)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---- 入库与授权留痕 ----

def test_save_signature_requires_authorization_note(session, world):
    with pytest.raises(ValueError, match="授权依据"):
        save_signature_asset(
            session,
            commission=world["commission"],
            filename="sig.png",
            mime="image/png",
            content=_signature_png(),
            authorization_note="   ",
        )


def test_save_signature_rejects_non_image(session, world):
    with pytest.raises(ValueError, match="图片格式"):
        save_signature_asset(
            session,
            commission=world["commission"],
            filename="sig.pdf",
            mime="application/pdf",
            content=b"%PDF-1.4",
            authorization_note="客户微信授权",
        )


def test_save_signature_upserts_with_audit(session, world):
    first = save_signature_asset(
        session,
        commission=world["commission"],
        filename="v1.png",
        mime="image/png",
        content=_signature_png(),
        authorization_note="客户 11-23 微信：同意用于本次申请",
    )
    second = save_signature_asset(
        session,
        commission=world["commission"],
        filename="v2.png",
        mime="image/png",
        content=_signature_png(),
        authorization_note="客户 11-24 邮件确认",
    )
    assert second.id == first.id
    assert second.filename == "v2.png"
    assert get_signature_asset(session, world["commission"].id).filename == "v2.png"
    events = [
        e
        for e in session.query(AuditEvent).all()
        if e.action == "commission.signature_uploaded"
    ]
    assert len(events) == 2
    assert "授权依据" in events[-1].reasons[0]


# ---- 图样清理 ----

def test_clean_signature_makes_white_transparent_and_trims():
    cleaned = clean_signature_png(_signature_png())
    from PIL import Image

    img = Image.open(io.BytesIO(cleaned))
    assert img.mode == "RGBA"
    # 透明背景：四角 alpha 为 0
    for xy in ((0, 0), (img.width - 1, 0), (0, img.height - 1), (img.width - 1, img.height - 1)):
        assert img.getpixel(xy)[3] == 0
    # 裁剪后尺寸小于原图（600x200 → 有内容 bbox）
    assert img.width < 600


# ---- 坐标定位与盖章 ----

def test_locate_signature_rects_on_bond_form():
    hits = locate_signature_rects(AGENT_BLANK.read_bytes(), ["Print Name and Title", "不存在域"])
    names = {name for name, _, _ in hits}
    assert "Print Name and Title" in names
    assert "不存在域" not in names


def test_stamp_signature_overlays_image():
    stamped, missing = stamp_signature(
        AGENT_BLANK.read_bytes(), _signature_png(), ["Print Name and Title"]
    )
    assert missing == []
    # 盖章后页面应包含图片 XObject
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(stamped))
    has_image = any(
        "/XObject" in (page.get("/Resources") or {})
        for page in reader.pages
    )
    assert has_image


def test_stamp_signature_missing_field_reported():
    stamped, missing = stamp_signature(AGENT_BLANK.read_bytes(), _signature_png(), ["不存在域"])
    assert missing == ["不存在域"]


# ---- 填表集成：签名域单独报告 ----

def test_fill_core_with_signature_field(session, world, monkeypatch, tmp_path):
    monkeypatch.setattr(
        formfill,
        "load_mapping",
        lambda format_id, path=None: {
            "Company Name": "legal_name",
            "Print Name and Title": {"signature": True},
        },
    )
    save_signature_asset(
        session,
        commission=world["commission"],
        filename="sig.png",
        mime="image/png",
        content=_signature_png(),
        authorization_note="客户微信授权",
    )
    data = {"legal_name": "Sunrise Home Goods LLC"}
    out = tmp_path / "filled.pdf"
    report = _fill_core(
        AGENT_BLANK.read_bytes(), "customs_bond_v1", data, str(out),
        signature_png=_signature_png(),
    )
    assert report["signature_fields"]["Print Name and Title"] == "stamped"
    assert out.exists()
    assert report["filled"] == ["Company Name"]
    assert "Print Name and Title" not in report["skipped"]


def test_fill_core_without_signature_asset_leaves_blank(session, world, monkeypatch, tmp_path):
    monkeypatch.setattr(
        formfill,
        "load_mapping",
        lambda format_id, path=None: {
            "Company Name": "legal_name",
            "Print Name and Title": {"signature": True},
        },
    )
    data = {"legal_name": "Sunrise Home Goods LLC"}
    out = tmp_path / "filled.pdf"
    report = _fill_core(
        AGENT_BLANK.read_bytes(), "customs_bond_v1", data, str(out), signature_png=None
    )
    assert report["signature_fields"]["Print Name and Title"] == "客户签名图样未提供，留空待客户签署"
    assert report["skipped"]["Print Name and Title"] == "需要客户本人签名"
    assert out.exists()


# ---- 已填表格补签（时序补救：先填表后传签名） ----

def test_stamp_signature_on_existing_stamps(monkeypatch, tmp_path):
    from autoemail import signature as signature_mod

    monkeypatch.setattr(settings, "filled_forms_dir", str(tmp_path))
    (tmp_path / "Acme LLC_bond.pdf").write_bytes(b"%PDF-1.4 old")
    monkeypatch.setattr(formfill, "_load_signature_png_by_name", lambda name: b"png")
    monkeypatch.setattr(
        formfill, "load_signature_anchor",
        lambda fmt: {"field": "bond_print_name", "gap": 5, "height": 24},
    )
    monkeypatch.setattr(
        signature_mod, "stamp_signature_at_anchor",
        lambda pdf, png, field, gap, height: (b"%PDF-1.4 stamped", True),
    )
    r = formfill.stamp_signature_on_existing("Acme LLC", "bond")
    assert r["stamped"] is True
    assert (tmp_path / "Acme LLC_bond.pdf").read_bytes() == b"%PDF-1.4 stamped"


def test_stamp_signature_on_existing_without_signature(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "filled_forms_dir", str(tmp_path))
    (tmp_path / "Acme LLC_bond.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(formfill, "_load_signature_png_by_name", lambda name: None)
    r = formfill.stamp_signature_on_existing("Acme LLC", "bond")
    assert r["stamped"] is False
    assert "签名图样" in r["reason"]


def test_stamp_signature_on_existing_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "filled_forms_dir", str(tmp_path))
    monkeypatch.setattr(formfill, "_load_signature_png_by_name", lambda name: b"png")
    r = formfill.stamp_signature_on_existing("Acme LLC", "bond")
    assert r["stamped"] is False
    assert r["reason"] == "文件不存在"


def test_stamp_signature_on_existing_unknown_short(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "filled_forms_dir", str(tmp_path))
    r = formfill.stamp_signature_on_existing("Acme LLC", "bogus")
    assert r["stamped"] is False
    assert "未知短名" in r["reason"]
