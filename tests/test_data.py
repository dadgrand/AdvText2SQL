r"""Offline acceptance tests for the LoRA training pipeline (``training/``).

Everything here is offline by construction:

* no test downloads a model, touches a GPU or opens a socket; the build subprocesses run with
  ``HF_HUB_OFFLINE=1`` / ``TRANSFORMERS_OFFLINE=1`` so a tokenizer download would fail rather than
  silently succeed;
* the training tests intercept ``import torch``/``trl``/``transformers``/``peft``/``datasets`` with
  a meta-path blocker, which proves the planner and the module import need none of them;
* teacher distillation is exercised through an injected executor (``ReplayExecutor`` / a lambda),
  never a database.

Run from the repo root::

    python -m pytest tests/test_data.py -q
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TRAINING = REPO / "training"
DATA = REPO / "data"
BUILD = TRAINING / "build_data.py"
TRAIN = TRAINING / "train_qlora.py"
FIXTURES = TRAINING / "fixtures"

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

OFFLINE_ENV = {
    **os.environ,
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_DATASETS_OFFLINE": "1",
    "PYTHONHASHSEED": "0",
}

DEV_FILES = {
    "bird": ("bird_small.json", "bird_large.json"),
    "ambrosia": ("ambrosia_small.json", "ambrosia_large.json"),
}

# Meta-path blocker used by the training tests: any import of the heavy training stack raises.
HEAVY_BLOCKER = """
import importlib
import importlib.abc
import sys

BLOCKED = ["torch", "trl", "transformers", "peft", "datasets", "bitsandbytes"]


class _BlockHeavy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in BLOCKED:
            raise ImportError("blocked heavy dependency: " + fullname)
        return None


sys.meta_path.insert(0, _BlockHeavy())
sys.path.insert(0, {training!r})
import train_qlora

loaded = sorted(name for name in BLOCKED if name in sys.modules)
if loaded:
    raise SystemExit("FAIL heavy modules imported at module load: " + ", ".join(loaded))
print("IMPORT_OK", flush=True)
{body}
"""


# ------------------------------------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------------------------------------
def _python() -> str:
    return sys.executable


def run_build(*args: str, out: str | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    cmd = [_python(), str(BUILD), *args]
    if out is not None:
        cmd += ["--out", out]
    return subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True, timeout=timeout,
                          env=OFFLINE_ENV)


def run_training_blocked(body: str, timeout: int = 120) -> subprocess.CompletedProcess:
    code = HEAVY_BLOCKER.format(training=str(TRAINING), body=body)
    return subprocess.run([_python(), "-c", code], cwd=str(REPO), capture_output=True, text=True,
                          timeout=timeout, env=OFFLINE_ENV)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dev_sets(benchmark: str) -> tuple[set[str], set[str], set[str]]:
    ids, questions, db_ids = set(), set(), set()
    for name in DEV_FILES[benchmark]:
        for item in json.loads((DATA / name).read_text(encoding="utf-8")):
            ids.add(str(item["question_id"]))
            questions.add(item["question"])
            db_ids.add(str(item.get("db_id")))
    return ids, questions, db_ids


@pytest.fixture(scope="session")
def built(tmp_path_factory) -> Path:
    """One shared build with synthesised schemas, reused by the content assertions."""
    out = tmp_path_factory.mktemp("build_out")
    proc = run_build("--self-test", out=str(out))
    assert proc.returncode == 0, proc.stderr
    return out


# ------------------------------------------------------------------------------------------------
# 1. the documented command, offline, under a minute, with the exact count table
# ------------------------------------------------------------------------------------------------
def test_self_test_runs_offline_and_prints_count_table(tmp_path):
    started = time.monotonic()
    proc = run_build("--self-test", out=str(tmp_path / "out"), timeout=60)
    elapsed = time.monotonic() - started

    assert proc.returncode == 0, proc.stderr
    assert elapsed < 60, f"--self-test took {elapsed:.1f}s, the contract is < 60s"
    stdout = proc.stdout
    assert "COUNT TABLE" in stdout
    for token in ("dropped=14", "mutated=1", "usable=342", "train/valid=290/52",
                  "ambrosia built=438 (394/44)", "combined=684"):
        assert token in stdout, f"COUNT TABLE is missing {token!r}"

    out = tmp_path / "out"
    for name in ("sft_bird_train.jsonl", "sft_bird_valid.jsonl", "sft_ambrosia_train.jsonl",
                 "sft_ambrosia_valid.jsonl", "sft_all_train.jsonl", "stats.json", "dropped.json"):
        assert (out / name).exists(), f"missing artifact {name}"

    stats = json.loads((out / "stats.json").read_text(encoding="utf-8"))
    assert stats["bird_dropped"] == 14, "the 14 unparseable targets stay a hard abort condition"
    assert stats["bird_mutated"] == 1, "question_id 2 parses but its semantics were changed"
    assert stats["bird_mutated_ids"] == ["2"]
    assert stats["bird_parseable"] == 343
    assert stats["bird_usable"] == 342
    assert (stats["bird_train"], stats["bird_valid"]) == (290, 52)
    assert (stats["ambrosia_built"], stats["ambrosia_train"], stats["ambrosia_valid"]) == (438, 394, 44)
    assert stats["combined_train"] == 684
    assert stats["token_basis"].startswith("chars/3.6"), "self-test must not load a tokenizer"

    dropped = json.loads((out / "dropped.json").read_text(encoding="utf-8"))
    by_reason = {}
    for row in dropped:
        by_reason.setdefault(row["reason"], []).append(str(row["question_id"]))
    assert len(by_reason["unparseable after repo sqlite_to_postgres"]) == 14
    assert by_reason["mutated-but-parseable"] == ["2"], (
        "the silently altered target (quoted identifier \"Charter School (Y/N)\") must be recorded "
        "as dropped, not trained on"
    )
    mutated = next(row for row in dropped if row["reason"] == "mutated-but-parseable")
    assert "NULLIF(N,0)" in mutated["mutated_target"]
    assert "Charter School (Y/N)" in mutated["plain_target"]

    # the poisoned target never reaches a training file
    for name in ("sft_bird_train.jsonl", "sft_bird_valid.jsonl", "sft_all_train.jsonl"):
        assert all(row["question_id"] != "2" for row in read_jsonl(out / name))


# ------------------------------------------------------------------------------------------------
# 2. determinism
# ------------------------------------------------------------------------------------------------
def test_build_is_byte_deterministic(tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    for target in (first, second):
        proc = run_build("--self-test", out=str(target))
        assert proc.returncode == 0, proc.stderr
    names = ["sft_bird_train.jsonl", "sft_bird_valid.jsonl", "sft_ambrosia_train.jsonl",
             "sft_ambrosia_valid.jsonl", "sft_all_train.jsonl", "dropped.json", "stats.json"]
    digests = {}
    for name in names:
        left, right = sha256(first / name), sha256(second / name)
        assert left == right, f"{name} is not byte-identical across two seeded runs"
        digests[name] = left
    assert len(set(digests.values())) == len(digests), "distinct artifacts must not collide"


# ------------------------------------------------------------------------------------------------
# 3. ambig_queries parsing and the 146 real schemas
# ------------------------------------------------------------------------------------------------
def test_ambig_queries_literal_eval_fallback_chain():
    from training.build_data import parse_ambig_queries

    rows = json.loads((DATA / "ambrosia_train.json").read_text(encoding="utf-8"))
    positives = [row for row in rows if (row.get("SQL") or "").strip() == "ambiguous"]
    assert len(positives) == 146

    for row in positives:
        parsed, basis = parse_ambig_queries(row["ambig_queries"])
        assert parsed, f"row {row['question_id']} produced no SQL"
        assert basis.startswith("literal_eval"), f"row {row['question_id']} used {basis}"

    # the other rungs of the chain
    assert parse_ambig_queries(["SELECT 1"])[1] == "already-list"
    assert parse_ambig_queries(json.dumps(["SELECT 1"]))[0] == ["SELECT 1"]
    assert parse_ambig_queries(json.dumps(["SELECT 1"]))[1] == "literal_eval"
    double_encoded = json.dumps(json.dumps(["SELECT 2"]))
    parsed, basis = parse_ambig_queries(double_encoded)
    assert parsed == ["SELECT 2"] and basis.startswith("literal_eval->"), basis
    assert parse_ambig_queries("not a list at all") == ([], "none")
    assert parse_ambig_queries(None) == ([], "empty")
    assert parse_ambig_queries("") == ([], "empty")


def test_every_ambiguous_row_gets_a_real_schema(built: Path):
    ambrosia_rows = (read_jsonl(built / "sft_ambrosia_train.jsonl")
                     + read_jsonl(built / "sft_ambrosia_valid.jsonl"))
    source = json.loads((DATA / "ambrosia_train.json").read_text(encoding="utf-8"))
    ambiguous_ids = {str(row["question_id"]) for row in source
                     if (row.get("SQL") or "").strip() == "ambiguous"}
    assert len(ambiguous_ids) == 146

    ambiguous_rows = [row for row in ambrosia_rows if row["question_id"] in ambiguous_ids]
    assert len(ambiguous_rows) == 146, "all 146 ambiguous rows must reach the output"
    for row in ambiguous_rows:
        assert "TABLE unknown" not in row["prompt"], f"row {row['question_id']} has the stub schema"
        assert "TABLE " in row["prompt"]

    for name in ("sft_bird_train.jsonl", "sft_bird_valid.jsonl", "sft_ambrosia_train.jsonl",
                 "sft_ambrosia_valid.jsonl", "sft_all_train.jsonl"):
        for row in read_jsonl(built / name):
            assert "TABLE unknown" not in row["prompt"], f"{name}: stub schema leaked into a prompt"

    stats = json.loads((built / "stats.json").read_text(encoding="utf-8"))
    assert stats["ambrosia_pos"] == 146
    assert stats["ambrosia_unknown_schema_rows"] == 0


# ------------------------------------------------------------------------------------------------
# 4. missing schemas: refuse by default, allow with the flag
# ------------------------------------------------------------------------------------------------
def test_build_refuses_without_schemas(tmp_path):
    proc = run_build("--no-tokenizer", out=str(tmp_path / "no_schemas"))
    assert proc.returncode != 0, "a build without a schema corpus must not succeed"
    assert "--schemas is required" in proc.stderr
    assert "--allow-synth-schemas" in proc.stderr


def test_build_refuses_partial_schema_corpus(tmp_path):
    partial = tmp_path / "schemas.json"
    partial.write_text(json.dumps({"california_schools": "TABLE schools\n  - School (text)"}),
                       encoding="utf-8")
    proc = run_build("--no-tokenizer", "--schemas", str(partial), out=str(tmp_path / "partial"))
    assert proc.returncode != 0
    assert "no schema for" in proc.stderr
    assert "california_schools" not in proc.stderr  # that one is present, the others are not


def test_build_stubs_missing_schemas_with_flag(tmp_path):
    partial = tmp_path / "schemas.json"
    partial.write_text(json.dumps({"california_schools": "TABLE schools\n  - School (text)"}),
                       encoding="utf-8")
    out = tmp_path / "stubbed"
    proc = run_build("--no-tokenizer", "--schemas", str(partial), "--allow-missing-schemas",
                     out=str(out))
    assert proc.returncode == 0, proc.stderr
    stats = json.loads((out / "stats.json").read_text(encoding="utf-8"))
    assert stats["schema_basis"] == "file+stub"
    stubbed = [row for row in read_jsonl(out / "sft_bird_train.jsonl")
               if "STUB" in row["prompt"]]
    assert stubbed, "--allow-missing-schemas must emit explicitly labelled stub schemas"
    assert all("TABLE unknown  -- STUB" in row["prompt"] for row in stubbed)
    # the one db_id that did have a schema keeps the real text
    assert any("TABLE schools" in row["prompt"] for row in read_jsonl(out / "sft_bird_train.jsonl"))


# ------------------------------------------------------------------------------------------------
# 5. distillation
# ------------------------------------------------------------------------------------------------


def test_teacher_filter_keeps_only_parseable_exec_matching():
    from training.teacher_filter import (DROP_EMPTY, DROP_EXEC_ERROR, DROP_EXEC_MISMATCH,
                                         DROP_UNPARSEABLE, KEEP_EXEC_MATCH, KEEP_PARSE_ONLY,
                                         filter_predictions)

    gold = {
        "1": "SELECT COUNT(*) FROM cards",
        "2": "SELECT id FROM cards",
        "3": "SELECT name FROM cards",
        "4": "SELECT artist FROM cards",
        "5": "SELECT setCode FROM sets",
        "6": "SELECT 1",
    }
    predictions = {
        "1": "SELECT COUNT(*) AS n FROM cards",       # keep: same result
        "2": "SELECT id FROM cards WHERE )",          # drop: not PostgreSQL
        "3": "SELECT DISTINCT name FROM cards",       # drop: different result
        "4": "   ",                                   # drop: empty
        "6": "SELECT 1",                              # drop: execution raises
        # "5" was never answered -> not_attempted
    }
    recorded = {
        ("1", "SELECT COUNT(*) AS n FROM cards"): [[7]],
        ("1", "SELECT COUNT(*) FROM cards"): [[7]],
        ("3", "SELECT DISTINCT name FROM cards"): [[4]],
        ("3", "SELECT name FROM cards"): [[5]],
    }

    def execute(question_id, sql):
        key = (question_id, sql.strip())
        if key not in recorded:
            raise RuntimeError("division by zero")
        return recorded[key]

    result = filter_predictions(predictions, gold, execute=execute)
    assert result.exec_checked is True
    assert [row["question_id"] for row in result.kept] == ["1"]
    assert result.kept[0]["reason"] == KEEP_EXEC_MATCH
    assert result.by_reason == {
        KEEP_EXEC_MATCH: 1,
        DROP_UNPARSEABLE: 1,
        DROP_EXEC_MISMATCH: 1,
        DROP_EMPTY: 1,
        DROP_EXEC_ERROR: 1,
    }
    reasons = {row["question_id"]: row["reason"] for row in result.dropped}
    assert reasons == {"2": DROP_UNPARSEABLE, "3": DROP_EXEC_MISMATCH, "4": DROP_EMPTY,
                       "6": DROP_EXEC_ERROR}
    assert result.attempted == 5 and result.not_attempted == 1

    # No executor injected -> parse-only mode, loudly reported, nothing silently "verified".
    parse_only = filter_predictions(predictions, gold)
    assert parse_only.exec_checked is False
    assert parse_only.as_dict()["mode"] == "parse-only"
    assert {row["question_id"] for row in parse_only.kept} == {"1", "3", "6"}
    assert parse_only.by_reason[KEEP_PARSE_ONLY] == 3

    # Verified distillation demanded but no executor -> keep nothing, say why.
    required = filter_predictions(predictions, gold, require_exec=True)
    assert required.kept == []
    assert required.by_reason == {DROP_EXEC_ERROR: 5}
    assert all("require_exec" in row["detail"] for row in required.dropped)


def test_results_match_is_order_and_float_tolerant():
    from training.teacher_filter import results_match

    assert results_match([[1, "a"], [2, "b"]], [[2.0000001, "b"], [1, "a"]])
    assert results_match([[1]], [[1]])
    assert not results_match([[1]], [[2]])
    assert not results_match([[1]], [[1, 2]])
    assert results_match(None, None)
    assert not results_match(None, [[None]])


def test_teacher_cli_fixture_keep_drop_counts(tmp_path):
    out = tmp_path / "distilled"
    proc = run_build("--self-test",
                     "--teacher", str(FIXTURES / "teacher_predictions.json"),
                     "--teacher-results", str(FIXTURES / "teacher_results.json"),
                     out=str(out))
    assert proc.returncode == 0, proc.stderr
    report = json.loads((out / "teacher_report.json").read_text(encoding="utf-8"))
    assert report["exec_checked"] is True
    assert report["attempted"] == 5
    assert report["kept"] == 2
    assert report["dropped"] == 3
    assert report["by_reason"] == {
        "keep-exec-match": 2, "unparseable-postgres": 1, "exec-mismatch": 1, "exec-error": 1,
    }
    assert report["not_attempted"] == 337  # 342 usable BIRD items - the 5 the teacher answered

    rows = read_jsonl(out / "sft_bird_train.jsonl")
    teacher_rows = [row for row in rows if row["target_source"] == "teacher"]
    assert {row["question_id"] for row in teacher_rows} == {"64", "400"}
    assert any("AS n" in row["completion"] for row in teacher_rows), "teacher SQL must be the target"
    # the valid split keeps gold targets: it is the track BIRD EX is measured on
    assert all(row["target_source"] == "gold" for row in read_jsonl(out / "sft_bird_valid.jsonl"))


def test_teacher_mode_without_results_is_parse_only(tmp_path):
    out = tmp_path / "parse_only"
    proc = run_build("--self-test", "--teacher", str(FIXTURES / "teacher_predictions.json"),
                     out=str(out))
    assert proc.returncode == 0, proc.stderr
    report = json.loads((out / "teacher_report.json").read_text(encoding="utf-8"))
    assert report["exec_checked"] is False
    assert report["by_reason"] == {"keep-parse-only": 4, "unparseable-postgres": 1}
    assert "parse-only" in proc.stdout


def test_teacher_strict_mode_keeps_only_verified_rows(tmp_path):
    out = tmp_path / "strict"
    proc = run_build("--allow-synth-schemas",
                     "--teacher", str(FIXTURES / "teacher_predictions.json"),
                     "--teacher-results", str(FIXTURES / "teacher_results.json"),
                     "--teacher-mode", "strict", out=str(out))
    assert proc.returncode == 0, proc.stderr
    stats = json.loads((out / "stats.json").read_text(encoding="utf-8"))
    assert stats["teacher"]["mode_flag"] == "strict"
    assert stats["bird_train"] == 2, "strict distillation keeps only teacher-verified BIRD rows"
    assert stats["combined_train"] == 2 + stats["ambrosia_train"]
    assert all(row["target_source"] == "teacher" for row in read_jsonl(out / "sft_bird_train.jsonl"))


def test_feature_prompt_block_is_inserted_verbatim(tmp_path):
    block = ("DIALECT NOTES: identifiers here are mixed-case; quote them with double quotes.\n"
             "NUMERIC TRAPS: guard every column denominator with NULLIF(x, 0).")
    feature = tmp_path / "feature_block.txt"
    feature.write_bytes((block.replace("\n", "\r\n") + "\r\n").encode("utf-8"))

    out = tmp_path / "featured"
    proc = run_build("--self-test", "--feature-prompt", str(feature), out=str(out))
    assert proc.returncode == 0, proc.stderr

    expected = block  # CRLF normalised to LF, trailing newline stripped, otherwise byte-identical
    for name in ("sft_bird_train.jsonl", "sft_bird_valid.jsonl"):
        rows = read_jsonl(out / name)
        assert rows
        for row in rows:
            assert expected in row["prompt"], f"{name}: feature block is not verbatim"
            assert expected.replace("\n", "\r\n") not in row["prompt"]
    for row in read_jsonl(out / "sft_ambrosia_train.jsonl"):
        assert "DIALECT NOTES" not in row["prompt"], "the feature block is a SQL-task surface"

    stats = json.loads((out / "stats.json").read_text(encoding="utf-8"))
    assert stats["feature_prompt"]["file"] == "feature_block.txt"
    assert stats["feature_prompt"]["chars"] == len(expected)
    assert stats["feature_prompt"]["sha256_16"] == hashlib.sha256(expected.encode("utf-8")).hexdigest()[:16]


def test_feature_prompt_rejects_missing_file(tmp_path):
    proc = run_build("--self-test", "--feature-prompt", str(tmp_path / "nope.txt"),
                     out=str(tmp_path / "out"))
    assert proc.returncode != 0
    assert "feature-prompt file not found" in proc.stderr


# ------------------------------------------------------------------------------------------------
# 6. train_qlora: lazy imports, --plan, provenance
# ------------------------------------------------------------------------------------------------
def test_train_qlora_imports_without_heavy_dependencies():
    proc = run_training_blocked("")
    assert proc.returncode == 0, proc.stderr
    assert "IMPORT_OK" in proc.stdout


def test_train_qlora_plan_prints_config_and_writes_provenance(built, tmp_path):
    out = tmp_path / "plan"
    body = ("raise SystemExit(train_qlora.main(['--plan', '--data', %r, '--out', %r]))"
            % (str(built), str(out)))
    proc = run_training_blocked(body)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    stdout = proc.stdout
    assert "RESOLVED CONFIG" in stdout
    assert '"r": 16' in stdout and '"alpha": 32' in stdout
    assert '"max_seq_len": 3072' in stdout and '"effective_batch": 16' in stdout
    assert '"train_rows": 684' in stdout
    assert "decision=blocked" in stdout, "TRL is not installed here: the probe must say so"
    assert "[gpu] not probed" in stdout

    provenance = json.loads((out / "provenance.json").read_text(encoding="utf-8"))
    assert provenance["model"]["id"] == "Qwen/Qwen2.5-Coder-7B-Instruct"
    assert provenance["seeds"] == {"data": 42, "data_seed": 42, "train": 42}
    assert provenance["pins"]["sqlglot"]["installed"] == "30.23.0"
    assert provenance["pins"]["sqlglot"]["match"] is True
    assert provenance["pins"]["trl"] == {
        "required": ">=0.16,<0.21", "installed": None, "decision": "blocked",
        "length_field": None, "eval_field": None, "completion_only_loss": None,
    }
    assert len(provenance["data"]["data_hash"]) == 64
    assert provenance["data"]["files"]["sft_all_train.jsonl"]["rows"] == 684
    assert provenance["config"]["completion_only_loss"] is True
    assert provenance["plan"]["trl_decision"] == "blocked"
    assert provenance["plan"]["training_would_be"].startswith("refused: "), (
        "the self-test dataset has synthesised schemas; the planner must say training would be refused"
    )

    git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO), capture_output=True, text=True)
    if git.returncode == 0:
        assert provenance["git"]["commit"] == git.stdout.strip()
        assert provenance["git"]["available"] is True


def test_train_refuses_smoke_data_before_touching_the_gpu(built, tmp_path):
    cmd = [_python(), str(TRAIN), "--data", str(built), "--out", str(tmp_path / "run")]
    proc = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True, timeout=120,
                          env=OFFLINE_ENV)
    assert proc.returncode != 0
    assert "schema_basis" in proc.stderr
    assert "--allow-smoke-data" in proc.stderr
    assert "CUDA" not in proc.stderr, "the data guard must fire before the GPU check"


# ------------------------------------------------------------------------------------------------
# 7. no evaluation-set question reaches training
# ------------------------------------------------------------------------------------------------
def test_no_dev_question_ids_in_training_rows(built):
    bird_ids, bird_questions, _ = dev_sets("bird")
    amb_ids, amb_questions, amb_db_ids = dev_sets("ambrosia")

    bird_rows = read_jsonl(built / "sft_bird_train.jsonl") + read_jsonl(built / "sft_bird_valid.jsonl")
    amb_rows = (read_jsonl(built / "sft_ambrosia_train.jsonl")
                + read_jsonl(built / "sft_ambrosia_valid.jsonl"))

    assert {row["question_id"] for row in bird_rows} & bird_ids == set()
    assert {row["question_id"] for row in amb_rows} & amb_ids == set()
    assert {row["db_id"] for row in amb_rows} & amb_db_ids == set()

    for row in bird_rows:
        assert not any(question in row["prompt"] for question in list(bird_questions)[:20])
    stats = json.loads((built / "stats.json").read_text(encoding="utf-8"))
    assert stats["leakage"]["status"] == "clean"
    assert stats["leakage"]["bird"]["id_overlap"] == []
    assert stats["leakage"]["ambrosia"]["db_id_overlap"] == []

def _fake_trl(root: Path, fields: list[str]) -> Path:
    """Create a minimal ``trl`` package whose ``SFTConfig`` declares exactly ``fields``."""
    package = root / "fake_site" / "trl"
    package.mkdir(parents=True, exist_ok=True)
    declarations = "\n".join(f"    {name}: int = 0" for name in fields)
    (package / "__init__.py").write_text(
        "from dataclasses import dataclass\n"
        '__version__ = "0.16.1"\n\n\n'
        "@dataclass\n"
        "class SFTConfig:\n"
        f"{declarations}\n",
        encoding="utf-8",
    )
    return root / "fake_site"


def _probe_trl_with(tmp_path: Path, fields: list[str] | None) -> dict:
    paths = []
    if fields is not None:
        paths.append(str(_fake_trl(tmp_path, fields)))
    paths.append(str(TRAINING))
    code = (
        "import json, sys\n"
        f"for path in {paths!r}:\n"
        "    sys.path.insert(0, path)\n"
        "import train_qlora\n"
        "print(json.dumps(train_qlora.probe_trl()))\n"
    )
    proc = subprocess.run([_python(), "-c", code], cwd=str(REPO), capture_output=True, text=True,
                          timeout=60, env=OFFLINE_ENV)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_trl_compatibility_probe_decisions(tmp_path):
    compatible = _probe_trl_with(
        tmp_path, ["max_length", "eval_strategy", "completion_only_loss", "output_dir"]
    )
    assert compatible["decision"] == "ok"
    assert compatible["installed"] == "0.16.1"
    assert compatible["length_field"] == "max_length"
    assert compatible["eval_field"] == "eval_strategy"
    assert compatible["completion_only_loss"] is True

    legacy_fields = _probe_trl_with(
        tmp_path / "legacy", ["max_seq_length", "evaluation_strategy", "output_dir"]
    )
    assert legacy_fields["decision"] == "blocked"
    assert legacy_fields["length_field"] == "max_seq_length"
    assert legacy_fields["eval_field"] == "evaluation_strategy"
    assert "completion_only_loss" in legacy_fields["detail"]
    assert "trl>=0.16,<0.21" in legacy_fields["action"]

    missing = _probe_trl_with(tmp_path / "missing", None)
    assert missing["decision"] == "blocked"
    assert "No module named" in missing["detail"]
