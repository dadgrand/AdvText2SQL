"""Unit tests for the offline evaluation layer.

Everything here runs in-process: no database, no network, no LLM key, no API
calls.  SQL is "executed" by :class:`tests.conftest.FakeDB`, which validates a
query as PostgreSQL and derives deterministic rows from its text.

The tests deliberately pin the headline numbers of the plan:

* the shipped rewrite destroys exactly **30/241** ``bird_large`` gold queries
  (``sqlglot 30.23.0``) and **2/22** ``bird_small`` ones,
* a perfect submission therefore scores **EX/241 = 211/241 = 87.55 %** but
  **EX/211 = 211/211 = 100 %** under the shipped evaluator,
* Wilson 50 % at n=22 is ``[0.3072, 0.6928]``,
* exact McNemar with b=6, c=0 gives ``p = 0.03125``,
* mini_dev soft-F1 reproduces its documented worked example (tp=4, fp=1, fn=1
  -> F1 = 0.8).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys

import pytest

from dev_tools import fixed_eval as fe
from dev_tools.fixed_eval import (
    cell_counts,
    gold_damage_report,
    main,
    mcnemar_exact_p,
    paired_bootstrap_ci,
    paired_comparison,
    repo_rewrite,
    report,
    score_predictions,
    soft_f1,
    sqlite_to_postgres_safe,
    wilson_ci,
)
from dev_tools.stub_llm import StubLLM, StubLLMError, prompt_hash

# ---------------------------------------------------------------------------
# Constants: measured with sqlglot 30.23.0 (research/advtext2sql/
# captain-verified-numbers.md, section B, reproduced by this module)
# ---------------------------------------------------------------------------

KNOWN_DAMAGED_241 = {
    "85", "62", "116", "125", "169", "317", "298", "219", "417", "402",
    "415", "506", "416", "371", "487", "716", "598", "639", "701", "818",
    "835", "760", "775", "743", "834", "1525", "1531", "1533", "1490", "1482",
}

KNOWN_DAMAGED_22 = {"417", "1533"}

DIFFICULTIES = ("simple", "moderate", "challenging")


# ---------------------------------------------------------------------------
# The shipped rewrite vs the safe AST rewrite
# ---------------------------------------------------------------------------


def test_repo_rewrite_is_the_shipped_defect():
    """The shipped regex eats the NULLIF token sqlglot already emitted."""

    query = "SELECT CAST(SUM(T1.id) AS REAL) * 100 / COUNT(T1.id) FROM t AS T1"
    rewritten = repo_rewrite(query)
    assert "NULLIF(NULLIF,0)" in rewritten
    assert not fe._pg_parses(rewritten)  # => gold cannot execute
    # the same query survives the AST-based rewrite
    assert fe._pg_parses(sqlite_to_postgres_safe(query))


def test_safe_rewrite_guards_a_column_denominator_exactly_once():
    assert sqlite_to_postgres_safe("SELECT a / b FROM t") == "SELECT a / NULLIF(b, 0) FROM t"


def test_safe_rewrite_keeps_qualified_denominators_intact():
    out = sqlite_to_postgres_safe("SELECT T1.Price / T2.Amount FROM t AS T1")
    assert out == "SELECT T1.Price / NULLIF(T2.Amount, 0) FROM t AS T1"
    # the shipped regex produces `NULLIF(T1,0).Amount` (or `NULLIF(NULLIF,0)(...)`);
    # the AST rewrite never splits a qualified name.
    assert "NULLIF(T1, 0)." not in out
    assert "NULLIF(T2, 0)." not in out


def test_safe_rewrite_guards_aggregate_denominators():
    out = sqlite_to_postgres_safe("SELECT CAST(COUNT(*) AS REAL) * 100 / COUNT(x) FROM t")
    assert out == "SELECT CAST(COUNT(*) AS REAL) * 100 / NULLIF(COUNT(x), 0) FROM t"


def test_safe_rewrite_preserves_identifier_case_and_quoting():
    out = sqlite_to_postgres_safe(
        "SELECT COUNT(T2.`School Code`), T1.AvgScrMath FROM satscores AS T1 "
        "INNER JOIN frpm AS T2 ON T1.cds = T2.CDSCode WHERE T2.`Charter Funding Type` = 'Directly funded'"
    )
    assert 'T2."School Code"' in out
    assert 'T2."Charter Funding Type"' in out
    assert "T1.AvgScrMath" in out  # casing preserved
    assert "`" not in out


def test_safe_rewrite_is_idempotent():
    once = sqlite_to_postgres_safe("SELECT a / b FROM t")
    twice = sqlite_to_postgres_safe(once)
    assert once == twice == "SELECT a / NULLIF(b, 0) FROM t"


def test_safe_rewrite_rejects_non_sql():
    with pytest.raises(Exception):
        sqlite_to_postgres_safe("SELECT FROM WHERE (")


# ---------------------------------------------------------------------------
# Gold-damage audit: the 30/241 and 2/22 ceiling
# ---------------------------------------------------------------------------


def test_gold_damage_report_reproduces_30_of_241(bird_large):
    items = gold_damage_report(bird_large)
    ids = [item["question_id"] for item in items]

    assert len(ids) == 30
    assert set(ids) == KNOWN_DAMAGED_241
    assert fe.expected_n_damaged("data/bird_large.json") == 30

    for item in items:
        assert item["parse_error"], item
        assert item["damage_kind"] == "nullif_regex_corruption"
        assert item["regex_evidence"]
        assert item["safe_rewritten"]
        assert item["safe_parse_error"] is None  # the fix reparses every damaged query


def test_gold_damage_report_reproduces_2_of_22(bird_small):
    items = gold_damage_report(bird_small)
    assert [item["question_id"] for item in items] == ["417", "1533"]
    assert {item["question_id"] for item in items} == KNOWN_DAMAGED_22
    assert fe.expected_n_damaged("data/bird_small.json") == 2


def test_gold_silent_corruption_report_finds_3_more(bird_large):
    """3 more gold queries survive the regex but are mutated inside a literal."""

    items = fe.gold_silent_corruption_report(bird_large)
    ids = [item["question_id"] for item in items]
    assert ids == ["4", "35", "572"]
    for item in items:
        assert item["rewritten"] != item["safe_rewritten"]
        assert fe._pg_parses(item["rewritten"])  # executes, but means something else
        assert fe._pg_parses(item["safe_rewritten"])


def test_gold_damage_report_accepts_a_path(data_dir):
    items = gold_damage_report(str(data_dir / "bird_small.json"))
    assert len(items) == 2


# ---------------------------------------------------------------------------
# Statistics with hand-computed values
# ---------------------------------------------------------------------------


def test_wilson_interval_half_at_22():
    low, high = wilson_ci(11, 22)
    assert low == pytest.approx(0.3072, abs=1e-4)
    assert high == pytest.approx(0.6928, abs=1e-4)
    assert low <= 0.5 <= high


def test_wilson_interval_bounds():
    assert wilson_ci(0, 10)[0] == pytest.approx(0.0, abs=1e-12)
    assert wilson_ci(10, 10)[1] == pytest.approx(1.0, abs=1e-12)
    assert wilson_ci(0, 0) == (None, None)
    with pytest.raises(ValueError):
        wilson_ci(11, 10)


def test_mcnemar_exact_matches_hand_computation():
    assert mcnemar_exact_p(6, 0) == pytest.approx(0.031, abs=1e-3)
    assert mcnemar_exact_p(6, 0) == pytest.approx(0.03125, abs=1e-12)
    assert mcnemar_exact_p(0, 0) == 1.0
    assert mcnemar_exact_p(5, 5) == 1.0
    assert mcnemar_exact_p(0, 6) == pytest.approx(0.03125, abs=1e-12)


def test_soft_f1_reproduces_the_mini_dev_worked_example():
    """mini_dev README table: tp=4, fp=1, fn=1 -> P=R=0.8 -> F1=0.8."""

    gold_rows = [("Apple", 325), ("Orange", None), ("Banana", 119)]
    pred_rows = [(325, "Apple"), (191, "Orange"), (None, "Banana")]

    assert cell_counts(pred_rows, gold_rows) == (4, 1, 1)
    assert soft_f1(pred_rows, gold_rows) == pytest.approx(0.8, abs=1e-12)


def test_soft_f1_edge_cases():
    assert soft_f1([], []) == 1.0
    assert soft_f1([(1,)], []) == 0.0
    assert soft_f1([], [(1,)]) == 0.0
    # order-insensitive alignment rescues a permuted table
    assert soft_f1([(1, 2), (3, 4)], [(3, 4), (1, 2)]) < 1.0
    assert soft_f1([(1, 2), (3, 4)], [(3, 4), (1, 2)], order_insensitive=True) == 1.0


def test_paired_bootstrap_is_seeded_and_brackets_the_point_estimate():
    deltas = [1.0] * 6 + [0.0] * 24
    low, high = paired_bootstrap_ci(deltas, n_boot=2000, seed=0)
    assert low is not None and high is not None
    assert 0.0 <= low <= 0.2 <= high <= 1.0
    assert (low, high) == paired_bootstrap_ci(deltas, n_boot=2000, seed=0)  # deterministic
    assert paired_bootstrap_ci([0.0] * 30, n_boot=500, seed=1) == (0.0, 0.0)


# ---------------------------------------------------------------------------
# Scoring: local vs fixed vs normalized, on the real 241 questions
# ---------------------------------------------------------------------------


def _as_safe_predictions(answer_rows):
    return {str(row["question_id"]): sqlite_to_postgres_safe(row["SQL"]) for row in answer_rows}


def test_local_mode_reproduces_the_harness_ceiling(bird_large, fake_db):
    """A perfect PostgreSQL submission scores 208/241 in local mode.

    30 gold items cannot execute at all (the regex eats the NULLIF that sqlglot
    emitted) and 3 more execute but were silently mutated inside a string
    literal / quoted identifier containing a slash, so 211 - 3 = 208.
    """

    predictions = _as_safe_predictions(bird_large)
    result = score_predictions(predictions, bird_large, mode="local", execute=fake_db)

    assert result["mode"] == "local"
    assert result["n_questions"] == 241
    assert result["ex_all"]["correct"] == 208
    assert result["ex_all"]["value"] == pytest.approx(208 / 241)
    assert result["ex_all"]["percent"] == pytest.approx(100.0 * 208 / 241)

    assert result["n_gold_unexecutable"] == 30
    assert set(result["gold_unexecutable_ids"]) == KNOWN_DAMAGED_241
    assert all(result["gold_unexecutable_reasons"][qid] for qid in KNOWN_DAMAGED_241)

    # only the 211 executable gold items can even be scored ...
    assert result["ex_gold_executable"]["n"] == 211
    # ... and 3 of them were mutated by the regex, so not even a perfect
    # submission reaches 211/211 under the shipped evaluator.
    assert result["ex_gold_executable"]["correct"] == 208
    assert result["ex_gold_executable"]["value"] == pytest.approx(208 / 211)

    mutated = {r["question_id"] for r in result["per_question"] if r["status"] == "scored" and not r["score"]}
    assert mutated == {"4", "35", "572"}

    assert result["n_pred_unexecutable"] == 0
    assert result["n_missing_predictions"] == 0
    assert result["paired"] is None


def test_fixed_mode_removes_the_ceiling(bird_large, fake_db):
    predictions = _as_safe_predictions(bird_large)
    result = score_predictions(predictions, bird_large, mode="fixed", execute=fake_db)

    assert result["n_gold_unexecutable"] == 0
    assert result["ex_all"]["correct"] == 241
    assert result["ex_all"]["value"] == 1.0
    assert result["ex_gold_executable"]["n"] == 241
    assert result["soft_f1_mean"] == pytest.approx(1.0)


def test_normalized_mode_rescues_sqlite_style_predictions(bird_large, fake_db):
    """Raw BIRD-style predictions (backticks, SQLite functions) fail as-is."""

    raw_predictions = {str(row["question_id"]): row["SQL"] for row in bird_large}

    local = score_predictions(raw_predictions, bird_large, mode="local", execute=fake_db)
    normalized = score_predictions(raw_predictions, bird_large, mode="normalized", execute=fake_db)

    assert local["n_pred_unexecutable"] > 0
    assert local["ex_all"]["correct"] < normalized["ex_all"]["correct"]
    assert normalized["ex_all"]["correct"] == 241
    assert normalized["n_gold_unexecutable"] == 0


def test_report_contract_keys_and_difficulty_blocks(bird_large, fake_db):
    predictions = _as_safe_predictions(bird_large)
    result = score_predictions(predictions, bird_large, mode="fixed", execute=fake_db)

    for key in ("ex_all", "ex_gold_executable", "n_gold_unexecutable", "per_difficulty", "paired"):
        assert key in result

    assert set(result["per_difficulty"]) == {"simple", "moderate", "challenging"}
    assert sum(block["n"] for block in result["per_difficulty"].values()) == 241
    for block in result["per_difficulty"].values():
        low, high = block["wilson95"]
        assert low <= block["ex"] <= high
        assert block["n"] == block["correct"] or block["ex"] == pytest.approx(block["correct"] / block["n"])


def test_missing_predictions_are_scored_zero_not_crashed(bird_large, fake_db):
    predictions = _as_safe_predictions(bird_large)
    for qid in ("9", "62", "1533"):
        predictions.pop(qid)
    result = score_predictions(predictions, bird_large, mode="fixed", execute=fake_db)
    assert result["n_missing_predictions"] == 3
    assert result["ex_all"]["correct"] == 238


def test_no_executor_means_a_clear_error(bird_large):
    predictions = _as_safe_predictions(bird_large[:1])
    with pytest.raises(ValueError, match="no executor"):
        score_predictions(predictions, bird_large, mode="fixed")


def test_fixed_path_never_imports_the_repo_benchmarks_package(bird_large, fake_db, repo_root):
    """The fixed path must not depend on the shipped evaluator at all."""

    predictions = _as_safe_predictions(bird_large)
    score_predictions(predictions, bird_large, mode="fixed", execute=fake_db)

    # prove the import graph in a clean interpreter, so that this test does not
    # depend on which other test modules the session has already imported.
    code = (
        "import sys;"
        "import dev_tools.fixed_eval;"
        "bad = [m for m in sys.modules if m == 'benchmarks' or m.startswith('benchmarks.')];"
        "assert not bad, bad;"
        "print('ok')"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"


# ---------------------------------------------------------------------------
# Paired comparison block against a baseline predictions file
# ---------------------------------------------------------------------------


def _synthetic_answers(n_questions: int = 30):
    return [
        {
            "question_id": index,
            "db_id": "db1",
            "question": f"synthetic question {index}",
            "evidence": "",
            "SQL": f"SELECT {index} AS v",
            "difficulty": DIFFICULTIES[index % len(DIFFICULTIES)],
        }
        for index in range(n_questions)
    ]


def _synthetic_predictions(answers, first_wrong_id: int, default_sql: str):
    predictions = {}
    for row in answers:
        index = int(row["question_id"])
        predictions[str(index)] = row["SQL"] if index < first_wrong_id else default_sql
    return predictions


def test_paired_block_b6_c0_matches_the_pre_registered_example(fake_db):
    """current right on 24/30, baseline right on 18/30 => b=6, c=0, p=0.031."""

    answers = _synthetic_answers(30)
    current = _synthetic_predictions(answers, 24, "SELECT -1 AS v")
    baseline = _synthetic_predictions(answers, 18, "SELECT -2 AS v")

    result = score_predictions(
        current,
        answers,
        mode="fixed",
        execute=fake_db,
        baseline=baseline,
        n_boot=10_000,
        seed=0,
    )

    paired = result["paired"]
    assert paired is not None
    assert paired["n_paired"] == 30
    assert paired["b"] == 6
    assert paired["c"] == 0
    assert paired["discordant"] == 6
    assert paired["mcnemar_exact_p"] == pytest.approx(0.03125, abs=1e-6)
    assert paired["delta_ex"] == pytest.approx(6 / 30)

    low, high = paired["bootstrap95"]
    assert low is not None and high is not None
    assert low > 0.0  # the pre-registered rule wants a CI that excludes 0
    assert low <= paired["delta_ex"] <= high
    assert paired["n_boot"] == 10_000 and paired["seed"] == 0

    # deterministic: a second run with the same seed gives the same interval
    again = score_predictions(
        current,
        answers,
        mode="fixed",
        execute=fake_db,
        baseline=baseline,
        n_boot=10_000,
        seed=0,
    )
    assert again["paired"]["bootstrap95"] == paired["bootstrap95"]

    # EX and soft-F1 of the current run
    assert result["ex_all"]["correct"] == 24
    assert result["soft_f1_mean"] == pytest.approx(24 / 30)


def test_paired_comparison_covers_every_gold_question(fake_db):
    """Both ledgers contain every gold question, so the pair is complete.

    A predictions file that omits questions is not silently dropped from the
    pair: the missing questions are scored 0 (and counted as
    ``missing_prediction``), exactly as the report does.
    """

    answers = _synthetic_answers(4)
    current = _synthetic_predictions(answers, 4, "SELECT -1 AS v")
    baseline = {"0": answers[0]["SQL"], "1": answers[1]["SQL"]}

    current_records = fe.evaluate_predictions(current, answers, fake_db, mode="fixed")
    baseline_records = fe.evaluate_predictions(baseline, answers, fake_db, mode="fixed")
    paired = paired_comparison(current_records, baseline_records, n_boot=200, seed=3)

    assert paired["n_paired"] == 4
    assert paired["b"] == 2  # ids 2 and 3: current right, baseline wrong
    assert paired["c"] == 0
    assert paired["missing_in_baseline"] == []
    assert paired["missing_in_current"] == []
    baseline_by_id = {r["question_id"]: r for r in baseline_records}
    assert baseline_by_id["2"]["status"] == "missing_prediction"
    assert baseline_by_id["2"]["score"] == 0


def test_report_is_callable_on_hand_built_records():
    records = [
        {
            "question_id": "1",
            "db_id": "db",
            "difficulty": "simple",
            "status": "scored",
            "score": 1,
            "gold_ok": True,
            "pred_ok": True,
            "gold_error": None,
            "pred_error": None,
            "soft_f1": 1.0,
            "gold_sql": "SELECT 1",
            "pred_sql": "SELECT 1",
        },
        {
            "question_id": "2",
            "db_id": "db",
            "difficulty": "simple",
            "status": "gold_error",
            "score": 0,
            "gold_ok": False,
            "pred_ok": True,
            "gold_error": "ParseError: boom",
            "pred_error": None,
            "soft_f1": None,
            "gold_sql": "SELECT 2",
            "pred_sql": "SELECT 2",
        },
    ]
    result = report(records, mode="fixed")
    assert result["ex_all"] == {
        "correct": 1,
        "n": 2,
        "value": 0.5,
        "percent": 50.0,
        "wilson95": list(wilson_ci(1, 2)),
    }
    assert result["ex_gold_executable"]["n"] == 1
    assert result["n_gold_unexecutable"] == 1
    assert result["gold_unexecutable_ids"] == ["2"]
    assert result["per_difficulty"]["simple"]["n"] == 2


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_gold_damage_writes_the_30_ids(tmp_path, data_dir):
    out = tmp_path / "gold_damage.json"
    exit_code = main(
        [
            "--answers",
            str(data_dir / "bird_large.json"),
            "--out",
            str(out),
            "--gold-damage",
            "--quiet",
        ]
    )
    assert exit_code == 0

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["n_damaged"] == 30
    assert payload["n_damaged_matches_expectation"] is True
    assert payload["by_kind"] == {"nullif_regex_corruption": 30}
    assert {item["question_id"] for item in payload["items"]} == KNOWN_DAMAGED_241


def test_cli_help_lists_the_mode_switch(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    text = capsys.readouterr().out
    for token in ("--predictions", "--answers", "--out", "--mode", "--baseline", "--gold-damage"):
        assert token in text
    assert "local" in text and "fixed" in text and "normalized" in text


def test_cli_scoring_without_a_database_is_a_usage_error(tmp_path, data_dir):
    out = tmp_path / "report.json"
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "--answers",
                str(data_dir / "bird_small.json"),
                "--predictions",
                str(tmp_path / "missing.json"),
                "--out",
                str(out),
                "--mode",
                "fixed",
            ]
        )
    assert excinfo.value.code == 2


def test_cli_scores_with_an_injected_executor(tmp_path, data_dir, monkeypatch, fake_db):
    """The full CLI path: --predictions/--answers/--out/--mode/--baseline."""

    answers_path = data_dir / "bird_small.json"
    answers = json.loads(answers_path.read_text(encoding="utf-8"))
    predictions = {str(row["question_id"]): sqlite_to_postgres_safe(row["SQL"]) for row in answers}

    predictions_path = tmp_path / "predictions.json"
    predictions_path.write_text(json.dumps(predictions), encoding="utf-8")

    baseline = dict(predictions)
    for qid in list(baseline)[:2]:
        baseline[qid] = "SELECT -1 AS wrong"
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")

    monkeypatch.setattr(fe, "PostgresExecutor", lambda *args, **kwargs: fake_db)

    out = tmp_path / "report.json"
    exit_code = main(
        [
            "--predictions",
            str(predictions_path),
            "--answers",
            str(answers_path),
            "--out",
            str(out),
            "--mode",
            "fixed",
            "--baseline",
            str(baseline_path),
            "--db-url",
            "stub:5432",
            "--username",
            "stub",
            "--password",
            "stub",
            "--quiet",
        ]
    )
    assert exit_code == 0

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["mode"] == "fixed"
    assert payload["n_questions"] == 22
    assert payload["ex_all"]["correct"] == 22
    assert payload["n_gold_unexecutable"] == 0
    assert payload["soft_f1_mean"] == pytest.approx(1.0)
    assert payload["paired"]["n_paired"] == 22
    assert payload["paired"]["b"] == 2
    assert payload["paired"]["c"] == 0
    assert payload["paired"]["mcnemar_exact_p"] == pytest.approx(0.5)


def test_cli_runs_as_a_module(tmp_path, repo_root):
    out = tmp_path / "damage_cli.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "dev_tools.fixed_eval",
            "--answers",
            "data/bird_large.json",
            "--out",
            str(out),
            "--gold-damage",
            "--quiet",
        ],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(out.read_text(encoding="utf-8"))["n_damaged"] == 30


# ---------------------------------------------------------------------------
# Stub LLM (offline substrate for the tool tests)
# ---------------------------------------------------------------------------


def test_stub_llm_is_deterministic_and_counts_usage():
    messages = [{"role": "user", "content": "How many schools are directly funded?"}]
    key = prompt_hash(messages)
    stub = StubLLM(responses={key: "SELECT COUNT(*) FROM schools"})

    first = asyncio.run(stub.create(messages=messages, model="gpt-stub"))
    second = asyncio.run(stub.create(messages=messages))

    assert first.choices[0].message.content == "SELECT COUNT(*) FROM schools"
    assert second.choices[0].message.content == first.choices[0].message.content
    assert first.model == "gpt-stub"

    usage = stub.get_usage()
    assert usage["calls"] == 2
    assert usage["prompt_tokens"] > 0
    assert usage["completion_tokens"] > 0
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
    assert stub.calls_by_prompt[key] == 2

    stub.reset()
    assert stub.get_usage() == {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


def test_stub_llm_serves_scripted_answers_in_order():
    stub = StubLLM(responses=["SELECT 1", "SELECT 2", "SELECT 3"])
    messages = [{"role": "user", "content": "anything"}]
    contents = [asyncio.run(stub.create(messages=messages)).choices[0].message.content for _ in range(4)]
    assert contents == ["SELECT 1", "SELECT 2", "SELECT 3", "SELECT 3"]  # last one repeats


def test_stub_llm_fails_on_the_nth_call():
    stub = StubLLM(responses=["SELECT 1", "SELECT 2", "SELECT 3"], fail_on_calls=[2])
    messages = [{"role": "user", "content": "anything"}]

    assert asyncio.run(stub.create(messages=messages)).choices[0].message.content == "SELECT 1"
    with pytest.raises(StubLLMError):
        asyncio.run(stub.create(messages=messages))
    assert asyncio.run(stub.create(messages=messages)).choices[0].message.content == "SELECT 3"
    assert stub.get_usage()["calls"] == 3  # the failed call is still counted/attempted


def test_stub_llm_can_raise_a_custom_error():
    stub = StubLLM(responses="SELECT 1").fail_on_calls(1, error=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(stub.create(messages=[{"role": "user", "content": "x"}]))
