"""飞书链接导入委托。

服务一个新客户会带来一整套新材料：客户把资料整理在一份飞书文档里，
把链接粘进系统即可拉取正文、提取委托字段、建档并留痕缺口。

- 读文档走飞书开放 API（自建应用凭据在「设置」页配置，wiki 链接自动解析）；
- 字段提取优先 LLM（task=commission_extract），未启用时退回常见标签的规则解析；
- 同名委托已存在则补全空缺字段，不覆盖已有人工数据。
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter
from mimetypes import guess_type
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit
from .config import settings
from .documents import save_document
from .llm_schemas import LLMCommissionExtractOutput
from .models import Commission, CommissionDocument, CommissionFile, CommissionImport, CommissionKnowledge
from .textutil import normalize_text

OPEN_BASE = "https://open.feishu.cn"
LOGGER = logging.getLogger(__name__)

# 询价必需字段（与 initiate.REQUIRED_COMMISSION_FIELDS 一致的键）
EXTRACT_FIELDS = (
    "legal_name",
    "entity_type",
    "ein",
    "state_of_inc",
    "addr_line1",
    "addr_city",
    "addr_state",
    "addr_zip",
    "signer_name",
    "signer_title",
)


def parse_lark_url(url: str) -> tuple[str, str]:
    """识别飞书链接类型与文档 token，返回 (kind, token)。"""
    raw = (url or "").strip()
    if not raw:
        raise ValueError("请粘贴飞书文档链接")
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise ValueError("请粘贴 http(s) 开头的飞书链接")
    segments = [seg for seg in parts.path.split("/") if seg]
    kinds = {"docx": "docx", "doc": "doc", "wiki": "wiki", "sheets": "sheet", "base": "base"}
    if len(segments) >= 2 and segments[0] in kinds:
        return kinds[segments[0]], segments[1]
    # 云空间文件夹：/drive/folder/<token> 或 /drive/folders/<token>
    if "folder" in segments or "folders" in segments:
        idx = max(segments.index("folder") if "folder" in segments else -1,
                  segments.index("folders") if "folders" in segments else -1)
        if idx + 1 < len(segments):
            return "folder", segments[idx + 1]
    raise ValueError(
        "无法识别的飞书链接；支持资料文件夹（/drive/folder/…）或文档（/docx/ /doc/ /wiki/ /sheets/ /base/）"
    )


def _tenant_access_token(app_id: str, app_secret: str) -> str:
    response = httpx.post(
        f"{OPEN_BASE}/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=15,
    )
    try:
        data = response.json()
    except ValueError as exc:
        raise ValueError(f"飞书认证接口响应异常：HTTP {response.status_code}") from exc
    if data.get("code") != 0 or not data.get("tenant_access_token"):
        raise ValueError(f"获取飞书访问凭证失败：{data.get('msg') or data.get('code')}")
    return data["tenant_access_token"]


def _get_json(path: str, token: str) -> dict:
    response = httpx.get(
        f"{OPEN_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    try:
        data = response.json()
    except ValueError as exc:
        raise ValueError(f"飞书接口响应异常：HTTP {response.status_code}") from exc
    if data.get("code") != 0:
        raise ValueError(f"飞书接口调用失败：{data.get('msg') or data.get('code')}")
    return data.get("data") or {}


def fetch_lark_doc_text(url: str) -> dict:
    """拉取飞书文档正文，返回 {kind, token, title, text}。

    wiki 链接先解析成真实文档 token；仅支持 docx（旧版 doc / 表格 / 多维
    表格会给出明确提示，请把资料整理为飞书文档）。
    """
    app_id = (settings.feishu_app_id or "").strip()
    app_secret = (settings.feishu_app_secret or "").strip()
    if not app_id or not app_secret:
        raise ValueError("尚未配置飞书应用凭据，请先到「设置」页填写 App ID / App Secret")
    kind, token = parse_lark_url(url)
    access = _tenant_access_token(app_id, app_secret)
    if kind == "wiki":
        node = _get_json(f"/open-apis/wiki/v2/spaces/get_node?token={token}", access)
        token = node.get("obj_token") or ""
        kind = node.get("obj_type") or "docx"
    if kind != "docx":
        raise ValueError(f"暂不支持直接读取「{kind}」类型；请把客户资料整理为飞书文档（docx）后再导入")
    meta = _get_json(f"/open-apis/docx/v1/documents/{token}", access)
    title = (meta.get("document") or {}).get("title") or ""
    content = _get_json(f"/open-apis/docx/v1/documents/{token}/raw_content", access).get("content") or ""
    if not content.strip():
        raise ValueError("文档正文为空，无法导入")
    return {"kind": kind, "token": token, "title": title, "text": content}


# ---- 资料文件夹：列文件、下载、按文件名语义分类 ----

# 客户资料包的常规命名（章程/EIN/SSN/驾照正反面/法人照片/地址查验/街景/房产/官网/客户需求/信息申报/回执/查重）
FILE_CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("address_verification", ("地址查验", "address verification")),
    ("street_view", ("街景", "street view")),
    ("property_info", ("房产信息", "property")),
    ("website", ("官网", "website")),
    ("ssn", ("ssn",)),
    ("ein", ("ein", "税号文件")),
    ("articles", ("公司章程", "articles of")),
    ("client_application", ("客户需求", "client application")),
    ("statement_of_information", ("信息申报", "statement of information")),
    ("filing_acknowledgment", ("申请通知书", "filing acknowledgment")),
    ("name_check", ("查重",)),
    ("driver_license_front", ("驾照正面", "照正面", "license front")),
    ("driver_license_back", ("驾照背面", "照背面", "照精面", "license back")),
    ("photo", ("法人照片", "照片", "photo", "人像")),
)
# 询价硬性三份（documents.DOC_LABELS）对应的语义分类
HARD_DOC_CATEGORIES = {"articles": "articles", "ein": "ein", "driver_license_front": "id"}


def classify_filename(name: str) -> str:
    lowered = (name or "").lower()
    for category, keywords in FILE_CATEGORY_RULES:
        if any(keyword in lowered for keyword in keywords):
            return category
    return "other"


def _download_media(token: str, access: str) -> bytes:
    response = httpx.get(
        f"{OPEN_BASE}/open-apis/drive/v1/medias/{token}/download",
        headers={"Authorization": f"Bearer {access}"},
        timeout=120,
    )
    if "application/json" in response.headers.get("content-type", ""):
        try:
            data = response.json()
        except ValueError:
            data = {}
        raise ValueError(f"下载文件失败：{data.get('msg') or data.get('code') or response.status_code}")
    response.raise_for_status()
    return response.content or b""


def fetch_lark_folder_files(url: str) -> dict:
    """列资料文件夹并下载全部文件，返回 {kind, token, files, texts}。

    files: [{name, kind, content}]；文件夹内的在线文档（docx）读正文进 texts，
    不当作文件存储。子文件夹不递归（客户资料包约定一层平铺）。
    """
    app_id = (settings.feishu_app_id or "").strip()
    app_secret = (settings.feishu_app_secret or "").strip()
    if not app_id or not app_secret:
        raise ValueError("尚未配置飞书应用凭据，请先到「设置」页填写 App ID / App Secret")
    kind, token = parse_lark_url(url)
    access = _tenant_access_token(app_id, app_secret)
    items = _get_json(f"/open-apis/drive/v1/files?folder_token={token}&page_size=200", access).get("files") or []
    files: list[dict] = []
    texts: list[str] = []
    _doc_text_pairs: list[tuple[str, str]] = []
    for item in items:
        name = item.get("name") or ""
        item_type = item.get("type") or ""
        item_token = item.get("token") or ""
        if item_type == "folder":
            continue
        if item_type == "docx":
            try:
                text = _get_json(f"/open-apis/docx/v1/documents/{item_token}/raw_content", access).get("content") or ""
            except ValueError:
                text = ""
            if text.strip():
                texts.append(f"【{name}】\n{text}")
                _doc_text_pairs.append((name, text))
            continue
        if not item_token:
            continue
        files.append(
            {
                "name": name,
                "kind": item_type or "file",
                "content": _download_media(item_token, access),
            }
        )
    if not files and not texts:
        raise ValueError("文件夹为空，或应用没有该文件夹的阅读权限")
    return {
        "kind": "folder",
        "token": token,
        "files": files,
        "texts": "\n\n".join(texts),
        "doc_texts": [{"name": name, "text": text} for name, text in _doc_text_pairs],
    }


# ---- 公司专属知识库：逐文件解析正文入库 ----

TEXT_EXTS = (".txt", ".md")
IMAGE_EXTS = (".png", ".jpg", ".jpeg")


def _extract_file_text(name: str, content: bytes) -> str:
    lower = (name or "").lower()
    if lower.endswith(TEXT_EXTS):
        return content.decode("utf-8", errors="replace")
    if lower.endswith(".pdf") and content:
        from .ingest.pdf import extract_pdf_text

        return extract_pdf_text(content)
    return ""


def _collect_knowledge_entries(files: list[dict], doc_texts: list[dict]) -> tuple[list[tuple[str, str, str]], str]:
    """解析全部材料正文。返回 (条目 [(source, category, text)], 拼接文本用于字段提取)。"""
    entries: list[tuple[str, str, str]] = []
    parts: list[str] = []
    for doc in doc_texts:
        text = (doc.get("text") or "").strip()
        if text:
            entries.append((doc.get("name") or "在线文档", "online_doc", text))
            parts.append(f"【{doc.get('name') or '在线文档'}】\n{text}")
    for item in files:
        text = _extract_file_text(item["name"], item["content"]).strip()
        if text:
            entries.append((item["name"], classify_filename(item["name"]), text))
            parts.append(f"【{item['name']}】\n{text}")
    return entries, "\n\n".join(parts)


def _license_fields_from_image(files: list[dict]) -> dict[str, str]:
    """驾照正面图片走多模态识别（仅 LLM 开启时），补法人个人信息字段。"""
    if not settings.llm_enabled:
        return {}
    for item in files:
        lower = item["name"].lower()
        if classify_filename(item["name"]) != "driver_license_front" or not lower.endswith(IMAGE_EXTS):
            continue
        from .vision import extract_driver_license

        info = extract_driver_license(item["content"], mime=guess_type(item["name"])[0] or "image/jpeg")
        if info is None:
            return {}
        data = info.model_dump()
        return {
            key: value
            for key, value in {
                "principal_full_name": data.get("full_name"),
                "principal_dob": data.get("date_of_birth"),
                "personal_address": data.get("address"),
                "driver_license_no": data.get("license_number"),
                "driver_license_state": data.get("state"),
                "driver_license_expiry": data.get("expiration_date"),
                "principal_sex": data.get("sex"),
            }.items()
            if value
        }
    return {}


def _save_knowledge_entries(
    session: Session,
    commission: Commission,
    entries: list[tuple[str, str, str]],
    files: list[dict],
) -> tuple[int, list[str]]:
    """知识条目入库（同来源覆盖）；客户需求 PDF 另走既有 intake 解析供填表取数。

    返回 (条数, notes)。
    """
    from .formfill import store_intake

    notes: list[str] = []
    count = 0
    for source, category, text in entries:
        row = session.scalar(
            select(CommissionKnowledge).where(
                CommissionKnowledge.commission_id == commission.id,
                CommissionKnowledge.source == source,
            )
        )
        if row is None:
            session.add(
                CommissionKnowledge(commission_id=commission.id, source=source, category=category, text=text)
            )
        else:
            row.category = category
            row.text = text
        count += 1
        if category == "client_application" and source.lower().endswith(".pdf"):
            content = next((item["content"] for item in files if item["name"] == source), b"")
            try:
                fields = store_intake(session, commission.id, content, filename=source)
                notes.append(f"客户需求表解析 {len(fields)} 字段")
            except Exception as exc:  # noqa: BLE001  解析失败不阻断导入
                notes.append(f"客户需求表解析失败：{exc}")
    session.flush()
    return count, notes


def _derive_legal_name(names: list[str]) -> str:
    """从文件名的公共前缀推导公司名（如「JA Tendo Foods——章程.pdf」）。"""
    prefixes: list[str] = []
    for name in names:
        for sep in ("——", "--", "—", "-"):
            if sep in name:
                prefixes.append(name.split(sep, 1)[0].strip())
                break
    prefixes = [p for p in prefixes if len(p) >= 2 and not p.isdigit()]
    if not prefixes:
        return ""
    return Counter(prefixes).most_common(1)[0][0]


def _save_folder_materials(session: Session, commission: Commission, files: list[dict], *, reviewer: str) -> int:
    """材料入库：全部进 CommissionFile 原始文件库；硬性三份同时进 CommissionDocument。

    法人证件以驾照正面为准；没有驾照时用法人照片兜底。返回硬性文件入库数。
    """
    hard_saved = 0
    id_done = False
    for item in files:
        category = classify_filename(item["name"])
        content = item["content"]
        existing = session.scalar(
            select(CommissionFile).where(
                CommissionFile.commission_id == commission.id,
                CommissionFile.filename == item["name"],
            )
        )
        if existing is None:
            session.add(
                CommissionFile(
                    commission_id=commission.id,
                    category=category,
                    filename=item["name"],
                    mime=guess_type(item["name"])[0] or "application/octet-stream",
                    size=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    content=content,
                )
            )
        else:
            existing.category = category
            existing.size = len(content)
            existing.content = content
        doc_type = HARD_DOC_CATEGORIES.get(category)
        if doc_type == "id":
            if id_done:
                continue
            id_done = True
        if doc_type:
            save_document(
                session,
                commission=commission,
                doc_type=doc_type,
                filename=item["name"],
                mime=guess_type(item["name"])[0],
                content=content,
                actor=reviewer,
            )
            hard_saved += 1
    if not id_done:  # 驾照缺失时法人照片充当代码证件
        for item in files:
            if classify_filename(item["name"]) == "photo":
                save_document(
                    session,
                    commission=commission,
                    doc_type="id",
                    filename=item["name"],
                    mime=guess_type(item["name"])[0],
                    content=item["content"],
                    actor=reviewer,
                )
                hard_saved += 1
                break
    session.flush()
    return hard_saved


def import_commission_from_feishu_folder(
    session: Session,
    url: str,
    *,
    reviewer: str,
    provider=None,
) -> CommissionImport:
    """粘贴资料文件夹链接：下载全部材料 → 按文件名分类入库 → 硬性三份进询价必需文件。

    公司名取文件名公共前缀；文件夹内的在线文档 / txt 正文会跑字段提取补档案。
    """
    url = (url or "").strip()
    try:
        folder = fetch_lark_folder_files(url)
    except ValueError as exc:
        session.add(
            CommissionImport(
                source_url=url or "-", fields={}, filled=[], missing=[],
                status="failed", error=str(exc), imported_by=reviewer,
            )
        )
        session.commit()
        audit.record(
            session,
            action="commission.feishu_import",
            actor=reviewer,
            decision="failed",
            reasons=[f"url={url}", str(exc)],
        )
        raise

    legal_name = _derive_legal_name([item["name"] for item in folder["files"]])
    # 知识库条目：在线文档 + txt/pdf 正文；拼接文本用于字段提取
    entries, text = _collect_knowledge_entries(folder["files"], folder.get("doc_texts") or [])

    fields: dict[str, str] = {}
    via = "rules"
    if text.strip():
        if provider is not None or settings.llm_enabled:
            from .llm import HttpxOpenAICompatibleProvider

            fields, llm_error = extract_fields_by_llm(
                text, provider=provider or HttpxOpenAICompatibleProvider()
            )
            if llm_error is None and fields.get("legal_name"):
                via = "llm"
            else:
                fields = {}
        if not fields.get("legal_name"):
            fields = extract_fields_by_rules(text)
    fields.update(_license_fields_from_image(folder["files"]))
    if legal_name and not (fields.get("legal_name") or "").strip():
        fields["legal_name"] = legal_name

    record = CommissionImport(
        source_url=url,
        doc_kind="folder",
        fields=fields,
        filled=[],
        missing=[label for field, label in _REQUIRED_LABELS if not (fields.get(field) or "").strip()],
        via=via,
        imported_by=reviewer,
    )
    if not (fields.get("legal_name") or "").strip():
        record.status = "failed"
        record.error = "无法从文件名前缀或文档正文确定公司名，请把文件统一命名为「公司名——材料名」后重试"
        session.add(record)
        session.commit()
        audit.record(
            session,
            action="commission.feishu_import",
            actor=reviewer,
            decision="failed",
            reasons=[f"url={url}", record.error],
        )
        return record

    commission, filled = _upsert_commission(session, fields)
    hard_saved = _save_folder_materials(session, commission, folder["files"], reviewer=reviewer)
    knowledge_count, intake_notes = _save_knowledge_entries(session, commission, entries, folder["files"])
    record.commission_id = commission.id
    record.filled = filled
    session.add(record)
    session.commit()
    audit.record(
        session,
        action="commission.feishu_import",
        case_id=None,
        actor=reviewer,
        decision="ok",
        reasons=[
            f"commission=#{commission.id} {commission.legal_name}",
            f"materials={len(folder['files'])}（硬性文件入库 {hard_saved}）",
            f"knowledge={knowledge_count} 条（{'；'.join(intake_notes) or '无 intake 备注'}）",
            f"filled={','.join(filled) or '无新增'}",
        ],
    )
    return record


# ---- 字段提取 ----

# 规则解析：常见中英文标签 → 字段（大小写不敏感的子串匹配）
_RULE_LABELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("legal_name", ("legal name", "legal entity", "exact legal entity", "company name", "公司法定全名", "公司全名", "公司名称", "注册名称")),
    ("entity_type", ("entity type", "company type", "business type", "公司类型", "实体类型")),
    ("ein", ("ein", "employer identification", "联邦税号", "税号")),
    ("state_of_inc", ("state of formation", "state of incorporation", "registered state", "注册州", "成立州")),
    ("addr_line1", ("street address", "address line 1", "principal address", "地址", "街道地址")),
    ("addr_city", ("city", "城市")),
    ("addr_state", ("state", "州")),
    ("addr_zip", ("zip", "postal code", "邮编", "邮政编码")),
    ("signer_name", ("signer", "authorized signer", "签署人")),
    ("signer_title", ("title", "职务")),
    ("phone", ("phone", "telephone", "电话")),
    ("mailbox_email", ("email", "e-mail", "邮箱")),
)
# "state" 会同时命中 state_of_inc / addr_state；按行内出现顺序，地址相关标签优先级见下
_LINE_RE = re.compile(r"^[ \t\-*•\d.、()（）]*\s*(?P<label>[^:：]{2,48})[:：]\s*(?P<value>.+?)[ \t]*$", re.MULTILINE)


def extract_fields_by_rules(text: str) -> dict[str, str]:
    fields = {field: "" for field, _ in _RULE_LABELS}
    for match in _LINE_RE.finditer(text or ""):
        label = match.group("label").strip().lower().rstrip("*_ ")
        value = match.group("value").strip()
        if not value:
            continue
        for field, aliases in _RULE_LABELS:
            if fields[field]:
                continue
            if any(alias in label for alias in aliases):
                # "state of formation: DE" 命中 state_of_inc 而不是 addr_state：
                # addr_state 只认裸 "state" 标签（前后不含 formation/incorporation）
                if field == "addr_state" and any(w in label for w in ("formation", "incorporation")):
                    continue
                fields[field] = value[:300]
                break
    return fields


def extract_fields_by_llm(text: str, *, provider) -> tuple[dict[str, str], str | None]:
    result = provider.complete_json(
        system_prompt=(
            "你是委托资料提取助手。从客户资料文档正文里提取海关保函委托所需字段，"
            "提取不到的字段留空字符串，只输出 JSON，不要编造。"
        ),
        user_prompt=f"客户资料文档正文：\n{text[: settings.llm_max_input_chars]}",
        schema=LLMCommissionExtractOutput,
        task="commission_extract",
    )
    if result.error is not None or result.parsed is None:
        return {}, result.error or "模型未返回结果"
    out = result.parsed
    fields = {name: (getattr(out, name) or "").strip() for name, _ in _RULE_LABELS}
    return fields, None


def _upsert_commission(session: Session, fields: dict[str, str]) -> tuple[Commission, list[str]]:
    """按公司名建档或补全空缺字段；返回 (委托, 本次填入的字段列表)。"""
    legal_name = fields.get("legal_name", "").strip()
    normalized = normalize_text(legal_name)
    commission = session.scalar(select(Commission).where(Commission.legal_name_norm == normalized))
    filled: list[str] = []
    if commission is None:
        commission = Commission(
            legal_name=legal_name,
            legal_name_norm=normalized,
            entity_type=fields.get("entity_type") or "LLC",
            status="active",
        )
        session.add(commission)
        session.flush()
    for field in fields:
        if field == "legal_name":
            continue
        value = (fields.get(field) or "").strip()
        if value and not str(getattr(commission, field, None) or "").strip():
            setattr(commission, field, value)
            filled.append(field)
    session.flush()
    return commission, filled


def import_commission_from_feishu(
    session: Session,
    url: str,
    *,
    reviewer: str,
    provider=None,
) -> CommissionImport:
    """粘贴飞书链接导入委托：拉正文 → 提字段 → 建档/补全 → 留痕。

    provider 传 None 时按 settings.llm_enabled 决定是否用 LLM，
    LLM 失败或未启用一律退回规则解析。
    传资料文件夹链接（/drive/folder/…）时自动走整套材料导入流程。
    """
    url = (url or "").strip()
    try:
        kind, _ = parse_lark_url(url)
    except ValueError:
        kind = ""
    if kind == "folder":
        return import_commission_from_feishu_folder(session, url, reviewer=reviewer, provider=provider)
    doc: dict | None = None
    try:
        doc = fetch_lark_doc_text(url)
    except ValueError as exc:
        record = CommissionImport(
            source_url=url or "-",
            fields={},
            filled=[],
            missing=[],
            status="failed",
            error=str(exc),
            imported_by=reviewer,
        )
        session.add(record)
        session.commit()
        audit.record(
            session,
            action="commission.feishu_import",
            actor=reviewer,
            decision="failed",
            reasons=[f"url={url}", str(exc)],
        )
        raise
    text = doc["text"]
    fields: dict[str, str] = {}
    via = "rules"
    llm_error: str | None = None
    if provider is not None or settings.llm_enabled:
        from .llm import HttpxOpenAICompatibleProvider

        fields, llm_error = extract_fields_by_llm(
            text, provider=provider or HttpxOpenAICompatibleProvider()
        )
        if llm_error is None and fields.get("legal_name"):
            via = "llm"
        else:
            fields = {}
    if not fields.get("legal_name"):
        fields = extract_fields_by_rules(text)
    missing = [label for field, label in _REQUIRED_LABELS if not (fields.get(field) or "").strip()]

    record = CommissionImport(
        source_url=url,
        doc_kind=doc["kind"],
        doc_title=doc.get("title") or None,
        fields=fields,
        filled=[],
        missing=missing,
        via=via,
        imported_by=reviewer,
    )
    if not (fields.get("legal_name") or "").strip():
        record.status = "failed"
        record.error = "未能从文档中提取到公司法定全名（legal name），请补在文档里后重试"
        session.add(record)
        session.commit()
        audit.record(
            session,
            action="commission.feishu_import",
            actor=reviewer,
            decision="failed",
            reasons=[f"url={url}", record.error],
        )
        return record

    commission, filled = _upsert_commission(session, fields)
    _save_knowledge_entries(
        session,
        commission,
        [(doc.get("title") or doc["token"], "online_doc", text)],
        [],
    )
    record.commission_id = commission.id
    record.filled = filled
    session.add(record)
    session.commit()
    audit.record(
        session,
        action="commission.feishu_import",
        case_id=None,
        actor=reviewer,
        decision="ok",
        reasons=[
            f"commission=#{commission.id} {commission.legal_name}",
            f"filled={','.join(filled) or '无新增'}",
            f"missing={','.join(missing) or '无'}",
            f"via={via}",
        ],
    )
    return record


def _required_labels() -> tuple[tuple[str, str], ...]:
    from .initiate import REQUIRED_COMMISSION_FIELDS

    return tuple(REQUIRED_COMMISSION_FIELDS)


_REQUIRED_LABELS = _required_labels()
