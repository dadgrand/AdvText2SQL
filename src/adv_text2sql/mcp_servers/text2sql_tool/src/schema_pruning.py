"""Deterministic schema pruning by lexical overlap.

Why lexical and not embeddings: the tool must run offline, must be reproducible
across runs (a benchmark number that depends on an embedding model's mood is not
a measurement), and must cost zero extra LLM calls.

Recall is the risk of pruning, so the policy is deliberately conservative: the
schema is pruned only when it is larger than ``min_schema_tables``, the pruned
block is a byte-level subset of the original text, and when no table matches the
question the full schema is returned unchanged.

Columns are scoring evidence, not selectable units: a selected table keeps all of
its columns. Dropping columns is where a pruner loses the join key or the metric
that the ORDER BY needs, and the token saving is small compared with dropping a
table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Iterable, Sequence

_TABLE_HEADER_RE: Final = re.compile(r"^TABLE\s+(?P<name>\S+)\s*$")
_COLUMN_RE: Final = re.compile(r"^\s*-\s+(?P<name>.+)\s+\((?P<type>[^()]*)\)\s*$")

_STOPWORDS: Final = frozenset(
    """
    a an the of for in on at to by with and or is are was were be been being
    that this these those what which who whom whose how many much does do did
    list show give find return all any each per from as than more most less
    least not no it its their there have has had can could should would will
    shall may might must if when where while also only other such same into
    over under between about after before during above below up down out off
    through total number count average avg sum max min top bottom first last
    me you we they he she them his her our your my name named called value
    values records record rows row entries entry
    """.split()
)

_TABLE_WEIGHT: Final = 3
"""A table-name hit is stronger evidence than a column-name hit."""


@dataclass(frozen=True)
class TableSpec:
    """One parsed schema block, kept as its original lines so pruning loses nothing."""

    name: str
    column_names: tuple[str, ...]
    lines: tuple[str, ...]


@dataclass(frozen=True)
class PrunedSchema:
    """Result of pruning, including whether pruning was applied at all."""

    schema: str
    tables: tuple[str, ...]
    applied: bool
    total_tables: int


def tokenize(text: str) -> tuple[str, ...]:
    """Split identifiers or prose into lowercase alphanumeric tokens.

    Identifiers like ``Charter Funding Type``, ``charter_funding_type`` and
    ``charterFundingType`` must produce the same tokens, because the question
    wording never matches the schema spelling exactly.
    """
    expanded = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    expanded = expanded.replace("_", " ").replace("-", " ")
    tokens = re.findall(r"[a-z0-9]+", expanded.lower())
    return tuple(token for token in tokens if token not in _STOPWORDS and (len(token) > 1 or token.isdigit()))


def parse_schema(schema: str) -> tuple[TableSpec, ...]:
    """Parse the light schema produced by ``Text2SQLGenerator.build``.

    Unknown formats (for example the heavy schema with ``CREATE TABLE`` blocks)
    yield an empty tuple, which callers treat as "do not prune" rather than as an
    error.
    """
    tables: list[TableSpec] = []
    header: str | None = None
    column_names: list[str] = []
    lines: list[str] = []

    def flush() -> None:
        if header is None:
            return
        match = _TABLE_HEADER_RE.match(header)
        if match is None:
            return
        tables.append(
            TableSpec(
                name=match.group("name"),
                column_names=tuple(column_names),
                lines=tuple(lines),
            )
        )

    for line in schema.splitlines():
        if _TABLE_HEADER_RE.match(line):
            flush()
            header = line
            column_names = []
            lines = [line]
            continue
        if header is None:
            continue
        column = _COLUMN_RE.match(line)
        if column is not None:
            column_names.append(column.group("name"))
        if line.strip():
            lines.append(line)
    flush()
    return tuple(tables)


def render_schema(tables: Sequence[TableSpec]) -> str:
    """Render selected blocks with the same block separator as ``build``."""
    return "\n\n".join("\n".join(table.lines) for table in tables)


def score_tables(
    tables: Iterable[TableSpec],
    question: str,
    *,
    table_weight: int = _TABLE_WEIGHT,
) -> dict[str, int]:
    """Score every table by how many question tokens its names share."""
    question_tokens = set(tokenize(question))
    if not question_tokens:
        return {table.name: 0 for table in tables}

    scores: dict[str, int] = {}
    for table in tables:
        name_hits = question_tokens.intersection(tokenize(table.name))
        column_hits: set[str] = set()
        for column in table.column_names:
            column_hits.update(question_tokens.intersection(tokenize(column)))
        scores[table.name] = table_weight * len(name_hits) + len(column_hits)
    return scores


def prune_schema(
    schema: str,
    question: str,
    *,
    max_tables: int = 12,
    min_schema_tables: int = 20,
) -> PrunedSchema:
    """Select the tables a question plausibly needs, or return everything.

    ``min_schema_tables`` is the recall guard: small schemas are already cheap,
    so pruning them only adds risk. ``max_tables`` bounds the prompt when the
    question is broad (for example "list all tables with a name").
    """
    tables = parse_schema(schema)
    full = PrunedSchema(
        schema=schema,
        tables=tuple(table.name for table in tables),
        applied=False,
        total_tables=len(tables),
    )
    if len(tables) <= min_schema_tables:
        return full

    scores = score_tables(tables, question)
    ranked = sorted(tables, key=lambda table: (-scores[table.name], table.name))
    selected = [table for table in ranked if scores[table.name] > 0][:max_tables]
    if not selected or len(selected) == len(tables):
        return full

    return PrunedSchema(
        schema=render_schema(selected),
        tables=tuple(table.name for table in selected),
        applied=True,
        total_tables=len(tables),
    )
