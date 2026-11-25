#!/usr/bin/env python3
"""Native-PostgreSQL SFT loader for the unified BIRD package.

Reads ``datasets/bird_pg_unified`` exactly as shipped and keeps ``pg_gold_sql`` as the
training target — there is no SQLite→PostgreSQL re-cleaning here. The split contract from
``datasets/splits`` is applied *before* anything is written:

* rows listed in ``splits_report.json -> excluded_rows`` are dropped (reasons
  ``ood_named`` and ``schema_lineage_extension``);
* every row of a non-``train`` schema family is rejected, so the train output contains
  zero rows on ``ood_reserved`` / ``internal_valid`` / ``external_test`` schemas;
* the kept counts are asserted against the t9 contract: 4,216 real + 2,691 synthetic.

Learner-visible fields are schema, question and evidence. Every service field (ids,
split, license, revision, provenance, verification, hashes, skill) is written to a
separate sidecar file and never composed into the prompt; ``renderer.render`` only reads
``db_id``/``question``/``evidence``/``pg_gold_sql``.

Outputs (default ``datasets/native_pg``):

    native_pg_train.jsonl      training records incl. rendered prompt/completion
    native_pg_sidecar.jsonl    service metadata, one line per training record
    loader_report.json         counts, hashes, assertions (consumed by the batch report)

Usage:
    python3 datasets/native_pg/loader.py
    python3 datasets/native_pg/loader.py --out datasets/native_pg
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
PROJECT = HERE.parents[1]  # research/advtext2sql
PACKAGE = PROJECT / "repo" / "datasets" / "bird_pg_unified"
SPLITS = PROJECT / "datasets" / "splits"
MANIFEST_PATH = SPLITS / "schema_family_split.json"
REPORT_PATH = SPLITS / "splits_report.json"
PACKAGE_MANIFEST = PACKAGE / "MANIFEST.json"
REAL_PATH = PACKAGE / "data" / "bird_train_pg.jsonl"
SYNTH_PATH = PACKAGE / "data" / "synthetic_bird_like.jsonl"

OOD_NAMED = [
    "disney", "movie_3", "talkingdata", "menu", "movies_4",
    "law_episode", "european_football_1", "social_media", "sales_in_weather", "music_tracker",
]
EXPECTED_ROWS_IN = 7925
EXPECTED_TRAIN_REAL = 4216
EXPECTED_TRAIN_SYNTHETIC = 2691
EXPECTED_FILTERED = 1018  # 894 real + 124 synthetic from splits_report.json

LICENSES = {
    "real": "CC-BY-SA-4.0 (BIRD train, birdsql/bird23-train-filtered)",
    "synthetic": "research-only synthetic derived from BIRD data (CC-BY-SA-4.0 source data)",
}


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_jsonl(path: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_split_contract() -> tuple[dict, dict]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    if report["manifest"]["sha256"] != sha256(MANIFEST_PATH):
        raise SystemExit("splits_report.json was built against a different schema_family_split.json")
    return manifest, report


def sample_id(row: dict, pool: str) -> str:
    base = str(row.get("sample_id") or row.get("question_id"))
    variant = row.get("variant")
    return f"{base}#{variant}" if variant is not None else base


def classify_skill(sql: str) -> str:
    """Deterministic SQL-skill label (the corpus has no WITH/window rows yet)."""
    text = re.sub(r"\s+", " ", sql).strip()
    upper = text.upper()
    if upper.startswith("WITH "):
        return "cte"
    if " OVER (" in upper or " OVER(" in upper:
        return "window"
    if " JOIN " in upper:
        return "join"
    if "GROUP BY" in upper or re.search(r"\b(COUNT|SUM|AVG|MIN|MAX)\s*\(", upper):
        return "aggregation"
    if " WHERE " in upper:
        return "filter"
    return "projection"


def verification_of(row: dict, pool: str) -> dict:
    if pool == "real":
        validation = row.get("pg_validation") or {}
        return {
            "status": "verified_execution",
            "executions": validation.get("executions"),
            "result_sha256": validation.get("result_sha256"),
            "empty_result": bool(row.get("empty_result")),
        }
    provenance = (row.get("provenance") or {}).get("checks") or ""
    return {
        "status": "verified_result_sha256",
        "result_sha256": row.get("result_sha256"),
        "result_rows": row.get("rows"),
        "answer_shape": row.get("answer_shape"),
        "checks": provenance,
    }


def build_records() -> dict:
    """Load, filter and render the training corpus; return everything + audit numbers."""
    if str(PROJECT) not in sys.path:
        sys.path.insert(0, str(PROJECT))
    from datasets.native_pg.renderer import render

    manifest, report = load_split_contract()
    families = manifest["families"]
    split_of = {db: family["split"] for db, family in families.items()}

    package_manifest = json.loads(PACKAGE_MANIFEST.read_text(encoding="utf-8"))
    source_revision = f"bird_pg_unified@{package_manifest.get('created', 'unknown')}"

    excluded = report["excluded_rows"]
    excluded_real = {entry["question_id"] for entry in excluded["bird_train_pg.jsonl"]}
    excluded_synth = {entry["question_id"] for entry in excluded["synthetic_bird_like.jsonl"]}
    reason_of = {entry["question_id"]: entry["reason"]
                 for entry in excluded["bird_train_pg.jsonl"] + excluded["synthetic_bird_like.jsonl"]}

    pools = [("real", REAL_PATH), ("synthetic", SYNTH_PATH)]
    rows_in = 0
    counts_by_split = {split: collections.Counter() for split in
                       ("train", "internal_valid", "ood_reserved", "external_test")}
    filtered_by_split = collections.Counter()
    filtered_by_reason = collections.Counter()
    filtered_ids = {"real": set(), "synthetic": set()}
    records: list[dict] = []
    sidecar: list[dict] = []
    seen_ids: set[str] = set()

    for pool, path in pools:
        for row in load_jsonl(path):
            rows_in += 1
            sid = sample_id(row, pool)
            db = row["db_id"]
            if db not in split_of:
                raise SystemExit(f"{path.name}:{sid}: db_id {db!r} is missing from the split manifest")
            split = split_of[db]
            counts_by_split[split][pool] += 1

            base_id = str(row.get("sample_id") or row.get("question_id"))
            is_excluded = base_id in (excluded_real if pool == "real" else excluded_synth)
            if is_excluded:
                filtered_by_split[split] += 1
                filtered_by_reason[reason_of[base_id]] += 1
                filtered_ids[pool].add(base_id)
                continue
            if split != "train":
                raise SystemExit(
                    f"{path.name}:{sid}: row on a {split!r} schema was not filtered "
                    "(split contract is inconsistent with excluded_rows)")
            if sid in seen_ids:
                raise SystemExit(f"duplicate sample_id {sid!r}")
            seen_ids.add(sid)

            record = {
                "sample_id": sid,
                "family_id": families[db]["schema_family"],
                "schema_family": families[db]["schema_family"],
                "db_id": db,
                "source_revision": source_revision,
                "license": LICENSES[pool],
                "question": row["question"],
                "evidence": row.get("evidence") or "",
                "pg_gold_sql": row["pg_gold_sql"],
                "skill": classify_skill(row["pg_gold_sql"]),
                "verification": verification_of(row, pool),
                "split": split,
            }
            rendered = render(record, mode="train")
            if rendered["completion"] != record["pg_gold_sql"]:
                raise SystemExit(f"{sid}: native gold SQL was modified during rendering")
            record["prompt"] = rendered["prompt"]
            record["completion"] = rendered["completion"]
            record["target_source"] = "native_pg_gold" if pool == "real" else "native_pg_synthetic"
            records.append(record)
            sidecar.append({
                "sample_id": sid,
                "family_id": record["family_id"],
                "schema_family": record["schema_family"],
                "db_id": db,
                "split": split,
                "source_pool": pool,
                "source_file": str(path),
                "source_row_sha256": sha256_text(json.dumps(row, sort_keys=True, ensure_ascii=False)),
                "prompt_sha256": sha256_text(rendered["prompt"]),
                "completion_sha256": sha256_text(record["pg_gold_sql"]),
                "source_revision": source_revision,
                "license": record["license"],
                "skill": record["skill"],
                "synthetic_shape": row.get("shape") if pool == "synthetic" else None,
                "verification": record["verification"],
            })

    # --- contract assertions (t9 numbers, no silent rounding) --------------------------------
    missing_excluded = (excluded_real - filtered_ids["real"]) | (excluded_synth - filtered_ids["synthetic"])
    if missing_excluded:
        raise SystemExit(f"excluded_rows ids not found in the pools: {sorted(missing_excluded)[:5]}")
    for entry in excluded["bird_train_pg.jsonl"] + excluded["synthetic_bird_like.jsonl"]:
        db = entry["db_id"]
        if entry["reason"] == "ood_named" and db not in OOD_NAMED:
            raise SystemExit(f"excluded row {entry['question_id']!r} says ood_named but {db!r} is not reserved")
        if entry["reason"] == "schema_lineage_extension" and db in OOD_NAMED:
            raise SystemExit(f"excluded row {entry['question_id']!r} marks a named OOD db as lineage extension")
        if split_of.get(db) != "ood_reserved":
            raise SystemExit(f"excluded row {entry['question_id']!r} is on a {split_of.get(db)!r} schema")
    kept_real = sum(1 for record in records if record["target_source"] == "native_pg_gold")
    kept_synthetic = len(records) - kept_real
    problems = []
    if rows_in != EXPECTED_ROWS_IN:
        problems.append(f"rows_in {rows_in} != {EXPECTED_ROWS_IN}")
    if len(records) != EXPECTED_TRAIN_REAL + EXPECTED_TRAIN_SYNTHETIC:
        problems.append(f"kept {len(records)} != {EXPECTED_TRAIN_REAL + EXPECTED_TRAIN_SYNTHETIC}")
    if kept_real != EXPECTED_TRAIN_REAL:
        problems.append(f"real train rows {kept_real} != {EXPECTED_TRAIN_REAL}")
    if kept_synthetic != EXPECTED_TRAIN_SYNTHETIC:
        problems.append(f"synthetic train rows {kept_synthetic} != {EXPECTED_TRAIN_SYNTHETIC}")
    if sum(filtered_by_split.values()) != EXPECTED_FILTERED:
        problems.append(f"filtered {sum(filtered_by_split.values())} != {EXPECTED_FILTERED}")
    if filtered_by_split.get("ood_reserved", 0) != EXPECTED_FILTERED:
        problems.append(f"non-OOD filtered rows: {dict(filtered_by_split)}")
    if any(record["split"] != "train" for record in records):
        problems.append("train output contains a non-train split row")
    service_leaks = 0
    for record in records:
        prompt = record["prompt"]
        for field in ("sample_id", "family_id", "schema_family", "source_revision", "license"):
            if str(record[field]) and str(record[field]) in prompt:
                service_leaks += 1
                break
    if service_leaks:
        problems.append(f"{service_leaks} prompts contain a service-field value")
    if problems:
        raise SystemExit("split contract violated: " + "; ".join(problems))

    return {
        "records": records,
        "sidecar": sidecar,
        "stats": {
            "rows_in": rows_in,
            "rows_kept": len(records),
            "rows_filtered": sum(filtered_by_split.values()),
            "rows_filtered_by_split": {split: filtered_by_split.get(split, 0) for split in
                                       ("train", "internal_valid", "ood_reserved", "external_test")},
            "rows_filtered_by_reason": dict(filtered_by_reason),
            "rows_kept_by_split": {"train": len(records), "internal_valid": 0,
                                   "ood_reserved": 0, "external_test": 0},
            "counts_by_split_and_source": {
                split: {"real": counts_by_split[split]["real"],
                        "synthetic": counts_by_split[split]["synthetic"]}
                for split in ("train", "internal_valid", "ood_reserved", "external_test")},
            "train_rows_real": kept_real,
            "train_rows_synthetic": kept_synthetic,
            "skills": dict(sorted(collections.Counter(record["skill"] for record in records).items())),
            "split_manifest": {
                "path": str(MANIFEST_PATH),
                "sha256": sha256(MANIFEST_PATH),
                "splits_report_sha256": sha256(REPORT_PATH),
            },
            "package_manifest": {
                "path": str(PACKAGE_MANIFEST),
                "sha256": sha256(PACKAGE_MANIFEST),
                "created": package_manifest.get("created"),
            },
            "source_files": {pool: {"path": str(path), "sha256": sha256(path)}
                             for pool, path in pools},
            "zero_non_train_rows": True,
            "native_gold_untouched": True,
            "service_fields_in_prompt": service_leaks,
            "prompt_composed_from": ["schema", "question", "evidence"],
        },
    }


def write(built: dict, out: pathlib.Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    train_path = out / "native_pg_train.jsonl"
    sidecar_path = out / "native_pg_sidecar.jsonl"
    train_path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n"
                                 for record in built["records"]), encoding="utf-8")
    sidecar_path.write_text("".join(json.dumps(entry, ensure_ascii=False) + "\n"
                                   for entry in built["sidecar"]), encoding="utf-8")
    report = dict(built["stats"])
    report.update({
        "outputs": {
            "train": {"path": str(train_path), "sha256": sha256(train_path),
                      "rows": len(built["records"])},
            "sidecar": {"path": str(sidecar_path), "sha256": sha256(sidecar_path),
                        "rows": len(built["sidecar"])},
        },
        "record_fields": sorted(built["records"][0].keys()),
        "sidecar_fields": sorted(built["sidecar"][0].keys()),
    })
    report_path = out / "loader_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def load_train_records(out: pathlib.Path | None = None) -> list[dict]:
    """Read the written training JSONL, building it first if missing."""
    out = pathlib.Path(out) if out else HERE
    path = out / "native_pg_train.jsonl"
    if not path.is_file():
        built = build_records()
        write(built, out)
    return load_jsonl(path)


def apply_long_schema_policy(records: list[dict] | None = None, out: pathlib.Path | None = None) -> dict:
    """Apply the t31 decisions deterministically to one corpus snapshot.

    ``train_main`` = rows outside the over-budget bucket; the bucket is kept whole in
    ``train_long_schema``; rows marked ``usable_after_shortening`` are returned with the
    shortened prompt from ``train_short_schema.jsonl`` and can be appended to a later arm.
    """
    out = pathlib.Path(out) if out else HERE
    report = json.loads((out / "short_schema_report.json").read_text(encoding="utf-8"))
    rows = records if records is not None else load_train_records(out)
    decisions = {str(item["sample_id"]): item for item in report["per_row"]}
    bucket_ids = set(report["bucket_ids"])
    short_rows = {str(item["sample_id"]): item for item in
                  load_jsonl(out / "train_short_schema.jsonl")} if (out / "train_short_schema.jsonl").is_file() else {}
    main = [row for row in rows if str(row["sample_id"]) not in bucket_ids]
    bucket = [row for row in rows if str(row["sample_id"]) in bucket_ids]
    usable = [short_rows[sid] for sid, decision in decisions.items()
              if decision["decision"] == "usable_after_shortening" and sid in short_rows]
    dropped = [sid for sid, decision in decisions.items() if decision["decision"] == "drop"]
    unusable_selector = [sid for sid, decision in decisions.items()
                         if decision["decision"] == "unusable_selector_coverage"]
    result = {"train_main": main, "train_long_schema": bucket,
              "usable_after_shortening": usable, "unusable_selector_coverage": unusable_selector,
              "dropped": dropped}
    if out.is_file() is False:
        pass
    return result


def dev_record(question_id: str) -> dict:
    """One reviewed dev-217 row as a renderer record (used by the semantic smoke tests)."""
    for row in load_jsonl(PACKAGE / "data" / "bird_dev_pg.jsonl"):
        if str(row.get("question_id")) == str(question_id):
            return {"sample_id": f"dev-{question_id}", "db_id": row["db_id"],
                    "question": row["question"], "evidence": row.get("evidence") or "",
                    "pg_gold_sql": row["pg_gold_sql"], "split": "internal_valid"}
    raise KeyError(f"dev question {question_id!r} not found")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="build the native-PostgreSQL SFT corpus")
    parser.add_argument("--out", type=pathlib.Path, default=HERE)
    parser.add_argument("--apply-long-schema-policy", action="store_true",
                        help="apply datasets/native_pg/short_schema_report.json and print the split counts")
    args = parser.parse_args(argv)
    built = build_records()
    report = write(built, args.out)
    print(json.dumps({key: report[key] for key in
                      ("rows_in", "rows_kept", "rows_filtered", "rows_filtered_by_split",
                       "rows_filtered_by_reason", "train_rows_real", "train_rows_synthetic",
                       "skills")}, indent=2))
    print(f"train: {report['outputs']['train']['path']}")
    print(f"sidecar: {report['outputs']['sidecar']['path']}")
    print(f"report: {args.out / 'loader_report.json'}")
    if args.apply_long_schema_policy:
        split = apply_long_schema_policy(built["records"], args.out)
        print(json.dumps({"main": len(split["train_main"]), "bucket": len(split["train_long_schema"]),
                          "usable_after_shortening": len(split["usable_after_shortening"]),
                          "dropped": len(split["dropped"])}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
