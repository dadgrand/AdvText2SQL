"""Offline tests for inference-time schema linking (``schema_linking.py`` + wiring).

No database, no model, no network: the schema comes from string fixtures, the LLM
from a recording stub, and the optional semantic boost from the dependency-free
hashing embedder (the sentence-transformer path is exercised only through its
recorded degradation, exactly as on this machine).

What is pinned:

* ``off`` (the default) leaves the schema block and the prompt byte-identical to
  the pre-change pruning path — the arm can be enabled and disabled without
  changing what a "default" run measures;
* ``tables`` and ``columns`` reduce the block, keep the best-scoring table, obey
  the character budget and never return an empty schema (fallback to pruning);
* the selector reads only schema + question + evidence: no gold SQL, no reference
  answer, no dev-question file — asserted on the signature and behaviourally;
* the per-question record (mode, counts, chars, block SHA-256) reaches the tool
  metadata, the procedure log and ``run.json``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

try:  # the tool package imports fastmcp; the benchmark tool does not need it
    import fastmcp  # noqa: F401
except ImportError:
    _stub = types.ModuleType("fastmcp")
    _stub.FastMCP = type("FastMCP", (), {})  # type: ignore[attr-defined]
    sys.modules["fastmcp"] = _stub

from adv_text2sql.mcp_servers.text2sql_tool.src import schema_linking  # noqa: E402
from adv_text2sql.mcp_servers.text2sql_tool.src.schema_pruning import prune_schema  # noqa: E402
from adv_text2sql.mcp_servers.text2sql_tool.src.text2sql_implementation import (  # noqa: E402
    PROCEDURE_ENV_VARS,
    Text2SQLConfig,
    Text2SQLGenerator,
    resolve_procedure_config,
)

# ---------------------------------------------------------------------------
# Fixtures: a small "schools" schema and a larger one that pruning does not touch
# ---------------------------------------------------------------------------

SCHEMA = (
    "TABLE schools\n"
    "  - CDSCode (TEXT)\n"
    "  - School Name (TEXT)\n"
    "  - Phone (TEXT)\n"
    "  - County (TEXT)\n"
    "  - District (TEXT)\n"
    "  - City (TEXT)\n"
    "  - Zip (TEXT)\n"
    "  - Street (TEXT)\n"
    "  - Latitude (TEXT)\n"
    "  - Longitude (TEXT)\n"
    "\n"
    "TABLE satscores\n"
    "  - cds (TEXT)\n"
    "  - AvgScrMath (INTEGER)\n"
    "  - AvgScrRead (INTEGER)\n"
    "  - AvgScrWrite (INTEGER)\n"
    "  - NumTstTaken (INTEGER)\n"
    "\n"
    "TABLE frpm\n"
    "  - CDSCode (TEXT)\n"
    "  - County Name (TEXT)\n"
    "  - Enrollment (K-12) (TEXT)\n"
    "  - Free Meal Count (K-12) (TEXT)\n"
    "  - Academic Year (TEXT)\n"
)

QUESTION = "What is the average SAT math score of schools in Alameda County?"


def _large_schema(table_count: int = 26) -> str:
    """A schema above the pruning threshold, so the baseline path also acts on it."""

    blocks: list[str] = []
    for index in range(table_count):
        blocks.append(
            f"TABLE table_{index:02d}\n"
            f"  - id (INTEGER)\n"
            f"  - account_id (INTEGER)\n"
            f"  - name (TEXT)\n"
            f"  - description (TEXT)\n"
            f"  - created_at (TIMESTAMP)\n"
            f"  - amount (INTEGER)\n"
            f"  - status (TEXT)\n"
            f"  - updated_at (TIMESTAMP)\n"
        )
    # One table the question is actually about, with a join key to table_00.
    blocks.append(
        "TABLE payments\n"
        "  - id (INTEGER)\n"
        "  - account_id (INTEGER)\n"
        "  - amount (INTEGER)\n"
        "  - payment_date (TEXT)\n"
        "  - status (TEXT)\n"
        "  - method (TEXT)\n"
        "  - reference (TEXT)\n"
        "  - note (TEXT)\n"
    )
    return "\n".join(blocks)


JOIN_SCHEMA = (
    "TABLE accounts\n"
    "  - account_id (INTEGER)\n"
    "  - holder (TEXT)\n"
    "  - branch (TEXT)\n"
    "  - opened (TEXT)\n"
    "\n"
    "TABLE payments\n"
    "  - payment_id (INTEGER)\n"
    "  - account_id (INTEGER)\n"
    "  - payment amount (INTEGER)\n"
    "  - payment date (TEXT)\n"
    "\n"
    "TABLE branches\n"
    "  - branch_id (INTEGER)\n"
    "  - branch name (TEXT)\n"
    "  - city (TEXT)\n"
)

COLUMNS_SCHEMA = (
    "TABLE events\n"
    "  - event_id (INTEGER)\n"
    "  - organiser_id (INTEGER)\n"
    "  - alpha (TEXT)\n"
    "  - beta (TEXT)\n"
    "  - gamma (TEXT)\n"
    "  - delta (TEXT)\n"
    "  - epsilon (TEXT)\n"
    "  - zeta (TEXT)\n"
    "  - eta (TEXT)\n"
    "  - theta (TEXT)\n"
)


# ---------------------------------------------------------------------------
# Stub LLM (records the exact prompt; the tests never call a real model)
# ---------------------------------------------------------------------------


class RecordingClient:
    def __init__(self, reply: str = "SELECT 1") -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    async def create(self, messages: Any, **kwargs: Any) -> Any:
        self.calls.append({"messages": list(messages), "kwargs": dict(kwargs)})
        usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2)
        return SimpleNamespace(content=self.reply, usage=usage)

    def last_prompt(self) -> str:
        return "\n".join(str(getattr(message, "content", message)) for message in self.calls[-1]["messages"])


def make_tool(
    *, config: Text2SQLConfig | None = None, schema: str = SCHEMA, client: Any = None
) -> tuple[Text2SQLGenerator, RecordingClient]:
    client = client or RecordingClient()
    tool = Text2SQLGenerator(
        db_uri="postgresql+psycopg://user:pass@localhost:5444/fixture_db",
        llm_client=client,
        schema=schema,
        engine=None,
        config=config or offline_config(),
    )
    tool.build()
    return tool, client


def offline_config(**overrides: Any) -> Text2SQLConfig:
    defaults: dict[str, Any] = {
        "ambiguous_mode": "off",
        "max_llm_retries": 0,
        "retry_backoff_s": 0.0,
        "execution_feedback": False,
    }
    defaults.update(overrides)
    return Text2SQLConfig(**defaults)


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in PROCEDURE_ENV_VARS.values():
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# The default: byte-identical to today
# ---------------------------------------------------------------------------


def test_off_is_byte_identical_to_the_pruning_path() -> None:
    tool, _client = make_tool()
    for schema in (SCHEMA, _large_schema()):
        tool = Text2SQLGenerator(
            db_uri="postgresql+psycopg://user:pass@localhost:5444/fixture_db",
            llm_client=RecordingClient(),
            schema=schema,
            engine=None,
            config=offline_config(),
        )
        tool.build()
        expected = prune_schema(
            tool.db_schema,
            QUESTION,
            max_tables=tool.config.prune_max_tables,
            min_schema_tables=tool.config.prune_min_schema_tables,
        )
        assert tool._schema_for(QUESTION) == expected.schema
        assert tool.last_selected_tables == expected.tables
        assert tool.last_schema_link is None


def test_off_never_touches_the_linking_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default path must not even import-time exercise the selector."""

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("schema_linking.link_schema must not run when the mode is off")

    import adv_text2sql.mcp_servers.text2sql_tool.src.text2sql_implementation as implementation

    monkeypatch.setattr(implementation, "link_schema", forbidden)
    tool, client = make_tool()
    assert asyncio.run(tool.query(QUESTION))["status"] == "success"
    assert "Worked examples" not in client.last_prompt()
    assert tool.procedure_metadata()["schema_link"]["enabled"] is False


def test_off_prompt_is_byte_identical_to_the_pre_change_prompt() -> None:
    """Prompt-level proof, not just function-level: both messages are identical.

    The reference is built from the same templates the tool uses, with the schema
    block today's ``prune_schema`` produces, so any drift in the off path (extra
    whitespace, a stray block, a different table set) fails this test.
    """

    from adv_text2sql.mcp_servers.text2sql_tool.src.prompts import (
        SQL_DIALECT,
        SQL_PROMPT_TEMPLATE,
        SYSTEM_PROMPT_TEMPLATE,
    )

    for schema in (SCHEMA, _large_schema()):
        tool, client = make_tool(schema=schema)
        assert asyncio.run(tool.query(QUESTION))["status"] == "success"
        expected_block = prune_schema(
            tool.db_schema,
            QUESTION,
            max_tables=tool.config.prune_max_tables,
            min_schema_tables=tool.config.prune_min_schema_tables,
        ).schema
        expected_system = SYSTEM_PROMPT_TEMPLATE.format(
            db_schema=expected_block, sql_dialect=SQL_DIALECT
        )
        expected_user = SQL_PROMPT_TEMPLATE.format(user_query=QUESTION, sql_dialect=SQL_DIALECT)
        messages = client.calls[-1]["messages"]
        assert str(messages[0].content) == expected_system
        assert str(messages[1].content).strip() == expected_user.strip()


# ---------------------------------------------------------------------------
# The selector itself
# ---------------------------------------------------------------------------


def test_tables_mode_keeps_the_question_tables_and_drops_the_rest() -> None:
    question = "What is the payment amount and status for account 42?"
    linked = schema_linking.link_schema(_large_schema(), question, mode="tables", k=4,
                                        min_score=0.5, embedder="none")
    assert linked.applied is True
    assert "payments" in linked.tables
    assert "table_25" not in linked.tables, "a low-scoring table must be dropped"
    assert linked.schema_chars < len(_large_schema())
    assert linked.schema_sha256 and len(linked.schema_sha256) == 64
    assert linked.metadata()["tables_selected"] == len(linked.tables)
    assert linked.metadata()["tables_total"] == 27


def test_columns_mode_drops_unmatched_columns_but_keeps_join_keys() -> None:
    linked = schema_linking.link_schema(
        COLUMNS_SCHEMA, "What is alpha for the event?", mode="columns", embedder="none"
    )
    assert linked.applied is True
    assert linked.columns_selected == 3
    assert linked.total_columns == 10
    rendered = linked.schema
    # matched column survives, both key columns survive, the rest is dropped
    for kept in ("alpha", "event_id", "organiser_id"):
        assert kept in rendered, kept
    for dropped in ("beta", "gamma", "theta"):
        assert f"- {dropped} (" not in rendered, dropped
    assert rendered.startswith("TABLE events")


def test_hard_budget_drops_weak_tables_but_always_keeps_the_best_one() -> None:
    linked = schema_linking.link_schema(
        _large_schema(), "What is the payment amount and status for account 42?",
        mode="tables", k=20, min_score=0.5, max_chars=400, embedder="none",
    )
    assert linked.applied is True
    assert linked.tables, "the budget must never empty the selection"
    assert len(linked.schema) <= 400
    assert linked.tables[0] == "payments", "the best-scoring table survives every cut"
    assert "table_" in linked.reason, "the dropped tables are named in the record"


def test_a_single_table_over_the_budget_is_kept_whole_and_flagged() -> None:
    """A block that cannot fit is still a schema: never drop it, report over_budget."""

    big_column = "x" * 240
    schema = f"TABLE huge\n  - {big_column} (TEXT)\n"
    linked = schema_linking.link_schema(
        schema, f"what is {big_column}?", mode="tables", max_chars=200, embedder="none"
    )
    assert linked.applied is True
    assert linked.tables == ("huge",)
    assert linked.over_budget is True
    assert linked.schema_chars > 200


def test_join_closure_pulls_the_fk_table_on_spare_slots() -> None:
    """The question names ``payments``; ``accounts`` joins it via ``account_id``."""

    linked = schema_linking.link_schema(
        JOIN_SCHEMA, "How much did the payments total?", mode="tables", k=3,
        min_score=1.0, embedder="none",
    )
    assert linked.tables == ("payments", "accounts")
    assert linked.join_closure == ("accounts",)
    assert "branches" not in linked.tables, "an unlinked table must not be pulled in"
    assert linked.metadata()["join_closure"] == ["accounts"]
    assert any(item["table"] == "payments" for item in linked.metadata()["scores"])


def test_never_returns_an_empty_schema_and_reports_fallbacks() -> None:
    for schema, question in (
        ("", QUESTION),                                   # no schema at all
        ("CREATE TABLE t (a INT);", QUESTION),             # not the light format
        (SCHEMA, "zzz qqq"),                               # no token matches
        (SCHEMA, "the of and to"),                         # only stopwords
    ):
        linked = schema_linking.link_schema(schema, question, mode="tables", embedder="none")
        assert linked.schema == ""
        assert linked.applied is False
        assert linked.reason


def test_link_schema_rejects_invalid_configuration() -> None:
    with pytest.raises(ValueError):
        schema_linking.link_schema(SCHEMA, QUESTION, mode="off")
    with pytest.raises(ValueError):
        schema_linking.link_schema(SCHEMA, QUESTION, k=0)
    with pytest.raises(ValueError):
        schema_linking.link_schema(SCHEMA, QUESTION, min_score=-1.0)
    with pytest.raises(ValueError):
        schema_linking.link_schema(SCHEMA, QUESTION, max_chars=10)
    with pytest.raises(ValueError):
        schema_linking.link_schema(SCHEMA, QUESTION, embedder="word2vec")


def test_hash_embedder_boost_is_deterministic_and_recorded() -> None:
    first = schema_linking.link_schema(SCHEMA, QUESTION, mode="tables", embedder="auto")
    second = schema_linking.link_schema(SCHEMA, QUESTION, mode="tables", embedder="auto")
    assert first.metadata() == second.metadata()
    # This machine has no sentence-transformer weights: auto must degrade to hash
    # and say why, instead of silently claiming a semantic boost.
    assert first.embedder == "hash"
    assert first.embedder_reason, "the fallback reason must be recorded"


def test_embedder_none_disables_the_boost() -> None:
    linked = schema_linking.link_schema(SCHEMA, QUESTION, mode="tables", embedder="none")
    assert linked.embedder == "none"
    assert linked.embedder_reason


# ---------------------------------------------------------------------------
# Honesty: no gold SQL, no reference answer, no dev questions
# ---------------------------------------------------------------------------


def test_selector_signature_has_no_gold_or_answer_input() -> None:
    parameters = set(inspect.signature(schema_linking.link_schema).parameters)
    assert parameters == {
        "schema", "question", "evidence", "mode", "k", "min_score", "max_chars",
        "embedder", "small_table_columns",
    }
    for forbidden in ("sql", "gold", "answer", "reference", "dev", "question_id"):
        assert not any(forbidden in name for name in parameters), forbidden


def test_selector_source_never_reads_dev_files_or_train_queries() -> None:
    source = inspect.getsource(schema_linking)
    for forbidden in ("bird_large", "bird_small", "train_queries", "question_id", "answer_file"):
        assert forbidden not in source, forbidden


def test_selection_follows_the_question_not_a_hypothetical_gold(monkeypatch: pytest.MonkeyPatch) -> None:
    """Behavioural proof: the gold tables of a question cannot influence the choice.

    The questions below mention ``neighbourhood``/``programs``, which no table in the
    fixture matches; a selector that could see gold SQL (which joins
    ``satscores``/``frpm``) would pick those. It picks nothing instead.
    """

    linked = schema_linking.link_schema(
        SCHEMA, "Which neighbourhood programs are listed?", mode="tables", min_score=1.0, embedder="none"
    )
    assert linked.applied is False
    assert linked.schema == ""

    matching = schema_linking.link_schema(SCHEMA, QUESTION, mode="tables", embedder="none")
    assert "satscores" in matching.tables


# ---------------------------------------------------------------------------
# Tool wiring: modes, metadata, procedure log, run.json aggregation
# ---------------------------------------------------------------------------


def test_tables_mode_reaches_the_prompt_and_the_metadata() -> None:
    schema = _large_schema()
    tool, client = make_tool(
        schema=schema, config=offline_config(schema_link="tables", schema_link_embedder="hash")
    )
    question = "What is the payment amount and status for account 42?"
    assert asyncio.run(tool.query(question))["status"] == "success"

    block = tool.procedure_metadata()["schema_link"]
    assert block["enabled"] is True
    assert block["mode"] == "tables"
    record = block["last"]
    assert record["applied"] is True
    assert record["schema_sha256"]
    assert record["schema_chars"] < len(schema)
    assert record["tables_selected"] >= 1

    prompt = client.last_prompt()
    linked = schema_linking.link_schema(
        schema, question, mode="tables", k=int(tool.config.schema_link_k),
        min_score=float(tool.config.schema_link_min_score),
        max_chars=int(tool.config.schema_link_max_chars), embedder="hash",
    )
    assert linked.schema in prompt, "the prompt carries exactly the linked block"
    assert record["schema_sha256"] == linked.schema_sha256
    assert "table_25" not in prompt, "a dropped table must not leak into the prompt"


def test_columns_mode_and_evidence_participation() -> None:
    evidence_question = (
        "question: What is the average SAT math score of schools in Alameda County?, "
        "evidence (may be empty): average of AvgScrMath"
    )
    tool, _client = make_tool(
        config=offline_config(schema_link="columns", schema_link_embedder="hash")
    )
    assert asyncio.run(tool.query(evidence_question))["status"] == "success"
    block = tool.procedure_metadata()["schema_link"]["last"]
    assert block["applied"] is True
    assert block["used_evidence"] is True
    assert block["columns_selected"] <= block["columns_total"]


def test_linker_fallback_keeps_todays_block_and_says_so() -> None:
    tool, _client = make_tool(
        schema=SCHEMA,
        config=offline_config(schema_link="tables", schema_link_embedder="hash"),
    )
    baseline = prune_schema(tool.db_schema, "zzz qqq").schema
    # route the linker to a question it cannot match
    assert tool._schema_for("zzz qqq") == baseline
    record = tool.procedure_metadata()["schema_link"]["last"]
    assert record["applied"] is False
    assert record["fallback"] == "prune_schema"


def test_bad_mode_and_embedder_are_loud_at_construction() -> None:
    with pytest.raises(ValueError, match="unknown schema_link mode"):
        make_tool(config=offline_config(schema_link="table"))
    with pytest.raises(ValueError, match="unknown schema_link_embedder"):
        make_tool(config=offline_config(schema_link="tables", schema_link_embedder="word2vec"))


def test_environment_switch_enables_the_arm() -> None:
    env = {
        "TEXT2SQL_SCHEMA_LINK": "columns",
        "TEXT2SQL_SCHEMA_LINK_K": "4",
        "TEXT2SQL_SCHEMA_LINK_MIN_SCORE": "1.5",
        "TEXT2SQL_SCHEMA_LINK_MAX_CHARS": "3000",
        "TEXT2SQL_SCHEMA_LINK_EMBEDDER": "hash",
    }
    resolved = resolve_procedure_config(Text2SQLConfig(), env)
    assert resolved.schema_link == "columns"
    assert resolved.schema_link_k == 4
    assert resolved.schema_link_min_score == pytest.approx(1.5)
    assert resolved.schema_link_max_chars == 3000
    assert resolved.schema_link_embedder == "hash"

    with pytest.raises(ValueError, match="not an integer"):
        resolve_procedure_config(Text2SQLConfig(), {"TEXT2SQL_SCHEMA_LINK_K": "four"})


def test_constructor_only_harness_path_honours_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``benchmarks/base.py`` passes no config: the arm must switch on from the env."""

    monkeypatch.setenv("TEXT2SQL_SCHEMA_LINK", "tables")
    monkeypatch.setenv("TEXT2SQL_SCHEMA_LINK_EMBEDDER", "hash")
    monkeypatch.setenv("TEXT2SQL_SCHEMA_LINK_K", "2")
    client = RecordingClient()
    tool = Text2SQLGenerator(
        db_uri="postgresql+psycopg://user:pass@localhost:5444/fixture_db",
        llm_client=client,
        schema=SCHEMA,
        engine=None,
        config=Text2SQLConfig(
            ambiguous_mode="off", max_llm_retries=0, retry_backoff_s=0.0, execution_feedback=False
        ),
    )
    tool.build()
    assert tool.schema_link_mode == "tables"
    assert tool.config.schema_link_k == 2
    assert asyncio.run(tool.query(QUESTION))["status"] == "success"
    assert tool.procedure_metadata()["schema_link"]["enabled"] is True
    assert tool.procedure_metadata()["schema_link"]["last"]["applied"] is True


def test_procedure_log_records_the_schema_link_block(tmp_path: Path) -> None:
    log = tmp_path / "procedures.jsonl"
    tool, _client = make_tool(
        config=offline_config(
            schema_link="tables", schema_link_embedder="hash", procedure_log=str(log)
        )
    )
    assert asyncio.run(tool.query(QUESTION))["status"] == "success"
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    link = records[0]["schema_link"]
    assert link["mode"] == "tables"
    assert link["schema_sha256"]
    assert link["schema_chars"] > 0
    assert records[0]["config"]["schema_link"] == "tables"


def test_run_json_aggregates_the_schema_link_records(tmp_path: Path) -> None:
    from dev_tools import run_benchmark

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    records = [
        {"schema_link": {"mode": "columns", "applied": True, "tables_selected": 2,
                         "columns_selected": 10, "schema_chars": 500,
                         "schema_sha256": "a" * 64, "embedder": "hash"}},
        {"schema_link": {"mode": "columns", "applied": True, "tables_selected": 4,
                         "columns_selected": 20, "schema_chars": 900,
                         "schema_sha256": "b" * 64, "embedder": "hash"}},
        {"schema_link": {"mode": "columns", "applied": False, "fallback": "prune_schema",
                         "tables_selected": 0, "columns_selected": 0, "schema_chars": 0}},
    ]
    (run_dir / "procedures.jsonl").write_text(
        "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
    )
    summary = run_benchmark.schema_link_summary(run_dir)
    assert summary is not None
    assert summary["modes"] == ["columns"]
    assert summary["questions"] == 3
    assert summary["applied"] == 2
    assert summary["fallbacks"] == 1
    assert summary["tables_selected_mean"] == 2.0
    assert summary["tables_selected_max"] == 4
    assert summary["schema_chars_max"] == 900
    assert summary["distinct_schema_sha256"] == 2


def test_run_json_stays_quiet_without_the_arm(tmp_path: Path) -> None:
    from dev_tools import run_benchmark

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert run_benchmark.schema_link_summary(run_dir) is None
    (run_dir / "procedures.jsonl").write_text(
        json.dumps({"retrieval": {"injected": 0}, "schema_link": None}) + "\n", encoding="utf-8"
    )
    assert run_benchmark.schema_link_summary(run_dir) is None

# ---------------------------------------------------------------------------
# Non-destructive modes: highlight and joins (R3-6)
# ---------------------------------------------------------------------------


def test_highlight_keeps_every_original_line_and_annotates() -> None:
    linked = schema_linking.link_schema(SCHEMA, QUESTION, mode="highlight", embedder="none")
    assert linked.applied is True
    assert linked.non_destructive is True
    assert linked.columns_selected == linked.total_columns, "nothing may be removed"
    block_lines = set(linked.schema.splitlines())
    for line in SCHEMA.splitlines():
        if line.strip():
            assert line in block_lines, line
    assert linked.schema.startswith("Relevant tables: ")
    assert linked.annotations == len(linked.tables) >= 1
    assert linked.schema.count("-- highlight: ") == linked.annotations
    metadata = linked.metadata()
    assert metadata["non_destructive"] is True
    assert metadata["annotations"] == linked.annotations
    assert metadata["join_edges"] == []


def test_highlight_orders_the_relevant_table_first() -> None:
    linked = schema_linking.link_schema(
        JOIN_SCHEMA, "How much did the payments total?", mode="highlight", embedder="none"
    )
    block = linked.schema
    assert block.index("TABLE payments") < block.index("TABLE branches")
    assert "TABLE payments" in block and "TABLE branches" in block


def test_joins_edges_come_only_from_the_schema_and_are_capped() -> None:
    linked = schema_linking.link_schema(
        JOIN_SCHEMA, "How much did the payments total?", mode="joins", k=3, embedder="none"
    )
    assert linked.join_edges, "the fixture shares account_id"
    # Direction follows the relevance order, so both spellings are the same edge.
    assert set(linked.join_edges) & {
        "accounts.account_id = payments.account_id",
        "payments.account_id = accounts.account_id",
    }
    assert len(linked.join_edges) <= schema_linking.MAX_JOIN_EDGES
    for edge in linked.join_edges:
        for side in edge.split(" = "):
            table, column = side.split(".", 1)
            assert f"TABLE {table}" in JOIN_SCHEMA, edge
            assert column in JOIN_SCHEMA, edge
    assert linked.join_block_chars > 0

    many = "\n".join(
        f"TABLE t{i}\n  - shared_id (INTEGER)\n  - value (TEXT)" for i in range(20)
    )
    capped = schema_linking.link_schema(many, "what is the value?", mode="joins", embedder="none")
    assert len(capped.join_edges) <= schema_linking.MAX_JOIN_EDGES


def test_joins_block_cost_is_recorded_and_matches_the_rendered_text() -> None:
    exact = schema_linking.link_schema(
        JOIN_SCHEMA, "How much did the payments total?", mode="joins", k=3, embedder="none"
    )
    highlight = schema_linking.link_schema(
        JOIN_SCHEMA, "How much did the payments total?", mode="highlight", k=3, embedder="none"
    )
    assert exact.join_block_chars == len(exact.schema) - len(highlight.schema) - len("\n\n")
    assert exact.metadata()["join_block_chars"] == exact.join_block_chars


def test_joins_without_shared_key_names_is_just_highlight() -> None:
    disjoint = "TABLE a\n  - alpha (TEXT)\n\nTABLE b\n  - beta (TEXT)"
    linked = schema_linking.link_schema(disjoint, "what is alpha?", mode="joins", embedder="none")
    assert linked.join_edges == ()
    assert linked.join_block_chars == 0
    assert linked.applied is True, "the block is still the full annotated schema"


def test_non_destructive_modes_are_valid_configurations() -> None:
    resolved = resolve_procedure_config(
        Text2SQLConfig(), {"TEXT2SQL_SCHEMA_LINK": "highlight", "TEXT2SQL_SCHEMA_LINK_K": "3"}
    )
    assert resolved.schema_link == "highlight"
    resolved = resolve_procedure_config(Text2SQLConfig(), {"TEXT2SQL_SCHEMA_LINK": "joins"})
    assert resolved.schema_link == "joins"
    tool, _client = make_tool(config=offline_config(schema_link="highlight", schema_link_embedder="hash"))
    assert tool.schema_link_mode == "highlight"
    with pytest.raises(ValueError, match="unknown schema_link mode"):
        make_tool(config=offline_config(schema_link="highlight-tables"))


def test_highlight_reaches_the_prompt_and_metadata() -> None:
    tool, client = make_tool(
        schema=JOIN_SCHEMA,
        config=offline_config(schema_link="highlight", schema_link_embedder="hash"),
    )
    result = asyncio.run(tool.query("How much did the payments total?"))
    assert result["status"] == "success"
    record = tool.procedure_metadata()["schema_link"]["last"]
    assert record["mode"] == "highlight"
    assert record["non_destructive"] is True
    assert record["schema_sha256"]
    assert record["schema_chars"] > len(JOIN_SCHEMA) - 1, "the full schema is still sent"
    prompt = client.last_prompt()
    assert "Relevant tables: " in prompt
    assert "-- highlight: " in prompt
    assert "TABLE branches" in prompt


def test_joins_reaches_the_prompt_with_its_edges() -> None:
    tool, client = make_tool(
        schema=JOIN_SCHEMA,
        config=offline_config(schema_link="joins", schema_link_embedder="hash"),
    )
    assert asyncio.run(tool.query("How much did the payments total?"))["status"] == "success"
    record = tool.procedure_metadata()["schema_link"]["last"]
    assert record["mode"] == "joins"
    assert record["join_edges"], "the fixture shares account_id"
    prompt = client.last_prompt()
    assert "Join paths (key columns; derived from the schema only):" in prompt
    for edge in record["join_edges"]:
        assert edge in prompt
