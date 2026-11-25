#!/usr/bin/env python3
"""Offline decontamination tests for the schema-family split contract.

Run directly (exit code 0 = clean, 1 = violation):

    python3 datasets/splits/test_no_overlap.py

or with pytest:

    pytest datasets/splits/test_no_overlap.py -q

The tests only read files in the workspace: no network, no database, no model.
They re-derive every count from the raw JSONL files instead of trusting
``splits_report.json``; the report is compared against the recomputation.
"""

from __future__ import annotations

import collections
import hashlib
import json
import pathlib
import re
import sys
import unicodedata

HERE = pathlib.Path(__file__).resolve().parent
PROJECT = HERE.parents[1]  # research/advtext2sql
PKG = PROJECT / "repo" / "datasets" / "bird_pg_unified"
CLUSTER = PROJECT / "cluster"
DELIVERY = PROJECT / "delivery"

MANIFEST_PATH = HERE / "schema_family_split.json"
REPORT_PATH = HERE / "splits_report.json"
EVAL_PATH = HERE / "eval_questions.jsonl"

OOD_RESERVED = [
    "disney", "movie_3", "talkingdata", "menu", "movies_4",
    "law_episode", "european_football_1", "social_media", "sales_in_weather", "music_tracker",
]
INTERNAL_VALID = [
    "california_schools", "card_games", "codebase_community", "debit_card_specializing",
    "financial", "student_club", "superhero", "toxicology",
]
EXTERNAL_TEST = ["formula_1", "european_football_2", "thrombosis_prediction"]
VALID_SPLITS = ("train", "internal_valid", "ood_reserved", "external_test")

TRAINABLE_FILES = [
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
TRAINABLE_FILES += sorted((DELIVERY / "out" / "synth" / "overnight").glob("*.jsonl"))

_CACHE: dict[str, object] = {}


# ----------------------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------------------
def load_jsonl(path: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def manifest() -> dict:
    if "manifest" not in _CACHE:
        _CACHE["manifest"] = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return _CACHE["manifest"]  # type: ignore[return-value]


def report() -> dict:
    if "report" not in _CACHE:
        _CACHE["report"] = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    return _CACHE["report"]  # type: ignore[return-value]


def families() -> dict[str, dict]:
    return manifest()["families"]


def db_to_split() -> dict[str, str]:
    return {db: family["split"] for db, family in families().items()}


def eval_rows() -> list[dict]:
    if "eval" not in _CACHE:
        _CACHE["eval"] = load_jsonl(EVAL_PATH)
    return _CACHE["eval"]  # type: ignore[return-value]


def pools() -> dict[str, list[dict]]:
    if "pools" not in _CACHE:
        _CACHE["pools"] = {
            name: load_jsonl(PKG / "data" / f"{name}.jsonl")
            for name in ("bird_train_pg", "synthetic_bird_like",
                         "synthetic_cap_unnamed", "synthetic_single_table_extra")
            if (PKG / "data" / f"{name}.jsonl").is_file()
        }
    return _CACHE["pools"]  # type: ignore[return-value]


def split_sets() -> dict[str, set[str]]:
    return {split: set(definition["db_ids"]) for split, definition in manifest()["splits"].items()}


def recompute_split_counts() -> dict[str, collections.Counter]:
    """Counts per split for the two main pools (real train, active synthetic)."""
    mapping = db_to_split()
    counts = {split: collections.Counter() for split in VALID_SPLITS}
    for pool, key in (("bird_train_pg", "real"), ("synthetic_bird_like", "synthetic")):
        for row in pools()[pool]:
            db = row["db_id"]
            assert db in mapping, f"{pool}: database {db!r} is missing from the manifest"
            counts[mapping[db]][key] += 1
    return counts


def recompute_ood_rows() -> tuple[list[dict], list[dict]]:
    """Rows mapped to the ood_reserved split (named OOD + lineage extensions)."""
    real = [row for row in pools()["bird_train_pg"] if db_to_split()[row["db_id"]] == "ood_reserved"]
    synth = [row for row in pools()["synthetic_bird_like"] if db_to_split()[row["db_id"]] == "ood_reserved"]
    return real, synth


def recompute_named_ood_overlap() -> tuple[list[dict], list[dict]]:
    """Strict playbook overlap: only the ten historical OOD names, regardless of split."""
    real = [row for row in pools()["bird_train_pg"] if row["db_id"] in OOD_RESERVED]
    synth = [row for row in pools()["synthetic_bird_like"] if row["db_id"] in OOD_RESERVED]
    return real, synth


def question_overlaps(threshold: float = 0.8) -> tuple[list[dict], float]:
    mapping = db_to_split()
    train = []
    for pool, rows in pools().items():
        for row in rows:
            if mapping[row["db_id"]] == "train" and row.get("question"):
                train.append((row["question"], f"{pool}:{row_id(row)}", row["db_id"]))
    eval_questions = [(row["question"], row["eval_id"], row["db_id"]) for row in eval_rows()
                      if row.get("question")]

    index: dict[tuple, list[int]] = collections.defaultdict(list)
    eval_grams = []
    for i, (question, _eval_id, _db) in enumerate(eval_questions):
        grams = ngrams(question)
        eval_grams.append(grams)
        for gram in grams:
            index[gram].append(i)

    violations = []
    maximum = 0.0
    for question, train_id, db in train:
        grams = ngrams(question)
        if not grams:
            continue
        candidates: collections.Counter = collections.Counter()
        for gram in grams:
            for i in index.get(gram, ()):
                candidates[i] += 1
        for i, shared in candidates.items():
            if shared < 2:
                continue
            score = shared / len(grams | eval_grams[i])
            maximum = max(maximum, score)
            if score >= threshold:
                violations.append({"train_id": train_id, "eval_id": eval_questions[i][1],
                                   "jaccard": round(score, 4)})
    return violations, maximum


# ----------------------------------------------------------------------------------------------
# tests
# ----------------------------------------------------------------------------------------------
def test_manifest_maps_every_registry_schema_to_one_valid_split():
    schema = json.loads((PKG / "schemas" / "schema.json").read_text(encoding="utf-8"))
    fam = families()
    missing = set(schema) - set(fam)
    assert not missing, f"registry databases without a split: {sorted(missing)}"
    missing_external = set(EXTERNAL_TEST) - set(fam)
    assert not missing_external, f"external test schemas without a family: {sorted(missing_external)}"
    for db, family in fam.items():
        assert family["split"] in VALID_SPLITS, f"{db}: invalid split {family['split']!r}"
        assert db in manifest()["splits"][family["split"]]["db_ids"], \
            f"{db}: split field disagrees with the splits section"


def test_no_db_id_or_schema_family_in_two_splits():
    sets = split_sets()
    for first in VALID_SPLITS:
        for second in VALID_SPLITS:
            if first >= second:
                continue
            common_dbs = sets[first] & sets[second]
            assert not common_dbs, f"db_ids in both {first} and {second}: {sorted(common_dbs)}"
            fam_first = set(manifest()["splits"][first]["schema_families"])
            fam_second = set(manifest()["splits"][second]["schema_families"])
            common_fam = fam_first & fam_second
            assert not common_fam, f"schema families in both {first} and {second}: {sorted(common_fam)}"
    total = sum(len(sets[split]) for split in VALID_SPLITS)
    assert total == len(families()), \
        f"split membership covers {total} db_ids but the manifest has {len(families())} db entries"
    unique_families = {family["schema_family"] for family in families().values()}
    listed_families = set()
    for definition in manifest()["splits"].values():
        listed_families.update(definition["schema_families"])
    assert listed_families == unique_families, "the splits section does not list every schema family"
    assert sum(definition["schema_family_count"] for definition in manifest()["splits"].values()) \
        == len(unique_families)


def test_ood_reserved_contract():
    assert manifest()["ood_reserved_names"] == OOD_RESERVED
    fam = families()
    for db in OOD_RESERVED:
        assert db in fam, f"OOD database {db!r} has no schema family"
        assert fam[db]["split"] == "ood_reserved", f"OOD database {db!r} is in {fam[db]['split']}"
    assert fam["talkingdata"]["pool_rows_real"] == 0
    assert fam["talkingdata"]["pool_rows_synthetic"] == 0
    assert manifest()["external_test_schemas"] == EXTERNAL_TEST
    assert set(manifest()["external_test_unavailable_schemas"]) <= set(fam)

    extensions = set(manifest()["ood_lineage_extensions"])
    assert extensions == {"simpson_episodes"}, f"unexpected OOD lineage extensions: {extensions}"
    ood_db_ids = set(manifest()["splits"]["ood_reserved"]["db_ids"])
    assert ood_db_ids == set(OOD_RESERVED) | extensions, \
        f"ood_reserved split contains unexpected databases: {sorted(ood_db_ids - set(OOD_RESERVED))}"
    for db in extensions:
        assert fam[db]["split"] == "ood_reserved", f"lineage sibling {db!r} is still trainable"
        assert fam[db]["schema_family"] == fam["law_episode"]["schema_family"], \
            "lineage siblings must share one schema family"
        assert isinstance(fam[db].get("pool_rows_real"), int)


def test_renamed_copies_never_cross_splits():
    fam = families()
    groups: dict[str, list[str]] = collections.defaultdict(list)
    for db, family in fam.items():
        if family.get("lineage_fingerprint"):
            groups[family["lineage_fingerprint"]].append(db)
    for fingerprint, dbs in groups.items():
        splits = {fam[db]["split"] for db in dbs}
        assert len(splits) <= 1, f"identical schema fingerprint {fingerprint[:12]} spans splits {sorted(splits)}: {dbs}"
    candidates = manifest()["renamed_copy_candidates"]
    cross_split = [entry for entry in candidates["exact_fingerprint_groups"] if len(entry["splits"]) > 1]
    assert not cross_split, f"renamed copies across splits: {cross_split}"

    lineage = candidates["near_duplicate_lineage_groups"]
    assert lineage, "expected the law_episode/simpson_episodes near-copy to be reported"
    for group in lineage:
        splits = {fam[db]["split"] for db in group["db_ids"]}
        assert len(splits) == 1, f"lineage near-copy spans splits {sorted(splits)}: {group['db_ids']}"
        assert len({fam[db]["schema_family"] for db in group["db_ids"]}) == 1


def _audit_trainable_file(path: pathlib.Path, mapping: dict[str, str]) -> dict:
    rows = load_jsonl(path)
    by_split = collections.Counter()
    unknown = 0
    for row in rows:
        split = mapping.get(row.get("db_id"))
        if split is None:
            unknown += 1
        else:
            by_split[split] += 1
    return {"rows": len(rows), "by_split": by_split, "unknown": unknown}


def test_external_test_schemas_absent_from_every_trainable_file():
    mapping = db_to_split()
    externals = {db for db, split in mapping.items() if split == "external_test"}
    assert externals, "no external_test schemas in the manifest"
    for path in TRAINABLE_FILES:
        if not path.is_file():
            continue
        audit = _audit_trainable_file(path, mapping)
        assert audit["unknown"] == 0, f"{path}: {audit['unknown']} rows with an unmapped db_id"
        leaked = audit["by_split"].get("external_test", 0)
        assert leaked == 0, f"{path}: {leaked} rows on external_test schemas"
    assert report()["external_test"]["checked_absent_from_trainable_files"] is True


def test_internal_valid_schemas_absent_from_trainable_files():
    mapping = db_to_split()
    for path in TRAINABLE_FILES:
        if not path.is_file():
            continue
        audit = _audit_trainable_file(path, mapping)
        leaked = audit["by_split"].get("internal_valid", 0)
        assert leaked == 0, f"{path}: {leaked} rows on internal_valid (reviewed dev) schemas"


def test_ood_rows_in_trainable_files_are_documented():
    mapping = db_to_split()
    real, synth = recompute_ood_rows()
    excluded = report()["excluded_rows"]
    assert {row_id(row) for row in real} == {row["question_id"] for row in excluded["bird_train_pg.jsonl"]}, \
        "excluded real rows in the report do not match the OOD rows in the unified train pool"
    assert {row_id(row) for row in synth} == {row["question_id"] for row in excluded["synthetic_bird_like.jsonl"]}, \
        "excluded synthetic rows in the report do not match the OOD rows in the synthetic pool"

    audited = {entry["path"]: entry for entry in report()["trainable_files"]}
    for path in TRAINABLE_FILES:
        if not path.is_file():
            continue
        audit = _audit_trainable_file(path, mapping)
        entry = audited.get(str(path))
        assert entry is not None, f"{path} is missing from the report's trainable-file audit"
        assert entry["rows"] == audit["rows"]
        assert entry["rows_by_split"]["ood_reserved"] == audit["by_split"].get("ood_reserved", 0), \
            f"{path}: OOD row count changed since the report was written"

    sft = CLUSTER / "sft_all_v2.jsonl"
    if sft.is_file():
        actual = _audit_trainable_file(sft, mapping)["by_split"].get("ood_reserved", 0)
        assert report()["training_corpus_warning"]["ood_reserved_rows_present"] == actual
        assert actual > 0, "expected sft_all_v2.jsonl to still contain the documented OOD overlap"


def test_no_question_in_both_train_and_eval_splits():
    violations, maximum = question_overlaps(0.8)
    assert not violations, f"{len(violations)} question pairs reach Jaccard >= 0.8: {violations[:5]}"
    stored = report()["question_decontamination"]
    assert stored["threshold"] == 0.8
    assert stored["violations"] == []
    assert abs(stored["max_jaccard"] - round(maximum, 4)) < 1e-3, \
        f"report max_jaccard {stored['max_jaccard']} != recomputed {maximum:.4f}"


def test_eval_registry_is_closed_and_matches_splits():
    rows = eval_rows()
    counts = collections.Counter(row["source_shard"] for row in rows)
    assert counts == {"internal_valid": 217, "ood_reserved": 876, "external_test": 167}, \
        f"unexpected eval registry counts: {dict(counts)}"
    assert len({row["eval_id"] for row in rows}) == len(rows), "duplicate eval_id in the registry"

    mapping = db_to_split()
    for row in rows:
        assert row["source_shard"] in VALID_SPLITS
        assert mapping[row["db_id"]] == row["source_shard"], \
            f"{row['db_id']} is in split {mapping[row['db_id']]} but registered in {row['source_shard']}"
        assert row["question"] and row["question_id"], f"incomplete eval row: {row['eval_id']}"
    internal = {row["db_id"] for row in rows if row["source_shard"] == "internal_valid"}
    assert internal == set(INTERNAL_VALID)
    ood = {row["db_id"] for row in rows if row["source_shard"] == "ood_reserved"}
    assert ood == set(OOD_RESERVED)
    external = {row["db_id"] for row in rows if row["source_shard"] == "external_test"}
    assert external == set(EXTERNAL_TEST)


def test_report_counts_are_recomputed_not_copied():
    recomputed = recompute_split_counts()
    stored = report()["splits"]
    for split in VALID_SPLITS:
        assert stored[split]["real_rows"] == recomputed[split]["real"], f"{split}: real row count drift"
        assert stored[split]["synthetic_rows"] == recomputed[split]["synthetic"], \
            f"{split}: synthetic row count drift"

    named_real, named_synth = recompute_named_ood_overlap()
    split_real, split_synth = recompute_ood_rows()
    claim = report()["claim_check"]
    assert claim["verdict"] == "confirmed", f"playbook claim not confirmed: {claim['mismatches']}"
    assert claim["recomputed"]["real_overlap_rows"] == len(named_real) == 745
    assert claim["recomputed"]["synthetic_overlap_rows"] == len(named_synth) == 112
    assert claim["recomputed"]["databases_with_overlap"] == 9
    assert claim["recomputed"]["real_remaining"] == len(pools()["bird_train_pg"]) - len(named_real) == 4365
    assert claim["recomputed"]["synthetic_remaining"] == \
        len(pools()["synthetic_bird_like"]) - len(named_synth) == 2703
    assert claim["lineage_aware_split"]["real_remaining"] == len(pools()["bird_train_pg"]) - len(split_real)
    assert claim["lineage_aware_split"]["synthetic_remaining"] == \
        len(pools()["synthetic_bird_like"]) - len(split_synth)
    assert stored["train"]["real_rows"] == len(pools()["bird_train_pg"]) - len(split_real)
    assert stored["train"]["synthetic_rows"] == len(pools()["synthetic_bird_like"]) - len(split_synth)
    assert stored["ood_reserved"]["real_rows"] == len(split_real)
    assert stored["ood_reserved"]["synthetic_rows"] == len(split_synth)

    assert report()["manifest"]["sha256"] == sha256(MANIFEST_PATH)
    assert report()["eval_registry"]["sha256"] == sha256(EVAL_PATH)
    counts = collections.Counter(row["source_shard"] for row in eval_rows())
    for split in VALID_SPLITS:
        assert stored[split]["eval_questions"] == counts.get(split, 0)
    assert len(eval_rows()) == report()["eval_registry"]["rows"]


TESTS = [
    ("manifest maps every registry schema to one valid split", test_manifest_maps_every_registry_schema_to_one_valid_split),
    ("no db_id or schema family appears in two splits", test_no_db_id_or_schema_family_in_two_splits),
    ("the ten historical OOD databases are reserved", test_ood_reserved_contract),
    ("renamed schema copies never cross splits", test_renamed_copies_never_cross_splits),
    ("external_test schemas absent from every trainable file", test_external_test_schemas_absent_from_every_trainable_file),
    ("internal_valid (dev) schemas absent from every trainable file", test_internal_valid_schemas_absent_from_trainable_files),
    ("OOD rows in trainable files match the documented excluded list", test_ood_rows_in_trainable_files_are_documented),
    ("no normalised question reaches Jaccard 0.8 across train/eval", test_no_question_in_both_train_and_eval_splits),
    ("eval registry is closed and agrees with the split map", test_eval_registry_is_closed_and_matches_splits),
    ("report counts are recomputed from the files", test_report_counts_are_recomputed_not_copied),
]


def main() -> int:
    failed = []
    for name, test in TESTS:
        try:
            test()
        except AssertionError as exc:
            failed.append((name, str(exc)))
            print(f"FAIL  {name}\n      {exc}")
        else:
            print(f"PASS  {name}")
    print()
    if failed:
        print(f"{len(failed)} of {len(TESTS)} tests failed")
        return 1
    print(f"ALL {len(TESTS)} TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
