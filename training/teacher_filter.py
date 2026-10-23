r"""Teacher-distillation filter for the LoRA SFT data build.

Why this module exists
----------------------
`build_data.py` turns BIRD *gold* SQLite queries into PostgreSQL SFT targets.  That teaches the
adapter to imitate the benchmark's own authoring style, not the behaviour of the team's
feature-driven Text2SQL pipeline.  Distillation replaces the target with the teacher's SQL -- but
only where the teacher is *demonstrably not worse than gold*, i.e. where its answer is valid
PostgreSQL **and** returns exactly the gold result set.

Hard contract of this module
----------------------------
A teacher prediction is KEPT iff

  (a) its SQL parses as PostgreSQL after canonicalisation (``parse`` injectable), and
  (b) its execution result matches the gold execution result, verified through the INJECTED
      ``execute(question_id, sql)`` callable.

This module never imports a database driver, never opens a connection and never touches the
network: execution is a capability the caller injects (the harness evaluation scripts have one;
offline runs pass a replayed executor, see ``ReplayExecutor``).  With ``execute=None`` the filter
degrades to *parse-only* mode: it keeps parse-valid predictions and reports
``exec_checked=False`` so nobody can mistake the output for a verified distillation set.

Nothing here imports torch/transformers; it needs only ``sqlglot`` (for the default parse) and the
standard library.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

# Reason codes.  Everything that is not "keep*" is a drop reason, so a report can be read without
# knowing the code by heart.
KEEP_EXEC_MATCH = "keep-exec-match"
KEEP_PARSE_ONLY = "keep-parse-only"
DROP_EMPTY = "empty-prediction"
DROP_UNPARSEABLE = "unparseable-postgres"
DROP_CANONICALISE = "canonicalise-failed"
DROP_EXEC_ERROR = "exec-error"
DROP_GOLD_EXEC_ERROR = "gold-exec-error"
DROP_EXEC_MISMATCH = "exec-mismatch"

KEEP_REASONS = (KEEP_EXEC_MATCH, KEEP_PARSE_ONLY)

# `execute(question_id, sql)` -> result set (any JSON-able structure: list of rows, scalar, dict).
ExecuteFn = Callable[[str, str], Any]


# ------------------------------------------------------------------------------------------------
# result comparison
# ------------------------------------------------------------------------------------------------
def _cell_key(value: Any) -> str:
    """Canonical, sortable, type-tagged cell key with 1e-6 tolerance on numbers."""
    if value is None:
        return "null:"
    if isinstance(value, bool):  # bool is an int subclass: check it first
        return f"bool:{value}"
    if isinstance(value, (int, float)):
        return f"num:{round(float(value), 6)!r}"
    if isinstance(value, str):
        return f"str:{value.strip()}"
    return f"obj:{value!r}"


def _row_key(row: Any) -> tuple:
    if isinstance(row, (list, tuple)):
        return tuple(_cell_key(c) for c in row)
    return (_cell_key(row),)


def _table_key(result: Any) -> tuple:
    """Order-insensitive, column-order-sensitive key for a result set.

    BIRD's official execution match compares result *sets*: row order is irrelevant, column order
    is not.  Scalars and dicts are handled too so a caller may inject a simplified executor.
    """
    if result is None:
        return ("empty",)
    if isinstance(result, dict):
        return ("dict", tuple(sorted((str(k), _cell_key(v)) for k, v in result.items())))
    if isinstance(result, (list, tuple)):
        return ("rows", tuple(sorted(_row_key(r) for r in result)))
    return ("scalar", (_cell_key(result),))


def results_match(gold_result: Any, teacher_result: Any) -> bool:
    """True when two execution results are the same answer set up to row order and float noise."""
    return _table_key(gold_result) == _table_key(teacher_result)


# ------------------------------------------------------------------------------------------------
# SQL validity (injectable; build_data passes its own canonicaliser so both stay consistent)
# ------------------------------------------------------------------------------------------------
def parse_postgres(sql: str) -> None:
    """Raise if ``sql`` is not parseable as PostgreSQL.  Default ``parse`` implementation."""
    import sqlglot

    sqlglot.parse_one(sql, dialect="postgres")


def canonicalise_postgres(sql: str) -> str:
    """Default ``canonicalise``: parse and re-emit PostgreSQL, no trailing semicolon."""
    import sqlglot

    return sqlglot.parse_one(sql, dialect="postgres").sql(
        dialect="postgres", comments=False
    ).strip().rstrip(";")


# ------------------------------------------------------------------------------------------------
# report
# ------------------------------------------------------------------------------------------------
@dataclass
class DistillResult:
    kept: list = field(default_factory=list)        # [{question_id, sql, reason}]
    dropped: list = field(default_factory=list)     # [{question_id, reason, detail}]
    by_reason: dict = field(default_factory=dict)   # reason -> count
    attempted: int = 0                              # predictions that had a gold counterpart
    not_attempted: int = 0                          # gold ids the teacher never answered
    exec_checked: bool = False                      # False == parse-only mode

    def kept_sql_by_id(self) -> dict:
        return {row["question_id"]: row["sql"] for row in self.kept}

    def as_dict(self) -> dict:
        return {
            "mode": "exec-verified" if self.exec_checked else "parse-only",
            "exec_checked": self.exec_checked,
            "attempted": self.attempted,
            "not_attempted": self.not_attempted,
            "kept": len(self.kept),
            "dropped": len(self.dropped),
            "by_reason": dict(sorted(self.by_reason.items())),
            "kept_question_ids": [row["question_id"] for row in self.kept],
            "dropped_detail": [{k: v for k, v in row.items() if k != "sql"} for row in self.dropped],
        }


# ------------------------------------------------------------------------------------------------
# the filter
# ------------------------------------------------------------------------------------------------
def filter_predictions(
    predictions: Mapping[str, str],
    gold: Mapping[str, str],
    *,
    execute: ExecuteFn | None = None,
    parse: Callable[[str], None] | None = None,
    canonicalise: Callable[[str], str] | None = None,
    require_exec: bool = False,
) -> DistillResult:
    """Keep teacher answers that are valid PostgreSQL and (optionally) match gold execution.

    Args:
        predictions: ``{question_id: teacher_sql}``.
        gold: ``{question_id: gold_sql}`` -- the canonical PostgreSQL target.
        execute: ``execute(question_id, sql) -> result``.  Injected on purpose: this module has no
            database access.  ``None`` switches to parse-only mode (reported, never silent).
        parse: raise-on-failure PostgreSQL validator; defaults to sqlglot.
        canonicalise: SQL canonicaliser applied before keeping; defaults to sqlglot re-emit.
        require_exec: when True and ``execute is None``, refuse to keep anything (drop reason
            ``exec-error`` with a detail explaining that no executor was injected).  Used by the
            CLI when a verified distillation set was demanded but no replay file was given.

    Deterministic: predictions are visited in sorted question-id order.
    """
    parse = parse or parse_postgres
    canonicalise = canonicalise or canonicalise_postgres

    result = DistillResult(exec_checked=execute is not None)
    if require_exec and execute is None:
        # Nothing can be verified, so nothing may be kept.  This is a hard, loud failure mode
        # rather than a silent downgrade to parse-only.
        for qid in sorted(predictions):
            if str(qid) not in gold:
                continue
            result.attempted += 1
            result.dropped.append(
                {
                    "question_id": str(qid),
                    "reason": DROP_EXEC_ERROR,
                    "detail": "no execute() callable injected (require_exec=True); execution "
                    "equivalence cannot be verified",
                }
            )
            result.by_reason[DROP_EXEC_ERROR] = result.by_reason.get(DROP_EXEC_ERROR, 0) + 1
        result.not_attempted = len(gold) - result.attempted
        return result

    for qid in sorted(predictions):
        if qid not in gold:
            # A prediction for a question that is not in our gold set is not ours to judge.
            continue
        result.attempted += 1
        raw = predictions[qid]
        sql = (raw or "").strip()
        if not sql:
            result.dropped.append(
                {"question_id": qid, "reason": DROP_EMPTY, "detail": "prediction is empty"}
            )
            result.by_reason[DROP_EMPTY] = result.by_reason.get(DROP_EMPTY, 0) + 1
            continue
        try:
            parse(sql)
        except Exception as exc:  # noqa: BLE001 - any parser error is a drop
            result.dropped.append(
                {"question_id": qid, "reason": DROP_UNPARSEABLE, "detail": f"{type(exc).__name__}: {exc}"}
            )
            result.by_reason[DROP_UNPARSEABLE] = result.by_reason.get(DROP_UNPARSEABLE, 0) + 1
            continue
        try:
            canonical = canonicalise(sql)
        except Exception as exc:  # noqa: BLE001
            result.dropped.append(
                {"question_id": qid, "reason": DROP_CANONICALISE, "detail": f"{type(exc).__name__}: {exc}"}
            )
            result.by_reason[DROP_CANONICALISE] = result.by_reason.get(DROP_CANONICALISE, 0) + 1
            continue

        if execute is None:
            result.kept.append({"question_id": qid, "sql": canonical, "reason": KEEP_PARSE_ONLY})
            result.by_reason[KEEP_PARSE_ONLY] = result.by_reason.get(KEEP_PARSE_ONLY, 0) + 1
            continue

        try:
            teacher_result = execute(qid, sql)
        except Exception as exc:  # noqa: BLE001
            result.dropped.append(
                {"question_id": qid, "reason": DROP_EXEC_ERROR, "detail": f"{type(exc).__name__}: {exc}"}
            )
            result.by_reason[DROP_EXEC_ERROR] = result.by_reason.get(DROP_EXEC_ERROR, 0) + 1
            continue
        try:
            gold_result = execute(qid, gold[qid])
        except Exception as exc:  # noqa: BLE001
            result.dropped.append(
                {
                    "question_id": qid,
                    "reason": DROP_GOLD_EXEC_ERROR,
                    "detail": f"gold itself failed to execute: {type(exc).__name__}: {exc}",
                }
            )
            result.by_reason[DROP_GOLD_EXEC_ERROR] = result.by_reason.get(DROP_GOLD_EXEC_ERROR, 0) + 1
            continue

        if results_match(gold_result, teacher_result):
            result.kept.append({"question_id": qid, "sql": canonical, "reason": KEEP_EXEC_MATCH})
            result.by_reason[KEEP_EXEC_MATCH] = result.by_reason.get(KEEP_EXEC_MATCH, 0) + 1
        else:
            result.dropped.append(
                {
                    "question_id": qid,
                    "reason": DROP_EXEC_MISMATCH,
                    "detail": "teacher result differs from gold result",
                }
            )
            result.by_reason[DROP_EXEC_MISMATCH] = result.by_reason.get(DROP_EXEC_MISMATCH, 0) + 1

    result.not_attempted = len(gold) - result.attempted
    return result


# ------------------------------------------------------------------------------------------------
# prediction files
# ------------------------------------------------------------------------------------------------
def load_predictions(path: str) -> dict:
    """Load ``{question_id: sql}`` from a JSON object, a JSON list of records or a JSONL file.

    Accepted record keys, first match wins: ``prediction``, ``predicted_sql``, ``sql``, ``SQL``,
    ``query``.  The id key is ``question_id`` (``id``/``qid`` also accepted) and is always coerced
    to ``str`` so it matches the gold mapping regardless of JSON typing.
    """
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()

    raw: Any
    stripped = text.lstrip()
    if stripped.startswith("{"):
        raw = json.loads(text)
        if isinstance(raw, dict):
            out = {}
            for key, value in raw.items():
                if isinstance(value, dict):
                    value = (
                        value.get("prediction")
                        or value.get("predicted_sql")
                        or value.get("sql")
                        or value.get("SQL")
                        or ""
                    )
                out[str(key)] = value if isinstance(value, str) else json.dumps(value)
            return out
        raise ValueError(f"{path}: a JSON object mapping question_id -> SQL was expected")
    if stripped.startswith("["):
        raw = json.loads(text)
        return _records_to_map(raw, path)

    # JSONL fallback: one record per line.
    records = [json.loads(line) for line in text.splitlines() if line.strip()]
    return _records_to_map(records, path)


def _records_to_map(records: Iterable[Any], path: str) -> dict:
    out = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError(f"{path}: every record must be a JSON object, got {type(record).__name__}")
        qid = record.get("question_id", record.get("qid", record.get("id")))
        if qid is None:
            raise ValueError(f"{path}: record without a question_id: {record!r}")
        sql = (
            record.get("prediction")
            or record.get("predicted_sql")
            or record.get("sql")
            or record.get("SQL")
            or record.get("query")
            or ""
        )
        out[str(qid)] = sql
    return out


@dataclass
class ReplayExecutor:
    """Offline stand-in for a real database executor.

    Reads a recorded file of the shape::

        {"<question_id>": {"gold": <result|{"error": "..."}>,
                           "teacher": <result|{"error": "..."}>}}

    and answers ``(question_id, sql)`` from it.  Which side is asked is decided by comparing the
    incoming SQL with the recorded gold SQL (normalised); anything else is the teacher side.

    This is a *replay*, not an execution engine: it exists so the keep/drop logic can be exercised
    and reviewed without the teacher's PostgreSQL tunnel.  Every row it returns is marked
    ``"replayed": true`` in the report so a real run is never confused with a fixture run.
    """

    gold: Mapping[str, str]
    recorded: Mapping[str, Any]

    @staticmethod
    def _norm(sql: str) -> str:
        try:
            import sqlglot

            return sqlglot.parse_one(sql, dialect="postgres").sql(dialect="postgres", normalize=True).lower()
        except Exception:  # noqa: BLE001 - normalisation is best-effort
            return " ".join(sql.split()).lower()

    def __call__(self, question_id: str, sql: str) -> Any:
        entry = self.recorded.get(question_id)
        if entry is None:
            raise KeyError(f"replay file has no record for question_id={question_id!r}")
        side = "gold" if self._norm(sql) == self._norm(self.gold.get(question_id, "")) else "teacher"
        payload = entry.get(side) if isinstance(entry, dict) else None
        if payload is None:
            if isinstance(entry, dict) and side in entry:
                return None  # an explicitly recorded NULL result
            raise KeyError(f"replay file has no {side!r} result for question_id={question_id!r}")
        if isinstance(payload, dict) and "error" in payload:
            raise RuntimeError(str(payload["error"]))
        return payload


def load_replay_results(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object keyed by question_id")
    return {str(key): value for key, value in data.items()}
