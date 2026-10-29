"""Optional self-consistency sampling for the Text2SQL tool - a procedure-only arm.

What it does
------------
With ``self_consistency`` enabled (OFF by default) the tool draws ``n`` candidate
SQL queries at ``temperature > 0`` with a fixed seed, executes each against the
database when one is reachable, and returns the candidate supported by the
plurality of *result sets*; when no candidate could be executed it falls back to
a plurality over the normalised SQL text. The point is a variance reduction that
is orthogonal to the adapter: repeated sampling with a majority vote can rescue
an item the single greedy draw loses.

The selection rules, in order (all deterministic)
--------------------------------------------------
1. Only candidates that passed the sqlglot gate are eligible.
2. If at least one candidate executed successfully, group the executed candidates
   by a canonical result-set signature and pick the largest group; ties go to the
   earliest candidate (lowest sampling index).
3. Otherwise group the eligible candidates by normalised SQL text and pick the
   largest group, same tie-break. This is the posture when the database is
   unreachable, and also when every candidate was rejected by it.

Nothing in this module executes SQL, sleeps or samples: it turns already-collected
candidates into a decision, which is what makes the rules testable offline. The
tool owns the sampling loop and the execution, and is required to never raise -
a failed sampling round degrades to "no SQL", exactly like a failed generation.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import sqlglot

__all__ = [
    "Candidate",
    "DEFAULT_N",
    "DEFAULT_TEMPERATURE",
    "HARD_CAP_N",
    "MAX_ROW_CAP",
    "MAX_TEMPERATURE",
    "Selection",
    "choose",
    "clamp_candidate_count",
    "normalise_sql_text",
    "result_signature",
    "sampling_args",
]

DEFAULT_N: int = 5
"""Five candidates is the usual self-consistency budget; each one is a full LLM call."""

HARD_CAP_N: int = 9
"""Ceiling even when configured higher: cost grows linearly, the vote saturates early."""

DEFAULT_TEMPERATURE: float = 0.7
"""High enough to disagree, low enough to stay SQL-shaped."""

MAX_TEMPERATURE: float = 2.0
"""The local OpenAI-compatible server refuses anything above this."""

MAX_ROW_CAP: int = 200
"""Rows per candidate kept for the comparison; enough to separate result sets."""

_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Candidate:
    """One sampled SQL string and everything known about it.

    ``execution`` is ``"ok"``, ``"rejected"`` (the database ran and refused it),
    ``"unreachable"`` (no database) or ``"skipped"`` (not attempted: an earlier
    candidate already proved the database is down, or the row cap was reached).
    """

    index: int
    sql: str | None = None
    valid: bool = False
    error: str = ""
    execution: str = "skipped"
    rows: tuple[tuple[Any, ...], ...] | None = None
    execution_error: str = ""

    def metadata(self) -> dict[str, Any]:
        """Artefact-facing description; SQL is included, rows are not (size)."""

        return {
            "index": self.index,
            "valid": self.valid,
            "sql": self.sql,
            "error": self.error,
            "execution": self.execution,
            "execution_error": self.execution_error,
            "row_count": None if self.rows is None else len(self.rows),
        }


@dataclass(frozen=True)
class Selection:
    """The chosen candidate plus the vote structure that produced it."""

    sql: str | None
    mode: str
    """``result-set-majority``, ``sql-text-majority`` or ``none``."""
    selected_index: int | None = None
    vote_count: int = 0
    groups: tuple[tuple[str, tuple[int, ...]], ...] = ()
    """``(group key digest, member candidate indices)`` in decision order."""
    detail: str = ""

    def metadata(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "selected_index": self.selected_index,
            "vote_count": self.vote_count,
            "groups": [{"key": key, "candidates": list(indices)} for key, indices in self.groups],
            "detail": self.detail,
        }


def clamp_candidate_count(requested: int | None) -> int:
    """Clamp ``n`` into ``[1, HARD_CAP_N]``; ``None`` means the default budget."""

    if requested is None:
        return DEFAULT_N
    try:
        value = int(requested)
    except (TypeError, ValueError):
        return DEFAULT_N
    return max(1, min(value, HARD_CAP_N))


def sampling_args(index: int, temperature: float = DEFAULT_TEMPERATURE, seed: int = 0) -> dict[str, Any]:
    """``extra_create_args`` for candidate ``index``: seeded, ``temperature > 0``.

    A distinct seed per candidate is what makes the round *deterministic*: the
    same question, seed and server state produce the same five samples, so a
    repeated measurement is a repeat, not a new draw.
    """

    value = float(temperature)
    if not value > 0.0:
        raise ValueError("self-consistency needs temperature > 0; use the greedy path otherwise")
    return {"temperature": min(value, MAX_TEMPERATURE), "seed": int(seed) + int(index)}


def normalise_sql_text(sql: str | None) -> str:
    """Canonical comparison key for two SQL strings.

    Parsed PostgreSQL when possible (so formatting and identifier quoting do not
    split a vote), whitespace-collapsed text otherwise - a candidate that the
    gate already accepted normally parses, and the text branch exists so the
    comparison can never raise.
    """

    text = _WHITESPACE_RE.sub(" ", str(sql or "")).strip().rstrip(";").strip()
    if not text:
        return ""
    try:
        parsed = sqlglot.parse_one(text, read="postgres")
        return parsed.sql(dialect="postgres", comments=False).strip().casefold()
    except Exception:  # noqa: BLE001 - the fallback is the point of this branch
        return text.casefold()


def _cell(value: Any) -> str:
    """Type-stable cell key; ``1`` and ``1.0`` compare differently on purpose."""

    if value is None:
        return "\x00NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return str(value)


def result_signature(rows: Iterable[Sequence[Any]]) -> tuple[tuple[str, ...], ...]:
    """Order-insensitive signature of a result set, stable across row order.

    BIRD compares rows as a set, so the vote must too: two candidates that
    return the same rows in a different order agree. Values are stringified with
    a type-preserving rule so ``1``/``"1"`` and ``1``/``1.0`` do not merge.
    """

    cells = [tuple(_cell(value) for value in row) for row in rows]
    return tuple(sorted(cells, key=repr))


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _majority(
    candidates: Sequence[Candidate], keys: Sequence[str]
) -> tuple[int, str, tuple[tuple[str, tuple[int, ...]], ...], int]:
    """Largest group, ties to the earliest candidate.

    Returns ``(chosen_index, chosen_sql, groups, votes)``; ``groups`` keeps the
    decision order so a caller can show which candidates agreed.
    """

    order: list[str] = []
    members: dict[str, list[int]] = {}
    by_index = {candidate.index: candidate for candidate in candidates}
    for candidate, key in zip(candidates, keys):
        if key not in members:
            members[key] = []
            order.append(key)
        members[key].append(candidate.index)

    best_key = ""
    best_votes = -1
    for key in order:
        votes = len(members[key])
        if votes > best_votes:  # strict: the first group wins a tie, deterministically
            best_key, best_votes = key, votes
    chosen_index = min(members[best_key])
    groups = tuple((_digest(key), tuple(members[key])) for key in order)
    return chosen_index, str(by_index[chosen_index].sql), groups, best_votes


def choose(candidates: Sequence[Candidate]) -> Selection:
    """Turn sampled candidates into one SQL string; never raises.

    ``choose`` is pure and order-preserving: the candidate list is in sampling
    order, so ``min(index)`` inside a tied group is "the first draw that agreed",
    which keeps a repeated run identical to the original one.
    """

    eligible = [candidate for candidate in candidates if candidate.valid and candidate.sql]
    if not eligible:
        return Selection(None, "none", None, 0, (), "no candidate passed the SQL gate")

    executed = [candidate for candidate in eligible if candidate.execution == "ok"]
    if executed:
        index, sql, groups, votes = _majority(
            executed, [repr(result_signature(candidate.rows or ())) for candidate in executed]
        )
        unreachable = sum(1 for candidate in eligible if candidate.execution == "unreachable")
        rejected = sum(1 for candidate in eligible if candidate.execution == "rejected")
        detail = (
            f"majority over result sets of {len(executed)} executed candidate(s); "
            f"{rejected} rejected, {unreachable} not executable"
        )
        return Selection(sql, "result-set-majority", index, votes, groups, detail)

    index, sql, groups, votes = _majority(
        eligible, [normalise_sql_text(candidate.sql) for candidate in eligible]
    )
    not_executed = sum(1 for candidate in eligible if candidate.execution != "ok")
    if any(candidate.execution == "unreachable" for candidate in eligible):
        detail = (
            f"database unavailable ({not_executed} candidate(s) not executed); "
            f"majority over normalised SQL text"
        )
    else:
        detail = "no candidate executed successfully; majority over normalised SQL text"
    return Selection(sql, "sql-text-majority", index, votes, groups, detail)
