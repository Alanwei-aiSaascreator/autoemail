"""本地网页审核台：查看 case、草稿、审计和 LLM 统计，并支持草稿编辑/拒绝/批准。

服务默认只监听 127.0.0.1（可设 AUTOEMAIL_WEB_HOST=0.0.0.0 开放给局域网）；批准前会检查 SMTP 配置，避免把草稿误标为失败。
"""
from __future__ import annotations

import html
import json
import sys
import webbrowser
import zlib
from datetime import datetime
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import audit
from .channels.imap import ImapSmtpChannel
from .config import settings
from .db import get_session
from .documents import DOC_LABELS, missing_documents, save_document
from .drafts import approve_draft, edit_draft, reject_draft
from .envfile import update_env
from .feishu_import import import_commission_from_feishu
from .initiate import missing_commission_fields, start_case
from .invoices import mark_paid
from .mailcheck import check_imap, check_smtp
from .state import set_auto_mode
from .textutil import normalize_text
from .models import (
    Agent,
    AuditEvent,
    Case,
    Commission,
    CommissionImport,
    CommissionKnowledge,
    Draft,
    FollowUpTask,
    FormTypeRegistry,
    INVOICE_UNPAID,
    Invoice,
    Message,
    Quote,
    Thread,
    utcnow,
)

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--line:#e3e6ea;--text:#1f2937;--muted:#6b7280;
--accent:#2563eb;--ok:#059669;--warn:#b45309;--bad:#dc2626}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}
header{background:#111827;color:#fff;padding:0 24px;display:flex;align-items:center;gap:18px;
height:56px;position:sticky;top:0;z-index:2}
header .brand{font-weight:600;font-size:15px;white-space:nowrap}
header nav{display:flex;gap:4px}
header a{color:#cbd5e1;text-decoration:none;font-size:14px;padding:6px 11px;border-radius:7px}
header a.active,header a:hover{color:#fff;background:#1f2937}
main{max-width:1180px;margin:24px auto;padding:0 20px 60px}
.crumbs{font-size:13px;color:var(--muted);margin-bottom:8px}
.crumbs a{color:var(--accent)}
.tabs{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 14px}
.tabs a{padding:6px 12px;border:1px solid var(--line);border-radius:999px;
background:var(--card);color:var(--text);font-size:13px}
.tabs a.active{background:var(--accent);border-color:var(--accent);color:#fff}
.tabs a .count{color:var(--muted);margin-left:6px}
.tabs a.active .count{color:#dbeafe}
.jump{margin:0 0 18px;font-size:13px}
.jump a{margin-right:14px}
section{scroll-margin-top:72px}
h1{font-size:20px;margin:0 0 16px}
h2{font-size:16px;margin:24px 0 10px;scroll-margin-top:72px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px}
.card .num{font-size:26px;font-weight:650;margin-top:4px}
.card .label{color:var(--muted);font-size:13px}
table{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}
th{background:#f9fafb;color:var(--muted);font-weight:500;font-size:12px;white-space:nowrap}
tr:last-child td{border-bottom:none}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.muted{color:var(--muted)}
pre{background:#0f172a;color:#e2e8f0;padding:14px;border-radius:10px;overflow:auto;
white-space:pre-wrap;word-break:break-word;font-size:13px}
.tag{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;
background:#eef2ff;color:#3730a3}
.tag.pending{background:#fef3c7;color:var(--warn)}
.tag.blocked,.tag.failed{background:#fee2e2;color:var(--bad)}
.tag.sent{background:#d1fae5;color:var(--ok)}
.kv{display:grid;grid-template-columns:140px 1fr;gap:6px 16px;background:var(--card);
border:1px solid var(--line);border-radius:10px;padding:16px}
.kv dt{color:var(--muted)}
.kv dd{margin:0}
ul.reasons{margin:0;padding-left:18px}
.alert{padding:10px 14px;border-radius:8px;background:#fef3c7;color:#92400e;
border:1px solid #fcd34d;margin:12px 0}
.alert.ok{background:#d1fae5;color:#065f46;border-color:#6ee7b7}
.alert.error{background:#fee2e2;color:#991b1b;border-color:#fca5a5}
form{margin:0 0 18px}
label{display:block;margin:10px 0 4px;color:var(--muted);font-size:13px}
input[type=text],input[type=email],input[type=password],textarea,select{width:100%;padding:8px 10px;border:1px solid var(--line);
border-radius:8px;font:inherit;background:#fff;color:var(--text)}
.panel h3{margin:16px 0 8px;font-size:14px}
textarea{min-height:260px;resize:vertical;font-family:ui-monospace,Consolas,monospace;font-size:13px}
button{padding:8px 16px;border:none;border-radius:8px;background:var(--accent);color:#fff;
font:inherit;cursor:pointer}
button.danger{background:#dc2626}
.row{display:flex;gap:18px;flex-wrap:wrap}
.row>div{flex:1 1 220px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px;margin-bottom:18px}
.hero{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;
background:linear-gradient(135deg,#1d4ed8,#2563eb);border-radius:12px;padding:18px 22px;
color:#fff;margin-bottom:20px}
.hero h1{color:#fff;margin:0;font-size:19px}
.hero p{margin:4px 0 0;color:#bfdbfe;font-size:13px}
.hero button{background:#fff;color:#1d4ed8;font-weight:600;font-size:15px;padding:11px 26px;
border-radius:9px;box-shadow:0 2px 8px rgba(0,0,0,.2)}
.hero button:hover{background:#eff6ff}
a.card{display:block;color:inherit;text-decoration:none;transition:box-shadow .15s}
a.card:hover{text-decoration:none;box-shadow:0 2px 10px rgba(37,99,235,.12);border-color:var(--accent)}
.steps{display:flex;gap:0;background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:16px 12px;margin-bottom:18px;overflow-x:auto}
.step{flex:1;min-width:90px;text-align:center;position:relative;color:var(--muted);font-size:12px}
.step .dot{width:26px;height:26px;border-radius:50%;background:#e5e7eb;color:#6b7280;
display:inline-flex;align-items:center;justify-content:center;font-size:13px;margin-bottom:6px;position:relative;z-index:1}
.step:not(:last-child)::after{content:"";position:absolute;top:13px;left:calc(50% + 15px);
width:calc(100% - 30px);height:2px;background:#e5e7eb}
.step.done{color:var(--ok)}
.step.done .dot{background:#d1fae5;color:var(--ok)}
.step.done:not(:last-child)::after{background:#6ee7b7}
.step.now{color:var(--accent);font-weight:600}
.step.now .dot{background:var(--accent);color:#fff}
.wait{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;background:#f0f9ff;color:#0369a1}
.flow{display:flex;gap:12px;margin:0 0 20px;flex-wrap:wrap}
.flow .fstep{flex:1;min-width:210px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.flow .fstep b{display:inline-flex;width:24px;height:24px;border-radius:50%;background:var(--accent);color:#fff;
align-items:center;justify-content:center;margin-right:8px;font-size:13px;vertical-align:middle}
.flow .fstep h4{display:inline;font-size:14px;margin:0;vertical-align:middle}
.flow .fstep p{margin:8px 0 0;font-size:12px;color:var(--muted)}
.stitle{font-size:15px;margin:0 0 12px}
.stitle b{display:inline-flex;width:26px;height:26px;border-radius:50%;background:var(--accent);color:#fff;
align-items:center;justify-content:center;font-size:14px;margin-right:9px;vertical-align:middle}
.doc-ok{color:var(--ok);font-weight:600}
.doc-miss{color:var(--bad);font-weight:600}
.ready{display:inline-block;padding:3px 11px;border-radius:999px;font-size:12px;font-weight:600}
.ready.yes{background:#d1fae5;color:var(--ok)}
.ready.no{background:#fee2e2;color:var(--bad)}
.picker{position:relative}
.picker input[type=text]{width:100%}
.picker-list{display:none;position:absolute;top:calc(100% + 4px);left:0;right:0;max-height:280px;overflow-y:auto;
background:#fff;border:1px solid var(--line);border-radius:8px;z-index:60;box-shadow:0 8px 24px rgba(15,23,42,.12)}
.picker-list.open{display:block}
.picker-item{padding:9px 14px;font-size:13px;cursor:pointer;display:flex;justify-content:space-between;gap:12px}
.picker-item:hover{background:#eff6ff}
.picker-item .pmuted{color:var(--muted);font-size:12px}
.picker-empty{padding:12px 14px;font-size:13px;color:var(--muted)}
.picker input.selected{border-color:var(--ok);background:#f0fdf4}
table tbody tr:hover td{background:#f8fafc}
.btn-mini{padding:5px 12px;font-size:12px;border-radius:7px}
.btn-mini.ghost{background:#fff;color:var(--text);border:1px solid var(--line)}
.btn-mini.ghost:hover{border-color:var(--accent);color:var(--accent)}
.comm-group{background:var(--card);border:1px solid var(--line);border-radius:12px;margin-bottom:16px;overflow:hidden;
transition:box-shadow .15s}
.comm-group:hover{box-shadow:0 2px 12px rgba(15,23,42,.07)}
.comm-head{display:flex;align-items:center;gap:12px;padding:12px 16px;border-bottom:1px solid var(--line);
background:linear-gradient(180deg,#fbfcfe,#f6f8fb)}
.comm-avatar{width:36px;height:36px;border-radius:9px;display:flex;align-items:center;justify-content:center;
color:#fff;font-weight:700;font-size:14px;flex:none;letter-spacing:.5px}
.comm-name{font-weight:650;font-size:14.5px}
.comm-mail{color:var(--muted);font-size:12px;margin-top:1px}
.comm-meta{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.comm-count{flex:none;font-size:12px;color:var(--accent);background:#eff6ff;border:1px solid #dbeafe;
padding:2px 10px;border-radius:999px;font-weight:600}
.comm-group table{margin:0;border:none}
.comm-group table thead th{background:#fafbfc;border-top:none}
.comm-empty{padding:14px 16px;color:var(--muted);font-size:13px}
details.section{background:var(--card);border:1px solid var(--line);border-radius:10px;margin-bottom:10px;overflow:hidden}
details.section>summary{cursor:pointer;padding:12px 16px;font-weight:600;font-size:14px;list-style:none;display:flex;align-items:center;gap:8px;user-select:none;color:var(--text)}
details.section>summary:hover{background:#f8fafc}
details.section>summary::before{content:"▸";transition:transform .15s;color:var(--muted);font-size:12px}
details.section[open]>summary::before{transform:rotate(90deg)}
details.section>summary .cnt{margin-left:auto;font-size:12px;color:var(--muted);font-weight:500;background:#f1f5f9;padding:1px 9px;border-radius:999px}
details.section .body{padding:0 16px 14px}
details.section .body table{margin:0;border:none}
details.section .body table th{background:#fafbfc}
details.section>summary .comm-avatar{width:28px;height:28px;font-size:12px;border-radius:7px;flex:none}
details.section>summary .comm-meta{display:flex;flex-direction:column;min-width:0;flex:1}
details.section>summary .comm-name{font-size:14px;font-weight:650;line-height:1.3}
details.section>summary .comm-mail{font-size:11px;color:var(--muted);font-weight:400}
.comm-list{display:flex;flex-direction:column;gap:10px}
a.comm-card{display:flex;align-items:center;gap:14px;padding:18px 20px;background:var(--card);border:1px solid var(--line);border-radius:12px;color:inherit;text-decoration:none;transition:box-shadow .15s,border-color .15s}
a.comm-card:hover{box-shadow:0 4px 18px rgba(15,23,42,.1);border-color:var(--accent);text-decoration:none}
a.comm-card .comm-avatar{width:46px;height:46px;font-size:16px;flex:none;border-radius:10px}
a.comm-card .comm-meta{display:flex;flex-direction:column;min-width:0;flex:1}
a.comm-card .comm-name{font-size:16px;font-weight:650;color:var(--text);line-height:1.3}
a.comm-card .comm-mail{font-size:12px;color:var(--muted);font-weight:400;margin-top:2px}
a.comm-card .comm-count{flex:none;font-size:12px;color:var(--accent);background:#eff6ff;border:1px solid #dbeafe;padding:3px 12px;border-radius:999px;font-weight:600}
a.comm-card .arrow{flex:none;color:var(--muted);font-size:18px}
tr.has-filled{background:#fefce8}
tr.has-filled:hover td{background:#fef9c3}
.tag.filled{background:#fef9c3;color:#92400e;border:1px solid #fcd34d}
.searchbar{display:flex;gap:10px;margin:14px 0;max-width:520px}
.searchbar input{flex:1}
@media (max-width:720px){
header{height:auto;flex-wrap:wrap;padding:10px 14px;gap:8px}
header nav{flex-wrap:wrap}
main{margin:14px auto;padding:0 12px 40px}
.steps .step{min-width:76px}
}
"""


def _e(value: object) -> str:
    if value is None:
        return '<span class="muted">—</span>'
    return html.escape(str(value))


def _fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _tag(status: str) -> str:
    return f'<span class="tag {_e(status)}">{_e(status)}</span>'


def _layout(title: str, active: str, content: str) -> str:
    def nav(key: str, href: str, label: str) -> str:
        cls = ' class="active"' if key == active else ""
        return f'<a href="{href}"{cls}>{label}</a>'

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)} · Bond Agent</title><style>{CSS}</style></head>
<body><header>
<span class="brand">Bond 邮件 Agent · 审核台</span>
<nav>
{nav("dashboard", "/", "看板")}
{nav("commissions", "/commissions", "委托")}
{nav("inquiry", "/inquiry", "发起询价")}
{nav("cases", "/cases", "Case")}
{nav("drafts", "/drafts", "草稿")}
{nav("llm", "/llm", "LLM")}
{nav("settings", "/settings", "设置")}
</nav>
</header><main>{content}</main>
<script>
// 操作人只需填一次，之后所有表单自动带上
(function() {{
  var saved = localStorage.getItem("autoemail.reviewer") || "";
  document.querySelectorAll('input[name=reviewer]').forEach(function(el) {{
    if (!el.value) el.value = saved;
    el.addEventListener("change", function() {{ localStorage.setItem("autoemail.reviewer", el.value.trim()); }});
  }});
  document.querySelectorAll("form").forEach(function(f) {{
    f.addEventListener("submit", function() {{
      var el = f.querySelector('input[name=reviewer]');
      if (el && el.value.trim()) localStorage.setItem("autoemail.reviewer", el.value.trim());
    }});
  }});
}})();
</script>
</body></html>"""


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return '<p class="muted">暂无数据</p>'
    head = "".join(f"<th>{_e(item)}</th>" for item in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _section(title: str, body: str, count: int | None = None, *, open_: bool = False) -> str:
    """折叠分区：默认收起，open_ 为 True 时展开。count 显示在标题右侧。"""
    open_attr = " open" if open_ else ""
    cnt = f'<span class="cnt">{count}</span>' if count is not None else ""
    return (
        f'<details class="section"{open_attr}>'
        f"<summary>{_e(title)} {cnt}</summary>"
        f'<div class="body">{body}</div>'
        "</details>"
    )


def _filled_case_nos() -> set[str]:
    """扫描 filled_forms 目录，返回有填表产物的 case_no 集合（形如 BOND-2026-0252）。"""
    import re

    filled_dir = Path(settings.filled_forms_dir)
    result: set[str] = set()
    if not filled_dir.is_dir():
        return result
    for item in filled_dir.iterdir():
        if not item.is_file():
            continue
        m = re.match(r"(BOND-\d{4}-\d{4})", item.name)
        if m:
            result.add(m.group(1))
    return result


def _tabs(items: list[tuple[str, str, str, int | None]], current: str) -> str:
    parts = []
    for value, label, href, count in items:
        cls = "active" if value == current else ""
        badge = f'<span class="count">{count}</span>' if count is not None else ""
        parts.append(f'<a class="{cls}" href="{href}">{_e(label)}{badge}</a>')
    return '<div class="tabs">' + "".join(parts) + "</div>"


def _reasons(items: list[str] | None) -> str:
    if not items:
        return '<span class="muted">—</span>'
    return "<ul class='reasons'>" + "".join(f"<li>{_e(item)}</li>" for item in items) + "</ul>"


# case 当前状态 → 当前在等什么（人话），按流程顺序排列
STATE_STEPS = ["init", "quoted_out", "quoted", "materials_requested", "bond_received", "invoiced", "paid"]
STATE_LABELS = {
    "init": "询价已发出",
    "quoted_out": "已发出询价",
    "quoted": "已收到报价",
    "materials_requested": "已索取材料",
    "bond_received": "已收到 Bond",
    "invoiced": "已收到发票",
    "paid": "已付款",
    "closed": "已关闭",
    "lost": "已流失",
}
STATE_WAITING = {
    "init": "等待代理商回复",
    "quoted_out": "等待代理商报价",
    "quoted": "等待确认报价",
    "materials_requested": "等待申请材料往来",
    "bond_received": "等待代理商开发票",
    "invoiced": "等待安排付款",
    "paid": "流程完成",
    "closed": "已结束",
    "lost": "已流失",
}


def _wait_badge(state: str) -> str:
    return f'<span class="wait">{_e(STATE_WAITING.get(state, state))}</span>'


def _steps_bar(state: str) -> str:
    """case 详情页顶部的流程进度条。"""
    if state in ("closed", "lost"):
        return f'<div class="steps"><div class="step done"><span class="dot">✓</span>{_e(STATE_LABELS.get(state, state))}</div></div>'
    current_idx = STATE_STEPS.index(state) if state in STATE_STEPS else 0
    parts = []
    for i, key in enumerate(STATE_STEPS):
        cls = "done" if i < current_idx else ("now" if i == current_idx else "")
        label = STATE_LABELS[key]
        dot = "✓" if i < current_idx else str(i + 1)
        parts.append(f'<div class="step {cls}"><span class="dot">{dot}</span><br>{_e(label)}</div>')
    return '<div class="steps">' + "".join(parts) + "</div>"


def render_dashboard(session: Session) -> str:
    draft_counts = dict(
        session.execute(select(Draft.status, func.count()).group_by(Draft.status)).all()
    )
    pending_drafts = draft_counts.get("pending", 0) + draft_counts.get("blocked", 0)
    unpaid_invoices = session.scalar(
        select(func.count()).select_from(Invoice).where(Invoice.status == INVOICE_UNPAID)
    ) or 0
    active_total = session.scalar(
        select(func.count()).select_from(Case).where(Case.state.notin_(("closed", "lost")))
    ) or 0

    todo_cards = [
        ("待审草稿", pending_drafts, "/drafts?status=pending", pending_drafts > 0),
        ("进行中 Case", active_total, "/cases?group=active", False),
        ("未付发票", unpaid_invoices, "/cases", unpaid_invoices > 0),
    ]
    cards_html = "".join(
        f'<a class="card" href="{href}"><div class="label">{_e(label)}</div>'
        f'<div class="num" style="{"color:var(--warn)" if alert else ""}">{_e(value)}</div></a>'
        for label, value, href, alert in todo_cards
    )

    cases = session.scalars(
        select(Case)
        .where(Case.state.notin_(("closed", "lost")))
        .order_by(Case.last_inbound_at.desc().nullslast(), Case.id.desc())
        .limit(10)
    ).all()
    case_rows = []
    for case in cases:
        commission = session.get(Commission, case.commission_id)
        agent = session.get(Agent, case.agent_id)
        case_rows.append(
            [
                f'<a href="/cases/{case.id}">{_e(case.case_no)}</a>',
                _e(commission.legal_name if commission else "—"),
                _e(agent.name if agent else "—"),
                _tag(case.state),
                _wait_badge(case.state),
                _fmt_dt(case.last_inbound_at),
            ]
        )

    events = session.scalars(
        select(AuditEvent).order_by(AuditEvent.ts.desc(), AuditEvent.id.desc()).limit(10)
    ).all()
    event_rows = [
        [
            _fmt_dt(event.ts),
            _e(event.action),
            _e(event.decision),
            _e("；".join(event.reasons or [])),
        ]
        for event in events
    ]

    content = f"""
<div class="hero">
  <div>
    <h1>Bond 询价台</h1>
    <p>上传委托文件 → 选代理商 → 点一次按钮，之后的代理商回复全部自动处理</p>
  </div>
  <a href="/inquiry"><button type="button">＋ 发起询价</button></a>
</div>
<div class="cards">{cards_html}</div>
<h2>进行中 Case</h2>
{_table(["Case 号", "委托公司", "代理商", "状态", "当前进展", "最新回复"], case_rows)}
<h2>最近动态</h2>
{_table(["时间", "动作", "决定", "原因"], event_rows)}
"""
    return _layout("看板", "dashboard", content)


def render_cases(session: Session, group: str = "active", commission_id: int | None = None) -> str:
    palette = ("#2563eb", "#059669", "#b45309", "#7c3aed", "#db2777", "#0e7490", "#4d7c0f")

    # 委托公司专属 case 列表页
    if commission_id is not None:
        commission = session.get(Commission, commission_id)
        if commission is None:
            return _layout("Case", "cases", "<h1>委托不存在</h1>")
        cases = session.scalars(
            select(Case).where(Case.commission_id == commission_id).order_by(Case.created_at.desc())
        ).all()

        # 有填表产物的 case 排最前并高亮
        filled_nos = _filled_case_nos()
        sorted_cases = sorted(cases, key=lambda c: (0 if c.case_no in filled_nos else 1, -(c.id or 0)))
        rows_html = []
        for case in sorted_cases:
            agent = session.get(Agent, case.agent_id)
            is_filled = case.case_no in filled_nos
            row_cls = ' class="has-filled"' if is_filled else ""
            badge = ' <span class="tag filled">已填表</span>' if is_filled else ""
            rows_html.append(
                f'<tr{row_cls}>'
                f'<td><a href="/cases/{case.id}">{_e(case.case_no)}</a>{badge}</td>'
                f'<td>{_e(agent.name if agent else "—")}</td>'
                f'<td>{_tag(case.state)}</td>'
                f'<td>{_wait_badge(case.state)}</td>'
                f'<td>{_e(case.auto_mode)}</td>'
                f'<td>{_fmt_dt(case.last_inbound_at)}</td>'
                f'</tr>'
            )
        filled_count = sum(1 for c in sorted_cases if c.case_no in filled_nos)
        table = (
            '<table id="case-table"><thead><tr>'
            '<th>Case 号</th><th>代理商</th><th>状态</th><th>当前进展</th><th>自动模式</th><th>最后入站</th>'
            '</tr></thead><tbody>'
            + "".join(rows_html)
            + "</tbody></table>"
        )
        content = f"""
<p class="crumbs"><a href="/cases">Case 列表</a> / {_e(commission.legal_name)}</p>
<h1>{_e(commission.legal_name)}</h1>
<div class="searchbar">
  <input type="text" id="case-search" placeholder="搜索 Case 号 / 代理商…（已填表 {filled_count} 个）" oninput="filterCases(this.value)">
</div>
{table}
<script>
function filterCases(q) {{
  q = q.toLowerCase();
  document.querySelectorAll('#case-table tbody tr').forEach(function(tr) {{
    tr.style.display = tr.textContent.toLowerCase().indexOf(q) >= 0 ? '' : 'none';
  }});
}}
</script>
"""
        return _layout(commission.legal_name, "cases", content)

    # 主页面：按委托公司分组的卡片
    total = session.scalar(select(func.count()).select_from(Case)) or 0
    closed = session.scalar(
        select(func.count()).select_from(Case).where(Case.state.in_(("closed", "lost")))
    ) or 0
    active = total - closed

    statement = select(Case).order_by(Case.created_at.desc())
    if group == "active":
        statement = statement.where(Case.state.notin_(("closed", "lost")))
    elif group == "closed":
        statement = statement.where(Case.state.in_(("closed", "lost")))
    cases = session.scalars(statement.limit(500)).all()

    groups: dict[int, list] = {}
    order: list[int] = []
    for case in cases:
        if case.commission_id not in groups:
            groups[case.commission_id] = []
            order.append(case.commission_id)
        groups[case.commission_id].append(case)

    cards = []
    for cid in order:
        commission = session.get(Commission, cid)
        name = commission.legal_name if commission else "未知委托"
        mailbox = (commission.mailbox_email if commission else "") or ""
        initials = "".join(part[0] for part in name.split() if part)[:2].upper() or "—"
        color = palette[zlib.crc32(name.encode()) % len(palette)]
        count_label = f"{len(groups[cid])} 个 Case"
        cards.append(
            f"""
<a class="comm-card" href="/cases?commission={cid}">
  <span class="comm-avatar" style="background:{color}">{_e(initials)}</span>
  <span class="comm-meta">
    <span class="comm-name">{_e(name)}</span>
    <span class="comm-mail">{_e(mailbox) if mailbox else '<span class="muted">未配置发信邮箱</span>'}</span>
  </span>
  <span class="comm-count">{_e(count_label)}</span>
  <span class="arrow">›</span>
</a>"""
        )

    tabs = _tabs(
        [
            ("active", "进行中", "/cases?group=active", active),
            ("closed", "已关闭", "/cases?group=closed", closed),
            ("all", "全部", "/cases?group=all", total),
        ],
        group,
    )
    empty = '<div class="comm-group"><div class="comm-empty">当前筛选下没有 Case</div></div>'
    list_html = f'<div class="comm-list">{"".join(cards)}</div>' if cards else empty
    content = f"""
<h1>Case 列表</h1>
{tabs}
{list_html}
"""
    return _layout("Case", "cases", content)


def perform_set_mode(session: Session, case_id: int, form: dict[str, str]) -> tuple[str, str]:
    reviewer = (form.get("reviewer") or "").strip()
    mode = (form.get("mode") or "").strip()
    if not reviewer:
        return "error", "请填写操作人"
    if mode == "on" and form.get("confirm") != "yes":
        return "error", "开启 on 模式前请勾选确认"
    case = session.get(Case, case_id)
    if case is None:
        return "error", f"Case 不存在：{case_id}"
    try:
        changed = set_auto_mode(session, case, mode, actor=reviewer)
    except ValueError as exc:
        return "error", str(exc)
    if not changed:
        return "ok", f"自动模式已经是 {mode}"
    return "ok", f"自动模式已切换为 {mode}"


def perform_invoice_paid(session: Session, case_id: int, form: dict[str, str]) -> tuple[str, str]:
    reviewer = (form.get("reviewer") or "").strip()
    method = (form.get("method") or "").strip()
    reference = (form.get("reference") or "").strip()
    if not reviewer:
        return "error", "请填写操作人"
    if form.get("confirm") != "yes":
        return "error", "回填付款前请勾选确认"
    if method not in {"wire", "ach", "card", "zelle"}:
        return "error", "请选择付款方式"
    if not reference:
        return "error", "请填写付款流水号"
    try:
        invoice_id = int(form.get("invoice_id") or 0)
    except ValueError:
        return "error", "Invoice 不合法"
    invoice = session.get(Invoice, invoice_id)
    if invoice is None or invoice.case_id != case_id:
        return "error", "该 Case 下没有这张 Invoice"
    try:
        changed = mark_paid(session, invoice, method=method, reference=reference, actor=reviewer)
    except ValueError as exc:
        return "error", str(exc)
    if not changed:
        return "ok", f"Invoice #{invoice_id} 已是已付款状态"
    case = session.get(Case, case_id)
    state = case.state if case else "unknown"
    return "ok", f"Invoice #{invoice_id} 已回填付款，Case 状态 {state}"


def perform_convert_form_type(session: Session, case_id: int, form: dict[str, str]) -> tuple[str, str]:
    """新类型表单"计入储备"：字段清单沉淀为格式骨架，类型标记为 converted。"""
    reviewer = (form.get("reviewer") or "").strip()
    if not reviewer:
        return "error", "请填写操作人"
    try:
        registry_id = int(form.get("registry_id") or 0)
    except ValueError:
        return "error", "参数不合法"
    row = session.get(FormTypeRegistry, registry_id)
    if row is None or row.source_case_id != case_id:
        return "error", "储备记录不存在"
    if row.status != "pending":
        return "error", "该类型已处理过"
    from .formfill import register_skeleton_format

    format_id = register_skeleton_format(row.type_id, row.sample_fields or [])
    row.status = "converted"
    row.converted_at = utcnow()
    row.converted_by = reviewer
    session.commit()
    audit.record(
        session,
        action="form.new_type_converted",
        case_id=case_id,
        decision="converted",
        reasons=[
            f"type_id={row.type_id}",
            f"skeleton_format={format_id}",
            f"reviewer={reviewer}",
        ],
    )
    return "ok", f"新类型「{row.name}」已计入储备（骨架格式 {format_id}，映射待人工补充）"


def perform_settings(session: Session, form: dict[str, str], *, env_path: str | None = None) -> tuple[str, str]:
    reviewer = (form.get("reviewer") or "").strip()
    if not reviewer:
        return "error", "请填写操作人"
    try:
        port = int((form.get("smtp_port") or "587").strip())
    except ValueError:
        return "error", "SMTP 端口必须是数字"
    if not 1 <= port <= 65535:
        return "error", "SMTP 端口超出范围"
    try:
        batch_size = int((form.get("daily_inquiry_batch_size") or "20").strip())
    except ValueError:
        return "error", "每日询价批次必须是数字"
    if not 1 <= batch_size <= 200:
        return "error", "每日询价批次需在 1~200 之间"

    plain_fields = (
        "imap_host", "imap_user", "smtp_host", "smtp_user", "from_addr", "wecom_webhook",
        "feishu_app_id",
    )
    updates = {f"AUTOEMAIL_{field.upper()}": (form.get(field) or "").strip() for field in plain_fields}
    updates["AUTOEMAIL_SMTP_PORT"] = str(port)
    updates["AUTOEMAIL_DAILY_INQUIRY_ENABLED"] = "true" if form.get("daily_inquiry_enabled") == "on" else "false"
    updates["AUTOEMAIL_DAILY_INQUIRY_BATCH_SIZE"] = str(batch_size)

    password_fields = (
        ("imap_password", "clear_imap_password", "AUTOEMAIL_IMAP_PASSWORD"),
        ("smtp_password", "clear_smtp_password", "AUTOEMAIL_SMTP_PASSWORD"),
        ("feishu_app_secret", "clear_feishu_app_secret", "AUTOEMAIL_FEISHU_APP_SECRET"),
    )
    for field, clear_key, env_key in password_fields:
        if form.get(clear_key) == "yes":
            updates[env_key] = ""
        elif (form.get(field) or "").strip():
            updates[env_key] = (form.get(field) or "").strip()

    update_env(updates, env_path)

    for field in plain_fields:
        setattr(settings, field, updates[f"AUTOEMAIL_{field.upper()}"])
    settings.smtp_port = port
    settings.daily_inquiry_enabled = updates["AUTOEMAIL_DAILY_INQUIRY_ENABLED"] == "true"
    settings.daily_inquiry_batch_size = batch_size
    for _, _, env_key in password_fields:
        if env_key in updates:
            setattr(settings, env_key.replace("AUTOEMAIL_", "").lower(), updates[env_key])

    audit.record(
        session,
        action="settings.mail_updated",
        actor=reviewer,
        decision="updated",
        reasons=["updated=" + ",".join(sorted(key.replace("AUTOEMAIL_", "").lower() for key in updates))],
    )
    return "ok", "设置已保存到本地 .env；密钥不会回显"


def perform_connection_test() -> tuple[str, str]:
    imap = check_imap()
    smtp = check_smtp()
    parts = []
    if imap.get("ok"):
        parts.append(f"IMAP 正常（收件箱 {imap.get('inbox_messages')} 封，未读 {imap.get('unseen')}）")
    else:
        parts.append(f"IMAP 失败：{imap.get('error')}")
    parts.append("SMTP 正常" if smtp.get("ok") else f"SMTP 未通：{smtp.get('error')}")
    return ("ok" if imap.get("ok") else "error"), "；".join(parts)


def render_settings(*, flash_kind: str | None = None, flash_message: str | None = None) -> str:
    flash = (
        f'<div class="alert {_e(flash_kind)}">{_e(flash_message)}</div>'
        if flash_message
        else ""
    )
    imap_state = (
        '<span class="tag sent">已配置</span>'
        if settings.imap_host and settings.imap_user and settings.imap_password
        else '<span class="tag blocked">未配置</span>'
    )
    smtp_state = (
        '<span class="tag sent">已配置</span>'
        if settings.smtp_host and settings.smtp_user and settings.smtp_password
        else '<span class="tag blocked">未配置</span>'
    )
    imap_hint = "已配置，留空则保持不变" if settings.imap_password else "未配置"
    smtp_hint = "已配置，留空则保持不变" if settings.smtp_password else "未配置"
    feishu_state = (
        '<span class="tag sent">已配置</span>' if settings.feishu_app_secret else '<span class="tag blocked">未配置</span>'
    )
    feishu_hint = "已配置，留空则保持不变" if settings.feishu_app_secret else "未配置"

    content = f"""
<p class="crumbs"><a href="/">看板</a> / 设置</p>
<h1>邮箱与通知设置</h1>
{flash}
<p class="muted">保存后立即生效，同时写入本地 .env（不会进入 Git）。密钥只显示是否已配置，不回显内容。</p>
<div class="panel">
<form method="post" action="/settings">
  <h3>IMAP 收件 {imap_state}</h3>
  <div class="row">
    <div><label>服务器</label><input type="text" name="imap_host" value="{_e(settings.imap_host)}" placeholder="imap.qq.com"></div>
    <div><label>账号</label><input type="text" name="imap_user" value="{_e(settings.imap_user)}" placeholder="your@qq.com"></div>
  </div>
  <label>授权码 / 密码</label>
  <input type="password" name="imap_password" autocomplete="new-password" placeholder="{_e(imap_hint)}">
  <label><input type="checkbox" name="clear_imap_password" value="yes"> 清除已保存的 IMAP 授权码</label>

  <h3>SMTP 发件 {smtp_state}</h3>
  <div class="row">
    <div><label>服务器</label><input type="text" name="smtp_host" value="{_e(settings.smtp_host)}" placeholder="smtp.qq.com"></div>
    <div><label>端口</label><input type="text" name="smtp_port" value="{settings.smtp_port}"></div>
  </div>
  <div class="row">
    <div><label>账号</label><input type="text" name="smtp_user" value="{_e(settings.smtp_user)}"></div>
    <div><label>发件地址</label><input type="text" name="from_addr" value="{_e(settings.from_addr)}"></div>
  </div>
  <label>授权码 / 密码</label>
  <input type="password" name="smtp_password" autocomplete="new-password" placeholder="{_e(smtp_hint)}">
  <label><input type="checkbox" name="clear_smtp_password" value="yes"> 清除已保存的 SMTP 授权码</label>

  <h3>企业微信群机器人</h3>
  <label>Webhook</label>
  <input type="password" name="wecom_webhook" autocomplete="new-password" value="{_e(settings.wecom_webhook)}" placeholder="留空表示不启用">

  <h3>飞书应用（委托资料文档读取）</h3>
  <p class="muted">用于「委托列表」页粘贴飞书链接导入客户资料。需在飞书开放平台创建自建应用并授予云文档阅读权限。</p>
  <div class="row">
    <div><label>App ID</label><input type="text" name="feishu_app_id" value="{_e(settings.feishu_app_id)}" placeholder="cli_xxx"></div>
    <div><label>App Secret {feishu_state}</label>
      <input type="password" name="feishu_app_secret" autocomplete="new-password" placeholder="{_e(feishu_hint)}"></div>
  </div>
  <label><input type="checkbox" name="clear_feishu_app_secret" value="yes"> 清除已保存的 App Secret</label>

  <h3>自动询价</h3>
  <label><input type="checkbox" name="daily_inquiry_enabled" value="on"{' checked' if settings.daily_inquiry_enabled else ''}>
    开启每日自动询价（文件齐全的委托每天自动分配代理商发起询价；关闭后只能手动发起）</label>
  <label>每家委托每日询价代理商数量</label>
  <input type="text" name="daily_inquiry_batch_size" value="{settings.daily_inquiry_batch_size}">

  <label>操作人</label>
  <input type="text" name="reviewer" required placeholder="填写你的名字或工号">
  <p><button type="submit">保存设置</button></p>
</form>
</div>
<div class="panel">
<h3>连接测试</h3>
<p class="muted">只登录，不收取也不发送。会使用已保存的配置。</p>
<form method="post" action="/settings/test">
  <button type="submit">测试 IMAP / SMTP</button>
</form>
</div>
"""
    return _layout("设置", "settings", content)


COMMISSION_FORM_FIELDS = (
    ("legal_name", "公司法定全名"),
    ("entity_type", "公司类型"),
    ("ein", "EIN"),
    ("state_of_inc", "注册州"),
    ("addr_line1", "地址"),
    ("addr_city", "城市"),
    ("addr_state", "州"),
    ("addr_zip", "邮编"),
    ("signer_name", "签署人"),
    ("signer_title", "签署人职务"),
)


def perform_create_commission(session: Session, form: dict[str, str]) -> tuple[str, str]:
    reviewer = (form.get("reviewer") or "").strip()
    if not reviewer:
        return "error", "请填写操作人"
    values = {
        field: (form.get(field) or "").strip() for field, _ in COMMISSION_FORM_FIELDS
    }
    missing = [label for field, label in COMMISSION_FORM_FIELDS if not values[field]]
    if missing:
        return "error", "委托资料不完整，缺少：" + "、".join(missing)
    normalized = normalize_text(values["legal_name"])
    existing = session.scalar(
        select(Commission).where(Commission.legal_name_norm == normalized)
    )
    if existing is not None:
        return "error", f"该委托已存在：#{existing.id} {existing.legal_name}"
    commission = Commission(
        **values,
        legal_name_norm=normalized,
        status="active",
    )
    session.add(commission)
    session.flush()
    audit.record(
        session,
        action="commission.created",
        actor=reviewer,
        case_id=None,
        decision=str(commission.id),
        reasons=[f"新建委托 {commission.legal_name}"],
    )
    return "ok", f"委托 #{commission.id} {commission.legal_name} 已创建，可发起询价"


def perform_upload_document(
    session: Session,
    form: dict[str, str],
    files: dict[str, tuple[str, bytes]],
) -> tuple[str, str]:
    reviewer = (form.get("reviewer") or "").strip()
    if not reviewer:
        return "error", "请填写操作人"
    doc_type = (form.get("doc_type") or "").strip()
    if doc_type not in DOC_LABELS:
        return "error", "请选择文件类型"
    try:
        commission_id = int(form.get("commission_id") or 0)
    except ValueError:
        return "error", "委托不合法"
    commission = session.get(Commission, commission_id)
    if commission is None:
        return "error", f"委托不存在：{commission_id}"
    upload = files.get("file")
    if not upload or not upload[1]:
        return "error", "请选择要上传的文件"
    filename, content = upload
    if len(content) > 20 * 1024 * 1024:
        return "error", "文件超过 20MB"
    try:
        save_document(
            session,
            commission=commission,
            doc_type=doc_type,
            filename=filename,
            mime=None,
            content=content,
            actor=reviewer,
        )
    except ValueError as exc:
        return "error", str(exc)
    return "ok", f"已上传{DOC_LABELS[doc_type]}：{filename}"


def perform_start_inquiry(
    session: Session, form: dict[str, str]
) -> tuple[str, str, int | None]:
    """一键询价：资料不完整就不发起。"""
    reviewer = (form.get("reviewer") or "").strip()
    if not reviewer:
        return "error", "请填写操作人", None
    agent_name = (form.get("agent_name") or "").strip()
    agent_id_raw = (form.get("agent_id") or "").strip()
    agent = None
    if agent_name:
        agent = session.scalar(select(Agent).where(Agent.name == agent_name))
    elif agent_id_raw:
        try:
            agent = session.get(Agent, int(agent_id_raw))
        except ValueError:
            agent = None
    if agent is None:
        return "error", "代理商不存在，请从列表中选择", None
    try:
        commission_id = int(form.get("commission_id") or 0)
    except ValueError:
        return "error", "委托不合法", None
    commission = session.get(Commission, commission_id)
    if commission is None or agent is None:
        return "error", "委托或代理商不存在", None
    missing = missing_commission_fields(commission)
    if missing:
        return "error", "委托资料不完整，缺少：" + "、".join(missing), None
    if form.get("confirm") != "yes":
        return "error", "请先确认委托文件完整", None

    direct_send = form.get("direct_send") == "yes"
    try:
        result = start_case(
            session,
            commission_id=commission.id,
            agent_id=agent.id,
            agent_email=(form.get("agent_email") or "").strip() or None,
            auto_mode="on" if direct_send else "dry_run",
            channel=ImapSmtpChannel() if direct_send else None,
        )
    except ValueError as exc:
        return "error", str(exc), None

    audit.record(
        session,
        action="inquiry.started",
        actor=reviewer,
        case_id=result.case.id,
        draft_id=result.draft.id if result.draft else None,
        decision=result.case.state,
        reasons=[f"委托 {commission.legal_name} → 代理商 {agent.name}"],
    )
    if direct_send and result.send_result is not None and result.send_result.ok:
        return "ok", f"{result.case.case_no} 询价邮件已发送", result.case.id
    if result.draft is not None:
        return "ok", f"{result.case.case_no} 询价草稿 #{result.draft.id} 已生成，待审核", result.case.id
    error = result.send_result.error if result.send_result else "未发送"
    return "error", f"{result.case.case_no} 创建了 case，但发送失败：{error}", result.case.id


def render_inquiry(
    session: Session,
    *,
    flash_kind: str | None = None,
    flash_message: str | None = None,
) -> str:
    commissions = session.scalars(select(Commission).order_by(Commission.id)).all()
    agents = session.scalars(select(Agent).order_by(Agent.id)).all()
    commission_rows = []
    readiness: list[tuple[str, bool, str]] = []  # (公司名, 是否可发起, 缺什么)
    for item in commissions:
        missing_fields = missing_commission_fields(item)
        missing_docs = missing_documents(session, item.id)  # e.g. ["公司章程", ...]
        doc_cells = []
        for label in DOC_LABELS.values():
            ok = label not in missing_docs
            doc_cells.append(f'<span class="{"doc-ok" if ok else "doc-miss"}">{"✓" if ok else "✗"}</span> {_e(label)}')
        company_ok = not missing_fields
        can_start = company_ok and not missing_docs
        missing_desc = "、".join(missing_fields + missing_docs) if not can_start else ""
        commission_rows.append(
            [
                f"#{item.id}",
                _e(item.legal_name),
                _e(item.ein),
                f'<span class="{"doc-ok" if company_ok else "doc-miss"}">{"✓" if company_ok else "✗"}</span> 公司信息',
                "　".join(doc_cells),
                f'<span class="ready {"yes" if can_start else "no"}">{"可发起询价" if can_start else "未就绪"}</span>',
            ]
        )
        readiness.append((item.legal_name, can_start, missing_desc))
    document_options = "".join(
        f'<option value="{key}">{_e(label)}</option>' for key, label in DOC_LABELS.items()
    )
    commission_options = "".join(
        f'<option value="{item.id}">{_e(item.legal_name)}</option>' for item in commissions
    )
    agent_names_js = json.dumps([item.name for item in agents]).replace("<", "\\u003c")
    flash = (
        f'<div class="alert {_e(flash_kind)}">{_e(flash_message)}</div>'
        if flash_message
        else ""
    )
    readiness_html = "<ul class='reasons'>" + "".join(
        f"<li>{_e(name)}：<span class='ready {'yes' if ok else 'no'}'>{'可发起询价' if ok else '未就绪'}</span>"
        + (f"<span class='muted'>　缺：{_e(miss)}</span>" if not ok else "")
        + "</li>"
        for name, ok, miss in readiness
    ) + "</ul>" if readiness else '<p class="muted">还没有委托公司，先完成第 1 步。</p>'
    new_commission_form = f"""
<div class="panel">
<h3 class="stitle"><b>1</b>委托公司</h3>
{_table(["ID", "公司", "EIN", "资料", "必要文件", "状态"], commission_rows)}
<details{' open' if not commissions else ''}>
<summary>{'＋ 新建委托公司' if commissions else '点此填写委托公司信息（第 1 步）'}</summary>
<form method="post" action="/inquiry/commission">
  <div class="row">
    <div><label>公司法定全名</label><input type="text" name="legal_name" required></div>
    <div><label>公司类型</label>
      <select name="entity_type">
        <option value="LLC">LLC</option>
        <option value="Corporation">Corporation</option>
        <option value="Partnership">Partnership</option>
        <option value="Sole Proprietor">Sole Proprietor</option>
      </select>
    </div>
    <div><label>EIN</label><input type="text" name="ein" required></div>
    <div><label>注册州</label><input type="text" name="state_of_inc" required placeholder="DE"></div>
  </div>
  <div class="row">
    <div><label>地址</label><input type="text" name="addr_line1" required></div>
    <div><label>城市</label><input type="text" name="addr_city" required></div>
    <div><label>州</label><input type="text" name="addr_state" required></div>
    <div><label>邮编</label><input type="text" name="addr_zip" required></div>
  </div>
  <div class="row">
    <div><label>签署人</label><input type="text" name="signer_name" required></div>
    <div><label>签署人职务</label><input type="text" name="signer_title" required></div>
    <div><label>操作人</label><input type="text" name="reviewer" required></div>
  </div>
  <p><button type="submit">保存委托</button></p>
</form>
</details>
</div>
"""
    content = f"""
{flash}
<h1>发起询价</h1>
<div class="flow">
  <div class="fstep"><b>1</b><h4>委托公司</h4><p>公司信息齐全（名称 / EIN / 地址 / 签署人）</p></div>
  <div class="fstep"><b>2</b><h4>上传文件</h4><p>公司章程、税号文件、法人证件，三份缺一不可</p></div>
  <div class="fstep"><b>3</b><h4>发起询价</h4><p>选代理商，点一次按钮，之后全自动</p></div>
</div>
{new_commission_form}
<div class="panel">
<h3 class="stitle"><b>2</b>上传必要文件</h3>
<p class="muted">每种文件传一次即可，重复上传自动覆盖旧文件。</p>
<form method="post" action="/inquiry/document" enctype="multipart/form-data">
  <div class="row">
    <div><label>委托公司</label><select name="commission_id">{commission_options}</select></div>
    <div><label>文件类型</label><select name="doc_type">{document_options}</select></div>
    <div><label>选择文件</label><input type="file" name="file" required></div>
    <div><label>操作人</label><input type="text" name="reviewer" required></div>
  </div>
  <p><button type="submit">上传文件</button></p>
</form>
<p class="muted">当前文件齐备情况见上表 ✓ / ✗ 标记。</p>
</div>
<div class="panel">
<h3 class="stitle"><b>3</b>发起询价</h3>
{readiness_html}
<form method="post" action="/inquiry" id="inquiry-form">
  <div class="row">
    <div><label>委托公司</label><select name="commission_id">{commission_options}</select></div>
    <div class="picker"><label>Bond 代理商（点击选择）</label>
      <input type="text" id="agent-search" placeholder="点开选择，或输入关键词筛选" autocomplete="off">
      <input type="hidden" name="agent_name" id="agent-name">
      <div class="picker-list" id="agent-dropdown"></div>
    </div>
    <div><label>代理商邮箱</label><input type="text" name="agent_email" placeholder="域名需与代理商档案一致；档案只有一个邮箱时可留空"></div>
    <div><label>操作人</label><input type="text" name="reviewer" required></div>
  </div>
  <label><input type="checkbox" name="confirm" value="yes"> 我已核对委托文件完整（公司信息、EIN、地址、签署人齐全）</label>
  <label><input type="checkbox" name="direct_send" value="yes"> 直接发送询价邮件（不勾选则只生成审核草稿）</label>
  <p><button type="submit">开始询价</button></p>
</form>
</div>
<script>
const AGENTS = {agent_names_js};
const search = document.getElementById('agent-search');
const hidden = document.getElementById('agent-name');
const drop = document.getElementById('agent-dropdown');
function pick(name) {{
  hidden.value = name;
  search.value = name;
  search.classList.add('selected');
  drop.classList.remove('open');
}}
function addOption(name) {{
  const d = document.createElement('div');
  d.className = 'picker-item';
  const s = document.createElement('span');
  s.textContent = name;
  d.appendChild(s);
  d.addEventListener('mousedown', e => {{ e.preventDefault(); pick(name); }});
  drop.appendChild(d);
}}
function renderList() {{
  drop.innerHTML = '';
  const q = search.classList.contains('selected') ? '' : search.value.trim().toLowerCase();
  const matches = q ? AGENTS.filter(n => n.toLowerCase().includes(q)) : AGENTS;
  const info = document.createElement('div');
  info.className = 'picker-item';
  info.style.cursor = 'default';
  const sp = document.createElement('span');
  sp.className = 'pmuted';
  sp.textContent = '共 ' + matches.length + ' 家' + (q ? ' 匹配「' + search.value.trim() + '」' : '（输入关键词可筛选）') + (matches.length > 80 ? '，显示前 80 家' : '');
  info.appendChild(sp);
  drop.appendChild(info);
  matches.slice(0, 80).forEach(addOption);
  if (!matches.length) {{
    const e = document.createElement('div');
    e.className = 'picker-empty';
    e.textContent = '没有匹配的代理商，换个关键词试试';
    drop.appendChild(e);
  }}
}}
search.addEventListener('focus', () => {{
  if (search.classList.contains('selected')) return;
  renderList();
  drop.classList.add('open');
}});
search.addEventListener('input', () => {{
  search.classList.remove('selected');
  hidden.value = '';
  renderList();
  drop.classList.add('open');
}});
search.addEventListener('blur', () => setTimeout(() => drop.classList.remove('open'), 150));
document.getElementById('inquiry-form').addEventListener('submit', e => {{
  if (!hidden.value) {{
    e.preventDefault();
    alert('请从列表中选择 Bond 代理商');
    search.focus();
  }}
}});
</script>
"""
    return _layout("发起询价", "inquiry", content)


def render_commissions(
    session: Session,
    *,
    flash_kind: str | None = None,
    flash_message: str | None = None,
) -> str:
    """委托列表：统计所有委托的资料 / 文件 / 询价数据，并提供飞书链接导入入口。"""
    from .daily_inquiry import TERMINAL_STATES as INQUIRY_STOP_STATES

    commissions = session.scalars(select(Commission).order_by(Commission.id)).all()
    imports = {
        row.commission_id: row
        for row in session.scalars(
            select(CommissionImport).where(CommissionImport.status == "ok").order_by(CommissionImport.id)
        ).all()
    }
    rows = []
    stats = {"total": len(commissions), "fields_ok": 0, "ready": 0, "paid": 0}
    active_cases_total = 0
    for item in commissions:
        missing_fields = missing_commission_fields(item)
        if not missing_fields:
            stats["fields_ok"] += 1
        cases = session.scalars(select(Case).where(Case.commission_id == item.id)).all()
        active = [c for c in cases if c.state not in INQUIRY_STOP_STATES]
        active_cases_total += len(active)
        missing_docs = missing_documents(session, item.id)
        has_paid = any(c.state == "paid" for c in cases)
        if has_paid:
            stats["paid"] += 1
        can_start = not missing_fields and not missing_docs and not has_paid
        if can_start:
            stats["ready"] += 1
        latest_import = imports.get(item.id)
        link_cell = "—"
        if latest_import is not None:
            link_cell = (
                f'<a href="{_e(latest_import.source_url)}" target="_blank" rel="noopener">飞书文档</a>'
                f'<span class="muted">（{_e(latest_import.created_at.strftime("%m-%d %H:%M"))}）</span>'
            )
        status_cell = (
            '<span class="ready yes">可询价</span>' if can_start
            else '<span class="ready no">已付款·停止询价</span>' if has_paid
            else '<span class="ready no">未就绪</span>'
        )
        rows.append(
            [
                f"#{item.id}",
                f'<a href="/cases?commission={item.id}">{_e(item.legal_name)}</a>',
                _e(item.ein or "—"),
                _e(item.state_of_inc or "—"),
                str(len(cases)),
                str(len(active)),
                f'<span class="{"doc-ok" if not missing_docs else "doc-miss"}">{3 - len(missing_docs)}/3</span>',
                f'<span class="{"doc-ok" if not missing_fields else "doc-miss"}">{len(missing_fields)} 项缺失</span>'
                + (f'<span class="muted">（{_e("、".join(missing_fields))}）</span>' if missing_fields else ""),
                status_cell,
                link_cell,
            ]
        )
    knowledge_counts = dict(
        session.execute(
            select(CommissionKnowledge.commission_id, func.count()).group_by(CommissionKnowledge.commission_id)
        ).all()
    )
    import_history = session.scalars(
        select(CommissionImport).order_by(CommissionImport.id.desc()).limit(8)
    ).all()
    history_rows = [
        [
            f"#{row.id}",
            f'<a href="/cases?commission={row.commission_id}">#{row.commission_id}</a>' if row.commission_id else "—",
            f'<a href="{_e(row.source_url)}" target="_blank" rel="noopener">{_e(row.doc_title or row.source_url)}</a>',
            "LLM" if row.via == "llm" else "规则",
            f'<span class="tag {"sent" if row.status == "ok" else "failed"}">{"成功" if row.status == "ok" else "失败"}</span>',
            _e(
                row.error
                or (
                    f"填入 {len(row.filled or [])} 项，缺 {len(row.missing or [])} 项"
                    + (
                        f"；知识库 {knowledge_counts.get(row.commission_id, 0)} 条"
                        if row.commission_id in knowledge_counts
                        else ""
                    )
                )
            ),
            _e(row.created_at.strftime("%m-%d %H:%M")),
        ]
        for row in import_history
    ]
    flash = (
        f'<div class="alert {_e(flash_kind)}">{_e(flash_message)}</div>'
        if flash_message
        else ""
    )
    auto_state = (
        '<span class="tag sent">已开启</span>' if settings.daily_inquiry_enabled
        else '<span class="tag blocked">已关闭</span>'
    )
    content = f"""
{flash}
<h1>委托列表</h1>
<div class="cards">
  <div class="card"><div class="num">{stats['total']}</div><div class="label">委托总数</div></div>
  <div class="card"><div class="num">{stats['fields_ok']}</div><div class="label">资料字段齐全</div></div>
  <div class="card"><div class="num">{stats['ready']}</div><div class="label">可发起询价</div></div>
  <div class="card"><div class="num">{active_cases_total}</div><div class="label">活跃 Case</div></div>
  <div class="card"><div class="num">{stats['paid']}</div><div class="label">已付款（停止询价）</div></div>
</div>
<div class="panel">
<h3>粘贴飞书链接，导入新客户资料</h3>
<p class="muted">两种链接都支持：<b>资料文件夹</b>（推荐，整套材料自动下载入库，公司章程 / EIN /
法人证件会直接补齐询价硬性文件，其余按语义分类存档）或<b>飞书文档</b>（纯资料表单，只导入文字字段）。
文件命名建议「公司名——材料名」（如 JA Tendo Foods——公司章程.pdf）。导入只补空缺字段，不会覆盖已有人工数据；
首次使用请先到 <a href="/settings">设置</a> 配置飞书应用凭据。</p>
<form method="post" action="/commissions/feishu">
  <div class="row">
    <div style="flex:3 1 320px"><label>飞书文档链接</label>
      <input type="text" name="feishu_url" required
        placeholder="https://xxx.feishu.cn/docx/… 或 /wiki/…"></div>
    <div><label>操作人</label><input type="text" name="reviewer" required></div>
  </div>
  <p><button type="submit">导入委托</button></p>
</form>
</div>
{_table(["ID", "公司", "EIN", "注册州", "Case", "活跃", "必要文件", "资料字段", "询价状态", "飞书来源"], rows)}
<div class="panel">
<h3>自动询价 {auto_state}</h3>
<p class="muted">文件齐全的委托由每日批量询价自动分配代理商（批次与开关在 <a href="/settings">设置</a> 页调整）。</p>
</div>
<div class="panel">
<h3>最近导入记录</h3>
{_table(["#", "委托", "来源文档", "提取方式", "状态", "结果", "时间"], history_rows) if history_rows else '<p class="muted">还没有导入记录。</p>'}
</div>
"""
    return _layout("委托列表", "commissions", content)


def perform_import_feishu(
    session: Session,
    form: dict[str, str],
    *,
    provider=None,
) -> tuple[str, str]:
    reviewer = (form.get("reviewer") or "").strip()
    if not reviewer:
        return "error", "请填写操作人"
    url = (form.get("feishu_url") or "").strip()
    if not url:
        return "error", "请粘贴飞书文档链接"
    try:
        record = import_commission_from_feishu(session, url, reviewer=reviewer, provider=provider)
    except ValueError as exc:
        return "error", str(exc)
    if record.status == "failed":
        return "error", f"导入失败：{record.error}"
    filled = "、".join(record.filled or []) or "无新增字段"
    missing = "、".join(record.missing or [])
    detail = f"填入：{filled}；缺：{missing}" if missing else f"填入：{filled}；资料齐全"
    return "ok", f"委托 #{record.commission_id} 已从飞书文档导入（{detail}）"


def render_case(
    session: Session,
    case_id: int,
    *,
    flash_kind: str | None = None,
    flash_message: str | None = None,
) -> str | None:
    case = session.get(Case, case_id)
    if case is None:
        return None
    commission = session.get(Commission, case.commission_id)
    agent = session.get(Agent, case.agent_id)

    # 填表产物：case 级以 case_no 前缀落盘，批量产物以委托名落盘
    filled_dir = Path(settings.filled_forms_dir)
    filled_files: dict[str, Path] = {}
    if filled_dir.is_dir():
        patterns = [f"{case.case_no}_*"]
        if commission is not None:
            patterns.append(f"{commission.legal_name}_*")
        for pattern in patterns:
            for item in filled_dir.glob(pattern):
                if item.is_file():
                    filled_files[item.name] = item
    filled_rows = []
    for name, item in sorted(filled_files.items()):
        try:
            mtime = datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        except OSError:
            mtime = "—"
        filled_rows.append(
            [
                f'<a href="/filled/{quote(name)}" target="_blank">{_e(name)}</a>',
                _e(f"{item.stat().st_size} B"),
                _e(mtime),
            ]
        )
    filled_html = (
        _table(["文件名", "大小", "生成时间"], filled_rows)
        if filled_rows
        else '<p class="muted">暂无填表产物（收到代理商表格并自动填充后生成）</p>'
    )

    facts = f"""
{_steps_bar(case.state)}
<dl class="kv">
<dt>当前进展</dt><dd>{_wait_badge(case.state)}</dd>
<dt>委托公司</dt><dd>{_e(commission.legal_name if commission else "—")}</dd>
<dt>代理商</dt><dd>{_e(agent.name if agent else "—")}</dd>
<dt>自动模式</dt><dd>{_e(case.auto_mode)}</dd>
<dt>进入当前状态</dt><dd>{_fmt_dt(case.state_entered_at)}</dd>
<dt>最后入站 / 出站</dt><dd>{_fmt_dt(case.last_inbound_at)} / {_fmt_dt(case.last_outbound_at)}</dd>
<dt>催办次数</dt><dd>{_e(case.followup_count)}</dd>
</dl>
"""

    # 新类型表单储备：LLM 判定为未见过的类型（含新字段结构）→ 提示人工"计入储备"
    registry_rows = session.scalars(
        select(FormTypeRegistry)
        .where(FormTypeRegistry.source_case_id == case.id)
        .order_by(FormTypeRegistry.id.desc())
    ).all()
    pending_registry = [item for item in registry_rows if item.status == "pending"]
    new_type_alert = ""
    if pending_registry:
        blocks = []
        for row in pending_registry:
            blocks.append(f"""
<div class="panel">
<h3>{_e(row.name)}（{_e(row.type_id)}）</h3>
<p class="muted">来自附件 {_e(row.source_filename or "—")}；样本字段 {len(row.sample_fields or [])} 个。
该表格类型此前从未见过（可能含有我们没有的字段结构），确认后计入储备并启动转化流程。</p>
<details><summary>LLM 判定依据</summary><pre>{_e(row.reason or "—")}</pre></details>
<details><summary>样本字段名</summary><pre>{_e(", ".join(row.sample_fields or []))}</pre></details>
<form method="post" action="/cases/{case.id}/form-type/convert">
  <input type="hidden" name="registry_id" value="{row.id}">
  <div class="row"><div><label>操作人</label><input type="text" name="reviewer" required></div></div>
  <p><button type="submit">计入储备</button></p>
</form>
</div>""")
        new_type_alert = (
            '<div class="alert">检测到新类型表单（不是已知类型的新格式，而是没见过的类型），'
            "请确认后计入储备：</div>" + "".join(blocks)
        )
    registry_rows_html = _table(
        ["ID", "类型", "状态", "来源附件", "字段数", "时间"],
        [
            [
                f"#{item.id}",
                _e(f"{item.name}（{item.type_id}）"),
                _tag(item.status),
                _e(item.source_filename or "—"),
                str(len(item.sample_fields or [])),
                _fmt_dt(item.converted_at or item.created_at),
            ]
            for item in registry_rows
        ],
    ) if registry_rows else '<p class="muted">暂无新类型登记</p>'

    messages = session.scalars(
        select(Message)
        .where(Message.thread_id.in_(select(Thread.id).where(Thread.case_id == case.id)))
        .order_by(Message.sent_at.asc(), Message.id.asc())
    ).all()
    message_rows = [
        [
            _e(item.direction),
            _fmt_dt(item.sent_at),
            _e(item.from_addr),
            _e(item.subject),
            f'<details><summary>查看正文</summary><pre>{_e(item.body_text)}</pre></details>',
        ]
        for item in messages
    ]

    drafts = session.scalars(select(Draft).where(Draft.case_id == case.id).order_by(Draft.id.desc())).all()
    draft_rows = [
        [
            f'<a href="/drafts/{draft.id}">#{draft.id}</a>',
            _e(draft.intent),
            _tag(draft.status),
            _e(draft.subject),
            _e(draft.model_id),
            _fmt_dt(draft.created_at),
        ]
        for draft in drafts
    ]

    invoices = session.scalars(select(Invoice).where(Invoice.case_id == case.id)).all()
    invoice_rows = [
        [
            f"#{item.id}",
            _e(item.invoice_no),
            _e(f"{item.amount} {item.currency}" if item.amount is not None else "未识别金额"),
            _e(item.payment_method),
            _e(item.due_date),
            _tag(item.status),
            _e(f"{item.paid_method} / {item.paid_reference}" if item.status == "paid" else None),
            _fmt_dt(item.created_at),
        ]
        for item in invoices
    ]
    unpaid = [item for item in invoices if item.status == INVOICE_UNPAID]
    unpaid_options = "".join(
        f'<option value="{item.id}">#{item.id} '
        f'{_e(item.invoice_no or "无发票号")} / '
        f'{_e(str(item.amount) if item.amount is not None else "未识别金额")} {_e(item.currency)}</option>'
        for item in unpaid
    )
    paid_form = (
        f"""
<div class="panel">
<h3>回填已付款</h3>
<form method="post" action="/cases/{case.id}/invoice-paid">
  <div class="row">
    <div><label>Invoice</label><select name="invoice_id">{unpaid_options}</select></div>
    <div><label>付款方式</label>
      <select name="method">
        <option value="wire">wire（电汇）</option>
        <option value="ach">ach</option>
        <option value="card">card（银行卡）</option>
        <option value="zelle">zelle</option>
      </select>
    </div>
    <div><label>流水号</label><input type="text" name="reference" required></div>
    <div><label>操作人</label><input type="text" name="reviewer" required></div>
  </div>
  <label><input type="checkbox" name="confirm" value="yes"> 我已确认款项已付出，回填后 Case 进入 paid</label>
  <p><button type="submit">回填已付款</button></p>
</form>
</div>
"""
        if unpaid
        else ""
    )

    quotes = session.scalars(
        select(Quote).where(Quote.case_id == case.id).order_by(Quote.id.desc())
    ).all()
    quote_rows = [
        [
            f"#{item.id}",
            _e(f"{item.amount} {item.currency}"),
            _e(", ".join(item.extras or []) or "无"),
            _e(f"{item.total_first_year} USD"),
            _e(item.vs_history_ratio),
            _fmt_dt(item.created_at),
        ]
        for item in quotes
    ]

    followups = session.scalars(
        select(FollowUpTask).where(FollowUpTask.case_id == case.id).order_by(FollowUpTask.id.desc())
    ).all()
    followup_rows = [
        [
            f"#{item.id}",
            _tag(item.status),
            _fmt_dt(item.due_at),
            _e(item.trigger_state),
            _e(f"{item.attempt_count}/{item.max_attempts}"),
        ]
        for item in followups
    ]

    events = session.scalars(
        select(AuditEvent)
        .where(AuditEvent.case_id == case.id)
        .order_by(AuditEvent.ts.desc(), AuditEvent.id.desc())
        .limit(30)
    ).all()
    event_rows = [
        [
            _fmt_dt(event.ts),
            _e(event.action),
            _e(event.decision),
            _e("；".join(event.reasons or [])),
        ]
        for event in events
    ]

    flash = (
        f'<div class="alert {_e(flash_kind)}">{_e(flash_message)}</div>'
        if flash_message
        else ""
    )

    has_pending_drafts = any(d.status in ("pending", "blocked") for d in drafts)
    has_unpaid = bool(unpaid)

    mode_form = f"""
<form method="post" action="/cases/{case.id}/mode">
  <div class="row">
    <div><label>操作人</label><input type="text" name="reviewer" required></div>
    <div><label>模式</label>
      <select name="mode">
        <option value="off"{" selected" if case.auto_mode == "off" else ""}>off（全人工，只出草稿）</option>
        <option value="dry_run"{" selected" if case.auto_mode == "dry_run" else ""}>dry_run（只出草稿，不发送）</option>
        <option value="on"{" selected" if case.auto_mode == "on" else ""}>on（允许自动发送）</option>
      </select>
    </div>
  </div>
  <label><input type="checkbox" name="confirm" value="yes"> 我确认切换自动模式（on 会允许自动发送）</label>
  <p><button type="submit">保存模式</button></p>
</form>
"""

    sections = "".join([
        _section("填表产物", filled_html, len(filled_rows), open_=True),
        _section("新类型表单储备", registry_rows_html, len(registry_rows), open_=bool(pending_registry)),
        _section(
            "草稿",
            _table(["ID", "意图", "状态", "主题", "模型", "创建时间"], draft_rows),
            len(drafts),
            open_=has_pending_drafts,
        ),
        _section(
            "邮件",
            _table(["方向", "时间", "发件人", "主题", "正文"], message_rows),
            len(messages),
        ),
        _section(
            "报价",
            _table(["ID", "报价", "附加费", "首年合计", "历史倍数", "创建时间"], quote_rows),
            len(quotes),
        ),
        _section(
            "Invoice",
            _table(["ID", "发票号", "金额", "方式", "到期日", "状态", "付款回填", "创建时间"], invoice_rows) + paid_form,
            len(invoices),
            open_=has_unpaid,
        ),
        _section(
            "催办任务",
            _table(["ID", "状态", "到期时间", "触发状态", "尝试次数"], followup_rows),
            len(followups),
        ),
        _section(
            "审计",
            _table(["时间", "动作", "决定", "原因"], event_rows),
            len(events),
        ),
        _section("自动模式", mode_form),
    ])

    content = f"""
<p class="crumbs"><a href="/cases">Case 列表</a> / {_e(case.case_no)}</p>
<h1>{_e(case.case_no)}</h1>
{flash}
{new_type_alert}
{facts}
{sections}
"""
    return _layout(case.case_no, "cases", content)


def render_drafts(session: Session, status: str = "all") -> str:
    counts = dict(
        session.execute(select(Draft.status, func.count()).group_by(Draft.status)).all()
    )
    total = sum(counts.values())
    statement = select(Draft).order_by(Draft.created_at.desc())
    if status != "all":
        statement = statement.where(Draft.status == status)
    drafts = session.scalars(statement.limit(200)).all()

    rows = []
    for draft in drafts:
        case = session.get(Case, draft.case_id)
        actions = ""
        if draft.status in ("pending", "blocked"):
            actions = (
                f'<form method="post" action="/drafts/{draft.id}/approve" style="margin:0;display:inline">'
                f'<input type="hidden" name="reviewer" value="">'
                f'<input type="hidden" name="confirm" value="yes">'
                f'<button class="btn-mini" onclick="return confirm(\'确认批准并发送该草稿？\')">批准</button></form> '
                f'<a class="btn-mini ghost" href="/drafts/{draft.id}">查看 / 编辑</a>'
            )
        rows.append(
            [
                f'<a href="/drafts/{draft.id}">#{draft.id}</a>',
                f'<a href="/cases/{draft.case_id}">{_e(case.case_no if case else draft.case_id)}</a>',
                _e(draft.intent),
                _tag(draft.status),
                _e(draft.subject),
                _fmt_dt(draft.created_at),
                actions,
            ]
        )

    tabs = _tabs(
        [
            ("all", "全部", "/drafts", total),
            ("pending", "待审", "/drafts?status=pending", counts.get("pending", 0)),
            ("blocked", "已拦截", "/drafts?status=blocked", counts.get("blocked", 0)),
            ("sent", "已发送", "/drafts?status=sent", counts.get("sent", 0)),
            ("rejected", "已拒绝", "/drafts?status=rejected", counts.get("rejected", 0)),
            ("failed", "失败", "/drafts?status=failed", counts.get("failed", 0)),
        ],
        status,
    )
    content = f"""
<p class="crumbs"><a href="/">看板</a> / 草稿</p>
<h1>草稿</h1>
{tabs}
{_table(["ID", "Case", "意图", "状态", "主题", "创建时间", "操作"], rows)}
"""
    return _layout("草稿", "drafts", content)


def _smtp_ready() -> bool:
    return bool(settings.smtp_host and settings.smtp_user and settings.smtp_password)


def _parse_addrs(value: str | None) -> list[str]:
    return [item.strip().lower() for item in (value or "").split(",") if item.strip()]


def perform_edit(session: Session, draft_id: int, form: dict[str, str]) -> tuple[str, str]:
    reviewer = (form.get("reviewer") or "").strip()
    if not reviewer:
        return "error", "请填写审核人"
    to_addrs = _parse_addrs(form.get("to"))
    if not to_addrs:
        return "error", "收件人不能为空"
    try:
        draft = edit_draft(
            session,
            draft_id,
            reviewer=reviewer,
            subject=(form.get("subject") or "").strip() or None,
            to_addrs=to_addrs,
            cc_addrs=_parse_addrs(form.get("cc")),
            body=form.get("body") or None,
        )
    except ValueError as exc:
        return "error", str(exc)
    return "ok", f"草稿已保存，当前版本 v{draft.version}"


def perform_reject(session: Session, draft_id: int, form: dict[str, str]) -> tuple[str, str]:
    reviewer = (form.get("reviewer") or "").strip()
    reason = (form.get("reason") or "").strip()
    if not reviewer or not reason:
        return "error", "请填写审核人和拒绝原因"
    try:
        reject_draft(session, draft_id, reviewer=reviewer, reason=reason)
    except ValueError as exc:
        return "error", str(exc)
    return "ok", "草稿已拒绝"


def perform_approve(
    session: Session, draft_id: int, form: dict[str, str], channel: object | None = None
) -> tuple[str, str]:
    reviewer = (form.get("reviewer") or "").strip()
    if not reviewer:
        return "error", "请填写审核人"
    if form.get("confirm") != "yes":
        return "error", "请勾选确认后再批准发送"
    if channel is None:
        if not _smtp_ready():
            return "error", "SMTP 未配置，已取消发送；请先在 .env 填写 SMTP 账号"
        channel = ImapSmtpChannel()
    try:
        result = approve_draft(session, draft_id, reviewer=reviewer, channel=channel)
    except ValueError as exc:
        return "error", str(exc)
    if result.draft.status == "sent":
        return "ok", "已通过发送闸门并发送"
    reasons = "；".join(result.reasons or [])
    return "error", f"未发送，状态 {result.draft.status}" + (f"：{reasons}" if reasons else "")


def render_draft(
    session: Session,
    draft_id: int,
    *,
    flash_kind: str | None = None,
    flash_message: str | None = None,
) -> str | None:
    draft = session.get(Draft, draft_id)
    if draft is None:
        return None
    case = session.get(Case, draft.case_id)

    facts = f"""
<dl class="kv">
<dt>草稿 ID</dt><dd>#{draft.id}</dd>
<dt>Case</dt><dd><a href="/cases/{draft.case_id}">{_e(case.case_no if case else draft.case_id)}</a></dd>
<dt>意图</dt><dd>{_e(draft.intent)}</dd>
<dt>状态</dt><dd>{_tag(draft.status)}</dd>
<dt>收件人</dt><dd>{_e(", ".join(draft.to_addrs or []))}</dd>
<dt>抄送</dt><dd>{_e(", ".join(draft.cc_addrs or []))}</dd>
<dt>命中规则</dt><dd>{_e(", ".join(draft.rule_hits or []))}</dd>
<dt>模型</dt><dd>{_e(draft.model_id)}</dd>
<dt>输入哈希</dt><dd>{_e(draft.input_hash)}</dd>
<dt>创建 / 发送</dt><dd>{_fmt_dt(draft.created_at)} / {_fmt_dt(draft.sent_at)}</dd>
</dl>
"""
    notices = _reasons(draft.notices)
    body = _e(draft.body)
    fields = html.escape(json.dumps(draft.controlled_fields or {}, ensure_ascii=False, indent=2, default=str))
    edited = (
        f"<h2>人工修改记录</h2><pre>{_e(draft.edited_diff)}</pre>" if draft.edited_diff else ""
    )
    error = f'<h2>发送错误</h2><pre>{_e(draft.send_error)}</pre>' if draft.send_error else ""
    flash = (
        f'<div class="alert {_e(flash_kind)}">{_e(flash_message)}</div>'
        if flash_message
        else ""
    )

    actions = ""
    if draft.status in {"pending", "blocked", "failed"}:
        smtp_hint = (
            ""
            if _smtp_ready()
            else '<div class="alert">SMTP 未配置：可以编辑或拒绝，但批准发送会被拦截。</div>'
        )
        actions = f"""
<h2>审核操作</h2>
{smtp_hint}
<div class="panel">
<form method="post" action="/drafts/{draft.id}/edit">
  <div class="row">
    <div><label>审核人</label><input type="text" name="reviewer" required></div>
    <div><label>收件人（逗号分隔）</label><input type="text" name="to" value="{_e(', '.join(draft.to_addrs or []))}" required></div>
    <div><label>抄送（逗号分隔）</label><input type="text" name="cc" value="{_e(', '.join(draft.cc_addrs or []))}"></div>
  </div>
  <label>主题</label><input type="text" name="subject" value="{_e(draft.subject)}">
  <label>正文</label><textarea name="body">{_e(draft.body)}</textarea>
  <p><button type="submit">保存修改</button></p>
</form>
</div>
<div class="panel">
<form method="post" action="/drafts/{draft.id}/approve">
  <label>审核人</label><input type="text" name="reviewer" required>
  <label><input type="checkbox" name="confirm" value="yes"> 我已核对收件人、金额和正文，确认发送</label>
  <p><button type="submit">批准并发送</button></p>
</form>
</div>
<div class="panel">
<form method="post" action="/drafts/{draft.id}/reject">
  <label>审核人</label><input type="text" name="reviewer" required>
  <label>拒绝原因</label><input type="text" name="reason" required>
  <p><button type="submit" class="danger">拒绝草稿</button></p>
</form>
</div>
"""

    content = f"""
<h1><a href="/drafts">草稿</a> / #{draft.id}</h1>
{flash}
{facts}
<h2>主题</h2><p>{_e(draft.subject)}</p>
<h2>正文</h2><pre>{body}</pre>
<h2>提示</h2>{notices}
<h2>受控字段</h2><pre>{fields}</pre>
{edited}
{error}
{actions}
"""
    return _layout(f"草稿 #{draft.id}", "drafts", content)


def render_llm(session: Session) -> str:
    stats = audit.llm_stats(session)
    shadow = stats["shadow"]
    rate = "—" if shadow["agree_rate"] is None else f"{shadow['agree_rate'] * 100:.0f}%"
    cards = [
        ("影子对比", shadow["total"]),
        ("一致", shadow["agree"]),
        ("不一致", shadow["disagree"]),
        ("一致率", rate),
        ("兜底次数", stats["fallback"]["total"]),
        ("模型草稿", stats["draft"]["total"]),
    ]
    cards_html = "".join(
        f'<div class="card"><div class="label">{_e(label)}</div><div class="num">{_e(value)}</div></div>'
        for label, value in cards
    )

    pair_rows = [
        [
            _e(pair["deterministic"]),
            _e(pair["llm"]),
            _e(pair["count"]),
            "一致" if pair["agree"] else "不一致",
        ]
        for pair in shadow["pairs"]
    ]

    events = session.scalars(
        select(AuditEvent)
        .where(AuditEvent.action.in_(audit.LLM_ACTIONS))
        .order_by(AuditEvent.ts.desc(), AuditEvent.id.desc())
        .limit(30)
    ).all()
    event_rows = [
        [
            _fmt_dt(event.ts),
            _e(event.action),
            _e(event.decision),
            _e(", ".join(event.rule_ids or [])),
            _e("；".join(event.reasons or [])),
            _e(event.model_id),
            _e(event.latency_ms),
        ]
        for event in events
    ]

    fallback_rows = [[_e(label), _e(count)] for label, count in stats["fallback"]["labels"].items()]
    draft_rows = [[_e(label), _e(count)] for label, count in stats["draft"]["decisions"].items()]

    content = f"""
<h1>LLM 统计</h1>
<div class="cards">{cards_html}</div>
<h2>影子对比分布</h2>
{_table(["确定性判断", "模型判断", "次数", "结果"], pair_rows)}
<h2>意图兜底</h2>
{_table(["模型标签", "次数"], fallback_rows)}
<h2>模型草稿建议</h2>
{_table(["结果", "次数"], draft_rows)}
<h2>最近 LLM 审计</h2>
{_table(["时间", "动作", "决定", "规则", "原因", "模型", "耗时 ms"], event_rows)}
"""
    return _layout("LLM", "llm", content)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = parsed.path
        # 填表产物下载：/filled/<filename> 直接回 PDF，不走 HTML 渲染
        if path.startswith("/filled/"):
            self._serve_filled(path.split("/", 2)[-1])
            return
        query = parse_qs(parsed.query)
        flash_kind = (query.get("kind") or [None])[0]
        flash_message = (query.get("msg") or [None])[0]
        status = 200
        session = get_session()
        try:
            if path == "/":
                body = render_dashboard(session)
            elif path == "/commissions":
                body = render_commissions(
                    session,
                    flash_kind=flash_kind,
                    flash_message=flash_message,
                )
            elif path == "/inquiry":
                body = render_inquiry(
                    session,
                    flash_kind=flash_kind,
                    flash_message=flash_message,
                )
            elif path == "/cases":
                group = (query.get("group") or ["active"])[0]
                commission_param = (query.get("commission") or [None])[0]
                try:
                    commission_id = int(commission_param) if commission_param else None
                except ValueError:
                    commission_id = None
                body = render_cases(session, group, commission_id=commission_id)
            elif path.startswith("/cases/"):
                try:
                    case_id = int(path.rstrip("/").rsplit("/", 1)[1])
                except ValueError:
                    case_id = -1
                body = render_case(
                    session,
                    case_id,
                    flash_kind=flash_kind,
                    flash_message=flash_message,
                )
                status = 200 if body else 404
                if body is None:
                    body = _layout("未找到", "cases", "<h1>Case 不存在</h1>")
            elif path == "/drafts":
                body = render_drafts(session, (query.get("status") or ["all"])[0])
            elif path.startswith("/drafts/"):
                try:
                    draft_id = int(path.rstrip("/").rsplit("/", 1)[1])
                except ValueError:
                    draft_id = -1
                body = render_draft(
                    session,
                    draft_id,
                    flash_kind=flash_kind,
                    flash_message=flash_message,
                )
                status = 200 if body else 404
                if body is None:
                    body = _layout("未找到", "drafts", "<h1>草稿不存在</h1>")
            elif path == "/llm":
                body = render_llm(session)
            elif path == "/settings":
                body = render_settings(flash_kind=flash_kind, flash_message=flash_message)
            else:
                status = 404
                body = _layout("未找到", "", "<h1>页面不存在</h1>")
        finally:
            session.close()

        self._respond(status, body)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        parts = [item for item in parsed.path.split("/") if item]
        is_settings = parsed.path == "/settings"
        is_settings_test = parsed.path == "/settings/test"
        is_inquiry = parsed.path == "/inquiry"
        is_new_commission = parsed.path == "/inquiry/commission"
        is_document_upload = parsed.path == "/inquiry/document"
        is_feishu_import = parsed.path == "/commissions/feishu"
        is_draft_action = (
            len(parts) == 3
            and parts[0] == "drafts"
            and parts[2] in {"edit", "reject", "approve"}
        )
        is_case_mode = len(parts) == 3 and parts[0] == "cases" and parts[2] == "mode"
        is_invoice_paid = (
            len(parts) == 3 and parts[0] == "cases" and parts[2] == "invoice-paid"
        )
        is_form_type_convert = (
            len(parts) == 4
            and parts[0] == "cases"
            and parts[2] == "form-type"
            and parts[3] == "convert"
        )
        if not (
            is_settings
            or is_settings_test
            or is_inquiry
            or is_new_commission
            or is_document_upload
            or is_feishu_import
            or is_draft_action
            or is_case_mode
            or is_invoice_paid
            or is_form_type_convert
        ):
            self._respond(404, _layout("未找到", "", "<h1>页面不存在</h1>"))
            return
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            self._respond(403, _layout("拒绝", "", "<h1>拒绝跨站请求</h1>"))
            return

        form, files = self._read_request()
        session = get_session()
        try:
            if is_settings:
                kind, message = perform_settings(session, form)
                location = f"/settings?kind={quote(kind)}&msg={quote(message)}"
            elif is_settings_test:
                kind, message = perform_connection_test()
                location = f"/settings?kind={quote(kind)}&msg={quote(message)}"
            elif is_inquiry:
                kind, message, case_id = perform_start_inquiry(session, form)
                base = f"/cases/{case_id}" if case_id else "/inquiry"
                location = f"{base}?kind={quote(kind)}&msg={quote(message)}"
            elif is_new_commission:
                kind, message = perform_create_commission(session, form)
                location = f"/inquiry?kind={quote(kind)}&msg={quote(message)}"
            elif is_document_upload:
                kind, message = perform_upload_document(session, form, files)
                location = f"/inquiry?kind={quote(kind)}&msg={quote(message)}"
            elif is_feishu_import:
                kind, message = perform_import_feishu(session, form)
                location = f"/commissions?kind={quote(kind)}&msg={quote(message)}"
            else:
                try:
                    item_id = int(parts[1])
                except ValueError:
                    self._respond(404, _layout("未找到", "", "<h1>记录不存在</h1>"))
                    return
                if is_case_mode:
                    kind, message = perform_set_mode(session, item_id, form)
                elif is_invoice_paid:
                    kind, message = perform_invoice_paid(session, item_id, form)
                elif is_form_type_convert:
                    kind, message = perform_convert_form_type(session, item_id, form)
                elif parts[2] == "edit":
                    kind, message = perform_edit(session, item_id, form)
                elif parts[2] == "reject":
                    kind, message = perform_reject(session, item_id, form)
                else:
                    kind, message = perform_approve(session, item_id, form)
                base = (
                    f"/cases/{item_id}"
                    if is_case_mode or is_invoice_paid or is_form_type_convert
                    else f"/drafts/{item_id}"
                )
                location = f"{base}?kind={quote(kind)}&msg={quote(message)}"
        finally:
            session.close()

        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_request(self) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            return self._read_form(), {}
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 20 * 1024 * 1024:
            return {}, {}
        body = self.rfile.read(length)
        raw = (
            b"Content-Type: "
            + content_type.encode("utf-8")
            + b"\r\nMIME-Version: 1.0\r\n\r\n"
            + body
        )
        message = BytesParser(policy=policy.default).parsebytes(raw)
        fields: dict[str, str] = {}
        files: dict[str, tuple[str, bytes]] = {}
        for part in message.walk():
            if part.is_multipart():
                continue
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if filename:
                files[name] = (filename, payload)
            else:
                fields[name] = payload.decode("utf-8", errors="replace")
        return fields, files

    def _read_form(self) -> dict[str, str]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 512 * 1024:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        parsed = parse_qs(raw, keep_blank_values=True)
        return {key: values[0] for key, values in parsed.items()}

    def _serve_filled(self, filename: str) -> None:
        # 先 URL 解码（空格/中文会被编码成 %20/%XX），再只取文件名防路径穿越
        name = Path(unquote(filename)).name
        base = Path(settings.filled_forms_dir)
        file_path = base / name
        if name in {"", ".", ".."} or not file_path.is_file():
            self.send_error(404, "File not found")
            return
        data = file_path.read_bytes()
        suffix = file_path.suffix.lower()
        mime = {
            ".pdf": "application/pdf",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".gif": "image/gif",
            ".txt": "text/plain; charset=utf-8",
        }.get(suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header(
            "Content-Disposition", f'inline; filename="{name}"'
        )
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _respond(self, status: int, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


def serve(port: int = 8787, open_browser: bool = True, host: str = "127.0.0.1") -> None:
    # 输出重定向到文件时 stdout 可能是 cp1252，打印中文会崩，统一改成 UTF-8
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    server = ThreadingHTTPServer((host, port), Handler)
    shown = host if host != "0.0.0.0" else f"<本机IP>({host})"
    url = f"http://{shown}:{port}/"
    print(f"只读看板已启动：{url}（Ctrl+C 停止）")
    if open_browser:
        webbrowser.open(f"http://127.0.0.1:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
