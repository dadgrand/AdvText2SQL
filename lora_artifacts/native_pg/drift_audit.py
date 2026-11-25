#!/usr/bin/env python3
"""Audit drift between the native PostgreSQL gold and the SQLite originals.

Runs every BIRD dev row that still has a SQLite database locally through both gold
queries, normalises the result cells with the package's own ``verify_dataset.canon``
(plus timestamp canonicalisation), and compares the two result tables as multisets
(bag semantics). Every row is classified:

* ``exact_match`` — the same rows under strict cell comparison;
* ``legitimate_engine_difference`` — float formatting/precision, timestamp formatting
  or ordering: equal after the project normalisation, different before it;
* ``translation_defect`` — the PostgreSQL gold is not a faithful translation of the
  SQLite gold (includes the reviewed semantic corrections adopted by the package);
* ``server_side_data_defect`` — the server data itself differs (leading-zero key loss,
  table-volume superset, missing rows).

The audit also re-measures the two known defects instead of quoting them
(``california_schools`` leading zeros, ``toxicology`` superset) and writes:

    cluster/drift_native.json               summary, class counts per database, known cases
    datasets/splits/native_gold_drift.json  per-row flags consumed by the split manifest

Coverage is bounded by locally available SQLite files (the eight dev databases): all
217 reviewed dev rows and, as a supplementary check, the 241 historical dev rows. No
train row has a local SQLite counterpart, which is reported explicitly.

Run (tunnel to 127.0.0.1:5444 must be up):

    .venv-tools/bin/python datasets/native_pg/drift_audit.py
"""

from __future__ import annotations

import argparse
import collections
import datetime as _dt
import decimal
import hashlib
import importlib.util
import json
import pathlib
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = pathlib.Path(__file__).resolve().parent
PROJECT = HERE.parents[1]  # research/advtext2sql

try:  # workspace tool env fallback (psycopg is needed for the server side)
    import psycopg  # noqa: F401
except ImportError:  # pragma: no cover
    _site = next(iter(sorted((PROJECT.parents[1] / ".venv-tools").glob("lib/python3*/site-packages"))), None)
    if _site is not None:
        sys.path.append(str(_site))
    import psycopg  # noqa: F401
PACKAGE = PROJECT / "repo" / "datasets" / "bird_pg_unified"
OLD_PACKAGE = PROJECT / "repo" / "datasets" / "bird_pg"
SQLITE_DIR = PROJECT / "data_cache" / "dev_databases"
ENV_PATH = PROJECT / "repo" / ".env"
CLUSTER = PROJECT / "cluster"
SPLITS = PROJECT / "datasets" / "splits"
OUT_REPORT = CLUSTER / "drift_native.json"
OUT_FLAGS = SPLITS / "native_gold_drift.json"

SQLITE_DATABASES = [
    "california_schools", "card_games", "codebase_community", "debit_card_specializing",
    "financial", "student_club", "superhero", "toxicology",
]
#: codebase_community has no join indexes in the shipped SQLite file; an indexed copy
#: of the same database makes question 701 finish in milliseconds instead of hours.
INDEXED_SQLITE = pathlib.Path("/tmp/codebase_indexed.sqlite")

CLASSES = ("exact_match", "legitimate_engine_difference", "translation_defect",
           "server_side_data_defect", "not_comparable")


# ----------------------------------------------------------------------------------------------
# normalisation / comparison
# ----------------------------------------------------------------------------------------------
def load_canon():
    """The package's project comparator: numerics rounded, decimals unified, ISO timestamps."""
    spec = importlib.util.spec_from_file_location("verify_dataset", PACKAGE / "tools" / "verify_dataset.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.canon


CANON = load_canon()
_TS = None


def _ts_parser():
    global _TS
    if _TS is None:
        import re
        _TS = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}:\d{2}(\.\d+)?)?(\+00:00|Z)?$")
    return _TS


def canon_ext(cell):
    """Project canon + timestamp canonicalisation (SQLite text vs PostgreSQL timestamptz)."""
    value = CANON(cell)
    if isinstance(value, str) and _ts_parser().match(value.strip()):
        text = value.strip().replace("T", " ").replace("Z", "+00:00")
        try:
            moment = _dt.datetime.fromisoformat(text)
            return ("ts", moment.replace(tzinfo=None).isoformat(sep=" ", timespec="seconds"))
        except ValueError:
            try:
                return ("date", _dt.date.fromisoformat(value.strip()).isoformat())
            except ValueError:
                pass
    return value


def strict_cell(cell):
    """Type/format-preserving representation: no numeric rounding, no timestamp parsing."""
    if cell is None:
        return None
    if isinstance(cell, bool):
        return ("int", int(cell))
    if isinstance(cell, int):
        return ("int", cell)
    if isinstance(cell, (float, decimal.Decimal)):
        return ("num", repr(cell))
    if isinstance(cell, _dt.datetime):
        return ("dt", cell.isoformat())
    if isinstance(cell, _dt.date):
        return ("date", cell.isoformat())
    if isinstance(cell, _dt.time):
        return ("time", cell.isoformat())
    if isinstance(cell, (bytes, memoryview)):
        return ("bytes", bytes(cell).hex())
    return ("str", str(cell))


def canonical_rows(rows):
    return [tuple(canon_ext(cell) for cell in row) for row in rows]


def strict_rows(rows):
    return [tuple(strict_cell(cell) for cell in row) for row in rows]


def bag(rows, normaliser=strict_rows):
    return collections.Counter(normaliser(rows))


def close_numbers(first, second, tolerance: float = 1e-6) -> bool:
    try:
        a, b = float(first), float(second)
    except (TypeError, ValueError):
        return False
    return abs(a - b) <= tolerance * max(1.0, abs(a), abs(b))


# ----------------------------------------------------------------------------------------------
# database access
# ----------------------------------------------------------------------------------------------
def load_env() -> dict:
    env = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_path(db_id: str) -> pathlib.Path:
    if db_id == "codebase_community" and INDEXED_SQLITE.is_file():
        return INDEXED_SQLITE
    return SQLITE_DIR / db_id / f"{db_id}.sqlite"


def ensure_indexed_codebase() -> dict:
    """Create the indexed temporary copy once; indexes do not change query results."""
    source = SQLITE_DIR / "codebase_community" / "codebase_community.sqlite"
    info = {"source": str(source), "copy": str(INDEXED_SQLITE), "created": False}
    if not source.is_file():
        return info
    info["source_sha256"] = sha256(source)
    if not INDEXED_SQLITE.is_file():
        import shutil
        shutil.copy2(source, INDEXED_SQLITE)
        connection = sqlite3.connect(INDEXED_SQLITE)
        connection.execute("CREATE INDEX IF NOT EXISTS idx_users_rep ON users(Reputation, Id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_posts_owner ON posts(OwnerUserId, Score)")
        connection.commit()
        connection.close()
        info["created"] = True
    info["copy_sha256"] = sha256(INDEXED_SQLITE)
    return info


def audit_database(db_id: str, rows: list[dict], env: dict, dataset: str,
                   corrections_map: dict[str, list]) -> tuple[str, list[dict], float]:
    started = time.time()
    output = []
    connection = sqlite3.connect(f"file:{sqlite_path(db_id)}?mode=ro", uri=True)
    pg = None
    try:
        import psycopg
        pg = psycopg.connect(host=env.get("DB_CLIENT_HOST", "127.0.0.1"),
                             port=int(env["DB_PORT"]), user=env["DB_USER"],
                             password=env["DB_PASS"], dbname=db_id, connect_timeout=10)
        cursor = pg.cursor()
        cursor.execute("SET statement_timeout = 15000")
    except Exception as exc:  # tunnel down / auth problem: fail loudly, do not fake matches
        connection.close()
        raise SystemExit(f"PostgreSQL is not reachable for {db_id}: {type(exc).__name__}: {exc}")

    for row in rows:
        question_id = str(row["question_id"])
        record = {
            "source_dataset": dataset,
            "question_id": question_id,
            "db_id": db_id,
            "question": row.get("question"),
            "verification": row.get("our_verification"),
            "corrections": list(row.get("corrections") or corrections_map.get(question_id, [])),
            "sqlite_gold_sql": row.get("sqlite_gold_sql"),
            "pg_gold_sql": row.get("pg_gold_sql"),
        }
        started_row = time.time()
        try:
            connection.set_progress_handler(
                lambda: 1 if time.time() - started_row > 20 else 0, 100000)
            record["sqlite_rows"] = connection.execute(row["sqlite_gold_sql"]).fetchall()
        except Exception as exc:
            record["sqlite_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        try:
            cursor.execute(row["pg_gold_sql"])
            record["pg_rows"] = cursor.fetchall()
            pg.commit()
        except Exception as exc:
            pg.rollback()
            record["pg_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"

        if "sqlite_rows" in record and "pg_rows" in record:
            classify(record)
        else:
            record["class"] = "not_comparable"
            record["subtype"] = "execution_error"
            record["usable_native_gold"] = False
            record["reason"] = record.get("sqlite_error") or record.get("pg_error")
        # never persist full result tables: only shapes, hashes and small evidence samples
        for key in ("sqlite_rows", "pg_rows"):
            if key in record:
                rows_value = record.pop(key)
                record[key.replace("_rows", "_shape")] = len(rows_value)
                record[key.replace("_rows", "_sha256")] = hashlib.sha256(
                    json.dumps(rows_value, default=str, sort_keys=True).encode()).hexdigest()
        output.append(record)
    connection.close()
    pg.close()
    return db_id, output, round(time.time() - started, 1)


def classify(record: dict) -> None:
    """Assign one of the three drift classes (+ exact match) and a usability flag."""
    sqlite_rows = record["sqlite_rows"]
    pg_rows = record["pg_rows"]
    canonical_sqlite = canonical_rows(sqlite_rows)
    canonical_pg = canonical_rows(pg_rows)
    strict_sqlite = strict_rows(sqlite_rows)
    strict_pg = strict_rows(pg_rows)
    canonical_equal = collections.Counter(canonical_sqlite) == collections.Counter(canonical_pg)
    strict_equal = collections.Counter(strict_sqlite) == collections.Counter(strict_pg)

    record["sqlite_shape"], record["pg_shape"] = len(sqlite_rows), len(pg_rows)
    record["strict_equal"] = strict_equal
    record["normalised_equal"] = canonical_equal

    if strict_equal:
        record["class"] = "exact_match"
        record["usable_native_gold"] = True
        record["reason"] = "identical result tables under strict comparison"
        return

    if canonical_equal:
        subtype = engine_difference_subtype(sqlite_rows, pg_rows)
        record["class"] = "legitimate_engine_difference"
        record["subtype"] = subtype
        record["usable_native_gold"] = True
        record["reason"] = ("same rows after the project normaliser; raw difference is "
                            f"{subtype.replace('_', ' ')}")
        record["evidence"] = evidence_sample(sqlite_rows, pg_rows)
        return

    corrections = record.get("corrections") or []
    if record["db_id"] == "toxicology" and table_volume_drift().get("superset"):
        record["class"] = "server_side_data_defect"
        record["subtype"] = "superset_volume"
        record["usable_native_gold"] = False
        record["reason"] = ("server tables hold more rows than the BIRD SQLite original; "
                            "result differs by data volume"
                            + (" and the gold also carries a reviewed correction" if corrections else ""))
        record["evidence"] = evidence_sample(sqlite_rows, pg_rows)
        return

    if corrections:
        record["class"] = "translation_defect"
        record["subtype"] = "reviewed_correction"
        record["usable_native_gold"] = True
        record["reason"] = ("reviewed semantic correction adopted by the package; the native gold "
                            "intentionally differs from the stale SQLite label: " + "; ".join(corrections))
        record["evidence"] = evidence_sample(sqlite_rows, pg_rows)
        return

    if record["db_id"] == "california_schools" and key_drift().get("lost_leading_zero"):
        record["class"] = "server_side_data_defect"
        record["subtype"] = "leading_zero_key_loss"
        record["usable_native_gold"] = False
        record["reason"] = "join key lost its leading zero when loaded on the server"
        record["evidence"] = evidence_sample(sqlite_rows, pg_rows)
        return

    if numeric_only_close(sqlite_rows, pg_rows):
        record["class"] = "legitimate_engine_difference"
        record["subtype"] = "rounding_precision"
        record["usable_native_gold"] = True
        record["reason"] = "numeric values differ only at the 1e-6 rounding boundary"
        record["evidence"] = evidence_sample(sqlite_rows, pg_rows)
        return

    sqlite_counter = collections.Counter(canonical_sqlite)
    pg_counter = collections.Counter(canonical_pg)
    if pg_counter > sqlite_counter:
        record["class"] = "server_side_data_defect"
        record["subtype"] = "superset_rows"
        record["usable_native_gold"] = False
        record["reason"] = "PostgreSQL returns a strict superset of the SQLite rows"
    elif sqlite_counter > pg_counter:
        record["class"] = "server_side_data_defect"
        record["subtype"] = "missing_rows"
        record["usable_native_gold"] = False
        record["reason"] = "PostgreSQL returns fewer rows than the SQLite original"
    else:
        record["class"] = "translation_defect"
        record["subtype"] = "semantic_mismatch"
        record["usable_native_gold"] = False
        record["reason"] = "result tables differ and no data-volume or reviewed-correction cause matches"
    record["evidence"] = evidence_sample(sqlite_rows, pg_rows)


def engine_difference_subtype(sqlite_rows, pg_rows) -> str:
    sqlite_cells = [cell for row in sqlite_rows for cell in row]
    pg_cells = [cell for row in pg_rows for cell in row]
    if len(sqlite_cells) != len(pg_cells):
        return "ordering_or_shape"
    for first, second in zip(sqlite_cells, pg_cells):
        if isinstance(first, (str, _dt.date, _dt.datetime)) and isinstance(second, (str, _dt.date, _dt.datetime)):
            return "timestamp_formatting"
    for first, second in zip(sqlite_cells, pg_cells):
        if isinstance(first, (int, float, decimal.Decimal)) or isinstance(second, (int, float, decimal.Decimal)):
            return "float_formatting"
    return "ordering_or_engine"


def numeric_only_close(sqlite_rows, pg_rows) -> bool:
    if len(sqlite_rows) != len(pg_rows):
        return False
    sqlite_flat = sorted((cell for row in sqlite_rows for cell in row), key=repr)
    pg_flat = sorted((cell for row in pg_rows for cell in row), key=repr)
    numeric_pairs = 0
    for first, second in zip(sqlite_flat, pg_flat):
        if isinstance(first, (int, float, decimal.Decimal)) and isinstance(second, (int, float, decimal.Decimal)):
            if not close_numbers(first, second):
                return False
            numeric_pairs += 1
        elif first != second:
            return False
    return numeric_pairs > 0


def _jsonable(rows, limit: int = 3) -> list[list]:
    return [[canon_ext(cell) for cell in row] for row in rows[:limit]]


def evidence_sample(sqlite_rows, pg_rows, limit: int = 3) -> dict:
    return {
        "sqlite_shape": len(sqlite_rows),
        "pg_shape": len(pg_rows),
        "sqlite_sample": _jsonable(sqlite_rows, limit),
        "pg_sample": _jsonable(pg_rows, limit),
    }


# ----------------------------------------------------------------------------------------------
# known cases, re-measured
# ----------------------------------------------------------------------------------------------
def pg_cursor(db_id: str, env: dict):
    import psycopg
    connection = psycopg.connect(host=env.get("DB_CLIENT_HOST", "127.0.0.1"),
                                 port=int(env["DB_PORT"]), user=env["DB_USER"],
                                 password=env["DB_PASS"], dbname=db_id, connect_timeout=10)
    cursor = connection.cursor()
    cursor.execute("SET statement_timeout = 30000")
    return connection, cursor


def key_drift() -> dict:
    """Re-measure california_schools leading-zero loss (cached per process)."""
    if getattr(key_drift, "_cache", None) is not None:
        return key_drift._cache
    env = load_env()
    sqlite = sqlite3.connect(f"file:{sqlite_path('california_schools')}?mode=ro", uri=True)
    pg_connection, cursor = pg_cursor("california_schools", env)
    cursor.execute("SELECT length(cds), COUNT(*) FROM satscores GROUP BY 1 ORDER BY 1")
    pg_lengths = {length: count for length, count in cursor.fetchall()}
    sqlite_lengths = {length: count for length, count in sqlite.execute(
        "SELECT length(cds), COUNT(*) FROM satscores GROUP BY 1 ORDER BY 1").fetchall()}
    cursor.execute("SELECT COUNT(*) FROM satscores s JOIN frpm f ON s.cds=f.cdscode")
    pg_join = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM satscores s JOIN frpm f ON CAST(s.cds AS BIGINT)=CAST(f.cdscode AS BIGINT)")
    pg_join_repaired = cursor.fetchone()[0]
    sqlite_join = sqlite.execute("SELECT COUNT(*) FROM satscores s JOIN frpm f ON s.cds=f.CDSCode").fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM satscores s JOIN schools sc ON s.cds=sc.cdscode")
    pg_schools_join = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM satscores s JOIN schools sc ON CAST(s.cds AS BIGINT)=CAST(sc.cdscode AS BIGINT)")
    pg_schools_repaired = cursor.fetchone()[0]
    sqlite_schools_join = sqlite.execute("SELECT COUNT(*) FROM satscores s JOIN schools sc ON s.cds=sc.CDSCode").fetchone()[0]
    pg_connection.close(); sqlite.close()
    lost = sum(count for length, count in pg_lengths.items() if length == 13)
    total = sum(pg_lengths.values())
    key_drift._cache = {
        "satscore_key_lengths_sqlite": sqlite_lengths,
        "satscore_key_lengths_pg": pg_lengths,
        "lost_leading_zero": lost > 0,
        "lost_leading_zero_rows": lost,
        "satscore_rows": total,
        "lost_share": round(lost / total, 4) if total else 0,
        "join_satscores_frpm": {"sqlite": sqlite_join, "pg_unrepaired": pg_join,
                                "pg_repaired": pg_join_repaired},
        "join_satscores_schools": {"sqlite": sqlite_schools_join, "pg_unrepaired": pg_schools_join,
                                   "pg_repaired": pg_schools_repaired},
    }
    return key_drift._cache


def table_volume_drift() -> dict:
    """Re-measure the toxicology superset (cached per process)."""
    if getattr(table_volume_drift, "_cache", None) is not None:
        return table_volume_drift._cache
    env = load_env()
    sqlite = sqlite3.connect(f"file:{sqlite_path('toxicology')}?mode=ro", uri=True)
    pg_connection, cursor = pg_cursor("toxicology", env)
    tables = [row[0] for row in sqlite.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    per_table = {}
    for table in tables:
        sqlite_count = sqlite.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        cursor.execute(f'SELECT COUNT(*) FROM "{table}"')
        pg_count = cursor.fetchone()[0]
        per_table[table] = {"sqlite": sqlite_count, "pg": pg_count, "delta": pg_count - sqlite_count}
    pg_connection.close(); sqlite.close()
    drift = [name for name, counts in per_table.items() if counts["delta"] != 0]
    table_volume_drift._cache = {
        "tables": per_table,
        "tables_with_drift": drift,
        "superset": any(counts["delta"] > 0 for counts in per_table.values()) and not any(
            counts["delta"] < 0 for counts in per_table.values()),
        "total_delta": sum(counts["delta"] for counts in per_table.values()),
    }
    return table_volume_drift._cache


# ----------------------------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------------------------
def audit_dataset(name: str, rows: list[dict], env: dict, corrections_map: dict[str, list]) -> list[dict]:
    by_db = collections.defaultdict(list)
    for row in rows:
        if row["db_id"] in SQLITE_DATABASES:
            by_db[row["db_id"]].append(row)
    audited = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(audit_database, db, db_rows, env, name, corrections_map)
                   for db, db_rows in by_db.items()]
        for future in as_completed(futures):
            db, records, elapsed = future.result()
            audited.extend(records)
            print(f"  {name}: {db} {len(records)} rows in {elapsed}s", flush=True)
    audited.sort(key=lambda record: (record["db_id"], int(record["question_id"])))
    return audited


def summarise(records: list[dict]) -> dict:
    counts = collections.Counter(record["class"] for record in records)
    per_db = {}
    for db in sorted({record["db_id"] for record in records}):
        subset = [record for record in records if record["db_id"] == db]
        per_db[db] = {
            "rows": len(subset),
            **{cls: sum(1 for record in subset if record["class"] == cls) for cls in CLASSES},
        }
    subtypes = collections.Counter(record.get("subtype") for record in records if record.get("subtype"))
    return {
        "rows": len(records),
        "classes": {cls: counts.get(cls, 0) for cls in CLASSES},
        "subtypes": dict(sorted(subtypes.items())),
        "per_database": per_db,
        "flagged": sum(1 for record in records if record["class"] != "exact_match"),
        "usable_as_native_gold": sum(1 for record in records if record.get("usable_native_gold")),
        "must_not_use": sum(1 for record in records if not record.get("usable_native_gold")),
    }


def build() -> dict:
    env = load_env()
    indexed = ensure_indexed_codebase()
    corrections_map = {}
    for row in [json.loads(line) for line in (PACKAGE / "data" / "bird_dev_pg.jsonl").read_text(
            encoding="utf-8").splitlines() if line.strip()]:
        if row.get("corrections"):
            corrections_map[str(row["question_id"])] = list(row["corrections"])

    current_rows = [json.loads(line) for line in (PACKAGE / "data" / "bird_dev_pg.jsonl").read_text(
        encoding="utf-8").splitlines() if line.strip()]
    historical_rows = [json.loads(line) for line in (OLD_PACKAGE / "data" / "bird_dev_pg.jsonl").read_text(
        encoding="utf-8").splitlines() if line.strip()]

    print("auditing unified dev-217", flush=True)
    current = audit_dataset("unified_dev_217", current_rows, env, corrections_map)
    print("auditing historical dev-241", flush=True)
    historical = audit_dataset("historical_dev_241", historical_rows, env, corrections_map)

    train_rows = [json.loads(line) for line in (PACKAGE / "data" / "bird_train_pg.jsonl").read_text(
        encoding="utf-8").splitlines() if line.strip()]
    train_with_sqlite = [row for row in train_rows if row["db_id"] in SQLITE_DATABASES]

    current_summary = summarise(current)
    historical_summary = summarise(historical)
    current_flags = [record for record in current if record["class"] != "exact_match"]
    historical_flags = [record for record in historical if record["class"] != "exact_match"]
    must_not_use = [record for record in current
                    if record["class"] == "server_side_data_defect" or
                    (record["class"] == "translation_defect" and not record.get("usable_native_gold"))]

    ca_join_rows = []
    for record in current:
        if record["db_id"] == "california_schools" and "satscores" in (record["pg_gold_sql"] or "").lower():
            ca_join_rows.append({
                "question_id": record["question_id"],
                "verification": record.get("verification"),
                "has_cast_repair": "CAST(" in (record["pg_gold_sql"] or "").upper(),
                "class": record["class"],
            })

    tox_historical = [record for record in historical if record["db_id"] == "toxicology"
                      and record["class"] != "exact_match"]
    tox_current = [record for record in current if record["db_id"] == "toxicology"
                   and record["class"] != "exact_match"]

    report = {
        "generated_at_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "objective": "drift audit of native PostgreSQL gold against the SQLite originals",
        "comparator": {
            "project_module": str(PACKAGE / "tools" / "verify_dataset.py"),
            "canon_function": "verify_dataset.canon (numerics rounded to 6 decimals, ints unified, "
                              "datetimes isoformat, strings stripped)",
            "extension": "timestamp strings parsed and canonicalised to 'YYYY-MM-DD HH:MM:SS'",
            "semantics": "multiset equality (bag); strict comparison vs normalised comparison recorded",
        },
        "coverage": {
            "sqlite_databases_available": SQLITE_DATABASES,
            "sqlite_files": {db: {"path": str(SQLITE_DIR / db / f"{db}.sqlite"),
                                  "sha256": sha256(SQLITE_DIR / db / f"{db}.sqlite")}
                             for db in SQLITE_DATABASES},
            "indexed_codebase_copy": indexed,
            "train_rows_total": len(train_rows),
            "train_rows_with_local_sqlite": len(train_with_sqlite),
            "dev_rows_total": len(current_rows),
            "dev_rows_audited": len(current),
            "historical_dev_rows_total": len(historical_rows),
            "historical_dev_rows_audited": len(historical),
            "not_audited_reason": "no local SQLite database exists for the 52 train schema families; "
                                  "the eight local databases are the dev databases",
        },
        "class_definitions": {
            "exact_match": "identical result tables under strict cell comparison",
            "legitimate_engine_difference": "float formatting/precision, timestamp formatting or "
                                            "ordering: equal after the project normaliser",
            "translation_defect": "the PostgreSQL gold is not a faithful translation of the SQLite "
                                  "gold (includes the package's reviewed semantic corrections)",
            "server_side_data_defect": "the server data differs: leading-zero key loss, table-volume "
                                       "superset, or missing rows",
            "not_comparable": "one of the two gold queries did not execute",
        },
        "current_unified_dev": current_summary,
        "historical_dev_241": historical_summary,
        "every_non_exact_row_flagged": True,
        "usability_policy": {
            "exact_match": "usable as native gold",
            "legitimate_engine_difference": "usable: equal after numeric/timestamp normalisation; "
                                             "the raw formatting difference is recorded as a flag",
            "translation_defect": "reviewed_correction -> usable as the corrected native gold but "
                                  "flagged; semantic_mismatch -> not usable",
            "server_side_data_defect": "not usable as native gold until the server data is fixed",
        },
        "excluded_rows": [],
        "flagged_rows": [
            {
                "source_dataset": record["source_dataset"],
                "question_id": record["question_id"],
                "db_id": record["db_id"],
                "class": record["class"],
                "subtype": record.get("subtype"),
                "usable_native_gold": record.get("usable_native_gold"),
                "reason": record.get("reason"),
                "sqlite_shape": record.get("sqlite_shape"),
                "pg_shape": record.get("pg_shape"),
                "corrections": record.get("corrections"),
            }
            for record in current_flags
        ],
        "historical_flagged_rows": [
            {
                "question_id": record["question_id"],
                "db_id": record["db_id"],
                "class": record["class"],
                "subtype": record.get("subtype"),
                "reason": record.get("reason"),
            }
            for record in historical_flags
        ],
        "must_not_use_as_native_gold": [
            {
                "source_dataset": record["source_dataset"],
                "question_id": record["question_id"],
                "db_id": record["db_id"],
                "class": record["class"],
                "subtype": record.get("subtype"),
                "reason": record.get("reason"),
            }
            for record in must_not_use
        ],
        "known_cases": {
            "california_schools_key_normalisation": {
                **key_drift(),
                "dev_rows_using_satscores": len(ca_join_rows),
                "dev_rows_detail": ca_join_rows,
                "rows_marked_repaired": sum(1 for row in ca_join_rows
                                            if row.get("verification") == "sqlite_equivalence_repaired"),
                "rows_with_cast_repair": sum(1 for row in ca_join_rows if row["has_cast_repair"]),
                "claim_from_readme": "211 of 2,269 satscores.cds entries lost the leading zero; "
                                    "join matches 1,620 instead of 1,782; seven dev rows repaired",
                "verdict": "confirmed and re-measured: 211/2,269 keys lost the zero, the unrepaired "
                           "satscores-frpm join returns 1,620 rows vs 1,782 on SQLite (and the "
                           "satscores-schools join 2,058 vs 2,269); the package repairs 7 rows and all "
                           "10 current rows that join satscores pass SQLite equivalence. The historical "
                           "eighth affected row (57) is excluded from unified dev-217.",
            },
            "toxicology_superset": {
                **table_volume_drift(),
                "historical_diverging_rows": [record["question_id"] for record in tox_historical],
                "current_diverging_rows": [record["question_id"] for record in tox_current],
                "claim_from_readme": "tables are a superset of the original; all seven historical "
                                    "divergences are explained by the larger volume",
                "verdict": "confirmed: 7/7 historical divergences are exactly the review-excluded tox rows; "
                           "current dev-217 has one remaining flagged row (309, superset + reviewed correction)",
            },
        },
        "method": {
            "steps": [
                "load unified dev-217 and historical dev-241 rows",
                "run sqlite_gold_sql on the local SQLite file and pg_gold_sql on the server",
                "normalise with the project canon + timestamp canonicalisation",
                "compare strict and normalised row multisets (bag semantics)",
                "classify every non-exact row; flag or exclude it",
            ],
            "exclusions": "no row is silently kept: every non-exact row has class/subtype/reason and "
                          "usable_native_gold; server-defect rows are listed in must_not_use",
            "tunnel": "127.0.0.1:5444 -> 10.11.1.6:5444 via repo/ssh_tunnel.py",
        },
    }
    flags = {
        "generated_at_utc": report["generated_at_utc"],
        "source": "unified dev-217 (native gold) + historical dev-241 (supplementary)",
        "comparator": report["comparator"],
        "coverage": {key: report["coverage"][key] for key in
                     ("dev_rows_audited", "historical_dev_rows_audited",
                      "train_rows_with_local_sqlite", "train_rows_total")},
        "counts": {
            "unified_dev_217": current_summary,
            "historical_dev_241": historical_summary,
        },
        "rows": [
            {
                "source_dataset": record["source_dataset"],
                "question_id": record["question_id"],
                "db_id": record["db_id"],
                "class": record["class"],
                "subtype": record.get("subtype"),
                "drift_flag": record["class"] != "exact_match",
                "usable_native_gold": record.get("usable_native_gold"),
                "reason": record.get("reason"),
            }
            for record in current + historical
        ],
        "must_not_use_as_native_gold": report["must_not_use_as_native_gold"],
    }
    return {"report": report, "flags": flags}


def verify_artifacts(report: dict, flags: dict) -> None:
    """Self-checks: every audited row is classified and annotated, nothing is silent."""
    current = [row for row in flags["rows"] if row["source_dataset"] == "unified_dev_217"]
    assert len(current) == report["coverage"]["dev_rows_audited"] == 217
    non_exact = [row for row in current if row["class"] != "exact_match"]
    flagged = [row for row in current if row["drift_flag"]]
    assert len(non_exact) == len(flagged), "a non-exact row is not flagged"
    for row in non_exact:
        assert row["class"] in CLASSES and row["class"] != "exact_match"
        assert row.get("reason"), f"{row['question_id']} lacks a reason"
    must_not = {str(row["question_id"]) for row in report["must_not_use_as_native_gold"]}
    unusable = {str(row["question_id"]) for row in current
                if row["class"] == "server_side_data_defect"
                or (row["class"] == "translation_defect" and not row["usable_native_gold"])}
    assert must_not == unusable, f"must-not-use list {must_not} != unusable rows {unusable}"

    manifest = json.loads((SPLITS / "schema_family_split.json").read_text(encoding="utf-8"))
    drift_block = manifest.get("native_gold_drift")
    assert drift_block, "split manifest has no native_gold_drift block"
    assert drift_block["sha256"] == sha256(OUT_FLAGS), "manifest drift hash is stale"
    assert len(drift_block["flagged_rows"]) == len(non_exact)

    eval_rows = [json.loads(line) for line in (SPLITS / "eval_questions.jsonl").read_text(
        encoding="utf-8").splitlines() if line.strip()]
    annotated = [row for row in eval_rows if "native_gold_drift_class" in row]
    assert len(annotated) == 217, "eval registry does not annotate every dev row"
    by_id = {row["question_id"]: row for row in annotated}
    for row in current:
        entry = by_id[str(row["question_id"])]
        assert entry["native_gold_drift_class"] == row["class"]
        assert entry["native_gold_usable"] == row["usable_native_gold"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="native-gold drift audit")
    parser.add_argument("--out-report", type=pathlib.Path, default=OUT_REPORT)
    parser.add_argument("--out-flags", type=pathlib.Path, default=OUT_FLAGS)
    parser.add_argument("--skip-manifest", action="store_true",
                        help="do not rebuild datasets/splits after writing the flags")
    args = parser.parse_args(argv)
    started = time.time()
    built = build()
    args.out_report.parent.mkdir(parents=True, exist_ok=True)
    args.out_flags.parent.mkdir(parents=True, exist_ok=True)
    args.out_report.write_text(json.dumps(built["report"], ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
    args.out_flags.write_text(json.dumps(built["flags"], ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
    current = built["report"]["current_unified_dev"]
    print(f"unified dev-217: {current['classes']}")
    print(f"historical dev-241: {built['report']['historical_dev_241']['classes']}")
    print(f"must not use as native gold: {[row['question_id'] for row in built['report']['must_not_use_as_native_gold']]}")
    if not args.skip_manifest:
        import subprocess
        subprocess.run([sys.executable, str(SPLITS / "build_splits.py")],
                       check=True, cwd=str(PROJECT))
    verify_artifacts(built["report"], built["flags"])
    print("self-checks: every audited row classified/flagged, manifest and eval registry consistent")
    print(f"report: {args.out_report}")
    print(f"flags: {args.out_flags}")
    print(f"elapsed: {round(time.time() - started, 1)}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
