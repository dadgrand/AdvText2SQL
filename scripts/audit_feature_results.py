"""Read-only audit of local large-set ablations and completed GPT runs."""

import ast
from collections import Counter
import json
from pathlib import Path
import re

from build_feature_dataset import parse_scores_from_log, parse_scores_from_query_results

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "research_results/runs/feature_audit_20261023"
IMPL = ROOT / "src/adv_text2sql/mcp_servers/text2sql_tool/src/text2sql_implementation.py"


def load(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def paired(scores, reference):
    common = set(scores) & set(reference)
    return {
        "reported_questions": len(scores), "reported_correct": sum(scores.values()),
        "common_questions": len(common), "common_correct": sum(scores[q] for q in common),
        "improved": sum(bool(scores[q]) and not bool(reference[q]) for q in common),
        "regressed": sum(not bool(scores[q]) and bool(reference[q]) for q in common),
    }


def call_profile(path, clarification_prefix, decomposition_prefix):
    groups = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        messages = row["messages"]
        first, last = messages[0]["content"], messages[-1]["content"]
        if last == "Classify the query.":
            name = "taxonomy"
        elif first.startswith(clarification_prefix):
            name = "clarification"
        elif first.startswith(decomposition_prefix):
            name = "decomposition"
        elif len(messages) == 2 and last.encode("unicode_escape").startswith(b"\\u041f\\u0440\\u043e\\u0432\\u0435\\u0440\\u044c"):
            name = "classic_ambiguity"
        else:
            name = "sql_generation"
        group = groups.setdefault(name, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
        group["calls"] += 1
        for key in ("prompt_tokens", "completion_tokens"):
            group[key] += row[key]
    return groups


def main():
    OUT.mkdir(exist_ok=True, parents=True)
    implementation = IMPL.read_text(encoding="utf-8")
    numbers = sorted({int(n) for n in re.findall(r"^FEAT_(\d+)\s*=", implementation, re.M)})
    descriptions = {}
    for line in (ROOT / "practices_doc.md").read_text(encoding="utf-8").splitlines():
        match = re.match(r"\|\s*(\d+)\s*\|", line)
        if match:
            descriptions.setdefault(int(match.group(1)), line.split("|")[2].strip())
    root = ROOT / "research_results/ablation_results_large"
    amb_gold = {str(row["question_id"]): row for row in load(ROOT / "data/ambrosia_large.json")}
    bird_baseline = parse_scores_from_log(root / "feat_baseline/bird.log")
    amb_baseline = parse_scores_from_query_results(root / "feat_baseline/ambrosia_query_results.json", amb_gold)
    result = {"historical_baseline": {"bird": paired(bird_baseline, bird_baseline),
                                      "ambrosia": paired(amb_baseline, amb_baseline)}, "features": {}}
    lines = ["# Historical large-set single-feature ablations", "",
             "Recomputed from saved results, not copied from small-set recommendations.",
             "Scores are observations, not causal estimates: runs were not repeated and dependencies matter.", "",
             "Baseline BIRD: 92/241 = 38.17%. Baseline Ambrosia: 127/227 = 55.95%.", "",
             "Ambrosia columns use the common 227-question baseline coverage; FEAT_6 originally reported 154/240.", "",
             "| Flag | Description | BIRD correct / 241 | Gains / losses | Ambrosia correct / 227 | Gains / losses |",
             "| --- | --- | ---: | ---: | ---: | ---: |"]
    for number in numbers:
        bird = parse_scores_from_log(root / f"feat_{number}_bird/bird.log")
        amb = parse_scores_from_query_results(root / f"feat_{number}_ambrosia/ambrosia_query_results.json", amb_gold)
        b, a = paired(bird, bird_baseline), paired(amb, amb_baseline)
        result["features"][f"FEAT_{number}"] = {"description": descriptions[number], "bird": b, "ambrosia": a}
        lines.append(f"| FEAT_{number} | {descriptions[number]} | {b['common_correct']} | "
                     f"+{b['improved']} / -{b['regressed']} | {a['common_correct']} | "
                     f"+{a['improved']} / -{a['regressed']} |")
    result["label_files"] = {}
    for name in ("feature_labels.json", "feature_labels_large.json", "feature_labels_ambrosia_large.json"):
        rows = load(ROOT / "data" / name)
        result["label_files"][name] = {
            "rows": len(rows), "feature_mask_sizes": dict(Counter(len(row.get("feature_scores", {})) for row in rows)),
            "baseline_correct": sum(row.get("baseline_correct", False) for row in rows),
            "best_combo_correct": sum(row.get("best_combo_correct", False) for row in rows),
        }
    prompts_path = IMPL.parent / "prompts.py"
    constants = {n.targets[0].id: ast.literal_eval(n.value) for n in ast.parse(prompts_path.read_text(encoding="utf-8")).body
                 if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and isinstance(n.value, ast.Constant)}
    result["gpt_call_profiles"] = {}
    for name, directory in {
        "bird_no32_33": "research_results/runs/gpt54mini_hybrid_bird_no32_33_full_20261023/bird",
        "ambrosia_hybrid": "research_results/runs/gpt54mini_hybrid_large_20261016/ambrosia_227",
    }.items():
        result["gpt_call_profiles"][name] = call_profile(
            ROOT / directory / "raw_responses.jsonl", constants["CLARIFICATION_PROMPT"].split("{")[0],
            constants["DECOMPOSITION_PROMPT"].split("{")[0])
    (OUT / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "mistral_large_ablation.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Audited {len(numbers)} flags; output: {OUT}")
    print(json.dumps(result["gpt_call_profiles"]))


if __name__ == "__main__":
    main()
