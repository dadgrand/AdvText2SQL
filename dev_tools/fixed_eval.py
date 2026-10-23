"""Trustworthy, offline measurement for the BIRD benchmark used in this repo.

Why this module exists
======================

``benchmarks/evaluate_bird.py`` is the shipped evaluator.  Four defects make it
under-report every submission:

1. It rewrites gold SQL with a *regex* (``evaluate_bird.py:28``)::

       re.sub(r"(\\b\\w+\\b)\\s*/\\s*(\\b\\w+\\b)", r"\\1 / NULLIF(\\2,0)", query_pg)

   applied **after** ``sqlglot`` has already converted every division into
   ``<numerator> / NULLIF(<denominator>, 0)`` (the ``sqlite -> postgres``
   transpile marks divisions as "safe" and the PostgreSQL generator guards
   them).  The regex therefore eats the ``NULLIF`` token itself and emits
   ``... / NULLIF(NULLIF,0)(COUNT(T1.id), 0)``.  PostgreSQL cannot parse that,
   so **gold never executes** and every submission is scored 0 on those
   questions, silently (the ``SQLAlchemyError`` branch records ``score = 0``).
   Measured with ``sqlglot 30.23.0``: **30 of 241** ``bird_large`` gold queries
   and **2 of 22** ``bird_small`` gold queries are destroyed this way, i.e. the
   harness can score at most 211 of 241 questions.
2. The same regex also fires inside string literals and quoted identifiers that
   contain a slash (``"Charter School (Y/N)"``, ``'... and/or ...'``): **3 more**
   ``bird_large`` gold queries still parse but no longer mean the same thing, so
   a perfect PostgreSQL submission tops out at **208/241 = 86.3 %**, not 100 %.
3. It never transpiles predictions (``evaluate_bird.py:78`` is commented out),
   so a model that imitates the BIRD gold style (backticked identifiers,
   ``IIF``/``STRFTIME``/``IFNULL``) is punished for being stylistically correct.
4. It reports a single accuracy number, with no confidence interval, no
   per-difficulty interval, no secondary metric and no paired test — so a
   two-point difference cannot be told apart from noise.

What this module provides
=========================

* ``repo_rewrite`` — a **verbatim** copy of the shipped rewrite, used only by
  ``mode="local"`` so that our numbers stay comparable with the harness.
* ``sqlite_to_postgres_safe`` — an AST-based rewrite: parse as SQLite, walk
  ``exp.Div`` nodes and wrap **only** column/expression denominators in
  ``NULLIF(denominator, 0)``, then generate PostgreSQL.  It never touches the
  text with a regex, it is idempotent, and it preserves identifier case and
  quoting (backticks become ANSI double quotes).
* ``gold_damage_report`` — the programmatic reproduction of the 30/241 (and
  2/22) damaged gold queries, each with the reason it fails.
* ``gold_silent_corruption_report`` — the quieter second failure mode: 3 more
  ``bird_large`` gold queries survive the regex but are *mutated* inside string
  literals or quoted identifiers containing a slash (``'... and/or ...'``,
  ``"Charter School (Y/N)"``), so a perfect PostgreSQL submission tops out at
  208/241 in ``mode="local"``.
* ``score_predictions`` / ``report`` — EX over all questions (EX/241), EX over
  gold-executable questions (EX/211), per-difficulty EX with Wilson 95 % CIs,
  the count of unexecutable gold items, mini_dev cell-level soft-F1 and a
  paired block (b, c, exact McNemar p, seeded paired-bootstrap CI) against a
  second predictions file.
* ``soft_f1`` / ``cell_counts`` — the mini_dev cell-level definition, including
  its documented worked example (tp=4, fp=1, fn=1 -> F1 = 0.8).

Non-invasiveness
================

This module imports **nothing** from ``benchmarks/``, ``data/`` or ``src/``.
Execution is injected: ``score_predictions(..., execute=fn)`` where
``fn(sql, db_id) -> list[tuple]``.  A database is needed only for the CLI path
(``--db-url``) — every unit test runs fully in-process.

Tested against ``sqlglot 30.23.0`` (the version pinned by the plan; the
``sqlglot`` dependency of the repo itself is unpinned, so record the version
next to every number this module prints).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import sys
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import sqlglot
from sqlglot import exp

__all__ = [
    "MODES",
    "MEASURED_DAMAGE",
    "cell_counts",
    "expected_n_damaged",
    "evaluate_predictions",
    "gold_damage_report",
    "gold_silent_corruption_report",
    "main",
    "mcnemar_exact_p",
    "paired_bootstrap_ci",
    "paired_comparison",
    "repo_rewrite",
    "report",
    "score_predictions",
    "soft_f1",
    "sqlglot_transpile_only",
    "sqlite_to_postgres_safe",
    "wilson_ci",
]

SQLGLOT_VERSION: str = sqlglot.__version__

MODES: Tuple[str, str, str] = ("local", "fixed", "normalized")

#: Two-sided 95 % normal quantile, used for Wilson intervals.
Z_95: float = 1.959964
DEFAULT_N_BOOT: int = 10_000
DEFAULT_SEED: int = 0

#: Measured number of gold queries destroyed by the repo's own rewrite, per
#: answers file and sqlglot version (provenance: captain_checks.py run recorded
#: in research/advtext2sql/captain-verified-numbers.md).  Used only as a
#: self-check label; ``gold_damage_report`` always returns what it measures.
MEASURED_DAMAGE: Dict[Tuple[str, str], int] = {
    ("bird_large.json", "30.23.0"): 30,
    ("bird_small.json", "30.23.0"): 2,
    ("train_queries.json", "30.23.0"): 14,
}

Executor = Callable[[str, Optional[str]], List[Sequence[Any]]]


# ---------------------------------------------------------------------------
# Parsing / loading helpers
# ---------------------------------------------------------------------------


def _load_json(source: Any) -> Any:
    """Load a JSON path (``str``/``PathLike``) or pass an already-loaded object."""

    if isinstance(source, (str, os.PathLike)):
        with open(source, "r", encoding="utf-8") as handle:
            return json.load(handle)
    return source


def _source_label(source: Any) -> str:
    if isinstance(source, (str, os.PathLike)):
        return os.fspath(source)
    return "<in-memory>"


def _answers_rows(answers: Any) -> List[dict]:
    rows = _load_json(answers)
    if isinstance(rows, dict):  # tolerate {"rows": [...]}
        rows = rows.get("rows", list(rows.values()))
    if not isinstance(rows, list):
        raise TypeError(f"answers must be a list of objects, got {type(rows).__name__}")
    return rows


def _is_ambiguous(sql: Any) -> bool:
    return isinstance(sql, str) and sql.strip().lower() == "ambiguous"


def _strip_fences_and_comments(query: str) -> str:
    """Same pre-processing as ``benchmarks/evaluate_bird.py:17-18``."""

    query = re.sub(r"```sql|```", "", query, flags=re.IGNORECASE)
    query = re.sub(r"/\*.*?\*/", "", query, flags=re.DOTALL)
    return query.strip()


def _pg_parses(sql: str) -> bool:
    try:
        sqlglot.parse_one(sql, dialect="postgres")
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# The shipped rewrite (verbatim) and its AST-based replacement
# ---------------------------------------------------------------------------


def repo_rewrite(query: str) -> str:
    """Verbatim copy of ``benchmarks/evaluate_bird.py:15-30`` (``sqlite_to_postgres``).

    Kept byte-for-byte equivalent in behaviour so that ``mode="local"``
    reproduces the shipped harness exactly.  The only difference is that the
    original prints ``"SQLGlot parse error:"`` on a transpile failure; this copy
    stays side-effect free (the failure is still visible: the raw query is
    returned and will not parse as PostgreSQL).
    """

    query = re.sub(r"```sql|```", "", query, flags=re.IGNORECASE)
    query = re.sub(r"/\*.*?\*/", "", query, flags=re.DOTALL).strip()

    try:
        query_pg = sqlglot.transpile(query, read="sqlite", write="postgres")[0]
    except Exception:
        query_pg = query

    return re.sub(r"(\b\w+\b)\s*/\s*(\b\w+\b)", r"\1 / NULLIF(\2,0)", query_pg)


def sqlglot_transpile_only(query: str) -> str:
    """``sqlglot`` transpile with the repo's pre-processing and no NULLIF regex.

    Used to classify *why* a gold query is destroyed: if this output parses as
    PostgreSQL but ``repo_rewrite`` does not, the regex is the culprit.
    """

    cleaned = _strip_fences_and_comments(query)
    return sqlglot.transpile(cleaned, read="sqlite", write="postgres")[0]


def _is_number_literal(node: exp.Expression) -> bool:
    return isinstance(node, exp.Literal) and not node.is_string


def sqlite_to_postgres_safe(query: str) -> str:
    """AST-based SQLite -> PostgreSQL rewrite with zero-division guards.

    * Parse with ``read="sqlite"`` and generate with ``dialect="postgres"`` —
      never a regex, so ``a.x / b.y`` keeps its qualified name.
    * Every ``exp.Div`` whose denominator is a column or an expression gets
      ``NULLIF(<denominator>, 0)``, mirroring SQLite's "division by zero yields
      NULL" semantics.  The guard is added through the AST, so the denominator
      is wrapped as a whole (``NULLIF(T1.Amount, 0)``) and never
      ``NULLIF(T1, 0).Amount``.
    * The guard is idempotent: a denominator that already is ``NULLIF(x, 0)`` is
      left alone, and the ``safe`` flag is cleared on wrapped divisions so the
      PostgreSQL generator does not add a second ``NULLIF`` around ours.
    * Plain numeric literals are left to the generator (a literal denominator
      cannot be a column, and sqlglot's own handling of ``1 / 2`` matches
      SQLite's integer division).  String literals are treated as expressions
      and get a guard, because ``'a' / 'b'`` is an expression division.
    * Identifier case is preserved (sqlglot's default) and backticked
      identifiers become ANSI double-quoted identifiers, i.e. the correct
      PostgreSQL spelling of a BIRD gold query.

    Raises ``sqlglot.errors.ParseError`` if ``query`` is not valid SQLite SQL.
    """

    tree = sqlglot.parse_one(_strip_fences_and_comments(query), read="sqlite")

    for division in tree.find_all(exp.Div):
        denominator = division.expression
        if denominator is None:
            continue
        if isinstance(denominator, exp.Nullif):
            # Already guarded, keep exactly one NULLIF.  The SQLite parser marks
            # every division as "safe", so clear the flag or the PostgreSQL
            # generator would wrap our guard a second time.
            division.set("safe", False)
            continue
        if _is_number_literal(denominator):
            # A non-column literal; the generator's own handling is enough.
            continue
        division.set(
            "expression",
            exp.Nullif(this=denominator.copy(), expression=exp.Literal.number(0)),
        )
        division.set("safe", False)

    return tree.sql(dialect="postgres")


def _rewrite(sql: str, mode: str) -> str:
    """Apply the rewrite that ``mode`` prescribes to a *gold* query."""

    if mode == "local":
        return repo_rewrite(sql)
    return sqlite_to_postgres_safe(sql)


# ---------------------------------------------------------------------------
# Gold-damage audit
# ---------------------------------------------------------------------------

_CORRUPTION_PATTERNS = (
    # NULLIF(<fn>(...), 0) eaten by the regex: NULLIF(NULLIF,0)(...)
    re.compile(r"NULLIF\(\s*NULLIF\s*,\s*0\s*\)\s*\("),
    # qualified name eaten: NULLIF(a,0).b
    re.compile(r"NULLIF\([^()]*,\s*0\)\s*\."),
    # function name eaten: NULLIF(COUNT,0)(...)
    re.compile(r"NULLIF\([A-Za-z_][A-Za-z_0-9]*\s*,\s*0\)\s*\("),
)


def _corruption_evidence(sql: str, width: int = 70) -> Optional[str]:
    """Return a short snippet around the first regex corruption, if any."""

    for pattern in _CORRUPTION_PATTERNS:
        match = pattern.search(sql)
        if match:
            start = max(0, match.start() - width)
            end = min(len(sql), match.end() + width)
            prefix = "..." if start > 0 else ""
            suffix = "..." if end < len(sql) else ""
            return f"{prefix}{sql[start:end]}{suffix}"
    return None


def gold_damage_report(answers: Any) -> List[dict]:
    """Every gold query that the repo's own rewrite destroys.

    ``answers`` is a path to an answers file (e.g. ``data/bird_large.json``) or
    the already-loaded list.  A gold query counts as destroyed when the string
    produced by ``repo_rewrite`` cannot be parsed as PostgreSQL — which, since
    the harness executes exactly that string, means it cannot execute and every
    submission is scored 0 on that question.

    Each item is a dict with the keys required by the plan plus the evidence
    needed to explain the failure::

        {
          "question_id": "85",
          "gold": "<original BIRD gold SQL>",
          "rewritten": "<repo_rewrite(gold)>",
          "parse_error": "ParseError: Expecting ). Line 1, Col: 63.",
          "damage_kind": "nullif_regex_corruption" | "transpile_unusable",
          "regex_evidence": "<snippet around the corrupt NULLIF>" | None,
          "safe_rewritten": "<sqlite_to_postgres_safe(gold)>",
          "safe_parse_error": None,
        }
    """

    items: List[dict] = []
    for row in _answers_rows(answers):
        gold = row.get("SQL")
        if not isinstance(gold, str) or _is_ambiguous(gold):
            continue

        try:
            rewritten = repo_rewrite(gold)
        except Exception as exc:  # defensive: the shipped code falls back internally
            items.append(
                {
                    "question_id": str(row.get("question_id")),
                    "gold": gold,
                    "rewritten": None,
                    "parse_error": f"{type(exc).__name__}: {exc}",
                    "damage_kind": "transpile_unusable",
                    "regex_evidence": None,
                    "safe_rewritten": None,
                    "safe_parse_error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue

        try:
            sqlglot.parse_one(rewritten, dialect="postgres")
            continue  # survives the repo rewrite
        except Exception as exc:
            parse_error = f"{type(exc).__name__}: {exc}"

        transpile_only = None
        transpile_only_error = None
        try:
            transpile_only = sqlglot_transpile_only(gold)
            sqlglot.parse_one(transpile_only, dialect="postgres")
            transpile_ok = True
        except Exception as exc:
            transpile_ok = False
            transpile_only_error = f"{type(exc).__name__}: {exc}"

        damage_kind = "nullif_regex_corruption" if transpile_ok else "transpile_unusable"

        safe_rewritten = None
        safe_parse_error = None
        try:
            safe_rewritten = sqlite_to_postgres_safe(gold)
            sqlglot.parse_one(safe_rewritten, dialect="postgres")
        except Exception as exc:
            safe_parse_error = f"{type(exc).__name__}: {exc}"

        items.append(
            {
                "question_id": str(row.get("question_id")),
                "gold": gold,
                "rewritten": rewritten,
                "parse_error": parse_error,
                "damage_kind": damage_kind,
                "regex_evidence": _corruption_evidence(rewritten)
                or (transpile_only_error if not transpile_ok else None),
                "safe_rewritten": safe_rewritten,
                "safe_parse_error": safe_parse_error,
            }
        )

    return items


def expected_n_damaged(answers_source: Any) -> Optional[int]:
    """Measured expectation from ``MEASURED_DAMAGE`` for this answers file, if known."""

    name = os.path.basename(_source_label(answers_source))
    return MEASURED_DAMAGE.get((name, SQLGLOT_VERSION))


def _ast_signature(sql: str) -> str:
    """Canonical PostgreSQL rendering, used to compare two rewrites semantically."""

    return sqlglot.parse_one(sql, dialect="postgres").sql(dialect="postgres")


def gold_silent_corruption_report(answers: Any) -> List[dict]:
    """Gold queries the repo's regex *changes* without breaking the parse.

    A second, quieter failure mode: the regex also fires inside string literals
    and quoted identifiers that contain a slash (``'... Weka and/or RapidMiner
    ...'``, ``"Charter School (Y/N)"``).  The result is still valid PostgreSQL —
    so the query executes — but it no longer answers the question, and every
    submission is scored against the mutated version.

    Measured on ``data/bird_large.json`` with ``sqlglot 30.23.0``: **3 of 241**
    (ids ``4``, ``35``, ``572``).  Together with the 30 unexecutable items this
    is why a *perfect* PostgreSQL submission tops out at 208/241 under the
    shipped evaluator (``mode="local"``), not at 100 %.

    Detection: both rewrite outputs are re-parsed as PostgreSQL and rendered
    canonically; a difference means the regex changed the query's AST.
    """

    items: List[dict] = []
    for row in _answers_rows(answers):
        gold = row.get("SQL")
        if not isinstance(gold, str) or _is_ambiguous(gold):
            continue
        try:
            rewritten = repo_rewrite(gold)
            sqlglot.parse_one(rewritten, dialect="postgres")
            safe_rewritten = sqlite_to_postgres_safe(gold)
            changed = _ast_signature(rewritten) != _ast_signature(safe_rewritten)
        except Exception:
            continue
        if not changed:
            continue
        items.append(
            {
                "question_id": str(row.get("question_id")),
                "gold": gold,
                "rewritten": rewritten,
                "safe_rewritten": safe_rewritten,
            }
        )
    return items


# ---------------------------------------------------------------------------
# Row handling, EX and soft-F1
# ---------------------------------------------------------------------------


def _hashable(value: Any) -> Any:
    """Make a database cell hashable, keeping equality semantics intact."""

    if isinstance(value, (list, tuple)):
        return tuple(_hashable(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((str(k), _hashable(v)) for k, v in value.items()))
    if isinstance(value, (set, frozenset)):
        return tuple(sorted(_hashable(v) for v in value))
    return value


def _norm_rows(rows: Iterable[Sequence[Any]] | None) -> List[Tuple[Any, ...]]:
    if rows is None:
        return []
    return [tuple(_hashable(v) for v in row) for row in rows]


def _row_key(rows: Iterable[Sequence[Any]] | None) -> frozenset:
    """BIRD EX semantics: set equality of row tuples (order- and duplicate-blind)."""

    return frozenset(_norm_rows(rows))


def cell_counts(
    pred_rows: Iterable[Sequence[Any]] | None,
    gold_rows: Iterable[Sequence[Any]] | None,
    order_insensitive: bool = False,
) -> Tuple[int, int, int]:
    """mini_dev soft-F1 cell counts ``(tp, fp, fn)``.

    The metric is cell-level, computed row by row over the aligned rows
    (row *i* of the prediction against row *i* of the gold table).  Inside a row
    the cells are compared as a set — this is what the mini_dev worked example
    implies, and it matches the set-based philosophy of BIRD EX:

    * ``tp`` — cells present in both the predicted and the gold row,
    * ``fp`` — cells only in the predicted row,
    * ``fn`` — cells only in the gold row.

    ``None`` cells are ignored on both sides (the README example counts only the
    two non-empty cells of a row that has an empty cell).  When the two tables
    have a different number of rows the shorter one is padded with empty rows,
    so the extra cells become ``fp`` / ``fn``.

    With ``order_insensitive=True`` both tables are sorted canonically before
    alignment, which makes the metric blind to row order (EX already is).
    """

    pred = _norm_rows(pred_rows)
    gold = _norm_rows(gold_rows)

    if order_insensitive:
        def sort_key(row: Tuple[Any, ...]) -> Tuple[str, ...]:
            return tuple(sorted(repr(cell) for cell in row))

        pred = sorted(pred, key=sort_key)
        gold = sorted(gold, key=sort_key)

    tp = fp = fn = 0
    for index in range(max(len(pred), len(gold))):
        pred_cells = {cell for cell in (pred[index] if index < len(pred) else ()) if cell is not None}
        gold_cells = {cell for cell in (gold[index] if index < len(gold) else ()) if cell is not None}
        tp += len(pred_cells & gold_cells)
        fp += len(pred_cells - gold_cells)
        fn += len(gold_cells - pred_cells)
    return tp, fp, fn


def soft_f1(
    pred_rows: Iterable[Sequence[Any]] | None,
    gold_rows: Iterable[Sequence[Any]] | None,
    order_insensitive: bool = False,
) -> float:
    """mini_dev cell-level soft F1 in ``[0, 1]``.

    ``F1 = 2PR / (P + R)`` with ``P = tp/(tp+fp)`` and ``R = tp/(tp+fn)``.
    Two empty tables score 1.0 (they match, exactly like EX); a degenerate
    precision or recall with ``tp == 0`` scores 0.0.
    """

    tp, fp, fn = cell_counts(pred_rows, gold_rows, order_insensitive=order_insensitive)
    precision = tp / (tp + fp) if (tp + fp) else (1.0 if fn == 0 else 0.0)
    recall = tp / (tp + fn) if (tp + fn) else (1.0 if fp == 0 else 0.0)
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def wilson_ci(k: int, n: int, z: float = Z_95) -> Tuple[Optional[float], Optional[float]]:
    """Wilson score interval for ``k`` successes out of ``n`` (no continuity fix).

    Hand-check: ``wilson_ci(11, 22) == (0.30718, 0.69282)``.
    """

    if n <= 0:
        return (None, None)
    if k < 0 or k > n:
        raise ValueError(f"k={k} outside [0, {n}]")
    p = k / n
    denominator = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / denominator
    half = (z / denominator) * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    low = max(0.0, centre - half)
    high = min(1.0, centre + half)
    return (low, high)


def mcnemar_exact_p(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value for discordant counts ``b`` and ``c``.

    ``p = min(1, 2 * sum_{k=0}^{min(b,c)} C(b+c, k) * 0.5**(b+c))``, i.e. the
    exact binomial test of ``b`` against ``Binomial(b+c, 0.5)``.  Hand-check:
    ``mcnemar_exact_p(6, 0) == 0.03125``.
    """

    if b < 0 or c < 0:
        raise ValueError("b and c must be non-negative")
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, k) for k in range(min(b, c) + 1)) / (2.0**n)
    return min(1.0, 2.0 * tail)


def _percentile(sorted_values: Sequence[float], q: float) -> Optional[float]:
    """Linear-interpolation percentile on an already sorted sequence."""

    if not sorted_values:
        return None
    if q <= 0:
        return sorted_values[0]
    if q >= 1:
        return sorted_values[-1]
    position = (len(sorted_values) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * weight


def paired_bootstrap_ci(
    deltas: Sequence[float],
    n_boot: int = DEFAULT_N_BOOT,
    seed: int = DEFAULT_SEED,
    level: float = 0.95,
) -> Tuple[Optional[float], Optional[float]]:
    """Paired percentile-bootstrap CI for the mean of per-question differences.

    ``deltas`` must be one value per *question id*; resampling is over question
    ids (with replacement), which is the paired design the plan requires.
    Deterministic: same ``deltas``, ``n_boot`` and ``seed`` -> same interval.
    """

    if n_boot <= 0:
        raise ValueError("n_boot must be positive")
    size = len(deltas)
    if size == 0:
        return (None, None)
    rng = random.Random(seed)
    means: List[float] = []
    for _ in range(n_boot):
        sample = rng.choices(deltas, k=size)
        means.append(sum(sample) / size)
    means.sort()
    alpha = (1.0 - level) / 2.0
    return (_percentile(means, alpha), _percentile(means, 1.0 - alpha))


def paired_comparison(
    current_records: Sequence[dict],
    baseline_records: Sequence[dict],
    n_boot: int = DEFAULT_N_BOOT,
    seed: int = DEFAULT_SEED,
    baseline_source: Optional[str] = None,
) -> dict:
    """Paired ledger of two runs over the same question ids.

    ``b`` = current right / baseline wrong, ``c`` = current wrong / baseline
    right (the standard McNemar layout), plus the exact McNemar p-value and a
    seeded paired-bootstrap 95 % CI for ``delta_ex``.
    """

    current = {r["question_id"]: r for r in current_records}
    baseline = {r["question_id"]: r for r in baseline_records}
    paired_ids = [r["question_id"] for r in current_records if r["question_id"] in baseline]

    b = sum(1 for qid in paired_ids if current[qid]["score"] == 1 and baseline[qid]["score"] == 0)
    c = sum(1 for qid in paired_ids if current[qid]["score"] == 0 and baseline[qid]["score"] == 1)
    deltas = [current[qid]["score"] - baseline[qid]["score"] for qid in paired_ids]
    low, high = paired_bootstrap_ci(deltas, n_boot=n_boot, seed=seed)

    return {
        "baseline": baseline_source,
        "n_paired": len(paired_ids),
        "b": b,
        "c": c,
        "discordant": b + c,
        "mcnemar_exact_p": mcnemar_exact_p(b, c),
        "delta_ex": (sum(deltas) / len(deltas)) if deltas else None,
        "bootstrap95": [low, high],
        "n_boot": n_boot,
        "seed": seed,
        "missing_in_baseline": sorted(set(current) - set(baseline)),
        "missing_in_current": sorted(set(baseline) - set(current)),
    }


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


class PostgresExecutor:
    """Default executor: run each query against the benchmark PostgreSQL.

    Mirrors ``benchmarks/base.py``: one database per ``db_id``, DSN
    ``postgresql+psycopg://{DB_USER}:{DB_PASS}@{db_url}/{db_id}``.  Imported
    lazily so that the module — and every unit test — works without SQLAlchemy
    or a database.
    """

    def __init__(self, db_url: str, user: Optional[str] = None, password: Optional[str] = None):
        self.db_url = db_url
        self.user = user if user is not None else os.environ.get("DB_USER")
        self.password = password if password is not None else os.environ.get("DB_PASS")
        if not self.user or not self.password:
            raise RuntimeError(
                "PostgresExecutor needs DB_USER and DB_PASS (env vars or constructor args)"
            )
        self._engines: Dict[str, Any] = {}

    def _engine(self, db_id: str) -> Any:
        if db_id not in self._engines:
            from sqlalchemy import create_engine  # noqa: PLC0415 (lazy on purpose)

            # The benchmark password contains "/" (and may contain @:#?), so it must be
            # percent-encoded or the DSN is malformed.  Same rule as
            # dev_tools/dump_schemas.postgres_dsn.
            from urllib.parse import quote  # noqa: PLC0415

            user = quote(str(self.user), safe="")
            password = quote(str(self.password), safe="")
            dsn = f"postgresql+psycopg://{user}:{password}@{self.db_url}/{db_id}"
            self._engines[db_id] = create_engine(dsn)
        return self._engines[db_id]

    def __call__(self, sql: str, db_id: Optional[str]) -> List[Sequence[Any]]:
        from sqlalchemy import text  # noqa: PLC0415

        if db_id is None:
            raise ValueError("db_id is required to pick a benchmark database")
        with self._engine(db_id).connect() as connection:
            return [tuple(row) for row in connection.execute(text(sql)).fetchall()]


def _resolve_executor(execute: Optional[Executor], db_url: Optional[str]) -> Executor:
    if execute is not None:
        return execute
    if db_url:
        return PostgresExecutor(db_url)
    raise ValueError(
        "no executor: pass execute=<callable> (offline tests) or db_url=<host:port> "
        "(and DB_USER/DB_PASS) to run against the benchmark PostgreSQL"
    )


def evaluate_predictions(
    predictions: Any,
    answers: Any,
    execute: Executor,
    mode: str = "local",
) -> List[dict]:
    """Per-question ledger for one predictions file (no aggregation).

    Returns one dict per question in the answers file, in file order, with
    ``score`` (0/1), ``gold_ok`` / ``pred_ok``, the error strings, and
    ``soft_f1`` when both sides executed.
    """

    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")

    answer_rows = _answers_rows(answers)
    preds = _load_json(predictions)
    if not isinstance(preds, dict):
        raise TypeError(
            f"predictions must be a mapping {{question_id: sql}} (query_results.json), "
            f"got {type(preds).__name__}"
        )

    records: List[dict] = []
    for item in answer_rows:
        qid = str(item.get("question_id"))
        gold_raw = item.get("SQL")
        db_id = item.get("db_id")
        difficulty = item.get("difficulty")
        pred_raw = preds.get(qid)

        record: dict = {
            "question_id": qid,
            "db_id": db_id,
            "difficulty": difficulty,
            "gold_sql": gold_raw,
            "pred_sql": pred_raw,
            "status": None,
            "score": 0,
            "gold_ok": False,
            "gold_error": None,
            "pred_ok": False,
            "pred_error": None,
            "soft_f1": None,
        }

        # ---- gold side: rewrite (mode-dependent) then execute -------------
        gold_rows: Optional[List[Tuple[Any, ...]]] = None
        gold_status = "ok"
        if _is_ambiguous(gold_raw):
            gold_status = "ambiguous"
            record["gold_ok"] = True  # nothing to execute; comparison is defined
        else:
            try:
                gold_sql = _rewrite(gold_raw, mode)
            except Exception as exc:
                gold_status = "error"
                record["gold_error"] = f"rewrite failed: {type(exc).__name__}: {exc}"
            else:
                try:
                    gold_rows = _norm_rows(execute(gold_sql, db_id))
                    record["gold_ok"] = True
                except Exception as exc:
                    gold_status = "error"
                    record["gold_error"] = f"{type(exc).__name__}: {exc}"

        # ---- the shipped evaluator's ambiguity branch (parity) ------------
        pred_is_ambiguous = _is_ambiguous(pred_raw)
        if gold_status == "ambiguous" or pred_is_ambiguous:
            record["status"] = "ambiguous"
            record["pred_ok"] = pred_is_ambiguous
            record["score"] = int(gold_status == "ambiguous" and pred_is_ambiguous)
            records.append(record)
            continue

        if pred_raw is None:
            record["status"] = "missing_prediction"
            records.append(record)
            continue

        if gold_status == "error":
            record["status"] = "gold_error"
            records.append(record)
            continue

        # ---- prediction side: optionally normalise, then execute ----------
        if not isinstance(pred_raw, str):
            record["status"] = "pred_error"
            record["pred_error"] = f"prediction is not a string: {type(pred_raw).__name__}"
            records.append(record)
            continue

        try:
            pred_sql = sqlite_to_postgres_safe(pred_raw) if mode == "normalized" else pred_raw
        except Exception as exc:
            record["status"] = "pred_error"
            record["pred_error"] = f"rewrite failed: {type(exc).__name__}: {exc}"
            records.append(record)
            continue

        try:
            pred_rows = _norm_rows(execute(pred_sql, db_id))
            record["pred_ok"] = True
        except Exception as exc:
            record["status"] = "pred_error"
            record["pred_error"] = f"{type(exc).__name__}: {exc}"
            records.append(record)
            continue

        record["status"] = "scored"
        record["score"] = int(_row_key(pred_rows) == _row_key(gold_rows))
        record["soft_f1"] = soft_f1(pred_rows, gold_rows)
        records.append(record)

    return records


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _accuracy_block(correct: int, n: int) -> dict:
    value = correct / n if n else None
    low, high = wilson_ci(correct, n)
    return {
        "correct": correct,
        "n": n,
        "value": value,
        "percent": (100.0 * value) if value is not None else None,
        "wilson95": [low, high],
    }


def _group_block(records: Sequence[dict], key: str) -> Dict[str, dict]:
    group: Dict[str, List[dict]] = {}
    for record in records:
        group.setdefault(str(record.get(key)), []).append(record)
    out: Dict[str, dict] = {}
    for name in sorted(group):
        subset = group[name]
        block = _accuracy_block(sum(r["score"] for r in subset), len(subset))
        out[name] = {
            "n": block["n"],
            "correct": block["correct"],
            "ex": block["value"],
            "percent": block["percent"],
            "wilson95": block["wilson95"],
        }
    return out


def report(
    records: Sequence[dict],
    *,
    mode: str,
    answers_source: Any = None,
    predictions_source: Any = None,
    baseline_records: Optional[Sequence[dict]] = None,
    baseline_source: Any = None,
    n_boot: int = DEFAULT_N_BOOT,
    seed: int = DEFAULT_SEED,
    include_per_question: bool = True,
) -> dict:
    """Aggregate a per-question ledger into the four reported numbers.

    * ``ex_all`` — EX over **all** questions in the answers file (EX/241), the
      number the shipped harness prints;
    * ``ex_gold_executable`` — EX over questions whose gold SQL actually
      executed (EX/211), the number that is not capped by the harness bug;
    * ``per_difficulty`` — EX per difficulty with Wilson 95 % CI;
    * ``paired`` — b/c, exact McNemar p and the paired-bootstrap CI against a
      second predictions file (``None`` when no baseline was supplied).

    Also carries ``n_gold_unexecutable`` (the size of the ceiling), prediction
    failure counts, soft-F1 means and per-database counts (counts only — the
    per-database groups are too small for significance).
    """

    n_total = len(records)
    correct = sum(r["score"] for r in records)
    executable = [r for r in records if r["gold_ok"]]
    correct_exec = sum(r["score"] for r in executable)
    unexecutable = [r for r in records if not r["gold_ok"]]

    soft_scores = [r["soft_f1"] for r in records if r["status"] == "scored" and r["soft_f1"] is not None]
    soft_mean = sum(soft_scores) / len(soft_scores) if soft_scores else None
    non_ambiguous = [r for r in records if r["status"] != "ambiguous"]
    soft_mean_all = (
        sum(r["soft_f1"] or 0.0 for r in non_ambiguous) / len(non_ambiguous)
        if non_ambiguous
        else None
    )

    per_question = [
        {
            "question_id": r["question_id"],
            "db_id": r["db_id"],
            "difficulty": r["difficulty"],
            "status": r["status"],
            "score": r["score"],
            "gold_ok": r["gold_ok"],
            "pred_ok": r["pred_ok"],
            "gold_error": r["gold_error"],
            "pred_error": r["pred_error"],
            "soft_f1": r["soft_f1"],
        }
        for r in records
    ]

    result: dict = {
        "tool": "dev_tools.fixed_eval",
        "sqlglot_version": SQLGLOT_VERSION,
        "mode": mode,
        "answers": _source_label(answers_source) if answers_source is not None else None,
        "predictions": _source_label(predictions_source) if predictions_source is not None else None,
        "n_questions": n_total,
        "ex_all": _accuracy_block(correct, n_total),
        "ex_gold_executable": _accuracy_block(correct_exec, len(executable)),
        "n_gold_unexecutable": len(unexecutable),
        "gold_unexecutable_ids": [r["question_id"] for r in unexecutable],
        "gold_unexecutable_reasons": {
            r["question_id"]: r["gold_error"] for r in unexecutable
        },
        "n_pred_unexecutable": sum(1 for r in records if r["status"] == "pred_error"),
        "n_missing_predictions": sum(1 for r in records if r["status"] == "missing_prediction"),
        "n_ambiguous": sum(1 for r in records if r["status"] == "ambiguous"),
        "per_difficulty": _group_block(records, "difficulty"),
        "per_database": _group_block(records, "db_id"),
        "soft_f1_mean": soft_mean,
        "soft_f1_mean_all": soft_mean_all,
        "paired": (
            paired_comparison(
                records,
                baseline_records,
                n_boot=n_boot,
                seed=seed,
                baseline_source=_source_label(baseline_source) if baseline_source is not None else None,
            )
            if baseline_records is not None
            else None
        ),
        "n_boot": n_boot,
        "seed": seed,
    }
    if include_per_question:
        result["per_question"] = per_question
    return result


def score_predictions(
    predictions: Any,
    answers: Any,
    db_url: Optional[str] = None,
    mode: str = "local",
    execute: Optional[Executor] = None,
    *,
    baseline: Any = None,
    baseline_execute: Optional[Executor] = None,
    n_boot: int = DEFAULT_N_BOOT,
    seed: int = DEFAULT_SEED,
    include_per_question: bool = True,
) -> dict:
    """Score one predictions file and return the full report.

    Parameters
    ----------
    predictions:
        ``{question_id: sql}`` mapping (the format of ``query_results.json``) or
        a path to that JSON file.
    answers:
        Path to an answers file (``data/bird_large.json``) or the loaded list.
    db_url / execute:
        Exactly one execution route is needed.  ``execute(sql, db_id) ->
        list[tuple]`` is the offline injection point used by the tests;
        ``db_url`` builds a :class:`PostgresExecutor` (needs ``DB_USER`` and
        ``DB_PASS``).
    mode:
        ``"local"``     — exactly what the shipped evaluator does: gold through
                          ``repo_rewrite``, predictions executed as-is;
        ``"fixed"``     — gold through ``sqlite_to_postgres_safe``, predictions
                          executed as-is (isolates the ceiling);
        ``"normalized"``— the safe rewrite on both gold and predictions
                          (isolates the dialect penalty of the predictions).
    baseline:
        Optional second predictions file for the paired block.  It is evaluated
        with the same ``mode`` and executor so that the two runs are paired.
    """

    executor = _resolve_executor(execute, db_url)
    records = evaluate_predictions(predictions, answers, executor, mode=mode)

    baseline_records = None
    if baseline is not None:
        baseline_executor = baseline_execute if baseline_execute is not None else executor
        baseline_records = evaluate_predictions(
            baseline, answers, baseline_executor, mode=mode
        )

    return report(
        records,
        mode=mode,
        answers_source=answers,
        predictions_source=predictions,
        baseline_records=baseline_records,
        baseline_source=baseline,
        n_boot=n_boot,
        seed=seed,
        include_per_question=include_per_question,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _fmt(value: Optional[float], digits: int = 4) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _one_line(text: Optional[str]) -> str:
    return " ".join(str(text).split()) if text else ""


def _fmt_p(p: Optional[float]) -> str:
    if p is None:
        return "n/a"
    if p != 0.0 and abs(p) < 1e-3:
        return f"{p:.3e}"
    return f"{p:.4f}"


def _pct(value: Optional[float], digits: int = 2) -> str:
    return "n/a" if value is None else f"{100.0 * value:.{digits}f}%"


def _print_report(result: dict, stream: Any = sys.stdout) -> None:
    ex_all = result["ex_all"]
    ex_exec = result["ex_gold_executable"]
    print(f"=== dev_tools.fixed_eval | mode={result['mode']} | sqlglot {result['sqlglot_version']} ===", file=stream)
    print(f"answers    : {result['answers']} ({result['n_questions']} questions)", file=stream)
    print(f"predictions: {result['predictions']}", file=stream)
    print("", file=stream)
    print(
        f"EX/{ex_all['n']} (all questions)     : {ex_all['correct']}/{ex_all['n']} = "
        f"{_pct(ex_all['value'])}   95% CI {_pct(ex_all['wilson95'][0])}..{_pct(ex_all['wilson95'][1])}",
        file=stream,
    )
    print(
        f"EX/{ex_exec['n']} (gold-executable)   : {ex_exec['correct']}/{ex_exec['n']} = "
        f"{_pct(ex_exec['value'])}   95% CI {_pct(ex_exec['wilson95'][0])}..{_pct(ex_exec['wilson95'][1])}",
        file=stream,
    )
    print(
        f"gold unexecutable items    : {result['n_gold_unexecutable']}"
        f"    (ids: {', '.join(result['gold_unexecutable_ids'][:12])}"
        f"{' ...' if result['n_gold_unexecutable'] > 12 else ''})",
        file=stream,
    )
    print(
        f"pred unexecutable / missing: {result['n_pred_unexecutable']} / {result['n_missing_predictions']}",
        file=stream,
    )
    print(f"soft-F1 mean (scored)      : {_fmt(result['soft_f1_mean'])}", file=stream)
    print("", file=stream)
    print("per difficulty:", file=stream)
    for difficulty, block in result["per_difficulty"].items():
        low, high = block["wilson95"]
        print(
            f"  {difficulty:<12}: {block['correct']:>3}/{block['n']:<3} = {_pct(block['ex']):>7}"
            f"   95% CI {_pct(low)}..{_pct(high)}",
            file=stream,
        )
    paired = result.get("paired")
    if paired:
        low, high = paired["bootstrap95"]
        print("", file=stream)
        print(
            f"paired vs {paired['baseline']} (n={paired['n_paired']}): "
            f"b={paired['b']} (current right, baseline wrong), c={paired['c']}; "
            f"exact McNemar p={_fmt_p(paired['mcnemar_exact_p'])}; "
            f"delta EX={_fmt(paired['delta_ex'])} 95% CI [{_fmt(low)}, {_fmt(high)}] "
            f"({paired['n_boot']} resamples, seed={paired['seed']})",
            file=stream,
        )
    print("", file=stream)


def _write_json(path: str, payload: dict) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dev_tools.fixed_eval",
        description=(
            "Offline, non-invasive BIRD evaluation: safe gold rewrite, EX/241 + EX/211, "
            "per-difficulty Wilson CIs, soft-F1 and a paired McNemar/bootstrap block."
        ),
    )
    parser.add_argument(
        "--answers",
        required=True,
        help="answers file, e.g. data/bird_large.json (the gold SQL source)",
    )
    parser.add_argument(
        "--predictions",
        help="predictions file ({question_id: sql}, e.g. query_results.json); "
        "not needed with --gold-damage",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="path of the JSON report to write (e.g. eval/report_fixed.json)",
    )
    parser.add_argument(
        "--mode",
        choices=list(MODES),
        default="fixed",
        help="local = exactly the shipped evaluator; fixed = safe gold rewrite, "
        "predictions as-is; normalized = safe rewrite on both (default: fixed)",
    )
    parser.add_argument(
        "--baseline",
        help="second predictions file for the paired block (b, c, exact McNemar p, "
        "paired-bootstrap CI)",
    )
    parser.add_argument(
        "--db-url",
        default=os.environ.get("BENCHMARK_DB_URL"),
        help="benchmark PostgreSQL host:port (default: $BENCHMARK_DB_URL); "
        "DB_USER/DB_PASS come from the environment",
    )
    parser.add_argument(
        "--username", default=os.environ.get("DB_USER"), help="database user (default: $DB_USER)"
    )
    parser.add_argument(
        "--password", default=os.environ.get("DB_PASS"), help="database password (default: $DB_PASS)"
    )
    parser.add_argument(
        "--seed", type=int, default=DEFAULT_SEED, help="bootstrap seed (default: 0)"
    )
    parser.add_argument(
        "--n-boot",
        type=int,
        default=DEFAULT_N_BOOT,
        help=f"paired bootstrap resamples (default: {DEFAULT_N_BOOT})",
    )
    parser.add_argument(
        "--gold-damage",
        action="store_true",
        help="do not score anything: report every gold query that the shipped rewrite "
        "destroys (--predictions/--db-url are then unnecessary)",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="write the JSON report without the text summary"
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.gold_damage:
        items = gold_damage_report(args.answers)
        silent = gold_silent_corruption_report(args.answers)
        by_kind: Dict[str, int] = {}
        for item in items:
            by_kind[item["damage_kind"]] = by_kind.get(item["damage_kind"], 0) + 1
        expected = expected_n_damaged(args.answers)
        payload = {
            "tool": "dev_tools.fixed_eval",
            "sqlglot_version": SQLGLOT_VERSION,
            "answers": args.answers,
            "n_questions": len(_answers_rows(args.answers)),
            "n_damaged": len(items),
            "n_damaged_expected": expected,
            "n_damaged_matches_expectation": (expected is None or expected == len(items)),
            "by_kind": by_kind,
            "items": items,
            "n_silently_corrupted": len(silent),
            "silently_corrupted_ids": [item["question_id"] for item in silent],
            "silent_corruption_items": silent,
        }
        _write_json(args.out, payload)
        if not args.quiet:
            print(
                f"=== gold damage audit | {args.answers} | sqlglot {payload['sqlglot_version']} ==="
            )
            print(
                f"gold queries destroyed by benchmarks/evaluate_bird.py:28 : "
                f"{payload['n_damaged']}/{payload['n_questions']}"
                + (
                    f"  (expected {expected})"
                    if expected is not None
                    else "  (no measured expectation for this file/version)"
                )
            )
            for item in items:
                print(
                    f"  id {item['question_id']:>5}  {item['damage_kind']}: "
                    f"{_one_line(item['parse_error'])}"
                )
                if item["regex_evidence"]:
                    print(f"         evidence: {_one_line(item['regex_evidence'])}")
            print(
                f"\ngold queries silently mutated (still parse, no longer mean the same) : "
                f"{payload['n_silently_corrupted']}"
                f"  (ids: {', '.join(payload['silently_corrupted_ids']) or 'none'})"
            )
            print(f"\nreport written to {args.out}")
        return 0

    if not args.predictions:
        parser.error("--predictions is required unless --gold-damage is given")

    if not args.db_url:
        parser.error(
            "no executor: pass --db-url (or $BENCHMARK_DB_URL) with DB_USER/DB_PASS; "
            "for offline use, call score_predictions(..., execute=...) from Python"
        )
    if not args.username or not args.password:
        parser.error(
            "DB_USER and DB_PASS must be set (environment variables or --username/--password)"
        )
    executor = PostgresExecutor(args.db_url, user=args.username, password=args.password)

    result = score_predictions(
        args.predictions,
        args.answers,
        mode=args.mode,
        execute=executor,
        baseline=args.baseline,
        n_boot=args.n_boot,
        seed=args.seed,
    )
    _write_json(args.out, result)
    if not args.quiet:
        _print_report(result)
        print(f"report written to {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())

