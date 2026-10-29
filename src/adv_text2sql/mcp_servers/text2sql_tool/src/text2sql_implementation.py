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
import hashlib
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, replace
from textwrap import dedent
from typing import Any, Final, Mapping, Sequence

from .prompts import (
    AMBIGUITY_PROMPT_TEMPLATE,
    FEW_SHOT_PROMPT_TEMPLATE,
    REPAIR_EXECUTION_PROMPT_TEMPLATE,
    REPAIR_SYNTAX_PROMPT_TEMPLATE,
    SQL_DIALECT,
    SQL_PROMPT_TEMPLATE,
    SYSTEM_PROMPT_TEMPLATE,
)
from .retrieval import (
    EMBEDDER_NAMES,
    HARD_CAP_K,
    Retriever,
    build_retriever,
    render_examples_block,
    split_question_evidence,
)
from .schema_pruning import prune_schema
from .self_consistency import (
    Candidate,
    HARD_CAP_N,
    choose,
    clamp_candidate_count,
    sampling_args,
)
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

    # -- optional procedure arms, both OFF by default (docs/PROCEDURES.md) ---------
    # The two arms below are orthogonal to the adapter: they change what the model
    # is asked and how its samples are combined, not the weights. Keeping them off
    # by default is what makes "adapter + procedure" a separate, measurable cell
    # instead of a silent change to the round-1 baseline.
    retrieval_few_shot: bool = False
    """Inject the k most similar *training* questions as worked examples."""
    retrieval_k: int = 3
    """Capped at ``retrieval.HARD_CAP_K``; each example is prompt tokens per call."""
    retrieval_min_similarity: float | None = None
    """None uses the embedder's own default: the two backends share no score scale."""
    retrieval_include_evidence: bool = True
    """Whether evidence joins the similarity query and the rendered example block."""
    retrieval_bank: str | None = None
    """None resolves to ``data/train_queries.json``; dev files are refused."""
    retrieval_embedder: str = "sentence-transformer"
    """``sentence-transformer`` (local weights only) or ``hash`` (dependency-free)."""
    retrieval_model: str | None = None
    """None resolves to the embedder's default; the weights must already be on disk."""
    db_id: str | None = None
    """Override the database name scoping the bank; derived from ``db_uri`` otherwise."""
    self_consistency: bool = False
    """Draw n candidates and vote: result sets first, normalised SQL text second."""
    self_consistency_n: int = 5
    """Capped at ``self_consistency.HARD_CAP_N``; every candidate is a full LLM call."""
    self_consistency_temperature: float = 0.7
    self_consistency_seed: int = 0
    """Candidate i is drawn with ``seed + i``; the round is therefore reproducible."""
    self_consistency_budget_s: float = 120.0
    """Wall-clock cap on the whole round (generation *and* execution); partial rounds vote."""
    self_consistency_row_cap: int = 200
    """Rows kept per candidate for the result-set comparison."""
    procedure_log: str | None = None
    """JSONL path for per-question arm metadata; a relative path lands in the run dir."""


PROCEDURE_ENV_VARS: Final[Mapping[str, str]] = {
    "retrieval_few_shot": "TEXT2SQL_RETRIEVAL_FEWSHOT",
    "retrieval_k": "TEXT2SQL_RETRIEVAL_K",
    "retrieval_min_similarity": "TEXT2SQL_RETRIEVAL_MIN_SIMILARITY",
    "retrieval_include_evidence": "TEXT2SQL_RETRIEVAL_EVIDENCE",
    "retrieval_bank": "TEXT2SQL_RETRIEVAL_BANK",
    "retrieval_embedder": "TEXT2SQL_RETRIEVAL_EMBEDDER",
    "retrieval_model": "TEXT2SQL_RETRIEVAL_MODEL",
    "self_consistency": "TEXT2SQL_SELF_CONSISTENCY",
    "self_consistency_n": "TEXT2SQL_SC_N",
    "self_consistency_temperature": "TEXT2SQL_SC_TEMPERATURE",
    "self_consistency_seed": "TEXT2SQL_SC_SEED",
    "self_consistency_budget_s": "TEXT2SQL_SC_BUDGET_S",
    "self_consistency_row_cap": "TEXT2SQL_SC_ROW_CAP",
    "procedure_log": "TEXT2SQL_PROCEDURE_LOG",
}
"""Env overrides for the procedure arms.

Why the environment and not a constructor argument: ``benchmarks/base.py`` builds
the tool as ``tool_cls(db_uri=..., llm_client=...)`` and that file is part of the
sealed harness contract (ADR-001). An eval arm is therefore switched on by the
process environment, which ``dev_tools/run_benchmark.py`` snapshots into run.json
under ``procedure_env`` - so an artifact still says which arms produced it.
"""

_TRUTHY: Final = frozenset({"1", "true", "yes", "on"})
_FALSY: Final = frozenset({"0", "false", "no", "off"})


def _env_bool(name: str, raw: str) -> bool:
    value = raw.strip().casefold()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    raise ValueError(f"{name}={raw!r} is not a boolean; use one of {sorted(_TRUTHY | _FALSY)}")


def _env_int(name: str, raw: str) -> int:
    try:
        return int(raw.strip())
    except ValueError as error:
        raise ValueError(f"{name}={raw!r} is not an integer") from error


def _env_float(name: str, raw: str) -> float:
    try:
        return float(raw.strip())
    except ValueError as error:
        raise ValueError(f"{name}={raw!r} is not a number") from error


def _env_str(name: str, raw: str) -> str:
    return raw.strip()


_ENV_PARSERS: Final[Mapping[str, Any]] = {
    "retrieval_few_shot": _env_bool,
    "retrieval_k": _env_int,
    "retrieval_min_similarity": _env_float,
    "retrieval_include_evidence": _env_bool,
    "retrieval_bank": _env_str,
    "retrieval_embedder": _env_str,
    "retrieval_model": _env_str,
    "self_consistency": _env_bool,
    "self_consistency_n": _env_int,
    "self_consistency_temperature": _env_float,
    "self_consistency_seed": _env_int,
    "self_consistency_budget_s": _env_float,
    "self_consistency_row_cap": _env_int,
    "procedure_log": _env_str,
}


def resolve_procedure_config(
    config: Text2SQLConfig, env: Mapping[str, str] | None = None
) -> Text2SQLConfig:
    """Apply ``TEXT2SQL_*`` overrides to a config; raise on a malformed value.

    Raising (rather than ignoring) mirrors :func:`resolve_ambiguity_mode`: a
    typo that silently disables an arm would make a run report numbers for a
    configuration nobody chose. Explicit constructor values win over the
    environment only for fields the environment does not set, so an arm is never
    switched on by accident.
    """

    values = os.environ if env is None else env
    overrides: dict[str, Any] = {}
    for field_name, env_name in PROCEDURE_ENV_VARS.items():
        raw = values.get(env_name)
        if raw is None or not str(raw).strip():
            continue
        overrides[field_name] = _ENV_PARSERS[field_name](env_name, str(raw))
    if not overrides:
        return config
    return replace(config, **overrides)


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
        self.config = resolve_procedure_config(config or Text2SQLConfig())
        self._validate_procedure_config()
        self.ambiguous_mode = resolve_ambiguity_mode(self.config.ambiguous_mode)

        self.db_schema: str = ""
        self.schema_source: str = "unset"
        self.system_prompt: str = ""
        self.usage: dict[str, int] = dict.fromkeys(_USAGE_KEYS, 0)
        self.llm_calls: int = 0
        self.last_selected_tables: tuple[str, ...] = ()

        # Procedure-arm state. It is reset per question and exposed through
        # ``procedure_metadata()`` and the optional procedure log, so an artefact can
        # show exactly which examples were injected and how a vote resolved.
        self.last_retrieval: dict[str, Any] = {
            "injected": 0,
            "examples": [],
            "block": "",
            "block_sha256": None,
            "query_used_evidence": False,
        }
        self.last_self_consistency: dict[str, Any] | None = None
        self._retriever: Retriever | None = None
        self._retriever_ready = False
        self._procedure_log_failed = False
        self._sampling_args_unsupported = False

        self._schema_override = schema
        self._engine = engine

    def _validate_procedure_config(self) -> None:
        """Refuse a procedure configuration that could only produce misleading numbers.

        Range errors are loud at construction time, exactly like an unknown
        ambiguity mode: nine candidates where the cap is nine is a clamp (logged),
        but ``temperature=0`` in self-consistency is a contradiction - the greedy
        path already exists - and a zero budget would silently disable the arm.
        """

        config = self.config
        if config.retrieval_few_shot:
            if int(config.retrieval_k) < 1:
                raise ValueError("retrieval_k must be >= 1")
            if int(config.retrieval_k) > HARD_CAP_K:
                logger.warning(
                    "retrieval_k=%d is capped at %d examples per call",
                    config.retrieval_k,
                    HARD_CAP_K,
                )
            embedder = str(config.retrieval_embedder).strip().lower()
            if embedder not in EMBEDDER_NAMES:
                raise ValueError(
                    f"unknown retrieval_embedder {config.retrieval_embedder!r}; "
                    f"expected one of {EMBEDDER_NAMES}"
                )
            threshold = config.retrieval_min_similarity
            if threshold is not None and not -1.0 <= float(threshold) <= 1.0:
                raise ValueError("retrieval_min_similarity must be within [-1, 1]")
        if config.self_consistency:
            if int(config.self_consistency_n) < 1:
                raise ValueError("self_consistency_n must be >= 1")
            if not float(config.self_consistency_temperature) > 0.0:
                raise ValueError(
                    "self-consistency needs a temperature > 0; leave the flag off for greedy decoding"
                )
            if float(config.self_consistency_budget_s) <= 0.0:
                raise ValueError("self_consistency_budget_s must be > 0")
            if int(config.self_consistency_row_cap) < 1:
                raise ValueError("self_consistency_row_cap must be >= 1")
            if int(config.self_consistency_n) > HARD_CAP_N:
                logger.warning(
                    "self_consistency_n=%d is capped at %d candidates",
                    config.self_consistency_n,
                    HARD_CAP_N,
                )

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
        self.last_self_consistency = None
        self.last_retrieval = {
            "injected": 0,
            "examples": [],
            "block": "",
            "block_sha256": None,
            "query_used_evidence": False,
        }
        if not question:
            logger.warning("Empty question received; nothing to translate")
            return {"status": "error", "query": None}

        if not self.system_prompt:
            self.build()

        try:
            verdict = await self._assess_ambiguity(question)
            if self.ambiguous_mode == "strict" and verdict.ambiguous:
                logger.info("Question treated as ambiguous (%s): %s", verdict.detail, question)
                return {"status": "ambiguous", "query": None}

            schema = self._schema_for(question)
            examples_block = self._retrieve_examples(question)
            sql = await self._generate_valid_sql(question, schema, examples_block=examples_block)
            if sql is None:
                return {"status": "error", "query": None}

            sql = await self._refine_with_execution(question, schema, sql)
            return {"status": "success", "query": sql}
        finally:
            # Written on every exit path (success, error, unexpected failure) so the
            # artefact shows the arm state for the question that was actually run.
            self._record_procedure(question)

    # --------------------------------------------------------------- procedures

    def _db_id(self) -> str:
        """Database name used to scope the few-shot bank; never the DSN itself."""

        if self.config.db_id:
            return str(self.config.db_id).strip()
        tail = (self.db_uri or "").rsplit("/", 1)[-1]
        return tail.split("?", 1)[0].strip()

    def _get_retriever(self) -> Retriever | None:
        """Build the retriever once per tool; ``None`` when the arm is off.

        Lazy on purpose: the bank costs an embed pass, and a run that never
        enables the arm must not pay for it (nor load weights it does not use).
        ``build_retriever`` returns an *unavailable* retriever with a reason
        instead of raising, so a missing model can never abort a benchmark.
        """

        if self._retriever_ready:
            return self._retriever
        self._retriever_ready = True
        if not self.config.retrieval_few_shot:
            return None
        self._retriever = build_retriever(
            source=self.config.retrieval_bank,
            embedder=self.config.retrieval_embedder,
            model=self.config.retrieval_model,
            k=self.config.retrieval_k,
            min_similarity=self.config.retrieval_min_similarity,
            include_evidence=self.config.retrieval_include_evidence,
        )
        return self._retriever

    def _retrieve_examples(self, question: str) -> str:
        """Retrieve the worked-example block for one question; ``""`` when the arm is off.

        Records the injected examples, the exact block and its hash in
        ``last_retrieval`` - that is what makes the arm visible in run metadata
        instead of inferable from an accuracy difference.
        """

        if not self.config.retrieval_few_shot:
            return ""
        retriever = self._get_retriever()
        if retriever is None or not retriever.available:
            return ""
        plain_question, evidence = split_question_evidence(question)
        if not self.config.retrieval_include_evidence:
            evidence = ""
        examples = retriever.retrieve(plain_question, db_id=self._db_id(), evidence=evidence)
        block = render_examples_block(
            examples, include_evidence=self.config.retrieval_include_evidence
        )
        self.last_retrieval = {
            "injected": len(examples),
            "examples": [example.metadata() for example in examples],
            "block": block,
            "block_sha256": hashlib.sha256(block.encode("utf-8")).hexdigest() if block else None,
            "query_used_evidence": bool(evidence),
        }
        return block

    def procedure_metadata(self) -> dict[str, Any]:
        """Procedure-arm configuration, bank provenance and the last question's state.

        ``dev_tools/run_benchmark.py`` snapshots the ``TEXT2SQL_*`` environment into
        ``run.json``; this method is the richer, per-question surface used by the
        procedure log and by the offline dry run.
        """

        retrieval: dict[str, Any] = {
            "enabled": bool(self.config.retrieval_few_shot),
            "k": int(self.config.retrieval_k),
            "min_similarity": self.config.retrieval_min_similarity,
            "include_evidence_in_query": bool(self.config.retrieval_include_evidence),
            "requested_embedder": self.config.retrieval_embedder,
            "bank": self.config.retrieval_bank,
            "last": self.last_retrieval,
        }
        if self.config.retrieval_few_shot:
            retriever = self._get_retriever()
            if retriever is not None:
                retrieval.update(retriever.metadata())
                retrieval["enabled"] = True
                retrieval["last"] = self.last_retrieval
        consistency: dict[str, Any] = {
            "enabled": bool(self.config.self_consistency),
            "n": clamp_candidate_count(self.config.self_consistency_n),
            "temperature": float(self.config.self_consistency_temperature),
            "seed": int(self.config.self_consistency_seed),
            "budget_s": float(self.config.self_consistency_budget_s),
            "row_cap": int(self.config.self_consistency_row_cap),
            "last": self.last_self_consistency,
        }
        return {
            "retrieval_few_shot": retrieval,
            "self_consistency": consistency,
            "procedure_log": self.config.procedure_log,
        }

    def _record_procedure(self, question: str) -> None:
        """Append one JSONL record with the arms' state for this question.

        Never raises: a broken log path must not cost a benchmark item. The DSN is
        deliberately absent (it carries credentials); only the database *name* is
        written.
        """

        path = self.config.procedure_log
        if not path:
            return
        if not (self.config.retrieval_few_shot or self.config.self_consistency):
            return
        record = {
            "db_id": self._db_id(),
            "question": question,
            "retrieval": self.last_retrieval,
            "self_consistency": self.last_self_consistency,
            "config": {
                "retrieval_few_shot": bool(self.config.retrieval_few_shot),
                "retrieval_k": int(self.config.retrieval_k),
                "retrieval_include_evidence": bool(self.config.retrieval_include_evidence),
                "retrieval_embedder": self.config.retrieval_embedder,
                "self_consistency": bool(self.config.self_consistency),
                "self_consistency_n": clamp_candidate_count(self.config.self_consistency_n),
                "self_consistency_temperature": float(self.config.self_consistency_temperature),
                "self_consistency_seed": int(self.config.self_consistency_seed),
            },
        }
        if self._procedure_log_failed:
            return
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n")
        except OSError:
            self._procedure_log_failed = True
            logger.exception("Could not append to the procedure log %s; continuing without it", path)

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
        examples_block: str = "",
        sampling: Mapping[str, Any] | None = None,
    ) -> str | None:
        """Ask the model for SQL; ``repair`` switches to a correction prompt.

        ``examples_block`` carries the retrieval arm's worked examples into the
        *initial* generation only: a repair prompt already contains the rejected
        SQL and the error, and adding examples there lengthens the call without
        addressing either. ``sampling`` carries the self-consistency arm's
        ``temperature``/``seed`` through ``extra_create_args``.
        """

        system = SYSTEM_PROMPT_TEMPLATE.format(db_schema=schema, sql_dialect=SQL_DIALECT)
        if repair is None:
            if examples_block:
                user = FEW_SHOT_PROMPT_TEMPLATE.format(
                    examples=examples_block, user_query=question, sql_dialect=SQL_DIALECT
                )
            else:
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
            ],
            extra_create_args=sampling,
        )

    async def _generate_valid_sql(
        self, question: str, schema: str, *, examples_block: str = ""
    ) -> str | None:
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

        Self-consistency (OFF by default) replaces the single generation with ``n``
        sampled candidates and a majority vote; its own budget is separate and bounded
        by ``self_consistency_budget_s``.
        """

        if self.config.self_consistency:
            return await self._self_consistent_sql(question, schema, examples_block)

        raw = await self._generate(question, schema, examples_block=examples_block)
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

    async def _self_consistent_sql(
        self, question: str, schema: str, examples_block: str
    ) -> str | None:
        """Sample ``n`` seeded candidates, execute what is valid, return the vote.

        Cost is bounded three ways: ``n`` is clamped to ``HARD_CAP_N``, the whole
        round (generation *and* execution) obeys ``self_consistency_budget_s``, and
        once one candidate proves the database unreachable the remaining executions
        are skipped. A round that produced no valid SQL returns ``None`` - the same
        posture as a single failed generation - and nothing here raises.
        """

        n = clamp_candidate_count(self.config.self_consistency_n)
        temperature = float(self.config.self_consistency_temperature)
        seed = int(self.config.self_consistency_seed)
        budget = float(self.config.self_consistency_budget_s)
        started = time.monotonic()
        db_available = self._engine is not None or create_engine is not None
        db_down = not db_available
        candidates: list[Candidate] = []

        for index in range(n):
            if index and time.monotonic() - started > budget:
                logger.warning(
                    "Self-consistency budget of %.1fs spent after %d/%d candidates",
                    budget,
                    index,
                    n,
                )
                break
            try:
                raw = await self._generate(
                    question,
                    schema,
                    examples_block=examples_block,
                    sampling=sampling_args(index, temperature, seed),
                )
            except Exception:  # noqa: BLE001 - a failed draw is a missing candidate
                logger.exception("Self-consistency candidate %d failed; continuing", index)
                continue

            validation = self._validate(raw)
            candidate = Candidate(
                index=index,
                sql=validation.sql if validation.ok else None,
                valid=validation.ok,
                error=(validation.error or "") if not validation.ok else "",
            )
            if candidate.valid and candidate.sql:
                if db_down:
                    candidate = replace(
                        candidate,
                        execution="unreachable" if not db_available else "skipped",
                        execution_error=(
                            "no database engine" if not db_available else "database unreachable"
                        ),
                    )
                else:
                    status, rows, error = self._execute_rows(candidate.sql)
                    if status == "unreachable":
                        db_down = True
                    candidate = replace(
                        candidate,
                        execution=status,
                        rows=rows if status == "ok" else None,
                        execution_error=error,
                    )
            candidates.append(candidate)

        selection = choose(candidates)
        self.last_self_consistency = {
            "n": n,
            "drawn": len(candidates),
            "temperature": temperature,
            "seed": seed,
            "budget_s": budget,
            "elapsed_s": round(time.monotonic() - started, 3),
            "database_executed": any(candidate.execution == "ok" for candidate in candidates),
            "selection": selection.metadata(),
            "selected_sql": selection.sql,
            "candidates": [candidate.metadata() for candidate in candidates],
        }
        if selection.sql is None:
            logger.warning(
                "Self-consistency produced no valid SQL (%d candidates): %s",
                len(candidates),
                selection.detail,
            )
        return selection.sql

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

    def _execute_rows(self, sql: str) -> tuple[str, tuple[tuple[Any, ...], ...] | None, str]:
        """Execute one candidate and return ``(status, rows, error)`` for the vote.

        Mirrors :meth:`_execute_for_feedback` (same statement timeout, same
        unreachable/rejected split) but keeps up to ``self_consistency_row_cap``
        rows because the self-consistency vote compares result sets. The row cap is
        what keeps a wide ``SELECT *`` from turning ``n`` executions into a memory
        problem; it is a comparison budget, not a score.
        """

        cap = max(1, int(self.config.self_consistency_row_cap))
        if self._engine is None and create_engine is None:
            return "unreachable", None, "sqlalchemy is not installed"
        try:
            engine = self._get_engine()
        except Exception as error:
            return "unreachable", None, str(error)

        try:
            with engine.connect() as connection:
                self._apply_statement_timeout(connection)
                statement = sql_text(sql) if sql_text is not None else sql
                rows = connection.execute(statement).fetchmany(cap)
        except Exception as error:
            if _is_connection_error(error):
                return "unreachable", None, str(error)
            return "rejected", None, str(error)
        return "ok", tuple(tuple(row) for row in rows or ()), ""

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

    async def _create(self, messages: Sequence[Any], extra_create_args: Mapping[str, Any] | None) -> Any:
        """One client call, tolerating a client that cannot take sampling parameters.

        The harness client is an ``autogen`` ``ChatCompletionClient``, whose
        ``create`` accepts ``extra_create_args`` (``temperature``/``seed`` override
        the client-level ``temperature=0`` set by ``benchmarks/base.py``). A stub or
        an older client that does not accept the keyword must not break the arm, so
        the fallback drops the parameters and logs it once - the run then reports
        greedy decoding instead of pretending to have sampled.
        """

        if not extra_create_args:
            return await self.llm_client.create(messages)
        try:
            return await self.llm_client.create(messages, extra_create_args=dict(extra_create_args))
        except TypeError:
            if not self._sampling_args_unsupported:
                self._sampling_args_unsupported = True
                logger.warning(
                    "The LLM client does not accept extra_create_args; self-consistency "
                    "falls back to the client's decoding settings"
                )
            return await self.llm_client.create(messages)

    async def _complete(
        self, messages: Sequence[Any], extra_create_args: Mapping[str, Any] | None = None
    ) -> str | None:
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
                    self._create(messages, extra_create_args), timeout=self.config.llm_timeout_s
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
