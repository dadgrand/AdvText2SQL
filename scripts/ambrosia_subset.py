"""Materialize the explicit 227-question Ambrosia subset used historically."""

import hashlib
import json

EXCLUDED_DATABASES = {
    "attachment_1tab_ref_seating_area",
    "scope_law_firms_legal_positions",
    "vague_2cols_rating2",
}


def prepare_subset(root):
    source = root / "data" / "ambrosia_large.json"
    items = json.loads(source.read_text(encoding="utf-8"))
    included = [item for item in items if item["db_id"] not in EXCLUDED_DATABASES]
    excluded = [item for item in items if item["db_id"] in EXCLUDED_DATABASES]
    if len(items) != 240 or len(included) != 227 or len(excluded) != 13:
        raise ValueError("Ambrosia source changed; review the subset before running")
    ids = [str(item["question_id"]) for item in included]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate question IDs in Ambrosia subset")
    target = root / "data" / "ambrosia_large_available_227.json"
    content = json.dumps(included, ensure_ascii=False, indent=2) + "\n"
    manifest = {
        "source": "data/ambrosia_large.json",
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "source_questions": len(items), "included_questions": len(included),
        "included_databases": len({item["db_id"] for item in included}),
        "question_ids": ids,
        "excluded": [{"question_id": item["question_id"], "db_id": item["db_id"],
                      "reason": "Database has no user tables; confirmed by PostgreSQL catalog checks"}
                     for item in excluded],
        "dataset_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }
    # Preserve exact bytes and question order for all subsequent configurations.
    if not target.exists() or target.read_bytes() != content.encode("utf-8"):
        target.write_bytes(content.encode("utf-8"))
    manifest_path = target.with_suffix(".manifest.json")
    manifest_content = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    if not manifest_path.exists() or manifest_path.read_bytes() != manifest_content.encode("utf-8"):
        manifest_path.write_bytes(manifest_content.encode("utf-8"))
    return target, manifest
