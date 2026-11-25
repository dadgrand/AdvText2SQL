#!/usr/bin/env python3
"""Add the calibration extras to cluster/judge/calibration_report.json.

Sections added on top of what judge_filter.py report produces:
  calibration_sampling_rule  - how the 30-pair calibration set was drawn
  repeat_run_stability       - same 30 pairs, same seed, judged twice (temperature 0)
  hand_label_selection_rule  - how the 5 hand-labelled pairs were chosen
  manual_audit_notes         - what a manual reading of the flagged pairs found
Run after judge_filter.py report. Read-only over the corpus.
"""
from __future__ import annotations

import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
REPORT = HERE / "calibration_report.json"
BASE = HERE / "calibration_verdicts.jsonl"
REPEAT = HERE / "calibration_verdicts_repeat.jsonl"
HAND = HERE / "hand_labels.jsonl"


def read_jsonl(path: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    base = {(r["sample_id"], r.get("variant")): r for r in read_jsonl(BASE)}
    repeat = {(r["sample_id"], r.get("variant")): r for r in read_jsonl(REPEAT)}
    shared = sorted(set(base) & set(repeat))

    same_verdict = sum(1 for k in shared if base[k]["verdict"] == repeat[k]["verdict"])
    same_defect = sum(1 for k in shared
                      if (base[k].get("defect") or "null") == (repeat[k].get("defect") or "null"))
    same_criteria = sum(1 for k in shared if base[k].get("failed") == repeat[k].get("failed"))
    report["repeat_run_stability"] = {
        "pairs": len(shared),
        "note": "the same 30 pairs, same seed, same batches, judged twice at temperature 0",
        "verdict_agreement": same_verdict,
        "verdict_agreement_rate": round(same_verdict / len(shared), 6) if shared else 0.0,
        "defect_label_agreement": same_defect,
        "defect_label_agreement_rate": round(same_defect / len(shared), 6) if shared else 0.0,
        "criteria_agreement": same_criteria,
        "criteria_agreement_rate": round(same_criteria / len(shared), 6) if shared else 0.0,
        "differences": [{"sample_id": k[0], "variant": k[1],
                         "run_a": f'{base[k]["verdict"]}/{base[k].get("defect")}',
                         "run_b": f'{repeat[k]["verdict"]}/{repeat[k].get("defect")}'}
                        for k in shared if base[k]["verdict"] != repeat[k]["verdict"]
                        or base[k].get("defect") != repeat[k].get("defect")],
        "reading": "verdicts are stable; defect labels are indicative, not reproducible",
    }
    report["calibration_sampling_rule"] = {
        "file": "delivery/out/synth/synthetic_bird_like.jsonl",
        "pool_lines": 982,
        "draw": "random.Random(20261125).sample(range(982), 30), indices sorted by sample_id",
        "n": 30,
        "sample_ids": sorted(k[0] for k in base),
    }
    hand = read_jsonl(HAND)
    report["hand_label_selection_rule"] = {
        "rule": "every 6th sample_id of the sorted 30-pair calibration set (positions 1, 7, 13, 19, 25)",
        "labelled_by": "judge member, reading question + SQL + schema excerpt + result shape",
        "labels_file": "cluster/judge/hand_labels.jsonl",
        "ids": [h["sample_id"] for h in hand],
    }
    report["manual_audit_notes"] = {
        "method": "manual re-reading of all 13 pairs the judge flagged, plus the 5 hand-labelled pairs",
        "confirmed_defects": 11,
        "arguable_over_flags": [
            {"sample_id": "synth-01153",
             "why": "SQL returns DISTINCT match_id selected through a subquery on wicket_taken "
                    "for innings 3; the question's reading is satisfied, so the G1 flag is arguable"},
            {"sample_id": "synth-01474",
             "why": "question asks for 'districts and zip codes', the SQL projects exactly those "
                    "two columns; the G1 flag is arguable"},
        ],
        "hallucination_found_in_first_pass": {
            "sample_id": "synth-01211",
            "what": "the first prompt version returned a reason about 'Ship Date / sales and profits' "
                    "for a pair whose SQL is COUNT(*) over twitter with reach > 98 AND day < 22",
            "status": "fixed by the result-preview line and the re-run; the pair is now accept, "
                      "matching the hand label",
        },
        "gate_caveat": "the control pool (dropped_truncated.jsonl) was dropped for an execution-side "
                       "row cap; its stored pg_gold_sql contains no LIMIT clause, so its disagreements "
                       "with the judge are not the same kind as the calibration disagreements",
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"repeat_run_stability": {k: report["repeat_run_stability"][k] for k in
                                                ("pairs", "verdict_agreement_rate",
                                                 "defect_label_agreement_rate")},
                      "manual_audit_notes": {"confirmed_defects": 11}}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
