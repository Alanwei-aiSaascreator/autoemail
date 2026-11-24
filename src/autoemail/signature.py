"""客户签名图样管理：客户本人提供的签名素材 + 授权留痕 + 插入表单署名处。

合规边界（硬性）：
- 只接受客户本人主动提供的签名图片，上传时必须填写授权依据（authorization_note）；
- 系统不做任何笔迹仿造、不代客户生成签名；
- 签名字段无素材时一律留空并报告，待客户提供或客户本人签署。

技术：Pillow 清理背景（白底转透明 + 裁剪），reportlab 生成覆盖页，pypdf 定位
签名域坐标并合并盖章。
"""

import hashlib
import io

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit
from .models import Commission, SignatureAsset

ALLOWED_MIME = {"image/png", "image/jpeg", "image/webp"}


def save_signature_asset(
    session: Session,
    *,
    commission: Commission,
    filename: str,
    mime: str | None,
    content: bytes,
    actor: str = "human",
    authorization_note: str,
) -> SignatureAsset:
    """保存客户提供的签名图样。同一委托重复上传覆盖旧图样。

    authorization_note 必填：授权依据（客户微信/邮件原话、日期、授权范围）。
    """
    if not content:
        raise ValueError("签名图片为空")
    if mime and mime.lower() not in ALLOWED_MIME:
        raise ValueError(f"仅支持图片格式 {sorted(ALLOWED_MIME)}，收到 {mime}")
    note = (authorization_note or "").strip()
    if not note:
        raise ValueError("必须填写授权依据（客户本人授权的原话/日期/范围），否则不能入库")

    existing = session.scalar(
        select(SignatureAsset).where(SignatureAsset.commission_id == commission.id)
    )
    if existing is None:
        asset = SignatureAsset(
            commission_id=commission.id,
            filename=filename,
            mime=mime,
            size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            content=content,
            authorization_note=note,
            uploaded_by=actor,
        )
        session.add(asset)
    else:
        asset = existing
        asset.filename = filename
        asset.mime = mime
        asset.size = len(content)
        asset.sha256 = hashlib.sha256(content).hexdigest()
        asset.content = content
        asset.authorization_note = note
        asset.uploaded_by = actor
    session.flush()
    audit.record(
        session,
        action="commission.signature_uploaded",
        actor=actor,
        decision="stored",
        reasons=[f"委托 #{commission.id} 客户签名图样：{filename}；授权依据：{note}"],
    )
    return asset


def get_signature_asset(session: Session, commission_id: int) -> SignatureAsset | None:
    return session.scalar(
        select(SignatureAsset)
        .where(SignatureAsset.commission_id == commission_id)
        .order_by(SignatureAsset.id.desc())
    )


def clean_signature_png(content: bytes) -> bytes:
    """清理签名图样：白底转透明、裁掉空白边，输出 PNG。"""
    from PIL import Image, ImageOps

    image = Image.open(io.BytesIO(content)).convert("RGBA")
    # 近白背景（含拍照纸底）转透明：按亮度做 alpha
    gray = image.convert("L")
    alpha = gray.point(lambda v: max(0, 255 - v) if v >= 200 else 255)
    image.putalpha(alpha)
    bbox = alpha.getbbox()
    if bbox:
        image = image.crop(bbox)
    if image.width > 1200 or image.height > 600:
        image = ImageOps.contain(image, (1200, 600))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def locate_signature_rects(pdf_bytes: bytes, field_names: list[str]) -> list[tuple[str, int, tuple]]:
    """在 PDF 各页注解中定位指定签名域，返回 (字段名, 页码, /Rect)。"""
    from pypdf import PdfReader
    from pypdf.generic import IndirectObject

    wanted = set(field_names)
    reader = PdfReader(io.BytesIO(pdf_bytes))
    hits: list[tuple[str, int, tuple]] = []
    for page_index, page in enumerate(reader.pages):
        for annot in page.get("/Annots") or []:
            if isinstance(annot, IndirectObject):
                annot = annot.get_object()
            name_parts: list[str] = []
            node = annot
            while node is not None:
                name = node.get("/T")
                if name:
                    name_parts.insert(0, str(name))
                node = node.get("/Parent")
                if isinstance(node, IndirectObject):
                    node = node.get_object()
            full_name = ".".join(name_parts)
            if full_name in wanted:
                rect = annot.get("/Rect")
                if rect:
                    hits.append((full_name, page_index, tuple(float(v) for v in rect)))
    return hits


def stamp_signature(pdf_bytes: bytes, png_bytes: bytes, field_names: list[str]) -> tuple[bytes, list[str]]:
    """把签名图样盖到指定域坐标处。返回 (新 PDF 字节, 未定位到坐标的域列表)。"""
    from pypdf import PdfReader, PdfWriter
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    hits = locate_signature_rects(pdf_bytes, field_names)
    if not hits:
        return pdf_bytes, list(field_names)

    missing = [f for f in field_names if f not in {name for name, _, _ in hits}]
    png = clean_signature_png(png_bytes)
    img = ImageReader(io.BytesIO(png))
    iw, ih = img.getSize()

    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter(clone_from=reader)
    by_page: dict[int, list[tuple[str, tuple]]] = {}
    for name, page_index, rect in hits:
        by_page.setdefault(page_index, []).append((name, rect))

    for page_index, entries in by_page.items():
        mediabox = writer.pages[page_index].mediabox
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=(float(mediabox.width), float(mediabox.height)))
        for _, rect in entries:
            x0, y0, x1, y1 = rect
            w, h = max(x1 - x0, 1), max(y1 - y0, 1)
            # 签名按高度撑满域，宽度等比，居中放置
            scale = min(w / iw, h / ih) * 0.95
            draw_w, draw_h = iw * scale, ih * scale
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            c.drawImage(img, cx - draw_w / 2, cy - draw_h / 2, draw_w, draw_h, mask="auto")
        c.save()
        buf.seek(0)
        overlay = PdfReader(buf).pages[0]
        writer.pages[page_index].merge_page(overlay)

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue(), missing


def stamp_signature_at_rects(
    pdf_bytes: bytes, png_bytes: bytes, rects: list[tuple[int, tuple]]
) -> bytes:
    """把签名图样盖到指定的 (页码, (x0, y0, x1, y1)) 坐标列表处，返回新 PDF 字节。

    用于签名区不是 acroform 字段、而是留白手写区的表格：坐标由外部定位后传入。
    """
    from pypdf import PdfReader, PdfWriter
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    png = clean_signature_png(png_bytes)
    img = ImageReader(io.BytesIO(png))
    iw, ih = img.getSize()

    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter(clone_from=reader)
    by_page: dict[int, list[tuple]] = {}
    for page_index, rect in rects:
        by_page.setdefault(page_index, []).append(rect)

    for page_index, entries in by_page.items():
        mediabox = writer.pages[page_index].mediabox
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=(float(mediabox.width), float(mediabox.height)))
        for rect in entries:
            x0, y0, x1, y1 = rect
            w, h = max(x1 - x0, 1), max(y1 - y0, 1)
            scale = min(w / iw, h / ih) * 0.95
            draw_w, draw_h = iw * scale, ih * scale
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            c.drawImage(img, cx - draw_w / 2, cy - draw_h / 2, draw_w, draw_h, mask="auto")
        c.save()
        buf.seek(0)
        overlay = PdfReader(buf).pages[0]
        writer.pages[page_index].merge_page(overlay)

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def stamp_signature_at_anchor(
    pdf_bytes: bytes,
    png_bytes: bytes,
    anchor_field: str,
    *,
    gap: float = 5.0,
    height: float = 24.0,
) -> tuple[bytes, bool]:
    """把签名图样盖到锚点字段上方的签名区。返回 (新 PDF 字节, 是否定位成功)。

    anchor_field 是"打印名"之类紧邻签名区的 acroform 字段；签名区取其正上方
    gap~gap+height 的矩形区域。
    """
    hits = locate_signature_rects(pdf_bytes, [anchor_field])
    if not hits:
        return pdf_bytes, False
    _, page_index, rect = hits[0]
    x0, y0, x1, y1 = rect
    target = (x0, y1 + gap, x1, y1 + gap + height)
    return stamp_signature_at_rects(pdf_bytes, png_bytes, [(page_index, target)]), True
