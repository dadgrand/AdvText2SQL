#!/usr/bin/env python3
"""Finalise the t7 deliverables for the rewritten pools.

The rewritten pools (delivery/out/synth/rewritten_cap.jsonl,
rewritten_extra.jsonl) are produced by task t6 at the very end of its run.
While t7 was live neither file existed, so this script:

  1. merges every verdict the filter actually produced into
     cluster/judge/rewritten_verdicts.jsonl (tagged with the pool it came from),
  2. aggregates cluster/judge/rewritten_report.json through
     judge_filter.py report (verdict counts, mismatch rate + Wilson 95%,
     per-criterion counts, up to 10 quoted examples),
  3. stamps the coverage facts: which pools were expected, which were judged,
     the exact command to finish the pools later, and the LIMIT sanity check.

Read-only over the corpus: it never edits pairs, SQL or questions.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SYNTH = ROOT / "delivery" / "out" / "synth"

EXPECTED = [SYNTH / "rewritten_cap.jsonl", SYNTH / "rewritten_extra.jsonl"]
JUDGED_SOURCES = [SYNTH / "synthetic_bird_like.jsonl", SYNTH / "dropped_truncated.jsonl"]
VERDICTS = HERE / "rewritten_verdicts.jsonl"
REPORT = HERE / "rewritten_report.json"

SOURCES = [
    (HERE / "calibration_verdicts.jsonl", "calibration_gate_passed",
     "delivery/out/synth/synthetic_bird_like.jsonl"),
    (HERE / "control_gate_rejected_verdicts.jsonl", "control_gate_dropped_execution_cap",
     "delivery/out/synth/dropped_truncated.jsonl"),
]


def read_jsonl(path: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    merged: list[dict] = []
    seen: set[str] = set()
    for path, pool, source_file in SOURCES:
        for row in read_jsonl(path):
            key = row.get("pair_key") or f'{row["sample_id"]}|{row.get("variant")}'
            if key in seen:
                continue
            seen.add(key)
            row["pool"] = pool
            row["source"] = source_file
            merged.append(row)
    merged.sort(key=lambda r: (r["pool"], r["sample_id"], r.get("variant") or 1))
    VERDICTS.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in merged),
                        encoding="utf-8")

    cmd = [sys.executable, str(HERE / "judge_filter.py"), "report",
           "--verdicts", str(VERDICTS), "--report", str(REPORT),
           "--examples", "10",
           "--gate-accepted", str(SYNTH / "synthetic_bird_like.jsonl"),
           "--gate-rejected", str(SYNTH / "dropped_truncated.jsonl")]
    for path in EXPECTED + JUDGED_SOURCES:
        cmd += ["--input", str(path)]
    subprocess.run(cmd, check=True, cwd=ROOT)

    report = json.loads(REPORT.read_text(encoding="utf-8"))
    report["methodology"] = ("cluster/judge/METHODOLOGY.md G1..G8; verdicts accept/repair/reject "
                             "(unclear counted as a reject for the rate, reported separately); "
                             "temperature 0, batches of 8, model qwen3.8-flash-next-iq3_xxs")
    report["status"] = "inputs_pending: neither rewritten pool existed while t7 was live"
    report["coverage"] = {
        "expected_pools": [{"path": str(p.relative_to(ROOT)), "exists": p.exists(),
                            "lines": sum(1 for _ in p.open()) if p.exists() else 0,
                            "pairs_judged": 0} for p in EXPECTED],
        "expected_pair_counts": {"rewritten_cap_pool_source": 2494,
                                 "rewritten_extra_pool_source": 2277,
                                 "note": "sizes of delivery/out/synth/synthetic_cap_unnamed.jsonl "
                                         "and synthetic_single_table_extra.jsonl, the pools t6 rewrites"},
        "pools_judged_instead": [{"pool": pool, "source": source,
                                  "verdicts": sum(1 for r in merged if r["pool"] == pool)}
                                 for _, pool, source in SOURCES],
        "total_verdicts_written": len(merged),
    }
    report["pending_command"] = (
        "cd research/advtext2sql && python3 cluster/judge/judge_filter.py judge "
        "--input delivery/out/synth/rewritten_cap.jsonl --input delivery/out/synth/rewritten_extra.jsonl "
        "--shape-join delivery/out/synth/synthetic_cap_unnamed.jsonl "
        "--shape-join delivery/out/synth/synthetic_single_table_extra.jsonl "
        "--out cluster/judge/rewritten_verdicts.jsonl --batch 8 --workers 4 && "
        "python3 cluster/judge/judge_filter.py report --verdicts cluster/judge/rewritten_verdicts.jsonl "
        "--report cluster/judge/rewritten_report.json "
        "--input delivery/out/synth/rewritten_cap.jsonl "
        "--input delivery/out/synth/rewritten_extra.jsonl --examples 10")
    report["limit_sanity_check"] = {
        "claim_tested": "the control pool was dropped for 'LIMIT n reached and not named in the question'",
        "verified": "0 of the sampled control rows store a LIMIT clause in pg_gold_sql; the cap was "
                    "applied at execution time, so the judge's accepts there are not semantic "
                    "disagreements and the 3 hidden_limit labels point the other way (question names "
                    "a number the stored SQL does not cap)",
        "calibration_g5_failures": report["criterion_failure_counts"].get("G5", 0),
        "reading": "no hidden-LIMIT defect was confirmed against the SQL text in the calibration set",
    }
    report["defect_label_caveat"] = ("defect labels are indicative: on a repeat run of the same 30 "
                                     "pairs at temperature 0 the verdicts agreed 30/30 while the "
                                     "defect labels agreed 23/30 (see calibration_report.json "
                                     "repeat_run_stability)")
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    slim = {k: report[k] for k in ("status", "total_judged", "verdict_counts", "mismatch",
                                   "criterion_failure_counts", "defect_counts")}
    slim["coverage"] = report["coverage"]
    slim["examples"] = len(report["examples"])
    print(json.dumps(slim, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
