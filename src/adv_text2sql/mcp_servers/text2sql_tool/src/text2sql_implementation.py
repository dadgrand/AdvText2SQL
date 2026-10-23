"""Harness-facing Text2SQL tool, version 2.

Contract with ``benchmarks/base.py`` (do not change it):
    tool = tool_cls(db_uri=..., llm_client=...)
    tool.build()                      # sync; afterwards tool.db_schema exists
    result = await tool.query(question)   # no try/except around this call

Two harness facts drive the design. First, ``query`` is awaited without a
try/except, so any exception aborts the whole benchmark run instead of losing a
single item: the public ``query`` therefore never raises and always returns a
dict with ``status`` in {"success", "ambiguous", "error"} and an optional
``query``. Second, predictions are executed verbatim on PostgreSQL, so a
markdown fence, a backtick or a non-SELECT statement is a silent zero - hence the
sqlglot gate in :mod:`sql_utils` and the single repair attempt.

Ambiguity mode (see :data:`DEFAULT_AMBIGUITY_MODE` and ``Text2SQLConfig``):
``strict`` honours the gate, ``birdsafe`` still asks the model but never returns
``ambiguous``, ``off`` skips the extra call.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
from dataclasses import dataclass
from textwrap import dedent
from typing import Any, Final, Sequence

from .prompts import (
    AMBIGUITY_PROMPT_TEMPLATE,
    REPAIR_EXECUTION_PROMPT_TEMPLATE,
    REPAIR_SYNTAX_PROMPT_TEMPLATE,
    SQL_DIALECT,
    SQL_PROMPT_TEMPLATE,
    SYSTEM_PROMPT_TEMPLATE,
)
from .schema_pruning import prune_schema
from .sql_utils import ValidationResult, strip_code_fence, validate_sql

try:  # pragma: no cover - exercised by the production environment only
    from autogen_core.models import SystemMessage, UserMessage
except ImportError:  # offline tests: a stub client only reads ``.content``
    from dataclasses import dataclass as _dataclass

    @_dataclass(frozen=True)
    class SystemMessage:  # type: ignore[no-redef]
        content: str
        source: str = "system"
        type: str = "SystemMessage"

    @_dataclass(frozen=True)
    class UserMessage:  # type: ignore[no-redef]
        content: str
        source: str = "user"
        type: str = "UserMessage"

try:  # sqlalchemy is optional so the module stays importable, and testable, offline
    from sqlalchemy import create_engine, inspect
    from sqlalchemy import text as sql_text
except ImportError:  # pragma: no cover - exercised by the production environment only
    create_engine = None  # type: ignore[assignment]
    inspect = None  # type: ignore[assignment]
    sql_text = None  # type: ignore[assignment]


logger = logging.getLogger("text2sql_tool")

AMBIGUITY_ENV_VAR: Final = "TEXT2SQL_AMBIGUITY_MODE"
"""Lets the benchmark entrypoints switch modes without touching tool code."""

AMBIGUITY_MODES: Final = ("strict", "birdsafe", "off")

# Default mode: birdsafe, not strict. BIRD (241 items, 0 ambiguous gold) turns every
# "ambiguous" answer into a guaranteed zero, while Ambrosia's never-ambiguous floor is
# already 70.8% (170/240). Losing a BIRD item is unrecoverable; Ambrosia's ambiguity
# upside stays reachable through TEXT2SQL_AMBIGUITY_MODE=strict.
DEFAULT_AMBIGUITY_MODE: Final = "birdsafe"
"""Why ``birdsafe``: protects BIRD, keeps the gate's diagnostics; see the comment above."""

_USAGE_KEYS: Final = ("prompt_tokens", "completion_tokens", "total_tokens")


@dataclass(frozen=True)
class Text2SQLConfig:
    """Tuning knobs, all defaulted so the harness can construct the tool bare."""

    ambiguous_mode: str | None = None
    """None means: read ``TEXT2SQL_AMBIGUITY_MODE``, else use the default mode."""
    heavy_schema: bool = False
    """Sample rows and low-cardinality values cost tokens and disable pruning."""
    prune_schema: bool = True
    prune_max_tables: int = 12
    prune_min_schema_tables: int = 20
    """Recall guard: schemas at or below this size are sent whole."""
    execution_feedback: bool = True
    """One execution per prediction buys real "column does not exist" feedback."""
    execution_timeout_ms: int = 5000
    max_llm_retries: int = 2
    """Transport retries only; the SQL repair attempt is separate and capped at 1."""
    llm_timeout_s: float = 120.0
    retry_backoff_s: float = 0.4


@dataclass(frozen=True)
class AmbiguityVerdict:
    """Result of the ambiguity gate; ``checked`` is False when the model never ran."""

    ambiguous: bool
    detail: str = ""
    checked: bool = True


@dataclass(frozen=True)
class RepairRequest:
    """A follow-up generation with the reason the previous SQL was rejected."""

    sql: str
    error: str
    execution: bool = False
    """Execution rejections use a different prompt: the SQL parsed but the DB refused it."""


@dataclass(frozen=True)
class ExecutionFeedback:
    """Outcome of the optional dry-run; ``unreachable`` must never cost the item."""

    status: str
    error: str = ""


def resolve_ambiguity_mode(explicit: str | None) -> str:
    """Resolve the mode from the argument, then the environment, then the default.

    Raising on a typo at construction time is intentional: a silently ignored
    mode would make a benchmark run report numbers for a configuration nobody
    chose.
    """
    raw = explicit or os.getenv(AMBIGUITY_ENV_VAR) or DEFAULT_AMBIGUITY_MODE
    mode = raw.strip().lower()
    if mode not in AMBIGUITY_MODES:
        raise ValueError(f"unknown ambiguity mode {raw!r}; expected one of {AMBIGUITY_MODES}")
    return mode


def parse_ambiguity_answer(text: str) -> AmbiguityVerdict:
    """Classify a model reply as unambiguous only when it starts with ``OK``.

    The harness compares the tool verdict, not the text, so tolerance here is
    about formatting (fences, punctuation, casing, trailing prose), not about
    guessing intent: the first decisive token wins, and anything that is not a
    clear ``OK`` counts as ambiguous because the contract demands exactly one of
    two forms. An empty reply is ambiguous: no verdict is not a licence to guess.
    """
    clean = strip_code_fence(text)
    if not clean:
        return AmbiguityVerdict(True, "empty answer")

    lowered = clean.lower()
    if lowered.startswith("ambiguous"):
        clause = clean[len("ambiguous") :].lstrip(": ").strip()
        return AmbiguityVerdict(True, clause or clean)

    head = re.match(r"[a-z]+", lowered)
    if head is not None and head.group(0) == "ok":
        return AmbiguityVerdict(False)

    return AmbiguityVerdict(True, clean)


def _coerce_question(user_query: Any) -> str:
    """Normalise whatever the harness passes into a stripped question string."""
    if user_query is None:
        return ""
    if isinstance(user_query, str):
        return user_query.strip()
    return str(user_query).strip()


def _message_text(content: Any) -> str | None:
    """Extract text from an autogen content payload (str or list of parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        return "".join(str(part) for part in content)
    if content is None:
        return None
    return str(content)


def _as_int(value: Any) -> int:
    """Token counters arrive as ints, floats or nothing at all."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _is_connection_error(error: BaseException) -> bool:
    """True when the database was unreachable rather than unhappy with the SQL.

    Duck-typed on class names because sqlalchemy is an optional import here; the
    names cover ``OperationalError`` (refused connection, statement timeout),
    ``InterfaceError`` and plain socket errors.
    """
    connection_names = {
        "OperationalError",
        "InterfaceError",
        "DisconnectionError",
        "ConnectionError",
        "ConnectionRefusedError",
        "ConnectionResetError",
        "TimeoutError",
        "OSError",
    }
    return bool({type(error).__name__} & connection_names)


class Text2SQLGenerator:
    """Text2SQL tool for the AdvText2SQL harness.

    ``build()`` reads the schema once per database (the harness builds one tool
    per ``db_id``); ``query()`` is the per-question entrypoint and is guaranteed
    to return a ``ToolResponse``-compatible dict.
    """

    def __init__(
        self,
        db_uri: str,
        llm_client: Any,
        *,
        schema: str | None = None,
        engine: Any = None,
        config: Text2SQLConfig | None = None,
    ) -> None:
        """Create the tool.

        ``schema`` and ``engine`` exist so the pipeline can be exercised offline
        (and inside an existing process) without a PostgreSQL server; in the
        harness both stay ``None`` and are derived from ``db_uri`` in ``build``.
        """
        self.db_uri = db_uri
        self.llm_client = llm_client
        self.config = config or Text2SQLConfig()
        self.ambiguous_mode = resolve_ambiguity_mode(self.config.ambiguous_mode)

        self.db_schema: str = ""
        self.schema_source: str = "unset"
        self.system_prompt: str = ""
        self.usage: dict[str, int] = dict.fromkeys(_USAGE_KEYS, 0)
        self.llm_calls: int = 0
        self.last_selected_tables: tuple[str, ...] = ()

        self._schema_override = schema
        self._engine = engine

    # ------------------------------------------------------------------ build

    def build(self) -> None:
        """Load the schema and render the system prompt; never raises.

        A crash here would abort the benchmark for every question in the run, so
        a database failure is logged and downgraded to an empty schema: the tool
        can still emit a guess, and the run keeps producing diagnostic output.
        """
        self.db_schema = self._load_schema()
        self.system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            db_schema=self.db_schema, sql_dialect=SQL_DIALECT
        )
        logger.info("Built schema for %s (%s, %d chars)", self.db_uri, self.schema_source, len(self.db_schema))

    def _load_schema(self) -> str:
        if self._schema_override is not None:
            self.schema_source = "override"
            return self._schema_override.strip()
        try:
            self._get_engine()
        except Exception:
            logger.exception("Cannot create an engine for %s; continuing without a schema", self.db_uri)
            self.schema_source = "unavailable"
            return ""
        try:
            reader = self._get_db_schema_heavy if self.config.heavy_schema else self._get_db_schema_light
            schema = reader()
        except Exception:
            logger.exception("Could not read the schema of %s", self.db_uri)
            self.schema_source = "unavailable"
            return ""
        self.schema_source = "database"
        return schema

    def _get_engine(self) -> Any:
        if self._engine is not None:
            return self._engine
        if create_engine is None:
            raise RuntimeError("sqlalchemy is required to talk to a database")
        self._engine = create_engine(self.db_uri, pool_pre_ping=True)
        return self._engine

    @staticmethod
    def _columns(inspector: Any, table: str) -> list[dict[str, Any]]:
        """Read columns, tolerating backends without a ``public`` schema."""
        try:
            return inspector.get_columns(table, schema="public")
        except Exception:
            return inspector.get_columns(table)

    def _get_db_schema_light(self) -> str:
        """Table names with column names and types; the cheapest useful schema."""
        inspector = inspect(self._get_engine())
        try:
            tables = inspector.get_table_names(schema="public")
        except Exception:
            tables = []
        if not tables:
            tables = inspector.get_table_names()
        if not tables:
            logger.error("No tables found for %s", self.db_uri)
            return ""

        parts: list[str] = []
        for table in tables:
            parts.append(f"TABLE {table}")
            for column in self._columns(inspector, table):
                parts.append(f"  - {column['name']} ({column['type']})")
            parts.append("")
        return "\n".join(parts).strip()

    def _get_db_schema_heavy(
        self,
        sample_rows_limit: int = 3,
        unique_threshold: int = 10,
        sample_tables_with_more_rows: dict[str, int] | None = None,
    ) -> str:
        """Schema plus sample rows and low-cardinality values.

        Opt-in via ``Text2SQLConfig.heavy_schema``: it costs many tokens, and its
        ``CREATE TABLE`` blocks are not parseable by :mod:`schema_pruning`, so
        pruning is skipped for it by design.
        """
        engine = self._get_engine()
        sample_tables_with_more_rows = sample_tables_with_more_rows or {}
        inspector = inspect(engine)
        schema_parts: list[str] = []
        unique_values_parts: list[str] = []

        with engine.connect() as connection:
            for table in inspector.get_table_names():
                schema_parts.append(f"-- Table: {table}")
                columns = self._columns(inspector, table)
                schema_parts.append(f"CREATE TABLE {table} (")
                for column in columns:
                    schema_parts.append(f"    {column['name']} {column['type']},")
                schema_parts.append(");")

                limit = sample_tables_with_more_rows.get(table, sample_rows_limit)
                try:
                    rows = connection.execute(
                        sql_text(f'SELECT * FROM "{table}" LIMIT {int(limit)}')
                    ).fetchall()
                except Exception:
                    logger.debug("Could not fetch sample rows for %s", table)
                    rows = []

                if rows:
                    schema_parts.append("/*")
                    for row in rows:
                        schema_parts.append("\t".join(map(str, row)))
                    schema_parts.append("*/")

                for column in columns:
                    values = self._distinct_values(connection, table, column["name"])
                    if 0 < len(values) <= unique_threshold:
                        formatted = ", ".join(repr(value) for value in values)
                        unique_values_parts.append(
                            f'Possible values for "{table}.{column["name"]}": [{formatted}]'
                        )

                schema_parts.append("\n")

        if unique_values_parts:
            schema_parts.append("\n### Low-cardinality value hints")
            schema_parts.extend(unique_values_parts)
        return "\n".join(schema_parts).strip()

    @staticmethod
    def _distinct_values(connection: Any, table: str, column: str) -> list[Any]:
        """Distinct non-null values of one column; unreadable types are skipped."""
        try:
            result = connection.execute(
                sql_text(
                    f'SELECT DISTINCT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'
                )
            )
            return [row[0] for row in result.fetchall()]
        except Exception:
            return []

    # ------------------------------------------------------------------ query

    async def query(self, user_query: Any) -> dict[str, Any]:
        """Answer one question with a ``ToolResponse``-compatible dict.

        This is the only entrypoint the harness calls and it never raises: an
        unexpected failure is logged and reported as ``status="error"`` so the
        remaining questions still run.
        """
        try:
            return await self._query(user_query)
        except Exception:
            logger.exception("Unhandled failure while answering %r", user_query)
            return {"status": "error", "query": None}

    async def _query(self, user_query: Any) -> dict[str, Any]:
        question = _coerce_question(user_query)
        if not question:
            logger.warning("Empty question received; nothing to translate")
            return {"status": "error", "query": None}

        if not self.system_prompt:
            self.build()

        verdict = await self._assess_ambiguity(question)
        if self.ambiguous_mode == "strict" and verdict.ambiguous:
            logger.info("Question treated as ambiguous (%s): %s", verdict.detail, question)
            return {"status": "ambiguous", "query": None}

        schema = self._schema_for(question)
        sql = await self._generate_valid_sql(question, schema)
        if sql is None:
            return {"status": "error", "query": None}

        sql = await self._refine_with_execution(question, schema, sql)
        return {"status": "success", "query": sql}

    def _schema_for(self, question: str) -> str:
        """Return the schema block to put in the prompt, pruned when it pays off."""
        if not self.config.prune_schema:
            self.last_selected_tables = ()
            return self.db_schema
        pruned = prune_schema(
            self.db_schema,
            question,
            max_tables=self.config.prune_max_tables,
            min_schema_tables=self.config.prune_min_schema_tables,
        )
        self.last_selected_tables = pruned.tables
        if pruned.applied:
            logger.debug(
                "Pruned schema from %d to %d tables: %s",
                pruned.total_tables,
                len(pruned.tables),
                ", ".join(pruned.tables),
            )
        return pruned.schema

    # -------------------------------------------------------------- ambiguity

    async def _assess_ambiguity(self, question: str) -> AmbiguityVerdict:
        if self.ambiguous_mode == "off":
            return AmbiguityVerdict(False, "", checked=False)

        prompt = AMBIGUITY_PROMPT_TEMPLATE.format(db_schema=self.db_schema, user_query=question)
        answer = await self._complete(
            [
                SystemMessage(content=prompt),
                UserMessage(source="user", content="Classify the question above."),
            ]
        )
        if answer is None:
            # Fail open: a transport failure of the optional gate must not cost an item.
            logger.warning("Ambiguity gate unavailable; treating the question as unambiguous")
            return AmbiguityVerdict(False, "", checked=False)

        verdict = parse_ambiguity_answer(answer)
        if verdict.ambiguous and self.ambiguous_mode == "birdsafe":
            logger.info("Ambiguity gate fired but birdsafe mode keeps the item: %s", verdict.detail)
        return verdict

    # ---------------------------------------------------------------- generate

    async def _generate(
        self,
        question: str,
        schema: str,
        *,
        repair: RepairRequest | None = None,
    ) -> str | None:
        """Ask the model for SQL; ``repair`` switches to a correction prompt."""
        system = SYSTEM_PROMPT_TEMPLATE.format(db_schema=schema, sql_dialect=SQL_DIALECT)
        if repair is None:
            user = SQL_PROMPT_TEMPLATE.format(user_query=question, sql_dialect=SQL_DIALECT)
        else:
            template = (
                REPAIR_EXECUTION_PROMPT_TEMPLATE if repair.execution else REPAIR_SYNTAX_PROMPT_TEMPLATE
            )
            user = template.format(
                user_query=question,
                sql_query=repair.sql,
                error=repair.error,
                sql_dialect=SQL_DIALECT,
            )
        return await self._complete(
            [
                SystemMessage(content=system),
                UserMessage(source="user", content=dedent(user)),
            ]
        )

    async def _generate_valid_sql(self, question: str, schema: str) -> str | None:
        """Produce SQL that passes the sqlglot gate, repairing at most once.

        Call budget per question (``R = max_llm_retries``): one ambiguity call (if the
        mode is not ``off``), one generation call, and one repair call - each worth at
        most ``R + 1`` transport attempts. A repair only ever follows a generation that
        returned SQL; a generation that produced nothing is a retry-level failure, so it
        ends the question instead of spending a second budgeted call on an empty repair
        prompt (verification finding F3: 9 calls where the configured budget was 6).

        The repair budget is one because a second failure is far more likely to be a
        model that does not know the schema than a fixable typo, and every extra call
        costs benchmark time.
        """
        raw = await self._generate(question, schema)
        if raw is None:
            logger.warning("Generation produced no SQL; reporting an error without a repair")
            return None

        validation = self._validate(raw)
        if validation.ok:
            return validation.sql

        error = validation.error or "no SQL returned"
        logger.info("SQL rejected (%s); asking for one repair", error)
        repaired_raw = await self._generate(
            question, schema, repair=RepairRequest(sql=raw, error=error)
        )
        repaired = self._validate(repaired_raw)
        if repaired.ok:
            return repaired.sql

        logger.warning("Repair failed (%s); reporting an error instead of invalid SQL", repaired.error)
        return None

    @staticmethod
    def _validate(raw: str | None) -> ValidationResult:
        if raw is None:
            return ValidationResult(False, None, "the model returned no SQL")
        return validate_sql(raw)

    # -------------------------------------------------------- execution check

    async def _refine_with_execution(self, question: str, schema: str, sql: str) -> str:
        """Best-effort dry-run: fix DB-rejected SQL, keep valid SQL on any failure.

        The value is concrete (a hallucinated column is rejected by PostgreSQL,
        which the model can then correct); the risk is bounded by returning the
        already valid SQL whenever execution is impossible or the repair is not
        clean.
        """
        if not self.config.execution_feedback:
            return sql

        feedback = self._execute_for_feedback(sql)
        if feedback.status != "rejected":
            return sql

        logger.info("Database rejected the SQL (%s); asking for one correction", feedback.error)
        repaired_raw = await self._generate(
            question,
            schema,
            repair=RepairRequest(sql=sql, error=feedback.error, execution=True),
        )
        repaired = self._validate(repaired_raw)
        if repaired.ok and repaired.sql and repaired.sql != sql:
            return repaired.sql
        return sql

    def _execute_for_feedback(self, sql: str) -> ExecutionFeedback:
        """Run the SQL with a statement timeout and classify the outcome."""
        if self._engine is None and create_engine is None:
            return ExecutionFeedback("unreachable", "sqlalchemy is not installed")
        try:
            engine = self._get_engine()
        except Exception as error:
            return ExecutionFeedback("unreachable", str(error))

        try:
            with engine.connect() as connection:
                self._apply_statement_timeout(connection)
                statement = sql_text(sql) if sql_text is not None else sql
                connection.execute(statement).fetchmany(1)
        except Exception as error:
            if _is_connection_error(error):
                logger.debug("Database unreachable for execution feedback: %s", error)
                return ExecutionFeedback("unreachable", str(error))
            return ExecutionFeedback("rejected", str(error))
        return ExecutionFeedback("ok")

    def _apply_statement_timeout(self, connection: Any) -> None:
        """Cap the dry-run cost; backends without PostgreSQL settings keep working."""
        if sql_text is None:
            return
        try:
            connection.execute(
                sql_text(f"SET LOCAL statement_timeout TO {int(self.config.execution_timeout_ms)}")
            )
        except Exception:
            logger.debug("Statement timeout is not supported by this backend")

    # ---------------------------------------------------------------- LLM I/O

    async def _complete(self, messages: Sequence[Any]) -> str | None:
        """One resilient completion: timeout, transport retries with jitter, usage.

        Returns ``None`` once ``max_llm_retries + 1`` attempts yielded no usable text.
        An empty or whitespace-only reply counts as a failed attempt (it is retried, not
        returned): every caller needs text, and a blank answer must never reach the
        repair path as an empty "Rejected SQL" block.

        Only transport-level failures are retried here. A malformed answer is not a
        transport failure: it is handled by the SQL gate and its repair prompt, which
        keeps the retry budgets independent and predictable.
        """
        attempts = self.config.max_llm_retries + 1
        for attempt in range(attempts):
            self.llm_calls += 1
            try:
                result = await asyncio.wait_for(
                    self.llm_client.create(messages), timeout=self.config.llm_timeout_s
                )
            except (Exception, asyncio.CancelledError) as error:
                # A client that cancels itself internally is a transport failure like
                # any other; a cancelled *caller* re-raises on the backoff sleep below.
                logger.warning(
                    "LLM call failed (attempt %d/%d): %s", attempt + 1, attempts, error
                )
            else:
                self._account_usage(getattr(result, "usage", None))
                text = _message_text(getattr(result, "content", None))
                if text is not None and text.strip():
                    return text
                logger.warning("LLM returned no usable text (attempt %d/%d)", attempt + 1, attempts)

            if attempt + 1 >= attempts:
                return None
            jitter = random.random() * self.config.retry_backoff_s
            await asyncio.sleep(self.config.retry_backoff_s * (2**attempt) + jitter)
        return None

    def _account_usage(self, usage: Any) -> None:
        """Accumulate token usage; the harness client tracks its own copy too."""
        if usage is None:
            return
        if isinstance(usage, dict):
            values = usage
        else:
            values = {key: getattr(usage, key, 0) for key in _USAGE_KEYS}
        prompt = _as_int(values.get("prompt_tokens"))
        completion = _as_int(values.get("completion_tokens"))
        # A backend may report a total that is not prompt+completion; trust it only
        # when present so the three counters cannot drift apart.
        total = _as_int(values.get("total_tokens")) or prompt + completion
        self.usage["prompt_tokens"] += prompt
        self.usage["completion_tokens"] += completion
        self.usage["total_tokens"] += total
