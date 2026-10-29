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
  the cap" is checkable from the artifact instead of re-read from a log;
* ``schemas_bytes_total``, ``schemas_empty_count`` (plus the database count, the
  names of the empty databases and whether ``--allow-empty-schemas`` was used) —
  the health of the schema fetch, because a failed fetch is silent otherwise: the
  harness renders ``""`` for every unreadable database and still runs the whole
  benchmark.  A run with an empty schema is **refused** (exit 2) unless the empty
  case was asked for explicitly, and the refusal is written into ``run.json``.

Safety
------
Nothing is spent until the caller passes ``--allow-llm-spend``.  Without it the
wrapper only prints the plan (the resolution of files, endpoint and tool class)
and exits 0 — that is also what ``--check`` does explicitly.  The API key is
never accepted on the command line: pass ``--llm-api-key-env NAME`` (default
``LLM_API_KEY``).

The tunnel must be up: the harness needs ``DB_USER``/``DB_PASS`` and a reachable
PostgreSQL (``baseline_agent.py``/``bird_benchmark.py`` pass ``BENCHMARK_DB_URL``).
The schema guard below checks that the *database side* of that sentence is true
before the run (one probe) and after it (the produced ``db_schemas.json``).
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
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urlsplit, urlunsplit

try:  # package import
    from . import paths
except ImportError:  # pragma: no cover - direct script execution
    import paths  # type: ignore[no-redef]

__all__ = [
    "RunRefused",
    "build_client",
    "main",
    "plan_run",
    "probe_database_schema",
    "read_schema_health",
    "run_harness",
    "schema_link_summary",
    "schema_link_summary_from_tools",
    "verify_schemas",
]

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


def schema_link_summary(run_dir: Path, *, log_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Aggregate the per-question schema-linking records into one run.json block.

    The tool records, per question, which tables/columns it kept, how many
    characters that was and the SHA-256 of the exact block
    (``TEXT2SQL_PROCEDURE_LOG``, one JSONL line per question).  ``run.json`` is the
    run-level artefact, so the arm's effect belongs there too: without this a
    "schema linking" arm would be indistinguishable from the default one on disk
    except by its accuracy, which is exactly the kind of unverifiable claim the
    round-2 recorder work exists to prevent.

    Returns ``None`` when the arm never ran (no log, no records, all ``off``), so
    the default path keeps a clean run.json.
    """

    path = Path(log_name or os.environ.get("TEXT2SQL_PROCEDURE_LOG") or "procedures.jsonl")
    if not path.is_absolute():
        path = Path(run_dir) / path
    if not path.is_file():
        return None

    links: List[Dict[str, Any]] = []
    questions = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        questions += 1
        link = record.get("schema_link")
        if isinstance(link, dict):
            links.append(link)
    if not links:
        return None

    def _numbers(key: str) -> List[float]:
        values: List[float] = []
        for link in links:
            try:
                values.append(float(link.get(key) or 0))
            except (TypeError, ValueError):
                continue
        return values

    def _mean(values: List[float]) -> Optional[float]:
        return round(sum(values) / len(values), 1) if values else None

    tables = _numbers("tables_selected")
    columns = _numbers("columns_selected")
    chars = _numbers("schema_chars")
    hashes = {str(link.get("schema_sha256")) for link in links if link.get("schema_sha256")}
    return {
        "modes": sorted({str(link.get("mode")) for link in links}),
        "questions": questions,
        "questions_with_link": len(links),
        "applied": sum(1 for link in links if link.get("applied")),
        "fallbacks": sum(1 for link in links if link.get("fallback")),
        "over_budget": sum(1 for link in links if link.get("over_budget")),
        "tables_selected_mean": _mean(tables),
        "tables_selected_max": max(tables) if tables else None,
        "columns_selected_mean": _mean(columns),
        "schema_chars_mean": _mean(chars),
        "schema_chars_max": max(chars) if chars else None,
        "distinct_schema_sha256": len(hashes),
        "embedders": sorted({str(link.get("embedder")) for link in links}),
        "source": "procedure-log",
        "procedure_log": str(path),
    }


def schema_link_summary_from_tools(tools: Iterable[Any]) -> Optional[Dict[str, Any]]:
    """Merge the in-process per-tool schema-link aggregates for ``run.json``.

    Why this exists: ``schema_link_summary`` reads the optional procedure log, and the
    round-3 arms did not set ``TEXT2SQL_PROCEDURE_LOG``, so ``run.json.schema_link`` was
    ``null`` while the arm was running and the prompt cost had to be replayed offline.
    Every tool now keeps its own counters (``schema_link_aggregate``); this function
    sums them across the per-database tools the harness built, so the record exists
    regardless of the log. The tool is the source of truth, the log stays a detailed
    per-question trace.

    Returns ``None`` when no tool rendered a linked prompt (the arm is off), which keeps
    a default run's ``run.json`` byte-identical to today's.
    """

    aggregates: List[Dict[str, Any]] = []
    for tool in tools:
        getter = getattr(tool, "schema_link_aggregate", None)
        if not callable(getter):
            continue
        try:
            record = getter()
        except Exception:  # pragma: no cover - a broken tool must not lose the run record
            logger.exception("schema-link aggregate failed for %r; skipping it", type(tool).__name__)
            continue
        if isinstance(record, dict) and int(record.get("questions") or 0) > 0:
            aggregates.append(record)
    if not aggregates:
        return None

    questions = sum(int(record.get("questions") or 0) for record in aggregates)

    def _weighted_mean(key: str) -> Optional[float]:
        total = 0.0
        for record in aggregates:
            value = record.get(key)
            if value is None:
                continue
            total += float(value) * int(record.get("questions") or 0)
        return round(total / questions, 1) if questions else None

    def _sum(key: str) -> int:
        return sum(int(record.get(key) or 0) for record in aggregates)

    def _max(key: str) -> Optional[float]:
        values = [float(record[key]) for record in aggregates if record.get(key) is not None]
        return max(values) if values else None

    modes = sorted({str(mode) for record in aggregates for mode in (record.get("modes") or [])})
    embedders = sorted(
        {str(item) for record in aggregates for item in (record.get("embedders") or [])}
    )
    return {
        "modes": modes,
        "questions": questions,
        "questions_with_link": _sum("questions_with_link"),
        "applied": _sum("applied"),
        "fallbacks": _sum("fallbacks"),
        "over_budget": _sum("over_budget"),
        "tables_selected_mean": _weighted_mean("tables_selected_mean"),
        "tables_selected_max": _max("tables_selected_max"),
        "columns_selected_mean": _weighted_mean("columns_selected_mean"),
        "schema_chars_mean": _weighted_mean("schema_chars_mean"),
        "schema_chars_max": _max("schema_chars_max"),
        # Each tool covers one database, so per-tool distinct counts do not overlap in
        # practice; the sum is the run-level count (documented, not silently assumed).
        "distinct_schema_sha256": _sum("distinct_schema_sha256"),
        "annotations_total": _sum("annotations_total"),
        "join_edges_total": _sum("join_edges_total"),
        "join_block_chars_total": _sum("join_block_chars_total"),
        "embedders": embedders,
        "source": "in-process",
        "procedure_log": None,
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


def recording_tool(tool_class: Any, registry: List[Any]) -> Any:
    """Subclass that registers every tool instance the harness builds.

    The sealed harness (``benchmarks/base.py``) builds one tool per database and hands
    them to ``predict``; ``run_harness`` never sees that dict.  Registering the
    instances from the constructor is the smallest way to ask them, after the run, for
    the in-process schema-link aggregate — so ``run.json`` no longer depends on the
    optional procedure log being enabled (the round-3 provenance gap). The subclass
    adds no behaviour to ``query``.
    """

    class RecordingTool(tool_class):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            registry.append(self)

    RecordingTool.__name__ = f"{getattr(tool_class, '__name__', 'Tool')}Recorded"
    RecordingTool.__qualname__ = RecordingTool.__name__
    return RecordingTool


def tool_for_run(
    plan: Dict[str, Any], tool_class: Any, *, registry: Optional[List[Any]] = None
) -> Any:
    """The tool class the run actually uses (feature-block wrapper when the arm has one).

    ``registry`` is an optional list that receives every constructed tool instance;
    the runners use it to read the per-run schema-link aggregate from the objects that
    actually rendered the prompts.
    """

    feature_meta = plan.get("feature_prompt")
    if feature_meta:
        block = Path(feature_meta["resolved"]).read_text(encoding="utf-8")
        tool_class = feature_prompt_tool(tool_class, block)
    if plan.get("sql_repair") == "disabled":
        tool_class = no_repair_tool(tool_class)
    if registry is not None:
        tool_class = recording_tool(tool_class, registry)
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
    allow_empty_schemas: bool = False,
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
        "allow_empty_schemas": bool(allow_empty_schemas),
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
    # Before the harness runs: one database through the tool's own credentials.  A
    # wrong credential / down tunnel would otherwise produce 241 answers from an
    # empty schema and a plausible ledger (see SCHEMA_MIN_CHARS).
    allow_empty_schemas = bool(plan.get("allow_empty_schemas"))
    schema_probe = probe_database_schema(
        plan.get("db_url"),
        first_db_id(query_file),
        user=os.environ.get("DB_USER"),
        password=os.environ.get("DB_PASS"),
    )
    probe_problem = preflight_schema_problem(schema_probe, allow_empty_schemas=allow_empty_schemas)
    if probe_problem is not None:
        raise RunRefused(probe_problem)
    if schema_probe.get("skipped"):
        logger.warning("pre-flight schema probe skipped: %s", schema_probe.get("error"))
    # The harness opens these paths *after* we chdir into the run directory, so
    # they must be absolute by then.
    query_file = Path(query_file).resolve()
    answer_file = Path(answer_file).resolve()
    previous_cwd = Path.cwd()
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tool_registry: List[Any] = []
    try:
        os.chdir(run_dir)
        benchmark = BenchmarkBIRD(
            db_url=plan["db_url"],
            query_file=str(query_file),
            answer_file=str(answer_file),
            llm_client=client,
            use_evidence=bool(plan.get("use_evidence")),
        )
        report = asyncio.run(
            benchmark.run(tool_for_run(plan, tool_class, registry=tool_registry))
        )
    finally:
        os.chdir(previous_cwd)

    server_after = probe_server(plan.get("llm_base_url"))
    artifacts = sorted(path.name for path in run_dir.iterdir() if path.is_file())
    # After the harness ran: the schemas it actually prompted with.  A run whose
    # fetch failed is recorded and then refused, so it can never be quoted as a
    # model result while the evidence stays on disk.
    schema_block, schema_refusal = verify_schemas(
        run_dir, allow_empty_schemas=allow_empty_schemas
    )
    # In-process aggregate first (it exists without the optional procedure log); the
    # log-based summary stays as the detailed-trace fallback for older runs.
    schema_link_block = schema_link_summary_from_tools(tool_registry) or schema_link_summary(run_dir)
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
        **schema_block,
        "schema_probe": schema_probe,
        "schema_link": schema_link_block,
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
    if schema_refusal is not None:
        summary["status"] = "refused"
        summary["refusal"] = schema_refusal
    (run_dir / "run.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    if schema_refusal is not None:
        raise RunRefused(schema_refusal)
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
    allow_empty_schemas = bool(plan.get("allow_empty_schemas"))
    schema_probe = probe_database_schema(
        plan.get("db_url"),
        first_db_id(query_file),
        user=os.environ.get("DB_USER"),
        password=os.environ.get("DB_PASS"),
    )
    probe_problem = preflight_schema_problem(schema_probe, allow_empty_schemas=allow_empty_schemas)
    if probe_problem is not None:
        raise RunRefused(probe_problem)
    if schema_probe.get("skipped"):
        logger.warning("pre-flight schema probe skipped: %s", schema_probe.get("error"))
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
        tool_registry: List[Any] = []
        tool_class_used = tool_for_run(plan, tool_class, registry=tool_registry)

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
    schema_block, schema_refusal = verify_schemas(
        run_dir, allow_empty_schemas=allow_empty_schemas
    )
    # Same rule as run_harness: the tool's in-process counters are the source of truth,
    # the optional procedure log is only a detailed trace.
    schema_link_block = schema_link_summary_from_tools(tool_registry) or schema_link_summary(run_dir)
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
        **schema_block,
        "schema_probe": schema_probe,
        "schema_link": schema_link_block,
        "artifacts": artifacts,
        "n_predictions": len(predictions),
        "llm_usage": usage,
        "note": "scoring is done by dev_tools.fixed_eval.py; the harness evaluation is skipped on purpose",
    }
    if schema_refusal is not None:
        summary["status"] = "refused"
        summary["refusal"] = schema_refusal
    (run_dir / "run.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    if schema_refusal is not None:
        raise RunRefused(schema_refusal)
    return summary


# ---------------------------------------------------------------------------
# Schema guard: a failed schema fetch must never look like a model result
# ---------------------------------------------------------------------------


SCHEMA_MIN_CHARS: int = 20
"""A schema block shorter than this cannot describe a database: it is a failed fetch.

A real light schema of a single table is hundreds of characters
(``TABLE t`` + one ``  - column (TYPE)`` line per column), so 20 is generous and
still catches ``""`` and a stub like ``"TABLE"``.
"""

SCHEMA_FILENAME: str = "db_schemas.json"
"""The file ``benchmarks/base.py`` writes into the run directory after ``build()``."""

SCHEMA_EMPTY_NAMES_SHOWN: int = 8
"""How many empty database names the artefact and the refusal message list."""


def read_schema_health(path: Path, *, min_chars: int = SCHEMA_MIN_CHARS) -> Dict[str, Any]:
    """Describe how healthy the harness's ``db_schemas.json`` fetch was.

    Why this exists: ``benchmarks/base.py::_dump_db_schemas_json`` writes
    ``{db_id: tool.db_schema}`` and the tool renders ``""`` when the schema could
    not be read (wrong credentials, tunnel down).  The harness then runs the whole
    benchmark with empty prompts and produces a plausible-looking ledger.  Round 2
    hit this twice (``runs/t2/r2-scale7b-ev`` and ``runs/ood-base7b-292``, the
    latter scoring 5.14 % where the same model with a real schema scored 71.67 %).

    Every count needed to detect that is returned, whether or not the file is
    usable, so ``run.json`` can record the health of the fetch instead of only the
    verdict.  A block counts as present only when it is a string with at least
    ``min_chars`` non-space characters.
    """

    health: Dict[str, Any] = {
        "schema_file": str(path),
        "schemas_present": Path(path).is_file(),
        "schemas_ok": False,
        "schemas_db_count": 0,
        "schemas_bytes_total": 0,
        "schemas_chars_total": 0,
        "schemas_empty_count": 0,
        "schemas_empty_databases": [],
        "schemas_min_chars": int(min_chars),
        "schemas_problem": "",
    }
    if not health["schemas_present"]:
        health["schemas_problem"] = f"{Path(path).name} was not written by the harness"
        return health

    try:
        raw = Path(path).read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        health["schemas_problem"] = f"{Path(path).name} is unreadable: {type(exc).__name__}: {exc}"
        return health

    health["schemas_bytes_total"] = len(raw)
    if not isinstance(payload, dict):
        health["schemas_problem"] = (
            f"{Path(path).name} is a {type(payload).__name__}, not an object of "
            f"{{db_id: schema}}"
        )
        return health

    health["schemas_db_count"] = len(payload)
    empty: List[str] = []
    chars_total = 0
    for db_id, schema in payload.items():
        text = schema if isinstance(schema, str) else ""
        chars_total += len(text.strip())
        if len(text.strip()) < int(min_chars):
            empty.append(str(db_id))
    health["schemas_chars_total"] = chars_total
    health["schemas_empty_count"] = len(empty)
    health["schemas_empty_databases"] = sorted(empty)[:SCHEMA_EMPTY_NAMES_SHOWN]

    if not payload:
        health["schemas_problem"] = f"{Path(path).name} lists no databases at all"
    elif empty:
        health["schemas_problem"] = (
            f"{len(empty)}/{len(payload)} database schemas are empty or shorter than "
            f"{int(min_chars)} characters"
        )
    else:
        health["schemas_ok"] = True
    return health


def schema_refusal_message(
    health: Dict[str, Any], *, allow_empty_schemas: bool, run_dir: Path
) -> Optional[str]:
    """The message to refuse a run with, or ``None`` when the schema fetch is sound.

    ``allow_empty_schemas`` turns the refusal off but never hides the evidence: the
    counts are still written to ``run.json`` (``schemas_allowed_empty: true``), so a
    reader can tell a deliberate empty-schema experiment from a broken one.
    """

    if health.get("schemas_ok") or allow_empty_schemas:
        return None
    names = ", ".join(health.get("schemas_empty_databases") or [])
    first = f" First: {names}." if names else ""
    return (
        f"schema build failed: {health.get('schemas_problem')}.{first} "
        f"Refusing to report this run ({run_dir}) as a model result: the model answered "
        f"from EMPTY schema prompts, which is a credential/tunnel defect, not a number. "
        f"Check DB_USER/DB_PASS/BENCHMARK_DB_URL — the `.env` quoting trap turns "
        f"readonly_user into \"\"readonly_user\"\" and CRLF line endings do the same — and "
        f"re-run with a new --run-id. Pass --allow-empty-schemas only for a deliberate "
        f"empty-schema experiment; that choice is recorded in run.json."
    )


def verify_schemas(
    run_dir: Path, *, allow_empty_schemas: bool
) -> tuple[Dict[str, Any], Optional[str]]:
    """Read the schema file a run produced; return (``run.json`` block, refusal)."""

    health = read_schema_health(Path(run_dir) / SCHEMA_FILENAME)
    block = {**health, "schemas_allowed_empty": bool(allow_empty_schemas)}
    return block, schema_refusal_message(
        health, allow_empty_schemas=allow_empty_schemas, run_dir=Path(run_dir)
    )


def first_db_id(query_file: Path) -> Optional[str]:
    """The first ``db_id`` of a query file, for the pre-flight schema probe."""

    try:
        rows = json.loads(Path(query_file).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("db_id"), str) and row["db_id"].strip():
            return row["db_id"].strip()
    return None


def _redact(text: str, secret: Optional[str]) -> str:
    """Never let a password reach ``run.json`` through a driver error message."""

    if secret and text and secret in text:
        return text.replace(secret, "***")
    return text


def probe_database_schema(
    db_url: Optional[str], db_id: Optional[str], *, user: Optional[str], password: Optional[str]
) -> Dict[str, Any]:
    """Read one database's table list through the credentials the tool will use.

    This is the *before* half of the guard: it mirrors the tool's own DSN
    (``postgresql+psycopg://user:password@db_url/db_id``) and its light-schema
    reflection, so a wrong credential or a down tunnel refuses the run before a
    single model call instead of after 241 of them.

    It never raises: a probe is evidence, and the caller decides.  ``skipped`` marks
    the case where no probe was possible (no sqlalchemy, no db_id) — that must not
    be confused with a failed probe.
    """

    detail: Dict[str, Any] = {
        "db_id": db_id,
        "probed": False,
        "skipped": False,
        "tables": None,
        "error": "",
    }
    if not db_url or not db_id:
        detail["skipped"] = True
        detail["error"] = "no db_url/db_id to probe"
        return detail
    try:
        from sqlalchemy import create_engine, inspect  # noqa: PLC0415
    except ImportError:  # pragma: no cover - sqlalchemy is a project dependency
        detail["skipped"] = True
        detail["error"] = "sqlalchemy is not installed; pre-flight schema probe skipped"
        return detail

    dsn = f"postgresql+psycopg://{user or ''}:{password or ''}@{db_url}/{db_id}"
    try:
        engine = create_engine(dsn, pool_pre_ping=True)
        try:
            inspector = inspect(engine)
            tables = inspector.get_table_names(schema="public") or inspector.get_table_names()
        finally:
            engine.dispose()
    except Exception as exc:  # noqa: BLE001 - a probe reports, it never raises
        detail["error"] = _redact(f"{type(exc).__name__}: {exc}", password)
        return detail

    detail["probed"] = True
    detail["tables"] = len(tables)
    if not tables:
        detail["error"] = "these credentials see no tables"
    return detail


def preflight_schema_problem(
    probe: Dict[str, Any], *, allow_empty_schemas: bool
) -> Optional[str]:
    """Refuse the run on a failed probe; a skipped probe only warns."""

    if allow_empty_schemas or probe.get("skipped") or probe.get("probed"):
        return None
    return (
        f"pre-flight schema probe failed for {probe.get('db_id')!r}: {probe.get('error')}. "
        f"The harness would run every question against an empty schema and produce a "
        f"plausible but meaningless ledger (the round-2 defect: 5.14 % instead of 71.67 %). "
        f"Check the tunnel and DB_USER/DB_PASS — the `.env` quoting/CRLF trap turns "
        f"readonly_user into \"\"readonly_user\"\". Pass --allow-empty-schemas only for a "
        f"deliberate empty-schema experiment."
    )


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
        "--allow-empty-schemas",
        action="store_true",
        help="do not refuse a run whose db_schemas.json is empty or truncated. Only for a "
        "deliberate empty-schema experiment: the flag, schemas_empty_count and "
        "schemas_bytes_total are recorded in run.json, so the choice can never be mistaken "
        "for a working schema fetch.",
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
            allow_empty_schemas=args.allow_empty_schemas,
        )
    except RunRefused as exc:
        print(f"refusing to run: {exc}", file=sys.stderr)
        return 2

    print("run plan:")
    for key in ("run_id", "run_dir", "query_file", "answer_file", "db_url", "tool_class",
                "llm_base_url", "llm_model", "git_commit", "tool_commit_dirty", "tool_branch",
                "use_evidence", "max_tokens", "max_tokens_source", "feature_prompt", "sql_repair",
                "allow_empty_schemas"):
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
    print(
        f"schemas: {summary.get('schemas_db_count')} database(s), "
        f"{summary.get('schemas_bytes_total')} bytes, "
        f"{summary.get('schemas_empty_count')} empty"
        + (" (allowed by --allow-empty-schemas)" if summary.get("schemas_allowed_empty") else "")
    )
    print(f"artifacts in {summary['run_dir']}: {', '.join(summary['artifacts']) or '(none)'}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())




