"""Tests for the round-2 decoder layer: guided proxy + repair-free arm.

Three things are asserted, because each one is a promise the measurement depends
on:

1. **Classification** — only the SQL-generation request may carry the grammar.
   A grammar accidentally applied to the ambiguity gate would force ``OK`` into a
   ``SELECT`` and quietly change the arm; a grammar *not* applied to the SQL call
   would make the "guided" arm unguided.
2. **No text rewriting** — the proxy adds ``structured_outputs`` and nothing else
   (no fence stripping, no cleanup), so the grammar arm's delta is decodable.
3. **The repair-free arm really is repair-free** — ``no_repair_tool`` must not
   spend a second generation on rejected SQL, while still using the sqlglot gate
   as the measurement and returning ``None`` (the harness's ``"error"``) when the
   first attempt is invalid.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, List

import pytest

from dev_tools import guided_proxy, run_benchmark
from dev_tools.guided_proxy import RunState, classify_messages, inject_structured_outputs


# ---------------------------------------------------------------------------
# Request classification
# ---------------------------------------------------------------------------


def test_sql_system_prompt_is_classified_guided():
    messages = [
        {"role": "system", "content": "You are an expert PostgreSQL analyst. ..."},
        {"role": "user", "content": "Translate: how many schools?"},
    ]
    assert classify_messages(messages) == "sql"


def test_ambiguity_system_prompt_is_left_unconstrained():
    messages = [
        {"role": "system", "content": "You audit a natural-language question for a database ..."},
        {"role": "user", "content": "Classify the question above."},
    ]
    assert classify_messages(messages) == "ambiguity"


def test_unknown_system_prompt_is_classified_other_and_never_guided():
    messages = [{"role": "system", "content": "You are a helpful assistant."}]
    assert classify_messages(messages) == "other"


def test_classification_reads_list_content_parts():
    messages = [
        {"role": "system", "content": [{"type": "text", "text": "You are an expert PostgreSQL analyst."}]}
    ]
    assert classify_messages(messages) == "sql"


def test_injection_adds_the_grammar_and_changes_nothing_else():
    payload = {
        "model": "m",
        "messages": [{"role": "system", "content": "You are an expert PostgreSQL analyst."}],
        "temperature": 0,
        "max_tokens": 512,
    }
    guided = inject_structured_outputs(payload, "root ::= \"SELECT\"")
    assert guided["structured_outputs"] == {"grammar": "root ::= \"SELECT\""}
    assert guided["messages"] == payload["messages"]
    assert guided["temperature"] == payload["temperature"]
    assert guided["max_tokens"] == payload["max_tokens"]
    # The caller's payload is not mutated: an arm must not leak into the next one.
    assert "structured_outputs" not in payload


# ---------------------------------------------------------------------------
# /health accounting
# ---------------------------------------------------------------------------


def test_run_state_counts_guided_and_unguided_requests(tmp_path):
    log = tmp_path / "llm.jsonl"
    state = RunState(log)
    state.record({"kind": "sql", "guided": True, "latency_s": 1.0, "ok": True, "completion_tokens": 40})
    state.record({"kind": "ambiguity", "guided": False, "latency_s": 0.5, "ok": True})
    state.record({"kind": "sql", "guided": True, "latency_s": 3.0, "ok": False, "completion_tokens": 90})

    assert state.requests == 3
    assert state.guided == 2
    assert state.unguided == 1
    assert state.by_kind == {"sql": 2, "ambiguity": 1}
    assert state.failures == 1
    assert state.max_completion_tokens == 90
    latency = state.latency_block()
    assert latency["guided"]["n"] == 2
    assert latency["guided"]["mean_s"] == 2.0
    assert latency["all"]["n"] == 3

    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [line["kind"] for line in lines] == ["sql", "ambiguity", "sql"]


# ---------------------------------------------------------------------------
# The repair-free arm
# ---------------------------------------------------------------------------


class _StubResult:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}


class _RecordingClient:
    """Minimal LLM client: returns canned answers in order and records the calls."""

    def __init__(self, answers: List[str]) -> None:
        self.answers = list(answers)
        self.calls: List[Any] = []

    async def create(self, messages: Any) -> _StubResult:
        self.calls.append(messages)
        return _StubResult(self.answers.pop(0) if self.answers else "")


class _StubTool:
    """Stand-in for ``Text2SQLGenerator`` with only the methods the wrapper touches."""

    def __init__(self, db_uri: str, llm_client: Any, **_: Any) -> None:
        self.db_uri = db_uri
        self.llm_client = llm_client
        self.system_prompt = "stub"

    async def _generate(self, question: str, schema: str, *, repair: Any = None) -> str:
        messages = [{"role": "system", "content": "sql"}]
        result = await self.llm_client.create(messages)  # type: ignore[attr-defined]
        return result.content

    @staticmethod
    def _validate(raw: Any) -> Any:
        text = (raw or "").strip()
        ok = text.upper().startswith("SELECT")
        return type("Validation", (), {"ok": ok, "sql": text, "error": None if ok else "not a select"})()


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_no_repair_tool_does_not_generate_twice_on_invalid_sql():
    client = _RecordingClient(["```sql\nnot sql\n```", "SELECT 1"])
    tool_cls = run_benchmark.no_repair_tool(_StubTool)
    tool = tool_cls(db_uri="x", llm_client=client)

    result = _run(tool._generate_valid_sql("q", "schema"))

    assert result is None, "an invalid first attempt must stay invalid, not be repaired"
    assert len(client.calls) == 1, "the repair call is exactly what this arm removes"


def test_no_repair_tool_keeps_the_first_valid_statement():
    client = _RecordingClient(["SELECT COUNT(*) FROM t"])
    tool_cls = run_benchmark.no_repair_tool(_StubTool)
    tool = tool_cls(db_uri="x", llm_client=client)

    assert _run(tool._generate_valid_sql("q", "schema")) == "SELECT COUNT(*) FROM t"
    assert len(client.calls) == 1


def test_no_repair_tool_skips_the_execution_repair():
    client = _RecordingClient([])
    tool_cls = run_benchmark.no_repair_tool(_StubTool)
    tool = tool_cls(db_uri="x", llm_client=client)

    assert _run(tool._refine_with_execution("q", "schema", "SELECT broken")) == "SELECT broken"
    assert client.calls == []


def test_no_repair_wrapper_is_named_for_run_json():
    wrapped = run_benchmark.no_repair_tool(_StubTool)
    assert wrapped.__name__ == "_StubToolNoRepair"
    plan = {"sql_repair": "disabled"}
    assert run_benchmark._tool_wrapper_name(plan) == "dev_tools.run_benchmark.no_repair_tool"


# ---------------------------------------------------------------------------
# plan_run refuses inert combinations
# ---------------------------------------------------------------------------


def test_plan_run_refuses_disable_repair_for_ambrosia(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_USER", "u")
    monkeypatch.setenv("DB_PASS", "p")
    query = tmp_path / "q.json"
    query.write_text("[]", encoding="utf-8")
    with pytest.raises(run_benchmark.RunRefused, match="disable-sql-repair"):
        run_benchmark.plan_run(
            run_id="r",
            query_file=query,
            answer_file=query,
            db_url="localhost:5444",
            tool_class="t",
            llm_base_url="http://127.0.0.1:8100/v1",
            llm_model="m",
            runs_dir=tmp_path / "runs",
            benchmark="ambrosia",
            sql_repair="disabled",
        )


def test_plan_run_records_sql_repair_state(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_USER", "u")
    monkeypatch.setenv("DB_PASS", "p")
    query = tmp_path / "q.json"
    query.write_text("[]", encoding="utf-8")
    plan = run_benchmark.plan_run(
        run_id="r",
        query_file=query,
        answer_file=query,
        db_url="localhost:5444",
        tool_class="t",
        llm_base_url="http://127.0.0.1:8100/v1",
        llm_model="m",
        runs_dir=tmp_path / "runs",
        sql_repair="disabled",
        server={"max_new_tokens": 512},
    )
    assert plan["sql_repair"] == "disabled"
    assert plan["max_tokens"] == 512


def test_grammar_file_is_shipped_and_parses_as_ebnf_text():
    grammar = guided_proxy.GRAMMAR_PATH
    text = grammar.read_text(encoding="utf-8")
    assert text.strip().startswith("#")
    assert "root ::=" in text
    assert 'select_stmt ::= "SELECT"' not in text  # the statement rule is composed, not a literal
    # Every rule must be defined exactly once: a duplicate silently changes the
    # language in llama.cpp-style GBNF, and xgrammar is not available on Windows
    # so this cheap check is what the offline test suite can enforce.
    names = [line.split("::=")[0].strip() for line in text.splitlines() if "::=" in line]
    assert len(names) == len(set(names)), "duplicate grammar rule"
    undefined = {
        token.strip()
        for line in text.splitlines()
        if "::=" in line
        for token in line.split("::=", 1)[1].split()
        if token.strip() and token.strip()[0].isalpha() and token.strip().isidentifier()
    } - set(names)
    assert not undefined, f"grammar references undefined rules: {sorted(undefined)}"


def test_grammar_keeps_its_negative_cases_out_of_the_language():
    """The four constructs the decoder must forbid stay forbidden.

    xgrammar is not importable offline, so this checks the grammar text: the
    fence/semicolon/lower-case forms must not appear as accepted terminals. It is
    a weak check by construction, and the strong one
    (``dev_tools.check_grammar`` on the A100 against 241 gold queries plus a
    59-case battery) is recorded in docs/DECODER.md.
    """

    text = guided_proxy.GRAMMAR_PATH.read_text(encoding="utf-8")
    rules = {
        line.split("::=")[0].strip(): line.split("::=", 1)[1].strip()
        for line in text.splitlines()
        if "::=" in line
    }
    # No rule may admit a fence, a semicolon or a backtick anywhere.
    assert "```" not in text
    assert ";" not in {char for rule in rules.values() for char in rule if char in ";`"}, (
        "the grammar must not accept semicolons or backticks"
    )
    # Keywords are upper-case: the lower-case alternation must not be present.
    assert '"select"' not in text and '"from"' not in text


def test_decoder_report_counts_parse_validity_and_sentinels():
    from dev_tools import decoder_report

    predictions = {
        "1": "SELECT 1",
        "2": "error",
        "3": "```sql\nSELECT 2\n```",  # unparseable as-is: fences are not PostgreSQL
        "4": "SELECT 3 FROM t",
    }
    block = decoder_report.parse_validity(predictions)
    assert block["n_predictions"] == 4
    assert block["n_error_sentinel"] == 1
    assert block["n_parse_fail"] == 1
    assert block["parse_fail_ids"] == ["3"]
    assert block["invalid_sql_rate"] == 0.5


def test_decoder_report_reads_decode_facts_from_a_proxy_log(tmp_path):
    from dev_tools import decoder_report

    log = tmp_path / "requests.jsonl"
    entries = [
        {"kind": "sql", "guided": True, "ok": True, "latency_s": 2.0, "completion_tokens": 20,
         "finish_reason": "stop", "completion": "SELECT 1"},
        {"kind": "sql", "guided": True, "ok": True, "latency_s": 4.0, "completion_tokens": 30,
         "finish_reason": "length", "completion": "```sql\nSELECT 2\n```"},
        {"kind": "ambiguity", "guided": False, "ok": True, "latency_s": 0.5,
         "completion": "OK"},
    ]
    log.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")

    facts = decoder_report.decode_facts(log)
    assert facts["n_requests"] == 3
    assert facts["n_sql_requests"] == 2
    assert facts["n_sql_guided"] == 2
    assert facts["n_finish_length"] == 1
    assert facts["n_with_fence"] == 1
    assert facts["n_not_starting_with_select"] == 1
    assert facts["latency_s_mean"] == 3.0
    assert facts["completion_tokens_mean"] == 25.0
