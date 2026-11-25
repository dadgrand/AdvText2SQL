#!/usr/bin/env python3
"""Build and freeze the PG-hard test set (t16).

Why this exists
===============

`dev-217` and `OOD-120` were already used inside the project (checkpoint selection and a
previous OOD report), and every captured BIRD schema is already assigned to exactly one
split by `datasets/splits/schema_family_split.json`. A credible final claim therefore needs a
set that is **closed before the model is chosen** and that nobody could have trained on.

This builder creates that set from scratch: hand-authored PostgreSQL schemas, hand-authored
analytical questions over them, and the gold answers executed against a real PostgreSQL
server. The playbook calls these "рукотворные PG-hard задачи" and asks for the share of
questions where a prediction matches the oracle on **all** fixtures (`POSTGRES_TEXT2SQL_LORA.md`
§6). Nothing here is trained on: the schemas did not exist in any pool before this build.

What is produced (all under ``datasets/pg_hard/``)
==================================================

* ``questions.jsonl`` — the student-visible surface plus evaluation metadata:
  ``qid, db_id, skill, difficulty, question, evidence, comparison, tie_policy``.
* ``gold.jsonl`` — gold SQL and the frozen expected result: ``result_sha256`` under the
  question's comparison mode, ``bag_sha256``/``ordered_sha256`` under both, row/column counts,
  column names and types, and the two-run determinism evidence.
* ``manifest.json`` — schema registry (stable ``db_id``, lineage fingerprint over tables,
  columns, keys and column types), per-table row counts and data hashes, question/skill
  counts, the freeze block with file hashes, server facts, and the changelog rule.
* ``report.json`` — per-skill counts, per-database counts, measured executable share and
  determinism, and the contamination checks against every trainable file and the closed eval
  registry.

Rules enforced here
===================

1. **Schema families are new.** Every database is created by this script; its lineage
   fingerprint and its type-free structure are compared with all 80 entries of the split
   manifest. A fingerprint collision or a table/column Jaccard above the threshold aborts the
   build.
2. **Questions execute, deterministically.** Each gold SQL is executed twice in a fresh
   transaction on the live PostgreSQL instance; the two canonical row digests must match.
   An empty result, an execution error or a mismatch aborts the build.
3. **Comparison mode and tie policy are mandatory.** ``comparison`` is ``bag`` (multiset of
   rows, duplicates and repetition counted) or ``ordered`` (sequence). Every question states
   its ``tie_policy``; ``ordered`` questions must pin a deterministic tie-break.
4. **Frozen once.** ``manifest.json.freeze`` carries sha256 of ``questions.jsonl`` and
   ``gold.jsonl``. A rebuild that would change them refuses to write unless ``--new-version``
   is passed, which bumps the version and appends a changelog entry.
5. **Skills the schema cannot support are dropped, not weakened.** A schema that cannot
   support a skill contributes a second question for another skill and records the drop with
   its reason in ``report.json.dropped_skills``.

Usage
=====

    python datasets/pg_hard/build_pg_hard.py                 # build, verify, freeze
    python datasets/pg_hard/build_pg_hard.py --verify        # re-execute frozen gold, compare hashes
    python datasets/pg_hard/build_pg_hard.py --new-version   # rebuild after a change (v+1)

The PostgreSQL instance is a bundled server (``pgserver``) on a local Unix socket; the datadir
defaults to ``~/.cache/advtext2sql/pghard_pgdata`` (persistent, not ``/tmp``) and can be
overridden with ``--pgdata`` or ``PGHARD_PGDATA``. After a reboot, ``--ensure`` starts the
existing instance; a lost datadir is rebuilt deterministically with the same frozen hashes.
The databases are named ``pghard_<slug>`` and are read-only for the role ``pghard_readonly``,
which the evaluation should use.
"""

from __future__ import annotations

import argparse
import collections
import datetime as _dt
import decimal
import hashlib
import importlib
import json
import os
import pathlib
import re
import sys
import time
import unicodedata

HERE = pathlib.Path(__file__).resolve().parent            # research/advtext2sql/datasets/pg_hard
DATASETS = HERE.parent                                     # research/advtext2sql/datasets
PROJECT = DATASETS.parent                                  # research/advtext2sql

SPLIT_MANIFEST = DATASETS / "splits" / "schema_family_split.json"
EVAL_REGISTRY = DATASETS / "splits" / "eval_questions.jsonl"
CHANGELOG = HERE / "CHANGELOG.md"
MANIFEST = HERE / "manifest.json"
QUESTIONS_FILE = HERE / "questions.jsonl"
GOLD_FILE = HERE / "gold.jsonl"
REPORT_FILE = HERE / "report.json"

#: Persistent fixture datadir: inside the user cache, not /tmp, so a reboot does not
#: discard the databases. ``--pgdata`` or ``PGHARD_PGDATA`` override it.
DEFAULT_PGDATA = os.environ.get(
    "PGHARD_PGDATA",
    str(pathlib.Path(os.environ.get("XDG_CACHE_HOME", pathlib.Path.home() / ".cache")) / "advtext2sql" / "pghard_pgdata"),
)
READONLY_ROLE = "pghard_readonly"
READONLY_PASSWORD = "pghard_readonly"
TIMEZONE = "UTC"
STATEMENT_TIMEOUT = "20s"

#: The eight skills of `POSTGRES_TEXT2SQL_LORA.md` §3, in the playbook's order.
SKILLS: dict[str, str] = {
    "grain_fanout_antijoin": "grain, fan-out, anti-join",
    "topn_ties": "top-N and windows with ties",
    "asof_state": "state as of a point in time",
    "periods_timezones": "periods, timezone, intervals",
    "window_frames_cohorts": "window frames, cohorts",
    "jsonb_arrays": "JSONB and arrays",
    "compositional": "compositional analytics",
    "hierarchies": "hierarchies",
}

SCHEMA_MODULES = [
    "ride_hailing",
    "retail_orders",
    "streaming",
    "banking",
    "logistics",
    "healthcare",
    "education",
    "energy",
    "airline",
    "marketplace",
    "telecom",
    "manufacturing",
]

#: PostgreSQL-native features the playbook asks the hard set to exercise.
FEATURES: dict[str, str] = {
    "with_cte": r"\bWITH\b",
    "recursive_cte": r"WITH RECURSIVE",
    "window_over": r"\bOVER\s*\(",
    "aggregate_filter": r"FILTER\s*\(\s*WHERE",
    "lateral_join": r"\bLATERAL\b",
    "distinct_on": r"DISTINCT ON",
    "jsonb_access": r"->>|->",
    "array_or_jsonb_containment": r"@>",
    "array_literal": r"\bARRAY\[",
    "at_time_zone": r"AT TIME ZONE",
    "range_frame": r"RANGE BETWEEN",
    "rows_frame": r"ROWS BETWEEN",
    "row_number": r"ROW_NUMBER\s*\(",
    "exists_subquery": r"\bEXISTS\s*\(",
    "recursive_union_all": r"UNION ALL",
}

#: Files that may carry training rows; a PG-hard question or schema must not appear in them.
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

#: Schema-copy guard: structure Jaccard above this against any split-manifest family aborts.
STRUCTURE_JACCARD_LIMIT = 0.5
#: Question-copy guard: 5-gram Jaccard at or above this against any registry/trainable question.
QUESTION_JACCARD_LIMIT = 0.8


# ---------------------------------------------------------------------------
# Canonicalisation and digests
# ---------------------------------------------------------------------------


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
    """JSON-safe canonical cell; numeric values compare by value, types stay distinct."""

    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["num", str(value)]
    if isinstance(value, decimal.Decimal):
        if not value.is_finite():
            return ["num", str(value)]
        return ["num", format(value.normalize(), "f")]
    if isinstance(value, float):
        return ["num", repr(value)]
    if isinstance(value, str):
        return ["str", value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return ["bytes", bytes(value).hex()]
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


def rows_canonical(rows) -> list[str]:
    return [canon([canon_cell(cell) for cell in row]) for row in rows]


def digest_rows(rows, mode: str) -> str:
    """sha256 of a result table.

    ``bag`` — multiset: rows sorted, duplicate rows preserved (the playbook's "multiset of rows
    with repetitions"); ``ordered`` — the sequence exactly as returned.
    """

    canonical = rows_canonical(rows)
    if mode == "bag":
        canonical = sorted(canonical)
    elif mode != "ordered":
        raise ValueError(f"unknown comparison mode {mode!r}")
    return hashlib.sha256(("\n".join(canonical)).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Schema fingerprint (same algorithm as datasets/splits/build_splits.py)
# ---------------------------------------------------------------------------


def fingerprint_entry(tables: list[dict]) -> str:
    """sha256 over tables, columns (name+type+not_null), primary and foreign keys."""

    payload = []
    for table in tables:
        columns = sorted(
            [[column["name"], column["type"], bool(column.get("not_null"))] for column in table["columns"]],
            key=lambda col: (col[0], str(col[1])),
        )
        foreign_keys = sorted(
            [
                [fk["column"], fk["references"]["table"], fk["references"]["column"]]
                for fk in table.get("foreign_keys") or []
            ]
        )
        payload.append([table["name"], columns, sorted(table.get("primary_key") or []), foreign_keys])
    payload.sort(key=lambda table: table[0])
    return hashlib.sha256(canon(payload).encode("utf-8")).hexdigest()


def structure_of(tables: list[dict]) -> list[list]:
    return sorted([[table["name"], sorted(column["name"] for column in table["columns"])] for table in tables])


def structure_jaccard(first: list[list], second: list[list]) -> float:
    first_tables = {name: set(columns) for name, columns in first}
    second_tables = {name: set(columns) for name, columns in second}
    shared_tables = set(first_tables) & set(second_tables)
    union_tables = set(first_tables) | set(second_tables)
    table_jaccard = len(shared_tables) / len(union_tables) if union_tables else 0.0
    first_columns = {(table, column) for table, columns in first_tables.items() for column in columns}
    second_columns = {(table, column) for table, columns in second_tables.items() for column in columns}
    union_columns = first_columns | second_columns
    column_jaccard = len(first_columns & second_columns) / len(union_columns) if union_columns else 0.0
    return round(max(table_jaccard, column_jaccard), 4), round(table_jaccard, 4), round(column_jaccard, 4)


# ---------------------------------------------------------------------------
# PostgreSQL plumbing
# ---------------------------------------------------------------------------


class Server:
    """A bundled PostgreSQL instance on a local Unix socket."""

    def __init__(self, pgdata: str):
        import pgserver  # noqa: PLC0415 - lazy: only needed for a live build

        self.pgdata = str(pgdata)
        pathlib.Path(self.pgdata).parent.mkdir(parents=True, exist_ok=True)
        self._server = pgserver.get_server(self.pgdata, cleanup_mode=None)
        self.version = self._query("select version()")[0][0]

    def _connect(self, dbname: str):
        import psycopg  # noqa: PLC0415

        return psycopg.connect(
            host=self.pgdata,
            user="postgres",
            dbname=dbname,
            autocommit=True,
            connect_timeout=20,
        )

    def _query(self, sql: str, dbname: str = "postgres"):
        with self._connect(dbname) as conn:
            return conn.execute(sql).fetchall()

    def recreate_database(self, name: str) -> None:
        with self._connect("postgres") as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            conn.execute(f'CREATE DATABASE "{name}"')
        with self._connect(name) as conn:
            conn.execute(f"SET TimeZone = '{TIMEZONE}'")

    def drop_database(self, name: str) -> None:
        with self._connect("postgres") as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')

    def promote_database(self, staged: str, final: str) -> None:
        """Replace the live fixture database with the freshly built staged one."""
        self.drop_database(final)
        for attempt in range(5):
            try:
                with self._connect("postgres") as conn:
                    conn.execute(f'ALTER DATABASE "{staged}" RENAME TO "{final}"')
                return
            except Exception:  # noqa: BLE001 - a lingering connection can block the rename
                if attempt == 4:
                    raise
                time.sleep(0.5)

    def apply(self, dbname: str, statements: list[str]) -> None:
        with self._connect(dbname) as conn:
            for statement in statements:
                conn.execute(statement)

    def insert(self, dbname: str, table: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        columns = list(rows[0].keys())
        for row in rows:
            if list(row.keys()) != columns:
                raise ValueError(f"{dbname}.{table}: every fixture row must use the same columns, got {list(row.keys())}")
        placeholders = ", ".join(["%s"] * len(columns))
        column_sql = ", ".join(f'"{column}"' for column in columns)
        statement = f'INSERT INTO "{table}" ({column_sql}) VALUES ({placeholders})'

        def adapt(value):
            from psycopg.types.json import Jsonb  # noqa: PLC0415 - lazy import

            if isinstance(value, dict):
                return Jsonb(value)
            return value

        with self._connect(dbname) as conn:
            with conn.cursor() as cursor:
                cursor.executemany(
                    statement,
                    [tuple(adapt(row[column]) for column in columns) for row in rows],
                )
        return len(rows)

    def read_back(self, dbname: str, table: str) -> list[dict]:
        with self._connect(dbname) as conn:
            conn.execute(f"SET TimeZone = '{TIMEZONE}'")
            cursor = conn.execute(f'SELECT * FROM "{table}"')
            names = [description.name for description in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]

    def catalog(self, dbname: str) -> list[dict]:
        """Tables with columns (name, type, not_null), primary key and foreign keys."""

        with self._connect(dbname) as conn:
            columns = conn.execute(
                """
                SELECT c.relname AS table_name,
                       a.attname AS column_name,
                       format_type(a.atttypid, a.atttypmod) AS column_type,
                       a.attnotnull AS not_null
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
                ORDER BY c.relname, a.attname
                """
            ).fetchall()
            foreign_keys = conn.execute(
                """
                SELECT c.relname AS table_name,
                       a.attname AS column_name,
                       rc.relname AS ref_table,
                       ra.attname AS ref_column
                FROM pg_constraint con
                JOIN pg_class c ON c.oid = con.conrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_class rc ON rc.oid = con.confrelid
                JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = con.conkey[1]
                JOIN pg_attribute ra ON ra.attrelid = con.confrelid AND ra.attnum = con.confkey[1]
                WHERE con.contype = 'f' AND n.nspname = 'public'
                ORDER BY c.relname, a.attname
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

    def ensure_readonly_role(self) -> None:
        with self._connect("postgres") as conn:
            if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (READONLY_ROLE,)).fetchone():
                conn.execute(f"CREATE ROLE {READONLY_ROLE} LOGIN PASSWORD '{READONLY_PASSWORD}'")
            conn.execute(f"GRANT pg_read_all_data TO {READONLY_ROLE}")
            conn.execute(f"ALTER ROLE {READONLY_ROLE} SET default_transaction_read_only = on")
            conn.execute(f"ALTER ROLE {READONLY_ROLE} SET statement_timeout = '{STATEMENT_TIMEOUT}'")

    def run_gold(self, dbname: str, sql: str) -> dict:
        import psycopg  # noqa: PLC0415

        started = time.time()
        with psycopg.connect(
            host=self.pgdata,
            user="postgres",
            dbname=dbname,
            autocommit=True,
            connect_timeout=20,
        ) as conn:
            conn.execute(f"SET TimeZone = '{TIMEZONE}'")
            conn.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")
            cursor = conn.execute(sql)
            rows = cursor.fetchall()
            columns = [
                {"name": description.name, "type": description.type_code}
                for description in cursor.description or []
            ]
        return {"rows": rows, "columns": columns, "seconds": round(time.time() - started, 4)}


# ---------------------------------------------------------------------------
# Loading the modules and checking their contract
# ---------------------------------------------------------------------------


def load_modules(only: list[str] | None = None) -> list:
    sys.path.insert(0, str(HERE))
    modules = []
    for name in (only or SCHEMA_MODULES):
        module = importlib.import_module(f"schemas.{name}")
        for attribute in ("SLUG", "DOMAIN", "DDL", "FIXTURES", "QUESTIONS", "DROPPED", "NOTES"):
            if not hasattr(module, attribute):
                raise SystemExit(f"schema module {name!r} is missing {attribute}")
        modules.append(module)
    return modules


def validate_module(module) -> list[dict]:
    slug = module.SLUG
    if not re.fullmatch(r"[a-z][a-z0-9_]*", slug):
        raise SystemExit(f"{slug}: bad slug")
    questions = []
    seen = set()
    for index, question in enumerate(module.QUESTIONS, 1):
        qid = f"pghard-{slug}-{index:02d}"
        required = {"skill", "question", "sql", "comparison", "tie_policy", "difficulty"}
        missing = required - set(question)
        if missing:
            raise SystemExit(f"{qid}: missing fields {sorted(missing)}")
        if question["skill"] not in SKILLS:
            raise SystemExit(f"{qid}: unknown skill {question['skill']!r}")
        if question["comparison"] not in ("bag", "ordered"):
            raise SystemExit(f"{qid}: comparison must be bag or ordered")
        if not question["tie_policy"].strip():
            raise SystemExit(f"{qid}: tie_policy is mandatory")
        if question["comparison"] == "ordered" and "not applicable" in question["tie_policy"].lower():
            raise SystemExit(f"{qid}: an ordered question needs a deterministic tie-break, not 'not applicable'")
        if question["difficulty"] not in ("moderate", "challenging", "hard"):
            raise SystemExit(f"{qid}: difficulty must be moderate/challenging/hard")
        text = question["question"].strip()
        if len(text) < 20:
            raise SystemExit(f"{qid}: the question is too short to be a self-contained task")
        if re.search(r"\b(SELECT|FROM|JOIN|WHERE|GROUP\s+BY|ORDER\s+BY|LIMIT|WITH)\b", text):
            raise SystemExit(f"{qid}: the question text leaks SQL syntax; it must be natural language")
        if qid in seen:
            raise SystemExit(f"duplicate qid {qid}")
        seen.add(qid)
        questions.append(
            {
                "qid": qid,
                "slug": slug,
                "db_id": f"pghard_{slug}",
                "skill": question["skill"],
                "difficulty": question["difficulty"],
                "question": question["question"].strip(),
                "evidence": (question.get("evidence") or "").strip(),
                "comparison": question["comparison"],
                "tie_policy": question["tie_policy"].strip(),
                "sql": question["sql"].strip(),
            }
        )
    if not (8 <= len(questions) <= 12):
        raise SystemExit(f"{slug}: {len(questions)} questions, the contract is 8..12 per schema")
    return questions


# ---------------------------------------------------------------------------
# Contamination checks
# ---------------------------------------------------------------------------


def load_manifest_families() -> tuple[dict, list[dict]]:
    payload = json.loads(SPLIT_MANIFEST.read_text(encoding="utf-8"))
    families = payload["families"]
    entries = []
    for db_id, family in families.items():
        entries.append(
            {
                "db_id": db_id,
                "split": family["split"],
                "fingerprint": family.get("lineage_fingerprint"),
                "tables": family.get("tables"),
                "structure": None,
            }
        )
    return payload, entries


def extract_questions(path: pathlib.Path) -> list[str]:
    """Question strings from a dataset file (JSONL or JSON array), tolerant of file shape."""

    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    rows: list[dict]
    if text.lstrip().startswith("["):
        rows = json.loads(text)
    else:
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
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


def contamination_report(modules, fingerprints: dict[str, dict], question_rows: list[dict]) -> dict:
    manifest, families = load_manifest_families()
    family_fingerprints = {entry["fingerprint"] for entry in families if entry.get("fingerprint")}
    family_ids = {entry["db_id"] for entry in families}
    family_structures = []
    schema_source = PROJECT / "repo" / "datasets" / "bird_pg_unified" / "schemas" / "schema.json"
    if schema_source.exists():
        schema_payload = json.loads(schema_source.read_text(encoding="utf-8"))
        raw = schema_payload.get("schemas") or schema_payload.get("databases") or schema_payload
        iterable = raw.values() if isinstance(raw, dict) else raw
        for entry in iterable:
            tables = entry.get("tables") or []
            if tables:
                structure = sorted([[table["name"], sorted(column["name"] for column in table.get("columns") or [])] for table in tables])
                family_structures.append({"db_id": entry.get("db_id"), "structure": structure})

    schema_findings = []
    for module in modules:
        slug = module.SLUG
        db_id = f"pghard_{slug}"
        entry = fingerprints[db_id]
        if db_id in family_ids:
            schema_findings.append({"db_id": db_id, "issue": "db_id already exists in the split manifest"})
        if entry["fingerprint"] in family_fingerprints:
            schema_findings.append({"db_id": db_id, "issue": "lineage fingerprint equals a split-manifest family"})
        best = {"jaccard": 0.0, "db_id": None, "table_jaccard": 0.0, "column_jaccard": 0.0}
        for family in family_structures:
            score, table_score, column_score = structure_jaccard(entry["structure"], family["structure"])
            if score > best["jaccard"]:
                best = {"jaccard": score, "db_id": family["db_id"], "table_jaccard": table_score, "column_jaccard": column_score}
        entry["closest_family"] = best
        if best["jaccard"] >= STRUCTURE_JACCARD_LIMIT:
            schema_findings.append(
                {"db_id": db_id, "issue": f"structure Jaccard {best['jaccard']} vs {best['db_id']}"}
            )

    registry = [json.loads(line) for line in EVAL_REGISTRY.read_text(encoding="utf-8").splitlines() if line.strip()] if EVAL_REGISTRY.exists() else []
    registry_questions = [(row["question"], row["eval_id"]) for row in registry]
    trainable = []
    file_stats = []
    for path in TRAINABLE_FILES:
        questions = extract_questions(path)
        file_stats.append({"path": str(path.relative_to(PROJECT)), "questions": len(questions)})
        trainable.extend((question, str(path.relative_to(PROJECT))) for question in questions)

    def best_matches(candidate: str, pool: list[tuple[str, str]], limit: int = 3) -> list[dict]:
        grams = ngrams(candidate)
        if not grams:
            return []
        matches = []
        for other, label in pool:
            other_grams = ngrams(other)
            if not other_grams:
                continue
            shared = len(grams & other_grams)
            if shared == 0:
                continue
            score = shared / len(grams | other_grams)
            matches.append({"match": label, "jaccard": round(score, 4), "question": other[:160]})
        matches.sort(key=lambda item: -item["jaccard"])
        return matches[:limit]

    exact_registry = {normalise_question(row["question"]) for row in registry}
    exact_trainable = {normalise_question(question) for question, _ in trainable}
    question_findings = []
    max_registry = 0.0
    max_trainable = 0.0
    for row in question_rows:
        normalized = normalise_question(row["question"])
        if normalized in exact_registry:
            question_findings.append({"qid": row["qid"], "issue": "exact question match in the closed eval registry"})
        if normalized in exact_trainable:
            question_findings.append({"qid": row["qid"], "issue": "exact question match in a trainable file"})
        for match in best_matches(row["question"], registry_questions, limit=1):
            max_registry = max(max_registry, match["jaccard"])
            if match["jaccard"] >= QUESTION_JACCARD_LIMIT:
                question_findings.append({"qid": row["qid"], "issue": f"5-gram Jaccard {match['jaccard']} vs registry question"})
        for match in best_matches(row["question"], trainable, limit=1):
            max_trainable = max(max_trainable, match["jaccard"])
            if match["jaccard"] >= QUESTION_JACCARD_LIMIT:
                question_findings.append({"qid": row["qid"], "issue": f"5-gram Jaccard {match['jaccard']} vs trainable question"})

    table_names = set()
    for module in modules:
        for statement in module.DDL:
            match = re.match(r'\s*CREATE TABLE "?([a-z_]+)"?', statement, re.IGNORECASE)
            if match:
                table_names.add(match.group(1))
    family_table_names = set()
    for family in family_structures:
        for table, _columns in family["structure"]:
            family_table_names.add(table)
    # Informational only: BIRD domains share generic table names (`orders`, `users`).
    shared_table_names = sorted(table_names & family_table_names)

    return {
        "verdict": "clean" if not schema_findings and not question_findings else "contaminated",
        "schema": {
            "checked": len(modules),
            "manifest_families": len(families),
            "structure_jaccard_limit": STRUCTURE_JACCARD_LIMIT,
            "findings": schema_findings,
        },
        "questions": {
            "checked": len(question_rows),
            "registry_questions": len(registry_questions),
            "trainable_questions": len(trainable),
            "question_jaccard_limit": QUESTION_JACCARD_LIMIT,
            "max_jaccard_vs_registry": round(max_registry, 4),
            "max_jaccard_vs_trainable": round(max_trainable, 4),
            "findings": question_findings,
            "trainable_files": file_stats,
        },
        "tables": {
            "note": "informational: generic table names recur across BIRD domains; the schema check above is the decisive one",
            "shared_names": shared_table_names,
        },
    }


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build(pgdata: str, new_version: bool, verify_only: bool, only: list[str] | None = None) -> int:
    modules = load_modules(only)
    questions: list[dict] = []
    for module in modules:
        questions.extend(validate_module(module))

    server = Server(pgdata)
    server.ensure_readonly_role()

    if verify_only:
        return verify(server, questions)

    started = _dt.datetime.now(_dt.timezone.utc)
    fingerprints: dict[str, dict] = {}
    schema_entries: list[dict] = []
    per_table_rows: dict[str, dict] = {}

    staged_databases: list[tuple[str, str]] = []
    for module in modules:
        db_id = f"pghard_{module.SLUG}"
        stage_id = f"{db_id}__stage"
        print(f"[build] {db_id}", file=sys.stderr, flush=True)
        server.recreate_database(stage_id)
        server.apply(stage_id, module.DDL)
        rows_by_table = {}
        for table, rows in module.FIXTURES.items():
            rows_by_table[table] = server.insert(stage_id, table, rows)
        with server._connect(stage_id) as conn:  # noqa: SLF001 - ANALYZE after load
            conn.execute("ANALYZE")
        staged_databases.append((stage_id, db_id))
        catalog = server.catalog(stage_id)
        entry = {
            "db_id": db_id,
            "slug": module.SLUG,
            "domain": module.DOMAIN,
            "tables": len(catalog),
            "columns": sum(len(table["columns"]) for table in catalog),
            "fingerprint": fingerprint_entry(catalog),
            "fingerprint_scheme": "pg_hard.catalog.v1 (format_type): sha256 over tables, columns(name,type,not_null), primary keys, foreign keys",
            "structure": structure_of(catalog),
            "ddl_sha256": hashlib.sha256("\n".join(module.DDL).encode("utf-8")).hexdigest(),
            "data_sha256": hashlib.sha256(
                canon(
                    {
                        table: hashlib.sha256(
                            canon(sorted(canon_cell_row(row) for row in server.read_back(stage_id, table))).encode("utf-8")
                        ).hexdigest()
                        for table in module.FIXTURES
                    }
                ).encode("utf-8")
            ).hexdigest(),
            "rows_per_table": {table: rows_by_table[table] for table in rows_by_table},
            "notes": list(module.NOTES),
            "dropped": list(module.DROPPED),
        }
        fingerprints[db_id] = entry
        schema_entries.append(entry)
        per_table_rows[db_id] = rows_by_table

    # ---- questions --------------------------------------------------------
    gold_rows: list[dict] = []
    unresolved: list[str] = []
    stage_for = {final: staged for staged, final in staged_databases}
    for question in questions:
        stage_id = stage_for[question["db_id"]]
        server_result = server.run_gold(stage_id, question["sql"])
        again = server.run_gold(stage_id, question["sql"])
        bag_first = digest_rows(server_result["rows"], "bag")
        bag_second = digest_rows(again["rows"], "bag")
        ordered_first = digest_rows(server_result["rows"], "ordered")
        ordered_second = digest_rows(again["rows"], "ordered")
        deterministic = bag_first == bag_second and ordered_first == ordered_second
        result_sha256 = bag_first if question["comparison"] == "bag" else ordered_first
        if len(server_result["rows"]) == 0:
            unresolved.append(f"{question['qid']}: empty result")
        if not deterministic:
            unresolved.append(f"{question['qid']}: two runs disagree")
        gold_rows.append(
            {
                "qid": question["qid"],
                "db_id": question["db_id"],
                "skill": question["skill"],
                "comparison": question["comparison"],
                "tie_policy": question["tie_policy"],
                "gold_sql": question["sql"],
                "result_sha256": result_sha256,
                "bag_sha256": bag_first,
                "ordered_sha256": ordered_first,
                "n_rows": len(server_result["rows"]),
                "n_columns": len(server_result["columns"]),
                "columns": server_result["columns"],
                "executed_twice": deterministic,
                "fixtures": [{"fixture_id": "default", "db_id": question["db_id"]}],
            }
        )
    if unresolved:
        print("BUILD FAILED — questions that do not meet the contract:", file=sys.stderr)
        for item in unresolved:
            print("  -", item, file=sys.stderr)
        for staged, _final in staged_databases:
            server.drop_database(staged)
        return 1

    # ---- artifacts --------------------------------------------------------
    question_rows = [
        {
            "qid": question["qid"],
            "db_id": question["db_id"],
            "skill": question["skill"],
            "difficulty": question["difficulty"],
            "question": question["question"],
            "evidence": question["evidence"],
            "comparison": question["comparison"],
            "tie_policy": question["tie_policy"],
        }
        for question in questions
    ]

    questions_body = "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in question_rows) + "\n"
    gold_body = "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in gold_rows) + "\n"
    questions_sha = hashlib.sha256(questions_body.encode("utf-8")).hexdigest()
    gold_sha = hashlib.sha256(gold_body.encode("utf-8")).hexdigest()

    existing = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else None
    version = 1
    changelog_entry = None
    frozen_at = started.isoformat(timespec="seconds")
    if existing:
        version = int(existing.get("version", 1))
        previous_freeze = existing.get("freeze") or {}
        changed = (
            previous_freeze.get("questions_sha256") != questions_sha
            or previous_freeze.get("gold_sha256") != gold_sha
        )
        if changed and not new_version:
            print(
                "REFUSING TO OVERWRITE A FROZEN SET.\n"
                f"  frozen questions sha256: {previous_freeze.get('questions_sha256')}\n"
                f"  rebuilt questions sha256: {questions_sha}\n"
                "The questions and gold SQL are frozen once written. If the change is intended, "
                "rerun with --new-version to create version "
                f"{version + 1} and append a CHANGELOG entry.",
                file=sys.stderr,
            )
            for staged, _final in staged_databases:
                server.drop_database(staged)
            return 1
        if not changed:
            frozen_at = existing.get("frozen_at_utc", frozen_at)
        if changed:
            version += 1
            changelog_entry = (
                f"\n## v{version} — {started.isoformat(timespec='seconds')}\n\n"
                f"- questions sha256: `{previous_freeze.get('questions_sha256')}` → `{questions_sha}`\n"
                f"- gold sha256: `{previous_freeze.get('gold_sha256')}` → `{gold_sha}`\n"
                f"- schemas: {len(schema_entries)}, questions: {len(question_rows)}\n"
            )

    data_bundle = hashlib.sha256(canon({entry["db_id"]: entry["data_sha256"] for entry in schema_entries}).encode("utf-8")).hexdigest()
    ddl_bundle = hashlib.sha256(canon({entry["db_id"]: entry["ddl_sha256"] for entry in schema_entries}).encode("utf-8")).hexdigest()

    contamination = contamination_report(modules, fingerprints, question_rows)
    if contamination["verdict"] != "clean":
        print("CONTAMINATION CHECK FAILED:", file=sys.stderr)
        print(json.dumps(contamination, indent=2, ensure_ascii=False)[:4000], file=sys.stderr)
        for staged, _final in staged_databases:
            server.drop_database(staged)
        return 1

    per_skill = collections.Counter(row["skill"] for row in question_rows)
    per_db = collections.Counter(row["db_id"] for row in question_rows)
    comparison_counts = collections.Counter(row["comparison"] for row in question_rows)

    manifest = {
        "name": "pg_hard",
        "version": version,
        "status": "frozen",
        "frozen_at_utc": frozen_at,
        "purpose": "pre-closed hand-authored PostgreSQL hard test set; schema families and questions did not exist before this build",
        "playbook": "docs/playbook/POSTGRES_TEXT2SQL_LORA.md sections 3 and 6 (PG-hard: prediction must match the oracle on all fixtures)",
        "contract": {
            "schema_count": [10, 15],
            "questions_per_schema": [8, 12],
            "comparison_modes": ["bag", "ordered"],
            "skills": SKILLS,
            "scoring": "share of questions where the prediction matches the gold result under the question's comparison mode on every fixture",
            "student_visible_fields": ["question", "evidence"],
            "evaluation_fields": ["qid", "db_id", "skill", "comparison", "tie_policy"],
        },
        "server": {
            "engine": server.version,
            "datadir": server.pgdata,
            "connection": f"unix socket host={server.pgdata} dbname=pghard_<slug>",
            "timezone": TIMEZONE,
            "statement_timeout": STATEMENT_TIMEOUT,
            "readonly_role": {"name": READONLY_ROLE, "password": READONLY_PASSWORD, "grants": ["pg_read_all_data", "default_transaction_read_only=on"]},
        },
        "schemas": schema_entries,
        "questions": {
            "n": len(question_rows),
            "per_skill": dict(sorted(per_skill.items())),
            "per_db": dict(sorted(per_db.items())),
            "comparison": dict(sorted(comparison_counts.items())),
        },
        "freeze": {
            "algorithm": "sha256",
            "questions_sha256": questions_sha,
            "gold_sha256": gold_sha,
            "ddl_bundle_sha256": ddl_bundle,
            "data_bundle_sha256": data_bundle,
            "rule": "questions.jsonl and gold.jsonl are written once. Any later change creates version+1 and appends CHANGELOG.md.",
            "changelog": "CHANGELOG.md",
        },
        "artifacts": {
            "questions": "questions.jsonl",
            "gold": "gold.jsonl",
            "manifest": "manifest.json",
            "report": "report.json",
        },
    }

    report = {
        "summary": {
            "schemas": len(schema_entries),
            "questions": len(question_rows),
            "tables": sum(entry["tables"] for entry in schema_entries),
            "rows": sum(sum(entry["rows_per_table"].values()) for entry in schema_entries),
            "per_skill": dict(sorted(per_skill.items())),
            "comparison": dict(sorted(comparison_counts.items())),
            "ordered_share": round(comparison_counts.get("ordered", 0) / len(question_rows), 4),
            "difficulty": dict(sorted(collections.Counter(row["difficulty"] for row in question_rows).items())),
        },
        "feature_usage": {
            name: sum(1 for row in gold_rows if re.search(pattern, row["gold_sql"]))
            for name, pattern in FEATURES.items()
        },
        "execution": {
            "attempted": len(question_rows),
            "executed": sum(1 for row in gold_rows if row["executed_twice"]),
            "executable_share": round(sum(1 for row in gold_rows if row["executed_twice"]) / len(question_rows), 4),
            "deterministic_share": round(sum(1 for row in gold_rows if row["executed_twice"]) / len(question_rows), 4),
            "two_run_check": "every gold SQL executed twice in a fresh transaction; bag and ordered digests compared",
            "empty_results": sum(1 for row in gold_rows if row["n_rows"] == 0),
            "rows_per_question": {"min": min(row["n_rows"] for row in gold_rows), "max": max(row["n_rows"] for row in gold_rows)},
        },
        "per_db": [
            {
                "db_id": entry["db_id"],
                "fingerprint": entry["fingerprint"],
                "tables": entry["tables"],
                "columns": entry["columns"],
                "rows": sum(entry["rows_per_table"].values()),
                "questions": per_db[entry["db_id"]],
                "skills": sorted({row["skill"] for row in question_rows if row["db_id"] == entry["db_id"]}),
                "dropped": entry["dropped"],
                "closest_family": entry.get("closest_family"),
            }
            for entry in schema_entries
        ],
        "dropped_skills": [
            {
                "db_id": entry["db_id"],
                "skill": item["skill"],
                "reason": item["reason"],
                "replacement": item["replacement"],
            }
            for entry in schema_entries
            for item in entry["dropped"]
        ],
        "contamination": contamination,
        "freeze": {
            "version": version,
            "questions_sha256": questions_sha,
            "gold_sha256": gold_sha,
            "ddl_bundle_sha256": ddl_bundle,
            "data_bundle_sha256": data_bundle,
            "questions_file": str(QUESTIONS_FILE),
            "gold_file": str(GOLD_FILE),
        },
        "built_at_utc": started.isoformat(timespec="seconds"),
    }

    for staged, final in staged_databases:
        server.promote_database(staged, final)

    QUESTIONS_FILE.write_text(questions_body, encoding="utf-8")
    GOLD_FILE.write_text(gold_body, encoding="utf-8")
    MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    REPORT_FILE.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if CHANGELOG.exists() and not changelog_entry:
        pass
    elif changelog_entry:
        CHANGELOG.write_text(CHANGELOG.read_text(encoding="utf-8").rstrip() + "\n" + changelog_entry, encoding="utf-8")
    elif not CHANGELOG.exists():
        CHANGELOG.write_text(
            "# PG-hard changelog\n\n"
            f"## v{version} — {started.isoformat(timespec='seconds')}\n\n"
            f"- first frozen build: {len(schema_entries)} schemas, {len(question_rows)} questions\n"
            f"- questions sha256: `{questions_sha}`\n"
            f"- gold sha256: `{gold_sha}`\n"
            "- rule: any later change creates a new version here; the frozen files are never edited in place\n",
            encoding="utf-8",
        )

    print(
        f"BUILT v{version}: {len(schema_entries)} schemas, {len(question_rows)} questions, "
        f"{report['summary']['rows']} fixture rows; executable {report['execution']['executable_share']:.2%}; "
        f"contamination {contamination['verdict']}"
    )
    print(f"  questions sha256 {questions_sha}")
    print(f"  gold sha256      {gold_sha}")
    return 0


def canon_cell_row(row: dict) -> str:
    return canon({key: canon_cell(row[key]) for key in sorted(row)})


def verify(server: Server, questions: list[dict]) -> int:
    """Re-execute the frozen gold SQL and compare against the frozen hashes."""

    if not MANIFEST.exists() or not GOLD_FILE.exists():
        print("nothing frozen to verify: run the build first", file=sys.stderr)
        return 1
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    frozen = manifest["freeze"]
    questions_body = QUESTIONS_FILE.read_text(encoding="utf-8")
    gold_body = GOLD_FILE.read_text(encoding="utf-8")
    checks = []
    checks.append(("questions.jsonl matches the frozen hash", hashlib.sha256(questions_body.encode()).hexdigest() == frozen["questions_sha256"]))
    checks.append(("gold.jsonl matches the frozen hash", hashlib.sha256(gold_body.encode()).hexdigest() == frozen["gold_sha256"]))
    gold_rows = [json.loads(line) for line in gold_body.splitlines() if line.strip()]
    failures = []
    for row in gold_rows:
        fresh = server.run_gold(row["db_id"], row["gold_sql"])
        digest = digest_rows(fresh["rows"], row["comparison"])
        if digest != row["result_sha256"]:
            failures.append(row["qid"])
    checks.append((f"all {len(gold_rows)} frozen results reproduce on a fresh execution", not failures))
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if failures:
        print("  mismatched qids:", failures[:20], file=sys.stderr)
        return 1
    print(f"VERIFY PASS: {len(gold_rows)} questions, v{manifest['version']}")
    return 0


def ensure(pgdata: str) -> int:
    """Start the fixture instance on an existing datadir and report what is there."""

    path = pathlib.Path(pgdata)
    if not path.exists():
        print(
            f"no fixture instance at {path}. Build it with:\n"
            f"  python datasets/pg_hard/build_pg_hard.py --pgdata {path}",
            file=sys.stderr,
        )
        return 1
    server = Server(pgdata)
    with server._connect("postgres") as conn:  # noqa: SLF001 - read-only inventory
        databases = [row[0] for row in conn.execute("SELECT datname FROM pg_database WHERE datname LIKE 'pghard_%' ORDER BY 1").fetchall()]
    print(f"fixture instance ready at {server.pgdata}")
    print(f"  {server.version}")
    print(f"  {len(databases)} pghard databases: {', '.join(databases) if databases else '(none — rebuild needed)'}")
    return 0 if databases else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build and freeze the PG-hard test set.")
    parser.add_argument("--pgdata", default=DEFAULT_PGDATA, help="PostgreSQL datadir of the fixture instance")
    parser.add_argument("--new-version", action="store_true", help="intended rebuild: bump version and append a changelog entry")
    parser.add_argument("--verify", action="store_true", help="only re-execute the frozen gold and compare hashes")
    parser.add_argument(
        "--ensure",
        action="store_true",
        help="start the fixture instance at --pgdata if it is not running and list its databases, without rebuilding",
    )
    parser.add_argument("--only", default="", help="development aid: comma-separated schema modules to build")
    args = parser.parse_args(argv)
    only = [name.strip() for name in args.only.split(",") if name.strip()] or None
    if args.ensure:
        return ensure(args.pgdata)
    return build(args.pgdata, args.new_version, args.verify, only)


if __name__ == "__main__":
    raise SystemExit(main())
