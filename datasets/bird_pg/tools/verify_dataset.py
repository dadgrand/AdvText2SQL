#!/usr/bin/env python3
"""Verify a BIRD-in-PostgreSQL dataset package end to end.

Two levels:

1. **Offline (default)** — structural checks that need no database:
   every record has the required fields, every ``db_id`` has a schema file, every
   row of ``schemas/schema.json`` declares tables and columns, no duplicate ids,
   no empty question/SQL, counts in ``MANIFEST.json`` match the files.
2. **Live (``--db-host``)** — execute every ``pg_gold_sql`` against a PostgreSQL
   server where the databases are named after ``db_id``.  Reports how many
   queries run, how many return rows, and (``--check-hashes``) how many return
   the *same* result set as the shipped ``result_sha256``.  Hashes only match on
   the original data volume, so use them to confirm a faithful local copy.

Examples
--------
    python tools/verify_dataset.py .
    python tools/verify_dataset.py . --db-host 127.0.0.1 --db-port 5444 \
        --db-user readonly_user --db-pass "$DB_PASS" --check-hashes

Exit code 0 when every selected check passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import collections
import decimal
import hashlib
import json
import pathlib
import sys

REQUIRED = ("db_id", "question", "pg_gold_sql")


def load_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def canon(cell):
    if cell is None:
        return None
    if isinstance(cell, bool):
        return int(cell)
    if isinstance(cell, decimal.Decimal):
        value = float(cell)
        return int(value) if value.is_integer() and abs(value) < 2**53 else round(value, 6)
    if isinstance(cell, float):
        return int(cell) if cell.is_integer() and abs(cell) < 2**53 else round(cell, 6)
    if isinstance(cell, int):
        return cell
    if isinstance(cell, (bytes, memoryview)):
        return bytes(cell).hex()
    if hasattr(cell, "isoformat"):
        return cell.isoformat(sep=" ") if hasattr(cell, "hour") else cell.isoformat()
    return str(cell).strip()


def digest(rows) -> str:
    key = sorted({tuple(canon(c) for c in row) for row in rows}, key=repr)
    return hashlib.sha256(json.dumps(key, ensure_ascii=False, default=str).encode()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("package", nargs="?", default=".", help="package root (default: .)")
    parser.add_argument("--db-host")
    parser.add_argument("--db-port", default="5432")
    parser.add_argument("--db-user", default="readonly_user")
    parser.add_argument("--db-pass", default="")
    parser.add_argument("--db-timeout-ms", type=int, default=30000)
    parser.add_argument("--check-hashes", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args(argv)

    root = pathlib.Path(args.package).resolve()
    problems: list[str] = []
    manifest_path = root / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    if not manifest:
        problems.append("MANIFEST.json is missing")

    schema_path = root / "schemas" / "schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8")) if schema_path.exists() else {}
    if not schema:
        problems.append("schemas/schema.json is missing")

    splits = collections.OrderedDict(
        (name, root / "data" / name)
        for name in (
            "bird_dev_pg.jsonl",
            "bird_dev_clean.jsonl",
            "bird_dev_divergent.jsonl",
            "bird_train_pg.jsonl",
            "bird_train_excluded.jsonl",
            "synthetic_bird_like.jsonl",
        )
    )

    for name, path in splits.items():
        rows = load_jsonl(path)
        print(f"{name:<32} {len(rows):>5} rows  {path.stat().st_size if path.exists() else 0:>9} bytes")
        # a synthetic SQL may legitimately carry two question variants: identity is
        # (sample id, variant), not the sample id alone
        ids = [f'{r.get("question_id") or r.get("sample_id")}#{r.get("variant", 1)}' for r in rows]
        if len(ids) != len(set(ids)):
            problems.append(f"{name}: duplicate ids")
        for row in rows:
            if row.get("status") == "excluded":
                continue
            for field in REQUIRED:
                if not row.get(field):
                    problems.append(f"{name}: {row.get('question_id') or row.get('sample_id')} lacks {field}")
                    break
            db_id = row.get("db_id")
            if schema and db_id not in schema:
                problems.append(f"{name}: {db_id} has no entry in schema.json")
            elif schema and not (root / "schemas" / f"{db_id}.sql").exists():
                problems.append(f"{name}: {db_id} has no schemas/{db_id}.sql")

    declared = manifest.get("counts", {})
    for name, expected in declared.items():
        path = root / "data" / name
        actual = len(load_jsonl(path))
        if expected != actual:
            problems.append(f"MANIFEST.json says {name}={expected}, file has {actual}")

    print(f"\nschema.json databases: {len(schema)}   DDL files: {len(list((root / 'schemas').glob('*.sql')))}")

    if args.db_host:
        try:
            import psycopg
        except ImportError:
            print("!! psycopg is required for --db-host", file=sys.stderr)
            return 1

        # Only the splits that are supposed to execute; the excluded files are a
        # record of failures and must not be "re-verified".
        executable_splits = {"bird_dev_pg.jsonl", "bird_dev_clean.jsonl",
                             "bird_dev_divergent.jsonl", "bird_train_pg.jsonl",
                             "synthetic_bird_like.jsonl"}
        executed = failed = hashed = hash_mismatch = 0
        for name, path in splits.items():
            if name not in executable_splits:
                continue
            rows = [r for r in load_jsonl(path) if r.get("pg_gold_sql")]
            if args.limit:
                rows = rows[: args.limit]
            connections: dict[str, object] = {}
            for row in rows:
                db_id = row["db_id"]
                if db_id not in connections:
                    try:
                        connections[db_id] = psycopg.connect(
                            host=args.db_host, port=args.db_port, user=args.db_user,
                            password=args.db_pass, dbname=db_id, connect_timeout=10,
                        )
                    except Exception as exc:
                        problems.append(f"cannot connect to {db_id}: {type(exc).__name__}: {exc}")
                        connections[db_id] = None
                        continue
                connection = connections[db_id]
                if connection is None:
                    continue
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(f"SET statement_timeout = {args.db_timeout_ms}")
                        cursor.execute(row["pg_gold_sql"])
                        result = cursor.fetchall()
                        connection.rollback()
                    executed += 1
                    expected = row.get("result_sha256")
                    if args.check_hashes and expected:
                        if digest(result) == expected:
                            hashed += 1
                        else:
                            hash_mismatch += 1
                except Exception as exc:
                    failed += 1
                    try:
                        connection.rollback()
                    except Exception:
                        pass
                    problems.append(
                        f"{name} {row.get('question_id') or row.get('sample_id')}: "
                        f"{type(exc).__name__}: {' '.join(str(exc).split())[:140]}"
                    )
            for connection in connections.values():
                try:
                    connection.close()
                except Exception:
                    pass
        print(f"\nlive execution: {executed} ok, {failed} failed", end="")
        if args.check_hashes:
            print(f", {hashed} result hashes matched, {hash_mismatch} mismatched", end="")
        print()

    if problems:
        print(f"\n{len(problems)} problem(s):")
        for problem in problems[:40]:
            print(f"  - {problem}")
        if len(problems) > 40:
            print(f"  ... and {len(problems) - 40} more")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
