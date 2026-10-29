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

What ``run.json`` must carry (the round-1 defect this fixes)
-----------------------------------------------------------
Round 1 ran E1 and E1-fair with the same wrapper and the same env vars; the only
difference was a constructor argument that the wrapper did not have a flag for, so
the two ``run.json`` files were indistinguishable on disk and E1 was compared to a
baseline that had evidence enabled. Every run now records, from the *serving
process* rather than from the caller's memory:

* ``use_evidence`` — whether ``BenchmarkBIRD.use_evidence`` was on;
* ``max_tokens`` / ``max_tokens_source`` — the generation cap that actually binds
  (the server's ``/health`` value; a disagreeing ``--max-new-tokens`` is refused);
* ``tool_commit_sha`` (+ ``tool_commit_dirty`` and the dirty paths) and the tool
  module's own SHA-256;
* ``model_id``, ``served_name``, ``adapter_id``, ``adapter_sha256``,
  ``adapter_config_sha256``, ``load_in_4bit``;
* ``cuda_max_memory_allocated_bytes`` / ``..._mib`` plus the whole ``/health``
  payload before and after the run;
* ``requests_served`` and ``max_completion_tokens_observed``, so "no prediction hit
  the cap" is checkable from the artifact instead of re-read from a log.

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
import logging
import os
import subprocess
import sys
import types
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlsplit, urlunsplit

try:  # package import
    from . import paths
except ImportError:  # pragma: no cover - direct script execution
    import paths  # type: ignore[no-redef]

__all__ = ["RunRefused", "build_client", "main", "plan_run", "run_harness"]

logger = logging.getLogger("dev_tools.run_benchmark")

DEFAULT_TOOL_CLASS = "adv_text2sql.mcp_servers.text2sql_tool.src.text2sql_implementation.Text2SQLGenerator"
DEFAULT_RUNS_DIR = paths.DATA_CACHE / "runs"
REPAIR_MODES = ("enabled", "disabled")
"""Whether the tool may spend a second generation fixing its own SQL (tool v2)."""
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


def _git_output(repo: Path, *args: str) -> Optional[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=10
        )
    except Exception:
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def git_provenance(repo: Path = paths.REPO_ROOT) -> Dict[str, Any]:
    """Commit SHA of the tool *and* whether the worktree that produced it was dirty.

    A commit SHA alone is a lie about a modified worktree: the code that actually
    ran may contain uncommitted edits (exactly the round-1 situation, where the
    runs came from a dirty tree).  Recording ``dirty`` and the changed paths makes
    the number reproducible-or-not without ambiguity.
    """

    commit = _git_commit(repo)
    status = _git_output(repo, "status", "--porcelain") or ""
    dirty_paths = [line for line in status.splitlines() if line.strip()]
    return {
        "tool_commit_sha": commit,
        "tool_commit_dirty": bool(dirty_paths) if commit else None,
        "tool_commit_dirty_paths": dirty_paths[:50],
        "tool_branch": _git_output(repo, "rev-parse", "--abbrev-ref", "HEAD"),
    }


def _health_urls(llm_base_url: str) -> List[str]:
    """Candidate ``/health`` URLs for an OpenAI base URL.

    ``LLM_BASE_URL`` is an OpenAI surface (``http://host:port/v1``) while ``/health``
    lives at the server root, so the ``/v1`` suffix has to be dropped — trying both
    is cheaper than guessing which shape a given server exposes.
    """

    base = llm_base_url.rstrip("/")
    urls: List[str] = []
    split = urlsplit(base)
    if split.path.endswith("/v1"):
        root_path = split.path[: -len("/v1")].rstrip("/")
        urls.append(urlunsplit((split.scheme, split.netloc, root_path, "", "")) + "/health")
    urls.append(base + "/health")
    return urls


def probe_server(llm_base_url: Optional[str], timeout: float = 5.0) -> Optional[Dict[str, Any]]:
    """GET ``/health`` of the OpenAI-compatible server, or ``None`` if unreachable.

    The serving process is the only place that knows the base model id, the adapter
    path/hash and ``torch.cuda.max_memory_allocated``; the harness itself only sees
    an HTTP endpoint.  This probe is what makes those fields recordable at all.
    """

    if not llm_base_url:
        return None
    for url in _health_urls(llm_base_url):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        payload["probed_url"] = url
        return payload
    return None


def tool_source_provenance(tool_class: Any) -> Dict[str, Any]:
    """Module name, file and SHA-256 of the tool class that produced the predictions."""

    module_name = getattr(tool_class, "__module__", None)
    module = sys.modules.get(module_name) if module_name else None
    source = getattr(module, "__file__", None)
    digest = None
    if source:
        try:
            digest = _sha256(Path(source))
        except OSError:
            digest = None
    return {
        "tool_module": module_name,
        "tool_source_file": source,
        "tool_source_sha256": digest,
    }


def _server_metrics(server: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The subset of ``/health`` a run.json needs, with byte -> MiB convenience."""

    if not server:
        return {}
    peak = server.get("cuda_max_memory_allocated_bytes")
    return {
        "model_id": server.get("base_model"),
        "served_name": server.get("served_name"),
        "adapter_id": server.get("adapter"),
        "adapter_sha256": server.get("adapter_sha256"),
        "adapter_config_sha256": server.get("adapter_config_sha256"),
        "load_in_4bit": server.get("load_in_4bit"),
        "quantization": server.get("quantization"),
        "device": server.get("device"),
        "requests_served": server.get("requests_served"),
        "max_completion_tokens_observed": server.get("max_completion_tokens_observed"),
        # Thinking-mode provenance (dev_tools/serve_openai.py, Qwen3-class arms):
        # was the chat template asked to disable reasoning, did the requested switch
        # actually apply, and how often did a residual reasoning block have to be
        # stripped? Without these a "thinking" arm could be quoted as a normal one,
        # or vice versa.
        "thinking_mode": server.get("thinking_mode"),
        "thinking_template_supported": server.get("thinking_template_supported"),
        "thinking_flag_passed": server.get("thinking_flag_passed"),
        "thinking_stripped_count": server.get("thinking_stripped_count"),
        "cuda_max_memory_allocated_bytes": peak,
        "cuda_max_memory_allocated_mib": (round(peak / (1024.0 * 1024.0), 1) if peak else None),
        # Filled by dev_tools/guided_proxy.py.  A run that claims constrained
        # decoding must be able to show *which* grammar the server applied and how
        # many requests it actually touched, otherwise the claim is unverifiable.
        "guided_decoding": server.get("guided_decoding"),
        "device_memory": server.get("device_memory"),
        "server_latency": server.get("latency"),
    }


def _procedure_env() -> Dict[str, str]:
    """Every ``TEXT2SQL_*`` variable of this process, for the run.json record.

    The two procedure arms (retrieval few-shot, self-consistency) are switched on
    from the environment because ``benchmarks/base.py`` - which builds the tool as
    ``tool_cls(db_uri=..., llm_client=...)`` - is part of the sealed harness
    contract (ADR-001). Snapshotting the prefix is what lets an artifact say which
    arm produced its scores instead of leaving that to memory.
    """

    return {key: value for key, value in os.environ.items() if key.startswith("TEXT2SQL_")}


def run_provenance(
    plan: Dict[str, Any],
    tool_class: Any,
    *,
    server_before: Optional[Dict[str, Any]] = None,
    server_after: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Everything a run.json must carry so the configuration cannot be misquoted.

    Round 1 recorded the model name from an env var and nothing else: it did not
    say whether evidence was on, what the generation cap was, which adapter was
    loaded or what the tool revision was, so E1 and E1-fair looked identical on
    disk.  This block is the fix; it is merged into every run.json.
    """

    before = server_before or plan.get("server_probe")
    after = server_after or before
    effective = _server_metrics(after or before)
    block: Dict[str, Any] = {
        "use_evidence": plan.get("use_evidence"),
        "max_tokens": plan.get("max_tokens"),
        "max_tokens_source": plan.get("max_tokens_source"),
        "feature_prompt": plan.get("feature_prompt"),
        "sql_repair": plan.get("sql_repair"),
        "procedure_env": _procedure_env(),
        "tool_wrapper": _tool_wrapper_name(plan),
        "tool_commit_sha": plan.get("tool_commit_sha"),
        "tool_commit_dirty": plan.get("tool_commit_dirty"),
        "tool_commit_dirty_paths": plan.get("tool_commit_dirty_paths"),
        "tool_branch": plan.get("tool_branch"),
        **tool_source_provenance(tool_class),
        **effective,
        "server_before": before or None,
        "server_after": after or None,
    }
    if extra:
        block.update(extra)
    return block



FEATURE_BLOCK_MARKER = "\nFeature block:\n"


def feature_block_report(path: Optional[str]) -> Optional[Dict[str, Any]]:
    """Validate a feature-block file with the rules the matrix uses (lazy import).

    The block rides in every SQL prompt, so an empty or runaway file must be refused here
    rather than discovered after the run started.  Rules live in :mod:`dev_tools.matrix`
    (``feature_file_report``) so the assembler and the runner cannot drift apart.
    """

    if not path:
        return None
    try:  # package import
        from . import matrix
    except ImportError:  # pragma: no cover - direct script execution
        import matrix  # type: ignore[no-redef]
    report = matrix.feature_file_report(path)
    if report.get("problems"):
        raise RunRefused(
            f"--feature-prompt {path}: " + "; ".join(str(problem) for problem in report["problems"])
        )
    return {
        "file": path,
        "resolved": report.get("resolved"),
        "chars": report.get("chars"),
        "sha256_16": report.get("sha256_16"),
    }


def feature_prompt_tool(tool_class: Any, block: str) -> Any:
    """Wrap the real tool so every question carries the team's feature block.

    The tool builds its SQL prompt from the question string (``prompts.SQL_PROMPT_TEMPLATE``)
    and the graded harness owns that template, so the edit-free injection point is the
    question itself: ``<question>\\nFeature block:\\n<block>``.  The block *text* is
    byte-identical to what ``training/build_data.py --feature-prompt`` puts into the
    training prompt.  Nothing under ``src/`` or ``benchmarks/`` changes.

    Side effect worth knowing: the ambiguity gate reads the same string, so it sees the block
    too.  On BIRD the gate is ``birdsafe`` (never answers "ambiguous"), so the cost is prompt
    tokens only, recorded as ``feature_prompt.chars`` in ``run.json``.
    """

    marker = FEATURE_BLOCK_MARKER + block

    class FeaturePromptTool(tool_class):  # type: ignore[misc, valid-type]
        async def query(self, user_query: Any) -> Dict[str, Any]:
            text = user_query if isinstance(user_query, str) else str(user_query)
            return await super().query(f"{text.rstrip()}{marker}")

    FeaturePromptTool.__name__ = f"{getattr(tool_class, '__name__', 'Tool')}WithFeatureBlock"
    FeaturePromptTool.__qualname__ = FeaturePromptTool.__name__
    return FeaturePromptTool


def tool_for_run(plan: Dict[str, Any], tool_class: Any) -> Any:
    """The tool class the run actually uses (feature-block wrapper when the arm has one)."""

    feature_meta = plan.get("feature_prompt")
    if feature_meta:
        block = Path(feature_meta["resolved"]).read_text(encoding="utf-8")
        tool_class = feature_prompt_tool(tool_class, block)
    if plan.get("sql_repair") == "disabled":
        tool_class = no_repair_tool(tool_class)
    return tool_class


def _tool_wrapper_name(plan: Dict[str, Any]) -> Optional[str]:
    """Human-readable list of the tool wrappers an arm applied (None means bare)."""

    wrappers: List[str] = []
    if plan.get("feature_prompt"):
        wrappers.append("dev_tools.run_benchmark.feature_prompt_tool")
    if plan.get("sql_repair") == "disabled":
        wrappers.append("dev_tools.run_benchmark.no_repair_tool")
    return " + ".join(wrappers) if wrappers else None


def no_repair_tool(tool_class: Any) -> Any:
    """Wrap the real tool so it never spends a second call fixing its own SQL.

    Why a wrapper and not a config flag: the repair lives inside the tool's method
    body (``_generate_valid_sql`` and ``_refine_with_execution``), and the grammar
    arm's claim is "the *decoder* removed invalid SQL, not the tool's repair
    loop".  Sharing the same class with the arm that keeps repair would make the
    two configurations differ in a place ``run.json`` could not show.  Overriding
    the two methods keeps every other behaviour (prompts, ambiguity gate, schema
    pruning, execution dry-run) byte-identical between the arms.

    Both repair paths are disabled, because both are the tool's own repair:

    * the syntax repair (a second generation after the sqlglot gate rejected the
      first attempt), and
    * the execution repair (a second generation after PostgreSQL rejected valid
      SQL), while the dry-run itself is skipped too - with no repair available it
      could only cost time, never change a prediction.

    The sqlglot gate itself stays: it is the *measurement*, and disabling it would
    leave no record of how often the model produced unparseable SQL.
    """

    class NoRepairTool(tool_class):  # type: ignore[misc, valid-type]
        async def _generate_valid_sql(self, question: str, schema: str) -> Any:
            raw = await self._generate(question, schema)  # type: ignore[attr-defined]
            if raw is None:
                logger.warning("Generation produced no SQL; repair is disabled for this arm")
                return None
            validation = self._validate(raw)  # type: ignore[attr-defined]
            if not validation.ok:
                logger.info(
                    "SQL rejected by the gate (%s); repair is disabled for this arm", validation.error
                )
                return None
            return validation.sql

        async def _refine_with_execution(self, question: str, schema: str, sql: str) -> str:
            return sql

    NoRepairTool.__name__ = f"{getattr(tool_class, '__name__', 'Tool')}NoRepair"
    NoRepairTool.__qualname__ = NoRepairTool.__name__
    return NoRepairTool


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
    use_evidence: bool = False,
    max_new_tokens: Optional[int] = None,
    server: Optional[Dict[str, Any]] = None,
    feature_prompt: Optional[str] = None,
    sql_repair: str = "enabled",
) -> dict:
    """Resolve and validate everything a run needs; raises :class:`RunRefused`.

    ``server`` is the probe of the serving endpoint's ``/health`` (see
    :func:`probe_server`).  When it is available the generation cap it reports is
    the one that will actually bind — the harness never sends ``max_tokens``, so a
    ``--max-new-tokens`` that disagrees with the server is a refusal, not a comment.
    """

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
    if use_evidence and benchmark != "bird":
        # Silently ignoring the flag is exactly the class of defect this wrapper is
        # meant to prevent: BenchmarkAmbrosia never reads evidence.
        raise RunRefused(
            f"--use-evidence has no effect for --benchmark {benchmark} "
            f"(only benchmarks/bird.py::BenchmarkBIRD reads it); refusing to record a lie"
        )
    if feature_prompt and benchmark != "bird":
        # The feature block is injected into the SQL prompt; Ambrosia asks for an ambiguity
        # verdict and never builds one, so the flag would be silently inert.
        raise RunRefused(
            f"--feature-prompt has no SQL prompt to enter for --benchmark {benchmark} "
            f"(only benchmarks/bird.py asks for SQL); refusing to record a lie"
        )
    feature_meta = feature_block_report(feature_prompt)

    if sql_repair not in REPAIR_MODES:
        raise RunRefused(f"sql_repair must be one of {REPAIR_MODES}, got {sql_repair!r}")
    if sql_repair == "disabled" and benchmark == "ambrosia":
        # BenchmarkAmbrosia asks for an ambiguity verdict, never for SQL, so the
        # flag would be silently inert - exactly the class of defect this wrapper
        # exists to prevent.
        raise RunRefused(
            "--disable-sql-repair has no SQL generation to switch off for "
            f"--benchmark {benchmark}; refusing to record a flag that changes nothing"
        )

    server_cap = (server or {}).get("max_new_tokens")
    if max_new_tokens is not None and server_cap is not None and int(server_cap) != int(max_new_tokens):
        raise RunRefused(
            f"--max-new-tokens {max_new_tokens} does not match the serving endpoint "
            f"({(server or {}).get('probed_url') or llm_base_url} reports max_new_tokens={server_cap}); "
            f"the server cap is what actually binds, so one of the two is wrong"
        )
    resolved_max_tokens = int(server_cap) if server_cap is not None else max_new_tokens
    max_tokens_source = (
        "server:/health"
        if server_cap is not None
        else ("cli:--max-new-tokens" if max_new_tokens is not None else None)
    )

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
        **git_provenance(),
        "use_evidence": bool(use_evidence),
        "max_tokens": resolved_max_tokens,
        "max_tokens_source": max_tokens_source,
        "feature_prompt": feature_meta,
        "sql_repair": sql_repair,
        "server_probe": server or None,
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
            use_evidence=bool(plan.get("use_evidence")),
        )
        report = asyncio.run(benchmark.run(tool_for_run(plan, tool_class)))
    finally:
        os.chdir(previous_cwd)

    server_after = probe_server(plan.get("llm_base_url"))
    artifacts = sorted(path.name for path in run_dir.iterdir() if path.is_file())
    summary = {
        **plan,
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **run_provenance(
            plan,
            tool_class,
            server_before=plan.get("server_probe"),
            server_after=server_after,
        ),
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
        benchmark_kwargs: Dict[str, Any] = {
            "db_url": plan["db_url"],
            "query_file": str(query_file),
            "answer_file": str(answer_file),
            "llm_client": client,
        }
        if plan.get("benchmark") == "bird":
            # BenchmarkBIRD is the only harness that reads evidence; BenchmarkAmbrosia
            # has no such field, which is why plan_run refuses the combination.
            benchmark_kwargs["use_evidence"] = bool(plan.get("use_evidence"))
        benchmark = _Benchmark(**benchmark_kwargs)
        tool_class_used = tool_for_run(plan, tool_class)

        async def _predict() -> Dict[str, str]:
            tools = await benchmark.build(tool_class_used)  # type: ignore[arg-type]
            return await benchmark.predict(tools)

        predictions = asyncio.run(_predict())
        benchmark._save_predictions(predictions, "query_results.json")
        usage = benchmark.llm_client.get_usage() if hasattr(benchmark.llm_client, "get_usage") else None
    finally:
        os.chdir(previous_cwd)

    server_after = probe_server(plan.get("llm_base_url"))
    artifacts = sorted(path.name for path in run_dir.iterdir() if path.is_file())
    evidence_block: Dict[str, Any] = {}
    if plan.get("benchmark") == "bird":
        rows = json.loads(query_file.read_text(encoding="utf-8"))
        evidence_block = {
            "n_questions": len(rows),
            "n_with_evidence": sum(1 for row in rows if row.get("evidence")),
        }
    summary = {
        **plan,
        "mode": "predict-only",
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **run_provenance(
            plan,
            tool_class,
            server_before=plan.get("server_probe"),
            server_after=server_after,
            extra=evidence_block,
        ),
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
        "--use-evidence",
        action="store_true",
        help="set BenchmarkBIRD.use_evidence=True (the stock bird_benchmark.py setting: the prompt "
        "carries the BIRD evidence). Refused for --benchmark ambrosia, which never reads it.",
    )
    parser.add_argument(
        "--feature-prompt",
        default=None,
        help="UTF-8 file whose text is injected verbatim as 'Feature block:\\n<text>' into every "
        "SQL question (the same file training/build_data.py --feature-prompt consumes). "
        "Validated like the matrix does (non-empty, size-capped) and recorded in run.json; "
        "refused for --benchmark ambrosia, which asks for no SQL.",
    )
    parser.add_argument(
        "--max-new-tokens",
        type=int,
        default=None,
        help="generation cap the run is claimed to use; must match the serving endpoint's "
        "/health max_new_tokens (the server's cap is the one that actually binds) and is "
        "recorded in run.json",
    )
    parser.add_argument(
        "--disable-sql-repair",
        action="store_true",
        help="wrap the tool so it never spends a second generation repairing rejected SQL "
        "(both the sqlglot syntax repair and the execution repair); the gate itself stays, so "
        "unparseable generations stay visibly invalid instead of being fixed away. Refused for "
        "--benchmark ambrosia, which never asks for SQL.",
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

    # Probe the serving endpoint before planning: its /health is the only source of
    # the real base model id, the adapter and its hash, the effective generation cap
    # and torch.cuda.max_memory_allocated.  A real run requires it; --check just says
    # so, because it must stay spend-free and side-effect-free.
    probe = probe_server(args.llm_base_url)

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
            use_evidence=args.use_evidence,
            max_new_tokens=args.max_new_tokens,
            server=probe,
            feature_prompt=args.feature_prompt,
            sql_repair="disabled" if args.disable_sql_repair else "enabled",
        )
    except RunRefused as exc:
        print(f"refusing to run: {exc}", file=sys.stderr)
        return 2

    print("run plan:")
    for key in ("run_id", "run_dir", "query_file", "answer_file", "db_url", "tool_class",
                "llm_base_url", "llm_model", "git_commit", "tool_commit_dirty", "tool_branch",
                "use_evidence", "max_tokens", "max_tokens_source", "feature_prompt", "sql_repair"):
        print(f"  {key}: {plan[key]}")
    if probe:
        print("serving endpoint (/health):")
        for key in ("probed_url", "served_name", "base_model", "adapter", "adapter_sha256",
                    "load_in_4bit", "max_new_tokens", "cuda_max_memory_allocated_bytes"):
            print(f"  {key}: {probe.get(key)}")
    else:
        print(f"serving endpoint: /health unreachable at {args.llm_base_url} (no model/adapter provenance)")
    if args.check:
        print("--check: nothing was started, nothing was written")
        return 0

    if probe is None:
        print(
            "refusing to run: the serving endpoint's /health did not answer, so the run.json "
            "could not record the model id, the adapter hash, the effective max_tokens or "
            "cuda_max_memory_allocated (the round-1 defect). Start dev_tools/serve_openai.py first.",
            file=sys.stderr,
        )
        return 2

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




