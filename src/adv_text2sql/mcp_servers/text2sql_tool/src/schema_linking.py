"""Inference-time schema linking: pick the tables and columns a question needs.

Why this exists
---------------
A BIRD prompt is dominated by the schema block: the light schema of a real
database is hundreds of lines, most of which the question never touches. The
procedural arm asks a narrow question — does telling the model *which* part of
the schema matters (tables, then columns) move execution accuracy, independently
of the adapter?

Modes (``TEXT2SQL_SCHEMA_LINK``)
--------------------------------
``off`` (default)
    The tool does nothing new: its ``_schema_for`` keeps calling today's
    :func:`schema_pruning.prune_schema` with the same arguments. A test asserts the
    resulting block is byte-identical to the pre-change behaviour.
``tables``
    **Destructive.** Score every table against the question (+ evidence) tokens, keep
    the top ``k`` above ``min_score`` and render those tables whole. Spare slots below
    ``k`` are filled by **join closure**: a table that shares a multi-word key
    column (``account_id``, ``client_id``) with a selected table is added, because
    a question about a client's loan still needs the ``account``/``disp`` join.
``columns``
    **Destructive.** The same table selection, then drop columns that neither match
    the question nor look like a join key. When in doubt the whole table is kept.
``highlight``
    **Non-destructive.** The *full* schema is sent, tables ordered by relevance, each
    relevant table marked with a ``-- highlight`` comment line and its question-matching
    columns named. Nothing is removed — used when the selection hypothesis failed and
    the only remaining lever is where the model looks.
``joins``
    **Non-destructive.** ``highlight`` plus a compact join-path block built from the
    schema's own key-column names (``account.account_id = loan.account_id``), capped
    at ``MAX_JOIN_EDGES`` edges — aimed at queries that read a column from the wrong
    side of a join.

Scoring
-------
Lexical scoring is the deterministic core: identifiers are split on camelCase,
snake_case, dashes and spaces (:func:`schema_pruning.tokenize`), a small stopword
list is removed, a table-name hit counts three times a column-name hit, and a
question token of three or more characters found inside a *flattened* identifier
counts at half weight — that is what lets ``sat`` and ``scores`` reach the table
``satscores``, which no token split can produce. An optional semantic boost adds
``BOOST_WEIGHT * cosine`` when a *local* sentence-transformer is available; when
its weights are absent the module falls back to the deterministic hashing
embedder **and records why**. No network, no API key, no extra LLM call: a
benchmark number must not depend on a download.

Honesty constraints (tested)
----------------------------
* The selector sees exactly three inputs: the schema text, the question and the
  evidence. There is no parameter for gold SQL, for a reference answer or for any
  file of dev questions, so it cannot memorise the target — the only way to use
  those would be to change this module's signature, which a test pins.
* The selector never returns an empty schema: with no table above ``min_score``,
  no parsable schema, or an unreadable block it returns ``applied=False`` and the
  caller keeps today's pruning result. A missing schema would be a guaranteed zero
  and the round-2 empty-schema incident showed how easily that looks like a model.

Cost
----
No LLM calls. One embed pass per (schema, embedder) per process for the optional
boost (a handful of short strings; the hashing embedder is pure Python). The
prompt shrinks by the difference between the full schema and the linked block —
measured in ``docs/SCHEMA_LINKING.md``.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Final, Iterable, Sequence

from .retrieval import EmbedderUnavailable, make_embedder
from .schema_pruning import TableSpec, parse_schema, tokenize

logger = logging.getLogger("text2sql_tool.schema_linking")

__all__ = [
    "BOOST_WEIGHT",
    "DEFAULT_EMBEDDER",
    "DEFAULT_K",
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MIN_SCORE",
    "EMBEDDER_CHOICES",
    "HARD_CAP_K",
    "LinkedSchema",
    "SCHEMA_LINK_MODES",
    "SMALL_TABLE_COLUMNS",
    "link_schema",
]

SCHEMA_LINK_MODES: tuple[str, ...] = ("off", "tables", "columns", "highlight", "joins")
"""``off`` is the default and is handled by the tool, not by :func:`link_schema`."""

DESTRUCTIVE_MODES: tuple[str, ...] = ("tables", "columns")
"""Modes that remove tables/columns; their recall risk is the whole R3-2 hypothesis."""

NON_DESTRUCTIVE_MODES: tuple[str, ...] = ("highlight", "joins")
"""Modes that keep every original line and only add annotations (R3-6)."""

MAX_JOIN_EDGES: int = 12
"""Cap on join-path edges; the block rides in every prompt, so it stays compact."""

DEFAULT_K: int = 6
"""Tables to keep in ``tables``/``columns`` mode.

Six is generous for BIRD's eight dev databases (2-13 tables each) and still cuts a
large schema by half; ``TEXT2SQL_SCHEMA_LINK_K`` overrides it.
"""

HARD_CAP_K: int = 20
"""A ceiling even when configured higher: every kept table is prompt tokens."""

DEFAULT_MIN_SCORE: float = 1.0
"""Minimum lexical/boosted score for a table to be selected."""

DEFAULT_MAX_CHARS: int = 6000
"""Hard character budget for the rendered block (roughly 1.5k tokens)."""

SMALL_TABLE_COLUMNS: int = 6
"""Tables with at most this many columns are kept whole in ``columns`` mode.

Dropping a column is where a linker loses the join key or the ORDER BY metric,
and the token saving on a small table is negligible — so "when in doubt, keep the
whole table" is the default rather than an exception.
"""

BOOST_WEIGHT: float = 2.0
"""Weight of the optional embedding cosine relative to the lexical score."""

TABLE_NAME_WEIGHT: float = 3.0
"""An exact table-name token hit; the strongest lexical evidence."""

TABLE_NAME_SUBSTRING_WEIGHT: float = 1.5
"""A question token inside the flattened table name (``sat`` in ``satscores``)."""

COLUMN_WEIGHT: float = 1.0
"""An exact column-name token hit."""

COLUMN_SUBSTRING_WEIGHT: float = 0.5
"""A question token inside the flattened column name."""

MIN_SUBSTRING_TOKEN: int = 3
"""Shorter question tokens are never matched as substrings (``id`` matches everything)."""

DEFAULT_EMBEDDER: str = "auto"
"""``auto`` tries a local sentence-transformer, then the hashing embedder."""

EMBEDDER_CHOICES: tuple[str, ...] = ("auto", "sentence-transformer", "hash", "none")

_KEY_TOKENS: Final = frozenset({"id", "key", "code"})
"""Column tokens that mark a join/foreign key; a superset would keep almost every
column of a BIRD table and defeat the column stage."""


# --------------------------------------------------------------------------- result


@dataclass(frozen=True)
class LinkedSchema:
    """Outcome of one linking call; ``applied=False`` means "caller, fall back".

    The metadata block is what the tool records per question and what an artefact
    can be audited against: the exact schema hash, the counts, the mode and the
    embedder that produced them.
    """

    schema: str
    mode: str
    applied: bool
    tables: tuple[str, ...]
    total_tables: int
    columns_selected: int
    total_columns: int
    reason: str = ""
    over_budget: bool = False
    scores: tuple[tuple[str, float], ...] = ()
    """Top table scores, highest first; evidence for why the selection happened."""
    join_closure: tuple[str, ...] = ()
    """Tables added only because they share a join key with a selected table."""
    non_destructive: bool = False
    """True for ``highlight``/``joins``: every original table and column is still sent."""
    annotations: int = 0
    """Number of marker lines added in a non-destructive mode."""
    join_edges: tuple[str, ...] = ()
    """``<table>.<col> = <other>.<col>`` lines added by ``joins`` (schema-derived only)."""
    join_block_chars: int = 0
    """Character cost of the join-path block (0 when the mode adds none)."""
    embedder: str = "none"
    embedder_reason: str = "disabled"
    used_evidence: bool = False
    max_chars: int = DEFAULT_MAX_CHARS

    @property
    def schema_chars(self) -> int:
        return len(self.schema)

    @property
    def schema_sha256(self) -> str | None:
        return hashlib.sha256(self.schema.encode("utf-8")).hexdigest() if self.schema else None

    def metadata(self) -> dict[str, Any]:
        """The block ``Text2SQLGenerator`` records in ``procedure_metadata()``.

        The rendered block itself is included (``block``) for the same reason the
        few-shot arm records its examples: an artefact must be able to show exactly
        what the model saw, not only a hash of it. The hash is there so two runs can
        be compared without diffing text.
        """

        return {
            "mode": self.mode,
            "applied": bool(self.applied),
            "tables": list(self.tables),
            "tables_selected": len(self.tables),
            "tables_total": int(self.total_tables),
            "columns_selected": int(self.columns_selected),
            "columns_total": int(self.total_columns),
            "schema_chars": self.schema_chars,
            "schema_sha256": self.schema_sha256,
            "block": self.schema,
            "max_chars": int(self.max_chars),
            "over_budget": bool(self.over_budget),
            "reason": self.reason,
            "scores": [{"table": name, "score": round(score, 3)} for name, score in self.scores],
            "join_closure": list(self.join_closure),
            "non_destructive": bool(self.non_destructive),
            "annotations": int(self.annotations),
            "join_edges": list(self.join_edges),
            "join_block_chars": int(self.join_block_chars),
            "embedder": self.embedder,
            "embedder_reason": self.embedder_reason,
            "used_evidence": bool(self.used_evidence),
        }


# ------------------------------------------------------------------------ embedder


_EMBEDDER_CACHE: dict[tuple[str, str], tuple[Any | None, str, str]] = {}
"""``(requested kind, model) -> (embedder | None, resolved name, reason)``."""

_VECTOR_CACHE: dict[tuple[str, str], tuple[tuple[float, ...], ...]] = {}
"""``(schema sha256, embedder name) -> one vector per table``; one pass per schema."""


def _resolve_embedder(kind: str, model: str | None) -> tuple[Any | None, str, str]:
    """Resolve the optional semantic embedder; never raises, always explains.

    ``auto`` prefers a local sentence-transformer and falls back to the
    deterministic hashing embedder. The fallback is *recorded*, because a lexical
    boost and a semantic boost are different arms: silently swapping them would
    make two runs look identical on disk.
    """

    key = (kind, model or "")
    cached = _EMBEDDER_CACHE.get(key)
    if cached is not None:
        return cached

    if kind == "none":
        result = (None, "none", "disabled by TEXT2SQL_SCHEMA_LINK_EMBEDDER=none")
        _EMBEDDER_CACHE[key] = result
        return result

    if kind in {"auto", "sentence-transformer"}:
        try:
            embedder = make_embedder("sentence-transformer", model)
        except EmbedderUnavailable as error:
            resolved = str(error)
            if kind == "sentence-transformer":
                logger.warning("schema-linking: %s; falling back to the hashing embedder", resolved)
            try:
                hashed = make_embedder("hash")
            except EmbedderUnavailable:  # pragma: no cover - hashing embedder needs nothing
                result = (None, "none", resolved)
                _EMBEDDER_CACHE[key] = result
                return result
            result = (hashed, "hash", resolved)
            _EMBEDDER_CACHE[key] = result
            return result
        result = (embedder, getattr(embedder, "name", "sentence-transformer"), "")
        _EMBEDDER_CACHE[key] = result
        return result

    if kind == "hash":
        hashed = make_embedder("hash")
        result = (hashed, "hash", "")
        _EMBEDDER_CACHE[key] = result
        return result

    raise ValueError(
        f"unknown schema-linking embedder {kind!r}; expected one of {EMBEDDER_CHOICES}"
    )


def _table_vectors(
    schema: str, tables: Sequence[TableSpec], embedder: Any, embedder_name: str
) -> tuple[tuple[float, ...], ...]:
    """Embed each table's name+columns once per (schema, embedder) per process."""

    schema_sha = hashlib.sha256(schema.encode("utf-8")).hexdigest()
    key = (schema_sha, embedder_name)
    cached = _VECTOR_CACHE.get(key)
    if cached is not None:
        return cached
    documents = [f"{table.name} {' '.join(table.column_names)}" for table in tables]
    vectors = tuple(tuple(vector) for vector in embedder.embed(documents))
    _VECTOR_CACHE[key] = vectors
    return vectors


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    return float(sum(a * b for a, b in zip(left, right)))


# ----------------------------------------------------------------------- selection


def _stem(token: str) -> str:
    """Fold a simple plural so ``accounts`` and ``account`` are the same evidence.

    Deliberately tiny and symmetric (applied to the question tokens *and* to the
    identifier tokens), so it can only ever help both sides or neither. BIRD is full
    of ``account``/``accounts``, ``school``/``schools`` pairs, and an exact-token
    matcher misses every one of them.
    """

    if len(token) <= 3:
        return token
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith(("ses", "xes", "zes", "ches", "shes")):
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _tokens(text: str) -> tuple[set[str], str]:
    """Stemmed token set plus a deterministic flattened form for substring hits."""

    stemmed = {_stem(token) for token in tokenize(text)}
    return stemmed, "".join(sorted(stemmed))


def _identifier_index(text: str) -> tuple[set[str], str]:
    """Tokens of an identifier plus its flattened form.

    ``tokenize`` already splits camelCase, snake_case, dashes and spaces, which
    covers most BIRD identifiers. The flattened form exists because some are simply
    concatenated words — ``satscores``, ``frpm``, ``card_games`` — and a question
    token like ``sat`` or ``scores`` would otherwise never match the table name.
    """

    return _tokens(text)


def _score_tables(
    tables: Sequence[TableSpec], question_tokens: set[str]
) -> dict[str, float]:
    """Score every table by exact token hits plus compound-name substring hits."""

    scores: dict[str, float] = {}
    for table in tables:
        name_tokens, name_flat = _identifier_index(table.name)
        score = TABLE_NAME_WEIGHT * len(question_tokens & name_tokens)
        score += TABLE_NAME_SUBSTRING_WEIGHT * sum(
            1
            for token in question_tokens
            if len(token) >= MIN_SUBSTRING_TOKEN and token in name_flat and token not in name_tokens
        )
        for column in table.column_names:
            column_tokens, column_flat = _identifier_index(column)
            score += COLUMN_WEIGHT * len(question_tokens & column_tokens)
            score += COLUMN_SUBSTRING_WEIGHT * sum(
                1
                for token in question_tokens
                if len(token) >= MIN_SUBSTRING_TOKEN
                and token in column_flat
                and token not in column_tokens
            )
        scores[table.name] = float(score)
    return scores


def _column_pairs(table: TableSpec) -> list[tuple[str, str]] | None:
    """``[(column name, original line)]`` or ``None`` when the block cannot be trusted.

    ``TableSpec.lines`` starts with the header and contains every non-empty line;
    ``column_names`` contains the parsed column names. If the two disagree (a
    format the parser only half-understood — e.g. a heavy ``CREATE TABLE`` block)
    the caller keeps the whole table instead of editing a block it cannot read.
    """

    body = list(table.lines[1:])
    if len(body) != len(table.column_names):
        return None
    return list(zip(table.column_names, body))


def _key_columns(column_names: Iterable[str]) -> set[str]:
    keys: set[str] = set()
    for column in column_names:
        tokens, _flat = _tokens(column)
        if _KEY_TOKENS.intersection(tokens):
            keys.add(column)
    return keys


def _key_signatures(table: TableSpec) -> set[tuple[str, ...]]:
    """Join-key signatures of a table: ``("account", "id")`` for ``account_id``.

    Single-word keys (``id``) are deliberately ignored: in a database where every
    table has an ``id`` column they would link everything to everything, and the
    column stage would keep no information. A two-word signature such as
    ``client_id`` or ``account_id`` is what actually identifies a foreign key.
    """

    signatures: set[tuple[str, ...]] = set()
    for column in table.column_names:
        tokens, _flat = _tokens(column)
        if len(tokens) >= 2 and _KEY_TOKENS.intersection(tokens):
            signatures.add(tuple(sorted(tokens)))
    return signatures


def _expand_by_join_keys(
    tables: Sequence[TableSpec],
    selected: Sequence[TableSpec],
    scores: dict[str, float],
    *,
    k: int,
) -> tuple[list[TableSpec], tuple[str, ...]]:
    """Fill spare slots with tables reachable through a shared join key.

    Recall, not precision, is what execution accuracy is made of: a question about
    a client's payments names only ``client``/``loan``, yet the gold query joins
    ``account`` and ``disp``. The closure adds those tables because they share a
    ``client_id``-style column with a selected table — using the schema only, never
    the gold SQL or the reference answer. It is transitive and bounded by ``k``, and
    it never replaces a table that scored, only appends to it.
    """

    if len(selected) >= k:
        return list(selected), ()
    selected_signatures: set[tuple[str, ...]] = set()
    for table in selected:
        selected_signatures |= _key_signatures(table)

    eligible = [table for table in selected]
    added: list[str] = []
    for table in sorted(tables, key=lambda item: (-scores.get(item.name, 0.0), item.name)):
        if len(eligible) >= k:
            break
        if any(table.name == chosen.name for chosen in eligible):
            continue
        signatures = _key_signatures(table)
        if signatures and selected_signatures.intersection(signatures):
            eligible.append(table)
            added.append(table.name)
            selected_signatures |= signatures
    return eligible, tuple(added)


def _filter_columns(
    table: TableSpec, question_tokens: set[str], *, small_table_columns: int
) -> tuple[list[str], int, str]:
    """Return ``(kept columns, total columns, reason)`` for one selected table.

    "When in doubt keep the whole table": a small table, a table the question does
    not touch, or a block whose lines cannot be aligned keep everything. Dropping a
    column is only worth it when the table is large and the match is confident.
    """

    total = len(table.column_names)
    if total <= small_table_columns:
        return list(table.column_names), total, "small table"

    matched = [
        column
        for column in table.column_names
        if question_tokens.intersection(_tokens(column)[0])
    ]
    if not matched:
        return list(table.column_names), total, "no column matched"

    keep = set(matched) | _key_columns(table.column_names)
    kept = [column for column in table.column_names if column in keep]
    if len(kept) < 2:
        return list(table.column_names), total, "too few columns would remain"
    return kept, total, ""


def _render_table(
    table: TableSpec, kept_columns: Sequence[str] | None
) -> tuple[str, int]:
    """Render one table, optionally with a subset of its columns.

    The output is always a byte-level subset of the original block: the original
    header line and the original column lines, in the original order.
    """

    if kept_columns is None or len(kept_columns) == len(table.column_names):
        return "\n".join(table.lines), len(table.column_names)
    pairs = _column_pairs(table)
    if pairs is None:  # pragma: no cover - guarded by _filter_columns
        return "\n".join(table.lines), len(table.column_names)
    keep = set(kept_columns)
    lines = [table.lines[0]] + [line for column, line in pairs if column in keep]
    return "\n".join(lines), len(keep)


def _score_with_boost(
    schema: str, tables: Sequence[TableSpec], combined: str, embedder: str
) -> tuple[dict[str, float], set[str], str, str]:
    """Lexical table scores plus the optional embedding boost; never raises.

    Shared by every mode so a highlight and a destructive selection cannot disagree
    about which table is relevant: the marker is the same score the pruner would use.
    """

    question_tokens, _flat = _tokens(combined)
    scores: dict[str, float] = _score_tables(tables, question_tokens)
    embedder_instance, embedder_name, embedder_reason = _resolve_embedder(embedder, None)
    if embedder_instance is not None:
        try:
            vectors = _table_vectors(schema, tables, embedder_instance, embedder_name)
            query_vector = embedder_instance.embed([combined])[0]
            for table, vector in zip(tables, vectors):
                boost = BOOST_WEIGHT * max(0.0, _cosine(query_vector, vector))
                if boost:
                    scores[table.name] = scores.get(table.name, 0.0) + boost
        except Exception:  # noqa: BLE001 - the boost is optional; never lose the item
            logger.exception("schema-linking: embedding boost failed; using lexical scores only")
            embedder_reason = "embedding boost failed at runtime; lexical scores only"
    return scores, question_tokens, embedder_name, embedder_reason


def _matched_columns(table: TableSpec, question_tokens: set[str]) -> list[str]:
    """Columns of one table whose tokens the question touches (order preserved)."""

    return [
        column
        for column in table.column_names
        if question_tokens.intersection(_tokens(column)[0])
    ]


def _is_join_key_column(name: str) -> bool:
    """``*_id`` / ``id`` / ``*_code`` / ``*key`` — the patterns the task names."""

    folded = name.casefold()
    tokens, _flat = _tokens(name)
    return (
        folded.endswith("id")
        or folded.endswith("code")
        or "id" in tokens
        or "code" in tokens
        or "key" in tokens
    )


def _join_edges(
    selected: Sequence[TableSpec], *, max_edges: int = MAX_JOIN_EDGES
) -> tuple[str, ...]:
    """``<table>.<col> = <other>.<col>`` edges from key columns the schema shares.

    Only the schema text is read: two tables are joined when they contain the same
    key-column *name* (``account_id`` in both). Multi-word keys come first, because a
    bare ``id`` in every table produces noise before it produces a join. The list is
    deduplicated and capped at ``max_edges``.
    """

    owners: dict[str, list[tuple[str, str]]] = {}
    for table in selected:
        for column in table.column_names:
            if not _is_join_key_column(column):
                continue
            key = "".join(column.casefold().split()).replace("-", "")
            owners.setdefault(key, []).append((table.name, column))

    multi: list[str] = []
    single: list[str] = []
    for key in sorted(owners):
        pairs = owners[key]
        if len(pairs) < 2:
            continue
        for index in range(len(pairs)):
            for other in range(index + 1, len(pairs)):
                left, right = pairs[index], pairs[other]
                if left[0] == right[0]:
                    continue
                edge = f"{left[0]}.{left[1]} = {right[0]}.{right[1]}"
                bucket = multi if len(_tokens(left[1])[0]) >= 2 else single
                if edge not in bucket:
                    bucket.append(edge)
    return tuple((multi + single)[: int(max_edges)])


def _annotated_block(
    tables: Sequence[TableSpec],
    *,
    scores: dict[str, float],
    relevant: Sequence[str],
    question_tokens: set[str],
    mode: str,
    join_edges: Sequence[str],
) -> tuple[str, int, int]:
    """Full schema, relevance-ordered, annotated; ``(block, annotations, join_chars)``.

    Every original header and column line is copied verbatim — the marker is an
    *added* comment line after the header, never a rewrite or a removal. This is the
    invariant that makes ``highlight``/``joins`` non-destructive.
    """

    relevant_set = set(relevant)
    lines: list[str] = []
    if relevant:
        lines.append(
            "Relevant tables: "
            + ", ".join(f"{name} ({scores.get(name, 0.0):.2f})" for name in relevant)
        )
    for table in tables:  # already relevance-ordered by the caller
        original = list(table.lines)
        lines.append(original[0])
        if table.name in relevant_set:
            matched = _matched_columns(table, question_tokens)
            detail = [f"score {scores.get(table.name, 0.0):.2f}"]
            if matched:
                detail.append("question columns: " + ", ".join(matched))
            lines.append("-- highlight: " + "; ".join(detail))
        lines.extend(original[1:])
    block = "\n".join(lines)

    join_chars = 0
    if mode == "joins" and join_edges:
        join_block = (
            "Join paths (key columns; derived from the schema only):\n"
            + "\n".join(join_edges)
        )
        join_chars = len(join_block)
        block = block + "\n\n" + join_block
    return block, len(relevant), join_chars


def link_schema(
    schema: str,
    question: str,
    *,
    evidence: str = "",
    mode: str = "tables",
    k: int = DEFAULT_K,
    min_score: float = DEFAULT_MIN_SCORE,
    max_chars: int = DEFAULT_MAX_CHARS,
    embedder: str = DEFAULT_EMBEDDER,
    small_table_columns: int = SMALL_TABLE_COLUMNS,
) -> LinkedSchema:
    """Select a schema block for one question; never raises on bad input.

    Inputs are exactly the schema, the question and the evidence — there is no
    gold-SQL parameter (see the module docstring). ``applied=False`` with an empty
    ``schema`` is the documented "fall back" signal: the caller then keeps today's
    pruning result, so the prompt is never left without a schema.
    """

    if mode not in ("tables", "columns", "highlight", "joins"):
        raise ValueError(
            f"link_schema mode must be one of ('tables', 'columns', 'highlight', 'joins'), got {mode!r}"
        )
    if int(k) < 1:
        raise ValueError("k must be >= 1")
    if float(min_score) < 0.0:
        raise ValueError("min_score must be >= 0")
    if int(max_chars) < 200:
        raise ValueError("max_chars must be >= 200 (a single table does not fit otherwise)")
    if embedder not in EMBEDDER_CHOICES:
        raise ValueError(f"unknown embedder {embedder!r}; expected one of {EMBEDDER_CHOICES}")

    tables = parse_schema(schema)
    if not tables:
        return LinkedSchema(
            schema="",
            mode=mode,
            applied=False,
            tables=(),
            total_tables=0,
            columns_selected=0,
            total_columns=0,
            reason="schema is empty or not in the light 'TABLE <name>' format",
            max_chars=int(max_chars),
        )

    combined = f"{question}\n{evidence}".strip() if evidence else question
    question_tokens, _question_flat = _tokens(combined)
    if not question_tokens:
        return LinkedSchema(
            schema="",
            mode=mode,
            applied=False,
            tables=(),
            total_tables=len(tables),
            columns_selected=0,
            total_columns=sum(len(table.column_names) for table in tables),
            reason="the question has no content tokens to link on",
            used_evidence=bool(evidence),
            max_chars=int(max_chars),
        )

    lexical = _score_tables(tables, question_tokens)
    scores: dict[str, float] = dict(lexical)

    # R3-6: highlight/joins keep every original line and only annotate. They share
    # the scorer and the relevance order with the destructive modes, so the marker
    # always agrees with what a `tables` run would have selected.
    if mode in NON_DESTRUCTIVE_MODES:
        scores, question_tokens, embedder_name, embedder_reason = _score_with_boost(
            schema, tables, combined, embedder
        )
        ranked = sorted(tables, key=lambda table: (-scores.get(table.name, 0.0), table.name))
        relevant = [
            table.name
            for table in ranked
            if scores.get(table.name, 0.0) >= float(min_score)
        ][: int(k)]
        selected_for_joins = list(ranked[: int(k)])
        join_edges = _join_edges(selected_for_joins) if mode == "joins" else ()
        block, annotations, join_chars = _annotated_block(
            ranked,
            scores=scores,
            relevant=relevant,
            question_tokens=question_tokens,
            mode=mode,
            join_edges=join_edges,
        )
        full_columns = sum(len(table.column_names) for table in tables)
        return LinkedSchema(
            schema=block,
            mode=mode,
            applied=bool(relevant),
            tables=tuple(relevant),
            total_tables=len(tables),
            columns_selected=full_columns,
            total_columns=full_columns,
            reason=(
                ""
                if relevant
                else f"no table scored >= min_score ({min_score}); nothing was highlighted"
            ),
            over_budget=len(block) > int(max_chars),
            scores=tuple((table.name, scores.get(table.name, 0.0)) for table in ranked[:5]),
            non_destructive=True,
            annotations=annotations,
            join_edges=join_edges,
            join_block_chars=join_chars,
            embedder=embedder_name,
            embedder_reason=embedder_reason,
            used_evidence=bool(evidence),
            max_chars=int(max_chars),
        )

    embedder_instance, embedder_name, embedder_reason = _resolve_embedder(embedder, None)
    if embedder_instance is not None:
        try:
            vectors = _table_vectors(schema, tables, embedder_instance, embedder_name)
            query_vector = embedder_instance.embed([combined])[0]
            for table, vector in zip(tables, vectors):
                boost = BOOST_WEIGHT * max(0.0, _cosine(query_vector, vector))
                if boost:
                    scores[table.name] = scores.get(table.name, 0.0) + boost
        except Exception:  # noqa: BLE001 - the boost is optional; never lose the item
            logger.exception("schema-linking: embedding boost failed; using lexical scores only")
            embedder_reason = "embedding boost failed at runtime; lexical scores only"

    ranked = sorted(
        tables, key=lambda table: (-scores.get(table.name, 0.0), table.name)
    )
    eligible = [table for table in ranked if scores.get(table.name, 0.0) >= float(min_score)]
    selected = eligible[: int(k)]
    if not selected:
        return LinkedSchema(
            schema="",
            mode=mode,
            applied=False,
            tables=(),
            total_tables=len(tables),
            columns_selected=0,
            total_columns=sum(len(table.column_names) for table in tables),
            reason=(
                f"no table scored >= min_score ({min_score}); "
                f"best was {ranked[0].name} at {scores.get(ranked[0].name, 0.0):.3f}"
            ),
            scores=tuple((table.name, scores.get(table.name, 0.0)) for table in ranked[:5]),
            embedder=embedder_name,
            embedder_reason=embedder_reason,
            used_evidence=bool(evidence),
            max_chars=int(max_chars),
        )

    total_columns = sum(len(table.column_names) for table in tables)
    selected, join_added = _expand_by_join_keys(tables, selected, scores, k=int(k))
    columns_by_table: dict[str, list[str] | None] = {}
    columns_selected = 0
    for table in selected:
        if mode == "columns":
            kept, _total, _reason = _filter_columns(
                table, question_tokens, small_table_columns=int(small_table_columns)
            )
            columns_by_table[table.name] = kept
            columns_selected += len(kept)
        else:
            columns_by_table[table.name] = None  # whole table
            columns_selected += len(table.column_names)

    def render(current: Sequence[TableSpec]) -> tuple[str, int]:
        blocks: list[str] = []
        rendered_columns = 0
        for table in current:
            block, kept = _render_table(table, columns_by_table.get(table.name))
            blocks.append(block)
            rendered_columns += kept
        return "\n\n".join(blocks), rendered_columns

    rendered, rendered_columns = render(selected)
    over_budget = False
    dropped: list[str] = []
    # Budget enforcement: drop the weakest tables first (the list is best-first), so
    # the best-scoring table is always kept. Never drop the last table.
    while len(rendered) > int(max_chars) and len(selected) > 1:
        dropped.append(selected[-1].name)
        selected = selected[:-1]
        rendered, rendered_columns = render(selected)

    if len(rendered) > int(max_chars) and mode == "columns" and selected:
        table = selected[0]
        pairs = _column_pairs(table)
        if pairs is not None:
            key_columns = _key_columns(table.column_names)
            keep = [
                column
                for column, _line in pairs
                if question_tokens.intersection(_tokens(column)[0]) or column in key_columns
            ]
            if keep and len(keep) < len(table.column_names):
                columns_by_table[table.name] = keep
                rendered, rendered_columns = render(selected)

    if len(rendered) > int(max_chars):
        over_budget = True
        logger.warning(
            "schema-linking: the best single table exceeds max_chars=%d; keeping it whole",
            int(max_chars),
        )

    reason = ""
    if dropped:
        reason = f"budget {max_chars} chars: dropped {', '.join(dropped)}"
    return LinkedSchema(
        schema=rendered,
        mode=mode,
        applied=True,
        tables=tuple(table.name for table in selected),
        total_tables=len(tables),
        columns_selected=rendered_columns,
        total_columns=total_columns,
        reason=reason,
        over_budget=over_budget,
        scores=tuple((table.name, scores.get(table.name, 0.0)) for table in selected),
        join_closure=join_added,
        embedder=embedder_name,
        embedder_reason=embedder_reason,
        used_evidence=bool(evidence),
        max_chars=int(max_chars),
    )
