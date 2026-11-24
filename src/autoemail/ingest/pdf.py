"""PDF 文本提取：只读文本层，扫描件或损坏文件返回空串。

发票金额经常只出现在 PDF 附件里，正文只写 "please find attached"。
这里不做 OCR；拿不到文本就交给人工，不猜。
"""

import io

MAX_PAGES = 5


def extract_pdf_text(content: bytes, *, max_pages: int = MAX_PAGES) -> str:
    if not content:
        return ""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
    except Exception:
        return ""

    chunks: list[str] = []
    for page in reader.pages[:max_pages]:
        try:
            chunks.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(chunk.strip() for chunk in chunks if chunk.strip())


def extract_invoice_pdf_text(attachments) -> str:  # noqa: ANN001
    """只提取发票类 PDF，避免把 Bond 文件、证件里的数字当成金额。"""
    chunks: list[str] = []
    for item in attachments:
        mime = (item.mime or "").lower()
        filename = (getattr(item, "filename", "") or "").lower()
        if mime != "application/pdf" and not filename.endswith(".pdf"):
            continue
        if item.doc_type != "invoice" and "invoice" not in filename:
            continue
        text = extract_pdf_text(item.content)
        if text:
            chunks.append(text)
    return "\n".join(chunks)
