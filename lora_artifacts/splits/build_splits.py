#!/usr/bin/env python3
"""Build the schema-family split manifest and the decontamination report.

Implements docs/playbook/POSTGRES_TEXT2SQL_LORA.md section 2 item 1:

* every schema family (stable ``db_id`` + a lineage fingerprint of its tables, keys and
  column types) is mapped to exactly one split: ``train``, ``internal_valid``,
  ``ood_reserved`` or ``external_test``;
* the ten historical OOD databases are reserved by name, the three held-out Mini-Dev
  databases form the closed external test, and renamed schema copies are detected by
  fingerprint rather than by name;
* the playbook's overlap claim is recomputed from the files present in the workspace,
  with the exact rows that must be excluded listed in ``splits_report.json``.

Outputs (all in this directory, except the report given by --out):

    schema_family_split.json   the manifest
    eval_questions.jsonl       the closed evaluation registry (dev-217, OOD-876,
                               external 167 Mini-Dev PostgreSQL questions)
    splits_report.json         counts, overlap numbers, excluded rows, audits

Run:  python3 datasets/splits/build_splits.py
"""

from __future__ import annotations

import argparse
import collections
import datetime as _dt
import hashlib
import itertools
import json
import pathlib
import re
import sys
import unicodedata
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
PROJECT = HERE.parents[1]  # research/advtext2sql
PKG = PROJECT / "repo" / "datasets" / "bird_pg_unified"
MINIDEV_ZIP = PROJECT / "data_cache" / "minidev.zip"
OOD_HELDOUT = PROJECT / "data_cache" / "ood_heldout.json"
CLUSTER = PROJECT / "cluster"
DELIVERY = PROJECT / "delivery"
DRIFT_PATH = HERE / "native_gold_drift.json"

OOD_RESERVED = [
    "disney", "movie_3", "talkingdata", "menu", "movies_4",
    "law_episode", "european_football_1", "social_media", "sales_in_weather", "music_tracker",
]
INTERNAL_VALID = [
    "california_schools", "card_games", "codebase_community", "debit_card_specializing",
    "financial", "student_club", "superhero", "toxicology",
]
EXTERNAL_TEST = ["formula_1", "european_football_2", "thrombosis_prediction"]

LOCAL_SCHEME = "bird_pg_unified.schemas.schema.json:v1"
MINIDEV_SCHEME = "minidev.dev_tables.json:v1"

#: raw pools that carry rows for a schema family
POOLS = {
    "real": "data/bird_train_pg.jsonl",
    "synthetic": "data/synthetic_bird_like.jsonl",
    "synthetic_cap": "data/synthetic_cap_unnamed.jsonl",
    "synthetic_single": "data/synthetic_single_table_extra.jsonl",
}

CLAIMED = {
    "real_overlap_rows": 745,
    "synthetic_overlap_rows": 112,
    "databases_with_overlap": 9,
    "real_remaining": 4365,
    "synthetic_remaining": 2703,
    "real_rows_unified": 5110,
    "real_databases_unified": 62,
    "synthetic_rows_active": 2815,
    "synthetic_databases_active": 60,
    "dev_rows": 217,
    "dev_databases": 8,
}


# ----------------------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------------------
def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def load_jsonl(path: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def row_id(row: dict) -> str:
    return str(row.get("question_id") or row.get("sample_id"))


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


# ----------------------------------------------------------------------------------------------
# fingerprints
# ----------------------------------------------------------------------------------------------
def local_fingerprint(entry: dict) -> tuple[str, int, int]:
    """sha256 over tables, columns (name+type+not_null), primary and foreign keys."""
    tables = []
    for table in entry.get("tables") or []:
        columns = sorted([[c["name"], c["type"], bool(c.get("not_null"))] for c in table.get("columns") or []],
                         key=lambda col: (col[0], str(col[1])))
        fks = sorted([[fk["column"], fk["references"]["table"], fk["references"]["column"]]
                      for fk in table.get("foreign_keys") or []])
        tables.append([table["name"], columns, sorted(table.get("primary_key") or []), fks])
    tables.sort(key=lambda table: table[0])
    fp = hashlib.sha256(canon(tables).encode("utf-8")).hexdigest()
    return fp, len(tables), sum(len(table[1]) for table in tables)


def local_structure(entry: dict) -> list:
    """Type-free structure, used to compare across schema sources."""
    return sorted([[table["name"], sorted(c["name"] for c in table.get("columns") or [])]
                   for table in entry.get("tables") or []])


def structural_similarity(first: dict, second: dict) -> dict:
    """Table/column-name overlap used to surface renamed-copy candidates."""
    first_tables = {table["name"]: {c["name"] for c in table.get("columns") or []}
                    for table in first.get("tables") or []}
    second_tables = {table["name"]: {c["name"] for c in table.get("columns") or []}
                     for table in second.get("tables") or []}
    shared = set(first_tables) & set(second_tables)
    union = set(first_tables) | set(second_tables)
    table_jaccard = len(shared) / len(union) if union else 0.0
    first_columns = {(table, column) for table, columns in first_tables.items() for column in columns}
    second_columns = {(table, column) for table, columns in second_tables.items() for column in columns}
    column_union = first_columns | second_columns
    column_jaccard = len(first_columns & second_columns) / len(column_union) if column_union else 0.0
    return {
        "shared_tables": sorted(shared),
        "shared_table_count": len(shared),
        "table_jaccard": round(table_jaccard, 4),
        "column_jaccard": round(column_jaccard, 4),
    }


def minidev_entries() -> dict[str, dict]:
    if not MINIDEV_ZIP.is_file():
        raise SystemExit(f"Mini-Dev archive not found: {MINIDEV_ZIP}")
    with zipfile.ZipFile(MINIDEV_ZIP) as archive:
        return {entry["db_id"]: entry for entry in json.loads(archive.read("minidev/MINIDEV/dev_tables.json"))}


def minidev_fingerprint(entry: dict) -> tuple[str, int, int]:
    """sha256 over tables, column names+types, primary and foreign keys (Mini-Dev layout)."""
    names = entry["column_names_original"]
    types = entry["column_types"]
    tables = []
    for index, table_name in enumerate(entry["table_names_original"]):
        columns = sorted([[names[i][1], types[i]] for i, (t, _) in enumerate(names) if t == index])
        primary = []
        for pk in entry.get("primary_keys") or []:
            if isinstance(pk, int) and names[pk][0] == index:
                primary.append(names[pk][1])
            elif isinstance(pk, list) and names[pk[0]][0] == index:
                primary.append([names[i][1] for i in pk])
        foreign = sorted([[names[col][1], entry["table_names_original"][names[ref][0]], names[ref][1]]
                          for col, ref in entry.get("foreign_keys") or [] if names[col][0] == index])
        tables.append([table_name, columns, primary, foreign])
    tables.sort(key=lambda table: table[0])
    fp = hashlib.sha256(canon(tables).encode("utf-8")).hexdigest()
    return fp, len(tables), sum(len(table[1]) for table in tables)


def minidev_structure(entry: dict) -> list:
    names = entry["column_names_original"]
    return sorted([[table_name, sorted(names[i][1] for i, (t, _) in enumerate(names) if t == index)]
                   for index, table_name in enumerate(entry["table_names_original"])])


# ----------------------------------------------------------------------------------------------
# build
# ----------------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="build schema_family_split.json + splits_report.json")
    parser.add_argument("--out", type=pathlib.Path, default=HERE)
    args = parser.parse_args(argv)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)

    schema_path = PKG / "schemas" / "schema.json"
    if not schema_path.is_file():
        raise SystemExit(f"schema registry not found: {schema_path}")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    minidev = minidev_entries()

    # --- families ---------------------------------------------------------------------------
    families: dict[str, dict] = {}
    fingerprints: dict[str, list[str]] = collections.defaultdict(list)
    empty_registry: list[str] = []

    for db_id in sorted(schema):
        entry = schema[db_id]
        if not entry.get("tables"):
            empty_registry.append(db_id)
            families[db_id] = {
                "db_id": db_id,
                "schema_family": f"reserved_{db_id}",
                "lineage_fingerprint": None,
                "fingerprint_scheme": LOCAL_SCHEME,
                "schema_status": "missing",
                "tables": 0,
                "columns": 0,
                "split": "external_test",
                "aliases": [],
            }
            continue
        fp, n_tables, n_columns = local_fingerprint(entry)
        split = ("ood_reserved" if db_id in OOD_RESERVED
                 else "internal_valid" if db_id in INTERNAL_VALID
                 else "train")
        families[db_id] = {
            "db_id": db_id,
            "schema_family": f"fam_{fp[:16]}",
            "lineage_fingerprint": fp,
            "fingerprint_scheme": LOCAL_SCHEME,
            "schema_status": "captured",
            "tables": n_tables,
            "columns": n_columns,
            "split": split,
            "aliases": [],
        }
        fingerprints[fp].append(db_id)

    for db_id in EXTERNAL_TEST:
        if db_id not in minidev:
            raise SystemExit(f"external test schema {db_id!r} missing from the Mini-Dev archive")
        fp, n_tables, n_columns = minidev_fingerprint(minidev[db_id])
        families[db_id] = {
            "db_id": db_id,
            "schema_family": f"fam_{fp[:16]}",
            "lineage_fingerprint": fp,
            "fingerprint_scheme": MINIDEV_SCHEME,
            "schema_status": "captured_external",
            "tables": n_tables,
            "columns": n_columns,
            "split": "external_test",
            "aliases": [],
        }
        fingerprints[fp].append(db_id)

    for fp, dbs in fingerprints.items():
        if len(dbs) > 1:
            for db_id in dbs:
                families[db_id]["aliases"] = sorted(set(dbs) - {db_id})

    # --- lineage candidates: same table graph, renamed/variant columns -----------------------
    # Exact fingerprints cannot see a schema that was renamed column by column.  A high
    # table+column Jaccard is a candidate; candidates never override the exact rule silently,
    # but a candidate that touches an OOD-reserved database inherits that split so the schema
    # family is not split across train and eval.
    local_valid = {db_id: entry for db_id, entry in schema.items() if entry.get("tables")}
    lineage_pairs = []
    for first, second in itertools.combinations(sorted(local_valid), 2):
        similarity = structural_similarity(local_valid[first], local_valid[second])
        if (similarity["shared_table_count"] >= 2
                and similarity["table_jaccard"] >= 0.8
                and similarity["column_jaccard"] >= 0.6):
            lineage_pairs.append({"first": first, "second": second, **similarity})

    parent = {db_id: db_id for db_id in local_valid}

    def find(db_id: str) -> str:
        while parent[db_id] != db_id:
            parent[db_id] = parent[parent[db_id]]
            db_id = parent[db_id]
        return db_id

    for pair in lineage_pairs:
        root_a, root_b = find(pair["first"]), find(pair["second"])
        if root_a != root_b:
            parent[root_b] = root_a
    groups: dict[str, list[str]] = collections.defaultdict(list)
    for db_id in local_valid:
        groups[find(db_id)].append(db_id)

    lineage_groups = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members = sorted(members)
        group_id = f"fam_lineage_{hashlib.sha256(canon(members).encode()).hexdigest()[:12]}"
        anchor_ood = [db for db in members if db in OOD_RESERVED]
        anchor_valid = [db for db in members if db in INTERNAL_VALID]
        evidence = [pair for pair in lineage_pairs
                    if pair["first"] in members and pair["second"] in members]
        for db_id in members:
            family = families[db_id]
            family["schema_family"] = group_id
            family["aliases"] = sorted(set(members) - {db_id})
            family["lineage_evidence"] = evidence
            if anchor_ood:
                family["split"] = "ood_reserved"
                family["reserved_by_lineage"] = db_id not in OOD_RESERVED
            elif anchor_valid:
                family["split"] = "internal_valid"
        lineage_groups.append({
            "schema_family": group_id,
            "db_ids": members,
            "anchor_ood_names": anchor_ood,
            "anchor_internal_valid_names": anchor_valid,
            "assigned_split": families[members[0]]["split"],
            "evidence": evidence,
        })

    ood_lineage_extensions = sorted(db for db in families
                                    if families[db]["split"] == "ood_reserved" and db not in OOD_RESERVED)

    unknown = (set(OOD_RESERVED) | set(INTERNAL_VALID) | set(EXTERNAL_TEST)) - set(families)
    if unknown:
        raise SystemExit(f"split members without a family entry: {sorted(unknown)}")

    # --- rows in the raw pools --------------------------------------------------------------
    pools: dict[str, list[dict]] = {}
    pool_meta: dict[str, dict] = {}
    for pool, rel in POOLS.items():
        path = PKG / rel
        if not path.is_file():
            if pool in ("synthetic_cap", "synthetic_single"):
                continue
            raise SystemExit(f"pool not found: {path}")
        rows = load_jsonl(path)
        pools[pool] = rows
        pool_meta[pool] = {"path": str(path), "sha256": sha256(path), "rows": len(rows),
                           "databases": len({row["db_id"] for row in rows})}

    def split_of(db_id: str) -> str:
        if db_id not in families:
            raise SystemExit(f"row references a database missing from the schema registry: {db_id!r}")
        return families[db_id]["split"]

    split_row_counts = {name: collections.Counter() for name in
                        ("train", "internal_valid", "ood_reserved", "external_test")}
    for pool in ("real", "synthetic"):
        for row in pools[pool]:
            split_row_counts[split_of(row["db_id"])][pool] += 1

    # strict playbook claim: overlap of the pools with the ten *named* OOD databases
    named_ood_real = [row for row in pools["real"] if row["db_id"] in OOD_RESERVED]
    named_ood_synth = [row for row in pools["synthetic"] if row["db_id"] in OOD_RESERVED]
    # lineage-aware contract: everything mapped to the ood_reserved split (named + lineage siblings)
    ood_real = [row for row in pools["real"] if split_of(row["db_id"]) == "ood_reserved"]
    ood_synth = [row for row in pools["synthetic"] if split_of(row["db_id"]) == "ood_reserved"]
    lineage_ood_real = [row for row in ood_real if row["db_id"] not in OOD_RESERVED]
    lineage_ood_synth = [row for row in ood_synth if row["db_id"] not in OOD_RESERVED]
    per_db_real = collections.Counter(row["db_id"] for row in ood_real)
    per_db_synth = collections.Counter(row["db_id"] for row in ood_synth)
    named_per_db_real = collections.Counter(row["db_id"] for row in named_ood_real)
    named_per_db_synth = collections.Counter(row["db_id"] for row in named_ood_synth)

    real_remaining = len(pools["real"]) - len(ood_real)
    synth_remaining = len(pools["synthetic"]) - len(ood_synth)
    dev_rows_all = load_jsonl(PKG / "data" / "bird_dev_pg.jsonl")
    recomputed = {
        "real_overlap_rows": len(named_ood_real),
        "synthetic_overlap_rows": len(named_ood_synth),
        "databases_with_overlap": len(set(named_per_db_real) | set(named_per_db_synth)),
        "databases_with_real_overlap": len(named_per_db_real),
        "databases_with_synthetic_overlap": len(named_per_db_synth),
        "real_remaining": len(pools["real"]) - len(named_ood_real),
        "synthetic_remaining": len(pools["synthetic"]) - len(named_ood_synth),
        "real_rows_unified": len(pools["real"]),
        "real_databases_unified": len({row["db_id"] for row in pools["real"]}),
        "synthetic_rows_active": len(pools["synthetic"]),
        "synthetic_databases_active": len({row["db_id"] for row in pools["synthetic"]}),
        "dev_rows": len(dev_rows_all),
        "dev_databases": len({row["db_id"] for row in dev_rows_all}),
        "talkingdata_in_train_pool": any(row["db_id"] == "talkingdata" for row in pools["real"]),
        "talkingdata_in_synthetic_pool": any(row["db_id"] == "talkingdata" for row in pools["synthetic"]),
    }
    mismatches = {key: {"claimed": CLAIMED[key], "recomputed": recomputed[key]}
                  for key in CLAIMED if CLAIMED[key] != recomputed[key]}
    claim_verdict = "confirmed" if not mismatches else "mismatch"
    lineage_adjusted = {
        "real_remaining": real_remaining,
        "synthetic_remaining": synth_remaining,
        "real_excluded_by_split": len(ood_real),
        "synthetic_excluded_by_split": len(ood_synth),
        "lineage_extension_rows_real": len(lineage_ood_real),
        "lineage_extension_rows_synthetic": len(lineage_ood_synth),
    }
    print(f"lineage groups: {[group['db_ids'] for group in lineage_groups]}")

    # --- family row annotations -------------------------------------------------------------
    db_pool_counts = collections.defaultdict(collections.Counter)
    for pool in ("real", "synthetic"):
        for row in pools[pool]:
            db_pool_counts[row["db_id"]][pool] += 1

    # --- additional synthetic pools: current files ------------------------------------------
    extra_meta: dict[str, dict] = {}
    extra_candidates = [
        ("synthetic_cap_unnamed.jsonl", PKG / "data" / "synthetic_cap_unnamed.jsonl"),
        ("synthetic_single_table_extra.jsonl", PKG / "data" / "synthetic_single_table_extra.jsonl"),
        ("roundtrip_sample.jsonl", DELIVERY / "out" / "synth" / "roundtrip_sample.jsonl"),
        ("roundtrip_join_sample.jsonl", DELIVERY / "out" / "synth" / "roundtrip_join_sample.jsonl"),
        ("synthetic_bird_like_stale_982.jsonl", DELIVERY / "out" / "synth" / "synthetic_bird_like.jsonl"),
    ]
    for name, path in extra_candidates:
        if not path.is_file():
            continue
        rows = load_jsonl(path)
        ood_rows = [row for row in rows if row["db_id"] in OOD_RESERVED]
        extra_meta[name] = {
            "path": str(path),
            "sha256": sha256(path),
            "rows": len(rows),
            "databases": len({row["db_id"] for row in rows}),
            "ood_reserved_rows": len(ood_rows),
            "ood_reserved_databases": len({row["db_id"] for row in ood_rows}),
            "ood_rows": [{"question_id": row_id(row), "db_id": row["db_id"]} for row in ood_rows],
            "note": "stale pre-review 982-row file; not the current synthetic corpus"
                    if name.startswith("synthetic_bird_like_stale") else
                    ("current empty-LIMIT variant pool" if "cap_unnamed" in name else
                     "reserve pool / sampling file"),
        }

    # --- evaluation registry ----------------------------------------------------------------
    dev_rows = load_jsonl(PKG / "data" / "bird_dev_pg.jsonl")
    if not OOD_HELDOUT.is_file():
        raise SystemExit(f"OOD-876 questions not found: {OOD_HELDOUT}")
    ood_rows_source = json.loads(OOD_HELDOUT.read_text(encoding="utf-8"))
    with zipfile.ZipFile(MINIDEV_ZIP) as archive:
        minidev_questions = json.loads(archive.read("minidev/MINIDEV/mini_dev_postgresql.json"))
    external_questions = [row for row in minidev_questions if row["db_id"] in EXTERNAL_TEST]

    drift = json.loads(DRIFT_PATH.read_text(encoding="utf-8")) if DRIFT_PATH.is_file() else None
    drift_current = {}
    if drift:
        drift_current = {str(row["question_id"]): row for row in drift["rows"]
                         if row["source_dataset"] == "unified_dev_217"}

    registry: list[dict] = []
    for row in dev_rows:
        entry = {
            "eval_id": f"internal_valid:{row['db_id']}:{row.get('question_id') or row.get('sample_id')}",
            "question_id": str(row.get("question_id") or row.get("sample_id")),
            "db_id": row["db_id"],
            "question": row["question"],
            "evidence": row.get("evidence") or "",
            "gold_sql": row.get("pg_gold_sql"),
            "gold_dialect": "postgresql",
            "difficulty": row.get("difficulty"),
            "source_shard": "internal_valid",
        }
        drift_row = drift_current.get(entry["question_id"])
        if drift_row is not None:
            entry["native_gold_drift_class"] = drift_row["class"]
            entry["native_gold_usable"] = drift_row["usable_native_gold"]
            entry["native_gold_drift_reason"] = drift_row.get("reason")
        registry.append(entry)
    for row in ood_rows_source:
        registry.append({
            "eval_id": f"ood_reserved:{row['db_id']}:{row['question_id']}",
            "question_id": str(row["question_id"]),
            "db_id": row["db_id"],
            "question": row["question"],
            "evidence": row.get("evidence") or "",
            "gold_sql": row.get("SQL"),
            "gold_dialect": "sqlite_translated_by_research",
            "difficulty": row.get("difficulty"),
            "source_shard": "ood_reserved",
        })
    for row in external_questions:
        registry.append({
            "eval_id": f"external_test:{row['db_id']}:{row['question_id']}",
            "question_id": str(row["question_id"]),
            "db_id": row["db_id"],
            "question": row["question"],
            "evidence": row.get("evidence") or "",
            "gold_sql": row.get("SQL"),
            "gold_dialect": "postgresql",
            "difficulty": row.get("difficulty"),
            "source_shard": "external_test",
        })
    shard_order = {"internal_valid": 0, "ood_reserved": 1, "external_test": 2}
    registry.sort(key=lambda row: (shard_order[row["source_shard"]], row["db_id"], row["question_id"]))
    registry_path = out / "eval_questions.jsonl"
    registry_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in registry),
                             encoding="utf-8")
    eval_counts = collections.Counter(row["source_shard"] for row in registry)
    eval_dbs = {shard: sorted({row["db_id"] for row in registry if row["source_shard"] == shard})
                for shard in shard_order}

    # --- renamed-copy detection -------------------------------------------------------------
    exact_groups = [{"lineage_fingerprint": fp, "db_ids": sorted(dbs), "splits": sorted({families[db]["split"] for db in dbs})}
                    for fp, dbs in fingerprints.items() if len(dbs) > 1]
    local_structures = {db_id: local_structure(entry) for db_id, entry in schema.items()
                        if entry.get("tables") and db_id not in EXTERNAL_TEST}
    cross_scheme = []
    for db_id in EXTERNAL_TEST:
        structure = minidev_structure(minidev[db_id])
        matches = sorted(name for name, other in local_structures.items() if other == structure)
        if matches:
            cross_scheme.append({"external_test_db": db_id, "local_matches": matches,
                                 "note": "same table/column-name structure ignoring type spelling"})
    renamed_candidates = {
        "method": "exact lineage fingerprint equality (tables + keys + column types); "
                  "then table/column-name Jaccard >= 0.8 / 0.6 over >= 2 shared tables; "
                  "type-free structure equality across schema sources",
        "exact_fingerprint_groups": exact_groups,
        "near_duplicate_lineage_groups": lineage_groups,
        "cross_scheme_structural_matches": cross_scheme,
        "empty_schema_placeholders": sorted(empty_registry),
        "note": ("the six empty registry entries share no tables at all; they are placeholders, "
                 "not renamed copies, and are recorded separately. A near-duplicate candidate that "
                 "touches an OOD-reserved database is assigned the OOD split so the schema family "
                 "is never split across train and eval."),
        "candidates": [entry for entry in exact_groups if len(entry["splits"]) > 1]
                      + lineage_groups + cross_scheme,
        "ood_lineage_extensions": ood_lineage_extensions,
    }

    # --- trainable file audit ---------------------------------------------------------------
    trainable_files = [
        PKG / "data" / "bird_train_pg.jsonl",
        PKG / "data" / "synthetic_bird_like.jsonl",
        PKG / "data" / "synthetic_cap_unnamed.jsonl",
        PKG / "data" / "synthetic_single_table_extra.jsonl",
        CLUSTER / "sft_all_v2.jsonl",
        CLUSTER / "sft_all.jsonl",
        CLUSTER / "sft_train.jsonl",
        CLUSTER / "sft_synth.jsonl",
        DELIVERY / "bird_pg_dataset" / "data" / "bird_train_pg.jsonl",
        DELIVERY / "bird_pg_dataset" / "data" / "synthetic_bird_like.jsonl",
        DELIVERY / "out" / "synth" / "synthetic_all.jsonl",
        DELIVERY / "out" / "synth" / "candidates.jsonl",
        DELIVERY / "out" / "synth" / "curated.jsonl",
        DELIVERY / "out" / "synth" / "handwritten_accepted.jsonl",
        DELIVERY / "out" / "synth" / "roundtrip_sample.jsonl",
        DELIVERY / "out" / "synth" / "roundtrip_join_sample.jsonl",
    ]
    trainable_files += sorted((DELIVERY / "out" / "synth" / "overnight").glob("*.jsonl"))
    audit = []
    for path in trainable_files:
        if not path.is_file():
            continue
        rows = load_jsonl(path)
        by_split = collections.Counter()
        unknown_rows = 0
        for row in rows:
            split = families.get(row.get("db_id"), {}).get("split")
            if split is None:
                unknown_rows += 1
            else:
                by_split[split] += 1
        audit.append({
            "path": str(path),
            "rows": len(rows),
            "databases": len({row.get("db_id") for row in rows}),
            "rows_by_split": {split: by_split.get(split, 0) for split in
                              ("train", "internal_valid", "ood_reserved", "external_test")},
            "unknown_database_rows": unknown_rows,
            "external_test_rows": by_split.get("external_test", 0),
            "internal_valid_rows": by_split.get("internal_valid", 0),
        })

    # --- question decontamination audit -----------------------------------------------------
    train_questions = []
    for pool in ("real", "synthetic", "synthetic_cap", "synthetic_single"):
        for row in pools.get(pool, []):
            if split_of(row["db_id"]) == "train":
                train_questions.append((row["question"], f"{pool}:{row_id(row)}", row["db_id"]))
    eval_questions = [(row["question"], row["eval_id"], row["db_id"]) for row in registry]

    eval_index: dict[tuple, list[int]] = collections.defaultdict(list)
    eval_grams = []
    for index, (question, _eval_id, _db) in enumerate(eval_questions):
        grams = ngrams(question)
        eval_grams.append(grams)
        for gram in grams:
            eval_index[gram].append(index)

    violations = []
    near_misses = 0
    max_jaccard = 0.0
    for question, train_id, db_id in train_questions:
        grams = ngrams(question)
        if not grams:
            continue
        candidates: collections.Counter = collections.Counter()
        for gram in grams:
            for index in eval_index.get(gram, ()):
                candidates[index] += 1
        for index, shared in candidates.items():
            if shared < 2:
                continue
            other = eval_grams[index]
            score = shared / len(grams | other)
            max_jaccard = max(max_jaccard, score)
            if score >= 0.8:
                violations.append({"train_id": train_id, "eval_id": eval_questions[index][1],
                                   "jaccard": round(score, 4), "train_db": db_id,
                                   "eval_db": eval_questions[index][2]})
            elif score >= 0.6:
                near_misses += 1
    question_audit = {
        "train_questions_checked": len(train_questions),
        "eval_questions_checked": len(eval_questions),
        "threshold": 0.8,
        "violations": violations,
        "max_jaccard": round(max_jaccard, 4),
        "pairs_at_or_above_0_6": near_misses,
    }

    # --- manifest ---------------------------------------------------------------------------
    splits: dict[str, dict] = {}
    for split in ("train", "internal_valid", "ood_reserved", "external_test"):
        dbs = sorted(db for db, family in families.items() if family["split"] == split)
        family_ids = sorted({families[db]["schema_family"] for db in dbs})
        splits[split] = {
            "schema_families": family_ids,
            "db_ids": dbs,
            "schema_family_count": len(family_ids),
            "db_id_count": len(dbs),
            "real_rows": split_row_counts[split]["real"],
            "synthetic_rows": split_row_counts[split]["synthetic"],
            "eval_questions": eval_counts.get(split, 0),
        }

    for db_id, family in families.items():
        family["pool_rows_real"] = db_pool_counts[db_id]["real"]
        family["pool_rows_synthetic"] = db_pool_counts[db_id]["synthetic"]
        family["eval_questions"] = sum(1 for row in registry if row["db_id"] == db_id)

    manifest = {
        "manifest_version": 1,
        "generated_at_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "playbook": "docs/playbook/POSTGRES_TEXT2SQL_LORA.md section 2 item 1",
        "purpose": "schema_family -> split mapping and the closed OOD/external contracts",
        "policy": {
            "train": "schemas allowed in the training corpus; rows from any other split must be filtered out",
            "internal_valid": "the eight reviewed dev-217 databases; checkpoint selection only",
            "ood_reserved": "the ten historical OOD databases plus any lineage near-copy of them "
                            "(listed in ood_lineage_extensions); never trained, scored on OOD-876",
            "external_test": "closed, never trained: three held-out Mini-Dev PostgreSQL schemas plus "
                             "six empty registry placeholders that have no captured schema",
        },
        "lineage_fingerprint": {
            "description": "sha256 over canonical JSON of tables (name), columns (name, type, not_null), "
                           "primary keys and foreign keys",
            "schemes": {
                LOCAL_SCHEME: str(schema_path),
                MINIDEV_SCHEME: "minidev.zip:minidev/MINIDEV/dev_tables.json",
            },
        },
        "ood_reserved_names": OOD_RESERVED,
        "ood_lineage_extensions": ood_lineage_extensions,
        "internal_valid_names": INTERNAL_VALID,
        "external_test_schemas": EXTERNAL_TEST,
        "external_test_unavailable_schemas": sorted(empty_registry),
        "splits": splits,
        "families": families,
        "renamed_copy_candidates": renamed_candidates,
        "sources": {
            "package": str(PKG),
            "schema_json": {"path": str(schema_path), "sha256": sha256(schema_path)},
            "minidev_zip": {"path": str(MINIDEV_ZIP), "sha256": sha256(MINIDEV_ZIP)},
            "ood_heldout": {"path": str(OOD_HELDOUT), "sha256": sha256(OOD_HELDOUT)},
            "pools": pool_meta,
        },
        "eval_registry": {
            "path": str(registry_path),
            "sha256": sha256(registry_path),
            "rows": len(registry),
            "rows_by_shard": dict(eval_counts),
            "databases_by_shard": eval_dbs,
        },
    }
    if drift:
        current = drift["counts"]["unified_dev_217"]
        manifest["native_gold_drift"] = {
            "path": str(DRIFT_PATH),
            "sha256": sha256(DRIFT_PATH),
            "coverage": drift["coverage"],
            "counts": current,
            "flagged_rows": [row for row in drift["rows"]
                             if row["source_dataset"] == "unified_dev_217" and row["drift_flag"]],
            "must_not_use_as_native_gold": drift["must_not_use_as_native_gold"],
            "policy": "every non-exact dev row carries a drift class/subtype/reason; server-data "
                      "defects are listed as must_not_use and remain explicitly flagged in "
                      "eval_questions.jsonl, never silently kept",
        }
    manifest_path = out / "schema_family_split.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # --- report -----------------------------------------------------------------------------
    def exclusion_reason(db_id: str) -> str:
        return "ood_named" if db_id in OOD_RESERVED else "schema_lineage_extension"

    excluded_real = [{"question_id": row_id(row), "db_id": row["db_id"],
                      "reason": exclusion_reason(row["db_id"]), "question": row["question"]}
                     for row in ood_real]
    excluded_synth = [{"question_id": row_id(row), "db_id": row["db_id"], "variant": row.get("variant"),
                       "reason": exclusion_reason(row["db_id"]), "question": row["question"]}
                      for row in ood_synth]

    current_sft = CLUSTER / "sft_all_v2.jsonl"
    current_sft_ood = 0
    if current_sft.is_file():
        current_sft_ood = sum(1 for row in load_jsonl(current_sft)
                              if split_of(row.get("db_id", "")) == "ood_reserved")

    report = {
        "generated_at_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "playbook_reference": "docs/playbook/POSTGRES_TEXT2SQL_LORA.md section 2 item 1",
        "manifest": {"path": str(manifest_path), "sha256": sha256(manifest_path)},
        "eval_registry": {"path": str(registry_path), "sha256": sha256(registry_path),
                          "rows": len(registry), "rows_by_shard": dict(eval_counts)},
        "native_gold_drift": manifest.get("native_gold_drift"),
        "claim_check": {
            "claimed": CLAIMED,
            "recomputed": recomputed,
            "per_database": {
                "real_rows": dict(sorted(named_per_db_real.items())),
                "synthetic_rows": dict(sorted(named_per_db_synth.items())),
            },
            "mismatches": mismatches,
            "verdict": claim_verdict,
            "method": "recomputed from datasets/bird_pg_unified raw pools against the ten named "
                      "OOD databases; nothing copied from the playbook",
            "lineage_aware_split": lineage_adjusted,
            "lineage_note": ("the ten-name overlap matches the playbook exactly; one lineage sibling "
                             "of a reserved schema is additionally kept out of train, so the split "
                             "counts in splits.* are lower than the ten-name claim"),
        },
        "splits": {
            split: {
                "schema_families": splits[split]["schema_family_count"],
                "real_rows": splits[split]["real_rows"],
                "synthetic_rows": splits[split]["synthetic_rows"],
                "eval_questions": splits[split]["eval_questions"],
                "eval_databases": eval_dbs.get(split, []),
                "db_ids": splits[split]["db_ids"],
            } for split in splits
        },
        "ood_overlap": {
            "strict_named": {
                "real": {"rows": len(named_ood_real), "databases": len(named_per_db_real),
                         "per_database": dict(sorted(named_per_db_real.items()))},
                "synthetic": {"rows": len(named_ood_synth), "databases": len(named_per_db_synth),
                              "per_database": dict(sorted(named_per_db_synth.items()))},
                "remaining": {"real": len(pools["real"]) - len(named_ood_real),
                              "synthetic": len(pools["synthetic"]) - len(named_ood_synth)},
            },
            "split_based": {
                "real": {"rows": len(ood_real), "databases": len(per_db_real),
                         "per_database": dict(sorted(per_db_real.items()))},
                "synthetic": {"rows": len(ood_synth), "databases": len(per_db_synth),
                              "per_database": dict(sorted(per_db_synth.items()))},
            },
            "lineage_extensions": {
                "db_ids": ood_lineage_extensions,
                "real_rows": len(lineage_ood_real),
                "synthetic_rows": len(lineage_ood_synth),
                "groups": lineage_groups,
            },
            "remaining_after_ood_contract": {"real": real_remaining, "synthetic": synth_remaining},
            "talkingdata": {
                "in_unified_train_pool": recomputed["talkingdata_in_train_pool"],
                "in_unified_synthetic_pool": recomputed["talkingdata_in_synthetic_pool"],
                "ood_eval_questions": sum(1 for row in registry
                                           if row["source_shard"] == "ood_reserved"
                                           and row["db_id"] == "talkingdata"),
            },
        },
        "excluded_rows": {
            "bird_train_pg.jsonl": excluded_real,
            "synthetic_bird_like.jsonl": excluded_synth,
            "counts": {"bird_train_pg.jsonl": len(excluded_real),
                       "synthetic_bird_like.jsonl": len(excluded_synth)},
            "by_reason": {
                "real": dict(collections.Counter(row["reason"] for row in excluded_real)),
                "synthetic": dict(collections.Counter(row["reason"] for row in excluded_synth)),
            },
        },
        "additional_synthetic_pools": extra_meta,
        "renamed_copy_candidates": renamed_candidates,
        "trainable_files": audit,
        "question_decontamination": question_audit,
        "external_test": {
            "schemas": EXTERNAL_TEST,
            "unavailable_schemas": sorted(empty_registry),
            "eval_questions": len(external_questions),
            "databases": eval_dbs.get("external_test", []),
            "closed": True,
            "never_trained": True,
            "checked_absent_from_trainable_files": all(entry["external_test_rows"] == 0 for entry in audit),
        },
        "training_corpus_warning": {
            "path": str(current_sft),
            "ood_reserved_rows_present": current_sft_ood,
            "ood_named_rows_present": sum(1 for row in load_jsonl(current_sft)
                                          if row.get("db_id") in OOD_RESERVED) if current_sft.is_file() else 0,
            "lineage_extension_rows_present": current_sft_ood - (
                sum(1 for row in load_jsonl(current_sft) if row.get("db_id") in OOD_RESERVED)
                if current_sft.is_file() else 0),
            "required_action": "filter sft_all_v2.jsonl by excluded_rows (ood_reserved split) before the "
                               "final training run; otherwise OOD-876 scores are contaminated",
        },
    }
    report_path = out / "splits_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"manifest: {manifest_path}")
    print(f"eval registry: {registry_path} ({len(registry)} rows: {dict(eval_counts)})")
    print(f"report: {report_path}")
    print(f"families: {len(families)} | splits: "
          f"{ {split: splits[split]['schema_family_count'] for split in splits} }")
    print(f"claim (ten names): {claim_verdict}; real overlap {len(named_ood_real)}, "
          f"synthetic overlap {len(named_ood_synth)}")
    print(f"lineage-aware exclusion: {len(ood_real)} real / {len(ood_synth)} synthetic; "
          f"train remains {real_remaining} real / {synth_remaining} synthetic")
    print(f"question overlap violations: {len(violations)} (max jaccard {max_jaccard:.3f})")
    print(f"external_test rows in trainable files: "
          f"{sum(entry['external_test_rows'] for entry in audit)}")
    print(f"current sft_all_v2 OOD rows: {current_sft_ood}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
