"""Offline tests for ``dev_tools.ood_eval``.

No database, no model, no network: a fake executor answers SQL from a dictionary, so
the tests pin the *plumbing* (per-database aggregation, the markdown table, the CLI
writing a report) rather than PostgreSQL behaviour, which ``fixed_eval``'s own tests
cover.

    python -m pytest tests/test_ood_eval.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dev_tools import ood_eval

GOLD = [
    {"question_id": "ood:alpha:0", "db_id": "alpha", "question": "How many rows?",
     "evidence": "", "SQL": "SELECT count(*) FROM t", "difficulty": "unknown"},
    {"question_id": "ood:alpha:1", "db_id": "alpha", "question": "Give a",
     "evidence": "", "SQL": "SELECT a FROM t", "difficulty": "unknown"},
    {"question_id": "ood:beta:0", "db_id": "beta", "question": "Give x",
     "evidence": "", "SQL": "SELECT x FROM u", "difficulty": "unknown"},
    {"question_id": "ood:beta:1", "db_id": "beta", "question": "Give y",
     "evidence": "", "SQL": "SELECT y FROM u", "difficulty": "unknown"},
]

PREDICTIONS = {
    "ood:alpha:0": "SELECT count(*) FROM t",        # same rows -> correct
    "ood:alpha:1": "SELECT b FROM t",               # executes, different rows -> wrong
    "ood:beta:0": "SELECT x FROM u LIMIT 1",        # same single row -> correct
    "ood:beta:1": "SELECT missing_column FROM u",   # raises -> pred_error
}

_ROWS = {
    "select count(*) from t": [(3,)],
    "select a from t": [(1,)],
    "select b from t": [(2,)],
    "select x from u": [(5,)],
    "select x from u limit 1": [(5,)],
    "select y from u": [(7,)],
}


def _normalise(sql: str) -> str:
    return " ".join(sql.lower().split())


def fake_execute(sql: str, db_id=None):
    """Deterministic stand-in for ``fixed_eval.PostgresExecutor``."""

    key = _normalise(sql)
    if "missing_column" in key:
        raise RuntimeError("(psycopg.errors.UndefinedColumn) column does not exist")
    if key not in _ROWS:
        raise RuntimeError(f"(psycopg.errors.SyntaxError) unexpected statement: {sql!r}")
    return list(_ROWS[key])


def _write_inputs(tmp_path: Path) -> tuple[Path, Path]:
    gold = tmp_path / "gold.json"
    preds = tmp_path / "preds.json"
    gold.write_text(json.dumps(GOLD), encoding="utf-8")
    preds.write_text(json.dumps(PREDICTIONS), encoding="utf-8")
    return preds, gold


def test_evaluate_aggregates_per_database_and_counts_scores():
    report = ood_eval.evaluate(PREDICTIONS, GOLD, executor=fake_execute)

    assert report["ex_all"]["correct"] == 2
    assert report["ex_all"]["n"] == 4
    table = {row["db_id"]: row for row in report["per_database"]}
    assert set(table) == {"alpha", "beta"}
    assert (table["alpha"]["correct"], table["alpha"]["n"]) == (1, 2)
    assert (table["beta"]["correct"], table["beta"]["n"]) == (1, 2)
    assert table["beta"]["pred_unexecutable"] == 1
    assert report["mode"] == "local"


def test_a_failing_prediction_is_an_error_not_an_exception():
    report = ood_eval.evaluate(PREDICTIONS, GOLD, executor=fake_execute)
    beta = [row for row in report["per_question"] if row["question_id"] == "ood:beta:1"][0]
    assert beta["score"] == 0
    assert beta["status"] == "pred_error"


def test_render_markdown_has_a_row_per_database():
    report = ood_eval.evaluate(PREDICTIONS, GOLD, executor=fake_execute)
    text = ood_eval.render_markdown(report["per_database"], title="fixture")
    assert "| database | n | EX |" in text
    assert "| alpha | 2 | 1/2 | 50.0 |" in text
    assert "| beta | 2 | 1/2 | 50.0 |" in text
    assert "**fixture**" in text


def test_mode_is_carried_into_the_report():
    report = ood_eval.evaluate(PREDICTIONS, GOLD, executor=fake_execute, mode="normalized")
    assert report["mode"] == "normalized"
    assert "mode=normalized" in report["protocol"]


def test_cli_writes_the_report_and_prints_the_table(tmp_path, capsys):
    preds, gold = _write_inputs(tmp_path)
    out = tmp_path / "scores" / "report.json"
    md = tmp_path / "table.md"

    code = ood_eval.main(
        ["--predictions", str(preds), "--gold", str(gold), "--out", str(out), "--markdown", str(md)],
        executor=fake_execute,
    )

    assert code == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["ex_all"]["correct"] == 2
    assert len(written["per_database"]) == 2
    assert md.read_text(encoding="utf-8").startswith("| database |")
    printed = capsys.readouterr().out
    assert "EX 2/4 = 50.00 %" in printed


def test_cli_refuses_a_gold_file_without_questions(tmp_path):
    preds, _ = _write_inputs(tmp_path)
    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    with pytest.raises(SystemExit):
        ood_eval.evaluate(PREDICTIONS, empty, executor=fake_execute)


def test_parser_defaults_to_the_local_mode():
    args = ood_eval.build_parser().parse_args(["--predictions", "p.json", "--gold", "g.json"])
    assert args.mode == "local"
    assert args.out is None
