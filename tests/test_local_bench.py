"""Tests for the offline BIRD substrate (schemas + SQLite protocol).

Everything here runs offline: no database server, no network, no downloads.  The
SQLite tests need the one-time extraction in ``research/advtext2sql/data_cache``
and are skipped (not failed) when it is absent — a clean checkout still runs the
renderer, DSN-encoding and in-memory scoring tests.

What is pinned here:

* the schema renderer is **byte-identical** to the tool's
  ``_get_db_schema_light`` (training/serving drift is silent and expensive);
* ``db_schemas.json`` covers all 8 BIRD dev databases and its table/column names
  come from the real database files, not from the question text;
* the gold artifact is deterministic and reports ``701`` as the only question
  that exceeds the official 30 s budget (0 execute errors, unlimited budget);
* the score path is the official SQLite protocol (set equality of rows), clearly
  named, and keeps unexecutable gold out of ``ex_over_scoreable``;
* distillation keeps only teacher answers that execute **and** match gold.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from dev_tools import dump_schemas, local_bench, paths

REPO_ROOT = Path(__file__).resolve().parents[1]
CAPTAIN_PG_DUMP = REPO_ROOT.parent / "artifacts" / "db_schemas_bird.json"
SHIPPED_SCHEMAS = paths.DEFAULT_SCHEMAS
GOLD_ARTIFACT = paths.ARTIFACTS_DIR / "gold_local_results.json"

HAVE_SQLITE = all(paths.find_sqlite(db_id) is not None for db_id in paths.BIRD_DATABASES)
requires_sqlite = pytest.mark.skipif(
    not HAVE_SQLITE,
    reason=(
        "official SQLite copies are not extracted; run the one-time download described "
        "in docs/LOCAL_BENCH.md (they live in research/advtext2sql/data_cache)"
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sqlite_db(root: Path, db_id: str = "demo") -> Path:
    """Create ``<root>/<db_id>/<db_id>.sqlite`` with a small deterministic table."""

    target = root / db_id / f"{db_id}.sqlite"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    connection = sqlite3.connect(str(target))
    try:
        connection.execute("CREATE TABLE cities (name TEXT, population INTEGER)")
        connection.executemany(
            "INSERT INTO cities (name, population) VALUES (?, ?)",
            [("Amsterdam", 900), ("Berlin", 3700), ("Cairo", 9500)],
        )
        connection.commit()
    finally:
        connection.close()
    return target


def _answers() -> list[dict]:
    return [
        {
            "question_id": 1,
            "db_id": "demo",
            "question": "How many cities are there?",
            "evidence": "",
            "SQL": "SELECT COUNT(*) FROM cities",
            "difficulty": "simple",
        },
        {
            "question_id": 2,
            "db_id": "demo",
            "question": "List the city names.",
            "evidence": "",
            "SQL": "SELECT name FROM cities",
            "difficulty": "moderate",
        },
    ]


# ---------------------------------------------------------------------------
# Schema renderer: format, tool parity, shipped dump
# ---------------------------------------------------------------------------


def test_render_light_schema_matches_the_documented_shape():
    metadata = [("cities", [("name", "TEXT"), ("population", "INTEGER")]), ("empty", [])]
    rendered = dump_schemas.render_light_schema(metadata)
    assert rendered == (
        "TABLE cities\n"
        "  - name (TEXT)\n"
        "  - population (INTEGER)\n"
        "\n"
        "TABLE empty"
    )


def test_parse_light_schema_round_trips_and_rejects_garbage():
    metadata = [("cities", [("name", "TEXT")]), ("other", [("x", "BIGINT")])]
    rendered = dump_schemas.render_light_schema(metadata)
    assert dump_schemas.parse_light_schema(rendered) == {
        "cities": [("name", "TEXT")],
        "other": [("x", "BIGINT")],
    }
    with pytest.raises(ValueError):
        dump_schemas.parse_light_schema("TABLE cities\n  name TEXT, no dash")
    with pytest.raises(ValueError):
        dump_schemas.parse_light_schema("just some words")


def test_validate_schema_dump_accepts_light_and_rejects_stubs():
    good = {"demo": dump_schemas.render_light_schema([("cities", [("name", "TEXT")])])}
    assert dump_schemas.validate_schema_dump(good) == []

    problems = dump_schemas.validate_schema_dump({"demo": "TABLE unknown  -- STUB, db_id=demo"})
    assert any("stub" in problem for problem in problems)

    assert dump_schemas.validate_schema_dump("not a dict")
    assert dump_schemas.validate_schema_dump({"demo": 42})


@requires_sqlite
def test_renderer_is_byte_identical_to_the_tool_for_one_database():
    """The training prompt must be the string the serving tool prints."""

    db_id = "california_schools"
    path = paths.find_sqlite(db_id)
    assert path is not None
    dsn = paths.sqlite_dsn(path)

    ours = dump_schemas.render_light_schema(dump_schemas.read_metadata(create_engine(dsn)))
    theirs = dump_schemas.tool_light_schema(dsn)

    assert ours == theirs
    assert "TABLE frpm" in ours and "  - " in ours


@requires_sqlite
def test_shipped_db_schemas_cover_the_eight_bird_databases_with_real_metadata():
    assert SHIPPED_SCHEMAS.is_file(), f"missing {SHIPPED_SCHEMAS}"
    payload = json.loads(SHIPPED_SCHEMAS.read_text(encoding="utf-8"))

    assert set(payload) == set(paths.BIRD_DATABASES)
    assert dump_schemas.validate_schema_dump(payload) == []

    for db_id in paths.BIRD_DATABASES:
        parsed = dump_schemas.parse_light_schema(payload[db_id])
        connection = local_bench.connect(db_id)
        try:
            real_tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                if not row[0].startswith("sqlite_")  # SQLAlchemy (and the tool) hide internals
            }
        finally:
            connection.close()
        assert set(parsed) == real_tables, f"{db_id}: schema tables != real tables"
        for table, columns in parsed.items():
            connection = local_bench.connect(db_id)
            try:
                real_columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            finally:
                connection.close()
            assert {name for name, _ in columns} == real_columns, f"{db_id}.{table}: columns differ"


@pytest.mark.skipif(not CAPTAIN_PG_DUMP.is_file(), reason="live-PostgreSQL schema dump not available")
def test_live_pg_dump_is_valid_light_format_and_structurally_close():
    """The PG dump is the preferred training source; only casing/types may differ."""

    theirs = json.loads(CAPTAIN_PG_DUMP.read_text(encoding="utf-8"))
    assert dump_schemas.validate_schema_dump(theirs) == []
    assert set(theirs) == set(paths.BIRD_DATABASES)

    if not SHIPPED_SCHEMAS.is_file():
        pytest.skip("SQLite schema dump not built")
    ours = json.loads(SHIPPED_SCHEMAS.read_text(encoding="utf-8"))
    drift = dump_schemas.compare_schema_dumps(
        ours, theirs, ours_label="sqlite", theirs_label="postgres"
    )

    # Same databases, same tables case-insensitively, no column added or lost:
    # the drift is casing (PostgreSQL folds unquoted identifiers) and BIGINT vs
    # INTEGER type text only.
    assert drift["only_ours"] == [] and drift["only_theirs"] == []
    assert drift["table_diffs"] == [] or all(d.get("kind") == "casing" for d in drift["table_diffs"])
    assert all(d.get("kind") == "casing" for d in drift["column_diffs"]), drift["column_diffs"][:3]
    assert all(
        {d["sqlite"], d["postgres"]} == {"INTEGER", "BIGINT"} for d in drift["type_diffs"]
    ), drift["type_diffs"][:3]
    assert drift["n_casing_differences"] > 0  # 4 of the 8 databases are folded by PostgreSQL


# ---------------------------------------------------------------------------
# Credentials and DSNs (the tunnel path, tested without the tunnel)
# ---------------------------------------------------------------------------


def test_postgres_dsn_percent_encodes_every_reserved_character():
    password = "p/a@ss:w#rd %&+"
    dsn = dump_schemas.postgres_dsn("db1", host="127.0.0.1", port=5444, user="readonly_user", password=password)

    assert "%2F" in dsn and "%40" in dsn and "%23" in dsn  # / @ # are encoded in the URL
    parsed = make_url(dsn)
    assert parsed.password == password  # ... and decoded again by SQLAlchemy
    assert parsed.username == "readonly_user"
    assert parsed.host == "127.0.0.1"
    assert parsed.port == 5444
    assert parsed.database == "db1"


def test_load_credentials_strips_quotes_and_fails_loudly(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        'DB_CLIENT_HOST="127.0.0.1"\nDB_PORT="5444"\nDB_USER="readonly_user"\nDB_PASS="a/b"\n',
        encoding="utf-8",
    )
    credentials = dump_schemas.load_credentials(env)
    assert credentials == {
        "host": "127.0.0.1",
        "port": "5444",
        "user": "readonly_user",
        "password": "a/b",
    }

    broken = tmp_path / "broken.env"
    broken.write_text('DB_CLIENT_HOST="127.0.0.1"\n', encoding="utf-8")
    with pytest.raises(RuntimeError, match="DB_PORT"):
        dump_schemas.load_credentials(broken)


def test_build_schemas_reports_missing_databases_instead_of_stubbing(tmp_path):
    _make_sqlite_db(tmp_path / "cache", "demo")
    schemas, manifest, missing, source = dump_schemas.build_schemas(
        ["demo", "absent_db"], sqlite_dir=tmp_path / "cache"
    )
    assert source == "sqlite"
    assert set(schemas) == {"demo"}
    assert manifest["demo"]["tables"] == 1
    assert [item["db_id"] for item in missing] == ["absent_db"]
    assert "absent_db" not in schemas  # never synthesised


# ---------------------------------------------------------------------------
# Local bench: gold execution, official EX, distillation
# ---------------------------------------------------------------------------


@requires_sqlite
def test_run_gold_bird_small_end_to_end_and_artifact_is_deterministic(tmp_path):
    answers = REPO_ROOT / "data" / "bird_small.json"
    first = local_bench.run_gold(answers, timeout_s=30)
    second = local_bench.run_gold(answers, timeout_s=30)

    assert first["protocol"] == "bird-sqlite-official"
    assert first["mode"] == "gold_execution"
    assert first["n_questions"] == 22
    assert first["n_error"] == 0
    assert first["n_timeout"] == 0
    assert first["n_missing_db"] == 0
    assert first["error_ids"] == [] and first["timeout_ids"] == []
    assert first == second  # deterministic artifact

    out = tmp_path / "gold_local_results.json"
    out.write_text(json.dumps(first, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert json.loads(out.read_text(encoding="utf-8"))["n_ok"] == 22


@requires_sqlite
def test_timeout_budget_interrupts_the_runaway_gold_query():
    """Question 701 needs minutes on SQLite; the official budget is 30 s."""

    result = local_bench.run_gold(
        REPO_ROOT / "data" / "bird_large.json", timeout_s=2, question_ids=["701"]
    )
    assert result["n_questions"] == 1
    record = result["per_question"][0]
    assert record["status"] == "timeout"
    assert "timeout after 2s" in record["error"]
    assert result["timeout_ids"] == ["701"]


@pytest.mark.skipif(not GOLD_ARTIFACT.is_file(), reason="gold artifact not generated yet")
def test_shipped_gold_artifact_measures_241_with_one_timeout():
    artifact = json.loads(GOLD_ARTIFACT.read_text(encoding="utf-8"))
    assert artifact["protocol"] == "bird-sqlite-official"
    assert artifact["n_questions"] == 241
    assert artifact["n_error"] == 0
    assert artifact["n_timeout"] == 1
    assert artifact["timeout_ids"] == ["701"]
    assert artifact["timeout_s"] == 30.0


def test_official_ex_is_set_equality_of_rows(tmp_path):
    _make_sqlite_db(tmp_path / "cache")
    answers = _answers()
    gold = {str(row["question_id"]): row["SQL"] for row in answers}

    # different row order -> same set -> EX 1
    shuffled = dict(gold, **{"2": "SELECT name FROM cities ORDER BY name DESC"})
    result = local_bench.score(shuffled, answers, sqlite_dir=tmp_path / "cache")
    assert result["mode"] == "sqlite_ex_official"
    assert result["n_correct"] == 2 and result["ex"] == 1.0

    # a different row set -> EX 0 for that question
    wrong = dict(gold, **{"2": "SELECT name FROM cities WHERE population > 5000"})
    result = local_bench.score(wrong, answers, sqlite_dir=tmp_path / "cache")
    assert result["n_correct"] == 1
    assert result["ex_percent"] == pytest.approx(50.0)

    # duplicate rows are invisible to `set()` — this is official BIRD behaviour
    duplicate = dict(gold, **{"2": "SELECT name FROM cities UNION ALL SELECT name FROM cities"})
    result = local_bench.score(duplicate, answers, sqlite_dir=tmp_path / "cache")
    assert result["n_correct"] == 2


def test_score_separates_gold_failures_from_model_failures(tmp_path):
    _make_sqlite_db(tmp_path / "cache")
    answers = [
        {
            "question_id": 10,
            "db_id": "demo",
            "question": "broken gold",
            "evidence": "",
            "SQL": "SELECT FROM nope (",
            "difficulty": "simple",
        }
    ]
    result = local_bench.score({"10": "SELECT 1"}, answers, sqlite_dir=tmp_path / "cache")
    assert result["n_gold_unusable"] == 1
    assert result["gold_unusable_ids"] == ["10"]
    assert result["ex"] == 0.0
    assert result["n_scored"] == 0
    assert result["ex_over_scoreable"] is None


@requires_sqlite
def test_score_bird_small_with_gold_as_predictions_is_perfect():
    answers = REPO_ROOT / "data" / "bird_small.json"
    gold = json.loads(answers.read_text(encoding="utf-8"))
    predictions = {str(row["question_id"]): row["SQL"] for row in gold}

    result = local_bench.score(predictions, answers, timeout_s=30)
    assert result["n_questions"] == 22
    assert result["n_correct"] == 22
    assert result["ex"] == 1.0
    assert result["n_gold_unusable"] == 0
    assert result["n_pred_error"] == 0
    assert set(result["by_difficulty"]) == {"simple", "moderate", "challenging"}


def test_filter_for_distillation_keeps_only_executing_matches(tmp_path):
    _make_sqlite_db(tmp_path / "cache")
    answers = _answers() + [
        {
            "question_id": 3,
            "db_id": "demo",
            "question": "broken gold",
            "evidence": "",
            "SQL": "SELECT FROM nope (",
            "difficulty": "simple",
        }
    ]
    teacher = {
        "1": "SELECT COUNT(*) FROM cities",          # executes, matches -> kept
        "2": "SELECT name FROM cities WHERE 1=0",    # executes, mismatch -> dropped
        "3": "SELECT 1",                             # gold unusable -> dropped
        # question 4 is missing entirely (answers has none) and 1/2/3 cover the rest
    }
    out = tmp_path / "distill_sft.json"
    report = local_bench.filter_for_distillation(
        teacher, answers, out=out, sqlite_dir=tmp_path / "cache"
    )

    assert report["mode"] == "distillation_filter"
    assert report["n_input"] == 3
    assert report["n_kept"] == 1
    assert report["kept_ids"] == ["1"]
    assert report["drop_counts"]["mismatch"] == 1
    assert report["drop_counts"]["gold_unusable"] == 1

    kept = json.loads(out.read_text(encoding="utf-8"))
    assert len(kept) == 1
    assert kept[0]["sql"] == "SELECT COUNT(*) FROM cities"
    assert kept[0]["question_id"] == "1"


def test_filter_for_distillation_jsonl_and_missing_predictions(tmp_path):
    _make_sqlite_db(tmp_path / "cache")
    answers = _answers()
    out = tmp_path / "distill.jsonl"
    report = local_bench.filter_for_distillation(
        {"1": "SELECT COUNT(*) FROM cities"},
        answers,
        out=out,
        sqlite_dir=tmp_path / "cache",
        output_format="jsonl",
    )
    assert report["n_kept"] == 1
    assert report["drop_counts"]["missing_prediction"] == 1
    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1 and lines[0]["question_id"] == "1"


# ---------------------------------------------------------------------------
# CLI and protocol naming
# ---------------------------------------------------------------------------


def test_protocol_is_named_so_it_cannot_be_confused_with_the_pg_harness(tmp_path):
    _make_sqlite_db(tmp_path / "cache")
    assert local_bench.PROTOCOL == "bird-sqlite-official"
    assert "NOT the course PostgreSQL harness" in local_bench.HARNESS

    result = local_bench.score(
        {"1": "SELECT COUNT(*) FROM cities"}, _answers()[:1], sqlite_dir=tmp_path / "cache"
    )
    assert result["mode"] == "sqlite_ex_official"
    assert "course PostgreSQL harness" in result["not_comparable_with"]
    assert "EX/241" in result["not_comparable_with"]


@requires_sqlite
def test_cli_gold_score_and_distill_write_their_artifacts(tmp_path):
    answers = REPO_ROOT / "data" / "bird_small.json"
    gold_out = tmp_path / "gold.json"
    assert local_bench.main(["gold", "--answers", str(answers), "--out", str(gold_out)]) == 0
    assert json.loads(gold_out.read_text(encoding="utf-8"))["n_questions"] == 22

    predictions = tmp_path / "predictions.json"
    gold = json.loads(answers.read_text(encoding="utf-8"))
    predictions.write_text(
        json.dumps({str(row["question_id"]): row["SQL"] for row in gold}), encoding="utf-8"
    )
    score_out = tmp_path / "score.json"
    assert (
        local_bench.main(
            ["score", "--predictions", str(predictions), "--answers", str(answers), "--out", str(score_out)]
        )
        == 0
    )
    assert json.loads(score_out.read_text(encoding="utf-8"))["n_correct"] == 22

    sft_out = tmp_path / "sft.json"
    assert (
        local_bench.main(
            ["distill", "--predictions", str(predictions), "--answers", str(answers), "--out", str(sft_out)]
        )
        == 0
    )
    assert len(json.loads(sft_out.read_text(encoding="utf-8"))) == 22


def test_connect_never_downloads_and_names_the_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError) as excinfo:
        local_bench.connect("totally_absent_db", tmp_path)
    message = str(excinfo.value)
    assert "totally_absent_db" in message
    assert "docs/LOCAL_BENCH.md" in message


# ---------------------------------------------------------------------------
# run_benchmark: the real harness wrapper (never called with a paid endpoint here)
# ---------------------------------------------------------------------------


def _run_argv(tmp_path, answers, runs_dir, *extra):
    return [
        "--run-id",
        "test-run",
        "--query-file",
        str(answers),
        "--answer-file",
        str(answers),
        "--db-url",
        "127.0.0.1:5444",
        "--llm-base-url",
        "http://127.0.0.1:9/v1",
        "--llm-model",
        "stub-model",
        "--runs-dir",
        str(runs_dir),
        *extra,
    ]


def test_run_benchmark_check_mode_plans_without_spending_or_writing(tmp_path, monkeypatch):
    from dev_tools import run_benchmark

    monkeypatch.setenv("DB_USER", "readonly_user")
    monkeypatch.setenv("DB_PASS", "secret/with/slashes")
    answers = REPO_ROOT / "data" / "bird_small.json"
    runs_dir = tmp_path / "runs"

    assert run_benchmark.main(_run_argv(tmp_path, answers, runs_dir, "--check")) == 0
    assert not runs_dir.exists()  # --check creates nothing


def test_run_benchmark_refuses_without_allow_llm_spend_and_on_existing_artifacts(tmp_path, monkeypatch):
    from dev_tools import run_benchmark

    monkeypatch.setenv("DB_USER", "readonly_user")
    monkeypatch.setenv("DB_PASS", "secret")
    answers = REPO_ROOT / "data" / "bird_small.json"
    runs_dir = tmp_path / "runs"

    # no --check, no --allow-llm-spend -> refused before any import or call
    assert run_benchmark.main(_run_argv(tmp_path, answers, runs_dir)) == 2

    # an existing, non-empty run directory must never be overwritten (R1-D22)
    existing = runs_dir / "test-run"
    existing.mkdir(parents=True)
    (existing / "query_results.json").write_text("{}", encoding="utf-8")
    assert run_benchmark.main(_run_argv(tmp_path, answers, runs_dir, "--check")) == 2
    assert (existing / "query_results.json").read_text(encoding="utf-8") == "{}"
