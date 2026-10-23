"""Offline contract tests for the Text2SQL tool.

No database, no network, no API key: every LLM reply comes from ``StubLLM`` and
every database interaction from a fake engine. The tests therefore pin the
behaviour the harness depends on (never-raise, ToolResponse shape, sqlglot gate,
single repair, ambiguity tolerance, pruning recall) rather than a model's output.

The tool is imported through its production dotted path
(``adv_text2sql.mcp_servers.text2sql_tool.src...``) because that is the path
``bird_benchmark.py`` uses; the missing optional dependencies of the surrounding
package are stubbed instead of bypassed.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest
import sqlglot
from sqlglot import exp

REPO_ROOT = Path(__file__).resolve().parents[1]


def _bootstrap_import_path() -> None:
    """Make the repo importable offline.

    ``adv_text2sql.mcp_servers.__init__`` imports ``fastmcp``, which is a runtime
    dependency of the MCP server, not of the benchmark tool. Stubbing it keeps
    the production import path under test without installing the world.
    """
    for path in (str(REPO_ROOT), str(REPO_ROOT / "src")):
        if path not in sys.path:
            sys.path.insert(0, path)
    try:
        import fastmcp  # noqa: F401
    except ImportError:
        stub = types.ModuleType("fastmcp")
        stub.FastMCP = type("FastMCP", (), {})  # type: ignore[attr-defined]
        sys.modules["fastmcp"] = stub


_bootstrap_import_path()

from benchmarks.response import ToolResponse  # noqa: E402
from adv_text2sql.mcp_servers.text2sql_tool.src.prompts import (  # noqa: E402
    AMBIGUITY_PROMPT_TEMPLATE,
    SQL_DIALECT,
    SQL_PROMPT_TEMPLATE,
    SYSTEM_PROMPT_TEMPLATE,
)
from adv_text2sql.mcp_servers.text2sql_tool.src.schema_pruning import (  # noqa: E402
    parse_schema,
    prune_schema,
)
from adv_text2sql.mcp_servers.text2sql_tool.src.sql_utils import (  # noqa: E402
    canonicalise_division,
    normalise_identifiers,
    strip_comments,
    strip_code_fence,
    validate_sql,
)
from adv_text2sql.mcp_servers.text2sql_tool.src import text2sql_implementation  # noqa: E402
from adv_text2sql.mcp_servers.text2sql_tool.src.text2sql_implementation import (  # noqa: E402
    AMBIGUITY_ENV_VAR,
    DEFAULT_AMBIGUITY_MODE,
    Text2SQLConfig,
    Text2SQLGenerator,
    parse_ambiguity_answer,
)

AMBIGUITY_MARKER = "audit a natural-language question"
SYNTAX_REPAIR_MARKER = "did not parse as valid"
EXECUTION_REPAIR_MARKER = "The database rejected the previous SQL"

VALID_SQL = """SELECT COUNT(*) FROM frpm WHERE "County Name" = 'Alameda'"""
INVALID_SQL = "SELEC County FROM frpm WHERE"
SLEEP_FOREVER = object()

PROMPTS_PATH = (
    REPO_ROOT
    / "src"
    / "adv_text2sql"
    / "mcp_servers"
    / "text2sql_tool"
    / "src"
    / "prompts.py"
)
TRAIN_QUERIES_PATH = REPO_ROOT / "data" / "train_queries.json"


# --------------------------------------------------------------------------- stubs


class StubLLM:
    """Deterministic stand-in for ``autogen``'s ``OpenAIChatCompletionClient``.

    The responder sees the concatenated prompt text and the call index, so a test
    can model a model that answers differently on the ambiguity call, the SQL call
    and the repair call.
    """

    def __init__(self, responder: Callable[[str, int], Any]) -> None:
        self._responder = responder
        self.calls: list[list[Any]] = []

    async def create(self, messages: list[Any]) -> Any:
        self.calls.append(list(messages))
        prompt_text = "\n".join(str(getattr(message, "content", "")) for message in messages)
        reply = self._responder(prompt_text, len(self.calls))
        if isinstance(reply, BaseException):
            raise reply
        if reply is SLEEP_FOREVER:
            await asyncio.sleep(3600)
        usage = SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18)
        return SimpleNamespace(content=reply, usage=usage)


def make_responder(
    *,
    ambiguity: Any = "OK",
    sql: Any = VALID_SQL,
    syntax_repair: Any = None,
    execution_repair: Any = None,
) -> Callable[[str, int], Any]:
    """Route replies by the prompt that was sent, mirroring the pipeline order."""

    def responder(prompt: str, _call_index: int) -> Any:
        if AMBIGUITY_MARKER in prompt:
            return ambiguity
        if SYNTAX_REPAIR_MARKER in prompt:
            return sql if syntax_repair is None else syntax_repair
        if EXECUTION_REPAIR_MARKER in prompt:
            return sql if execution_repair is None else execution_repair
        return sql

    return responder


def make_tool(
    *,
    responder: Callable[[str, int], Any] | None = None,
    config: Text2SQLConfig | None = None,
    schema: str | None = "TABLE frpm\n  - County Name (TEXT)",
    engine: Any = None,
    build: bool = True,
) -> tuple[Text2SQLGenerator, StubLLM]:
    """Construct the tool the way ``benchmarks/base.py`` does (plus test seams).

    Defaults to :func:`offline_config`, not the production defaults, so a tool built
    for a shape/plumbing test can never open a database connection; pass
    ``config=Text2SQLConfig()`` explicitly when the real defaults are under test.
    """
    llm = StubLLM(responder or make_responder())
    tool = Text2SQLGenerator(
        db_uri="postgresql+psycopg://user:pass@localhost:5444/fixture",
        llm_client=llm,
        schema=schema,
        engine=engine,
        config=offline_config() if config is None else config,
    )
    if build:
        tool.build()
    return tool, llm


def offline_config(**overrides: Any) -> Text2SQLConfig:
    """Fast, deterministic config: no ambiguity call, no sleeps, no database."""
    defaults: dict[str, Any] = {
        "ambiguous_mode": "off",
        "max_llm_retries": 0,
        "retry_backoff_s": 0.0,
        "execution_feedback": False,
    }
    defaults.update(overrides)
    return Text2SQLConfig(**defaults)


def ask(tool: Text2SQLGenerator, question: Any = "How many schools are in Alameda County?") -> dict[str, Any]:
    """Run one query and prove the harness can validate the reply."""
    result = asyncio.run(tool.query(question))
    assert isinstance(result, dict)
    parsed = ToolResponse.model_validate(result)
    assert parsed.status in {"success", "ambiguous", "error"}
    return result


class OperationalError(Exception):
    """Name-matches sqlalchemy's connection-level error without importing it."""


class ProgrammingError(Exception):
    """Name-matches sqlalchemy's SQL-level error without importing it."""


class _FakeResult:
    def fetchmany(self, _size: int = 1) -> list[tuple[int]]:
        return [(1,)]

    def fetchall(self) -> list[tuple[int]]:
        return [(1,)]


class _FakeConnection:
    def __init__(self, error: BaseException | None) -> None:
        self._error = error

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, statement: Any) -> _FakeResult:
        text = str(getattr(statement, "text", statement))
        if text.upper().startswith("SET "):
            return _FakeResult()
        if self._error is not None:
            raise self._error
        return _FakeResult()


class _FakeEngine:
    """Minimal SQLAlchemy engine surface used by the execution-feedback path."""

    def __init__(self, error: BaseException | None = None) -> None:
        self._error = error
        self.connections = 0

    def connect(self) -> _FakeConnection:
        self.connections += 1
        return _FakeConnection(self._error)


@pytest.fixture(autouse=True)
def _forbid_database_engines(monkeypatch: Any) -> Any:
    """No test in this file may create a real database engine.

    Verification round 2 (finding of t18): once sqlalchemy and psycopg became
    importable in the shared interpreter, a tool built with the *default* config
    reached the execution-feedback path and created a real engine, so the
    harness-contract test dialled a database it never needed. A failed engine
    creation is swallowed by the tool as "database unreachable", so patching alone
    would hide the attempt again - the teardown assertion is what makes it visible.
    """
    attempts: list[tuple[Any, ...]] = []

    def recording_create_engine(*args: Any, **kwargs: Any) -> Any:
        attempts.append(args)
        raise AssertionError("unit tests must not create database engines")

    monkeypatch.setattr(text2sql_implementation, "create_engine", recording_create_engine)
    yield
    assert not attempts, f"the tool tried to create a database engine: {attempts}"


# ------------------------------------------------------------------ harness contract


class _UnreachableEngine:
    """Engine stand-in whose connections fail the way a down database fails.

    An injected engine instead of a DSN on purpose: a refused/timed-out real connect
    makes the suite depend on network behaviour (psycopg waits ~130 s on a filtered
    port in this environment) while exercising the same ``build`` handler. It counts
    connection attempts so a test can prove the engine was never even touched.
    """

    def __init__(self) -> None:
        self.connections = 0

    def connect(self) -> Any:
        self.connections += 1
        raise OperationalError("connection refused")


def test_harness_contract_is_unchanged() -> None:
    """``base.py`` calls ``tool_cls(db_uri=..., llm_client=...)`` then ``build()``.

    The test only asserts the constructor/pipeline shape, so it must never dial: the
    tool is built with ``offline_config()`` (no execution feedback) and an injected
    unreachable engine, and the engine is asserted untouched.
    """
    signature = inspect.signature(Text2SQLGenerator.__init__)
    parameters = signature.parameters
    assert "db_uri" in parameters
    assert "llm_client" in parameters
    assert not inspect.iscoroutinefunction(Text2SQLGenerator.build)
    assert inspect.iscoroutinefunction(Text2SQLGenerator.query)

    engine = _UnreachableEngine()
    tool, _llm = make_tool(build=False, config=offline_config(), engine=engine)
    tool.build()

    assert isinstance(tool.db_schema, str)
    assert tool.db_schema
    # base.py dumps {db_id: tool.db_schema} to JSON before predicting.
    assert json.loads(json.dumps({"fixture": tool.db_schema}))["fixture"] == tool.db_schema

    result = ask(tool)
    assert result["status"] == "success"
    assert result["query"]
    assert engine.connections == 0, "the harness-contract test must not touch a database"


def test_build_without_database_degrades_instead_of_raising() -> None:
    """A broken DB must not abort the benchmark run inside ``build``."""
    tool = Text2SQLGenerator(
        db_uri="postgresql+psycopg://nobody@127.0.0.1:1/missing",
        llm_client=StubLLM(make_responder()),
        schema=None,
        engine=_UnreachableEngine(),
        config=offline_config(),
    )
    tool.build()
    assert tool.db_schema == ""
    assert tool.schema_source == "unavailable"
    assert isinstance(tool.system_prompt, str) and tool.system_prompt

    # A schema-less tool still answers (the model may guess from the question) and
    # still never raises; only the prompt lost the schema block.
    result = ask(tool)
    assert result["status"] in {"success", "error"}
    assert "TABLE" not in tool.system_prompt


def test_build_with_empty_database_still_sets_a_schema() -> None:
    """An empty database is a schema of zero tables, not a crash."""
    sqlalchemy = pytest.importorskip("sqlalchemy")  # the DB layer is an optional extra
    tool = Text2SQLGenerator(
        db_uri="sqlite://",
        llm_client=StubLLM(make_responder()),
        schema=None,
        engine=sqlalchemy.create_engine("sqlite://"),
        config=offline_config(),
    )
    tool.build()
    assert tool.db_schema == ""
    assert tool.schema_source == "database"
    assert isinstance(tool.system_prompt, str) and tool.system_prompt


# ----------------------------------------------------------------- never raises


def ask_tool(reply: Any, config: Text2SQLConfig, question: Any) -> dict[str, Any]:
    """Helper: build a tool whose model always replies with ``reply``."""

    def responder(_prompt: str, _index: int) -> Any:
        return reply

    tool, _llm = make_tool(responder=responder, config=config)
    return ask(tool, question)


def test_query_never_raises_on_hostile_input() -> None:
    """Any input and any LLM failure returns a ToolResponse-shaped dict."""

    def raising(_prompt: str, _index: int) -> Any:
        raise RuntimeError("transport exploded")

    scenarios: list[tuple[str, Callable[[], dict[str, Any]]]] = []
    scenarios.append(
        (
            "transport-error",
            lambda: ask_tool(raising, offline_config(max_llm_retries=1), "question"),
        )
    )
    scenarios.append(
        ("timeout", lambda: ask_tool(SLEEP_FOREVER, offline_config(llm_timeout_s=0.01, max_llm_retries=0), "question"))
    )
    scenarios.append(
        ("malformed-json", lambda: ask_tool("{not json: [", offline_config(), "question"))
    )
    scenarios.append(("empty-question", lambda: ask_tool(VALID_SQL, offline_config(), "")))
    scenarios.append(("whitespace-question", lambda: ask_tool(VALID_SQL, offline_config(), "   \n ")))
    scenarios.append(("1000-char-question", lambda: ask_tool(VALID_SQL, offline_config(), "why " * 250)))
    scenarios.append(("non-string", lambda: ask_tool(VALID_SQL, offline_config(), 12345)))
    scenarios.append(("none", lambda: ask_tool(VALID_SQL, offline_config(), None)))
    scenarios.append(("invalid-sql", lambda: ask_tool(INVALID_SQL, offline_config(), "question")))
    scenarios.append(
        (
            "client-cancels-itself",
            lambda: ask_tool(asyncio.CancelledError(), offline_config(max_llm_retries=1), "question"),
        )
    )
    scenarios.append(
        (
            "default-config-transport-error",
            lambda: ask_tool(raising, Text2SQLConfig(ambiguous_mode="off", retry_backoff_s=0.0, execution_feedback=False), "question"),
        )
    )

    for name, scenario in scenarios:
        result = scenario()
        assert isinstance(result, dict), name
        assert "status" in result, name
        # Only ToolResponse fields may reach the harness; nothing debug-shaped.
        assert set(result) <= {"status", "query"}, name


# ------------------------------------------------------------------- ambiguity


AMBIGUITY_CASES: list[tuple[str, bool]] = [
    ("OK", False),
    ("OK.", False),
    ("ok\n", False),
    ("Ok!", False),
    ("```OK```", False),
    ("  ok  ", False),
    ("OK, ready to translate", False),
    ("OKAY", True),
    ("AMBIGUOUS: which time period applies?", True),
    ("ambiguous - missing aggregation", True),
    ("NO", True),
    ("unclear", True),
    ("not answerable", True),
    ("", True),
    ("```\n```", True),
]


def test_ambiguity_parsing_is_tolerant_but_strict_about_no() -> None:
    """Formatting tolerance must not turn a refusal into an ``OK``."""
    assert len(AMBIGUITY_CASES) >= 10
    for reply, expected_ambiguous in AMBIGUITY_CASES:
        verdict = parse_ambiguity_answer(reply)
        assert verdict.ambiguous is expected_ambiguous, reply
        assert verdict.checked is True


def test_ambiguity_modes_and_default(monkeypatch: Any) -> None:
    """The default protects BIRD; ``strict`` is opt-in; ``off`` drops the call.

    ``make_tool`` defaults to ``offline_config()`` (mode ``off``), so the production
    defaults are requested explicitly here: this test is exactly the place where the
    real ``Text2SQLConfig()`` resolution must be observed.
    """
    monkeypatch.delenv(AMBIGUITY_ENV_VAR, raising=False)
    default_tool, _ = make_tool(config=Text2SQLConfig())
    assert default_tool.ambiguous_mode == DEFAULT_AMBIGUITY_MODE == "birdsafe"

    monkeypatch.setenv(AMBIGUITY_ENV_VAR, "strict")
    env_tool, _ = make_tool(config=Text2SQLConfig())
    assert env_tool.ambiguous_mode == "strict"

    explicit_tool, _ = make_tool(config=Text2SQLConfig(ambiguous_mode="off"))
    assert explicit_tool.ambiguous_mode == "off"

    with pytest.raises(ValueError):
        Text2SQLGenerator(
            db_uri="postgresql+psycopg://u:p@localhost/fixture",
            llm_client=StubLLM(make_responder()),
            schema="TABLE t\n  - a (TEXT)",
            config=Text2SQLConfig(ambiguous_mode="sometimes"),
        )


def test_birdsafe_keeps_the_item_strict_loses_it() -> None:
    """Same model answer, opposite harness outcome: this is why birdsafe is default."""
    ambiguous_reply = "AMBIGUOUS: which time period applies?"

    strict_tool, strict_llm = make_tool(
        responder=make_responder(ambiguity=ambiguous_reply),
        config=offline_config(ambiguous_mode="strict"),
    )
    strict_result = ask(strict_tool)
    assert strict_result["status"] == "ambiguous"
    assert strict_result["query"] is None
    assert len(strict_llm.calls) == 1

    safe_tool, safe_llm = make_tool(
        responder=make_responder(ambiguity=ambiguous_reply),
        config=offline_config(ambiguous_mode="birdsafe"),
    )
    safe_result = ask(safe_tool)
    assert safe_result["status"] == "success"
    assert safe_result["query"] == VALID_SQL
    assert len(safe_llm.calls) == 2  # gate + generation

    off_tool, off_llm = make_tool(
        responder=make_responder(ambiguity=ambiguous_reply),
        config=offline_config(ambiguous_mode="off"),
    )
    off_result = ask(off_tool)
    assert off_result["status"] == "success"
    assert len(off_llm.calls) == 1  # no gate call at all


# --------------------------------------------------------------- SQL gate + repair


def test_sql_gate_rejects_non_select_statements() -> None:
    """Predictions are executed verbatim, so DDL/DML must never leave the tool."""
    for statement in (
        "INSERT INTO frpm VALUES (1)",
        "UPDATE frpm SET x = 1",
        "DELETE FROM frpm",
        "DROP TABLE frpm",
    ):
        validation = validate_sql(statement)
        assert validation.ok is False, statement
        assert validation.sql is None
        result = ask_tool(statement, offline_config(), "question")
        assert result["status"] == "error", statement
        assert result["query"] is None


def test_sql_gate_normalises_fences_backticks_and_comments() -> None:
    """A correct query wrapped in markdown would otherwise be a silent zero."""
    reply = "```sql\n-- the answer\nSELECT `County Name` FROM `frpm`;\n```"
    tool, _llm = make_tool(responder=make_responder(sql=reply), config=offline_config())
    result = ask(tool, "Which county names exist?")
    assert result["status"] == "success"
    query = result["query"]
    assert query == 'SELECT "County Name" FROM "frpm"'
    assert "`" not in query and "```" not in query and "--" not in query


def test_text_utilities_leave_literals_alone() -> None:
    assert strip_code_fence("```sql\nSELECT 1\n```") == "SELECT 1"
    assert strip_code_fence("```OK```") == "OK"
    assert strip_code_fence("ok") == "ok"
    assert strip_comments("SELECT '--not a comment' AS a -- trailing") == "SELECT '--not a comment' AS a "
    assert normalise_identifiers("SELECT 'a`b' AS `b`") == 'SELECT \'a`b\' AS "b"'
    assert canonicalise_division("SELECT a / b FROM t") == "SELECT a / NULLIF(b, 0) FROM t"
    assert canonicalise_division("SELECT a / NULLIF(b, 0) FROM t") == "SELECT a / NULLIF(b, 0) FROM t"
    assert validate_sql("SELECT '```' AS fence").ok is True


def test_repair_happens_exactly_once() -> None:
    """One repair attempt, then an error - never invalid SQL under ``success``."""
    always_bad_tool, bad_llm = make_tool(
        responder=make_responder(sql=INVALID_SQL, syntax_repair=INVALID_SQL),
        config=offline_config(),
    )
    result = ask(always_bad_tool, "How many schools?")
    assert result["status"] == "error"
    assert result["query"] is None
    assert len(bad_llm.calls) == 2
    assert SYNTAX_REPAIR_MARKER in str(bad_llm.calls[1][1].content)

    fixed_tool, fixed_llm = make_tool(
        responder=make_responder(sql=INVALID_SQL, syntax_repair=VALID_SQL),
        config=offline_config(),
    )
    fixed = ask(fixed_tool, "How many schools?")
    assert fixed["status"] == "success"
    assert fixed["query"] == VALID_SQL
    assert len(fixed_llm.calls) == 2


def _call_kinds(llm: StubLLM) -> list[str]:
    """Classify each recorded call by the prompt it carried.

    Counting calls is not enough to prove a budget: the kind of each call shows
    whether a repair was spent or a generation attempt was retried.
    """
    kinds: list[str] = []
    for messages in llm.calls:
        prompt = "\n".join(str(getattr(message, "content", "")) for message in messages)
        if AMBIGUITY_MARKER in prompt:
            kinds.append("ambiguity")
        elif SYNTAX_REPAIR_MARKER in prompt:
            kinds.append("syntax-repair")
        elif EXECUTION_REPAIR_MARKER in prompt:
            kinds.append("execution-repair")
        else:
            kinds.append("generation")
    return kinds


def test_call_budget_of_one_question() -> None:
    """F3 regression: one ambiguity call, one generation, at most one repair.

    Before the fix an all-failing run made 9 calls where the configured budget was
    6, because a repair was issued even when the generation had returned no SQL at
    all: the repair consumed a fresh retry budget instead of ending the question.
    """
    retries = 2
    budget = retries + 1

    happy_tool, happy_llm = make_tool(
        responder=make_responder(sql=VALID_SQL),
        config=offline_config(ambiguous_mode="birdsafe", max_llm_retries=retries),
    )
    assert ask(happy_tool)["status"] == "success"
    assert _call_kinds(happy_llm) == ["ambiguity", "generation"]

    repair_tool, repair_llm = make_tool(
        responder=make_responder(sql=INVALID_SQL, syntax_repair=VALID_SQL),
        config=offline_config(ambiguous_mode="birdsafe", max_llm_retries=retries),
    )
    assert ask(repair_tool)["status"] == "success"
    assert _call_kinds(repair_llm) == ["ambiguity", "generation", "syntax-repair"]

    def always_failing(_prompt: str, _index: int) -> Any:
        raise RuntimeError("transport exploded")

    failed_tool, failed_llm = make_tool(
        responder=always_failing,
        config=offline_config(ambiguous_mode="birdsafe", max_llm_retries=retries),
    )
    assert ask(failed_tool)["status"] == "error"
    # Gate plus generation, budget attempts each, and no repair after a failed
    # generation: 2 * 3 = 6 calls (the pre-fix behaviour was 9).
    assert len(failed_llm.calls) == 2 * budget
    assert _call_kinds(failed_llm) == ["ambiguity"] * budget + ["generation"] * budget


def test_no_repair_after_a_generation_that_returned_nothing() -> None:
    """A repair prompt must never carry an empty SQL block (F3, second half)."""
    retries = 1

    empty_tool, empty_llm = make_tool(
        responder=make_responder(ambiguity="OK", sql="   "),
        config=offline_config(ambiguous_mode="birdsafe", max_llm_retries=retries),
    )
    assert ask(empty_tool)["status"] == "error"
    kinds = _call_kinds(empty_llm)
    assert "syntax-repair" not in kinds
    assert "execution-repair" not in kinds
    # The blank reply counts as a failed generation attempt and is retried instead.
    assert kinds == ["ambiguity"] + ["generation"] * (retries + 1)
    for messages in empty_llm.calls:
        assert "### Rejected SQL" not in str(messages[1].content)

    def always_failing(_prompt: str, _index: int) -> Any:
        raise RuntimeError("transport exploded")

    failed_tool, failed_llm = make_tool(
        responder=always_failing,
        config=offline_config(ambiguous_mode="birdsafe", max_llm_retries=0),
    )
    assert ask(failed_tool)["status"] == "error"
    assert _call_kinds(failed_llm) == ["ambiguity", "generation"]


def test_a_blank_reply_is_retried_instead_of_being_repaired() -> None:
    """A blank generation consumes a retry, so a transient blank is not fatal."""
    attempts = {"generation": 0}

    def blank_then_valid(prompt: str, _index: int) -> Any:
        if AMBIGUITY_MARKER in prompt:
            return "OK"
        attempts["generation"] += 1
        return "" if attempts["generation"] == 1 else VALID_SQL

    tool, llm = make_tool(
        responder=blank_then_valid,
        config=offline_config(ambiguous_mode="birdsafe", max_llm_retries=1),
    )
    result = ask(tool)
    assert result["status"] == "success"
    assert result["query"] == VALID_SQL
    assert _call_kinds(llm) == ["ambiguity", "generation", "generation"]


# ------------------------------------------------------------ execution feedback


def test_execution_feedback_repairs_rejected_sql() -> None:
    engine = _FakeEngine(error=ProgrammingError('column "foo" does not exist'))
    tool, llm = make_tool(
        responder=make_responder(sql="SELECT foo FROM frpm", execution_repair=VALID_SQL),
        config=offline_config(execution_feedback=True),
        engine=engine,
    )
    result = ask(tool, "How many schools?")
    assert result["status"] == "success"
    assert result["query"] == VALID_SQL
    assert len(llm.calls) == 2
    assert EXECUTION_REPAIR_MARKER in str(llm.calls[1][1].content)


def test_execution_feedback_degrades_when_database_is_unreachable() -> None:
    """An unreachable DB returns the valid, unexecuted SQL instead of failing."""
    engine = _FakeEngine(error=OperationalError("connection refused"))
    tool, llm = make_tool(
        responder=make_responder(sql=VALID_SQL),
        config=offline_config(execution_feedback=True),
        engine=engine,
    )
    result = ask(tool, "How many schools?")
    assert result["status"] == "success"
    assert result["query"] == VALID_SQL
    assert len(llm.calls) == 1
    assert engine.connections == 1


def test_execution_feedback_keeps_good_sql_untouched() -> None:
    engine = _FakeEngine()
    tool, llm = make_tool(
        responder=make_responder(sql=VALID_SQL), config=offline_config(execution_feedback=True), engine=engine
    )
    result = ask(tool, "How many schools?")
    assert result["status"] == "success"
    assert result["query"] == VALID_SQL
    assert len(llm.calls) == 1


def test_execution_feedback_disabled_makes_no_call() -> None:
    engine = _FakeEngine()
    tool, _llm = make_tool(
        responder=make_responder(sql=VALID_SQL), config=offline_config(execution_feedback=False), engine=engine
    )
    ask(tool)
    assert engine.connections == 0


# ---------------------------------------------------------------- schema pruning


def _mine_real_tables(db_id: str) -> dict[str, list[str]]:
    """Recover table and column names of one BIRD database from its gold SQL.

    The training JSON ships questions and gold SQL but no schema, so the schema
    fixture used by the pruning test is rebuilt from the gold queries: table names
    and the columns they reference are real BIRD identifiers, the 37 filler tables
    are synthetic padding to reach 40 tables.
    """
    dataset = json.loads(TRAIN_QUERIES_PATH.read_text(encoding="utf-8"))
    tables: dict[str, set[str]] = {}
    for item in dataset:
        if item["db_id"] != db_id:
            continue
        try:
            parsed = sqlglot.parse_one(item["SQL"], read="sqlite")
        except sqlglot.errors.SqlglotError:
            continue
        gold_tables = {table.name for table in parsed.find_all(exp.Table)}
        columns = {column.name for column in parsed.find_all(exp.Column)}
        for table in gold_tables:
            tables.setdefault(table, set()).update(columns)
    return {name: sorted(columns) for name, columns in tables.items()}


def _synthetic_schema(tables: dict[str, list[str]], total_tables: int = 40) -> str:
    lines: list[str] = []
    for name in sorted(tables):
        lines.append(f"TABLE {name}")
        for column in tables[name]:
            lines.append(f"  - {column} (TEXT)")
        lines.append("")
    filler = 0
    while len(tables) + filler < total_tables:
        lines.append(f"TABLE filler_{filler:02d}")
        lines.append("  - filler_key (TEXT)")
        lines.append("  - filler_value (TEXT)")
        lines.append("  - filler_date (TEXT)")
        lines.append("")
        filler += 1
    return "\n".join(lines).strip()


def _gold_tables(sql: str) -> set[str]:
    try:
        parsed = sqlglot.parse_one(sql, read="sqlite")
    except sqlglot.errors.SqlglotError:
        return set()
    return {table.name for table in parsed.find_all(exp.Table)}


def test_pruning_recovers_gold_tables_for_real_bird_questions() -> None:
    """Pruning must shrink a 40-table schema without losing the tables BIRD needs."""
    dataset = json.loads(TRAIN_QUERIES_PATH.read_text(encoding="utf-8"))
    db_id = "california_schools"
    real_tables = _mine_real_tables(db_id)
    assert real_tables, "fixture requires gold SQL for the chosen database"
    schema = _synthetic_schema(real_tables)
    assert len(parse_schema(schema)) == 40

    questions = [item for item in dataset if item["db_id"] == db_id]
    recoverable: list[tuple[int, set[str], tuple[str, ...]]] = []
    pruned_questions = 0
    for item in questions:
        gold = _gold_tables(item["SQL"])
        if not gold:
            continue
        pruned = prune_schema(schema, item["question"])
        if not pruned.applied:
            # No lexical overlap at all: the recall guard sends the full schema.
            continue
        pruned_questions += 1
        assert len(pruned.schema) < len(schema) // 2
        if gold.issubset(set(pruned.tables)):
            recoverable.append((item["question_id"], gold, pruned.tables))

    assert pruned_questions >= 0.8 * len(questions), f"pruning fired for only {pruned_questions} questions"
    assert len(recoverable) >= 5, f"only {len(recoverable)} of {len(questions)} questions kept their tables"

    # Determinism: the same question must prune to the same block every run.
    sample = questions[0]
    assert prune_schema(schema, sample["question"]) == prune_schema(schema, sample["question"])


def test_pruning_reaches_the_prompt_and_spares_the_rest() -> None:
    """The pruned block, not the 40-table dump, is what the model sees."""
    db_id = "california_schools"
    schema = _synthetic_schema(_mine_real_tables(db_id))
    dataset = json.loads(TRAIN_QUERIES_PATH.read_text(encoding="utf-8"))
    item = next(entry for entry in dataset if entry["db_id"] == db_id)
    gold = _gold_tables(item["SQL"])

    tool, llm = make_tool(responder=make_responder(sql=VALID_SQL), config=offline_config(), schema=schema)
    result = ask(tool, item["question"])

    assert result["status"] == "success"
    system_text = str(llm.calls[-1][0].content)
    assert "filler_37" not in system_text
    for table in gold:
        assert f"TABLE {table}" in system_text
    assert len(tool.last_selected_tables) < 40
    assert set(gold).issubset(set(tool.last_selected_tables))


def test_pruning_keeps_small_and_unmatched_schemas_whole() -> None:
    """Recall guard: tiny schemas and zero-overlap questions are never pruned."""
    small = "TABLE a\n  - x (TEXT)"
    assert prune_schema(small, "anything").applied is False

    big = _synthetic_schema({"alpha": ["x"]}, total_tables=40)
    unmatched = prune_schema(big, "zzz")
    assert unmatched.applied is False or "TABLE alpha" in unmatched.tables


# ---------------------------------------------------------------------- prompts


def test_prompts_are_english_only() -> None:
    """Russian prompts were the baseline's root cause: questions are English."""
    source = PROMPTS_PATH.read_text(encoding="utf-8")
    cyrillic = sorted({character for character in source if "\u0400" <= character <= "\u04ff"})
    assert cyrillic == []


def test_system_prompt_embeds_the_built_schema_verbatim() -> None:
    schema = "TABLE frpm\n  - County Name (TEXT)\n\nTABLE schools\n  - CDSCode (TEXT)"
    tool, llm = make_tool(schema=schema, config=offline_config())
    assert tool.db_schema == schema
    assert schema in tool.system_prompt
    assert SQL_DIALECT in tool.system_prompt

    ask(tool, "How many schools are in Alameda County?")
    assert schema in str(llm.calls[-1][0].content)


def test_prompt_templates_render_without_stray_braces() -> None:
    sql_prompt = SQL_PROMPT_TEMPLATE.format(user_query="How many schools?", sql_dialect=SQL_DIALECT)
    assert "How many schools?" in sql_prompt
    ambiguity_prompt = AMBIGUITY_PROMPT_TEMPLATE.format(db_schema="TABLE t", user_query="How many?")
    assert "OK" in ambiguity_prompt
    assert "AMBIGUOUS:" in ambiguity_prompt
    assert "{db_schema}" not in SYSTEM_PROMPT_TEMPLATE.format(db_schema="TABLE t", sql_dialect=SQL_DIALECT)


# --------------------------------------------------------------- LLM resilience


def test_transport_retries_then_success_and_usage_accounting() -> None:
    """Transient transport errors are retried; usage is accumulated per answered call."""

    def flaky(_prompt: str, call_index: int) -> Any:
        if call_index <= 2:
            raise RuntimeError("temporary network failure")
        return VALID_SQL

    tool, llm = make_tool(
        responder=flaky,
        config=offline_config(max_llm_retries=2, retry_backoff_s=0.0),
    )
    result = ask(tool)
    assert result["status"] == "success"
    assert len(llm.calls) == 3
    assert tool.llm_calls == 3
    assert tool.usage["prompt_tokens"] == 11
    assert tool.usage["completion_tokens"] == 7
    assert tool.usage["total_tokens"] == 18


# ------------------------------------------------------------- shipped-data guard


def test_gate_keeps_every_gold_sql_in_the_shipped_data() -> None:
    """The gate must not reject what the benchmark already considers correct.

    Measured on the shipped files: 241/241 bird_large, 357/357 train and 170/170
    ambrosia_large gold queries pass. A gate that rejects gold SQL would cap the
    achievable score below the benchmark ceiling before any model error.
    """
    seen = 0
    for name in ("bird_large", "train_queries", "ambrosia_large"):
        dataset = json.loads((REPO_ROOT / "data" / f"{name}.json").read_text(encoding="utf-8"))
        for item in dataset:
            if item["SQL"] == "ambiguous":
                continue
            result = validate_sql(item["SQL"])
            assert result.ok is True, (name, item["question_id"], result.error)
            assert result.sql is not None
            assert "`" not in result.sql
            seen += 1
    assert seen >= 700

