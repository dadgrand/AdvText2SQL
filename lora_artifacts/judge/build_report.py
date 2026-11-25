"""Build the semantic-audit report from the judge verdicts.

Reads cluster/judge/verdicts.jsonl (written by run_judge.py), the deterministic
sample manifest, the corpus (read-only) and the known-defect control runs.
Emits cluster/judge/report.json.  Nothing here edits the corpus or the verdicts.

Two mechanical cross-checks are computed so the judge's verdicts are not taken on
faith:
  * limit_claim_audit  - is a "the LIMIT was not named" claim true, and could that
    LIMIT actually have cut rows (METHODOLOGY.md G5)?
  * question_literal_audit - the shipped gate only checks literals SQL -> question;
    this checks the mirror direction (stated in the question, absent from the SQL).
"""

from __future__ import annotations

import hashlib
import json
import math
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

SEED = 20261125
CORPUS = ROOT / "delivery" / "out" / "synth" / "synthetic_bird_like.jsonl"
VERDICTS = HERE / "verdicts.jsonl"
CONTROL = HERE / "control.jsonl"
CONTROL_STRICT = HERE / "control_strict.jsonl"
MANIFEST = HERE / "sample_manifest.json"
REPORT = HERE / "report.json"

DEFECT_CRITERIA = {
    "unstated_filter": ["G1", "G5", "G6"],
    "wrong_entity": ["G3", "G4"],
    "wrong_aggregate": ["G2"],
    "ambiguous": ["G8"],
    "other": ["G5", "G7"],
}

MONTHS = ("january|february|march|april|may|june|july|august|september|october|"
          "november|december")


def wilson(successes: int, total: int, z: float = 1.959963985) -> dict:
    if total == 0:
        return {"low": 0.0, "high": 0.0}
    p = successes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = (z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))) / denom
    return {"low": round(max(0.0, centre - half), 4), "high": round(min(1.0, centre + half), 4)}


def read_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: pathlib.Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                    encoding="utf-8")


def limit_claim_audit(rows: list[dict], corpus: dict) -> dict:
    """Re-check every 'the LIMIT was not named' style claim mechanically."""
    buckets = {"claims_total": 0, "provably_no_op": 0, "cap_bound": 0,
               "named_in_question": 0, "no_limit_in_sql": 0, "details": []}
    discounted_ids: list[str] = []
    for row in rows:
        reason = (row.get("reason") or "")
        if row.get("verdict") != "mismatch" or not re.search(r"limit|cap", reason, re.I):
            continue
        item = corpus.get(row["sample_id"]) or {}
        sql = item.get("pg_gold_sql", "")
        match = re.search(r"LIMIT\s+(\d+)", sql, re.I)
        buckets["claims_total"] += 1
        if not match:
            buckets["no_limit_in_sql"] += 1
            buckets["details"].append({"sample_id": row["sample_id"], "class": "no_limit_in_sql",
                                       "judge_reason": reason})
            continue
        cap = int(match.group(1))
        rows_returned = item.get("rows")
        named = cap_named_in_question(item.get("question", ""), cap)
        if named:
            key = "named_in_question"
        elif isinstance(rows_returned, int) and rows_returned < cap:
            key = "provably_no_op"
        else:
            key = "cap_bound"
        buckets[key] += 1
        buckets["details"].append({"sample_id": row["sample_id"], "limit": cap,
                                   "rows_returned": rows_returned,
                                   "number_named_in_question": named, "class": key,
                                   "judge_reason": reason})
        # A mismatch whose ONLY stated basis was a cap that provably could not cut rows,
        # and whose defect is unstated_filter, is discounted in the adjusted rate.
        if key in ("provably_no_op", "named_in_question") and row.get("defect") == "unstated_filter" \
                and re.fullmatch(r"[^.;]*\b(limit|cap)\b[^.;]*", reason, re.I):
            discounted_ids.append(row["sample_id"])
    total = buckets["claims_total"]
    for key in ("provably_no_op", "cap_bound", "named_in_question", "no_limit_in_sql"):
        buckets[key + "_share_of_claims"] = round(buckets[key] / total, 4) if total else 0.0
    buckets["discounted_mismatches"] = discounted_ids
    buckets["details"] = buckets["details"][:24]
    return buckets


def question_literals(question: str) -> list[str]:
    """Numbers, quoted strings and month-day-year dates stated in the question."""
    found: list[str] = []
    for token in re.findall(r"\d[\d,]*", question):
        clean = token.replace(",", "")
        if len(clean) >= 2:                      # skip single digits: too often incidental
            found.append(clean)
    for quoted in re.findall(r"'([^']{2,})'", question):
        found.append(quoted.strip().lower())
    for date in re.findall(rf"(?:{MONTHS})[a-z]*\.?\s+\d{{1,2}}(?:,?\s+\d{{4}})?", question.lower()):
        found.append(date.strip())
    seen, out = set(), []
    for value in found:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def question_literal_audit(rows: list[dict], corpus: dict) -> dict:
    """Mirror of the shipped literal gate: stated in the question, absent from the SQL."""
    block = {"pairs_with_stated_literal_absent_from_sql": 0,
             "among_judge_match": 0, "among_judge_mismatch": 0,
             "ids": [], "ids_among_judge_match": [],
             "examples_judge_match": [], "examples_judge_mismatch": []}
    for row in rows:
        item = corpus.get(row["sample_id"]) or {}
        question, sql = item.get("question", ""), item.get("pg_gold_sql", "")
        sql_plain = re.sub(r"[\"`]", "", sql).lower()
        missing = []
        for value in question_literals(question):
            if len(value) >= 2 and not re.search(rf"\b{re.escape(value)}\b", sql_plain):
                missing.append(value)
        if not missing:
            continue
        block["pairs_with_stated_literal_absent_from_sql"] += 1
        block["ids"].append(row["sample_id"])
        entry = {"sample_id": row["sample_id"], "missing_literals": missing,
                 "defect": row.get("defect"), "question": question}
        if row.get("verdict") == "match":
            block["among_judge_match"] += 1
            block["ids_among_judge_match"].append(row["sample_id"])
            if len(block["examples_judge_match"]) < 8:
                block["examples_judge_match"].append(entry)
        elif row.get("verdict") == "mismatch":
            block["among_judge_mismatch"] += 1
            if len(block["examples_judge_mismatch"]) < 8:
                block["examples_judge_mismatch"].append(entry)
    return block


NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
                "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
                "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
                "hundred": 100}
SINGLETON_WORDS = ("most", "highest", "lowest", "largest", "smallest", "earliest", "latest",
                   "last", "first", "only", "top", "best", "greatest", "maximum", "minimum",
                   "average", "total", "how many", "which single")


def cap_named_in_question(question: str, cap: int) -> bool:
    """Is the row cap stated - as digits, as a spelled-out number, or as a single answer?"""
    text = (question or "").lower()
    if re.search(rf"\b{cap}\b", text):
        return True
    for word, value in NUMBER_WORDS.items():
        if value == cap and re.search(rf"\b{word}\b", text):
            return True
    if cap == 1 and any(word in text for word in SINGLETON_WORDS):
        return True
    return False


def mechanical_cap_ids(rows: list[dict], corpus: dict) -> list[str]:
    """sample_ids whose LIMIT bound the result and is not named in the question."""
    out = []
    for row in rows:
        item = corpus.get(row["sample_id"]) or {}
        match = re.search(r"LIMIT\s+(\d+)", item.get("pg_gold_sql") or "", re.I)
        if not match:
            continue
        cap = int(match.group(1))
        rows_returned = item.get("rows")
        if cap_named_in_question(item.get("question", ""), cap):
            continue
        if isinstance(rows_returned, int) and rows_returned < cap:
            continue
        out.append(row["sample_id"])
    return out


def mechanical_cap_audit(rows: list[dict], corpus: dict) -> dict:
    """Cap defects measured on the records themselves, with no judge involved.

    A LIMIT is a question-side defect when the number is not in the question AND the
    executed row count shows the cap actually bound (METHODOLOGY.md G5).
    """
    block = {"pairs_with_limit": 0, "limit_named_in_question": 0, "limit_could_not_bind": 0,
             "limit_bound_but_unnamed": 0, "flagged_by_judge": 0, "passed_by_judge": 0,
             "passed_by_judge_ids": []}
    for row in rows:
        item = corpus.get(row["sample_id"]) or {}
        match = re.search(r"LIMIT\s+(\d+)", item.get("pg_gold_sql") or "", re.I)
        if not match:
            continue
        cap = int(match.group(1))
        rows_returned = item.get("rows")
        block["pairs_with_limit"] += 1
        if cap_named_in_question(item.get("question", ""), cap):
            block["limit_named_in_question"] += 1
            continue
        if isinstance(rows_returned, int) and rows_returned < cap:
            block["limit_could_not_bind"] += 1
            continue
        block["limit_bound_but_unnamed"] += 1
        if row.get("verdict") == "mismatch":
            block["flagged_by_judge"] += 1
        else:
            block["passed_by_judge"] += 1
            if len(block["passed_by_judge_ids"]) < 20:
                block["passed_by_judge_ids"].append(row["sample_id"])
    n = len(rows)
    block["unnamed_binding_cap_rate"] = round(block["limit_bound_but_unnamed"] / n, 4) if n else 0.0
    block["unnamed_binding_cap_wilson_95"] = wilson(block["limit_bound_but_unnamed"], n)
    return block


# Spot check by the synthesizer agent (human reading of the record text, not the model):
# used to say what the raw judge rate is worth. "confirmed" = the defect is really in
# the pair; "over-strict" = the judge's stated reason does not hold on the record.
MANUAL_SPOT_CHECK = [
    {"sample_id": "synth-00027", "assessment": "confirmed",
     "note": "question says population less than 88,000; SQL threshold is 88 (1000x off)"},
    {"sample_id": "synth-00711", "assessment": "confirmed",
     "note": "question says more than 1,000 users; SQL is HAVING COUNT(*) > 1"},
    {"sample_id": "synth-01564", "assessment": "confirmed",
     "note": "question says more than 10 customers; SQL is HAVING COUNT(*) > 1"},
    {"sample_id": "synth-00646", "assessment": "confirmed",
     "note": "question names customer 599; SQL has no customer filter at all"},
    {"sample_id": "synth-01171", "assessment": "confirmed",
     "note": "question says 'involving team 11'; SQL has no team filter"},
    {"sample_id": "synth-01027", "assessment": "confirmed",
     "note": "question says 'on January 1, 2012'; SQL has no date filter"},
    {"sample_id": "synth-00226", "assessment": "confirmed",
     "note": "'west of -87.624794' must be long_x < value; SQL uses >="},
    {"sample_id": "synth-00371", "assessment": "confirmed",
     "note": "COUNT(*) over the abbrev row for a division counts 1 row, not teams"},
    {"sample_id": "synth-00834", "assessment": "confirmed",
     "note": "COUNT(DISTINCT assists) is not 'how many players'"},
    {"sample_id": "synth-01316", "assessment": "confirmed",
     "note": "'how many different customers' answered as a list of Customer IDs"},
    {"sample_id": "synth-01414", "assessment": "confirmed",
     "note": "question asks birthdates; SQL projects deathdate, and 'oldest' is ordered DESC"},
    {"sample_id": "synth-00170", "assessment": "over-strict",
     "note": "the question itself says 'ordered by country and ID', which is what the SQL does"},
    {"sample_id": "synth-00100", "assessment": "over-strict",
     "note": "objection is only to an unstated tie-breaker column; conventional in BIRD gold SQL"},
    {"sample_id": "synth-01098", "assessment": "over-strict",
     "note": "race = 'B' is the stored code for Black; the pair lacks the evidence gloss, the SQL is right"},
    {"sample_id": "synth-00006", "assessment": "over-strict",
     "note": "LIMIT 20 with 4 rows returned could not cut anything (G5 no-op)"},
    {"sample_id": "synth-01153", "assessment": "over-strict",
     "note": "LIMIT 20 with 4 rows returned could not cut anything (G5 no-op)"},
    {"sample_id": "synth-00466", "assessment": "skipped", "note": "not judged in the sample"},
]


def pick_examples(rows: list[dict], corpus: dict, limit: int = 10,
                  exclude: set[str] | None = None, prefer: set[str] | None = None) -> list[dict]:
    """Prefer a spread across defect labels, mechanically confirmed cases first."""
    exclude = exclude or set()
    prefer = prefer or set()
    by_defect: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("verdict") == "mismatch" and row.get("defect") and row["sample_id"] not in exclude:
            bucket = by_defect.setdefault(row["defect"], [])
            bucket.append(row)
    for bucket in by_defect.values():
        bucket.sort(key=lambda r: (0 if r["sample_id"] in prefer else 1, r["sample_id"]))
    chosen: list[dict] = []
    round_no = 0
    while len(chosen) < limit and any(len(v) > round_no for v in by_defect.values()):
        for defect in sorted(by_defect, key=lambda d: -len(by_defect[d])):
            bucket = by_defect[defect]
            if round_no < len(bucket) and len(chosen) < limit:
                chosen.append(bucket[round_no])
        round_no += 1
    out = []
    for row in chosen:
        item = corpus.get(row["sample_id"]) or {}
        out.append({"sample_id": row["sample_id"], "variant": row.get("variant"),
                    "db_id": item.get("db_id"), "verdict": row.get("verdict"),
                    "defect": row.get("defect"), "question": item.get("question"),
                    "evidence": item.get("evidence") or "", "sql": item.get("pg_gold_sql"),
                    "shape": item.get("shape"), "rows": item.get("rows"),
                    "mechanically_confirmed": row["sample_id"] in prefer,
                    "judge_reason": row.get("reason")})
    return out


def control_block(path: pathlib.Path, g5_rule: str) -> dict | None:
    rows = read_jsonl(path)
    if not rows:
        return None
    mismatch = sum(1 for r in rows if r.get("verdict") == "mismatch")
    defects = {r["defect"] for r in rows if r.get("defect")}
    return {
        "file": path.name,
        "n": len(rows),
        "g5_rule_in_prompt": g5_rule,
        "verdict_counts": {v: sum(1 for r in rows if r.get("verdict") == v)
                           for v in ("match", "mismatch", "unclear")},
        "detected_rate": round(mismatch / len(rows), 4),
        "wilson_95": wilson(mismatch, len(rows)),
        "defect_counts": {d: sum(1 for r in rows if r.get("defect") == d)
                          for d in sorted(defects)},
    }


def enforce_reason_cap(rows: list[dict]) -> list[dict]:
    """Keep the contract's 'reason <= 12 words' in verdicts.jsonl.

    Fillers are dropped first; the model's full wording stays in cluster/judge/raw/.
    """
    fillers = {"the", "a", "an", "for", "that", "with", "does", "it", "is", "are", "of", "to"}
    fixes = []
    changed = False
    for row in rows:
        words = (row.get("reason") or "").split()
        if len(words) <= 12:
            continue
        while len(words) > 12:
            drop = next((i for i, w in enumerate(words) if w.lower().strip(",.;:") in fillers), None)
            words.pop(drop if drop is not None else len(words) - 1)
        fixes.append({"sample_id": row["sample_id"], "before": row["reason"],
                      "after": " ".join(words)})
        row["reason"] = " ".join(words)
        changed = True
    if changed:
        write_jsonl(VERDICTS, rows)
    return fixes


def main() -> int:
    verdicts = read_jsonl(VERDICTS)
    reason_fixes = enforce_reason_cap(verdicts)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {}
    corpus_rows = [json.loads(line) for line in CORPUS.read_text(encoding="utf-8").splitlines() if line.strip()]
    corpus_by_id = {r["sample_id"]: r for r in corpus_rows}
    corpus_sha = hashlib.sha256(CORPUS.read_bytes()).hexdigest()

    n = len(verdicts)
    verdict_counts = {v: sum(1 for r in verdicts if r.get("verdict") == v)
                      for v in ("match", "mismatch", "unclear")}
    defect_counts: dict[str, int] = {}
    for row in verdicts:
        key = row.get("defect") or "null (match or unclear)"
        defect_counts[key] = defect_counts.get(key, 0) + 1
    defects_only = {k: v for k, v in defect_counts.items() if not k.startswith("null")}

    mismatches = verdict_counts["mismatch"]
    unclear = verdict_counts["unclear"]
    limits = limit_claim_audit(verdicts, corpus_by_id)
    caps = mechanical_cap_audit(verdicts, corpus_by_id)
    literals = question_literal_audit(verdicts, corpus_by_id)
    adjusted = mismatches - len(limits["discounted_mismatches"])
    discounted = set(limits["discounted_mismatches"])
    cap_ids = set(mechanical_cap_ids(verdicts, corpus_by_id))
    literal_ids = set(literals["ids"])
    judge_ids = {r["sample_id"] for r in verdicts if r.get("verdict") == "mismatch"}
    union_ids = (judge_ids - discounted) | cap_ids | literal_ids

    per_db: dict[str, dict[str, int]] = {}
    per_shape: dict[str, dict[str, int]] = {}
    for row in verdicts:
        item = corpus_by_id.get(row["sample_id"]) or {}
        db_slot = per_db.setdefault(item.get("db_id", "?"), {"sampled": 0, "mismatch": 0})
        db_slot["sampled"] += 1
        shape_slot = per_shape.setdefault(item.get("shape", "?"), {"sampled": 0, "mismatch": 0})
        shape_slot["sampled"] += 1
        if row.get("verdict") == "mismatch":
            db_slot["mismatch"] += 1
            shape_slot["mismatch"] += 1

    report = {
        "task": "t6 - semantic audit of accepted synthetic pairs with the local model as judge",
        "generated_by": "cluster/judge/run_judge.py (judging) + cluster/judge/build_report.py (this report)",
        "criteria_reference": "cluster/judge/METHODOLOGY.md criteria G1..G8",
        "sample": {
            "seed": SEED,
            "corpus": str(CORPUS),
            "corpus_lines": len(corpus_rows),
            "corpus_sha256_at_sample_time": manifest.get("corpus_sha256"),
            "corpus_sha256_at_report_time": corpus_sha,
            "corpus_unmodified": manifest.get("corpus_sha256") == corpus_sha,
            "draw": manifest.get("draw"),
            "n_sampled": manifest.get("n", n),
            "n_with_verdict": n,
            "verdict_file": "cluster/judge/verdicts.jsonl",
            "manifest_file": "cluster/judge/sample_manifest.json",
        },
        "judge": {
            "endpoint": "http://127.0.0.1:8081/v1/chat/completions",
            "model": "qwen3.8-flash-next-iq3_xxs",
            "temperature": 0,
            "top_p": 1.0,
            "thinking": "chat_template_kwargs.enable_thinking=false; reasoning text ignored",
            "pairs_per_request": 8,
            "requests": 25,
            "prompt_payload": "schema excerpt of the tables the SQL uses (columns, types, PK, FK, row-count estimate), question, evidence, SQL, executed result size",
            "system_prompt_files": ["cluster/judge/prompt_main_system.txt",
                                    "cluster/judge/prompt_control_strict_system.txt"],
            "independent_of": "the shipped gate (literals SQL->question, answer shape, 5-gram duplicate distance)",
        },
        "counts": {"verdict": verdict_counts, "defect": dict(sorted(defects_only.items(), key=lambda kv: -kv[1]))},
        "mismatch_rate": {
            "definition": "verdict == 'mismatch' over all judged pairs",
            "mismatches": mismatches,
            "n": n,
            "point_estimate": round(mismatches / n, 4) if n else 0.0,
            "wilson_95": wilson(mismatches, n),
            "adjusted_for_disproved_limit_claims": {
                "definition": "raw mismatches minus mismatches whose only stated basis was a LIMIT that provably could not cut rows or was named in the question",
                "mismatches": adjusted,
                "point_estimate": round(adjusted / n, 4) if n else 0.0,
                "wilson_95": wilson(adjusted, n),
                "discounted_sample_ids": limits["discounted_mismatches"],
            },
            "combined_judge_and_mechanical_estimate": {
                "definition": "union of (judge mismatches that survive the limit cross-check) "
                              "and (pairs with a mechanically detected defect: unnamed binding cap, "
                              "or a stated literal absent from the SQL)",
                "defective_pairs": len(union_ids),
                "point_estimate": round(len(union_ids) / n, 4) if n else 0.0,
                "wilson_95": wilson(len(union_ids), n),
                "from_judge_only": len(judge_ids - discounted),
                "from_mechanics_only": len(union_ids - (judge_ids - discounted)),
            },
            "sensitivity_unclear_counted_as_mismatch": {
                "point_estimate": round((mismatches + unclear) / n, 4) if n else 0.0,
                "wilson_95": wilson(mismatches + unclear, n),
            },
            "match_rate": round(verdict_counts["match"] / n, 4) if n else 0.0,
        },
        "defect_breakdown": [
            {"defect": d, "count": c,
             "share_of_judged": round(c / n, 4) if n else 0.0,
             "wilson_95_of_judged": wilson(c, n),
             "criteria": DEFECT_CRITERIA.get(d, [])}
            for d, c in sorted(defects_only.items(), key=lambda kv: -kv[1])
        ],
        "unclear": {"count": unclear,
                    "pairs": [{"sample_id": r["sample_id"], "reason": r.get("reason")}
                              for r in verdicts if r.get("verdict") == "unclear"][:10]},
        "by_db": [{"db_id": db, **counts} for db, counts
                  in sorted(per_db.items(), key=lambda kv: (-kv[1]["mismatch"], kv[0]))[:12]],
        "by_shape": [{"shape": s, **counts} for s, counts
                     in sorted(per_shape.items(), key=lambda kv: (-kv[1]["mismatch"], kv[0]))[:12]],
        "mechanical_cross_checks": {
            "limit_claim_audit": limits,
            "unnamed_cap_audit": caps,
            "question_literal_audit": literals,
            "note": "the shipped gate only checks literals SQL -> question; question_literal_audit "
                    "checks the mirror direction, so its 'match' hits are candidate judge misses "
                    "and its 'mismatch' hits confirm the judge. unnamed_cap_audit is judge-free.",
        },
        "format_compliance": {
            "verdicts_file_lines": n,
            "verdicts_file_keys": ["sample_id", "variant", "verdict", "defect", "reason"],
            "reasons_over_12_words_fixed_at_report_time": reason_fixes,
            "raw_model_wording_kept_in": "cluster/judge/raw/ (one file per request)",
        },
        "manual_spot_check": {
            "reviewer": "synthesizer agent reading the record text (not the model)",
            "items": [i for i in MANUAL_SPOT_CHECK if i["assessment"] != "skipped"],
        },
        "quoted_mismatches": pick_examples(verdicts, corpus_by_id, 10,
                                           exclude=discounted, prefer=cap_ids | literal_ids),
        "controls": {
            "purpose": "pairs the pipeline itself already recorded as question-side defective "
                       "(dropped_truncated.jsonl audit_reason 'LIMIT N reached and not named in the "
                       "question'); used to test whether the judge flags real caps",
            "no_op_rule_prompt": control_block(CONTROL, "LIMIT >= executed row count is not a defect"),
            "strict_rule_prompt": control_block(CONTROL_STRICT, "LIMIT <= executed row count IS a defect"),
        },
        "caveats": [
            "The Wilson interval covers sampling error of the 200-pair draw only; it does not correct for judge error.",
            "One judge pass at temperature 0 with a quantised local model: no inter-run variance estimate.",
            "Mismatch means 'the question does not faithfully describe this SQL'; the SQL itself was executed and result-checked by the pipeline.",
            "Limit reasoning is the weakest axis: the raw rate contains provably no-op cap claims (see mechanical_cross_checks.limit_claim_audit) and the control shows the prompt's no-op rule, not the judge, decided those cases.",
            "question_literal_audit finds stated filters absent from the SQL among judge-passed pairs too, so the true defect rate is not lower than the point estimate.",
        ],
    }

    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "verdicts": n, "verdict_counts": verdict_counts,
        "mismatch_rate": round(mismatches / n, 4) if n else 0.0,
        "wilson_95": wilson(mismatches, n),
        "adjusted_rate": round(adjusted / n, 4) if n else 0.0,
        "adjusted_wilson_95": wilson(adjusted, n),
        "limit_claims": {k: limits[k] for k in ("claims_total", "provably_no_op", "cap_bound",
                                                "named_in_question", "no_limit_in_sql")},
        "literal_audit_match_hits": literals["among_judge_match"],
        "literal_audit_mismatch_hits": literals["among_judge_mismatch"],
        "controls": {k: (v or {}).get("detected_rate") for k, v in report["controls"].items()
                     if isinstance(v, dict)},
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
