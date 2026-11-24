"""委托方文件管理：公司章程、税号文件、法人证件。

询价前必须三份齐全；缺一份就不发起询价。
"""

import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit
from .models import Commission, CommissionDocument

DOC_LABELS = {
    "articles": "公司章程",
    "ein": "税号文件",
    "id": "法人证件",
}
REQUIRED_DOC_TYPES = tuple(DOC_LABELS)


def missing_documents(session: Session, commission_id: int) -> list[str]:
    present = set(
        session.scalars(
            select(CommissionDocument.doc_type).where(
                CommissionDocument.commission_id == commission_id
            )
        ).all()
    )
    return [DOC_LABELS[doc_type] for doc_type in REQUIRED_DOC_TYPES if doc_type not in present]


def stage_supporting_documents(
    session: Session,
    case,
    commission: Commission,
    out_dir: str,
) -> list[str]:
    """把委托支持文件（公司章程、法人证件）的原始文件落盘，返回可发送路径。

    代理要求填表时通常还要附上带照片的身份证明（Photo ID）和公司注册文件，
    这些以原始文件发出（不读字段、不结构化），供邮件附件使用。
    """
    from pathlib import Path

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for doc_type in ("articles", "id"):
        doc = session.scalar(
            select(CommissionDocument).where(
                CommissionDocument.commission_id == commission.id,
                CommissionDocument.doc_type == doc_type,
            )
        )
        if doc is None or not doc.content:
            continue
        suffix = Path(doc.filename or f"{doc_type}.bin").suffix or ".bin"
        path = out / f"{case.case_no}_supporting_{doc_type}{suffix}"
        path.write_bytes(doc.content)
        paths.append(str(path))
    return paths


def save_document(
    session: Session,
    *,
    commission: Commission,
    doc_type: str,
    filename: str,
    mime: str | None,
    content: bytes,
    actor: str = "human",
) -> CommissionDocument:
    """同一类型重复上传时覆盖旧文件。"""
    if doc_type not in DOC_LABELS:
        raise ValueError(f"不支持的文件类型：{doc_type}")
    if not content:
        raise ValueError("上传文件为空")
    existing = session.scalar(
        select(CommissionDocument).where(
            CommissionDocument.commission_id == commission.id,
            CommissionDocument.doc_type == doc_type,
        )
    )
    if existing is None:
        document = CommissionDocument(
            commission_id=commission.id,
            doc_type=doc_type,
            filename=filename,
            mime=mime,
            size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            content=content,
            uploaded_by=actor,
        )
        session.add(document)
    else:
        document = existing
        document.filename = filename
        document.mime = mime
        document.size = len(content)
        document.sha256 = hashlib.sha256(content).hexdigest()
        document.content = content
        document.uploaded_by = actor
    session.flush()
    audit.record(
        session,
        action="commission.document_uploaded",
        actor=actor,
        decision=doc_type,
        reasons=[f"委托 #{commission.id} {DOC_LABELS[doc_type]}：{filename}"],
    )
    return document
