"""Reproduce the exact ambiguity-gated subset of a completed BIRD hybrid run."""

import hashlib
import json

from feature_presets import feature_flags
from research_paths import resolve_artifact_path


def select_ambiguous(root, source):
    source = resolve_artifact_path(root, source).resolve()
    metadata = json.loads((source / "metadata.json").read_text(encoding="utf-8-sig"))
    if metadata["status"] != "completed":
        raise ValueError("Source hybrid must be completed")
    if metadata["configuration"]["feature_flags"] != feature_flags("hybrid"):
        raise ValueError("Source is not the fixed hybrid configuration")
    dataset = root / "data/bird_large.json"
    if hashlib.sha256(dataset.read_bytes()).hexdigest() != metadata["configuration"]["dataset_sha256"]:
        raise ValueError("Source BIRD dataset hash differs")
    original = json.loads(dataset.read_text(encoding="utf-8"))
    checkpoint = source / "query_results.json"
    predictions = json.loads(checkpoint.read_text(encoding="utf-8-sig"))
    if set(predictions) != {str(r["question_id"]) for r in original}:
        raise ValueError("Source hybrid coverage differs from the full dataset")
    selected = [r for r in original if predictions[str(r["question_id"])] == "ambiguous"]
    if len(selected) != 50:
        raise ValueError(f"Expected the agreed 50-question subset, found {len(selected)}")
    return selected, {
        "kind": "diagnostic_subset_selected_by_previous_ambiguous_predictions",
        "source_run": str(source),
        "source_checkpoint_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
        "source_dataset_sha256": metadata["configuration"]["dataset_sha256"],
        "source_configuration": metadata["configuration"],
        "source_questions": len(original), "selected_questions": len(selected),
        "question_ids": [str(r["question_id"]) for r in selected],
        "disabled_features": ["FEAT_32", "FEAT_33"],
    }


def compare_subset(root, directory, manifest, report):
    source = resolve_artifact_path(root, manifest["source_run"])
    old_report = json.loads((source / "report.json").read_text(encoding="utf-8-sig"))
    old_results = {str(r["question_id"]): r for r in old_report["results"]}
    new_results = {str(r["question_id"]): r for r in report["results"]}
    expected = set(manifest["question_ids"])
    if set(new_results) != expected:
        raise ValueError("Subset evaluation coverage differs")
    correct = sum(bool(r["score"]) for r in new_results.values())
    source_correct = sum(bool(r["score"]) for r in old_results.values())
    if any(old_results[q]["score"] for q in expected):
        raise ValueError("Expected zero correct source answers on ambiguity-gated questions")
    comparison = {
        "selected_questions": len(expected), "subset_correct": correct,
        "subset_accuracy": 100 * correct / len(expected),
        "source_subset_correct": 0,
        "source_full_correct": source_correct, "source_full_questions": len(old_results),
        "composite_full_correct": source_correct + correct,
        "composite_full_accuracy": 100 * (source_correct + correct) / len(old_results),
        "note": "Composite reuses the original predictions on all other questions; not a full ablation rerun.",
    }
    baseline_file = root / "research_results/runs/gpt54mini_baseline_large_20261016/bird/report.json"
    if baseline_file.exists():
        baseline = {str(r["question_id"]): r for r in json.loads(baseline_file.read_text(encoding="utf-8-sig"))["results"]}
        comparison["baseline_subset_correct"] = sum(bool(baseline[q]["score"]) for q in expected)
    return comparison
