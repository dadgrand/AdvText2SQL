"""Offline tests for the log-independent schema-link recorder (R4-1).

The defect these pin: ``run.json.schema_link`` was built from the optional procedure
log, so a round-3 arm that did not set ``TEXT2SQL_PROCEDURE_LOG`` produced
``schema_link: null`` while it was running, and the prompt cost had to be replayed
offline. The tool now keeps its own per-run counters and the runner merges them from
the tool instances — the log is an optional detailed trace, not the source of truth.

No database, no model, no network: the harness is a stub, the LLM a canned client, and
the schema comes from a string fixture. What is pinned is the provenance contract:
aggregate present with the log off, aggregate equal to what the prompt contained, and
default (``off``) runs byte-identical to before.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

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

from adv_text2sql.mcp_servers.text2sql_tool.src.text2sql_implementation import (  # noqa: E402
    PROCEDURE_ENV_VARS,
    Text2SQLConfig,
    Text2SQLGenerator,
)
from dev_tools import run_benchmark  # noqa: E402
from dev_tools.run_benchmark import (  # noqa: E402
    recording_tool,
    schema_link_summary_from_tools,
)

SCHEMA = (
    "TABLE schools\n"
    "  - CDSCode (TEXT)\n"
    "  - County (TEXT)\n"
    "  - School Name (TEXT)\n"
    "\n"
    "TABLE satscores\n"
    "  - cds (TEXT)\n"
    "  - AvgScrMath (INTEGER)\n"
)

QUESTIONS: List[Dict[str, Any]] = [
    {
        "question_id": 1,
        "db_id": "demo",
        "question": "Which county has the most schools?",
        "evidence": "",
        "SQL": "SELECT County FROM schools",
    },
    {
        "question_id": 2,
        "db_id": "demo",
        "question": "What is the average SAT math score?",
        "evidence": "AvgScrMath is the math score",
        "SQL": "SELECT AVG(AvgScrMath) FROM satscores",
    },
]


class _CannedClient:
    """Deterministic LLM stub (no network, no model); records every message list."""

    def __init__(self) -> None:
        self.calls = 0
        self.history: List[List[Any]] = []

    async def create(self, messages: Any, **kwargs: Any) -> Any:
        self.calls += 1
        self.history.append(list(messages))
        usage = SimpleNamespace(prompt_tokens=5, completion_tokens=5, total_tokens=10)
        return SimpleNamespace(content="SELECT 1", usage=usage)

    def get_usage(self) -> Dict[str, int]:
        return {"calls": self.calls, "prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}


def _offline_config(**overrides: Any) -> Text2SQLConfig:
    defaults: Dict[str, Any] = {
        "ambiguous_mode": "off",
        "max_llm_retries": 0,
        "retry_backoff_s": 0.0,
        "execution_feedback": False,
    }
    defaults.update(overrides)
    return Text2SQLConfig(**defaults)


def make_tool(*, schema_link: str | None = None, log: str | None = None) -> tuple[Text2SQLGenerator, _CannedClient]:
    client = _CannedClient()
    tool = Text2SQLGenerator(
        db_uri="postgresql+psycopg://user:pass@localhost:1/demo",
        llm_client=client,
        schema=SCHEMA,
        engine=None,
        config=_offline_config(
            schema_link=schema_link or "off",
            schema_link_embedder="hash",
            procedure_log=log,
        ),
    )
    tool.build()
    return tool, client


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in PROCEDURE_ENV_VARS.values():
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# The tool's own aggregate
# ---------------------------------------------------------------------------


def test_aggregate_is_present_with_the_procedure_log_off(tmp_path: Path) -> None:
    log = tmp_path / "never-written.jsonl"
    tool, _client = make_tool(schema_link="highlight", log=None)
    for item in QUESTIONS:
        assert asyncio.run(tool.query(item["question"]))["status"] == "success"

    aggregate = tool.schema_link_aggregate()
    assert aggregate["source"] == "in-process"
    assert aggregate["mode"] == "highlight"
    assert aggregate["modes"] == ["highlight"]
    assert aggregate["questions"] == 2
    assert aggregate["questions_with_link"] == 2
    assert aggregate["applied"] == 2
    assert aggregate["fallbacks"] == 0
    assert aggregate["schema_chars_mean"] and aggregate["schema_chars_mean"] > 0
    assert aggregate["annotations_total"] == 2  # one marker per question
    assert aggregate["distinct_schema_sha256"] == 2
    assert aggregate["embedders"] == ["hash"]
    assert not log.exists()


def test_aggregate_matches_what_the_prompt_actually_contained() -> None:
    """The recorded block, the recorded hash and the prompt must be the same text."""

    import hashlib
    import re

    tool, client = make_tool(schema_link="joins")
    assert asyncio.run(tool.query(QUESTIONS[0]["question"]))["status"] == "success"

    prompt = "\n".join(str(getattr(message, "content", "")) for message in client.history[-1])
    match = re.search(r"### Database schema\n(.*?)\n\n### Output contract", prompt, re.S)
    assert match is not None, "the prompt carries the schema section"
    in_prompt = match.group(1)

    record = tool.procedure_metadata()["schema_link"]["last"]
    assert record["block"] == in_prompt
    assert record["schema_sha256"] == hashlib.sha256(in_prompt.encode("utf-8")).hexdigest()

    aggregate = tool.schema_link_aggregate()
    assert aggregate["schema_chars_mean"] == len(in_prompt)
    assert aggregate["distinct_schema_sha256"] == 1
    assert aggregate["join_edges_total"] == len(record.get("join_edges") or [])
    assert aggregate["join_block_chars_total"] == int(record.get("join_block_chars") or 0)


def client_calls(client: _CannedClient) -> List[Any]:
    """Every message list the stub saw (kept for readability in assertions)."""

    return client.history


def test_off_aggregate_reports_mode_off_and_zero_effect() -> None:
    tool, _client = make_tool()
    for item in QUESTIONS:
        assert asyncio.run(tool.query(item["question"]))["status"] == "success"
    aggregate = tool.schema_link_aggregate()
    assert aggregate["mode"] == "off"
    assert aggregate["modes"] == ["off"]
    assert aggregate["questions"] == 0
    assert aggregate["applied"] == 0
    assert aggregate["schema_chars_mean"] is None
    assert aggregate["distinct_schema_sha256"] == 0
    assert aggregate["embedders"] == []
    assert tool.procedure_metadata()["schema_link"]["last"] is None


def test_merging_tools_sums_and_weights_by_questions() -> None:
    tool_a, _ = make_tool(schema_link="highlight")
    tool_b, _ = make_tool(schema_link="highlight")
    assert asyncio.run(tool_a.query(QUESTIONS[0]["question"]))["status"] == "success"
    assert asyncio.run(tool_b.query(QUESTIONS[0]["question"]))["status"] == "success"
    assert asyncio.run(tool_b.query(QUESTIONS[1]["question"]))["status"] == "success"

    merged = schema_link_summary_from_tools([tool_a, tool_b])
    assert merged is not None
    assert merged["source"] == "in-process"
    assert merged["questions"] == 3
    assert merged["applied"] == 3
    assert merged["procedure_log"] is None
    # Cross-tool distinct counts are summed (each tool covers one database); the two
    # blocks of tool_b and the one of tool_a give 3, even though tool_a's block text
    # equals tool_b's first block.
    assert merged["distinct_schema_sha256"] == 3

    aggregate_a, aggregate_b = tool_a.schema_link_aggregate(), tool_b.schema_link_aggregate()
    expected = round(
        (float(aggregate_a["schema_chars_mean"]) * 1 + float(aggregate_b["schema_chars_mean"]) * 2)
        / 3,
        1,
    )
    assert merged["schema_chars_mean"] == expected
    assert merged["schema_chars_max"] == max(
        aggregate_a["schema_chars_max"], aggregate_b["schema_chars_max"]
    )


def test_merging_is_none_when_the_arm_is_off() -> None:
    tool, _ = make_tool()
    assert schema_link_summary_from_tools([tool]) is None
    assert schema_link_summary_from_tools([]) is None


def test_recording_tool_registers_instances_and_keeps_behaviour() -> None:
    registry: List[Any] = []
    recorded = recording_tool(Text2SQLGenerator, registry)
    tool = recorded(
        db_uri="postgresql+psycopg://user:pass@localhost:1/demo",
        llm_client=_CannedClient(),
        schema=SCHEMA,
        engine=None,
        config=_offline_config(schema_link="tables", schema_link_embedder="hash"),
    )
    tool.build()
    assert registry == [tool]
    assert isinstance(tool, Text2SQLGenerator)
    assert asyncio.run(tool.query(QUESTIONS[1]["question"]))["status"] == "success"
    assert tool.schema_link_aggregate()["questions"] == 1


# ---------------------------------------------------------------------------
# End to end through run_predict_only: run.json without the procedure log
# ---------------------------------------------------------------------------


class _RecorderHarness:
    """Stub of ``BenchmarkBIRD`` that builds real tools and asks them real questions."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.llm_client = _CannedClient()
        self.tools: Dict[str, Any] = {}

    async def build(self, tool_class: Any) -> Dict[str, Any]:
        tool = tool_class(
            db_uri="postgresql+psycopg://user:pass@localhost:1/demo",
            llm_client=self.llm_client,
            schema=SCHEMA,
            engine=None,
            config=_offline_config(schema_link_embedder="hash"),
        )
        tool.build()
        Path("db_schemas.json").write_text(json.dumps({"demo": SCHEMA}), encoding="utf-8")
        self.tools["demo"] = tool
        return self.tools

    async def predict(self, tools: Dict[str, Any]) -> Dict[str, str]:
        predictions: Dict[str, str] = {}
        tool = tools["demo"]
        for item in QUESTIONS:
            result = await tool.query(item["question"])
            predictions[str(item["question_id"])] = str(result.get("query") or "error")
        return predictions

    def _save_predictions(self, predictions: Dict[str, str], path: str) -> None:
        Path(path).write_text(json.dumps(predictions), encoding="utf-8")


@pytest.fixture()
def prepared_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A plan plus the stub harness, with network and DB probes neutralised."""

    import benchmarks.bird as bird_module

    monkeypatch.setenv("DB_USER", "readonly_user")
    monkeypatch.setenv("DB_PASS", "secret")
    query = tmp_path / "queries.json"
    query.write_text(json.dumps(QUESTIONS), encoding="utf-8")
    plan = run_benchmark.plan_run(
        run_id="recorder-run",
        query_file=query,
        answer_file=query,
        db_url="127.0.0.1:5444",
        tool_class=run_benchmark.DEFAULT_TOOL_CLASS,
        llm_base_url="http://127.0.0.1:9/v1",
        llm_model="stub-model",
        runs_dir=tmp_path / "runs",
    )
    monkeypatch.setattr(run_benchmark, "probe_server", lambda *_a, **_k: None)
    monkeypatch.setattr(
        run_benchmark,
        "probe_database_schema",
        lambda *_a, **_k: {"db_id": "demo", "probed": True, "skipped": False, "tables": 2, "error": ""},
    )
    monkeypatch.setattr(bird_module, "BenchmarkBIRD", _RecorderHarness)
    run_dir = tmp_path / "runs" / "recorder-run"
    return plan, query, run_dir


def _normalise(summary: Dict[str, Any], *, masks: List[str] | None = None) -> str:
    """Serialise a summary with run-specific fields/paths masked, for byte comparisons."""

    masked = dict(summary)
    for key in ("started_at", "finished_at", "run_dir", "run_id", "artifacts"):
        if key in masked:
            masked[key] = "<normalised>"
    text = json.dumps(masked, ensure_ascii=False, sort_keys=True, default=str)
    for mask in sorted(masks or [], key=len, reverse=True):
        text = text.replace(mask, "<normalised>")
    return text


def test_run_json_gets_schema_link_without_the_log(prepared_run, monkeypatch) -> None:
    plan, query, run_dir = prepared_run
    monkeypatch.setenv("TEXT2SQL_SCHEMA_LINK", "highlight")
    monkeypatch.setenv("TEXT2SQL_SCHEMA_LINK_EMBEDDER", "hash")

    summary = run_benchmark.run_predict_only(
        plan=plan, query_file=query, answer_file=query, tool_class=Text2SQLGenerator, client=_CannedClient()
    )
    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    block = record["schema_link"]
    assert block is not None, "run.json must carry the aggregate even with no procedure log"
    assert block["source"] == "in-process"
    assert block["modes"] == ["highlight"]
    assert block["questions"] == 2
    assert block["applied"] == 2
    assert block["schema_chars_mean"] and block["schema_chars_mean"] > 0
    assert block["annotations_total"] == 2
    assert block["procedure_log"] is None
    assert not (run_dir / "procedures.jsonl").exists(), "the log was deliberately off"
    assert record["procedure_env"].get("TEXT2SQL_SCHEMA_LINK") == "highlight"


def test_in_process_aggregate_wins_over_the_log(prepared_run, monkeypatch, tmp_path) -> None:
    plan, query, run_dir = prepared_run
    monkeypatch.setenv("TEXT2SQL_SCHEMA_LINK", "joins")
    monkeypatch.setenv("TEXT2SQL_SCHEMA_LINK_EMBEDDER", "hash")
    monkeypatch.setenv("TEXT2SQL_PROCEDURE_LOG", "procedures.jsonl")

    run_benchmark.run_predict_only(
        plan=plan, query_file=query, answer_file=query, tool_class=Text2SQLGenerator, client=_CannedClient()
    )
    assert (run_dir / "procedures.jsonl").is_file(), "the log is written when asked for"
    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert record["schema_link"]["source"] == "in-process"
    assert record["schema_link"]["procedure_log"] is None


def test_off_run_json_is_byte_identical_to_the_pre_change_writer(prepared_run, monkeypatch) -> None:
    """With the arm off, the new writer must produce exactly what the old one did."""

    plan, query, run_dir = prepared_run

    # Run 1: the current code path, arm off (the aggregate is None, so the field is None).
    first = run_benchmark.run_predict_only(
        plan=plan, query_file=query, answer_file=query, tool_class=Text2SQLGenerator, client=_CannedClient()
    )
    assert first["schema_link"] is None
    masks = [str(run_dir), str(run_dir.parent / "recorder-run-old"), "recorder-run-old", "recorder-run"]
    first_json = _normalise(first, masks=masks)

    # Run 2: simulate the pre-change writer by forcing the in-process aggregator to None,
    # which is what it returned for an off arm before this change existed.
    monkeypatch.setattr(run_benchmark, "schema_link_summary_from_tools", lambda _tools: None)
    plan_again = dict(plan, run_id="recorder-run-old", run_dir=str(run_dir.parent / "recorder-run-old"))
    second = run_benchmark.run_predict_only(
        plan=plan_again, query_file=query, answer_file=query, tool_class=Text2SQLGenerator, client=_CannedClient()
    )
    assert second["schema_link"] is None
    assert _normalise(second, masks=masks) == first_json, "an off run must not gain or lose a field"
    assert not any(key.startswith("schema_link_") for key in first)
