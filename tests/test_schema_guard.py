"""Offline tests for the empty-schema guard in ``dev_tools/run_benchmark.py``.

The defect these tests pin: ``benchmarks/base.py`` writes ``{db_id: tool.db_schema}``
and the tool renders ``""`` when it cannot read a schema (wrong credentials, tunnel
down).  The harness then runs the whole benchmark against empty prompts and writes a
plausible ledger — observed twice in round 2 (``runs/t2/r2-scale7b-ev`` and
``runs/ood-base7b-292``, the latter 5.14 % where the same model with a real schema
scored 71.67 %).

No database, no network, no model: the schema files are written into ``tmp_path``,
the pre-flight probe is monkeypatched, and the harness is replaced by a stub that
writes a controllable ``db_schemas.json``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from dev_tools import run_benchmark  # noqa: E402

HEALTHY_ONE = "TABLE frpm\n  - County Name (TEXT)\n  - Enrollment (INTEGER)"
HEALTHY_TWO = "TABLE satscores\n  - cds (TEXT)\n  - AvgScrMath (INTEGER)"
HEALTHY = {"california_schools": HEALTHY_ONE, "financial": HEALTHY_TWO}

BROKEN = {"california_schools": "", "financial": HEALTHY_TWO}
TRUNCATED = {"california_schools": "TABLE", "financial": HEALTHY_TWO}


def _write_schemas(run_dir: Path, payload: Any, *, raw: bytes | None = None) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / run_benchmark.SCHEMA_FILENAME
    if raw is not None:
        path.write_bytes(raw)
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# read_schema_health: the four shapes from the task
# ---------------------------------------------------------------------------


def test_all_healthy_schemas_pass(tmp_path):
    path = _write_schemas(tmp_path, HEALTHY)
    health = run_benchmark.read_schema_health(path)
    assert health["schemas_ok"] is True
    assert health["schemas_db_count"] == 2
    assert health["schemas_empty_count"] == 0
    assert health["schemas_empty_databases"] == []
    assert health["schemas_bytes_total"] == path.stat().st_size
    assert health["schemas_chars_total"] == len(HEALTHY_ONE) + len(HEALTHY_TWO)
    assert health["schemas_problem"] == ""


def test_one_empty_value_fails_and_is_named(tmp_path):
    health = run_benchmark.read_schema_health(_write_schemas(tmp_path, BROKEN))
    assert health["schemas_ok"] is False
    assert health["schemas_empty_count"] == 1
    assert health["schemas_empty_databases"] == ["california_schools"]
    assert "1/2" in health["schemas_problem"]


def test_one_truncated_value_fails(tmp_path):
    health = run_benchmark.read_schema_health(_write_schemas(tmp_path, TRUNCATED))
    assert health["schemas_ok"] is False
    assert health["schemas_empty_count"] == 1
    assert health["schemas_empty_databases"] == ["california_schools"]
    assert str(run_benchmark.SCHEMA_MIN_CHARS) in health["schemas_problem"]


def test_empty_dict_fails_with_no_databases(tmp_path):
    health = run_benchmark.read_schema_health(_write_schemas(tmp_path, {}))
    assert health["schemas_ok"] is False
    assert health["schemas_db_count"] == 0
    assert health["schemas_empty_count"] == 0
    assert "no databases" in health["schemas_problem"]


def test_missing_file_is_a_failed_fetch(tmp_path):
    health = run_benchmark.read_schema_health(tmp_path / "db_schemas.json")
    assert health["schemas_ok"] is False
    assert health["schemas_present"] is False
    assert "not written" in health["schemas_problem"]


def test_unreadable_and_wrong_shape_files_fail_safely(tmp_path):
    unreadable = run_benchmark.read_schema_health(
        _write_schemas(tmp_path, None, raw=b"{not json")
    )
    assert unreadable["schemas_ok"] is False
    assert "unreadable" in unreadable["schemas_problem"]

    wrong = run_benchmark.read_schema_health(_write_schemas(tmp_path / "other", ["a list"]))
    assert wrong["schemas_ok"] is False
    assert "not an object" in wrong["schemas_problem"]


def test_non_string_values_count_as_empty(tmp_path):
    health = run_benchmark.read_schema_health(
        _write_schemas(tmp_path, {"california_schools": None, "financial": HEALTHY_TWO})
    )
    assert health["schemas_empty_count"] == 1


def test_empty_databases_list_is_capped_but_the_count_is_exact(tmp_path):
    """The message names a few databases; the counts stay exact for the artefact."""

    payload = {f"db_{index}": "" for index in range(12)}
    health = run_benchmark.read_schema_health(_write_schemas(tmp_path, payload))
    assert health["schemas_empty_count"] == 12
    assert len(health["schemas_empty_databases"]) == run_benchmark.SCHEMA_EMPTY_NAMES_SHOWN
    assert "12/12" in health["schemas_problem"]


# ---------------------------------------------------------------------------
# The two real files from the 2026-10-29 incident (skipped on a clean checkout)
# ---------------------------------------------------------------------------

OOD_RUNS = REPO_ROOT / "runs" / "ood"
BROKEN_INCIDENT = OOD_RUNS / "ood-base7b-292" / "db_schemas.json"
HEALTHY_INCIDENT = OOD_RUNS / "ood-base7b-120" / "db_schemas.json"

INCIDENT_DATABASES = (
    "disney",
    "european_football_1",
    "law_episode",
    "menu",
    "movie_3",
    "movies_4",
    "music_tracker",
    "sales_in_weather",
    "social_media",
    "talkingdata",
)


def test_the_incident_reproduced_inline_is_refused(tmp_path):
    """A clean checkout still reproduces the incident: ten databases, every value ''."""

    payload = {db_id: "" for db_id in INCIDENT_DATABASES}
    health = run_benchmark.read_schema_health(_write_schemas(tmp_path, payload))
    assert health["schemas_ok"] is False
    assert health["schemas_db_count"] == 10
    assert health["schemas_empty_count"] == 10
    message = run_benchmark.schema_refusal_message(
        health, allow_empty_schemas=False, run_dir=tmp_path
    )
    assert message is not None
    assert "10/10" in message
    assert "disney" in message  # a name from the real file


@pytest.mark.skipif(not BROKEN_INCIDENT.is_file(), reason="the 2026-10-29 OOD run is not on this checkout")
def test_the_real_broken_schema_file_is_refused():
    """The exact file that produced the false 5.14 % base-model result."""

    health = run_benchmark.read_schema_health(BROKEN_INCIDENT)
    assert health["schemas_ok"] is False
    assert health["schemas_db_count"] == 10
    assert health["schemas_empty_count"] == 10
    assert health["schemas_chars_total"] == 0
    assert health["schemas_bytes_total"] == 209
    message = run_benchmark.schema_refusal_message(
        health, allow_empty_schemas=False, run_dir=BROKEN_INCIDENT.parent
    )
    assert message is not None
    assert str(BROKEN_INCIDENT.parent) in message
    assert any(name in message for name in INCIDENT_DATABASES)


@pytest.mark.skipif(not HEALTHY_INCIDENT.is_file(), reason="the healthy OOD run is not on this checkout")
def test_the_real_healthy_schema_file_passes():
    health = run_benchmark.read_schema_health(HEALTHY_INCIDENT)
    assert health["schemas_ok"] is True
    assert health["schemas_db_count"] == 10
    assert health["schemas_empty_count"] == 0
    assert health["schemas_bytes_total"] == 9872
    assert health["schemas_chars_total"] == 9176
    assert (
        run_benchmark.schema_refusal_message(
            health, allow_empty_schemas=False, run_dir=HEALTHY_INCIDENT.parent
        )
        is None
    )


# ---------------------------------------------------------------------------
# The refusal and the artefact block
# ---------------------------------------------------------------------------


def test_refusal_names_the_databases_and_the_credential_trap(tmp_path):
    path = _write_schemas(tmp_path, BROKEN)
    health = run_benchmark.read_schema_health(path)
    message = run_benchmark.schema_refusal_message(
        health, allow_empty_schemas=False, run_dir=tmp_path
    )
    assert message is not None
    assert "california_schools" in message
    assert "DB_USER" in message and "DB_PASS" in message
    assert ".env" in message and "readonly_user" in message
    assert "--allow-empty-schemas" in message


def test_no_refusal_when_the_fetch_is_sound_or_explicitly_allowed(tmp_path):
    healthy = run_benchmark.read_schema_health(_write_schemas(tmp_path / "ok", HEALTHY))
    broken = run_benchmark.read_schema_health(_write_schemas(tmp_path / "bad", BROKEN))
    assert run_benchmark.schema_refusal_message(
        healthy, allow_empty_schemas=False, run_dir=tmp_path
    ) is None
    assert run_benchmark.schema_refusal_message(
        broken, allow_empty_schemas=True, run_dir=tmp_path
    ) is None


def test_verify_schemas_records_counts_and_the_escape_hatch(tmp_path):
    _write_schemas(tmp_path, BROKEN)
    block, refusal = run_benchmark.verify_schemas(tmp_path, allow_empty_schemas=False)
    assert refusal is not None
    assert block["schemas_empty_count"] == 1
    assert block["schemas_bytes_total"] > 0
    assert block["schemas_allowed_empty"] is False

    allowed_block, allowed_refusal = run_benchmark.verify_schemas(
        tmp_path, allow_empty_schemas=True
    )
    assert allowed_refusal is None
    assert allowed_block["schemas_empty_count"] == 1  # the evidence is not hidden
    assert allowed_block["schemas_allowed_empty"] is True


# ---------------------------------------------------------------------------
# Pre-flight probe and first_db_id
# ---------------------------------------------------------------------------


def test_first_db_id_reads_the_query_file(tmp_path):
    query = tmp_path / "q.json"
    query.write_text(
        json.dumps([{"question_id": 1, "db_id": "financial", "question": "q"}]), encoding="utf-8"
    )
    assert run_benchmark.first_db_id(query) == "financial"

    query.write_text("[]", encoding="utf-8")
    assert run_benchmark.first_db_id(query) is None
    query.write_text("not json", encoding="utf-8")
    assert run_benchmark.first_db_id(query) is None


class _FakeInspector:
    def __init__(self, tables):
        self._tables = tables

    def get_table_names(self, schema=None):
        return list(self._tables)


class _FakeEngine:
    def __init__(self, tables):
        self._tables = tables
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


def test_probe_reads_one_database_through_the_tool_credentials(monkeypatch):
    seen = {}

    def fake_create_engine(dsn, **kwargs):
        seen["dsn"] = dsn
        return _FakeEngine(["frpm", "satscores"])

    import sqlalchemy

    monkeypatch.setattr(sqlalchemy, "create_engine", fake_create_engine)
    monkeypatch.setattr(sqlalchemy, "inspect", lambda engine: _FakeInspector(engine._tables))
    detail = run_benchmark.probe_database_schema(
        "127.0.0.1:5444", "california_schools", user="readonly_user", password="p@ss"
    )
    assert detail["probed"] is True
    assert detail["tables"] == 2
    assert detail["error"] == ""
    assert seen["dsn"].endswith("@127.0.0.1:5444/california_schools")
    assert seen["dsn"].startswith("postgresql+psycopg://readonly_user:")


def test_probe_failure_is_reported_without_leaking_the_password(monkeypatch):
    import sqlalchemy

    def failing_create_engine(dsn, **kwargs):
        raise RuntimeError(f"password authentication failed: {dsn}")

    monkeypatch.setattr(sqlalchemy, "create_engine", failing_create_engine)
    detail = run_benchmark.probe_database_schema(
        "127.0.0.1:5444", "financial", user="readonly_user", password="s3cret-pw"
    )
    assert detail["probed"] is False
    assert detail["skipped"] is False
    assert "s3cret-pw" not in detail["error"]
    assert "***" in detail["error"]


def test_probe_is_skipped_only_when_it_cannot_try():
    detail = run_benchmark.probe_database_schema(None, None, user="u", password="p")
    assert detail["skipped"] is True
    assert detail["probed"] is False


def test_preflight_refuses_a_failed_probe_unless_allowed():
    failed = {"db_id": "financial", "probed": False, "skipped": False, "tables": None,
              "error": "OperationalError: password authentication failed"}
    message = run_benchmark.preflight_schema_problem(failed, allow_empty_schemas=False)
    assert message is not None and "pre-flight" in message and "--allow-empty-schemas" in message
    assert run_benchmark.preflight_schema_problem(failed, allow_empty_schemas=True) is None
    ok = {"db_id": "financial", "probed": True, "skipped": False, "tables": 3, "error": ""}
    assert run_benchmark.preflight_schema_problem(ok, allow_empty_schemas=False) is None
    skipped = {"db_id": None, "probed": False, "skipped": True, "tables": None, "error": "no db"}
    assert run_benchmark.preflight_schema_problem(skipped, allow_empty_schemas=False) is None


# ---------------------------------------------------------------------------
# End to end through run_predict_only: refuse, record, and the escape hatch
# ---------------------------------------------------------------------------


class _FakeClient:
    def get_usage(self) -> Dict[str, int]:
        return {"calls": 1, "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}


class _FakeHarness:
    """Stands in for ``BenchmarkBIRD``: writes the schema file the harness would."""

    schemas: Dict[str, Any] = dict(HEALTHY)

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.llm_client = _FakeClient()

    async def build(self, tool_class: Any) -> Dict[str, Any]:
        Path(run_benchmark.SCHEMA_FILENAME).write_text(json.dumps(self.schemas), encoding="utf-8")
        return {}

    async def predict(self, tools: Dict[str, Any]) -> Dict[str, str]:
        return {"1": "SELECT 1"}

    def _save_predictions(self, predictions: Dict[str, str], path: str) -> None:
        Path(path).write_text(json.dumps(predictions), encoding="utf-8")


@pytest.fixture()
def run_prepared(tmp_path, monkeypatch):
    """A valid plan + query file, with the network and the harness stubbed out."""

    import benchmarks.bird as bird_module

    monkeypatch.setenv("DB_USER", "readonly_user")
    monkeypatch.setenv("DB_PASS", "secret")
    query = tmp_path / "queries.json"
    query.write_text(
        json.dumps(
            [
                {"question_id": 1, "db_id": "california_schools", "question": "q",
                 "evidence": "", "SQL": "SELECT 1"}
            ]
        ),
        encoding="utf-8",
    )
    plan = run_benchmark.plan_run(
        run_id="schema-guard-run",
        query_file=query,
        answer_file=query,
        db_url="127.0.0.1:5444",
        tool_class=run_benchmark.DEFAULT_TOOL_CLASS,
        llm_base_url="http://127.0.0.1:9/v1",
        llm_model="stub-model",
        runs_dir=tmp_path / "runs",
    )
    monkeypatch.setattr(run_benchmark, "probe_server", lambda *_a, **_k: None)
    monkeypatch.setattr(bird_module, "BenchmarkBIRD", _FakeHarness)
    return plan, query, tmp_path / "runs" / "schema-guard-run"


def _healthy_probe() -> Dict[str, Any]:
    return {"db_id": "california_schools", "probed": True, "skipped": False, "tables": 2, "error": ""}


def test_predict_only_refuses_a_broken_schema_and_writes_the_evidence(
    run_prepared, monkeypatch
):
    plan, query, run_dir = run_prepared
    monkeypatch.setattr(run_benchmark, "probe_database_schema", lambda *_a, **_k: _healthy_probe())
    _FakeHarness.schemas = dict(BROKEN)

    with pytest.raises(run_benchmark.RunRefused) as excinfo:
        run_benchmark.run_predict_only(
            plan=plan, query_file=query, answer_file=query, tool_class=object, client=_FakeClient()
        )
    message = str(excinfo.value)
    assert "schema build failed" in message
    assert "california_schools" in message
    assert ".env" in message

    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert record["status"] == "refused"
    assert record["refusal"] == message
    assert record["schemas_ok"] is False
    assert record["schemas_empty_count"] == 1
    assert record["schemas_bytes_total"] > 0
    assert record["schemas_allowed_empty"] is False
    # the predictions are still on disk: the evidence is kept, the verdict is not
    assert (run_dir / "query_results.json").is_file()


def test_allow_empty_schemas_runs_and_records_the_escape_hatch(run_prepared, monkeypatch):
    plan, query, run_dir = run_prepared
    plan = {**plan, "allow_empty_schemas": True}
    monkeypatch.setattr(run_benchmark, "probe_database_schema", lambda *_a, **_k: _healthy_probe())
    _FakeHarness.schemas = dict(BROKEN)

    summary = run_benchmark.run_predict_only(
        plan=plan, query_file=query, answer_file=query, tool_class=object, client=_FakeClient()
    )
    assert summary["mode"] == "predict-only"
    assert summary["schemas_allowed_empty"] is True
    assert summary["schemas_empty_count"] == 1
    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert record["schemas_allowed_empty"] is True
    assert "status" not in record or record.get("status") != "refused"


def test_predict_only_refuses_before_spending_on_a_failed_probe(run_prepared, monkeypatch):
    plan, query, run_dir = run_prepared
    monkeypatch.setattr(
        run_benchmark,
        "probe_database_schema",
        lambda *_a, **_k: {"db_id": "california_schools", "probed": False, "skipped": False,
                           "tables": None, "error": "OperationalError: password authentication failed"},
    )

    with pytest.raises(run_benchmark.RunRefused, match="pre-flight"):
        run_benchmark.run_predict_only(
            plan=plan, query_file=query, answer_file=query, tool_class=object, client=_FakeClient()
        )
    assert not (run_dir / "run.json").exists(), "a refused pre-flight must not leave a run record"


def test_healthy_run_succeeds_and_records_the_counts(run_prepared, monkeypatch):
    plan, query, run_dir = run_prepared
    monkeypatch.setattr(run_benchmark, "probe_database_schema", lambda *_a, **_k: _healthy_probe())
    _FakeHarness.schemas = dict(HEALTHY)
    summary = run_benchmark.run_predict_only(
        plan=plan, query_file=query, answer_file=query, tool_class=object, client=_FakeClient()
    )
    assert summary["schemas_ok"] is True
    assert summary["schemas_empty_count"] == 0
    assert summary["schemas_db_count"] == 2
    assert summary["schema_probe"]["probed"] is True
    assert (run_dir / "run.json").is_file()


class _FakeScoringHarness(_FakeHarness):
    """The default runner path: ``BenchmarkBIRD.run`` builds, predicts and scores."""

    async def run(self, tool_class: Any) -> Dict[str, Any]:
        await self.build(tool_class)
        return {"overall_accuracy": 0.0, "accuracy_by_difficulty": {}, "total": 1,
                "false_ambiguous": 0, "false_ambiguous_rate": 0.0}


def test_run_harness_refuses_a_broken_schema_too(run_prepared, monkeypatch):
    """The default entrypoint must be guarded as well; the two paths cannot drift."""

    import benchmarks.bird as bird_module

    plan, query, run_dir = run_prepared
    monkeypatch.setattr(bird_module, "BenchmarkBIRD", _FakeScoringHarness)
    monkeypatch.setattr(run_benchmark, "probe_database_schema", lambda *_a, **_k: _healthy_probe())
    _FakeScoringHarness.schemas = dict(BROKEN)

    with pytest.raises(run_benchmark.RunRefused, match="schema build failed"):
        run_benchmark.run_harness(
            plan=plan, query_file=query, answer_file=query, tool_class=object, client=_FakeClient()
        )
    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert record["status"] == "refused"
    assert record["schemas_empty_count"] == 1


def test_run_harness_scores_a_healthy_schema(run_prepared, monkeypatch):
    import benchmarks.bird as bird_module

    plan, query, run_dir = run_prepared
    monkeypatch.setattr(bird_module, "BenchmarkBIRD", _FakeScoringHarness)
    monkeypatch.setattr(run_benchmark, "probe_database_schema", lambda *_a, **_k: _healthy_probe())
    _FakeScoringHarness.schemas = dict(HEALTHY)
    summary = run_benchmark.run_harness(
        plan=plan, query_file=query, answer_file=query, tool_class=object, client=_FakeClient()
    )
    assert summary["schemas_ok"] is True
    assert summary["report"]["total"] == 1
    assert (run_dir / "run.json").is_file()


# ---------------------------------------------------------------------------
# CLI: the flag exists, is off by default, and lands in the plan
# ---------------------------------------------------------------------------


def _argv(tmp_path, *extra: str) -> list:
    answers = REPO_ROOT / "data" / "bird_small.json"
    return [
        "--run-id", "check-run",
        "--query-file", str(answers),
        "--answer-file", str(answers),
        "--db-url", "127.0.0.1:5444",
        "--llm-base-url", "http://127.0.0.1:9/v1",
        "--llm-model", "stub-model",
        "--runs-dir", str(tmp_path / "runs"),
        "--check",
        *extra,
    ]


def test_cli_flag_defaults_to_off_and_is_accepted(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DB_USER", "readonly_user")
    monkeypatch.setenv("DB_PASS", "secret")

    assert run_benchmark.build_parser().parse_args(
        ["--run-id", "x", "--query-file", "q", "--answer-file", "a"]
    ).allow_empty_schemas is False

    assert run_benchmark.main(_argv(tmp_path)) == 0
    default_out = capsys.readouterr().out
    assert "allow_empty_schemas: False" in default_out

    assert run_benchmark.main(_argv(tmp_path, "--allow-empty-schemas")) == 0
    flagged_out = capsys.readouterr().out
    assert "allow_empty_schemas: True" in flagged_out
    assert not (tmp_path / "runs").exists(), "--check creates nothing"
