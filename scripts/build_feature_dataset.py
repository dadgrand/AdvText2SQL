"""
Build per-question feature attribution dataset from ablation results.

For each question in the BIRD dataset, records which features helped it pass.
Output: data/feature_labels.json

Usage:
    python scripts/build_feature_dataset.py [--ablation-dir research_results/ablation_results_small/] [--out data/feature_labels.json]

Dataset schema (one entry per question):
{
  "question_id": 9,
  "question": "...",
  "db_id": "california_schools",
  "difficulty": "simple",
  "evidence": "...",
  "feature_scores": {            # 1 = feature helped this question pass, 0 = didn't help
    "FEAT_1": 0,
    "FEAT_2": 1,
    ...
  },
  "baseline_correct": false,     # true if all-false baseline already passes this question
  "best_combo_correct": true     # true if best known combo passes this question
}
"""

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from research_paths import resolve_artifact_path


# Small dataset dirs (ablation v2, 22 questions)
FEAT_DIR_MAP_SMALL = {
    "FEAT_1":  "feat_01_fk_pk",
    "FEAT_2":  "feat_02_column_stats",
    "FEAT_3":  "feat_03_regex_types",
    "FEAT_4":  "feat_04_pg_stat",
    "FEAT_5":  "feat_05_light_schema",
    "FEAT_6":  "feat_06_heavy_schema",
    "FEAT_7":  "feat_07_compact_stats",
    "FEAT_8":  "feat_08_rollback",
    "FEAT_9":  "feat_09_schema_dump",
    "FEAT_10": "feat_10_timing",
    "FEAT_11": "feat_11_json_log",
    "FEAT_12": "feat_12_optimistic",
    "FEAT_13": "feat_13_fewshot",
    "FEAT_14": "feat_14_strict_sql",
    "FEAT_15": "feat_15_sanitize",
    "FEAT_16": "feat_16_validate",
    "FEAT_17": "feat_17_retry",
    "FEAT_18": "feat_18_backoff",
    "FEAT_19": "feat_19_throttle",
    "FEAT_20": "feat_20_llm_verify",
    "FEAT_21": "feat_21_minhash",
    "FEAT_23": "feat_23_sql_to_text",
    "FEAT_25": "feat_25_schema_pruning",
    "FEAT_26": "feat_26_learnt_hints",
    "FEAT_27": "feat_27_self_correction",
    "FEAT_28": "feat_28_self_consistency",
    "FEAT_29": "feat_29_complexity",
    "FEAT_30": "feat_30_sub_question",
    "FEAT_32": "feat_32_taxonomy",
    "FEAT_33": "feat_33_clarification",
    "FEAT_34": "feat_34_rewrite",
    "FEAT_35": "feat_35_tsv",
    "FEAT_36": "feat_36_smart_limit",
}

# Large dataset dirs — produced by local/ablation_large_bird.sh / ablation_large_ambrosia.sh
FEAT_DIR_MAP_LARGE_BIRD = {feat: f"{feat.lower()}_bird" for feat in FEAT_DIR_MAP_SMALL}
FEAT_DIR_MAP_LARGE_AMBROSIA = {feat: f"{feat.lower()}_ambrosia" for feat in FEAT_DIR_MAP_SMALL}

FEAT_DIR_MAP = FEAT_DIR_MAP_SMALL  # default; overridden by --large/--benchmark flags

BASELINE_DIR = "feat_baseline"
BEST_COMBO_DIR = "feat_4_both"  # best known combination on small dataset


def parse_scores_from_log(log_path: Path) -> dict[str, int]:
    """Parse per-question scores from bird.log file.

    The log ends with print(results) — a Python list of dicts with question_id and score.
    Uses ast.literal_eval for safe parsing.
    Returns {question_id_str: score_int}.
    """
    text = log_path.read_text(encoding="utf-8", errors="replace")

    # Find the big printed list: starts with "[{'" and ends with "}]"
    # It's the last occurrence of a list-of-dicts pattern in the file
    matches = list(re.finditer(r"\[\{['\"]question_id['\"].*?\}\]", text, re.DOTALL))
    if not matches:
        return {}

    raw = matches[-1].group(0)
    try:
        results = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        print(f"  WARNING: could not parse results from {log_path}", file=sys.stderr)
        return {}

    scores = {}
    for r in results:
        qid = str(r.get("question_id", ""))
        score = r.get("score", 0)
        scores[qid] = int(bool(score))
    return scores


def load_bird_questions(data_path: Path) -> dict[str, dict]:
    """Load BIRD dataset, return {question_id_str: item}."""
    with open(data_path, encoding="utf-8") as f:
        items = json.load(f)
    return {str(item["question_id"]): item for item in items}


def parse_scores_from_query_results(
    query_results_path: Path, gold_questions: dict[str, dict]
) -> dict[str, int]:
    """Compute per-question correctness for Ambrosia from saved predictions.

    Ambrosia's evaluate_ambrosia.py only returns an aggregate report (no
    per-question dump like BIRD's printed results list), so correctness is
    recomputed here the same way: gold_is_ambiguous == predicted_is_ambiguous.
    """
    if not query_results_path.exists():
        return {}
    with open(query_results_path, encoding="utf-8") as f:
        predictions = json.load(f)

    scores = {}
    for qid, pred in predictions.items():
        gold = gold_questions.get(qid)
        if gold is None:
            continue
        gold_is_amb = gold["SQL"] == "ambiguous"
        pred_is_amb = pred == "ambiguous"
        scores[qid] = int(gold_is_amb == pred_is_amb)
    return scores


def main():
    parser = argparse.ArgumentParser(description="Build feature attribution dataset from ablation results")
    parser.add_argument("--ablation-dir", default=None, help="Ablation directory under research_results/ (small or large by --large)")
    parser.add_argument("--data-file", default="data/bird_small.json", help="Dataset file (BIRD or Ambrosia, small or large)")
    parser.add_argument("--out", default="data/feature_labels.json", help="Output file")
    parser.add_argument("--large", action="store_true", help="Use large dataset dirs instead of small")
    parser.add_argument("--benchmark", default="bird", choices=["bird", "ambrosia"], help="Which benchmark's ablation results to parse")
    args = parser.parse_args()

    global FEAT_DIR_MAP
    if args.large:
        FEAT_DIR_MAP = FEAT_DIR_MAP_LARGE_AMBROSIA if args.benchmark == "ambrosia" else FEAT_DIR_MAP_LARGE_BIRD

    ablation_root = resolve_artifact_path(Path(__file__).resolve().parents[1], args.ablation_dir or (
        "research_results/ablation_results_large" if args.large else "research_results/ablation_results_small"))
    data_file = Path(args.data_file)
    out_file = Path(args.out)
    log_name = "ambrosia.log" if args.benchmark == "ambrosia" else "bird.log"
    results_name = "ambrosia_query_results.json" if args.benchmark == "ambrosia" else "bird_query_results.json"

    print(f"Loading {args.benchmark} questions from {data_file}...")
    questions = load_bird_questions(data_file)
    print(f"  {len(questions)} questions")

    def parse_scores(dir_path: Path) -> dict[str, int]:
        if args.benchmark == "ambrosia":
            return parse_scores_from_query_results(dir_path / results_name, questions)
        return parse_scores_from_log(dir_path / log_name)

    # Load baseline scores (all-features-false)
    baseline_scores: dict[str, int] = {}
    baseline_dir = ablation_root / BASELINE_DIR
    if (baseline_dir / log_name).exists() or (baseline_dir / results_name).exists():
        baseline_scores = parse_scores(baseline_dir)
        correct = sum(baseline_scores.values())
        print(f"Baseline: {correct}/{len(baseline_scores)} correct ({100*correct/max(len(baseline_scores),1):.1f}%)")
    else:
        print(f"WARNING: baseline not found at {baseline_dir}")

    # Load best combo scores (BIRD only — no large-Ambrosia best combo dir yet)
    best_combo_scores: dict[str, int] = {}
    best_combo_dir = ablation_root / BEST_COMBO_DIR
    if args.benchmark == "bird" and (best_combo_dir / log_name).exists():
        best_combo_scores = parse_scores(best_combo_dir)
        correct = sum(best_combo_scores.values())
        print(f"Best combo: {correct}/{len(best_combo_scores)} correct ({100*correct/max(len(best_combo_scores),1):.1f}%)")

    # Load per-feature scores
    feature_scores_by_question: dict[str, dict[str, int]] = {qid: {} for qid in questions}
    feat_coverage = {}

    print("\nParsing ablation results...")
    for feat, dir_name in FEAT_DIR_MAP.items():
        dir_path = ablation_root / dir_name
        if not (dir_path / log_name).exists() and not (dir_path / results_name).exists():
            print(f"  {feat}: results not found at {dir_path}")
            feat_coverage[feat] = 0
            continue

        scores = parse_scores(dir_path)
        feat_coverage[feat] = len(scores)
        n_helped = 0
        for qid, score in scores.items():
            if qid in feature_scores_by_question:
                feature_scores_by_question[qid][feat] = score
                if score and not baseline_scores.get(qid, 0):
                    n_helped += 1
        correct = sum(scores.values())
        print(f"  {feat}: {correct}/{len(scores)} correct, helped {n_helped} vs baseline")

    # Build output dataset
    out_file.parent.mkdir(parents=True, exist_ok=True)
    dataset = []
    for qid, q in questions.items():
        entry = {
            "question_id": q["question_id"],
            "question": q["question"],
            "db_id": q["db_id"],
            "feature_scores": feature_scores_by_question.get(qid, {}),
            "baseline_correct": bool(baseline_scores.get(qid, 0)),
            "best_combo_correct": bool(best_combo_scores.get(qid, 0)),
        }
        if args.benchmark == "bird":
            entry["difficulty"] = q["difficulty"]
            entry["evidence"] = q.get("evidence", "")
        else:
            entry["gold_ambiguous"] = q["SQL"] == "ambiguous"
        dataset.append(entry)

    # Sort by question_id
    dataset.sort(key=lambda x: x["question_id"])

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(dataset, f, ensure_ascii=False, indent=2)

    print(f"\nDataset saved to {out_file}")
    print(f"  Total questions: {len(dataset)}")
    print(f"  Features covered: {sum(1 for v in feat_coverage.values() if v > 0)}/{len(FEAT_DIR_MAP)}")

    # Summary stats
    baseline_correct = sum(1 for q in dataset if q["baseline_correct"])
    best_correct = sum(1 for q in dataset if q["best_combo_correct"])
    no_feat_helps = sum(1 for q in dataset if not q["best_combo_correct"])
    print(f"  Baseline correct: {baseline_correct}/{len(dataset)}")
    print(f"  Best combo correct: {best_correct}/{len(dataset)}")
    print(f"  Unsolvable (hard negatives): {no_feat_helps}")

    # Feature utility ranking
    print("\nFeature utility (questions helped beyond baseline):")
    utility = []
    for feat in FEAT_DIR_MAP:
        helped = sum(
            1 for q in dataset
            if q["feature_scores"].get(feat, 0) == 1 and not q["baseline_correct"]
        )
        utility.append((feat, helped))
    for feat, n in sorted(utility, key=lambda x: -x[1]):
        if n > 0:
            print(f"  {feat}: +{n} questions")


if __name__ == "__main__":
    main()
