"""Offline BIRD execution harness on the official SQLite copies.

**Protocol name: ``bird-sqlite-official``.**  This is the dialect the shipped
gold queries are written in and the dialect the BIRD leaderboard measures.  It
is *not* the course harness: that one rewrites gold ``sqlite -> postgres`` with a
regex, executes on PostgreSQL and reports ``EX/241`` with its own ceiling
(30 unparseable + 2 identifier-mutated gold items — see ``docs/EVAL.md``).
Every artifact produced here carries :data:`PROTOCOL` and :data:`HARNESS` so the
two cannot be confused in a table.

What it does
------------
``run_gold(answers_file)``
    Executes the shipped gold SQL against ``data_cache/dev_databases/<db>/<db>.sqlite``
    and records pass / execute-error / timeout per question.  ``0`` execute
    errors on ``bird_large`` and ``bird_small`` (measured, sqlglot-free), and
    exactly one timeout at the official 30 s budget (``question_id 701``); the
    artifact is deterministic, so two runs are byte-identical.
``score(predictions, answers_file)``
    Official BIRD SQLite EX: ``set(predicted_rows) == set(gold_rows)`` — order-
    and duplicate-blind, no tolerance, no column-name check.  This is what an
    adapter is measured with when the course PostgreSQL is unavailable.
``filter_for_distillation(teacher_predictions, answers_file)``
    Keeps teacher answers that execute **and** match gold, and writes them as a
    clean SFT source with counts (drop reasons: missing, error, timeout,
    mismatch, unusable gold).

Everything is read-only: databases are opened ``mode=ro&immutable=1`` and the
only writes are the JSON artifacts you ask for.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:  # package import (``python -m dev_tools.local_bench``)
    from . import paths
except ImportError:  # pragma: no cover - direct script execution
    import paths  # type: ignore[no-redef]

__all__ = [
    "HARNESS",
    "PROTOCOL",
    "connect",
    "execute_sql",
    "filter_for_distillation",
    "main",
    "run_gold",
    "score",
]

PROTOCOL: str = "bird-sqlite-official"
HARNESS: str = "offline-sqlite (official BIRD dialect) - NOT the course PostgreSQL harness"
NOT_COMPARABLE_WITH: str = (
    "course PostgreSQL harness EX/241 / EX/211 (benchmarks/evaluate_bird.py, sqlite->postgres "
    "rewrite); see docs/EVAL.md and docs/LOCAL_BENCH.md"
)
#: Official BIRD per-query execution budget (mini_dev ``func_timeout(30)``).
DEFAULT_TIMEOUT_S: float = 30.0
#: Abort the query after this many VM instructions without a deadline check.
PROGRESS_STEPS: int = 10_000


# ---------------------------------------------------------------------------
# Connections and execution
# ---------------------------------------------------------------------------


def connect(db_id: str, sqlite_dir: Any = None) -> sqlite3.Connection:
    """Open the SQLite copy of ``db_id`` read-only.

    Raises ``FileNotFoundError`` with the expected path and the download pointer —
    the module never downloads anything itself (tests must stay offline).
    """

    found = paths.find_sqlite(db_id, Path(sqlite_dir) if sqlite_dir else None)
    if found is None:
        expected = paths.sqlite_candidates(db_id, Path(sqlite_dir) if sqlite_dir else None)[0]
        raise FileNotFoundError(
            f"no SQLite copy for {db_id!r}: looked for {expected}. "
            f"Populate {paths.SQLITE_DIR} once (see docs/LOCAL_BENCH.md, section 'Getting the "
            f"databases'), or pass --sqlite-dir."
        )
    return sqlite3.connect(paths.sqlite_ro_uri(found), uri=True)


class Execution:
    """Outcome of one SQL statement."""

    __slots__ = ("rows", "error", "timed_out", "elapsed_s")

    def __init__(
        self,
        rows: Optional[List[Tuple[Any, ...]]],
        error: Optional[str] = None,
        timed_out: bool = False,
        elapsed_s: float = 0.0,
    ) -> None:
        self.rows = rows
        self.error = error
        self.timed_out = timed_out
        self.elapsed_s = elapsed_s

    @property
    def ok(self) -> bool:
        return self.rows is not None and self.error is None

    def status(self) -> str:
        if self.ok:
            return "ok"
        return "timeout" if self.timed_out else "error"


def execute_sql(conn: sqlite3.Connection, sql: str, timeout_s: Optional[float] = DEFAULT_TIMEOUT_S) -> Execution:
    """Run ``sql`` with a wall-clock budget, mirroring the official 30 s limit.

    The deadline is enforced with ``set_progress_handler``: SQLite calls the
    handler every :data:`PROGRESS_STEPS` VM instructions and aborts the statement
    when it returns non-zero.  That is portable (no ``SIGALRM`` on Windows) and
    interrupts *long* statements deterministically.  ``timeout_s=None`` disables
    the budget (useful to measure how slow a query really is).
    """

    if timeout_s is None:
        started = time.monotonic()
        try:
            rows = conn.execute(sql).fetchall()
        except Exception as exc:
            return Execution(None, f"{type(exc).__name__}: {exc}", False, time.monotonic() - started)
        return Execution([tuple(row) for row in rows], None, False, time.monotonic() - started)

    deadline = time.monotonic() + float(timeout_s)
    fired = {"timeout": False}

    def _progress() -> int:
        if time.monotonic() > deadline:
            fired["timeout"] = True
            return 1
        return 0

    conn.set_progress_handler(_progress, PROGRESS_STEPS)
    started = time.monotonic()
    try:
        rows = conn.execute(sql).fetchall()
    except Exception as exc:
        elapsed = time.monotonic() - started
        timed_out = fired["timeout"] or (
            elapsed >= float(timeout_s) - 1e-3 and "interrupt" in str(exc).lower()
        )
        message = (
            f"timeout after {float(timeout_s):g}s"
            if timed_out
            else f"{type(exc).__name__}: {exc}"
        )
        return Execution(None, message, timed_out, elapsed)
    finally:
        conn.set_progress_handler(None, 0)
    return Execution([tuple(row) for row in rows], None, False, time.monotonic() - started)


# ---------------------------------------------------------------------------
# Loading and shared helpers
# ---------------------------------------------------------------------------


def _load_json(source: Any) -> Any:
    if isinstance(source, (str, os.PathLike)):
        with open(source, "r", encoding="utf-8") as handle:
            return json.load(handle)
    return source


def _answers_rows(answers: Any) -> List[dict]:
    rows = _load_json(answers)
    if not isinstance(rows, list):
        raise TypeError(f"answers must be a list of objects, got {type(rows).__name__}")
    return rows


def _is_ambiguous(sql: Any) -> bool:
    return isinstance(sql, str) and sql.strip().lower() == "ambiguous"


def _row_set(rows: Optional[Iterable[Sequence[Any]]]) -> frozenset:
    """Official BIRD EX comparison key: a set of row tuples."""

    return frozenset(tuple(row) for row in (rows or []))


def _write_json(path: Any, payload: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, list):
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    else:
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    target.write_text(text + "\n", encoding="utf-8")


class _ConnectionPool:
    """One read-only connection per ``db_id``, closed together."""

    def __init__(self, sqlite_dir: Any = None) -> None:
        self.sqlite_dir = sqlite_dir
        self._connections: Dict[str, sqlite3.Connection] = {}
        self.missing: List[str] = []

    def get(self, db_id: str) -> Optional[sqlite3.Connection]:
        if db_id in self._connections:
            return self._connections[db_id]
        try:
            connection = connect(db_id, self.sqlite_dir)
        except FileNotFoundError:
            self.missing.append(db_id)
            return None
        self._connections[db_id] = connection
        return connection

    def close(self) -> None:
        for connection in self._connections.values():
            try:
                connection.close()
            except Exception:
                pass
        self._connections.clear()


def _group_by_difficulty(rows: Sequence[dict]) -> Dict[str, dict]:
    groups: Dict[str, List[dict]] = {}
    for row in rows:
        groups.setdefault(str(row.get("difficulty")), []).append(row)
    out: Dict[str, dict] = {}
    for name in sorted(groups):
        subset = groups[name]
        correct = sum(1 for row in subset if row.get("score") == 1)
        scored = sum(1 for row in subset if row.get("score") is not None)
        out[name] = {
            "n": len(subset),
            "n_scored": scored,
            "correct": correct,
            "ex": (correct / scored) if scored else None,
            "percent": (100.0 * correct / scored) if scored else None,
        }
    return out


# ---------------------------------------------------------------------------
# 1. Gold execution
# ---------------------------------------------------------------------------


def run_gold(
    answers: Any,
    sqlite_dir: Any = None,
    timeout_s: Optional[float] = DEFAULT_TIMEOUT_S,
    limit: Optional[int] = None,
    question_ids: Optional[Iterable[str]] = None,
) -> dict:
    """Execute every shipped gold query on the SQLite copies.

    Deterministic by construction: no timestamps and no per-query durations in
    the return value, so two runs produce byte-identical JSON (the test asserts
    exactly that).  ``status`` is one of ``ok`` / ``error`` / ``timeout`` /
    ``ambiguous`` / ``missing_db``.
    """

    answer_rows = _answers_rows(answers)
    wanted = {str(qid) for qid in question_ids} if question_ids is not None else None
    pool = _ConnectionPool(sqlite_dir)
    per_question: List[dict] = []
    try:
        for item in answer_rows:
            qid = str(item.get("question_id"))
            if wanted is not None and qid not in wanted:
                continue
            if limit is not None and len(per_question) >= limit:
                break
            db_id = str(item.get("db_id"))
            gold_sql = item.get("SQL")
            record = {
                "question_id": qid,
                "db_id": db_id,
                "difficulty": item.get("difficulty"),
                "status": None,
                "rows": None,
                "error": None,
            }
            if _is_ambiguous(gold_sql):
                record["status"] = "ambiguous"
                per_question.append(record)
                continue
            connection = pool.get(db_id)
            if connection is None:
                record["status"] = "missing_db"
                record["error"] = f"no SQLite copy for {db_id}"
                per_question.append(record)
                continue
            execution = execute_sql(connection, str(gold_sql), timeout_s=timeout_s)
            record["status"] = execution.status()
            record["rows"] = len(execution.rows) if execution.rows is not None else None
            record["error"] = execution.error
            per_question.append(record)
    finally:
        pool.close()

    def _count(status: str) -> int:
        return sum(1 for row in per_question if row["status"] == status)

    by_difficulty: Dict[str, dict] = {}
    for name in sorted({str(row.get("difficulty")) for row in per_question}):
        subset = [row for row in per_question if str(row.get("difficulty")) == name]
        by_difficulty[name] = {
            "n": len(subset),
            "ok": sum(1 for row in subset if row["status"] == "ok"),
            "error": sum(1 for row in subset if row["status"] == "error"),
            "timeout": sum(1 for row in subset if row["status"] == "timeout"),
        }

    return {
        "tool": "dev_tools.local_bench",
        "protocol": PROTOCOL,
        "harness": HARNESS,
        "not_comparable_with": NOT_COMPARABLE_WITH,
        "mode": "gold_execution",
        "deterministic": True,
        "sqlite_dir": str(sqlite_dir) if sqlite_dir else str(paths.SQLITE_DIR),
        "timeout_s": timeout_s,
        "n_questions": len(per_question),
        "n_ok": _count("ok"),
        "n_error": _count("error"),
        "n_timeout": _count("timeout"),
        "n_ambiguous": _count("ambiguous"),
        "n_missing_db": _count("missing_db"),
        "error_ids": [row["question_id"] for row in per_question if row["status"] == "error"],
        "timeout_ids": [row["question_id"] for row in per_question if row["status"] == "timeout"],
        "missing_db_ids": [row["question_id"] for row in per_question if row["status"] == "missing_db"],
        "by_difficulty": by_difficulty,
        "per_question": per_question,
    }


# ---------------------------------------------------------------------------
# 2. Official SQLite EX scoring
# ---------------------------------------------------------------------------


def score(
    predictions: Any,
    answers: Any,
    sqlite_dir: Any = None,
    timeout_s: Optional[float] = DEFAULT_TIMEOUT_S,
) -> dict:
    """Official BIRD SQLite execution accuracy.

    ``EX = set(predicted_rows) == set(gold_rows)``; a query that errors or times
    out scores 0.  Questions whose *gold* is unusable are excluded from
    ``ex_over_scoreable`` (the SQLite analogue of EX/211) but kept in ``ex``.
    """

    answer_rows = _answers_rows(answers)
    predictions_map = _load_json(predictions)
    if not isinstance(predictions_map, dict):
        raise TypeError(
            "predictions must be a mapping {question_id: sql} (query_results.json), "
            f"got {type(predictions_map).__name__}"
        )

    pool = _ConnectionPool(sqlite_dir)
    per_question: List[dict] = []
    try:
        for item in answer_rows:
            qid = str(item.get("question_id"))
            db_id = str(item.get("db_id"))
            gold_sql = item.get("SQL")
            prediction = predictions_map.get(qid)
            record = {
                "question_id": qid,
                "db_id": db_id,
                "difficulty": item.get("difficulty"),
                "status": None,
                "score": 0,
                "gold_rows": None,
                "pred_rows": None,
                "error": None,
            }

            gold_is_ambiguous = _is_ambiguous(gold_sql)
            pred_is_ambiguous = _is_ambiguous(prediction)
            if gold_is_ambiguous or pred_is_ambiguous:
                record["status"] = "ambiguous"
                record["score"] = int(gold_is_ambiguous and pred_is_ambiguous)
                per_question.append(record)
                continue

            if prediction is None:
                record["status"] = "missing_prediction"
                per_question.append(record)
                continue

            connection = pool.get(db_id)
            if connection is None:
                record["status"] = "missing_db"
                record["error"] = f"no SQLite copy for {db_id}"
                per_question.append(record)
                continue

            gold_execution = execute_sql(connection, str(gold_sql), timeout_s=timeout_s)
            record["gold_rows"] = len(gold_execution.rows) if gold_execution.rows is not None else None
            if not gold_execution.ok:
                record["status"] = f"gold_{gold_execution.status()}"
                record["error"] = f"gold: {gold_execution.error}"
                per_question.append(record)
                continue

            prediction_execution = execute_sql(connection, str(prediction), timeout_s=timeout_s)
            record["pred_rows"] = (
                len(prediction_execution.rows) if prediction_execution.rows is not None else None
            )
            if not prediction_execution.ok:
                record["status"] = f"pred_{prediction_execution.status()}"
                record["error"] = f"prediction: {prediction_execution.error}"
                per_question.append(record)
                continue

            record["status"] = "scored"
            record["score"] = int(
                _row_set(prediction_execution.rows) == _row_set(gold_execution.rows)
            )
            per_question.append(record)
    finally:
        pool.close()

    n_questions = len(per_question)
    correct = sum(1 for row in per_question if row["score"] == 1)
    scoreable = [row for row in per_question if row["status"] == "scored"]
    n_gold_unusable = sum(
        1 for row in per_question if row["status"] in ("gold_error", "gold_timeout")
    )
    correct_scoreable = sum(1 for row in scoreable if row["score"] == 1)

    def _ids(prefixes: Sequence[str]) -> List[str]:
        return [row["question_id"] for row in per_question if row["status"] in prefixes]

    return {
        "tool": "dev_tools.local_bench",
        "protocol": PROTOCOL,
        "harness": HARNESS,
        "not_comparable_with": NOT_COMPARABLE_WITH,
        #: The mode name is deliberately explicit: this is the official SQLite
        #: protocol, never the course PostgreSQL harness number.
        "mode": "sqlite_ex_official",
        "answers_file": str(answers) if isinstance(answers, (str, os.PathLike)) else "<in-memory>",
        "predictions": str(predictions) if isinstance(predictions, (str, os.PathLike)) else "<in-memory>",
        "sqlite_dir": str(sqlite_dir) if sqlite_dir else str(paths.SQLITE_DIR),
        "timeout_s": timeout_s,
        "n_questions": n_questions,
        "n_scored": len(scoreable),
        "n_correct": correct,
        "ex": (correct / n_questions) if n_questions else None,
        "ex_percent": (100.0 * correct / n_questions) if n_questions else None,
        "n_gold_unusable": n_gold_unusable,
        "ex_over_scoreable": (correct_scoreable / len(scoreable)) if scoreable else None,
        "n_missing_predictions": sum(1 for row in per_question if row["status"] == "missing_prediction"),
        "n_pred_error": sum(1 for row in per_question if row["status"] == "pred_error"),
        "n_pred_timeout": sum(1 for row in per_question if row["status"] == "pred_timeout"),
        "n_ambiguous": sum(1 for row in per_question if row["status"] == "ambiguous"),
        "n_missing_db": sum(1 for row in per_question if row["status"] == "missing_db"),
        "gold_unusable_ids": _ids(("gold_error", "gold_timeout")),
        "pred_error_ids": _ids(("pred_error",)),
        "pred_timeout_ids": _ids(("pred_timeout",)),
        "missing_prediction_ids": _ids(("missing_prediction",)),
        "by_difficulty": _group_by_difficulty(per_question),
        "per_question": per_question,
    }


# ---------------------------------------------------------------------------
# 3. Distillation filter
# ---------------------------------------------------------------------------


def filter_for_distillation(
    teacher_predictions: Any,
    answers: Any,
    *,
    out: Any = None,
    sqlite_dir: Any = None,
    timeout_s: Optional[float] = DEFAULT_TIMEOUT_S,
    output_format: str = "json",
) -> dict:
    """Keep teacher answers that execute **and** match gold; write the SFT source.

    Returns the report (counts + kept ids + drop reasons) and, when ``out`` is
    given, writes the clean SFT records there: one object per kept question with
    ``question_id``, ``db_id``, ``difficulty``, ``question``, ``evidence``,
    ``sql`` (the teacher SQL, verified to reproduce gold) and ``rows``.
    """

    answer_rows = _answers_rows(answers)
    predictions_map = _load_json(teacher_predictions)
    if not isinstance(predictions_map, dict):
        raise TypeError("teacher predictions must be a mapping {question_id: sql}")

    pool = _ConnectionPool(sqlite_dir)
    kept: List[dict] = []
    drops: Dict[str, int] = {
        "missing_prediction": 0,
        "gold_ambiguous": 0,
        "gold_unusable": 0,
        "pred_error": 0,
        "pred_timeout": 0,
        "mismatch": 0,
        "missing_db": 0,
    }
    try:
        for item in answer_rows:
            qid = str(item.get("question_id"))
            db_id = str(item.get("db_id"))
            gold_sql = item.get("SQL")
            teacher_sql = predictions_map.get(qid)

            if teacher_sql is None:
                drops["missing_prediction"] += 1
                continue
            if _is_ambiguous(gold_sql):
                drops["gold_ambiguous"] += 1
                continue
            connection = pool.get(db_id)
            if connection is None:
                drops["missing_db"] += 1
                continue

            gold_execution = execute_sql(connection, str(gold_sql), timeout_s=timeout_s)
            if not gold_execution.ok:
                drops["gold_unusable"] += 1
                continue

            prediction_execution = execute_sql(connection, str(teacher_sql), timeout_s=timeout_s)
            if not prediction_execution.ok:
                drops["pred_timeout" if prediction_execution.timed_out else "pred_error"] += 1
                continue
            if _row_set(prediction_execution.rows) != _row_set(gold_execution.rows):
                drops["mismatch"] += 1
                continue

            kept.append(
                {
                    "question_id": qid,
                    "db_id": db_id,
                    "difficulty": item.get("difficulty"),
                    "question": item.get("question"),
                    "evidence": item.get("evidence"),
                    "sql": teacher_sql,
                    "rows": len(gold_execution.rows or []),
                }
            )
    finally:
        pool.close()

    n_input = len(answer_rows)
    report = {
        "tool": "dev_tools.local_bench",
        "protocol": PROTOCOL,
        "harness": HARNESS,
        "not_comparable_with": NOT_COMPARABLE_WITH,
        "mode": "distillation_filter",
        "answers_file": str(answers) if isinstance(answers, (str, os.PathLike)) else "<in-memory>",
        "teacher_predictions": (
            str(teacher_predictions) if isinstance(teacher_predictions, (str, os.PathLike)) else "<in-memory>"
        ),
        "sqlite_dir": str(sqlite_dir) if sqlite_dir else str(paths.SQLITE_DIR),
        "timeout_s": timeout_s,
        "n_input": n_input,
        "n_kept": len(kept),
        "kept_share": (len(kept) / n_input) if n_input else None,
        "drop_counts": drops,
        "kept_ids": [row["question_id"] for row in kept],
        "by_difficulty": {
            name: {
                "kept": sum(1 for row in kept if str(row.get("difficulty")) == name),
                "input": sum(1 for row in answer_rows if str(row.get("difficulty")) == name),
            }
            for name in sorted({str(row.get("difficulty")) for row in answer_rows})
        },
    }
    if out is not None:
        target = Path(out)
        target.parent.mkdir(parents=True, exist_ok=True)
        if output_format == "jsonl":
            target.write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in kept) + ("\n" if kept else ""),
                encoding="utf-8",
            )
        else:
            _write_json(target, kept)
        report["out"] = str(target)
        report["output_format"] = output_format
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dev_tools.local_bench",
        description=(
            "Offline BIRD protocol on the official SQLite copies: gold execution, official "
            "SQLite EX and a distillation filter. This is NOT the course PostgreSQL harness."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "--sqlite-dir",
            default=None,
            help=f"directory with the extracted SQLite copies (default: {paths.SQLITE_DIR})",
        )
        subparser.add_argument(
            "--timeout",
            type=float,
            default=DEFAULT_TIMEOUT_S,
            help=f"per-query wall-clock budget in seconds; 0 disables it "
            f"(default: {DEFAULT_TIMEOUT_S:g}, the official BIRD budget)",
        )

    gold = sub.add_parser("gold", help="execute the shipped gold SQL and report failures")
    add_common(gold)
    gold.add_argument("--answers", required=True, help="e.g. data/bird_large.json")
    gold.add_argument("--out", default=None, help="artifact path (default: <data_cache>/artifacts/gold_local_results.json)")
    gold.add_argument("--limit", type=int, default=None, help="only the first N questions")
    gold.add_argument("--question-id", action="append", default=None, help="restrict to this id (repeatable)")

    scoring = sub.add_parser("score", help="official SQLite EX for a predictions file")
    add_common(scoring)
    scoring.add_argument("--predictions", required=True, help="e.g. query_results.json")
    scoring.add_argument("--answers", required=True, help="e.g. data/bird_large.json")
    scoring.add_argument("--out", default=None, help="artifact path (default: <data_cache>/artifacts/local_score.json)")

    distill = sub.add_parser("distill", help="filter teacher predictions into a clean SFT source")
    add_common(distill)
    distill.add_argument("--predictions", required=True, help="teacher predictions JSON")
    distill.add_argument("--answers", required=True, help="e.g. data/bird_train.json / data/bird_large.json")
    distill.add_argument("--out", default=None, help="SFT records path (default: <data_cache>/artifacts/distill_sft.json)")
    distill.add_argument(
        "--format", choices=("json", "jsonl"), default="json", help="output format of the kept records"
    )

    return parser


def _artifact_path(out: Optional[str], command: str) -> Path:
    if out:
        return Path(out)
    defaults = {
        "gold": paths.ARTIFACTS_DIR / "gold_local_results.json",
        "score": paths.ARTIFACTS_DIR / "local_score.json",
        "distill": paths.ARTIFACTS_DIR / "distill_sft.json",
    }
    return defaults[command]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    timeout = None if not args.timeout or args.timeout <= 0 else float(args.timeout)

    if args.command == "gold":
        result = run_gold(
            args.answers,
            sqlite_dir=args.sqlite_dir,
            timeout_s=timeout,
            limit=args.limit,
            question_ids=args.question_id,
        )
        out = _artifact_path(args.out, "gold")
        _write_json(out, result)
        print(f"protocol: {result['protocol']}  ({result['harness']})")
        print(f"answers: {args.answers}  n={result['n_questions']}  timeout={result['timeout_s']}")
        print(
            f"gold ok={result['n_ok']} error={result['n_error']} timeout={result['n_timeout']} "
            f"ambiguous={result['n_ambiguous']} missing_db={result['n_missing_db']}"
        )
        if result["error_ids"]:
            print(f"  error ids: {', '.join(result['error_ids'])}")
        if result["timeout_ids"]:
            print(f"  timeout ids: {', '.join(result['timeout_ids'])}")
        print(f"artifact: {out}")
        return 0

    if args.command == "score":
        result = score(args.predictions, args.answers, sqlite_dir=args.sqlite_dir, timeout_s=timeout)
        out = _artifact_path(args.out, "score")
        _write_json(out, result)
        print(f"protocol: {result['protocol']}  ({result['harness']})")
        print(
            f"EX(sqlite) = {result['n_correct']}/{result['n_questions']} = "
            f"{result['ex_percent']:.2f}%   [official SQLite protocol, NOT the Postgres harness]"
        )
        print(
            f"scoreable={result['n_scored']}  gold_unusable={result['n_gold_unusable']}  "
            f"ex_over_scoreable={result['ex_over_scoreable']}"
        )
        for name, block in result["by_difficulty"].items():
            print(f"  {name:<12} {block['correct']}/{block['n']} = {block['percent']:.2f}%")
        print(f"artifact: {out}")
        return 0

    assert args.command == "distill"
    out = Path(args.out) if args.out else (paths.ARTIFACTS_DIR / "distill_sft.json")
    report = filter_for_distillation(
        args.predictions,
        args.answers,
        out=out,
        sqlite_dir=args.sqlite_dir,
        timeout_s=timeout,
        output_format=args.format,
    )
    report_out = out.with_name(out.stem + "_report.json")
    _write_json(report_out, report)
    print(f"protocol: {report['protocol']}  ({report['harness']})")
    print(f"kept {report['n_kept']}/{report['n_input']} teacher answers -> {report['out']}")
    print(f"drops: {report['drop_counts']}")
    print(f"report: {report_out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
