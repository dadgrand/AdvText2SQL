#!/usr/bin/env python3
"""Convert a dataset JSONL of this package into chat-SFT JSONL.

The prompt template is the one the LoRA track trained with (see ``training/`` in
the repository), so the output can be concatenated with that corpus directly:

    system : PostgreSQL expert, one SELECT, PostgreSQL dialect rules
    user   : schema block + question (+ evidence)
    assistant : the gold SQL

Examples
--------
    python tools/to_sft.py data/bird_dev_clean.jsonl out/dev_sft.jsonl
    python tools/to_sft.py data/synthetic_bird_like.jsonl out/synth_sft.jsonl --evidence always
    python tools/to_sft.py data/bird_train_pg.jsonl out/train_sft.jsonl --limit 1000

Fields written per line: ``prompt`` (full chat text, ``<|im_start|>`` format),
``completion`` (SQL), plus ``db_id``, ``question_id`` and ``target_source``.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

SYSTEM = (
    "You are a PostgreSQL expert. Translate the question into ONE SELECT statement.\n"
    "Rules:\n"
    "- Use only tables and columns from the schema below. Quote identifiers exactly as written.\n"
    "- PostgreSQL dialect: single quotes for strings, double quotes only for identifiers, "
    "no backticks, no IFNULL/IIF/STRFTIME, no trailing semicolon.\n"
    "- Use x / NULLIF(y, 0) for every division whose denominator is a column.\n"
    "- Never add comments, markdown fences, or explanations.\n"
    "Output: SQL only."
)


def schema_block(schema: dict, db_id: str, max_chars: int) -> str:
    description = schema.get(db_id)
    if not description:
        return ""
    lines = ["Schema:"]
    for table in description["tables"]:
        lines.append(f"TABLE {table['name']}")
        for column in table["columns"]:
            lines.append(f"  - {column['name']} ({column['type']})")
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[:max_chars] + "\n... (truncated)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", help="dataset JSONL (dev, train or synthetic)")
    parser.add_argument("output", help="SFT JSONL to write")
    parser.add_argument("--schema", default="schemas/schema.json",
                        help="path to schema.json (default: relative to the package root)")
    parser.add_argument("--evidence", choices=["auto", "always", "never"], default="auto",
                        help="auto = include only when the dataset row has evidence")
    parser.add_argument("--max-schema-chars", type=int, default=6000)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--only-accepted", action="store_true",
                        help="skip rows whose status is not accepted/verified")
    args = parser.parse_args(argv)

    schema_path = pathlib.Path(args.schema)
    if not schema_path.exists():
        schema_path = pathlib.Path(__file__).resolve().parents[1] / "schemas" / "schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    rows = [json.loads(line) for line in pathlib.Path(args.input).read_text(encoding="utf-8").splitlines()
            if line.strip()]
    if args.only_accepted:
        rows = [r for r in rows if r.get("status") in (None, "accepted", "verified")]
    if args.limit:
        rows = rows[: args.limit]

    out_path = pathlib.Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    written = skipped = 0
    with out_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            sql = row.get("pg_gold_sql")
            if not sql:
                skipped += 1
                continue
            parts = [schema_block(schema, row["db_id"], args.max_schema_chars), "",
                     f"Question: {row['question']}"]
            evidence = (row.get("evidence") or "").strip()
            if evidence and args.evidence != "never":
                parts.append(f"Evidence: {evidence}")
            user = "\n".join(parts).strip()
            prompt = (
                f"<|im_start|>system\n{SYSTEM}<|im_end|>\n"
                f"<|im_start|>user\n{user}<|im_end|>\n"
                f"<|im_start|>assistant\n"
            )
            record = {
                "prompt": prompt,
                "completion": sql,
                "db_id": row["db_id"],
                "question_id": str(row.get("question_id") or row.get("sample_id")),
                "target_source": "gold" if row.get("source") != "synthetic" else "synthetic_template",
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            written += 1

    print(f"wrote {written} rows to {out_path}" + (f" ({skipped} skipped: no SQL)" if skipped else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
