"""Cheap variant sweep for the schema-linking arm: what each cell sends, per question.

Why this exists
---------------
The arm has four knobs (``k``, ``tables`` vs ``columns``, ``min_score``, optional
sample values). Measuring EX/241 for every cell would need one full harness run per
cell — on this card a 241-question run takes ~30-40 minutes (measured: see
``runs/t2/e-scale/run.json``), so 24 cells is ~15 hours of GPU. This script measures
the part that does **not** need the model, exactly and reproducibly:

* the schema block each cell would send (characters and cl100k tokens),
* how many tables/columns that is,
* whether the benchmark's gold tables are still in the block (a recall proxy — gold
  SQL is parsed only to score the selector, never passed to it),
* how often the linker falls back to today's pruning.

EX cells are *not* fabricated here: the sweep emits the exact ``run_benchmark``
command block per cell (``--emit-commands``) and the measured cost of running it, so
the evaluation owner can spend the GPU window on the cells the curve says matter.

Usage
-----
::

    python -m dev_tools.schema_link_sweep                         # 24 cells, CPU only
    python -m dev_tools.schema_link_sweep --samples               # + 3 sample values (live DB)
    python -m dev_tools.schema_link_sweep --emit-commands         # print the EX commands
    python -m dev_tools.schema_link_sweep --cells k6-columns --out runs/t28

The sample-values variant reads the live PostgreSQL read-only (``SELECT * FROM
"table" LIMIT 3`` per table, no credentials printed); it is a *cost* variant — the
tool has no ``schema_link_samples`` knob yet, so it is measured, not measured-for-EX.

Exit codes: ``0`` the sweep ran, ``2`` a required input is missing.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:  # package import (``python -m dev_tools.schema_link_sweep``)
    from .paths import DATA_CACHE, DATA_DIR, REPO_ROOT
except ImportError:  # pragma: no cover - direct script execution
    from paths import DATA_CACHE, DATA_DIR, REPO_ROOT  # type: ignore[no-redef]

DEFAULT_OUT: Path = REPO_ROOT / "runs" / "t28-schema-link-sweep"
SCHEMA_CANDIDATES: Sequence[Path] = (
    DATA_CACHE / "db_schemas.json",
    REPO_ROOT / "db_schemas.json",
)
K_VALUES: Tuple[int, ...] = (6, 12, 20)
COLUMN_VALUES: Tuple[bool, ...] = (False, True)
MIN_SCORE_VALUES: Tuple[float, ...] = (0.0, 1.0)
SAMPLE_VALUES: Tuple[int, ...] = (0, 3)
RUN_MINUTES_PER_241 = 39.0
"""Measured: ``runs/t2/e-scale/run.json`` 11:04:57 -> 11:44:02 for 241 questions."""
SAMPLE_VALUE_QUERIES = 3


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

from adv_text2sql.mcp_servers.text2sql_tool.src import schema_linking  # noqa: E402
from adv_text2sql.mcp_servers.text2sql_tool.src.schema_pruning import (  # noqa: E402
    prune_schema,
)

try:  # token counts are exact when tiktoken is present, chars/4 otherwise
    import tiktoken  # noqa: PLC0415

    _ENCODER = tiktoken.get_encoding("cl100k_base")
    TOKEN_METHOD = "tiktoken:cl100k_base"
except Exception:  # pragma: no cover - depends on the environment
    _ENCODER = None
    TOKEN_METHOD = "chars/4"


def token_count(text: str) -> int:
    if _ENCODER is not None:
        return len(_ENCODER.encode(text))
    return max(1, len(text) // 4)


# --------------------------------------------------------------------------- inputs


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


def load_dev() -> List[dict]:
    path = DATA_DIR / "bird_large.json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError(f"{path} must contain a JSON array")
    return rows


def gold_tables(sql: str) -> set:
    """Table names the gold SQL touches — used to SCORE the selector, never by it."""

    try:
        import sqlglot  # noqa: PLC0415
        from sqlglot import exp  # noqa: PLC0415

        parsed = sqlglot.parse_one(sql, read="sqlite")
    except Exception:
        return set()
    return {table.name.lower() for table in parsed.find_all(exp.Table) if table.name}


# ------------------------------------------------------------------------- sample values


def _live_engine(db_id: str) -> Any:
    """Read-only engine for one database, built through the repo's credential loader.

    ``dump_schemas.load_credentials`` is the tested path for the ``.env`` trap
    (quoted values, ``DB_CLIENT_HOST`` for the tunnel side) and
    ``dump_schemas.postgres_dsn`` percent-encodes the password, which contains
    reserved characters. Re-using it is what keeps this sweep from repeating the
    quoted-credential failure that produced the `ood-base7b-292` empty schema.
    """

    from sqlalchemy import create_engine  # noqa: PLC0415

    try:  # package import
        from . import dump_schemas  # noqa: PLC0415
    except ImportError:  # pragma: no cover - direct script execution
        import dump_schemas  # type: ignore[no-redef]  # noqa: PLC0415

    credentials = dump_schemas.load_credentials()
    dsn = dump_schemas.postgres_dsn(db_id, **credentials)
    return create_engine(dsn, pool_pre_ping=True)


def sample_values_by_table(db_id: str, tables: Sequence[str]) -> Dict[str, Dict[str, List[str]]]:
    """3 non-null example values per column, read with ``SELECT * ... LIMIT 3``.

    Safe: one read-only query per table (52 tables across the 8 dev databases), no
    ``DISTINCT`` scans, nothing written. Returns ``{table: {column: [v1, v2, v3]}}``.
    """

    from sqlalchemy import text as sql_text  # noqa: PLC0415

    engine = _live_engine(db_id)
    sampled: Dict[str, Dict[str, List[str]]] = {}
    try:
        with engine.connect() as connection:
            for table in tables:
                try:
                    result = connection.execute(
                        sql_text(f'SELECT * FROM "{table}" LIMIT {SAMPLE_VALUE_QUERIES}')
                    )
                    rows = result.fetchall()
                    columns = list(result.keys())
                except Exception:  # noqa: BLE001 - a sample is optional, never fatal
                    continue
                per_column: Dict[str, List[str]] = {}
                for position, column in enumerate(columns):
                    values: List[str] = []
                    for row in rows:
                        value = row[position]
                        if value is None:
                            continue
                        text_value = str(value).strip().replace("\n", " ")
                        if text_value and text_value not in values:
                            values.append(text_value[:40])
                        if len(values) >= SAMPLE_VALUE_QUERIES:
                            break
                    if values:
                        per_column[str(column)] = values
                if per_column:
                    sampled[str(table)] = per_column
    finally:
        engine.dispose()
    return sampled


def render_with_samples(block: str, samples: Dict[str, Dict[str, List[str]]]) -> str:
    """Append ``-- e.g. v1, v2, v3`` to each column line that has samples.

    The format is a proposal, not a tool feature: the point of this variant is its
    token cost and its readability, and the tool has no ``schema_link_samples`` knob
    yet (documented in ``docs/SCHEMA_LINKING.md`` §Variants).
    """

    current_table: Optional[str] = None
    out: List[str] = []
    for line in block.splitlines():
        if line.startswith("TABLE "):
            current_table = line[len("TABLE ") :].strip()
            out.append(line)
            continue
        stripped = line.strip()
        if current_table and stripped.startswith("- ") and " (" in stripped:
            column = stripped[2:].split(" (", 1)[0].strip()
            values = (samples.get(current_table) or {}).get(column) or []
            if values:
                out.append(f"{line}  -- e.g. " + ", ".join(values))
                continue
        out.append(line)
    return "\n".join(out)


# ------------------------------------------------------------------------- cells


def cell_id(k: int, columns: bool, min_score: float, samples: int) -> str:
    return (
        f"k{k}-{'columns' if columns else 'tables'}"
        f"-min{'0' if min_score == 0.0 else '1'}-samples{samples}"
    )


def build_cells() -> List[Dict[str, Any]]:
    cells: List[Dict[str, Any]] = []
    for k in K_VALUES:
        for columns in COLUMN_VALUES:
            for min_score in MIN_SCORE_VALUES:
                for samples in SAMPLE_VALUES:
                    cells.append(
                        {
                            "id": cell_id(k, columns, min_score, samples),
                            "k": k,
                            "mode": "columns" if columns else "tables",
                            "min_score": min_score,
                            "samples": samples,
                        }
                    )
    return cells


def measure_cell(
    cell: Dict[str, Any],
    *,
    schemas: Dict[str, str],
    dev: List[dict],
    samples_cache: Dict[str, Dict[str, Dict[str, List[str]]]],
) -> Dict[str, Any]:
    chars: List[int] = []
    tokens: List[int] = []
    tables_sent: List[int] = []
    columns_sent: List[int] = []
    fallbacks = 0
    covered = 0
    scored = 0
    per_db: Dict[str, List[int]] = {}
    baseline_chars: List[int] = []
    baseline_tokens: List[int] = []

    for item in dev:
        db_id = item["db_id"]
        schema = schemas.get(db_id)
        if not schema:
            continue
        question = item["question"]
        evidence = str(item.get("evidence") or "")
        baseline_block = prune_schema(schema, f"{question} {evidence}".strip()).schema
        baseline_chars.append(len(baseline_block))
        baseline_tokens.append(token_count(baseline_block))

        linked = schema_linking.link_schema(
            schema,
            question,
            evidence=evidence,
            mode=cell["mode"],
            k=int(cell["k"]),
            min_score=float(cell["min_score"]),
            embedder="hash",  # the deterministic arm; auto == hash on this machine
        )
        if not linked.applied:
            fallbacks += 1
            block = baseline_block
            tables_sent.append(len(prune_schema(schema, question).tables))
            columns_sent.append(0)
        else:
            block = linked.schema
            tables_sent.append(len(linked.tables))
            columns_sent.append(linked.columns_selected)
        if cell["samples"]:
            block = render_with_samples(block, samples_cache.get(db_id, {}))
        chars.append(len(block))
        tokens.append(token_count(block))

        gold = gold_tables(str(item.get("SQL") or ""))
        if not gold:
            continue
        scored += 1
        if linked.applied and gold.issubset({name.lower() for name in linked.tables}):
            covered += 1
            per_db.setdefault(db_id, [0, 0])[0] += 1
        per_db.setdefault(db_id, [0, 0])[1] += 1

    def _mean(values: Sequence[float]) -> Optional[float]:
        return round(statistics.mean(values), 1) if values else None

    def _median(values: Sequence[float]) -> Optional[float]:
        return round(statistics.median(values), 1) if values else None

    return {
        **cell,
        "questions": len(chars),
        "schema_chars_mean": _mean(chars),
        "schema_chars_median": _median(chars),
        "schema_tokens_mean": _mean(tokens),
        "schema_tokens_median": _median(tokens),
        "baseline_tokens_mean": _mean(baseline_tokens),
        "token_share_of_baseline": (
            round(statistics.mean(tokens) / statistics.mean(baseline_tokens), 3)
            if tokens and baseline_tokens and statistics.mean(baseline_tokens)
            else None
        ),
        "tables_mean": _mean(tables_sent),
        "columns_mean": _mean(columns_sent),
        "fallbacks": fallbacks,
        "gold_table_recall": round(covered / scored, 3) if scored else None,
        "gold_table_recall_count": f"{covered}/{scored}",
        "per_database_recall": {
            db: f"{ok}/{total}" for db, (ok, total) in sorted(per_db.items())
        },
    }


def baseline_cell(dev: List[dict], schemas: Dict[str, str]) -> Dict[str, Any]:
    chars: List[int] = []
    tokens: List[int] = []
    tables: List[int] = []
    for item in dev:
        schema = schemas.get(item["db_id"])
        if not schema:
            continue
        question = f"{item['question']} {str(item.get('evidence') or '')}".strip()
        pruned = prune_schema(schema, question)
        chars.append(len(pruned.schema))
        tokens.append(token_count(pruned.schema))
        tables.append(len(pruned.tables))
    return {
        "id": "off-baseline",
        "mode": "off",
        "k": None,
        "min_score": None,
        "samples": 0,
        "questions": len(chars),
        "schema_chars_mean": round(statistics.mean(chars), 1) if chars else None,
        "schema_chars_median": round(statistics.median(chars), 1) if chars else None,
        "schema_tokens_mean": round(statistics.mean(tokens), 1) if tokens else None,
        "schema_tokens_median": round(statistics.median(tokens), 1) if tokens else None,
        "baseline_tokens_mean": round(statistics.mean(tokens), 1) if tokens else None,
        "token_share_of_baseline": 1.0,
        "tables_mean": round(statistics.mean(tables), 1) if tables else None,
        "columns_mean": None,
        "fallbacks": 0,
        "gold_table_recall": None,
        "gold_table_recall_count": "n/a (pruning only)",
        "per_database_recall": {},
    }


# --------------------------------------------------------------------------- output


def markdown_table(rows: Sequence[Dict[str, Any]], *, sample_measured: bool) -> str:
    lines = [
        "| cell | mode | k | min_score | samples | schema tokens (mean) | share of baseline | tables (mean) | gold-table recall | EX/241 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        ex = "— (blocked: no endpoint)" if row["mode"] != "off" else "— (baseline)"
        samples = row["samples"] if sample_measured or not row["samples"] else "n/m"
        if row["samples"] and not sample_measured:
            tokens = "n/m"
            share = "n/m"
        else:
            tokens = row["schema_tokens_mean"]
            share = (
                "%.0f%%" % (100 * row["token_share_of_baseline"])
                if row["token_share_of_baseline"] is not None
                else "—"
            )
        lines.append(
            f"| `{row['id']}` | {row['mode']} | {row['k'] if row['k'] is not None else '—'} | "
            f"{row['min_score'] if row['min_score'] is not None else '—'} | {samples} | "
            f"{tokens} | {share} | "
            f"{row['tables_mean']} | {row['gold_table_recall_count']} | {ex} |"
        )
    return "\n".join(lines)


def emit_commands(cells: Sequence[Dict[str, Any]], schemas_path: Path) -> str:
    """The exact command block for an EX cell — for the evaluation owner's window."""

    lines = [
        "# EX per cell: one run_benchmark run + fixed_eval score, 241 questions each.",
        f"# ~{RUN_MINUTES_PER_241:.0f} min per cell on the local 16 GB card (measured: runs/t2/e-scale).",
        "# Requires the serving endpoint (LLM_BASE_URL/LLM_MODEL_NAME/LLM_API_KEY) and the DB tunnel.",
        "",
    ]
    for cell in cells:
        env = (
            f'$env:TEXT2SQL_SCHEMA_LINK="{cell["mode"]}"; '
            f'$env:TEXT2SQL_SCHEMA_LINK_K="{cell["k"]}"; '
            f'$env:TEXT2SQL_SCHEMA_LINK_MIN_SCORE="{cell["min_score"]}"; '
            f'$env:TEXT2SQL_SCHEMA_LINK_EMBEDDER="hash"; '
            f'$env:TEXT2SQL_PROCEDURE_LOG="procedures.jsonl"'
        )
        lines.append(f"# {cell['id']}")
        lines.append(env)
        lines.append(
            "python -m dev_tools.run_benchmark --allow-llm-spend --predict-only "
            f"--run-id {cell['id']} --query-file data/bird_large.json "
            f"--answer-file data/bird_large.json --runs-dir runs/t28-schema-link-sweep/runs"
        )
        lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dev_tools.schema_link_sweep",
        description=(
            "Sweep the schema-linking variants offline: per-cell schema characters/tokens, "
            "tables/columns sent, gold-table recall and fallbacks; EX cells are emitted as "
            "commands, never fabricated."
        ),
    )
    parser.add_argument("--schemas", default=None, help="schema dump JSON (default: data_cache/db_schemas.json)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="artifact directory")
    parser.add_argument("--cells", default=None, help="comma-separated cell ids or prefixes to keep")
    parser.add_argument("--samples", action="store_true", help="measure the 3-sample-values variant (live DB, read-only)")
    parser.add_argument("--emit-commands", action="store_true", help="print the EX command block per cell")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = REPO_ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        schemas_path = resolve_schemas(args.schemas)
    except FileNotFoundError as error:
        print(f"refusing to run: {error}", file=sys.stderr)
        return 2
    schemas = json.loads(schemas_path.read_text(encoding="utf-8"))
    dev = load_dev()
    print(f"sweep: {len(dev)} questions, {len(schemas)} databases from {schemas_path}")
    print(f"token counts: {TOKEN_METHOD}")

    cells = build_cells()
    if args.cells:
        wanted = [item.strip() for item in str(args.cells).split(",") if item.strip()]
        cells = [cell for cell in cells if any(cell["id"].startswith(prefix) for prefix in wanted)]
    print(f"cells: {len(cells)}")

    samples_cache: Dict[str, Dict[str, Dict[str, List[str]]]] = {}
    sample_measured = False
    if args.samples:
        try:
            for db_id, schema in schemas.items():
                tables = [table.name for table in schema_linking.parse_schema(schema)]
                samples_cache[db_id] = sample_values_by_table(db_id, tables)
            sample_measured = True
            print(f"sample values: read {len(samples_cache)} databases from the live PostgreSQL (LIMIT 3 per table)")
        except Exception as error:  # noqa: BLE001 - the variant is optional
            # Never echo the message: a driver error can carry the DSN (credentials).
            print(
                f"sample values: NOT measured ({type(error).__name__}); "
                f"check the DB tunnel and the .env quoting — no credentials are printed",
                file=sys.stderr,
            )

    rows: List[Dict[str, Any]] = [baseline_cell(dev, schemas)]
    for cell in cells:
        row = measure_cell(
            cell, schemas=schemas, dev=dev, samples_cache=samples_cache
        )
        rows.append(row)
        print(
            f"  {row['id']:<40} tokens {str(row['schema_tokens_mean']):>6} "
            f"({('%.0f%%' % (100 * row['token_share_of_baseline'])) if row['token_share_of_baseline'] is not None else '—'}), "
            f"tables {row['tables_mean']}, recall {row['gold_table_recall_count']}, fallbacks {row['fallbacks']}"
        )

    payload = {
        "tool": "dev_tools.schema_link_sweep",
        "statement": (
            "Offline sweep of the schema-linking variants: exact per-cell schema characters/"
            "tokens, tables/columns sent, gold-table recall and fallbacks. EX/241 is NOT "
            "measured here — each cell needs one full harness run; commands are emitted."
        ),
        "token_method": TOKEN_METHOD,
        "schemas": str(schemas_path),
        "questions": len(dev),
        "run_minutes_per_241_measured": RUN_MINUTES_PER_241,
        "sample_values_measured": sample_measured,
        "baseline": rows[0],
        "cells": rows[1:],
    }
    (out_dir / "sweep.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    table = markdown_table(rows, sample_measured=sample_measured)
    (out_dir / "sweep.md").write_text(table + "\n", encoding="utf-8")
    if args.emit_commands:
        (out_dir / "ex-commands.txt").write_text(
            emit_commands(cells, schemas_path) + "\n", encoding="utf-8"
        )
    print()
    print(table)
    print()
    print(f"artifacts: {out_dir / 'sweep.json'}, {out_dir / 'sweep.md'}"
          + (f", {out_dir / 'ex-commands.txt'}" if args.emit_commands else ""))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
