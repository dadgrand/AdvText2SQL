"""Offline contract tests for ``dev_tools/matrix.py`` (the feature × model assembler).

Everything here is synthetic or read from the repo's own ledgers: no database, no model
server, no socket. The tests pin the properties the task requires:

* measured arms come from ledgers on disk and are cross-checked against the independent
  2×2 ledger, with disagreements surfaced as findings;
* arms with no artifact are *reported* (``blocked`` / ``not measured``) and never executed;
* the feature-block file contract is validated (fatal problems vs advisory warnings);
* the assembler imports nothing that could start a server, and runs with sockets poisoned.
"""

from __future__ import annotations

import importlib
import json
import socket
import sys
from pathlib import Path

import pytest

from dev_tools import matrix


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_ledger(
    path: Path,
    scores: dict,
    *,
    per_difficulty: dict | None = None,
    soft_f1_mean: float | None = 0.5,
    n_pred_unexecutable: int = 0,
) -> Path:
    """A minimal ledger in the shape ``dev_tools.fixed_eval`` writes."""

    items = [{"question_id": qid, "score": score} for qid, score in scores.items()]
    correct = sum(scores.values())
    n = len(scores)
    payload = {
        "tool": "dev_tools.fixed_eval",
        "mode": "local",
        "items": items,
        "per_question": [
            {"question_id": qid, "score": score, "status": "scored"} for qid, score in scores.items()
        ],
        "ex_all": {"correct": correct, "n": n, "value": correct / n, "percent": 100.0 * correct / n},
        "ex_gold_executable": {"correct": correct, "n": n, "value": correct / n,
                               "percent": 100.0 * correct / n},
        "per_difficulty": per_difficulty or {},
        "soft_f1_mean": soft_f1_mean,
        "n_pred_unexecutable": n_pred_unexecutable,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _runs_dir(tmp_path: Path) -> Path:
    return tmp_path / "runs"


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------


def test_default_manifest_has_four_measured_and_the_blocked_cells(tmp_path):
    arms = matrix.default_arms(_runs_dir(tmp_path))
    ids = [arm.id for arm in arms]
    assert len(ids) == len(set(ids)), "arm ids must be unique"

    measured = [arm for arm in arms if arm.status == "measured"]
    assert [arm.id for arm in measured] == ["base_noev", "base_ev", "adapter_noev", "adapter_ev"]
    assert all(arm.feature_prompt is None for arm in measured)

    features = [arm for arm in arms if arm.feature_prompt and arm.feature_prompt.endswith(".txt")]
    assert len(features) == 4
    assert {(arm.model, arm.use_evidence) for arm in features} == {
        ("base", False), ("base", True), ("adapter", False), ("adapter", True)
    }
    assert all(arm.status == "blocked" and arm.blocked_reason == matrix.BLOCKED_REASON for arm in features)

    api = [arm for arm in arms if arm.model == "gpt-5.4-mini"]
    assert len(api) == 2 and all(arm.status == "blocked" for arm in api)


# ---------------------------------------------------------------------------
# Ledger loading
# ---------------------------------------------------------------------------


def test_load_ledger_normalises_blocks_items_and_difficulty(tmp_path):
    ledger = _write_ledger(
        tmp_path / "ledger.json",
        {"1": 1, "2": 0, "3": 1},
        per_difficulty={
            "simple": {"correct": 2, "n": 2, "percent": 100.0},
            "challenging": {"correct": 0, "n": 1, "percent": 0.0},
        },
        soft_f1_mean=0.75,
    )
    data = matrix.load_ledger(ledger)
    assert data["n"] == 3
    assert data["ex_all"]["correct"] == 2
    assert data["ex_gold_executable"]["correct"] == 2
    assert data["per_difficulty"]["simple"]["percent"] == 100.0
    assert data["soft_f1_mean"] == 0.75
    assert data["items"] == [
        {"question_id": "1", "score": 1},
        {"question_id": "2", "score": 0},
        {"question_id": "3", "score": 1},
    ]


def test_load_ledger_accepts_the_frozen_percentage_ledger(tmp_path):
    """The frozen GPT file stores percentages and an ``ex_exec`` key, not our blocks."""

    frozen = tmp_path / "pred_scores.json"
    frozen.write_text(json.dumps({
        "ex_all": 42.32,
        "ex_exec": 48.80,
        "by_difficulty": {"simple": 56.04, "moderate": 40.45, "challenging": 24.59},
        "items": [{"question_id": str(i), "score": 1 if i < 102 else 0} for i in range(241)],
    }), encoding="utf-8")
    data = matrix.load_ledger(frozen, executable_n=209)
    assert data["n"] == 241
    assert data["ex_all"]["correct"] == 102
    assert data["ex_gold_executable"]["n"] == 209
    assert data["ex_gold_executable"]["correct"] == 102  # 48.80 % of 209
    assert data["per_difficulty"]["simple"]["percent"] == 56.04


# ---------------------------------------------------------------------------
# Assembly: measured / blocked / not measured
# ---------------------------------------------------------------------------


def test_assemble_reports_measured_blocked_and_not_measured(tmp_path):
    runs = _runs_dir(tmp_path)
    scores = runs / "scores"
    _write_ledger(scores / "e1-bird-large.json", {"1": 1, "2": 0})
    _write_ledger(scores / "e3-bird-large.json", {"1": 1, "2": 1})

    arms = [
        matrix.Arm(id="base_noev", model="base", use_evidence=False, feature_prompt=None,
                   status="measured", ledger=str(scores / "e1-bird-large.json")),
        matrix.Arm(id="adapter_noev", model="adapter", use_evidence=False, feature_prompt=None,
                   status="measured", ledger=str(scores / "e3-bird-large.json")),
        matrix.Arm(id="base_noev_features", model="base", use_evidence=False,
                   feature_prompt=matrix.DEFAULT_FEATURE_FILE, status="blocked",
                   blocked_reason=matrix.BLOCKED_REASON, blockers=["team feature file"]),
        matrix.Arm(id="ghost", model="base", use_evidence=True, feature_prompt=None,
                   status="measured", ledger=str(scores / "absent.json")),
    ]
    rows = matrix.assemble(arms, feature_file="does/not/exist.txt", runs_dir=runs)
    by_id = {row["id"]: row for row in rows}

    assert by_id["base_noev"]["status"] == "measured"
    assert by_id["base_noev"]["ex_all"]["correct"] == 1
    assert by_id["adapter_noev"]["ex_all"]["correct"] == 2

    blocked = by_id["base_noev_features"]
    assert blocked["status"] == "blocked"
    assert blocked["blocked_reason"] == matrix.BLOCKED_REASON
    assert any("feature file" in blocker for blocker in blocked["blockers"])
    assert "--feature-prompt" in blocked["command"]

    assert by_id["ghost"]["status"] == "not measured"
    assert "absent.json" in by_id["ghost"]["blocked_reason"]


def test_arm_command_names_the_feature_file_and_the_evidence_flag():
    arm = matrix.Arm(id="adapter_ev_features", model="adapter", use_evidence=True,
                     feature_prompt="training/feature_block.txt", status="blocked")
    command = matrix.arm_command(arm)
    assert "--feature-prompt training/feature_block.txt" in command
    assert "--use-evidence" in command
    assert "--adapter runs/e3-r16/adapter" in command  # serving hint for the right model


# ---------------------------------------------------------------------------
# Caller-supplied arms
# ---------------------------------------------------------------------------


def test_load_arms_reads_a_json_list_and_validates_it(tmp_path):
    arms_file = tmp_path / "arms.json"
    arms_file.write_text(json.dumps([
        {"id": "base_noev", "model": "base", "use_evidence": False,
         "ledger": "runs/t2/scores/e1-bird-large.json"},
        {"id": "future_adapter", "model": "adapter", "use_evidence": True,
         "feature_prompt": "training/feature_block.txt", "status": "blocked",
         "blocked_reason": matrix.BLOCKED_REASON, "blockers": ["GPU window"]},
    ]), encoding="utf-8")
    arms = matrix.load_arms(arms_file)
    assert [arm.id for arm in arms] == ["base_noev", "future_adapter"]
    assert arms[1].use_evidence is True
    assert arms[1].status == "blocked"

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"id": "x", "model": "base"}]), encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        matrix.load_arms(bad)
    assert "use_evidence" in str(excinfo.value)

    duplicate = tmp_path / "dup.json"
    duplicate.write_text(json.dumps([
        {"id": "x", "model": "base", "use_evidence": False},
        {"id": "x", "model": "base", "use_evidence": True},
    ]), encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        matrix.load_arms(duplicate)
    assert "unique" in str(excinfo.value)


def test_cli_accepts_a_caller_supplied_arm_list(tmp_path, capsys):
    runs = _runs_dir(tmp_path)
    _write_ledger(runs / "scores" / "e1-bird-large.json", {"1": 1, "2": 0})
    arms_file = tmp_path / "arms.json"
    arms_file.write_text(json.dumps([
        {"id": "only_arm", "model": "base", "use_evidence": False,
         "ledger": str(runs / "scores" / "e1-bird-large.json")},
    ]), encoding="utf-8")
    out = tmp_path / "matrix.json"

    code = matrix.main(["--arms", str(arms_file), "--runs-dir", str(runs), "--json", str(out)])
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert [arm["id"] for arm in payload["arms"]] == ["only_arm"]
    assert payload["arms"][0]["status"] == "measured"
    assert "only_arm" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Feature-block contract
# ---------------------------------------------------------------------------


def test_feature_file_report_distinguishes_fatal_from_advisory(tmp_path):
    missing = matrix.feature_file_report(str(tmp_path / "nope.txt"))
    assert missing["present"] is False and missing["ok"] is False
    assert "file not found" in missing["problems"]

    empty = tmp_path / "empty.txt"
    empty.write_text("   \n\n", encoding="utf-8")
    report = matrix.feature_file_report(str(empty))
    assert report["ok"] is False and any("empty" in problem for problem in report["problems"])

    huge = tmp_path / "huge.txt"
    huge.write_text("x" * (matrix.MAX_FEATURE_CHARS + 1), encoding="utf-8")
    report = matrix.feature_file_report(str(huge))
    assert report["ok"] is False and any("cap" in problem for problem in report["problems"])

    large = tmp_path / "large.txt"
    large.write_text("y" * (matrix.WARN_FEATURE_CHARS + 10), encoding="utf-8")
    report = matrix.feature_file_report(str(large))
    assert report["ok"] is True, "a large-but-legal block is a warning, not a refusal"
    assert report["warnings"] and not report["problems"]

    good = tmp_path / "good.txt"
    good.write_text("domain: school funding", encoding="utf-8")
    report = matrix.feature_file_report(str(good))
    assert report["ok"] is True and report["present"] is True
    assert len(report["sha256_16"]) == 16 and report["chars"] == 22


# ---------------------------------------------------------------------------
# Paired block
# ---------------------------------------------------------------------------


def test_paired_block_computes_b_c_and_delta_against_the_reference(tmp_path):
    runs = _runs_dir(tmp_path)
    scores = runs / "scores"
    # reference: right on q1 and q2; current: right on q1 and q3
    _write_ledger(scores / "ref.json", {"q1": 1, "q2": 1, "q3": 0, "q4": 0})
    _write_ledger(scores / "cur.json", {"q1": 1, "q2": 0, "q3": 1, "q4": 0})
    rows = matrix.assemble(
        [
            matrix.Arm(id="cur", model="base", use_evidence=False, feature_prompt=None,
                       status="measured", ledger=str(scores / "cur.json")),
            matrix.Arm(id="ref", model="adapter", use_evidence=False, feature_prompt=None,
                       status="measured", ledger=str(scores / "ref.json")),
        ],
        feature_file=None,
        runs_dir=runs,
    )
    paired = matrix.paired_block(rows, "ref")
    assert paired is not None and paired["reference"] == "ref"
    comparison = paired["comparisons"]["cur"]
    assert (comparison["b"], comparison["c"]) == (1, 1)
    assert comparison["mcnemar_exact_p"] == 1.0
    assert comparison["delta_ex"] == 0.0
    low, high = comparison["bootstrap95"]
    assert low <= 0.0 <= high, "the resampled mean of [0, -1, +1, 0] must straddle zero"


def test_paired_block_is_none_when_the_reference_is_missing(tmp_path):
    rows = matrix.assemble(
        [matrix.Arm(id="cur", model="base", use_evidence=False, feature_prompt=None,
                    status="measured", ledger=str(tmp_path / "absent.json"))],
        feature_file=None,
        runs_dir=_runs_dir(tmp_path),
    )
    assert matrix.paired_block(rows, "not-there") is None


# ---------------------------------------------------------------------------
# Cross-check against the independent 2x2 ledger
# ---------------------------------------------------------------------------


def _assembled_rows(tmp_path):
    scores = _runs_dir(tmp_path) / "scores"
    _write_ledger(scores / "e1-bird-large.json", {"1": 1, "2": 0},
                  per_difficulty={"simple": {"correct": 1, "n": 2, "percent": 50.0}})
    arms = [
        matrix.Arm(id="base_noev", model="base", use_evidence=False, feature_prompt=None,
                   status="measured", ledger=str(scores / "e1-bird-large.json")),
    ]
    return matrix.assemble(arms, feature_file=None, runs_dir=_runs_dir(tmp_path))


def _reference_cells(tmp_path, ex_correct: int) -> Path:
    path = tmp_path / "2x2-analysis.json"
    path.write_text(json.dumps({"cells": {"A": {
        "ex_all": {"correct": 1, "n": 2},
        "ex_gold_executable": {"correct": 1, "n": 2},
        "per_difficulty": {"simple": {"correct": 1, "n": 2, "percent": 50.0}},
    }}}), encoding="utf-8")
    return path


def test_cross_check_is_silent_when_the_cells_agree(tmp_path):
    rows = _assembled_rows(tmp_path)
    assert matrix.cross_check(rows, _reference_cells(tmp_path, 1)) == []


def test_cross_check_reports_a_discrepancy_as_a_finding(tmp_path):
    rows = _assembled_rows(tmp_path)
    reference = tmp_path / "2x2-analysis.json"
    reference.write_text(json.dumps({"cells": {"A": {
        "ex_all": {"correct": 99, "n": 2},
        "ex_gold_executable": {"correct": 1, "n": 2},
        "per_difficulty": {"simple": {"correct": 1, "n": 2, "percent": 50.0}},
    }}}), encoding="utf-8")
    findings = matrix.cross_check(rows, reference)
    assert findings and findings[0]["kind"] == "discrepancy"
    assert findings[0]["arm"] == "base_noev" and findings[0]["assembled"] == 1
    assert findings[0]["t2_reference"] == 99


def test_cross_check_reports_when_the_reference_ledger_is_absent(tmp_path):
    rows = _assembled_rows(tmp_path)
    findings = matrix.cross_check(rows, tmp_path / "missing.json")
    assert findings and findings[0]["kind"] == "cross_check_unavailable"


# ---------------------------------------------------------------------------
# CPU-only guarantee
# ---------------------------------------------------------------------------


def test_matrix_module_does_not_import_the_server_or_an_http_client():
    """The assembler may *name* the server in its printed hint, but must never import or spawn it.

    The import check runs in a fresh interpreter: in the suite's own process another test has
    already imported ``dev_tools.serve_openai``, which would make a plain ``sys.modules``
    assertion order-dependent and meaningless.
    """

    import re
    import subprocess

    repo_root = Path(matrix.__file__).resolve().parents[1]
    code = (
        "import sys; from dev_tools import matrix; "
        "assert 'dev_tools.serve_openai' not in sys.modules, 'serve_openai imported'; "
        "assert matrix.__name__ == 'dev_tools.matrix'; print('ok')"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code], cwd=str(repo_root), capture_output=True, text=True, timeout=120
    )
    assert completed.returncode == 0, completed.stderr
    assert "ok" in completed.stdout

    source = Path(matrix.__file__).read_text(encoding="utf-8")
    imports = set(re.findall(r"^\s*(?:from|import)\s+([\w.]+)", source, re.MULTILINE))
    forbidden_prefixes = ("urllib", "requests", "http.client", "http.server", "subprocess", "socket")
    offenders = {name for name in imports if name.startswith(forbidden_prefixes)}
    assert not offenders, f"the assembler must not import {sorted(offenders)}"
    assert not any("serve_openai" in name for name in imports)


def test_cli_runs_with_sockets_poisoned(tmp_path, monkeypatch, capsys):
    """The whole CLI must work with networking impossible: no server, no DB, no HTTP."""

    def _no_network(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("matrix.py attempted a network connection")

    monkeypatch.setattr(socket, "socket", _no_network)
    monkeypatch.setattr(socket, "create_connection", _no_network)

    runs = _runs_dir(tmp_path)
    out = tmp_path / "matrix.json"
    code = matrix.main([
        "--runs-dir", str(runs),
        "--feature-file", str(tmp_path / "absent_feature.txt"),
        "--cross-check", str(tmp_path / "absent-2x2.json"),
        "--frozen", str(tmp_path / "absent-frozen.json"),
        "--json", str(out),
    ])
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["cpu_only"] is True
    statuses = {arm["id"]: arm["status"] for arm in payload["arms"]}
    assert statuses["base_noev"] == "not measured"  # no ledger in this tmp runs dir
    assert statuses["base_noev_features"] == "blocked"
    assert statuses["gpt_features"] == "blocked"
    captured = capsys.readouterr().out
    assert "feature × model matrix" in captured
    assert matrix.BLOCKED_REASON in captured
