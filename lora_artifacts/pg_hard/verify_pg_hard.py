#!/usr/bin/env python3
"""Acceptance checks for the frozen PG-hard test set (t16).

Recomputes everything from the live fixture databases instead of trusting the
builder's own report:

1. the four artifacts exist and parse; the frozen sha256 of ``questions.jsonl``
   and ``gold.jsonl`` matches ``manifest.json``;
2. 10-15 schemas, 8-12 questions per schema, every skill of the playbook's skill
   table covered (or an explicit drop recorded);
3. every question states its comparison mode (``bag``/``ordered``) and a tie
   policy, and every ``ordered`` question pins a deterministic tie-break;
4. every gold query executes twice with identical row digests, returns a
   non-empty result, and reproduces the frozen ``result_sha256``,
   ``bag_sha256`` and ``ordered_sha256``;
5. the live databases' catalog fingerprints and row counts equal the manifest,
   so the frozen expected results belong to exactly these fixtures;
6. no schema overlaps the split manifest: no ``db_id`` collision, no lineage
   fingerprint collision, and the type-free table/column Jaccard against every
   captured BIRD schema stays below the threshold;
7. no question duplicates a question from the closed eval registry (dev-217,
   OOD-876, external) or from any trainable file, by exact normalised match or
   by 5-gram Jaccard >= 0.8.

Usage:

    python datasets/pg_hard/verify_pg_hard.py

Exit code 0 means every check passed.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import pathlib
import re
import sys
import unicodedata

HERE = pathlib.Path(__file__).resolve().parent
DATASETS = HERE.parent
PROJECT = DATASETS.parent
SPLIT_MANIFEST = DATASETS / "splits" / "schema_family_split.json"
EVAL_REGISTRY = DATASETS / "splits" / "eval_questions.jsonl"
BIRD_SCHEMAS = PROJECT / "repo" / "datasets" / "bird_pg_unified" / "schemas" / "schema.json"

TRAINABLE_FILES = [
    PROJECT / "datasets" / "native_pg" / "native_pg_train.jsonl",
    PROJECT / "bird_pg_unified" / "data" / "bird_train_pg.jsonl",
    PROJECT / "bird_pg_unified" / "data" / "synthetic_bird_like.jsonl",
    PROJECT / "bird_pg_unified" / "data" / "synthetic_cap_unnamed.jsonl",
    PROJECT / "bird_pg_unified" / "data" / "synthetic_single_table_extra.jsonl",
    PROJECT / "cluster" / "sft_all_v2.jsonl",
    PROJECT / "cluster" / "sft_train.jsonl",
    PROJECT / "cluster" / "sft_synth.jsonl",
]

SKILLS = [
    "grain_fanout_antijoin",
    "topn_ties",
    "asof_state",
    "periods_timezones",
    "window_frames_cohorts",
    "jsonb_arrays",
    "compositional",
    "hierarchies",
]
STRUCTURE_JACCARD_LIMIT = 0.5
QUESTION_JACCARD_LIMIT = 0.8


def canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def normalise_question(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    text = re.sub(r"[^0-9a-z]+", " ", text)
    return " ".join(text.split())


def ngrams(text: str, n: int = 5) -> set[tuple[str, ...]]:
    tokens = normalise_question(text).split()
    if not tokens:
        return set()
    if len(tokens) < n:
        return {tuple(tokens)}
    return {tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)}


def canon_cell(value) -> list:
    import datetime as _dt
    import decimal

    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["num", str(value)]
    if isinstance(value, decimal.Decimal):
        return ["num", format(value.normalize(), "f") if value.is_finite() else str(value)]
    if isinstance(value, float):
        return ["num", repr(value)]
    if isinstance(value, str):
        return ["str", value]
    if isinstance(value, _dt.datetime):
        return ["timestamp", value.isoformat()]
    if isinstance(value, _dt.date):
        return ["date", value.isoformat()]
    if isinstance(value, _dt.time):
        return ["time", value.isoformat()]
    if isinstance(value, _dt.timedelta):
        return ["interval", str(value.total_seconds())]
    if isinstance(value, (list, tuple)):
        return ["seq", [canon_cell(item) for item in value]]
    if isinstance(value, dict):
        return ["map", sorted((str(key), canon_cell(item)) for key, item in value.items())]
    return ["repr", f"{type(value).__name__}:{value!r}"]


def digest_rows(rows, mode: str) -> str:
    canonical = [canon([canon_cell(cell) for cell in row]) for row in rows]
    if mode == "bag":
        canonical = sorted(canonical)
    return hashlib.sha256("\n".join(canonical).encode("utf-8")).hexdigest()


def fingerprint(catalog: list[dict]) -> str:
    payload = []
    for table in catalog:
        columns = sorted(
            [[column["name"], column["type"], bool(column.get("not_null"))] for column in table["columns"]],
            key=lambda col: (col[0], str(col[1])),
        )
        foreign_keys = sorted(
            [[fk["column"], fk["references"]["table"], fk["references"]["column"]] for fk in table.get("foreign_keys") or []]
        )
        payload.append([table["name"], columns, sorted(table.get("primary_key") or []), foreign_keys])
    payload.sort(key=lambda table: table[0])
    return hashlib.sha256(canon(payload).encode("utf-8")).hexdigest()


def structure_of(catalog: list[dict]) -> list[list]:
    return sorted([[table["name"], sorted(column["name"] for column in table["columns"])] for table in catalog])


def jaccard(first: list[list], second: list[list]) -> float:
    first_tables = {name: set(columns) for name, columns in first}
    second_tables = {name: set(columns) for name, columns in second}
    union_tables = set(first_tables) | set(second_tables)
    shared_tables = set(first_tables) & set(second_tables)
    table_score = len(shared_tables) / len(union_tables) if union_tables else 0.0
    first_columns = {(table, column) for table, columns in first_tables.items() for column in columns}
    second_columns = {(table, column) for table, columns in second_tables.items() for column in columns}
    union_columns = first_columns | second_columns
    column_score = len(first_columns & second_columns) / len(union_columns) if union_columns else 0.0
    return round(max(table_score, column_score), 4)


def read_catalog(conn) -> list[dict]:
    columns = conn.execute(
        """
        SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'r' AND n.nspname = 'public' AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY c.relname, a.attnum
        """
    ).fetchall()
    primary_keys = conn.execute(
        """
        SELECT c.relname, a.attname
        FROM pg_index i
        JOIN pg_class c ON c.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
        WHERE i.indisprimary AND n.nspname = 'public'
        """
    ).fetchall()
    foreign_keys = conn.execute(
        """
        SELECT c.relname, a.attname, rc.relname, ra.attname
        FROM pg_constraint con
        JOIN pg_class c ON c.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_class rc ON rc.oid = con.confrelid
        JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = con.conkey[1]
        JOIN pg_attribute ra ON ra.attrelid = con.confrelid AND ra.attnum = con.confkey[1]
        WHERE con.contype = 'f' AND n.nspname = 'public'
        """
    ).fetchall()
    by_table: dict[str, dict] = {}
    for table_name, column_name, column_type, not_null in columns:
        table = by_table.setdefault(table_name, {"name": table_name, "columns": [], "primary_key": [], "foreign_keys": []})
        table["columns"].append({"name": column_name, "type": column_type, "not_null": bool(not_null)})
    for table_name, column_name in primary_keys:
        by_table.setdefault(table_name, {"name": table_name, "columns": [], "primary_key": [], "foreign_keys": []})["primary_key"].append(column_name)
    for table_name, column_name, ref_table, ref_column in foreign_keys:
        by_table.setdefault(table_name, {"name": table_name, "columns": [], "primary_key": [], "foreign_keys": []})["foreign_keys"].append(
            {"column": column_name, "references": {"table": ref_table, "column": ref_column}}
        )
    return [by_table[name] for name in sorted(by_table)]


def extract_questions(path: pathlib.Path) -> list[str]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    rows = json.loads(text) if text.lstrip().startswith("[") else [json.loads(line) for line in text.splitlines() if line.strip()]
    questions = []
    for row in rows:
        if isinstance(row.get("question"), str) and row["question"].strip():
            questions.append(row["question"])
            continue
        prompt = row.get("prompt")
        if isinstance(prompt, str):
            match = re.search(r"Question:\s*(.+?)(?:\nEvidence:|\n<\|im_end\||$)", prompt, re.DOTALL)
            if match:
                questions.append(match.group(1).strip())
    return questions


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pgdata", default="/tmp/pghard_pgdata", help="datadir of the fixture PostgreSQL instance")
    args = parser.parse_args(argv)

    import psycopg

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, condition, detail: str = "") -> None:
        checks.append((name, bool(condition), detail))

    questions_path = HERE / "questions.jsonl"
    gold_path = HERE / "gold.jsonl"
    manifest_path = HERE / "manifest.json"
    report_path = HERE / "report.json"
    for path in (questions_path, gold_path, manifest_path, report_path):
        check(f"{path.name} exists", path.exists())
    if not all(path.exists() for path in (questions_path, gold_path, manifest_path, report_path)):
        for name, ok, detail in checks:
            print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")
        return 1

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    questions = [json.loads(line) for line in questions_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    gold = [json.loads(line) for line in gold_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    # 1. freeze
    questions_sha = hashlib.sha256(questions_path.read_bytes()).hexdigest()
    gold_sha = hashlib.sha256(gold_path.read_bytes()).hexdigest()
    check("manifest is frozen", manifest.get("status") == "frozen")
    check("questions.jsonl matches the frozen hash", questions_sha == manifest["freeze"]["questions_sha256"])
    check("gold.jsonl matches the frozen hash", gold_sha == manifest["freeze"]["gold_sha256"])
    check("report.json records the same freeze", report["freeze"]["questions_sha256"] == questions_sha)

    # 2. shapes and coverage
    schema_count = len(manifest["schemas"])
    check("10-15 schemas", 10 <= schema_count <= 15, str(schema_count))
    per_db = collections.Counter(row["db_id"] for row in questions)
    check("every schema has 8-12 questions", all(8 <= per_db[entry["db_id"]] <= 12 for entry in manifest["schemas"]), str(dict(per_db)))
    check("one question set per schema", set(per_db) == {entry["db_id"] for entry in manifest["schemas"]})
    per_skill = collections.Counter(row["skill"] for row in questions)
    dropped = {item["skill"] for item in report.get("dropped_skills", [])}
    for skill in SKILLS:
        check(f"skill covered: {skill} (>= 8 questions)", per_skill.get(skill, 0) >= 8 or skill in dropped, str(per_skill.get(skill, 0)))
    check("at most one dropped skill recorded with a reason", len(report.get("dropped_skills", [])) <= 1 and all(item.get("reason") for item in report.get("dropped_skills", [])))

    # 3. comparison mode and tie policy
    check("every question names a comparison mode", all(row.get("comparison") in ("bag", "ordered") for row in questions))
    check("every question states a tie policy", all(isinstance(row.get("tie_policy"), str) and row["tie_policy"].strip() for row in questions))
    ordered = [row for row in questions if row["comparison"] == "ordered"]
    check("every ordered question pins a deterministic tie-break", all("not applicable" not in row["tie_policy"].lower() for row in ordered), str(len(ordered)))
    check("gold keeps the same comparison and tie policy", all(
        gold_row["comparison"] == question["comparison"] and gold_row["tie_policy"] == question["tie_policy"]
        for question, gold_row in zip(questions, gold)
    ))

    # 4. live fixtures + gold re-execution
    with psycopg.connect(host=args.pgdata, user="postgres", dbname="postgres", autocommit=True) as admin:
        databases = {row[0] for row in admin.execute("SELECT datname FROM pg_database").fetchall()}
    reexecuted = 0
    live_structures: dict[str, list] = {}
    mismatches: list[str] = []
    empty: list[str] = []
    for entry in manifest["schemas"]:
        db_id = entry["db_id"]
        check(f"{db_id}: database exists", db_id in databases)
        if db_id not in databases:
            continue
        with psycopg.connect(host=args.pgdata, user="postgres", dbname=db_id, autocommit=True) as conn:
            conn.execute("SET TimeZone = 'UTC'")
            catalog = read_catalog(conn)
            live_structures[db_id] = structure_of(catalog)
            check(f"{db_id}: catalog fingerprint matches manifest", fingerprint(catalog) == entry["fingerprint"])
            counts = {table["name"]: conn.execute(f'SELECT COUNT(*) FROM "{table["name"]}"').fetchone()[0] for table in catalog}
            check(f"{db_id}: row counts match manifest", counts == entry["rows_per_table"], str(counts))
            for gold_row in [row for row in gold if row["db_id"] == db_id]:
                first = conn.execute(gold_row["gold_sql"]).fetchall()
                second = conn.execute(gold_row["gold_sql"]).fetchall()
                if not first:
                    empty.append(gold_row["qid"])
                if digest_rows(first, "bag") != digest_rows(second, "bag"):
                    mismatches.append(f"{gold_row['qid']}: non-deterministic")
                    continue
                if digest_rows(first, gold_row["comparison"]) != gold_row["result_sha256"]:
                    mismatches.append(f"{gold_row['qid']}: result_sha256 mismatch")
                elif digest_rows(first, "bag") != gold_row["bag_sha256"] or digest_rows(first, "ordered") != gold_row["ordered_sha256"]:
                    mismatches.append(f"{gold_row['qid']}: bag/ordered hash mismatch")
                else:
                    reexecuted += 1
    check("every gold query re-executes to the frozen hash", not mismatches, "; ".join(mismatches[:5]))
    check("frozen gold is non-empty", not empty, ", ".join(empty[:5]))
    check("all questions re-executed", reexecuted == len(gold), f"{reexecuted}/{len(gold)}")

    # 5. schema overlap with the split manifest
    split = json.loads(SPLIT_MANIFEST.read_text(encoding="utf-8"))
    family_fingerprints = {family.get("lineage_fingerprint") for family in split["families"].values() if family.get("lineage_fingerprint")}
    family_ids = set(split["families"])
    bird = json.loads(BIRD_SCHEMAS.read_text(encoding="utf-8"))
    bird_structures = [
        {"db_id": db_id, "structure": structure_of_raw(entry["tables"])}
        for db_id, entry in bird.items()
        if entry.get("tables")
    ]
    check("no pg-hard db_id exists in the split manifest", not ({entry["db_id"] for entry in manifest["schemas"]} & family_ids))
    check("no pg-hard fingerprint equals a split-manifest fingerprint", not ({entry["fingerprint"] for entry in manifest["schemas"]} & family_fingerprints))
    worst = {"jaccard": 0.0, "db_id": None}
    for entry in manifest["schemas"]:
        structure = live_structures.get(entry["db_id"]) or []
        for family in bird_structures:
            score = jaccard(structure, family["structure"])
            if score > worst["jaccard"]:
                worst = {"jaccard": score, "db_id": f"{entry['db_id']}~{family['db_id']}"}
    check("structure Jaccard against every BIRD schema stays below the limit", worst["jaccard"] < STRUCTURE_JACCARD_LIMIT, str(worst))

    # 6. question duplication
    registry = [json.loads(line)["question"] for line in EVAL_REGISTRY.read_text(encoding="utf-8").splitlines() if line.strip()]
    trainable: list[str] = []
    for path in TRAINABLE_FILES:
        trainable.extend(extract_questions(path))
    registry_exact = {normalise_question(text) for text in registry}
    trainable_exact = {normalise_question(text) for text in trainable}
    exact_hits = [
        row["qid"] for row in questions
        if normalise_question(row["question"]) in registry_exact or normalise_question(row["question"]) in trainable_exact
    ]
    check("no exact question duplicate in the eval registry or trainable files", not exact_hits, ", ".join(exact_hits[:5]))
    worst_question = 0.0
    near_hits = []
    for row in questions:
        grams = ngrams(row["question"])
        for pool in (registry, trainable):
            for text in pool:
                other = ngrams(text)
                if not other:
                    continue
                shared = len(grams & other)
                if shared == 0:
                    continue
                score = shared / len(grams | other)
                worst_question = max(worst_question, score)
                if score >= QUESTION_JACCARD_LIMIT:
                    near_hits.append(row["qid"])
                    break
            if row["qid"] in near_hits:
                break
    check("no near-duplicate question (5-gram Jaccard >= 0.8)", not near_hits, ", ".join(sorted(set(near_hits))[:5]))
    print(f"  question pools: registry {len(registry)}, trainable {len(trainable)}; worst 5-gram Jaccard {worst_question:.4f}")

    failed = [item for item in checks if not item[1]]
    print(f"VERIFY {len(checks) - len(failed)}/{len(checks)} checks passed")
    for name, ok, detail in checks:
        if not ok:
            print(f"  FAILED: {name} {detail}")
    return 1 if failed else 0


def structure_of_raw(tables: list[dict]) -> list[list]:
    return sorted([[table["name"], sorted(column["name"] for column in table.get("columns") or [])] for table in tables])



if __name__ == "__main__":
    raise SystemExit(main())
