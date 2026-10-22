"""Pure SQL text normalisation and validation helpers.

Every function here is deterministic and side-effect free: the harness executes
tool predictions verbatim, so the rewrite rules that protect a prediction from a
guaranteed zero must be testable in isolation from the LLM and the database.
Nothing in this module performs I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

import sqlglot
from sqlglot import exp

DIALECT: Final = "postgres"
"""sqlglot dialect name. BIRD/Ambrosia predictions are executed on PostgreSQL."""

_FENCE_BLOCK_RE: Final = re.compile(r"```[A-Za-z0-9_+-]*[ \t]*\r?\n(.*?)\r?\n?```", re.DOTALL)
_FENCE_INLINE_RE: Final = re.compile(r"```(.*?)```", re.DOTALL)
_FENCE_HEAD_RE: Final = re.compile(r"^```[A-Za-z0-9_+-]*\s*")
_FENCE_TAIL_RE: Final = re.compile(r"\s*```$")

_FORBIDDEN_STATEMENTS: Final = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.TruncateTable,
    exp.Merge,
)
"""Statement types that must never leave the tool: predictions are executed as-is."""


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of the SQL gate.

    Why a dataclass instead of a bool: the caller needs the parse error text to
    build a repair prompt, and the caller must receive the *normalised* SQL
    because that is the string the harness will execute.
    """

    ok: bool
    sql: str | None
    error: str | None


def strip_code_fence(text: str) -> str:
    """Return the payload of a markdown fence, or the stripped text when there is none.

    Also used for the ambiguity answer, where models like to wrap ``OK`` in a
    fence even though the contract forbids it; the language tag is only treated
    as such when the payload starts on its own line, so `` ```OK``` `` keeps its
    payload instead of being read as a language name.
    """
    block = _FENCE_BLOCK_RE.search(text)
    if block:
        return block.group(1).strip()
    inline = _FENCE_INLINE_RE.search(text)
    if inline:
        return inline.group(1).strip()
    cleaned = _FENCE_HEAD_RE.sub("", text.strip(), count=1)
    cleaned = _FENCE_TAIL_RE.sub("", cleaned, count=1)
    return cleaned.strip()


def _skip_quoted(sql: str, start: int, quote: str) -> int:
    """Return the index just past the closing quote, honouring doubled quotes."""
    index = start + 1
    length = len(sql)
    while index < length:
        if sql[index] == quote:
            if index + 1 < length and sql[index + 1] == quote:
                index += 2
                continue
            return index + 1
        index += 1
    return length


def strip_comments(sql: str) -> str:
    """Remove ``/* ... */`` and ``-- ...`` comments outside of string literals.

    Models are told not to emit comments, but a comment left inside a prediction
    is executed by the harness; removing it silently is strictly safer than
    failing the item.
    """
    out: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char in ("'", '"'):
            end = _skip_quoted(sql, index, char)
            out.append(sql[index:end])
            index = end
        elif sql.startswith("--", index):
            newline = sql.find("\n", index)
            index = length if newline == -1 else newline
        elif sql.startswith("/*", index):
            closing = sql.find("*/", index + 2)
            index = length if closing == -1 else closing + 2
        else:
            out.append(char)
            index += 1
    return "".join(out)


def normalise_identifiers(sql: str) -> str:
    """Rewrite MySQL/SQLite backtick identifiers to PostgreSQL double quotes.

    BIRD gold SQL is SQLite, so models fall back to backticks; PostgreSQL rejects
    them, which would turn an otherwise correct prediction into a zero.
    """
    out: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char == "'":
            end = _skip_quoted(sql, index, char)
            out.append(sql[index:end])
            index = end
        elif char == "`":
            closing = sql.find("`", index + 1)
            if closing == -1:
                out.append(char)
                index += 1
                continue
            out.append('"' + sql[index + 1 : closing] + '"')
            index = closing + 1
        else:
            out.append(char)
            index += 1
    return "".join(out)


def first_statement(sql: str) -> str:
    """Keep the first statement only.

    A trailing second statement would be executed by the harness as well; the
    benchmark expects one SELECT per prediction.
    """
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char in ("'", '"'):
            index = _skip_quoted(sql, index, char)
            continue
        if char == ";":
            return sql[:index]
        index += 1
    return sql


def strip_trailing_semicolon(sql: str) -> str:
    """Drop trailing semicolons; the harness compares executed results, not text."""
    return sql.rstrip().rstrip(";").rstrip()


def normalise_sql(raw: str) -> str:
    """Apply every text-level rewrite the tool trusts: fences, identifiers, comments.

    Whitespace inside string literals is preserved on purpose: collapsing it
    would silently change compared values (``'a  b'``).
    """
    sql = strip_code_fence(raw)
    sql = normalise_identifiers(sql)
    sql = strip_comments(sql)
    sql = first_statement(sql)
    return strip_trailing_semicolon(sql)


def _is_column_like(node: exp.Expression) -> bool:
    """True for expressions whose value can be zero but is not a literal."""
    if isinstance(node, exp.Column):
        return True
    if isinstance(node, exp.Paren) and node.this is not None:
        return _is_column_like(node.this)
    if isinstance(node, exp.Cast) and node.this is not None:
        return _is_column_like(node.this)
    return False


def _wrap_divisions(parsed: exp.Expression) -> exp.Expression:
    """Guard column denominators with ``NULLIF`` so a zero divisor yields NULL.

    BIRD answers frequently divide by a count that can be zero; without the
    guard PostgreSQL raises ``division by zero`` and the item is lost even when
    the SQL logic is right.
    """
    for division in list(parsed.find_all(exp.Div)):
        denominator = division.expression
        if denominator is None or isinstance(denominator, exp.Nullif):
            continue
        if not _is_column_like(denominator):
            continue
        division.set(
            "expression",
            exp.Nullif(this=denominator.copy(), expression=exp.Literal.number(0)),
        )
    return parsed


def canonicalise_division(sql: str) -> str:
    """Idempotently apply the NULLIF guard to an already valid SQL string."""
    try:
        parsed = sqlglot.parse_one(sql, read=DIALECT)
    except sqlglot.errors.SqlglotError:
        return sql
    if parsed is None:
        return sql
    return _wrap_divisions(parsed).sql(dialect=DIALECT)


def _forbidden_statement(parsed: exp.Expression) -> exp.Expression | None:
    for node in parsed.walk():
        if isinstance(node, _FORBIDDEN_STATEMENTS):
            return node
    return None


def validate_sql(raw: str) -> ValidationResult:
    """Gate a model reply through normalisation, a PostgreSQL parse and a SELECT check.

    The returned SQL is always re-parsed after the NULLIF rewrite, so the caller
    never hands the harness a string the tool itself could not parse.
    """
    sql = normalise_sql(raw)
    if not sql:
        return ValidationResult(False, None, "empty SQL")

    try:
        parsed = sqlglot.parse_one(sql, read=DIALECT)
    except sqlglot.errors.SqlglotError as error:
        return ValidationResult(False, None, f"PostgreSQL parse error: {error}")

    if parsed is None:
        return ValidationResult(False, None, "PostgreSQL parse error: empty statement")

    forbidden = _forbidden_statement(parsed)
    if forbidden is not None:
        kind = type(forbidden).__name__.upper()
        return ValidationResult(False, None, f"unsupported statement type: {kind}")

    if not isinstance(parsed, exp.Query):
        kind = type(parsed).__name__.upper()
        return ValidationResult(False, None, f"not a SELECT statement: {kind}")

    rewritten = _wrap_divisions(parsed).sql(dialect=DIALECT)
    try:
        recheck = sqlglot.parse_one(rewritten, read=DIALECT)
    except sqlglot.errors.SqlglotError as error:
        return ValidationResult(False, None, f"NULLIF rewrite produced invalid SQL: {error}")
    if not isinstance(recheck, exp.Query):
        return ValidationResult(False, None, "NULLIF rewrite produced a non-SELECT statement")

    return ValidationResult(True, rewritten, None)
