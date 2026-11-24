import json
from pathlib import Path

from autoemail.evaluation import evaluate_labels, export_labels
from autoemail.importing import import_eml

FIXTURES = Path(__file__).parent / "fixtures"


def _read(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n",
        encoding="utf-8",
    )


def test_export_then_evaluate_is_perfect(session, world, tmp_path):
    import_eml(session, FIXTURES)
    labels = tmp_path / "labels.jsonl"

    count = export_labels(session, labels)
    report = evaluate_labels(session, labels)

    assert count == 2
    assert report["missing"] == 0
    assert {item["predicted"]["intent"] for item in _read(labels)} == {
        "quote_reply",
        "invoice",
    }
    for field in ("intent", "case_no", "method"):
        assert report["metrics"][field]["accuracy"] == 1.0


def test_wrong_label_is_reported(session, world, tmp_path):
    import_eml(session, FIXTURES)
    labels = tmp_path / "labels.jsonl"
    export_labels(session, labels)

    records = _read(labels)
    records[0]["expected"]["intent"] = "form_request"
    _write(labels, records)

    report = evaluate_labels(session, labels)

    assert report["metrics"]["intent"]["accuracy"] < 1.0
    assert report["mismatches"][0]["field"] == "intent"
    assert report["mismatches"][0]["expected"] == "form_request"


def test_unknown_message_is_counted_as_missing(session, world, tmp_path):
    labels = tmp_path / "labels.jsonl"
    _write(
        labels,
        [
            {
                "message_key": "not-exists",
                "expected": {"intent": "quote_reply"},
            }
        ],
    )

    report = evaluate_labels(session, labels)

    assert report["missing"] == 1
    assert report["evaluated"] == 0
    assert report["metrics"]["intent"]["accuracy"] is None
