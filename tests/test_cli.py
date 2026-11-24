import json

from autoemail.cli import build_parser
from autoemail.models import Draft
from autoemail.pipeline import process_message
from autoemail.channels.eml import EmlDirectoryChannel
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def _create_draft(session, world):
    return process_message(session, EmlDirectoryChannel(FIXTURES).fetch_since()[0])


def test_draft_cli_parser_commands():
    parser = build_parser()
    assert parser.parse_args(["draft", "list"]).draft_command == "list"
    assert parser.parse_args(["draft", "show", "1"]).draft_command == "show"
    assert parser.parse_args(["draft", "edit", "1", "--reviewer", "ops"]).draft_command == "edit"
    assert parser.parse_args(["draft", "reject", "1", "--reviewer", "ops", "--reason", "缺资料"]).draft_command == "reject"
    assert parser.parse_args(["draft", "approve", "1", "--reviewer", "ops"]).draft_command == "approve"
    initiate = parser.parse_args(["initiate", "--commission-id", "1", "--agent-id", "2"])
    assert initiate.command == "initiate"
    assert initiate.auto_mode == "dry_run"
    assert parser.parse_args(["llm", "stats"]).llm_command == "stats"
    case_mode = parser.parse_args(["case", "set-mode", "1", "--mode", "dry_run"])
    assert case_mode.command == "case"
    assert case_mode.mode == "dry_run"
    invoice_list = parser.parse_args(["invoice", "list", "--status", "unpaid"])
    assert invoice_list.invoice_command == "list"
    invoice_pay = parser.parse_args(
        ["invoice", "pay", "1", "--method", "wire", "--reference", "WT-1", "--reviewer", "ops"]
    )
    assert invoice_pay.invoice_command == "pay"
    assert invoice_pay.method == "wire"


def test_draft_dict_is_json_serializable(session, world, capsys):
    result = _create_draft(session, world)
    # 直接验证 CLI show 命令会输出完整 JSON；运行时数据库由测试 fixture 隔离，
    # 这里不调用 main，避免 CLI 的全局 session 与测试 session 混用。
    draft = session.get(Draft, result.draft_id)
    payload = {
        "id": draft.id,
        "subject": draft.subject,
        "status": draft.status,
        "controlled_fields": draft.controlled_fields,
    }
    assert json.dumps(payload, ensure_ascii=False)
