"""Offline dry run for the schema-linking arm: all three modes, no database.

What this proves
----------------
``off``, ``tables`` and ``columns`` can each be enabled end to end with **no
database, no model and no network**: the schema comes from a schema dump, the LLM
is the deterministic ``dev_tools.stub_llm.StubLLM``, and the tool is built with
``engine=None`` so no connection is ever attempted. For every mode it checks the
property the arm is measured on:

* ``off`` — the schema block in the system prompt is byte-identical to today's
  ``prune_schema`` output (the default path must not move);
* ``tables`` / ``columns`` — a linked block was produced (``applied``), its
  SHA-256 / table / column / character counts are recorded, and the prompt
  contains exactly that block instead of the full schema.

It is a plumbing proof, not a score: the stub answers are canned. Accuracy is
``eval-run``'s measurement against the real endpoint.

Usage
-----
::

    python -m dev_tools.schema_link_dryrun                  # writes runs/t25-schema-linking/
    python -m dev_tools.schema_link_dryrun --embedder none  # lexical-only arm
    python -m dev_tools.schema_link_dryrun --modes off,tables

Exit codes: ``0`` every requested mode behaved as documented, ``2`` a mode did
not (the transcript says which and why).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

try:  # package import (``python -m dev_tools.schema_link_dryrun``)
    from .paths import DATA_CACHE, REPO_ROOT
    from .stub_llm import StubLLM
except ImportError:  # pragma: no cover - direct script execution
    from paths import DATA_CACHE, REPO_ROOT  # type: ignore[no-redef]
    from stub_llm import StubLLM  # type: ignore[no-redef]

DEFAULT_OUT: Path = REPO_ROOT / "runs" / "t31-schema-linking"
DEFAULT_MODES: Sequence[str] = ("off", "tables", "columns", "highlight", "joins")
SCHEMA_CANDIDATES: Sequence[Path] = (
    DATA_CACHE / "db_schemas.json",
    REPO_ROOT / "db_schemas.json",
)

QUESTION_DB = "financial"
QUESTION = "How much did client 617 pay in transactions in 1998?"
CANNED_SQL = "SELECT SUM(amount) FROM trans"


def _bootstrap_import_path() -> None:
    for path in (str(REPO_ROOT), str(REPO_ROOT / "src")):
        if path not in sys.path:
            sys.path.insert(0, path)
    try:
        import fastmcp  # noqa: F401
    except ImportError:
        stub = types.ModuleType("fastmcp")
        stub.FastMCP = type("FastMCP", (), {})  # type: ignore[attr-defined]
        sys.modules["fastmcp"] = stub


_bootstrap_import_path()

from adv_text2sql.mcp_servers.text2sql_tool.src.prompts import (  # noqa: E402
    SQL_DIALECT,
    SQL_PROMPT_TEMPLATE,
    SYSTEM_PROMPT_TEMPLATE,
)
from adv_text2sql.mcp_servers.text2sql_tool.src.schema_pruning import (  # noqa: E402
    prune_schema,
)
from adv_text2sql.mcp_servers.text2sql_tool.src.text2sql_implementation import (  # noqa: E402
    Text2SQLConfig,
    Text2SQLGenerator,
)


def resolve_schemas(path: Optional[str]) -> Path:
    if path:
        candidate = Path(path)
        if not candidate.is_file():
            raise FileNotFoundError(f"schema dump not found: {candidate}")
        return candidate
    for candidate in SCHEMA_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "no schema dump found; pass --schemas (looked in: "
        + ", ".join(str(candidate) for candidate in SCHEMA_CANDIDATES)
        + ")"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dev_tools.schema_link_dryrun",
        description=(
            "Enable the schema-linking arm in all three modes offline (no database, no "
            "model, no network) and prove the prompt carries the linked block."
        ),
    )
    parser.add_argument("--schemas", default=None, help="schema dump JSON (default: data_cache/db_schemas.json)")
    parser.add_argument("--db-id", default=QUESTION_DB, help=f"database to link (default: {QUESTION_DB})")
    parser.add_argument("--question", default=QUESTION, help="question to link against")
    parser.add_argument("--modes", default=",".join(DEFAULT_MODES), help="comma-separated modes to prove")
    parser.add_argument("--k", type=int, default=6, help="tables kept (default: 6)")
    parser.add_argument("--min-score", type=float, default=1.0, help="minimum table score (default: 1.0)")
    parser.add_argument("--max-chars", type=int, default=6000, help="schema character budget (default: 6000)")
    parser.add_argument(
        "--embedder",
        default="auto",
        choices=("auto", "sentence-transformer", "hash", "none"),
        help="semantic boost backend; auto falls back to hash when weights are absent (default)",
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="artifact directory")
    return parser


def _reference_prompt(db_schema: str, question: str) -> Dict[str, str]:
    """The exact messages the off path must produce (built from the templates)."""

    return {
        "system": SYSTEM_PROMPT_TEMPLATE.format(db_schema=db_schema, sql_dialect=SQL_DIALECT),
        "user": SQL_PROMPT_TEMPLATE.format(user_query=question, sql_dialect=SQL_DIALECT),
    }


def run_mode(
    mode: str,
    *,
    schemas: Dict[str, str],
    db_id: str,
    question: str,
    args: argparse.Namespace,
    out_dir: Path,
) -> Dict[str, Any]:
    schema = schemas[db_id]
    stub = StubLLM(responses=[CANNED_SQL], default=CANNED_SQL)
    config = Text2SQLConfig(
        ambiguous_mode="off",
        max_llm_retries=0,
        retry_backoff_s=0.0,
        execution_feedback=False,  # no database connection is attempted
        schema_link=mode,
        schema_link_k=int(args.k),
        schema_link_min_score=float(args.min_score),
        schema_link_max_chars=int(args.max_chars),
        schema_link_embedder=str(args.embedder),
        procedure_log=str(out_dir / "procedures.jsonl"),
    )
    tool = Text2SQLGenerator(
        db_uri=f"postgresql+psycopg://dry-run@localhost:1/{db_id}",
        llm_client=stub,
        schema=schema,
        engine=None,
        config=config,
    )
    tool.build()
    result = asyncio.run(tool.query(question))
    metadata = tool.procedure_metadata()["schema_link"]
    prompt = stub.history[-1]["messages"][0]["content"] if stub.history else ""
    user_prompt = stub.history[-1]["messages"][-1]["content"] if stub.history else ""

    baseline_block = prune_schema(
        tool.db_schema,
        question,
        max_tables=tool.config.prune_max_tables,
        min_schema_tables=tool.config.prune_min_schema_tables,
    ).schema
    reference = _reference_prompt(baseline_block, question)

    checks: List[Dict[str, Any]] = []
    checks.append(
        {
            "check": "query_succeeded",
            "ok": result.get("status") == "success",
            "detail": str(result.get("status")),
        }
    )
    if mode == "off":
        checks.append(
            {
                "check": "off_is_byte_identical_to_pruning",
                "ok": prompt == reference["system"] and user_prompt.strip() == reference["user"].strip(),
                "detail": "system+user prompt equal the prune_schema reference",
            }
        )
        checks.append(
            {
                "check": "off_records_no_link",
                "ok": metadata.get("last") is None,
                "detail": "last_schema_link is None",
            }
        )
        linked_sha = None
        linked_chars = len(baseline_block)
        applied = None
    else:
        last = metadata.get("last") or {}
        applied = bool(last.get("applied"))
        linked_block = last.get("schema") if isinstance(last.get("schema"), str) else ""
        checks.append(
            {
                "check": "linker_applied",
                "ok": applied,
                "detail": str(last.get("reason") or last.get("fallback_reason") or ""),
            }
        )
        checks.append(
            {
                "check": "sha_and_counts_recorded",
                "ok": bool(last.get("schema_sha256")) and int(last.get("schema_chars") or 0) > 0,
                "detail": f"sha={str(last.get('schema_sha256'))[:16]} chars={last.get('schema_chars')} "
                f"tables={last.get('tables_selected')}/{last.get('tables_total')} "
                f"columns={last.get('columns_selected')}/{last.get('columns_total')}",
            }
        )
        # The prompt check uses the block the tool says it injected; the block text
        # lives in the procedure log (the last record), so read it back from there.
        log_lines = (out_dir / "procedures.jsonl").read_text(encoding="utf-8").splitlines()
        record = json.loads(log_lines[-1]) if log_lines else {}
        logged_block = (record.get("schema_link") or {}).get("block")
        checks.append(
            {
                "check": "prompt_carries_the_linked_block",
                "ok": bool(logged_block) and logged_block in prompt,
                "detail": f"logged block of {len(logged_block or '')} chars is in the system prompt",
            }
        )
        if mode in ("highlight", "joins"):
            # Non-destructive invariant: every original schema line survives verbatim.
            original_lines = [line for line in schema.splitlines() if line.strip()]
            block_lines = set((logged_block or "").splitlines())
            missing = [line for line in original_lines if line not in block_lines]
            checks.append(
                {
                    "check": "non_destructive_every_original_line_present",
                    "ok": bool(logged_block) and not missing,
                    "detail": f"{len(original_lines) - len(missing)}/{len(original_lines)} original lines kept"
                    + (f"; first missing: {missing[0]!r}" if missing else ""),
                }
            )
            checks.append(
                {
                    "check": "annotations_or_edges_present",
                    "ok": int(last.get("annotations") or 0) > 0 or bool(last.get("join_edges")),
                    "detail": f"annotations={last.get('annotations')} "
                    f"edges={len(last.get('join_edges') or [])} "
                    f"join_chars={last.get('join_block_chars')}",
                }
            )
        linked_sha = last.get("schema_sha256")
        linked_chars = int(last.get("schema_chars") or 0)

    return {
        "mode": mode,
        "db_id": db_id,
        "question": question,
        "status": result.get("status"),
        "sql": result.get("query"),
        "schema_total_chars": len(schema),
        "block_chars": linked_chars,
        "block_fraction": round(linked_chars / max(1, len(schema)), 3),
        "schema_sha256": linked_sha,
        "applied": applied,
        "metadata": metadata,
        "checks": checks,
        "ok": all(item["ok"] for item in checks),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = REPO_ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    log = out_dir / "procedures.jsonl"
    if log.exists():
        log.unlink()

    try:
        schemas_path = resolve_schemas(args.schemas)
    except FileNotFoundError as error:
        print(f"refusing to run: {error}", file=sys.stderr)
        return 2
    schemas = json.loads(schemas_path.read_text(encoding="utf-8"))
    if args.db_id not in schemas:
        print(
            f"refusing to run: {args.db_id!r} is not in {schemas_path} "
            f"(available: {', '.join(sorted(schemas))})",
            file=sys.stderr,
        )
        return 2

    modes = [mode.strip().lower() for mode in str(args.modes).split(",") if mode.strip()]
    unknown = [mode for mode in modes if mode not in DEFAULT_MODES]
    if unknown:
        print(f"refusing to run: unknown mode(s) {unknown}", file=sys.stderr)
        return 2

    print("== schema-linking dry run (offline: no database, no model, no network) ==")
    print(f"schemas: {schemas_path}")
    print(f"database: {args.db_id} ({len(schemas[args.db_id])} chars of schema)")
    print(f"question: {args.question}")
    print(
        f"config: k={args.k} min_score={args.min_score} max_chars={args.max_chars} "
        f"embedder={args.embedder}"
    )
    print()

    results: List[Dict[str, Any]] = []
    failures = 0
    for mode in modes:
        record = run_mode(
            mode,
            schemas=schemas,
            db_id=args.db_id,
            question=args.question,
            args=args,
            out_dir=out_dir,
        )
        results.append(record)
        last = record["metadata"].get("last") or {}
        print(f"-- mode={mode}: status={record['status']} applied={record['applied']}")
        print(
            f"   block {record['block_chars']}/{record['schema_total_chars']} chars "
            f"({record['block_fraction']:.0%}), sha {str(record['schema_sha256'])[:16]}"
        )
        if last and mode != "off":
            print(
                f"   tables {last.get('tables_selected')}/{last.get('tables_total')}: "
                f"{', '.join(last.get('tables') or [])}"
            )
            print(
                f"   columns {last.get('columns_selected')}/{last.get('columns_total')}, "
                f"embedder={last.get('embedder')}"
                + (f" (fallback: {str(last.get('embedder_reason'))[:60]}...)" if last.get("embedder_reason") else "")
            )
        for check in record["checks"]:
            mark = "ok" if check["ok"] else "FAILED"
            print(f"   [{mark}] {check['check']}: {check['detail']}")
            if not check["ok"]:
                failures += 1
        print()

    summary = {
        "tool": "dev_tools.schema_link_dryrun",
        "statement": (
            "All requested schema-linking modes were enabled with no database, no model and no "
            "network; the LLM is dev_tools.stub_llm.StubLLM. The off mode is byte-identical to "
            "the prune_schema prompt. This is a plumbing proof, not a score."
        ),
        "invoked": vars(args),
        "schemas": str(schemas_path),
        "results": results,
        "ok": failures == 0,
        "artifacts": ["dry_run.json", "procedures.jsonl"],
    }
    (out_dir / "dry_run.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"artifacts written under {out_dir}: dry_run.json, procedures.jsonl")
    if failures:
        print(f"FAILED: {failures} check(s) did not hold; see the transcript above", file=sys.stderr)
        return 2
    print("OK: every requested mode behaved as documented, with no database.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
