#!/usr/bin/env python3
"""Offline checks for judge_filter.py that need no model and touch no corpus.

    python3 cluster/judge/test_judge_filter.py
Covers: strict-JSON extraction (fenced, prose-wrapped, flat objects),
verdict/defect normalisation, the 12-word reason cap, repair-only fix_hint,
schema-excerpt table matching, missing-table -> unclear warning, CTE handling,
the resume key, and the Wilson interval.
"""
from __future__ import annotations

import importlib.util
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("judge_filter", HERE / "judge_filter.py")
jf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jf)

checks: list[tuple[str, bool, object]] = []


def check(name: str, ok: bool, value: object = "") -> None:
    checks.append((name, bool(ok), value))


# --- strict JSON extraction -------------------------------------------------
fenced = ('Sure thing:\n```json\n[{"sample_id":"x","variant":1,"verdict":"accept",'
          '"failed":[],"defect":null,"reason":"ok","fix_hint":null}]\n```\nDone.')
got = jf.extract_json_array(fenced)
check("fenced array parsed", isinstance(got, list) and len(got) == 1 and got[0]["sample_id"] == "x", got)

prose = ('My analysis: the pair looks fine.\n[{"sample_id":"y","variant":2,"verdict":"reject",'
         '"failed":["G4"],"defect":"fanout_inflation","reason":"join inflates rows",'
         '"fix_hint":null}]\nHope that helps.')
got = jf.extract_json_array(prose)
check("array inside prose parsed", isinstance(got, list) and got[0]["verdict"] == "reject", got)

flat = jf.extract_json_array('{"sample_id":"z","verdict":"repair","failed":["G5"],'
                             '"defect":"hidden_limit","reason":"cap not named","fix_hint":"say 20"}')
check("flat objects fallback parsed", isinstance(flat, list) and flat[0]["verdict"] == "repair", flat)

check("garbage returns None", jf.extract_json_array("I cannot judge this pair.") is None)

# --- normalisation ----------------------------------------------------------
row = jf.normalise({"sample_id": "x", "verdict": "REPAIR", "failed": ["g1", "G9", "G3"],
                    "defect": "nonsense label", "fix_hint": "  say 20 rows  ",
                    "reason": "one two three four five six seven eight nine ten eleven twelve thirteen"})
check("repair kept", row["verdict"] == "repair", row)
check("criteria filtered to G1..G8", row["failed"] == ["G1", "G3"], row["failed"])
check("unknown defect mapped to other", row["defect"] == "other", row["defect"])
check("reason capped at 12 words", len(row["reason"].split()) == 12 and row.get("reason_truncated"),
      row["reason"])
check("fix_hint kept for repair", row["fix_hint"] == "say 20 rows", row["fix_hint"])

row = jf.normalise({"verdict": "accept", "failed": ["G1"], "defect": "wrong_entity",
                    "fix_hint": "nope", "reason": "fine"})
check("accept drops criteria, defect and hint",
      row == {"verdict": "accept", "failed": [], "defect": None, "reason": "fine", "fix_hint": None}, row)

row = jf.normalise({"verdict": "reject", "failed": ["G7"], "defect": "unanswerable",
                    "fix_hint": "nope", "reason": "no such rows"})
check("reject drops fix_hint", row["fix_hint"] is None and row["defect"] == "unanswerable", row)

check("unknown verdict rejected", jf.normalise({"verdict": "maybe"}) is None)
check("unclear allowed", jf.normalise({"verdict": "unclear", "reason": "no schema"})["defect"] is None)

# --- schema excerpt and table matching -------------------------------------
db = {"db_id": "d", "schema": "public", "tables": [
    {"name": "real", "columns": [{"name": "a", "type": "int", "not_null": True},
                                 {"name": "b", "type": "text"}],
     "primary_key": ["a"], "foreign_keys": [{"from_column": "b", "to_table": "other",
                                             "to_column": "id"}], "row_count_estimate": 7},
    {"name": "unrelated", "columns": [{"name": "c", "type": "int"}], "primary_key": [],
     "foreign_keys": [], "row_count_estimate": 3}]}

excerpt, picked = jf.schema_excerpt(db, 'SELECT "a" FROM "real" WHERE "b" = 1')
check("only used tables in excerpt", picked == ["real"], picked)
check("excerpt shows type, PK, FK and row estimate",
      all(token in excerpt for token in ("int NOT NULL", "[PK a]", "[FK b->other.id]", "[~7 rows]")),
      excerpt)

block, missing = jf.pair_block(1, {"sample_id": "s", "variant": 1, "db_id": "d",
                                  "question": "q", "evidence": "",
                                  "pg_gold_sql": 'SELECT * FROM "ghost" JOIN real ON 1=1'}, db)
check("missing table reported", missing == ["ghost"], missing)
check("missing table forces unclear warning in the prompt", 'verdict must be "unclear"' in block)

block, missing = jf.pair_block(1, {"sample_id": "s", "variant": 1, "db_id": "d", "question": "q",
                                  "evidence": "",
                                  "pg_gold_sql": 'WITH g AS (SELECT a FROM real) SELECT * FROM g'}, db)
check("CTE name is not mistaken for a missing table", missing == [], missing)

block, _ = jf.pair_block(1, {"sample_id": "s", "variant": 1, "db_id": "d", "question": "q",
                            "evidence": "", "pg_gold_sql": "SELECT a FROM real", "rows": 4,
                            "columns": 2}, db)
check("result preview line present when rows/columns known", "4 row(s) x 2 column(s)" in block)

# --- resume key ------------------------------------------------------------
pair = {"sample_id": "synth-1", "variant": 1, "db_id": "d", "question": "q", "evidence": "",
        "pg_gold_sql": "SELECT a FROM real"}
verdict_row = {"sample_id": "synth-1", "variant": 1, "db_id": "d",
               "pair_key": jf.key_of(pair), "verdict": "accept"}
check("verdict row is recognised on resume without the question text",
      jf.key_of(verdict_row) == jf.key_of(pair), jf.key_of(verdict_row))
check("a different question under the same id is a different pair",
      jf.key_of({**pair, "question": "other"}) != jf.key_of(pair))

# --- Wilson interval -------------------------------------------------------
low, high = jf.wilson(13, 30)
check("Wilson 13/30 brackets the 43.3% rate", low < 0.4333 < high and low > 0.27 and high < 0.61,
      (low, high))
check("Wilson 0/10 starts at zero", jf.wilson(0, 10)[0] == 0.0, jf.wilson(0, 10))
check("Wilson 0/0 is degenerate but safe", jf.wilson(0, 0) == (0.0, 0.0))

failed = [name for name, ok, _ in checks if not ok]
for name, ok, value in checks:
    print(f'{"PASS" if ok else "FAIL"}  {name}' + (f'  -> {value}' if not ok else ""))
print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
raise SystemExit(1 if failed else 0)
