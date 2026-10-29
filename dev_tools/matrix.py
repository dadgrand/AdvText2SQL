"""CPU-only assembler for the round-2 **feature × model** matrix.

What this module is
===================
The round-2 plan's Phase 3 measures a matrix, not one number: the team's API pipeline
with and without its feature set, the local 7B base with the same prompt surface, and
the local 7B + LoRA adapter with and without the feature block — so that "does the
adapter stack with the features?" can be answered instead of guessed. This module is the
*assembler* for that matrix: it reads per-question score ledgers that already exist and
prints one table. It is deliberately **not** a runner.

Hard constraints (enforced by the tests, not just by comment)
-------------------------------------------------------------
* **No model server, no GPU, no LLM call, no database.** Every number comes from a
  ``dev_tools/fixed_eval.py`` ledger on disk (``runs/t2/scores/*.json``). This module
  imports neither ``serve_openai`` nor any HTTP client, and it never spawns a process.
* **Arms with no artifact are reported, never executed.** An arm whose ledger is absent
  shows up as ``not measured`` / ``blocked`` with the reason; the CLI has no flag that
  would make it start anything.
* Missing cells are the point of the matrix, so they are first-class rows, not omissions.

Arms
----
An arm is ``{model, use_evidence, feature_prompt}``:

* ``model`` — ``base`` (Qwen2.5-Coder-7B-Instruct, 4-bit serving), ``adapter``
  (``runs/e3-r16/adapter`` on top of it), or ``gpt-5.4-mini`` (the team's API pipeline;
  the only model in the matrix that is not ours to run).
* ``use_evidence`` — ``bool``: whether ``BenchmarkBIRD.use_evidence`` was on.
* ``feature_prompt`` — ``None`` (no feature block) or a file path (the team's feature set,
  injected verbatim into the SQL user prompt).

The four measured cells (feature block absent) exist as ``runs/t2/scores/*.json`` and are
cross-checked against the independent ``2x2-analysis.json`` ledger; a disagreement is
reported as a finding, not smoothed over.

Feature-block input format (what the runner needs when the file appears)
-----------------------------------------------------------------------
* **v1, global (supported now):** a UTF-8 text file, e.g. ``training/feature_block.txt``,
  inserted verbatim as ``Feature block:\\n<text>`` into the SQL user prompt after the
  question. The same file feeds ``training/build_data.py --feature-prompt``, so training
  and serving see the same block. Empty/whitespace-only files are refused; more than
  ``MAX_FEATURE_CHARS`` characters are refused and more than ``WARN_FEATURE_CHARS`` are
  flagged (the 4-bit server spends prompt budget on every request).
* **v2, per-question (documented, not yet wired):** a JSON object
  ``{"<question_id>": "<feature text>"}``. The runner would have to select the block per
  question; until the team ships such a file, v1 is the contract.

Wiring for the blocked feature arms
-----------------------------------
``dev_tools/run_benchmark.py --feature-prompt FILE`` takes the same file, validates it
identically, injects ``Feature block:\\n<text>`` into the question string the tool sees,
and records ``feature_prompt {file, chars, sha256_16}`` in ``run.json``. So when the file
appears and a GPU window exists, the blocked rows become measured by running exactly the
command this module prints — no new code path.

CLI
---
    python -m dev_tools.matrix                      # the matrix + paired block vs adapter_ev
    python -m dev_tools.matrix --reference base_ev  # pair every measured arm against B
    python -m dev_tools.matrix --feature-file training/feature_block.txt --json out.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

try:  # package import
    from . import fixed_eval, paths, score_runs
except ImportError:  # pragma: no cover - direct script execution
    import fixed_eval  # type: ignore[no-redef]
    import paths  # type: ignore[no-redef]
    import score_runs  # type: ignore[no-redef]

__all__ = [
    "Arm",
    "DEFAULT_FEATURE_FILE",
    "DEFAULT_REFERENCE",
    "MAX_FEATURE_CHARS",
    "WARN_FEATURE_CHARS",
    "arm_command",
    "assemble",
    "cross_check",
    "default_arms",
    "feature_file_report",
    "load_arms",
    "load_ledger",
    "main",
    "matrix_row",
    "paired_block",
    "render",
]

DEFAULT_RUNS_DIR = paths.REPO_ROOT / "runs" / "t2"
DEFAULT_REFERENCE = "adapter_ev"
DEFAULT_FEATURE_FILE = "training/feature_block.txt"
DEFAULT_CROSS_CHECK = DEFAULT_RUNS_DIR / "scores" / "2x2-analysis.json"
FROZEN_LEDGER = paths.TOPIC_ROOT / "artifacts" / "pred_scores.json"

#: The feature block is re-sent with every question: refuse runaway files, warn on large ones.
MAX_FEATURE_CHARS = 20_000
WARN_FEATURE_CHARS = 4_000

BLOCKED_REASON = "needs team feature file / endpoint"
DIFFICULTIES = ("simple", "moderate", "challenging")


# ---------------------------------------------------------------------------
# Arms
# ---------------------------------------------------------------------------


@dataclass
class Arm:
    """One cell of the matrix and everything needed to say what it needs."""

    id: str
    model: str  # base | adapter | gpt-5.4-mini
    use_evidence: Optional[bool]
    feature_prompt: Optional[str]  # None | file path | "team best combo"
    status: str  # measured | blocked | not measured
    ledger: Optional[str] = None
    run_id: Optional[str] = None
    blocked_reason: str = ""
    blockers: List[str] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "model": self.model,
            "use_evidence": self.use_evidence,
            "feature_prompt": self.feature_prompt,
            "status": self.status,
            "ledger": self.ledger,
            "run_id": self.run_id,
            "blocked_reason": self.blocked_reason,
            "blockers": list(self.blockers),
            "note": self.note,
        }


def default_arms(runs_dir: Path = DEFAULT_RUNS_DIR) -> List[Arm]:
    """The planned matrix: four measured cells, four blocked feature cells, the API rows."""

    scores = Path(runs_dir) / "scores"
    measured = [
        Arm(
            id="base_noev",
            model="base",
            use_evidence=False,
            feature_prompt=None,
            status="measured",
            ledger=str(scores / "e1-bird-large.json"),
            run_id="e1-bird-large",
            note="2x2 cell A",
        ),
        Arm(
            id="base_ev",
            model="base",
            use_evidence=True,
            feature_prompt=None,
            status="measured",
            ledger=str(scores / "e1fair-bird-large.json"),
            run_id="e1fair-bird-large",
            note="2x2 cell B",
        ),
        Arm(
            id="adapter_noev",
            model="adapter",
            use_evidence=False,
            feature_prompt=None,
            status="measured",
            ledger=str(scores / "e3-bird-large.json"),
            run_id="e3-bird-large",
            note="2x2 cell C",
        ),
        Arm(
            id="adapter_ev",
            model="adapter",
            use_evidence=True,
            feature_prompt=None,
            status="measured",
            ledger=str(scores / "e3fair-bird-large.json"),
            run_id="e3fair-bird-large",
            note="2x2 cell D",
        ),
    ]
    feature_cells = []
    for model in ("base", "adapter"):
        for use_evidence in (False, True):
            feature_cells.append(
                Arm(
                    id=f"{model}_{'ev' if use_evidence else 'noev'}_features",
                    model=model,
                    use_evidence=use_evidence,
                    feature_prompt=DEFAULT_FEATURE_FILE,
                    status="blocked",
                    blocked_reason=BLOCKED_REASON,
                    blockers=["team feature file", "GPU window (t6 until ~14:30, then t7 first)"],
                    note="same prompt surface as its feature-free twin, plus the feature block",
                )
            )
    api_rows = [
        Arm(
            id="gpt_features",
            model="gpt-5.4-mini",
            use_evidence=None,
            feature_prompt="team best combo",
            status="blocked",
            blocked_reason=BLOCKED_REASON,
            blockers=["teacher endpoint (GPT-5.4-mini + best feature combo)"],
            note="the 44.81 % claim lives here; the frozen file measures 42.32 % id-level",
        ),
        Arm(
            id="gpt_no_features",
            model="gpt-5.4-mini",
            use_evidence=None,
            feature_prompt=None,
            status="blocked",
            blocked_reason=BLOCKED_REASON,
            blockers=["teacher endpoint (GPT-5.4-mini without the feature combo)"],
            note="isolates the feature contribution inside the API pipeline",
        ),
    ]
    return measured + feature_cells + api_rows


def load_arms(path: Any) -> List[Arm]:
    """Read a caller-supplied arm list: a JSON array of ``{id, model, use_evidence, ...}``.

    Every arm needs ``id``, ``model`` and ``use_evidence``; ``feature_prompt``, ``ledger``,
    ``status``, ``blocked_reason``, ``blockers`` and ``note`` are optional.  This is how the
    matrix can grow without editing the module (a new adapter, a new feature file, an API row).
    """

    payload = _read_json(Path(path))
    if not isinstance(payload, list):
        raise ValueError(f"{path}: the arm list must be a JSON array")
    arms: List[Arm] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ValueError(f"{path}: arm #{index} must be an object")
        missing = [key for key in ("id", "model", "use_evidence") if key not in item]
        if missing:
            raise ValueError(f"{path}: arm #{index} is missing {missing}")
        arms.append(
            Arm(
                id=str(item["id"]),
                model=str(item["model"]),
                use_evidence=item["use_evidence"],
                feature_prompt=item.get("feature_prompt"),
                status=str(item.get("status") or "not measured"),
                ledger=item.get("ledger"),
                run_id=item.get("run_id"),
                blocked_reason=str(item.get("blocked_reason") or ""),
                blockers=list(item.get("blockers") or []),
                note=str(item.get("note") or ""),
            )
        )
    if len({arm.id for arm in arms}) != len(arms):
        raise ValueError(f"{path}: arm ids must be unique")
    return arms


# ---------------------------------------------------------------------------
# Ledgers
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_ledger(path: Any, *, executable_n: Optional[int] = None) -> Dict[str, Any]:
    """Normalise a score ledger (ours or the frozen id-level file) into one shape.

    Returns ``{source, n, ex_all, ex_gold_executable, per_difficulty, soft_f1_mean,
    n_pred_unexecutable, items}``.  ``items`` is ``[{question_id, score}]`` so the paired
    block can reuse :func:`dev_tools.score_runs.compare_ledgers` unchanged.

    ``executable_n`` is needed only by ledgers that store a bare percentage instead of a
    ``{correct, n}`` block (the frozen GPT file: 48.80 % is over the 209 gold-executable
    questions, not over the 241 total).  Callers that know the denominator pass it; without
    it the percentage is reported with ``n`` unknown rather than guessed.
    """

    source = Path(path)
    payload = _read_json(source)
    if not isinstance(payload, dict):
        raise ValueError(f"{source}: a ledger must be a JSON object")

    items = payload.get("items")
    if items is None and "per_question" in payload:
        items = payload["per_question"]
    if items is None:
        raise ValueError(f"{source}: ledger has neither 'items' nor 'per_question'")
    pairs = [
        {"question_id": str(item["question_id"]), "score": int(item["score"])}
        for item in items
    ]

    ex_all = payload.get("ex_all")
    ex_exec = payload.get("ex_gold_executable")
    if ex_exec is None:
        ex_exec = payload.get("ex_exec")  # the frozen file's key
    if ex_all is None:  # the frozen id-level file stores percentages, not blocks
        correct = sum(item["score"] for item in pairs)
        ex_all = {"correct": correct, "n": len(pairs), "value": correct / len(pairs) if pairs else None,
                  "percent": 100.0 * correct / len(pairs) if pairs else None}
    elif isinstance(ex_all, (int, float)):  # tolerate a bare percentage
        correct = int(round(ex_all / 100.0 * len(pairs)))
        ex_all = {"correct": correct, "n": len(pairs), "percent": float(ex_all),
                  "value": float(ex_all) / 100.0}
    if ex_exec is None:
        ex_exec = dict(ex_all)
    elif isinstance(ex_exec, (int, float)):
        n_exec = executable_n
        correct = int(round(ex_exec / 100.0 * n_exec)) if n_exec else None
        ex_exec = {"correct": correct, "n": n_exec, "percent": float(ex_exec),
                   "value": float(ex_exec) / 100.0}

    per_difficulty = payload.get("per_difficulty") or payload.get("by_difficulty") or {}
    normalized_difficulty: Dict[str, Dict[str, Any]] = {}
    for name, block in per_difficulty.items():
        if isinstance(block, dict):
            normalized_difficulty[str(name)] = {
                "correct": block.get("correct"),
                "n": block.get("n"),
                "percent": block.get("percent", (100.0 * block["value"]) if block.get("value") is not None else None),
            }
        else:  # a bare percentage (the frozen file)
            normalized_difficulty[str(name)] = {"correct": None, "n": None, "percent": float(block)}

    return {
        "source": str(source),
        "n": len(pairs),
        "ex_all": ex_all,
        "ex_gold_executable": ex_exec,
        "per_difficulty": normalized_difficulty,
        "soft_f1_mean": payload.get("soft_f1_mean"),
        "n_pred_unexecutable": payload.get("n_pred_unexecutable"),
        "items": pairs,
    }


# ---------------------------------------------------------------------------
# Feature block
# ---------------------------------------------------------------------------


def feature_file_report(path: Optional[str]) -> Dict[str, Any]:
    """Validate the feature-block file without touching a model.

    The rules mirror ``training/build_data.py::load_feature_block`` (non-empty after
    stripping) plus a size ceiling, because the block is re-sent on every request.
    ``problems`` are fatal (the runner refuses); ``warnings`` are advisory (a large block
    costs prompt tokens but is not an error).
    """

    if not path:
        return {"path": None, "present": False, "problems": [], "warnings": [],
                "chars": None, "sha256_16": None, "ok": False}
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = paths.REPO_ROOT / candidate
    report: Dict[str, Any] = {
        "path": str(path),
        "resolved": str(candidate),
        "present": candidate.is_file(),
        "problems": [],
        "warnings": [],
        "chars": None,
        "sha256_16": None,
    }
    if not candidate.is_file():
        report["problems"].append("file not found")
        report["ok"] = False
        return report
    try:
        text = candidate.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        report["problems"].append(f"not valid UTF-8: {exc}")
        report["ok"] = False
        return report
    report["chars"] = len(text)
    report["sha256_16"] = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    if not text.strip():
        report["problems"].append("empty after stripping whitespace")
    if len(text) > MAX_FEATURE_CHARS:
        report["problems"].append(
            f"{len(text)} chars exceeds the {MAX_FEATURE_CHARS}-char cap (the block rides in every prompt)"
        )
    elif len(text) > WARN_FEATURE_CHARS:
        report["warnings"].append(
            f"{len(text)} chars is large (> {WARN_FEATURE_CHARS}); expect a prompt-token cost on every question"
        )
    report["ok"] = not report["problems"]
    return report


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def matrix_row(arm: Arm, feature_report: Dict[str, Any], runs_dir: Path | None = None) -> Dict[str, Any]:
    """Resolve one arm into a table row; never runs anything.

    Resolution order: an existing ledger wins (``measured``); otherwise a declared
    ``blocked`` arm keeps its reason and blockers; otherwise the arm is ``not measured``.
    """

    row = arm.as_dict()
    feature_path = arm.feature_prompt if arm.feature_prompt and arm.feature_prompt.endswith(".txt") else None
    row["feature_prompt_report"] = feature_report if feature_path else None

    ledger = Path(arm.ledger) if arm.ledger else None
    if ledger is not None and ledger.is_file():
        try:
            data = load_ledger(ledger)
        except (ValueError, KeyError, OSError) as exc:
            row["status"] = "not measured"
            row["blocked_reason"] = f"ledger unreadable: {exc}"
            return row
        row["status"] = "measured"
        row["ledger"] = str(ledger)
        row["ex_all"] = data["ex_all"]
        row["ex_gold_executable"] = data["ex_gold_executable"]
        row["per_difficulty"] = data["per_difficulty"]
        row["soft_f1_mean"] = data["soft_f1_mean"]
        row["n_pred_unexecutable"] = data["n_pred_unexecutable"]
        row["n_questions"] = data["n"]
        row["_items"] = data["items"]
        if feature_path:
            ready = bool(feature_report.get("ok"))
            row["note"] = (row.get("note") or "") + (
                "; feature file present and valid" if ready else "; feature file still missing/invalid"
            )
        return row

    if arm.status == "blocked":
        # A declared blocked arm keeps whatever it is waiting for; the feature block (when this
        # arm uses one) is one of the blockers, the GPU window is the other.
        row["status"] = "blocked"
        row["blocked_reason"] = arm.blocked_reason or BLOCKED_REASON
        blockers = list(arm.blockers)
        if feature_path:
            present = bool(feature_report.get("present"))
            blockers = [
                "team feature file" + (" (present)" if present else ""),
                "GPU window (t6 until ~14:30, then t7 first)",
            ]
        row["blockers"] = blockers
        row["command"] = arm_command(arm) if feature_path else (
            "# teacher pipeline: not runnable from this repo"
        )
        return row

    row["status"] = "not measured"
    row["blocked_reason"] = f"no ledger at {arm.ledger or '(none set)'}"
    return row


def arm_command(arm: Arm) -> str:
    """The exact commands that would produce this arm on a free GPU (never executed here)."""

    served = {
        "base": "qwen2.5-coder-7b",
        "adapter": "qwen2.5-coder-7b+e3-r16",
    }.get(arm.model, arm.model)
    serve = [
        "training\\.venv-train\\Scripts\\python.exe -m dev_tools.serve_openai",
        "--model D:\\AI\\Models\\qwen2.5-coder-7b-instruct",
    ]
    if arm.model == "adapter":
        serve.append("--adapter runs/e3-r16/adapter")
    serve += [f"--served-name {served}", "--port 8000", "--max-new-tokens 512", "--load-in-4bit"]
    run_id = f"{arm.id}-bird-large"
    run = [
        "python -m dev_tools.run_benchmark --allow-llm-spend --predict-only",
        f"--run-id {run_id}",
        "--query-file data/bird_large.json --answer-file data/bird_large.json",
        "--db-url localhost:5444 --runs-dir runs/t2",
        f"--llm-model {served}",
    ]
    if arm.use_evidence:
        run.append("--use-evidence")
    if arm.feature_prompt and arm.feature_prompt.endswith(".txt"):
        run.append(f"--feature-prompt {arm.feature_prompt}")
    return "# serve first: " + " ".join(serve) + "\n# then: " + " ".join(run)


def assemble(
    arms: Sequence[Arm],
    *,
    feature_file: Optional[str] = None,
    runs_dir: Path = DEFAULT_RUNS_DIR,
) -> List[Dict[str, Any]]:
    """Resolve every arm; arms with a feature block share one file report."""

    feature_report = feature_file_report(feature_file or DEFAULT_FEATURE_FILE)
    return [matrix_row(arm, feature_report, runs_dir) for arm in arms]


def paired_block(rows: Sequence[Dict[str, Any]], reference_id: str) -> Optional[Dict[str, Any]]:
    """Paired b/c + exact McNemar + 10k bootstrap for every measured row vs the reference."""

    measured = {row["id"]: row for row in rows if row.get("status") == "measured" and row.get("_items")}
    if reference_id not in measured:
        return None
    reference = measured[reference_id]
    out: Dict[str, Any] = {"reference": reference_id, "comparisons": {}}
    for arm_id, row in measured.items():
        if arm_id == reference_id:
            continue
        comparison = score_runs.compare_ledgers(
            {"items": row["_items"]},
            {"items": reference["_items"]},
            n_boot=10_000,
            seed=0,
            labels=(arm_id, reference_id),
        )
        out["comparisons"][arm_id] = comparison
    return out


def cross_check(
    rows: Sequence[Dict[str, Any]],
    reference: Any = DEFAULT_CROSS_CHECK,
) -> List[Dict[str, Any]]:
    """Compare the assembled cells with the independent 2x2 ledger; report every mismatch."""

    path = Path(reference)
    if not path.is_file():
        return [{"kind": "cross_check_unavailable", "reference": str(path),
                 "detail": "the independent 2x2 ledger is absent; nothing to cross-check"}]
    payload = _read_json(path)
    cells = payload.get("cells") or {}
    mapping = {"base_noev": "A", "base_ev": "B", "adapter_noev": "C", "adapter_ev": "D"}
    findings: List[Dict[str, Any]] = []
    compared = 0
    for row in rows:
        if row.get("status") != "measured" or row["id"] not in mapping:
            continue
        cell = cells.get(mapping[row["id"]])
        if not cell:
            findings.append({"kind": "missing_cell", "arm": row["id"], "cell": mapping[row["id"]]})
            continue
        checks = [
            ("ex_all.correct", row["ex_all"].get("correct"), cell["ex_all"].get("correct")),
            ("ex_gold_executable.correct", row["ex_gold_executable"].get("correct"),
             cell["ex_gold_executable"].get("correct")),
        ]
        for difficulty in DIFFICULTIES:
            checks.append((
                f"per_difficulty.{difficulty}.correct",
                (row.get("per_difficulty") or {}).get(difficulty, {}).get("correct"),
                (cell.get("per_difficulty") or {}).get(difficulty, {}).get("correct"),
            ))
        compared += 1
        for field_name, assembled, expected in checks:
            if assembled != expected:
                findings.append({
                    "kind": "discrepancy",
                    "arm": row["id"],
                    "cell": mapping[row["id"]],
                    "field": field_name,
                    "assembled": assembled,
                    "t2_reference": expected,
                    "reference": str(path),
                })
    if compared == 0:
        findings.append({
            "kind": "cross_check_not_applicable",
            "reference": str(path),
            "detail": "none of the assembled arms is one of the four t2 cells; nothing to compare",
        })
    return findings


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _pct(value: Any, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.{digits}f} %"


def _ratio(row: Dict[str, Any], key: str) -> str:
    block = row.get(key) or {}
    correct, n = block.get("correct"), block.get("n")
    if correct is None or n is None:
        return "n/a"
    return f"{correct}/{n}"


def render(rows: Sequence[Dict[str, Any]], paired: Optional[Dict[str, Any]],
           findings: Sequence[Dict[str, Any]], *, stream: Any = None) -> None:
    stream = stream if stream is not None else sys.stdout
    print("=== feature × model matrix | data/bird_large.json (241 questions) | mode=local ===", file=stream)
    print("", file=stream)
    header = (f"{'arm':<26} {'model':<14} {'evid':<5} {'features':<22} {'status':<10} "
              f"{'EX/241':>12} {'EX/209':>12} {'simple':>8} {'moderate':>9} {'challenging':>12} {'softF1':>7}")
    print(header, file=stream)
    print("-" * len(header), file=stream)
    for row in rows:
        features = "none" if not row.get("feature_prompt") else os.path.basename(str(row["feature_prompt"]))
        evidence = "n/a" if row.get("use_evidence") is None else ("on" if row["use_evidence"] else "off")
        status = row.get("status", "?")
        if status == "measured":
            ex241 = f"{_ratio(row, 'ex_all')} {_pct((row.get('ex_all') or {}).get('percent'))}"
            ex209 = f"{_ratio(row, 'ex_gold_executable')} {_pct((row.get('ex_gold_executable') or {}).get('percent'))}"
            diff = {name: _pct((row.get('per_difficulty') or {}).get(name, {}).get('percent')).replace(' %', '')
                    for name in DIFFICULTIES}
            soft = "n/a" if row.get("soft_f1_mean") is None else f"{row['soft_f1_mean']:.4f}"
        else:
            ex241 = ex209 = "—"
            diff = {name: "—" for name in DIFFICULTIES}
            soft = "—"
        print(f"{row['id']:<26} {str(row.get('model')):<14} {evidence:<5} {features:<22} {status:<10} "
              f"{ex241:>12} {ex209:>12} {diff['simple']:>8} {diff['moderate']:>9} "
              f"{diff['challenging']:>12} {soft:>7}", file=stream)
    print("", file=stream)

    blocked = [row for row in rows if row.get("status") != "measured"]
    if blocked:
        print("blocked / not measured:", file=stream)
        for row in blocked:
            print(f"  {row['id']:<26} {row.get('blocked_reason') or row.get('status')}"
                  f"  blockers={row.get('blockers') or '—'}", file=stream)
            if row.get("command"):
                print(f"      produce with: {row['command']}", file=stream)
        print("", file=stream)

    if paired and paired.get("comparisons"):
        print(f"paired block vs {paired['reference']} (exact McNemar, 10k paired bootstrap, seed 0):", file=stream)
        for arm_id, block in paired["comparisons"].items():
            low, high = block["bootstrap95"]
            print(f"  {arm_id:<26} b={block['b']:>3} c={block['c']:>3} "
                  f"p={block['mcnemar_exact_p']:.3e} dEX={block['delta_ex'] * 100:+.2f} pp "
                  f"CI [{low * 100:+.2f}, {high * 100:+.2f}] pp", file=stream)
        print("", file=stream)

    if findings:
        print("cross-check findings:", file=stream)
        for finding in findings:
            if finding["kind"] in ("cross_check_unavailable", "cross_check_not_applicable"):
                print(f"  (skip) {finding['detail']}", file=stream)
            elif finding["kind"] == "discrepancy":
                print(f"  DISCREPANCY {finding['arm']} cell {finding['cell']} {finding['field']}: "
                      f"assembled={finding['assembled']} t2={finding['t2_reference']}", file=stream)
            else:
                print(f"  {finding}", file=stream)
        print("", file=stream)
    else:
        print("cross-check: assembled cells agree with the independent 2x2 ledger.", file=stream)
        print("", file=stream)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dev_tools.matrix",
        description=(
            "Assemble the feature × model matrix from existing score ledgers. CPU-only: it never "
            "starts a server, calls a model or touches the database; arms without an artifact are "
            "reported as blocked/not measured."
        ),
    )
    parser.add_argument("--runs-dir", default=str(DEFAULT_RUNS_DIR),
                        help="directory holding the t2 score ledgers (default: runs/t2)")
    parser.add_argument("--arms", default=None,
                        help="JSON array of arms to assemble instead of the built-in manifest "
                             "(each needs at least id/model/use_evidence)")
    parser.add_argument("--reference", default=DEFAULT_REFERENCE,
                        help=f"arm id the paired block is computed against (default: {DEFAULT_REFERENCE})")
    parser.add_argument("--feature-file", default=None,
                        help=f"feature-block file to validate and attach to the blocked feature arms "
                             f"(default: {DEFAULT_FEATURE_FILE})")
    parser.add_argument("--cross-check", default=str(DEFAULT_CROSS_CHECK),
                        help="independent ledger to compare the assembled cells with (default: 2x2-analysis.json)")
    parser.add_argument("--frozen", default=None,
                        help="frozen GPT baseline ledger (id-level) to add as a reference row "
                             f"(default: {FROZEN_LEDGER} for the built-in manifest; omitted with --arms)")
    parser.add_argument("--json", dest="json_out", default=None,
                        help="write the assembled matrix, paired block and findings to this JSON file")
    parser.add_argument("--quiet", action="store_true", help="do not print the tables")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    runs_dir = Path(args.runs_dir)

    arms = default_arms(runs_dir)
    if args.arms:
        try:
            arms = load_arms(args.arms)
        except (ValueError, KeyError, OSError) as exc:
            print(f"refusing to assemble: {exc}", file=sys.stderr)
            return 2
    rows = assemble(arms, feature_file=args.feature_file or DEFAULT_FEATURE_FILE, runs_dir=runs_dir)

    # The frozen GPT file is a reference, not an arm we can re-run: id-level scores only.
    # A caller-supplied arm list is assembled on its own; the reference is opt-in there.
    frozen_path: Optional[Path] = None
    if args.frozen:
        frozen_path = Path(args.frozen)
    elif not args.arms:
        frozen_path = FROZEN_LEDGER
    if frozen_path is not None and frozen_path.is_file():
        # Its EX/209 is a percentage over the gold-executable denominator, which the measured
        # ledgers know (209 of 241); pass it so the row can show "correct/n" instead of a guess.
        executable_n = next(
            (row["ex_gold_executable"]["n"] for row in rows
             if (row.get("ex_gold_executable") or {}).get("n")),
            None,
        )
        try:
            frozen = load_ledger(frozen_path, executable_n=executable_n)
        except (ValueError, KeyError, OSError):
            frozen = None
        if frozen is not None:
            rows.append({
                "id": "gpt_frozen_file",
                "model": "team-api (frozen)",
                "use_evidence": None,
                "feature_prompt": "team best combo (claimed)",
                "status": "measured",
                "ledger": str(frozen_path),
                "blocked_reason": "",
                "blockers": [],
                "note": (
                    "id-level ledger only (no SQL); the team claimed 44.81 %, the file scores 42.32 %. "
                    "The artifact does not record which model produced it (the claim names a GPT-5.x-mini)."
                ),
                "ex_all": frozen["ex_all"],
                "ex_gold_executable": frozen["ex_gold_executable"],
                "per_difficulty": frozen["per_difficulty"],
                "soft_f1_mean": frozen["soft_f1_mean"],
                "n_pred_unexecutable": None,
                "n_questions": frozen["n"],
                "_items": frozen["items"],
            })

    paired = paired_block(rows, args.reference)
    findings = cross_check(rows, args.cross_check)

    if not args.quiet:
        render(rows, paired, findings)

    if args.json_out:
        payload = {
            "tool": "dev_tools.matrix",
            "cpu_only": True,
            "data": "data/bird_large.json",
            "n_questions": 241,
            "arms": [{key: value for key, value in row.items() if not key.startswith("_")} for row in rows],
            "paired": paired,
            "findings": findings,
        }
        target = Path(args.json_out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        if not args.quiet:
            print(f"matrix written to {target}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
