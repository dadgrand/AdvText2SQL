"""Run the real course harness once, and record everything the run produced.

The harness (`benchmarks/base.py::BenchmarkBase.run`) writes ``query_results.json``
and ``db_schemas.json`` into the *current working directory*, which is how two
runs silently overwrite each other (defect R1-D22).  This wrapper gives every run
its own directory:

```
data_cache/runs/<run-id>/
  query_results.json     predictions of this run (written by the harness)
  db_schemas.json        the schemas the tool actually saw (written by the harness)
  run.json               provenance: argv, tool class, model, endpoint, file hashes,
                         git commit, started/finished timestamps, report summary
```

and it refuses to start when ``<run-id>`` already exists and is not empty.

Safety
------
Nothing is spent until the caller passes ``--allow-llm-spend``.  Without it the
wrapper only prints the plan (the resolution of files, endpoint and tool class)
and exits 0 — that is also what ``--check`` does explicitly.  The API key is
never accepted on the command line: pass ``--llm-api-key-env NAME`` (default
``LLM_API_KEY``).

The tunnel must be up: the harness needs ``DB_USER``/``DB_PASS`` and a reachable
PostgreSQL (``baseline_agent.py``/``bird_benchmark.py`` pass ``BENCHMARK_DB_URL``).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import os
import subprocess
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

try:  # package import
    from . import paths
except ImportError:  # pragma: no cover - direct script execution
    import paths  # type: ignore[no-redef]

__all__ = ["RunRefused", "build_client", "main", "plan_run", "run_harness"]

DEFAULT_TOOL_CLASS = "adv_text2sql.mcp_servers.text2sql_tool.src.text2sql_implementation.Text2SQLGenerator"
DEFAULT_RUNS_DIR = paths.DATA_CACHE / "runs"
MODEL_INFO = {
    "json_output": False,
    "function_calling": True,
    "vision": False,
    "family": "unknown",
    "structured_output": False,
}


class RunRefused(RuntimeError):
    """Raised when a run must not start (existing artifacts, missing inputs)."""


# ---------------------------------------------------------------------------
# Inputs, provenance
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_commit(repo: Path = paths.REPO_ROOT) -> Optional[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def plan_run(
    *,
    run_id: str,
    query_file: Path,
    answer_file: Path,
    db_url: Optional[str],
    tool_class: str,
    llm_base_url: Optional[str],
    llm_model: Optional[str],
    runs_dir: Path,
    benchmark: str = "bird",
) -> dict:
    """Resolve and validate everything a run needs; raises :class:`RunRefused`."""

    if not query_file.is_file():
        raise RunRefused(f"query file not found: {query_file}")
    if not answer_file.is_file():
        raise RunRefused(f"answer file not found: {answer_file}")
    if not os.environ.get("DB_USER") or not os.environ.get("DB_PASS"):
        raise RunRefused("DB_USER and DB_PASS must be set in the environment (see repo/.env)")
    if not db_url:
        raise RunRefused("no --db-url (or BENCHMARK_DB_URL): the harness cannot run without the database")
    if not llm_base_url or not llm_model:
        raise RunRefused("--llm-base-url and --llm-model are required (OpenAI-compatible endpoint)")

    run_dir = Path(runs_dir) / run_id
    if run_dir.exists() and any(run_dir.iterdir()):
        raise RunRefused(
            f"refusing to overwrite existing run artifacts in {run_dir} "
            f"(R1-D22: every run gets its own directory; pick a new --run-id)"
        )

    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "query_file": str(query_file),
        "answer_file": str(answer_file),
        "query_sha256": _sha256(query_file),
        "answer_sha256": _sha256(answer_file),
        "db_url": db_url,
        "tool_class": tool_class,
        "benchmark": benchmark,
        "llm_base_url": llm_base_url,
        "llm_model": llm_model,
        "git_commit": _git_commit(),
        "planned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# Imports of the shipped harness (offline-safe)
# ---------------------------------------------------------------------------


def _ensure_harness_importable() -> None:
    """Make ``benchmarks`` and the tool importable without the MCP runtime.

    ``adv_text2sql.mcp_servers.__init__`` imports ``fastmcp``; the benchmark tool
    does not need it.  Same stub technique as ``tests/test_tool.py`` and
    ``dev_tools/dump_schemas.py``.
    """

    for entry in (str(paths.REPO_ROOT), str(paths.REPO_ROOT / "src")):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    try:
        import fastmcp  # noqa: F401
    except ImportError:
        stub = types.ModuleType("fastmcp")
        stub.FastMCP = type("FastMCP", (), {})  # type: ignore[attr-defined]
        sys.modules["fastmcp"] = stub


def resolve_tool_class(dotted: str) -> Any:
    _ensure_harness_importable()
    module_name, _, attribute = dotted.rpartition(".")
    if not module_name:
        raise RunRefused(f"--tool-class must be a dotted path, got {dotted!r}")
    module = importlib.import_module(module_name)
    try:
        return getattr(module, attribute)
    except AttributeError as exc:
        raise RunRefused(f"{module_name} has no attribute {attribute}") from exc


def build_client(base_url: str, model: str, api_key: str) -> Any:
    from autogen_ext.models.openai import OpenAIChatCompletionClient  # noqa: PLC0415

    return OpenAIChatCompletionClient(
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=0,
        model_info=dict(MODEL_INFO),
    )


# ---------------------------------------------------------------------------
# The run itself
# ---------------------------------------------------------------------------


def run_harness(
    *,
    plan: Dict[str, Any],
    query_file: Path,
    answer_file: Path,
    tool_class: Any,
    client: Any,
) -> Dict[str, Any]:
    """Run ``BenchmarkBIRD`` with the process cwd set to the run directory."""

    from benchmarks.bird import BenchmarkBIRD  # noqa: PLC0415

    run_dir = Path(plan["run_dir"])
    run_dir.mkdir(parents=True, exist_ok=True)
    # The harness opens these paths *after* we chdir into the run directory, so
    # they must be absolute by then.
    query_file = Path(query_file).resolve()
    answer_file = Path(answer_file).resolve()
    previous_cwd = Path.cwd()
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        os.chdir(run_dir)
        benchmark = BenchmarkBIRD(
            db_url=plan["db_url"],
            query_file=str(query_file),
            answer_file=str(answer_file),
            llm_client=client,
        )
        report = asyncio.run(benchmark.run(tool_class))
    finally:
        os.chdir(previous_cwd)

    artifacts = sorted(path.name for path in run_dir.iterdir() if path.is_file())
    summary = {
        **plan,
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "artifacts": artifacts,
        "report": {
            "overall_accuracy": report.get("overall_accuracy"),
            "accuracy_by_difficulty": report.get("accuracy_by_difficulty"),
            "total": report.get("total"),
            "false_ambiguous": report.get("false_ambiguous"),
            "false_ambiguous_rate": report.get("false_ambiguous_rate"),
        }
        if isinstance(report, dict)
        else None,
    }
    (run_dir / "run.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return summary


def run_predict_only(
    *,
    plan: Dict[str, Any],
    query_file: Path,
    answer_file: Path,
    tool_class: Any,
    client: Any,
) -> Dict[str, Any]:
    """Build + predict only; leave scoring to ``dev_tools/fixed_eval.py``.

    Why this exists: ``BenchmarkBase.run`` finishes with ``evaluate_bird.run_evaluation``,
    which creates a **new** SQLAlchemy engine per question and connects with no
    timeout.  Over a flaky SSH tunnel that stalls for minutes (measured: a
    ``bird_small`` run completed all 22 predictions and then hung inside
    ``evaluate_bird.py:82``), and the harness catches only ``SQLAlchemyError``.
    Predictions are the expensive part; the official scoring path for this branch
    is ``fixed_eval`` (EX/241 + EX/211 + per-question ledger), so this mode stops
    after predictions and writes ``query_results.json`` + ``db_schemas.json`` +
    ``run.json`` (with token usage) into the run directory.
    """

    if plan.get("benchmark") == "ambrosia":
        from benchmarks.ambrosia import BenchmarkAmbrosia as _Benchmark  # noqa: PLC0415
    else:
        from benchmarks.bird import BenchmarkBIRD as _Benchmark  # noqa: PLC0415

    run_dir = Path(plan["run_dir"])
    run_dir.mkdir(parents=True, exist_ok=True)
    query_file = Path(query_file).resolve()
    answer_file = Path(answer_file).resolve()
    previous_cwd = Path.cwd()
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        os.chdir(run_dir)
        benchmark = _Benchmark(
            db_url=plan["db_url"],
            query_file=str(query_file),
            answer_file=str(answer_file),
            llm_client=client,
        )

        async def _predict() -> Dict[str, str]:
            tools = await benchmark.build(tool_class)  # type: ignore[arg-type]
            return await benchmark.predict(tools)

        predictions = asyncio.run(_predict())
        benchmark._save_predictions(predictions, "query_results.json")
        usage = benchmark.llm_client.get_usage() if hasattr(benchmark.llm_client, "get_usage") else None
    finally:
        os.chdir(previous_cwd)

    artifacts = sorted(path.name for path in run_dir.iterdir() if path.is_file())
    summary = {
        **plan,
        "mode": "predict-only",
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "artifacts": artifacts,
        "n_predictions": len(predictions),
        "llm_usage": usage,
        "note": "scoring is done by dev_tools.fixed_eval.py; the harness evaluation is skipped on purpose",
    }
    (run_dir / "run.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dev_tools.run_benchmark",
        description=(
            "Run the course harness once into data_cache/runs/<run-id>/ (query_results.json, "
            "db_schemas.json, run.json) and refuse to overwrite an existing run."
        ),
    )
    parser.add_argument("--run-id", required=True, help="unique run id, e.g. 2026-10-23-baseline")
    parser.add_argument("--query-file", required=True, help="e.g. data/bird_small.json")
    parser.add_argument("--answer-file", required=True, help="e.g. data/bird_small.json")
    parser.add_argument(
        "--db-url",
        default=os.environ.get("BENCHMARK_DB_URL"),
        help="benchmark PostgreSQL host:port (default: $BENCHMARK_DB_URL)",
    )
    parser.add_argument("--tool-class", default=DEFAULT_TOOL_CLASS, help="dotted path of the tool class")
    parser.add_argument(
        "--benchmark",
        choices=("bird", "ambrosia"),
        default="bird",
        help="which harness benchmark to drive (default: bird); ambrosia does not read evidence "
        "and compares ambiguity flags instead of SQL results",
    )
    parser.add_argument(
        "--llm-base-url",
        default=os.environ.get("LLM_BASE_URL"),
        help="OpenAI-compatible endpoint (default: $LLM_BASE_URL)",
    )
    parser.add_argument("--llm-model", default=os.environ.get("LLM_MODEL_NAME"), help="model name")
    parser.add_argument(
        "--llm-api-key-env",
        default="LLM_API_KEY",
        help="environment variable holding the API key (default: LLM_API_KEY); the key is never "
        "taken from the command line",
    )
    parser.add_argument("--runs-dir", default=str(DEFAULT_RUNS_DIR), help="where run directories live")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate and print the plan only; touches no LLM and creates nothing",
    )
    parser.add_argument(
        "--allow-llm-spend",
        action="store_true",
        help="required to actually call the model (the wrapper refuses otherwise)",
    )
    parser.add_argument(
        "--predict-only",
        action="store_true",
        help="stop after predictions (query_results.json + db_schemas.json) and leave scoring to "
        "dev_tools/fixed_eval.py; avoids the harness's per-question DB connects during evaluation",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.allow_llm_spend and not args.check:
        print(
            "refusing to run: this spends model tokens. Re-run with --check to see the plan, "
            "or with --allow-llm-spend once the captain approved the endpoint.",
            file=sys.stderr,
        )
        return 2

    try:
        plan = plan_run(
            run_id=args.run_id,
            query_file=Path(args.query_file),
            answer_file=Path(args.answer_file),
            db_url=args.db_url,
            tool_class=args.tool_class,
            llm_base_url=args.llm_base_url,
            llm_model=args.llm_model,
            runs_dir=Path(args.runs_dir),
            benchmark=args.benchmark,
        )
    except RunRefused as exc:
        print(f"refusing to run: {exc}", file=sys.stderr)
        return 2

    print("run plan:")
    for key in ("run_id", "run_dir", "query_file", "answer_file", "db_url", "tool_class",
                "llm_base_url", "llm_model", "git_commit"):
        print(f"  {key}: {plan[key]}")
    if args.check:
        print("--check: nothing was started, nothing was written")
        return 0

    api_key = os.environ.get(args.llm_api_key_env)
    if not api_key:
        print(f"refusing to run: ${args.llm_api_key_env} is not set", file=sys.stderr)
        return 2

    tool_class = resolve_tool_class(args.tool_class)
    client = build_client(plan["llm_base_url"], plan["llm_model"], api_key)

    runner = run_predict_only if args.predict_only else run_harness
    try:
        summary = runner(
            plan=plan,
            query_file=Path(args.query_file),
            answer_file=Path(args.answer_file),
            tool_class=tool_class,
            client=client,
        )
    except RunRefused as exc:
        print(f"run failed: {exc}", file=sys.stderr)
        return 2

    report = summary.get("report") or {}
    print(
        f"run {summary['run_id']} finished: overall_accuracy={report.get('overall_accuracy')} "
        f"total={report.get('total')}"
    )
    print(f"artifacts in {summary['run_dir']}: {', '.join(summary['artifacts']) or '(none)'}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())




