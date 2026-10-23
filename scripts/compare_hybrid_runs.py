"""Compare completed GPT hybrid results with its baseline and historical best."""

import argparse
import json
from pathlib import Path
import re
from research_paths import resolve_artifact_path

ROOT = Path(__file__).resolve().parents[1]
CURRENT = ROOT / "research_results/runs/gpt54mini_hybrid_large_20261016"
BASELINE = ROOT / "research_results/runs/gpt54mini_baseline_large_20261016"
HISTORICAL = ROOT / "research_results/ablation_results_small/feat_best_hybrid_large"


def load(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def ambiguity_metrics(predictions, gold):
    tp = sum(gold[q] and v == "ambiguous" for q, v in predictions.items())
    tn = sum(not gold[q] and v != "ambiguous" for q, v in predictions.items())
    fp = sum(not gold[q] and v == "ambiguous" for q, v in predictions.items())
    fn = sum(gold[q] and v != "ambiguous" for q, v in predictions.items())
    accuracy = 100 * (tp + tn) / len(predictions)
    f1 = 200 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0
    return accuracy, f1, tp, fp


def main(run_dir=CURRENT):
    current = resolve_artifact_path(ROOT, run_dir).resolve()
    lines = ["# Hybrid comparison", "",
             "Historical results use a different model; the GPT baseline isolates the feature effect.", ""]
    for name, folder in (("bird", "bird"), ("ambrosia", "ambrosia_227")):
        new_dir = current / folder
        if not (new_dir / "report.json").exists():
            lines.append(f"{name}: hybrid report not available yet.")
            continue
        metadata = load(new_dir / "metadata.json")
        if metadata["status"] != "completed":
            lines.append(f"{name}: hybrid run not completed yet.")
            continue
        new = load(new_dir / "query_results.json")
        baseline = load(BASELINE / folder / "query_results.json")
        historical = load(HISTORICAL / f"{name}_query_results.json")
        if set(new) != set(baseline) or set(new) != set(historical):
            raise ValueError(f"{name}: question coverage differs; comparison refused")
        lines += ["", f"## {name} — {len(new)} questions", ""]
        if name == "bird":
            old_text = (HISTORICAL / "summary.txt").read_text(encoding="utf-8")
            old_accuracy = re.search(r"BIRD:\s*([\d.]+)%", old_text).group(1)
            baseline_accuracy = load(BASELINE / folder / "report.json")["overall_accuracy"]
            new_accuracy = load(new_dir / "report.json")["overall_accuracy"]
            label = ("GPT hybrid without FEAT_32/33" if metadata.get("feature_configuration") == "hybrid_no32_33"
                     else "GPT hybrid")
            lines += ["| Run | Accuracy |", "| --- | ---: |",
                      f"| GPT baseline | {baseline_accuracy:.2f}% |",
                      f"| {label} | {new_accuracy:.2f}% |",
                      f"| Historical best hybrid | {old_accuracy}% |"]
            original = CURRENT / "bird/report.json"
            if current != CURRENT.resolve() and original.exists():
                original_accuracy = load(original)["overall_accuracy"]
                lines.append(f"| Original GPT hybrid with FEAT_32/33 | {original_accuracy:.2f}% |")
        else:
            gold = {str(r["question_id"]): r["SQL"] == "ambiguous"
                    for r in load(ROOT / "data/ambrosia_large_available_227.json")}
            lines += ["| Run | Accuracy | Ambiguity F1 | Detected / 67 | False positives |",
                      "| --- | ---: | ---: | ---: | ---: |"]
            for label, predictions in (("GPT baseline", baseline), ("GPT hybrid", new),
                                       ("Historical best hybrid", historical)):
                accuracy, f1, tp, fp = ambiguity_metrics(predictions, gold)
                lines.append(f"| {label} | {accuracy:.2f}% | {f1:.2f}% | {tp} | {fp} |")
    result = "\n".join(lines) + "\n"
    print(result)
    if current.exists():
        (current / "comparison.md").write_text(result, encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=CURRENT,
                        help="Hybrid run root containing bird/ and/or ambrosia_227/")
    main(parser.parse_args().run_dir)
