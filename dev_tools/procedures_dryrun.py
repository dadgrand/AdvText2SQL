"""Offline dry run for the two procedure arms: retrieval few-shot and self-consistency.

What this proves
----------------
Both arms can be switched on end to end with **no database, no GPU, no network
and no API key**: the retrieval bank is built from the repo's train file with the
dependency-free hashing embedder, the LLM is the deterministic
``dev_tools.stub_llm.StubLLM``, and the "database" is an injected engine that
raises ``ConnectionRefusedError`` on connect - so the self-consistency round
exercises its documented *database-unreachable* fallback (majority over
normalised SQL text) instead of a result-set majority.

It is a plumbing proof, not a measurement: the stub answers are canned, so the
accuracy of the arms is measured by ``eval-run`` against the real endpoint. What
is checked here is what that measurement depends on - the injected example block
is non-empty and recorded, the vote resolved, the artefacts were written, and
nothing opened a socket.

Usage
-----
::

    python -m dev_tools.procedures_dryrun                 # writes runs/t16-dryrun/
    python -m dev_tools.procedures_dryrun --out runs/t16-dryrun-check --no-evidence
    python -m dev_tools.procedures_dryrun --embedder sentence-transformer   # expect unavailable

Exit codes: ``0`` arms were enabled and produced artefacts, ``2`` an arm was
requested but did not activate (the run is then *not* a proof and says why).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

try:  # package import (``python -m dev_tools.procedures_dryrun``)
    from .paths import DATA_DIR, REPO_ROOT
    from .stub_llm import StubLLM
except ImportError:  # pragma: no cover - direct script execution
    from paths import DATA_DIR, REPO_ROOT  # type: ignore[no-redef]
    from stub_llm import StubLLM  # type: ignore[no-redef]

DEFAULT_OUT: Path = REPO_ROOT / "runs" / "t16-dryrun"
DEFAULT_K: int = 2
DEFAULT_N: int = 3
DEFAULT_TEMPERATURE: float = 0.7
DEFAULT_SEED: int = 0

#: Canned, deterministic SQL answers. Two of the three agree as text, so the
#: unreachable-database fallback has something to vote about. Every string must
#: pass the tool's PostgreSQL gate, otherwise the arm would report "no valid SQL".
CANNED_SQL: Sequence[str] = (
    'SELECT COUNT(*) FROM frpm WHERE "County Name" = \'Alameda\'',
    'SELECT COUNT("County Name") FROM frpm',
    'SELECT COUNT(*) FROM frpm WHERE "County Name" = \'Alameda\'',
)


class UnreachableEngine:
    """Engine stand-in that refuses every connection, like a down database.

    Injected instead of a real DSN on purpose: the dry run must not depend on
    (or wait for) a refused socket, and the tool classifies a
    ``ConnectionRefusedError`` as "unreachable" exactly as it does a live refusal.
    """

    def __init__(self) -> None:
        self.connections = 0

    def connect(self) -> Any:
        self.connections += 1
        raise ConnectionRefusedError("dry run: the database is deliberately unreachable")


def _bootstrap_import_path() -> None:
    """Put ``src`` on the path and stub ``fastmcp`` (same trick as the test suite)."""

    src = REPO_ROOT / "src"
    for path in (str(REPO_ROOT), str(src)):
        if path not in sys.path:
            sys.path.insert(0, path)
    try:
        import fastmcp  # noqa: F401
    except ImportError:
        stub = types.ModuleType("fastmcp")
        stub.FastMCP = type("FastMCP", (), {})  # type: ignore[attr-defined]
        sys.modules["fastmcp"] = stub


_bootstrap_import_path()

from adv_text2sql.mcp_servers.text2sql_tool.src.retrieval import DEFAULT_BANK, DEFAULT_MODEL  # noqa: E402
from adv_text2sql.mcp_servers.text2sql_tool.src.text2sql_implementation import (  # noqa: E402
    Text2SQLConfig,
    Text2SQLGenerator,
)

SCHEMA = (
    "TABLE frpm\n"
    "  - County Name (TEXT)\n"
    "  - Enrollment (K-12) (TEXT)\n"
    "\n"
    "TABLE satscores\n"
    "  - cds (TEXT)\n"
    "  - AvgScrMath (INTEGER)"
)


def load_questions(limit: int, *, with_evidence: bool) -> List[Dict[str, Any]]:
    """Dev questions used as *inputs*, preferring ones that carry evidence text.

    The questions come from ``data/bird_large.json`` because a dry run must use
    the benchmark's own inputs; the few-shot *examples* still come from the train
    bank only. Nothing here reads a dev question into the bank.
    """

    items = json.loads((DATA_DIR / "bird_large.json").read_text(encoding="utf-8"))
    with_text = [item for item in items if str(item.get("evidence") or "").strip()]
    without = [item for item in items if not str(item.get("evidence") or "").strip()]
    chosen = (with_text + without if with_evidence else without + with_text)[:limit]
    return chosen


def harness_question(item: Dict[str, Any], *, with_evidence: bool) -> str:
    """Rebuild the exact string ``benchmarks/bird.py`` passes when evidence is on."""

    if not with_evidence:
        return str(item["question"])
    return (
        f"question: {item['question']}, "
        f"evidence (may be empty): {item.get('evidence') or ''}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dev_tools.procedures_dryrun",
        description=(
            "Enable both procedure arms offline (no database, no GPU, no network) and "
            "prove they inject examples, vote, and write their metadata."
        ),
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="artifact directory (default: runs/t16-dryrun)")
    parser.add_argument("--bank", default=str(DEFAULT_BANK), help="train-only few-shot bank source")
    parser.add_argument(
        "--embedder",
        default="hash",
        choices=("hash", "sentence-transformer"),
        help="hash needs no weights (default); sentence-transformer needs local weights",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="local sentence-transformer weights")
    parser.add_argument("--k", type=int, default=DEFAULT_K, help=f"examples per prompt (default: {DEFAULT_K})")
    parser.add_argument(
        "--min-similarity", type=float, default=None, help="override the embedder's own threshold"
    )
    parser.add_argument(
        "--evidence",
        dest="evidence",
        action="store_true",
        default=True,
        help="wrap questions the way benchmarks/bird.py does with use_evidence (default: on)",
    )
    parser.add_argument("--no-evidence", dest="evidence", action="store_false", help="plain questions")
    parser.add_argument("--n", type=int, default=DEFAULT_N, help=f"candidates per question (default: {DEFAULT_N})")
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--questions", type=int, default=2, help="how many dev questions to run")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = REPO_ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    procedure_log = out_dir / "procedures.jsonl"
    if procedure_log.exists():
        procedure_log.unlink()

    questions = load_questions(max(1, int(args.questions)), with_evidence=bool(args.evidence))
    engine = UnreachableEngine()
    config = Text2SQLConfig(
        ambiguous_mode="off",
        max_llm_retries=0,
        retry_backoff_s=0.0,
        execution_feedback=True,
        db_id="california_schools",
        retrieval_few_shot=True,
        retrieval_k=int(args.k),
        retrieval_min_similarity=args.min_similarity,
        retrieval_include_evidence=bool(args.evidence),
        retrieval_bank=str(args.bank),
        retrieval_embedder=str(args.embedder),
        retrieval_model=str(args.model),
        self_consistency=True,
        self_consistency_n=int(args.n),
        self_consistency_temperature=float(args.temperature),
        self_consistency_seed=int(args.seed),
        procedure_log=str(procedure_log),
    )
    stub = StubLLM(responses=list(CANNED_SQL), default=CANNED_SQL[0])
    tool = Text2SQLGenerator(
        db_uri="postgresql+psycopg://dry-run@localhost:1/california_schools",
        llm_client=stub,
        schema=SCHEMA,
        engine=engine,
        config=config,
    )
    tool.build()

    retrieval_status = tool.procedure_metadata()["retrieval_few_shot"]
    if not retrieval_status.get("available"):
        print("FAILED: the retrieval arm did not activate:", retrieval_status.get("reason"))
        return 2

    prompts: List[Dict[str, Any]] = []
    results: List[Dict[str, Any]] = []
    for number, item in enumerate(questions, start=1):
        question = harness_question(item, with_evidence=bool(args.evidence))
        result = asyncio.run(tool.query(question))
        state = tool.procedure_metadata()
        retrieval = state["retrieval_few_shot"].get("last") or {}
        consistency = state["self_consistency"].get("last") or {}
        prompt = stub.history[-1]["messages"][-1]["content"] if stub.history else ""
        block = retrieval.get("block") or ""
        if not block:
            print(f"FAILED: question {number} injected no worked example (arm active but empty)")
            return 2
        if not consistency or not consistency.get("drawn"):
            print(f"FAILED: question {number} drew no self-consistency candidate")
            return 2
        prompts.append(
            {
                "question": question,
                "user_prompt": prompt,
                "examples_block": block,
                "block_sha256": retrieval.get("block_sha256"),
            }
        )
        results.append(
            {
                "question_id": item.get("question_id"),
                "db_id": item.get("db_id"),
                "question": question,
                "status": result.get("status"),
                "sql": result.get("query"),
                "retrieval": {
                    key: value
                    for key, value in retrieval.items()
                    if key in {"injected", "block_sha256", "query_used_evidence", "examples"}
                },
                "self_consistency": {
                    key: value
                    for key, value in consistency.items()
                    if key in {"n", "drawn", "temperature", "seed", "database_executed", "selection"}
                },
            }
        )

        selection = consistency.get("selection", {})
        print(f"-- question {number}/{len(questions)} (qid={item.get('question_id')}, db={item.get('db_id')})")
        print(f"   retrieval: {retrieval.get('injected')} example(s) injected, block sha256 {retrieval.get('block_sha256')}")
        print(f"   candidates: {consistency.get('drawn')} drawn, database executed: {consistency.get('database_executed')}")
        print(
            f"   vote: {selection.get('mode')} -> candidate #{selection.get('selected_index')} "
            f"with {selection.get('vote_count')} vote(s) [{selection.get('detail')}]"
        )
        print(f"   status: {result.get('status')}, selected SQL: {result.get('query')}")

    first_prompt = prompts[0]["examples_block"] if prompts else ""
    print("\n== worked-example block, first question (verbatim) ==")
    print(first_prompt)
    print("== end of block ==")

    summary = {
        "tool": "dev_tools.procedures_dryrun",
        "statement": (
            "Both procedure arms were enabled with no database, no GPU, no network and no "
            "API key. The LLM is dev_tools.stub_llm.StubLLM (canned answers); the engine is "
            "an injected ConnectionRefusedError stub. This is a plumbing proof, not a score."
        ),
        "invoked": vars(args),
        "environment": {
            "database": "injected unreachable engine; 0 sockets",
            "llm": "dev_tools.stub_llm.StubLLM (deterministic, offline)",
            "gpu": "none",
            "embedder": args.embedder,
        },
        "bank": retrieval_status,
        "questions": results,
        "database_connection_attempts": engine.connections,
        "stub_calls": stub.get_usage()["calls"],
        "artifacts": ["dry_run.json", "prompts.txt", "procedures.jsonl"],
        "written_at_out": str(out_dir),
    }
    (out_dir / "dry_run.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    (out_dir / "prompts.txt").write_text(
        "\n\n".join(
            f"### question {index}\n{entry['user_prompt']}" for index, entry in enumerate(prompts, start=1)
        )
        + "\n",
        encoding="utf-8",
    )

    bank = summary["bank"]
    print("\n== configuration ==")
    print(f"bank: {bank.get('source')} (sha256 {str(bank.get('source_sha256'))[:16]}...), "
          f"rows {bank.get('rows_built')}, drops {bank.get('drops')}")
    print(f"embedder: {bank.get('embedder')} / {bank.get('embedding_model')} "
          f"(dims {bank.get('dimensions')}), k={bank.get('k')}, "
          f"threshold={bank.get('threshold_used')}")
    print(f"self-consistency: n={config.self_consistency_n}, temperature={config.self_consistency_temperature}, "
          f"seed={config.self_consistency_seed}, budget={config.self_consistency_budget_s}s")
    print(f"database connections attempted by the injected engine: {engine.connections} (all refused)")
    print(f"stub LLM calls: {stub.get_usage()['calls']} (no GPU, no network)")
    print(f"\nartifacts written under {out_dir}:")
    for name in summary["artifacts"]:
        print(f"  - {name}")
    print("OK: both arms enabled and audible in prompt + metadata, with no database.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
