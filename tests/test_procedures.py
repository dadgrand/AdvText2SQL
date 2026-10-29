"""Offline tests for the two procedure arms (retrieval few-shot, self-consistency).

No database, no network, no API key, no GPU: the bank is read from the repo's
``data/`` files (or a temporary fixture), embeddings come from the dependency-free
hashing embedder, and every SQL reply comes from a recording stub client. What is
pinned here is the *contract* of the arms - the leakage guard, the degradation
paths, the vote rules and the determinism - not a model's output.

The leakage tests are the important ones: a few-shot bank built from a dev file
would turn a BIRD score into a memorisation score, so both layers of the guard
(source allowlist and content filter) are asserted against real dev rows.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"


def _bootstrap_import_path() -> None:
    """Same offline bootstrap as ``tests/test_tool.py``: stub ``fastmcp`` only."""

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

from adv_text2sql.mcp_servers.text2sql_tool.src import (  # noqa: E402
    retrieval,
    self_consistency,
    text2sql_implementation,
)
from adv_text2sql.mcp_servers.text2sql_tool.src.retrieval import (  # noqa: E402
    BankRow,
    HashEmbedder,
    build_bank,
    build_retriever,
    filter_bank_rows,
    is_allowed_bank_source,
    load_bank_rows,
    load_dev_guard,
    normalise_question,
    render_examples_block,
    split_question_evidence,
    transpile_gold_to_postgres,
)
from adv_text2sql.mcp_servers.text2sql_tool.src.self_consistency import (  # noqa: E402
    Candidate,
    HARD_CAP_N,
    choose,
    clamp_candidate_count,
    normalise_sql_text,
    result_signature,
    sampling_args,
)
from adv_text2sql.mcp_servers.text2sql_tool.src.text2sql_implementation import (  # noqa: E402
    PROCEDURE_ENV_VARS,
    Text2SQLConfig,
    Text2SQLGenerator,
    resolve_procedure_config,
)

SCHEMA = "TABLE frpm\n  - County Name (TEXT)\n\nTABLE satscores\n  - AvgScrMath (INTEGER)"
QUESTION = "How many schools are in Alameda County in total?"


# --------------------------------------------------------------------------- stubs


class RecordingClient:
    """Deterministic client that records the exact messages and request kwargs."""

    def __init__(self, responder: Callable[[str, int], Any]) -> None:
        self._responder = responder
        self.calls: list[dict[str, Any]] = []

    @staticmethod
    def prompt_text(messages: Iterable[Any]) -> str:
        return "\n".join(str(getattr(message, "content", message)) for message in messages)

    async def create(self, messages: Iterable[Any], **kwargs: Any) -> Any:
        self.calls.append({"messages": list(messages), "kwargs": dict(kwargs)})
        prompt = self.prompt_text(messages)
        reply = self._responder(prompt, len(self.calls) - 1)
        if isinstance(reply, BaseException):
            raise reply
        if asyncio.iscoroutine(reply):
            reply = await reply
        usage = SimpleNamespace(prompt_tokens=5, completion_tokens=5, total_tokens=10)
        return SimpleNamespace(content=str(reply), usage=usage)

    def last_prompt(self) -> str:
        return self.prompt_text(self.calls[-1]["messages"])


class _Result:
    def __init__(self, rows: Iterable[Any]) -> None:
        self._rows = list(rows)

    def fetchmany(self, size: int = 1) -> list[Any]:
        return self._rows[:size]

    def fetchall(self) -> list[Any]:
        return list(self._rows)


class _Connection:
    def __init__(self, engine: "_Engine") -> None:
        self._engine = engine

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, statement: Any) -> _Result:
        text = str(getattr(statement, "text", statement))
        if text.upper().startswith("SET "):
            return _Result([])
        self._engine.executions.append(text)
        value = self._engine.rows_for(text)
        if isinstance(value, BaseException):
            raise value
        return _Result(value)


class _Engine:
    """Fake engine: maps SQL substrings to rows (or an exception), counts connects."""

    def __init__(
        self,
        rows_by_fragment: dict[str, Any] | None = None,
        connect_error: BaseException | None = None,
    ) -> None:
        self.rows_by_fragment = dict(rows_by_fragment or {})
        self.connect_error = connect_error
        self.connections = 0
        self.executions: list[str] = []

    def connect(self) -> _Connection:
        self.connections += 1
        if self.connect_error is not None:
            raise self.connect_error
        return _Connection(self)

    def rows_for(self, sql: str) -> Any:
        for fragment, value in self.rows_by_fragment.items():
            if fragment in sql:
                return value
        return [(0,)]


class OperationalError(Exception):
    """Name-matches sqlalchemy's connection-level error without importing it."""


class ProgrammingError(Exception):
    """Name-matches sqlalchemy's SQL-level error without importing it."""


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ambient ``TEXT2SQL_*`` value and no real engine in this file.

    The procedure arms are env-driven by design, so a stray variable in the
    developer's shell would otherwise change what these tests assert. The engine
    guard makes the "no database" claim of the file enforceable: a test that needs
    a database injects a fake engine object instead.
    """

    for name in PROCEDURE_ENV_VARS.values():
        monkeypatch.delenv(name, raising=False)
    attempts: list[tuple[Any, ...]] = []

    def forbidden_create_engine(*args: Any, **kwargs: Any) -> Any:
        attempts.append(args)
        raise AssertionError("procedure tests must not create database engines")

    monkeypatch.setattr(text2sql_implementation, "create_engine", forbidden_create_engine)
    yield
    assert not attempts, f"the tool tried to create a database engine: {attempts}"


@pytest.fixture()
def synth_bank(tmp_path: Path) -> Path:
    """A miniature, self-contained train file (the real one is 357 rows)."""

    rows = [
        {
            "question_id": 1001,
            "db_id": "fixture_db",
            "question": "How many schools are in Alameda County?",
            "evidence": "",
            "SQL": "SELECT COUNT(*) FROM frpm WHERE `County Name` = 'Alameda'",
            "difficulty": "simple",
        },
        {
            "question_id": 1002,
            "db_id": "fixture_db",
            "question": "What is the average math score of the schools in Riverside?",
            "evidence": "Average math score is `AvgScrMath`",
            "SQL": "SELECT AVG(`AvgScrMath`) FROM satscores",
            "difficulty": "moderate",
        },
        {
            "question_id": 1003,
            "db_id": "fixture_db",
            "question": "List the phone numbers of the schools in Alameda County.",
            "evidence": "",
            "SQL": "SELECT `Phone` FROM schools WHERE `County` = 'Alameda'",
            "difficulty": "simple",
        },
        {
            "question_id": 1004,
            "db_id": "other_db",
            "question": "How many toxicology tests were performed on male subjects?",
            "evidence": "",
            "SQL": "SELECT COUNT(*) FROM test WHERE `Sex` = 'M'",
            "difficulty": "simple",
        },
    ]
    path = tmp_path / "train_queries.json"
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return path


def _dev_items() -> list[dict[str, Any]]:
    return json.loads((DATA_DIR / "bird_large.json").read_text(encoding="utf-8"))


def make_tool(
    *,
    responder: Callable[[str, int], Any] | None = None,
    config: Text2SQLConfig | None = None,
    engine: Any = None,
    build: bool = True,
) -> tuple[Text2SQLGenerator, RecordingClient]:
    client = RecordingClient(responder or (lambda _prompt, _index: "SELECT COUNT(*) FROM frpm"))
    tool = Text2SQLGenerator(
        db_uri="postgresql+psycopg://user:secret-password@localhost:5444/fixture_db",
        llm_client=client,
        schema=SCHEMA,
        engine=engine,
        config=config or offline_config(),
    )
    if build:
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


def ask(tool: Text2SQLGenerator, question: str = QUESTION) -> dict[str, Any]:
    result = asyncio.run(tool.query(question))
    assert isinstance(result, dict)
    assert result["status"] in {"success", "ambiguous", "error"}
    return result


def retrieval_config(bank: Path, **overrides: Any) -> Text2SQLConfig:
    overrides.setdefault("retrieval_few_shot", True)
    overrides.setdefault("retrieval_bank", str(bank))
    overrides.setdefault("retrieval_embedder", "hash")
    overrides.setdefault("retrieval_k", 2)
    return offline_config(**overrides)


# ------------------------------------------------------------------- bank sources


def test_source_allowlist_accepts_only_train_files(synth_bank: Path, tmp_path: Path) -> None:
    """Layer one: dev files are refused by name, train files are accepted."""

    assert is_allowed_bank_source(DATA_DIR / "train_queries.json")[0]
    assert is_allowed_bank_source(synth_bank)[0]

    for name in ("bird_small.json", "bird_large.json", "mini_dev_sqlite.json"):
        allowed, reason = is_allowed_bank_source(DATA_DIR / name)
        assert not allowed, name
        assert "dev" in reason.lower() or "train" in reason.lower()

    bird_train = tmp_path / "bird_train" / "bird23_train_filtered.jsonl"
    bird_train.parent.mkdir(parents=True)
    bird_train.write_text("{}\n", encoding="utf-8")
    assert is_allowed_bank_source(bird_train)[0]

    other = tmp_path / "gold_predictions.json"
    other.write_text("[]", encoding="utf-8")
    assert not is_allowed_bank_source(other)[0]


def test_loading_a_dev_source_raises_instead_of_leaking() -> None:
    with pytest.raises(ValueError, match="train-only|allowed sources"):
        load_bank_rows(DATA_DIR / "bird_large.json")


def test_content_guard_drops_a_dev_row_by_id_and_by_text() -> None:
    """Layer two: even a permitted filename cannot carry a dev question through."""

    dev_item = _dev_items()[0]
    guard = load_dev_guard()
    by_id = BankRow(
        source_question_id=int(dev_item["question_id"]),
        db_id=str(dev_item["db_id"]),
        question=str(dev_item["question"]),
        evidence="",
        sql=str(dev_item["SQL"]),
    )
    # Same question text, an id the dev set does not use: the text check must fire.
    by_text = BankRow(
        source_question_id=987654,
        db_id=str(dev_item["db_id"]),
        question=f"  {dev_item['question']}  ",
        evidence="",
        sql=str(dev_item["SQL"]),
    )
    clean = BankRow(
        source_question_id=555,
        db_id="fixture_db",
        question="Which fixture question is not a dev question?",
        evidence="",
        sql="SELECT 1",
    )
    kept, drops = filter_bank_rows([by_id, by_text, clean], guard)
    assert drops["dev_question_id"] == 1
    assert drops["dev_question_text"] == 1
    assert [row.source_question_id for row in kept] == [555]


def test_a_dev_file_renamed_to_train_queries_yields_no_bank(tmp_path: Path) -> None:
    """Both layers together: a copy of a dev file is refused by content, not name."""

    disguised = tmp_path / "train_queries.json"
    disguised.write_text((DATA_DIR / "bird_large.json").read_text(encoding="utf-8"), encoding="utf-8")
    assert is_allowed_bank_source(disguised)[0]  # the name passes layer one

    kept, metadata = load_bank_rows(disguised)
    assert kept == []
    assert metadata["drops"]["dev_question_id"] == 241
    with pytest.raises(retrieval.EmbedderUnavailable):
        build_bank(source=disguised, embedder="hash")

    retriever = build_retriever(source=disguised, embedder="hash")
    assert not retriever.available
    assert "no usable training rows" in retriever.reason


def test_shipped_train_bank_is_leak_free_and_deterministic() -> None:
    """The real bank: no dev id, no exact dev question, only PostgreSQL gold SQL."""

    guard = load_dev_guard()
    rows, metadata = load_bank_rows()
    assert metadata["source_kind"] == "train_queries"
    assert len(rows) > 300, metadata
    assert metadata["drops"]["dev_question_id"] == 0
    assert metadata["drops"]["dev_question_text"] == 0
    assert metadata["dev_questions_guarded"] >= 241

    dev_ids = {int(item["question_id"]) for item in _dev_items()}
    dev_questions = {normalise_question(item["question"]) for item in _dev_items()}
    for row in rows:
        assert row.source_question_id not in dev_ids
        assert normalise_question(row.question) not in dev_questions
        assert guard.leak_reason(row) == ""
        assert "`" not in row.sql, "a backtick would teach the model the wrong dialect"

    again, _ = load_bank_rows()
    assert [row.sql for row in rows] == [row.sql for row in again]


def test_transpile_rejects_what_postgresql_cannot_parse() -> None:
    converted = transpile_gold_to_postgres("SELECT `County Name` FROM frpm")
    assert converted is not None and "`" not in converted
    assert transpile_gold_to_postgres("SELEC nonsense FROM") is None
    assert transpile_gold_to_postgres("") is None


# ------------------------------------------------------------------- retrieval


def test_hash_embedder_is_deterministic_and_order_preserving() -> None:
    first = HashEmbedder()
    second = HashEmbedder()
    a = first.embed(["schools in Alameda County"])[0]
    b = second.embed(["schools in Alameda County"])[0]
    assert a == b
    assert len(a) == first.dimensions
    assert first.embed(["aaa bbb"])[0] != first.embed(["ccc ddd"])[0]
    assert first.embed(["one", "two"]) == [first.embed(["one"])[0], first.embed(["two"])[0]]


def test_retrieval_returns_the_closest_training_question_first(synth_bank: Path) -> None:
    bank = build_bank(source=synth_bank, embedder="hash")
    examples = bank.retrieve(QUESTION, db_id="fixture_db", k=2, min_similarity=0.0)
    assert examples, "a lexical overlap must retrieve something at threshold 0"
    assert examples[0].source_question_id == 1001
    assert examples[0].sql.startswith("SELECT COUNT(*) FROM frpm")
    assert all(example.db_id == "fixture_db" for example in examples)
    assert len(examples) <= 2

    # Same query, same bank: identical examples in identical order.
    assert [example.metadata() for example in examples] == [
        example.metadata() for example in bank.retrieve(QUESTION, db_id="fixture_db", k=2, min_similarity=0.0)
    ]


def test_retrieval_threshold_and_database_scope_are_enforced(synth_bank: Path) -> None:
    bank = build_bank(source=synth_bank, embedder="hash")
    assert bank.retrieve(QUESTION, db_id="fixture_db", k=2, min_similarity=1.01) == ()
    assert bank.retrieve(QUESTION, db_id="unknown_db", k=2, min_similarity=0.0) == ()


def test_examples_block_is_rendered_deterministically(synth_bank: Path) -> None:
    bank = build_bank(source=synth_bank, embedder="hash")
    examples = bank.retrieve(QUESTION, db_id="fixture_db", k=2, min_similarity=0.0)
    block = render_examples_block(examples, include_evidence=True)
    assert block == render_examples_block(examples, include_evidence=True)
    assert "Example 1 (similarity" in block
    assert 'SQL: SELECT COUNT(*) FROM frpm WHERE "County Name" = \'Alameda\'' in block
    without_evidence = render_examples_block(examples, include_evidence=False)
    assert "Evidence:" not in without_evidence


def test_question_evidence_split_understands_only_the_harness_format() -> None:
    question, evidence = split_question_evidence(
        "question: How many schools?, evidence (may be empty): count of rows in frpm"
    )
    assert question == "How many schools?"
    assert evidence == "count of rows in frpm"
    assert split_question_evidence("How many schools?") == ("How many schools?", "")


def test_tool_injects_examples_and_records_them(synth_bank: Path, tmp_path: Path) -> None:
    """The arm is visible: in the prompt, in ``procedure_metadata`` and in the log."""

    log = tmp_path / "procedures.jsonl"
    tool, client = make_tool(
        config=retrieval_config(synth_bank, procedure_log=str(log), retrieval_include_evidence=True)
    )
    result = ask(tool)
    assert result["status"] == "success"

    prompt = client.last_prompt()
    assert "Worked examples" in prompt
    assert 'SELECT COUNT(*) FROM frpm WHERE "County Name" = \'Alameda\'' in prompt
    assert "The examples are different questions" in prompt

    metadata = tool.procedure_metadata()
    retrieval_block = metadata["retrieval_few_shot"]
    assert retrieval_block["enabled"] and retrieval_block["available"]
    assert retrieval_block["embedder"] == "hash"
    assert retrieval_block["source"].endswith("train_queries.json")
    assert retrieval_block["k"] == 2
    assert retrieval_block["last"]["injected"] >= 1
    assert retrieval_block["last"]["block_sha256"]
    assert retrieval_block["last"]["examples"][0]["source_question_id"] == 1001
    assert metadata["self_consistency"]["enabled"] is False

    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    record = records[0]
    assert record["db_id"] == "fixture_db"
    assert record["retrieval"]["block"] == retrieval_block["last"]["block"]
    assert record["retrieval"]["injected"] == retrieval_block["last"]["injected"]
    assert "secret-password" not in log.read_text(encoding="utf-8")
    assert "postgresql+psycopg" not in log.read_text(encoding="utf-8")


def test_retrieval_without_local_weights_degrades_cleanly(synth_bank: Path) -> None:
    """No sentence-transformer weights on this machine: the arm is a recorded no-op."""

    retriever = build_retriever(
        source=synth_bank,
        embedder="sentence-transformer",
        model="sentence-transformers/this-model-is-not-on-this-machine",
    )
    assert not retriever.available
    assert retriever.reason

    tool, client = make_tool(
        config=retrieval_config(
            synth_bank,
            retrieval_embedder="sentence-transformer",
            retrieval_model="sentence-transformers/this-model-is-not-on-this-machine",
        )
    )
    result = ask(tool)
    assert result["status"] == "success", "a missing embedder must never cost an item"
    assert "Worked examples" not in client.last_prompt()
    block = tool.procedure_metadata()["retrieval_few_shot"]
    assert block["enabled"] and block["available"] is False
    assert "this-model-is-not-on-this-machine" in block["reason"]


def test_retrieval_is_off_by_default_and_unknown_embedder_is_loud(synth_bank: Path) -> None:
    tool, client = make_tool()
    assert ask(tool)["status"] == "success"
    assert "Worked examples" not in client.last_prompt()
    assert tool.procedure_metadata()["retrieval_few_shot"]["enabled"] is False

    with pytest.raises(ValueError, match="unknown retrieval_embedder"):
        make_tool(config=retrieval_config(synth_bank, retrieval_embedder="word2vec"))


# --------------------------------------------------------------- self-consistency


def _candidate(index: int, sql: str, execution: str = "skipped", rows: Any = None) -> Candidate:
    return Candidate(index=index, sql=sql, valid=True, execution=execution, rows=rows)


def test_vote_prefers_the_largest_result_set_group() -> None:
    candidates = [
        _candidate(0, "SELECT 1", "ok", ((7,),)),
        _candidate(1, "SELECT 2", "ok", ((9,),)),
        _candidate(2, "SELECT 3", "ok", ((7,),)),
        _candidate(3, "SELEC broken", "skipped"),
    ]
    candidates[3] = Candidate(index=3, sql=None, valid=False, error="parse error")
    selection = choose(candidates)
    assert selection.mode == "result-set-majority"
    assert selection.sql == "SELECT 1"
    assert selection.vote_count == 2
    assert selection.selected_index == 0


def test_result_signature_ignores_row_order_but_not_values() -> None:
    assert result_signature([(1, "a"), (2, "b")]) == result_signature([(2, "b"), (1, "a")])
    assert result_signature([(1,)]) != result_signature([(1.0,)])
    assert result_signature([(None,)]) != result_signature([("None",)])


def test_vote_uses_sql_text_when_the_database_is_unreachable() -> None:
    candidates = [
        _candidate(0, "SELECT 1", "unreachable"),
        _candidate(1, "SELECT 2", "unreachable"),
        _candidate(2, "select   1", "unreachable"),
    ]
    selection = choose(candidates)
    assert selection.mode == "sql-text-majority"
    assert selection.sql == "SELECT 1"
    assert selection.vote_count == 2
    assert "database unavailable" in selection.detail


def test_vote_returns_nothing_without_a_valid_candidate() -> None:
    selection = choose([Candidate(index=0, sql=None, valid=False, error="parse error")])
    assert selection.mode == "none"
    assert selection.sql is None


def test_normalise_sql_text_is_formatting_insensitive() -> None:
    assert normalise_sql_text("SELECT  1 FROM t;") == normalise_sql_text("select 1 from t")
    assert normalise_sql_text("SELECT 1") != normalise_sql_text("SELECT 2")
    assert normalise_sql_text(None) == ""


def test_sampling_args_are_seeded_and_refuse_greedy_decoding() -> None:
    args = sampling_args(2, temperature=0.8, seed=7)
    assert args["temperature"] == 0.8
    assert args["seed"] == 9
    with pytest.raises(ValueError):
        sampling_args(0, temperature=0.0)
    assert clamp_candidate_count(99) == HARD_CAP_N
    assert clamp_candidate_count(0) == 1
    assert clamp_candidate_count(None) == self_consistency.DEFAULT_N


def _cycling(sqls: list[str]) -> Callable[[str, int], Any]:
    return lambda _prompt, index: sqls[index % len(sqls)]


def test_self_consistency_samples_n_candidates_and_votes_by_result_set() -> None:
    engine = _Engine(
        rows_by_fragment={
            "FROM a": [(1,)],
            "FROM b": [(2,)],
        }
    )
    tool, client = make_tool(
        responder=_cycling(
            [
                "SELECT x FROM a",
                "SELECT x FROM b",
                "SELECT x FROM a",
            ]
        ),
        engine=engine,
        config=offline_config(self_consistency=True, self_consistency_n=3),
    )
    result = ask(tool)
    assert result["status"] == "success"
    assert result["query"] == "SELECT x FROM a"
    assert len(client.calls) == 3
    assert engine.connections == 3

    state = tool.procedure_metadata()["self_consistency"]["last"]
    assert state["n"] == 3
    assert state["drawn"] == 3
    assert state["database_executed"] is True
    assert state["selection"]["mode"] == "result-set-majority"
    assert state["selection"]["selected_index"] == 0
    for call in client.calls:
        args = call["kwargs"]["extra_create_args"]
        assert args["temperature"] == pytest.approx(0.7)
    assert [call["kwargs"]["extra_create_args"]["seed"] for call in client.calls] == [0, 1, 2]


def test_self_consistency_falls_back_to_sql_text_when_the_database_is_down() -> None:
    engine = _Engine(connect_error=OperationalError("connection refused"))
    tool, client = make_tool(
        responder=_cycling(
            [
                "SELECT x FROM a",
                "SELECT x FROM b",
                "SELECT x FROM a",
            ]
        ),
        engine=engine,
        config=offline_config(self_consistency=True, self_consistency_n=3),
    )
    result = ask(tool)
    assert result["status"] == "success"
    assert result["query"] == "SELECT x FROM a"
    assert engine.connections == 1, "once the database is down the round stops connecting"
    state = tool.procedure_metadata()["self_consistency"]["last"]
    assert state["selection"]["mode"] == "sql-text-majority"
    assert state["database_executed"] is False
    assert len(client.calls) == 3


def test_self_consistency_never_raises_and_reports_no_sql() -> None:
    tool, _client = make_tool(
        responder=_cycling(["SELEC nonsense", "not sql at all"]),
        config=offline_config(self_consistency=True, self_consistency_n=2),
    )
    result = ask(tool)
    assert result == {"status": "error", "query": None}
    state = tool.procedure_metadata()["self_consistency"]["last"]
    assert state["selection"]["mode"] == "none"
    assert state["drawn"] == 2


def test_self_consistency_round_is_reproducible() -> None:
    responses = ["SELECT x FROM a", "SELECT x FROM b", "SELECT x FROM a", "SELECT x FROM b"]

    def run() -> tuple[Any, list[dict[str, Any]]]:
        tool, client = make_tool(
            responder=_cycling(responses),
            engine=_Engine(connect_error=OperationalError("down")),
            config=offline_config(self_consistency=True, self_consistency_n=4),
        )
        result = ask(tool)
        state = tool.procedure_metadata()["self_consistency"]["last"]
        return result["query"], [call["kwargs"]["extra_create_args"] for call in client.calls]

    first_query, first_args = run()
    second_query, second_args = run()
    assert first_query == second_query
    assert first_args == second_args == [
        {"temperature": 0.7, "seed": 0},
        {"temperature": 0.7, "seed": 1},
        {"temperature": 0.7, "seed": 2},
        {"temperature": 0.7, "seed": 3},
    ]


def test_self_consistency_caps_n_and_the_time_budget() -> None:
    tool, client = make_tool(
        responder=_cycling(["SELECT x FROM a"]),
        engine=_Engine(connect_error=OperationalError("down")),
        config=offline_config(self_consistency=True, self_consistency_n=99),
    )
    assert ask(tool)["status"] == "success"
    assert len(client.calls) == HARD_CAP_N
    assert tool.procedure_metadata()["self_consistency"]["n"] == HARD_CAP_N

    async def slow(prompt: str, index: int) -> str:
        await asyncio.sleep(0.05)
        return "SELECT x FROM a"

    tool, client = make_tool(
        responder=slow,
        engine=_Engine(connect_error=OperationalError("down")),
        config=offline_config(
            self_consistency=True, self_consistency_n=5, self_consistency_budget_s=0.01
        ),
    )
    assert ask(tool)["status"] == "success"
    assert len(client.calls) == 1, "the second draw must not start after the budget is spent"
    assert tool.procedure_metadata()["self_consistency"]["last"]["drawn"] == 1


def test_self_consistency_is_off_by_default_and_leaves_greedy_calls_untouched() -> None:
    tool, client = make_tool(responder=lambda _prompt, _index: "SELECT COUNT(*) FROM frpm")
    assert ask(tool)["status"] == "success"
    assert len(client.calls) == 1
    assert all(call["kwargs"] == {} for call in client.calls)
    assert tool.procedure_metadata()["self_consistency"]["enabled"] is False
    assert tool.procedure_metadata()["self_consistency"]["last"] is None


def test_self_consistency_rejects_a_greedy_configuration() -> None:
    with pytest.raises(ValueError, match="temperature > 0"):
        make_tool(
            config=offline_config(self_consistency=True, self_consistency_temperature=0.0)
        )


# ---------------------------------------------------------------- configuration


def test_environment_switches_the_arms_on() -> None:
    env = {
        "TEXT2SQL_RETRIEVAL_FEWSHOT": "1",
        "TEXT2SQL_RETRIEVAL_K": "4",
        "TEXT2SQL_RETRIEVAL_EMBEDDER": "hash",
        "TEXT2SQL_SELF_CONSISTENCY": "true",
        "TEXT2SQL_SC_N": "3",
        "TEXT2SQL_SC_TEMPERATURE": "1.1",
        "TEXT2SQL_PROCEDURE_LOG": "procedures.jsonl",
    }
    resolved = resolve_procedure_config(Text2SQLConfig(), env)
    assert resolved.retrieval_few_shot is True
    assert resolved.retrieval_k == 4
    assert resolved.retrieval_embedder == "hash"
    assert resolved.self_consistency is True
    assert resolved.self_consistency_n == 3
    assert resolved.self_consistency_temperature == pytest.approx(1.1)
    assert resolved.procedure_log == "procedures.jsonl"


def test_constructor_only_harness_path_honours_the_environment(
    monkeypatch: pytest.MonkeyPatch, synth_bank: Path, tmp_path: Path
) -> None:
    """The harness builds ``tool_cls(db_uri=..., llm_client=...)`` and nothing else.

    This is the path an eval arm actually takes: no constructor argument can be
    added without editing ``benchmarks/base.py``, so the arms must be reachable
    through the process environment alone.
    """

    monkeypatch.setenv("TEXT2SQL_RETRIEVAL_FEWSHOT", "1")
    monkeypatch.setenv("TEXT2SQL_RETRIEVAL_EMBEDDER", "hash")
    monkeypatch.setenv("TEXT2SQL_RETRIEVAL_BANK", str(synth_bank))
    monkeypatch.setenv("TEXT2SQL_RETRIEVAL_K", "1")
    monkeypatch.setenv("TEXT2SQL_PROCEDURE_LOG", str(tmp_path / "procedures.jsonl"))

    client = RecordingClient(lambda _prompt, _index: "SELECT COUNT(*) FROM frpm")
    tool = Text2SQLGenerator(
        db_uri="postgresql+psycopg://user:secret-password@localhost:5444/fixture_db",
        llm_client=client,
        schema=SCHEMA,
        config=Text2SQLConfig(
            ambiguous_mode="off", max_llm_retries=0, retry_backoff_s=0.0, execution_feedback=False
        ),
    )
    tool.build()
    assert asyncio.run(tool.query(QUESTION))["status"] == "success"
    assert "Worked examples" in client.last_prompt()
    assert tool.config.retrieval_k == 1
    assert (tmp_path / "procedures.jsonl").exists()


def test_environment_typos_raise_instead_of_silently_disabling_an_arm() -> None:
    with pytest.raises(ValueError, match="not a boolean"):
        resolve_procedure_config(Text2SQLConfig(), {"TEXT2SQL_RETRIEVAL_FEWSHOT": "maybe"})
    with pytest.raises(ValueError, match="not an integer"):
        resolve_procedure_config(Text2SQLConfig(), {"TEXT2SQL_SC_N": "five"})
    with pytest.raises(ValueError, match="not a number"):
        resolve_procedure_config(Text2SQLConfig(), {"TEXT2SQL_SC_TEMPERATURE": "hot"})


def test_empty_environment_keeps_the_defaults() -> None:
    config = Text2SQLConfig()
    assert resolve_procedure_config(config, {}) is config
    assert resolve_procedure_config(config, {"TEXT2SQL_RETRIEVAL_FEWSHOT": "  "}) is config


def test_procedure_log_is_not_written_when_both_arms_are_off(tmp_path: Path) -> None:
    log = tmp_path / "procedures.jsonl"
    tool, _client = make_tool(config=offline_config(procedure_log=str(log)))
    assert ask(tool)["status"] == "success"
    assert not log.exists()


def test_run_provenance_records_the_procedure_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run.json`` names the arms: the environment is the only switchboard on the sealed path."""

    from dev_tools import run_benchmark

    monkeypatch.setenv("TEXT2SQL_RETRIEVAL_FEWSHOT", "1")
    monkeypatch.setenv("TEXT2SQL_SC_N", "4")
    block = run_benchmark.run_provenance({"use_evidence": False}, object)
    assert block["procedure_env"] == {
        "TEXT2SQL_RETRIEVAL_FEWSHOT": "1",
        "TEXT2SQL_SC_N": "4",
    }
