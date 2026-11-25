#!/usr/bin/env python3
"""Audit and clean the synthetic rows that the training arms actually consume.

Scope (fixed): rows of datasets/native_pg/native_pg_train.jsonl whose sidecar says
source_pool == "synthetic" - 2,691 rows. Not the stale 982-row
delivery/out/synth/synthetic_bird_like.jsonl, not the dev/OOD synthetic rows.

Four checks per row, in this order:

  1. LIMIT_CHECK (code). If the SQL caps rows, the number must be honest:
     named in the question (digits or spelled out), or the cap is provably a
     no-op (verification.result_rows < cap), or cap == 1 with singular/superlative
     wording (counted separately, never merged into the pass count silently).
     The model is NOT trusted with this axis.
  2. LITERAL_CHECK (code), both directions:
     every string literal and every meaningful number in the SQL must appear in
     the question, and every number or quoted string in the question must appear
     in the SQL. LIMIT/OFFSET values are handled by check 1, NULLIF(x, 0) is not a
     question value.
  3. SEMANTIC (G1..G8 via the local judge, strict accept/repair/reject) plus a
     grounding check: every aggregate/cap/literal/filter claim in the judge's own
     reason is verified against the SQL text. A claim that the SQL contradicts is
     marked refuted and excluded from the verified mismatch rate.
  4. EVIDENCE_GAP (code): the row depends on a code or abbreviation the evidence
     never glosses. Counted separately; such rows are not silently accepted.

Stages:
    corpus_filter.py mechanical   checks 1, 2, 4 on every row   -> corpus_mechanical.jsonl
    corpus_filter.py judge        check 3 on every row          -> corpus_judge.jsonl (idempotent)
    corpus_filter.py finalize     merge + grounding             -> corpus_verdicts.jsonl,
                                                                    corpus_clean.jsonl,
                                                                    corpus_report.json
    corpus_filter.py all

Nothing here edits the corpus, the SQL or the question text.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import json
import pathlib
import re
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent                                    # research/advtext2sql
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("judge_filter", HERE / "judge_filter.py")
jf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(jf)

DATASET = ROOT / "datasets" / "native_pg"
TRAIN = DATASET / "native_pg_train.jsonl"
SIDECAR = DATASET / "native_pg_sidecar.jsonl"
SCHEMA_PATH = jf.SCHEMA_PATH

MECHANICAL = HERE / "corpus_mechanical.jsonl"
JUDGE_OUT = HERE / "corpus_judge.jsonl"
JUDGE_RAW = HERE / "raw_corpus_judge"
VERDICTS = HERE / "corpus_verdicts.jsonl"
CLEAN = HERE / "corpus_clean.jsonl"
REPORT = HERE / "corpus_report.json"

BATCH = 8
WORKERS = 4
SEED = 20261125

# ---------------------------------------------------------------- corpus scope

def load_scope() -> list[dict]:
    sidecar = {}
    for line in SIDECAR.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            sidecar[row["sample_id"]] = row
    rows = []
    for line in TRAIN.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        meta = sidecar.get(row["sample_id"], {})
        if meta.get("source_pool") != "synthetic":
            continue
        row["_sidecar"] = meta
        rows.append(row)
    return rows


def variant_of(sample_id: str) -> int:
    if "#" in sample_id:
        tail = sample_id.rsplit("#", 1)[1]
        return int(tail) if tail.isdigit() else 1
    return 1


def as_pair(row: dict) -> dict:
    return {"sample_id": row["sample_id"], "variant": variant_of(row["sample_id"]),
            "db_id": row["db_id"], "question": row["question"],
            "evidence": row.get("evidence", ""), "pg_gold_sql": row["pg_gold_sql"]}


# ------------------------------------------------------------ shared extractors

WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
            "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
            "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
            "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
            "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
WORD_NUM_RE = re.compile(r"\b(" + "|".join(WORD_NUM) + r")\b", re.I)
NUM_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
SINGLE_QUOTED_RE = re.compile(r"'([^']*)'")
QUESTION_QUOTED_RE = re.compile(r"'([^']+)'|\"([^\"]+)\"")

SUPERLATIVE_CUES = re.compile(
    r"\b(first|earliest|latest|last|only|single|one|top|lowest|highest|smallest|largest|"
    r"minimum|maximum|youngest|oldest|newest|cheapest|dearest|fastest|slowest)\b", re.I)

AGG_TOKENS = {"COUNT": r"COUNT\s*\(", "SUM": r"\bSUM\s*\(", "AVG": r"\b(AVG|AVERAGE|MEAN)\s*\(",
              "MIN": r"\bMIN\s*\(", "MAX": r"\bMAX\s*\(", "DISTINCT": r"\bDISTINCT\b"}
AGG_WORDS = {"COUNT": r"\bcount|number of|how many\b", "SUM": r"\bsum|total\b",
             "AVG": r"\bavg|average|mean\b", "MIN": r"\bmin|smallest|lowest|least|earliest\b",
             "MAX": r"\bmax|largest|highest|greatest|most\b", "DISTINCT": r"\bdistinct|unique\b"}


def strip_strings(sql: str) -> str:
    return SINGLE_QUOTED_RE.sub(" ", sql)


def sql_capt(sql: str) -> list[int]:
    body = strip_strings(sql)
    return [int(m.group(1)) for m in re.finditer(r"\bLIMIT\s+(\d+)", body, re.I)] + \
           [int(m.group(1)) for m in re.finditer(r"\bFETCH\s+(?:FIRST\s+)?(\d+)", body, re.I)]


def sql_numbers(sql: str) -> set[float]:
    """Numbers the question is expected to state.

    Removed because they are not question values: row caps (check 1 owns them),
    NULLIF(x, 0) division guards, the placeholder in EXISTS (SELECT 1 ...), and any
    digit that is part of an identifier ("isbn13", "population_2010").
    """
    body = strip_strings(sql)
    body = re.sub(r"\bLIMIT\s+\d+", " ", body, flags=re.I)
    body = re.sub(r"\bOFFSET\s+\d+", " ", body, flags=re.I)
    body = re.sub(r"\bFETCH\s+(?:FIRST\s+)?\d+", " ", body, flags=re.I)
    body = re.sub(r"NULLIF\s*\([^()]*,\s*0\s*\)", " NULLIF_GUARD ", body, flags=re.I)
    body = re.sub(r"\bSELECT\s+1\s", " SELECT ", body, flags=re.I)
    values: set[float] = set()
    for match in re.finditer(r"(?<![\w$.])\d+(?:\.\d+)?(?![\w$.])", body):
        values.add(float(match.group(0)))
    return values


def sql_number_tokens(sql: str) -> set[float]:
    """Every number written anywhere in the SQL text, identifiers and quoted data
    included. Used only to decide whether a question value was invented."""
    return {float(m.group(0).replace(",", "")) for m in re.finditer(r"\d+(?:[.,]\d+)*", sql)}



def sql_literals(sql: str) -> list[str]:
    return [m for m in SINGLE_QUOTED_RE.findall(sql) if m.strip()]


def question_numbers(question: str) -> set[float]:
    values: set[float] = set()
    for match in NUM_RE.finditer(question):
        values.add(float(match.group(0).replace(",", "")))
    for match in WORD_NUM_RE.finditer(question):
        values.add(float(WORD_NUM[match.group(1).lower()]))
    return values


def question_literals(question: str) -> list[str]:
    out = []
    for match in QUESTION_QUOTED_RE.finditer(question):
        value = match.group(1) or match.group(2)
        if value.strip():
            out.append(value)
    return out


CAP_PHRASE = re.compile(
    r"\b(first|top|last|final|next|best|worst|few|most recent|earliest|latest)\s+"
    r"(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty|thirty|forty|fifty)\b|"
    r"\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty|thirty|forty|fifty)\s+"
    r"(highest|lowest|largest|smallest|greatest|recent|oldest|newest|longest|shortest|best|worst|"
    r"rows|records|entries|items|cheapest|dearest)\b", re.I)


def question_number_claims(question: str) -> tuple[set[float], set[float]]:
    """(hard claims, soft claims).

    Hard: digits written in the question - they are values the SQL must carry.
    Soft: spelled-out numbers that sit in a row-cap phrase ("the five most recent");
    they are value claims only in that context, so "two writer records" is not one.
    """
    hard: set[float] = set()
    for match in NUM_RE.finditer(question):
        hard.add(float(match.group(0).replace(",", "")))
    soft: set[float] = set()
    for match in CAP_PHRASE.finditer(question):
        word = next((g for g in match.groups() if g and g.lower() in WORD_NUM), None)
        if word:
            soft.add(float(WORD_NUM[word.lower()]))
    hard -= soft
    return hard, soft




# --------------------------------------------------------------- check 1: LIMIT

def check_limit(row: dict) -> dict:
    sql = row["pg_gold_sql"]
    caps = sql_capt(sql)
    result_rows = (row.get("verification") or {}).get("result_rows")
    if not caps:
        return {"name": "limit_check", "status": "pass", "cap": None,
                "detail": "no row cap in the SQL"}
    cap = max(caps)
    named = number_in_question(float(cap), row["question"])
    if named:
        return {"name": "limit_check", "status": "pass", "cap": cap,
                "detail": f"cap {cap} named in the question as '{named}'"}
    if isinstance(result_rows, int) and result_rows < cap:
        return {"name": "limit_check", "status": "pass", "cap": cap,
                "detail": f"cap {cap} not binding: result has {result_rows} rows (proved no-op)",
                "note": "cap_not_binding"}
    if cap == 1 and SUPERLATIVE_CUES.search(row["question"]):
        return {"name": "limit_check", "status": "review", "cap": cap,
                "detail": "LIMIT 1 with singular/superlative wording but no named number",
                "note": "cap_single_row_superlative"}
    return {"name": "limit_check", "status": "fail", "cap": cap,
            "detail": f"SQL caps at {cap} rows and the question never names {cap}",
            "note": "cap_unnamed"}


# ----------------------------------------------------- check 2: literals, both ways

def literal_matches(literal: str, text: str) -> bool:
    """A literal counts as stated when it, or its LIKE pattern with the wildcards
    removed, appears in the other side (case-insensitive)."""
    candidates = {literal, literal.strip("%").strip(), literal.replace("%", ""),
                  literal.replace("_", " ").strip()}
    lowered = text.lower()
    return any(candidate.strip() and candidate.lower() in lowered for candidate in candidates)


def number_variants(value: float) -> list[str]:
    if value == int(value):
        number = int(value)
        return [str(number), f"{number:,}"]
    return [f"{value:g}", f"{value:f}", f"{value:,.2f}"]


def number_in_question(value: float, question: str) -> str | None:
    """Is this numeric value stated in the text? Returns the wording found."""
    lowered = question.lower()
    for token in number_variants(value):
        if re.search(rf"(?<![\d.,]){re.escape(token)}(?![\d.])", question):
            return token
    if value == int(value):
        spelled = next((word for word, num in WORD_NUM.items() if num == int(value)), None)
        if spelled and re.search(rf"\b{spelled}(st|nd|rd|th)?\b", lowered):
            return spelled
        repeat = {1: r"\bonce\b|\bsingle\b|\bonly one\b", 2: r"\btwice\b",
                  3: r"\bthrice\b"}.get(int(value))
        if repeat and re.search(repeat, lowered):
            return "repeat-word"
    return None


def check_literals(row: dict) -> dict:
    """Bidirectional value check.

    The statement surface is question + evidence, because both are in the model's
    input: a SQL value glossed by the evidence is not an unstated filter.
    """
    sql, question = row["pg_gold_sql"], row["question"]
    evidence = (row.get("evidence") or "").strip()
    caps = {float(c) for c in sql_capt(sql)}
    missing_sql_to_question: list[str] = []

    for value in sorted(sql_numbers(sql)):
        if value in caps:
            continue                                     # check 1 owns caps
        stated = number_in_question(value, question) or (
            evidence and number_in_question(value, evidence))
        if stated:
            continue
        missing_sql_to_question.append(
            str(int(value)) if value == int(value) else f"{value:g}")
    for literal in sql_literals(sql):
        if literal_matches(literal, question) or (evidence and literal_matches(literal, evidence)):
            continue
        missing_sql_to_question.append(f"'{literal}'")

    sql_number_tokens_all = sql_number_tokens(sql)
    inside_sql_strings = SINGLE_QUOTED_RE.findall(sql)
    missing_question_to_sql: list[str] = []

    hard_claims, soft_claims = question_number_claims(question)
    for value in sorted(hard_claims | soft_claims):
        if value in sql_number_tokens_all:
            continue
        text = str(int(value)) if value == int(value) else f"{value:g}"
        if any(text in s or text.replace(",", "") in s.replace(",", "")
               for s in inside_sql_strings):
            continue                                     # value lives inside a quoted datum
        missing_question_to_sql.append(text)
    for literal in question_literals(question):
        if literal_matches(literal, sql):
            continue
        missing_question_to_sql.append(f"'{literal}'")

    detail = []
    if missing_sql_to_question:
        detail.append("in SQL but not stated: " + ", ".join(missing_sql_to_question))
    if missing_question_to_sql:
        detail.append("in question but not in SQL: " + ", ".join(missing_question_to_sql))
    status = "fail" if (missing_sql_to_question or missing_question_to_sql) else "pass"
    return {"name": "literal_check", "status": status,
            "missing_sql_to_question": missing_sql_to_question,
            "missing_question_to_sql": missing_question_to_sql,
            "detail": "; ".join(detail) or "both directions match",
            "note": "literal_sql_unstated" if missing_sql_to_question else ""}


# ------------------------------------------------------- check 4: evidence gap

# A value the reader has to decode: one letter, or a 2-3 letter code / code+number
# such as 'F', 't', 'GA', 'TX-23'. Longer words ('PIKE', 'UNION', 'No') are their own
# gloss and are not treated as codes.
OPAQUE_CODE = re.compile(r"^[A-Za-z]$|^[A-Za-z]{2,3}$|^[A-Za-z]{1,3}-\d+$")
UNIT_TOKENS = ("kg", "km", "m2", "m3", "cm", "mm", "sq ft", "mph", "kph", "mAh", "watt",
               "watts", "liter", "liters", "ml", "USD", "EUR", "GBP", "percent", "%")


def quoted_word_in(text: str, token: str) -> bool:
    return bool(re.search(rf"(?<![\w-]){re.escape(token.lower())}(?![\w-])", text))


def check_evidence_gap(row: dict) -> dict:
    """The row depends on a code or a unit that nothing in the row explains."""
    sql, question = row["pg_gold_sql"], row["question"]
    evidence = (row.get("evidence") or "").strip()
    codes: list[str] = []
    for literal in sql_literals(sql):
        value = literal.strip()
        if not OPAQUE_CODE.match(value):
            continue                                     # self-explanatory word
        if quoted_word_in(question, value):
            continue                                     # question carries the code itself
        if evidence and quoted_word_in(evidence, value):
            continue                                     # evidence glosses it
        codes.append(value)
    units: list[str] = []
    if not evidence:
        for token in UNIT_TOKENS:
            if re.search(rf"(?<![\w]){re.escape(token)}(?![\w])", question, re.I) and \
                    not re.search(rf"(?<![\w]){re.escape(token)}(?![\w])", sql, re.I):
                units.append(token)
    if codes or units:
        parts = []
        if codes:
            parts.append("SQL filters on a code nothing in the row glosses: "
                         + ", ".join(f"'{c}'" for c in codes))
        if units:
            parts.append("question states a unit the SQL never names and no evidence covers: "
                         + ", ".join(units))
        return {"name": "evidence_gap", "flag": True, "code_gap": bool(codes),
                "unit_gap": bool(units), "codes": codes, "units": units,
                "evidence_present": bool(evidence), "detail": "; ".join(parts)}
    return {"name": "evidence_gap", "flag": False, "code_gap": False, "unit_gap": False,
            "codes": [], "units": [], "evidence_present": bool(evidence),
            "detail": "no unglossed code or unit"}


# --------------------------------------------------------------- mechanical stage

def cmd_mechanical(args) -> int:
    rows = load_scope()
    out: list[dict] = []
    for row in rows:
        limit = check_limit(row)
        literals = check_literals(row)
        gap = check_evidence_gap(row)
        out.append({"sample_id": row["sample_id"], "variant": variant_of(row["sample_id"]),
                    "db_id": row["db_id"], "limit_check": limit,
                    "literal_check": literals, "evidence_gap": gap,
                    "mechanical_pass": limit["status"] == "pass" and literals["status"] == "pass"})
    MECHANICAL.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out),
                          encoding="utf-8")

    def counter(fn):
        from collections import Counter
        return dict(Counter(fn(r) for r in out))

    summary = {
        "rows": len(out),
        "limit_status": counter(lambda r: r["limit_check"]["status"]),
        "limit_notes": counter(lambda r: r["limit_check"].get("note") or "none"),
        "literal_status": counter(lambda r: r["literal_check"]["status"]),
        "literal_directions": {
            "sql_values_absent_from_question": sum(1 for r in out if r["literal_check"]["missing_sql_to_question"]),
            "question_values_absent_from_sql": sum(1 for r in out if r["literal_check"]["missing_question_to_sql"])},
        "evidence_gap_flagged": sum(1 for r in out if r["evidence_gap"]["flag"]),
        "mechanical_pass": sum(1 for r in out if r["mechanical_pass"]),
        "mechanical_fail": sum(1 for r in out if not r["mechanical_pass"]),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    def show(label: str, predicate, limit_n: int = 8) -> None:
        picked = [r for r in out if predicate(r)][:limit_n]
        print(f"\n--- {label} ({len([r for r in out if predicate(r)])}) ---")
        for r in picked:
            pair = next((row for row in rows if row["sample_id"] == r["sample_id"]), {})
            print(f"  {r['sample_id']} [{r['db_id']}] {pair.get('question', '')[:130]}")
            print(f"      SQL: {pair.get('pg_gold_sql', '')[:170]}")

    show("limit_check fail", lambda r: r["limit_check"]["status"] == "fail")
    show("limit_check review", lambda r: r["limit_check"]["status"] == "review")
    show("literal fail (question promises a value the SQL lacks)",
         lambda r: bool(r["literal_check"]["missing_question_to_sql"]))
    show("literal fail (SQL value never stated)",
         lambda r: bool(r["literal_check"]["missing_sql_to_question"]))
    show("evidence_gap flagged", lambda r: r["evidence_gap"]["flag"])
    return 0


# ------------------------------------------------------------------- judge stage

def cmd_judge(args) -> int:
    rows = load_scope()
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    done = {}
    if JUDGE_OUT.exists():
        for line in JUDGE_OUT.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                done[record.get("pair_key") or record["sample_id"]] = record
    pending = []
    for row in rows:
        pair = as_pair(row)
        key = jf.key_of(pair)
        pair["pair_key"] = key
        if key not in done:
            pending.append(pair)
    batches = [pending[i:i + args.batch] for i in range(0, len(pending), args.batch)]
    print(f"[judge] rows={len(rows)} already={len(rows) - len(pending)} to_judge={len(pending)} "
          f"batches={len(batches)} workers={args.workers}", flush=True)
    JUDGE_RAW.mkdir(parents=True, exist_ok=True)
    failures = 0
    written = 0
    started = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(jf.judge_batch, batch, schema, max(320, 130 * len(batch))): batch
                   for batch in batches}
        for future in concurrent.futures.as_completed(futures):
            batch = futures[future]
            verdicts, raw_text, stats = future.result()
            records = []
            for pair, verdict in zip(batch, verdicts):
                if verdict is None:
                    failures += 1
                    continue
                records.append({"sample_id": pair["sample_id"], "variant": pair["variant"],
                                "db_id": pair["db_id"], "pair_key": pair["pair_key"], **verdict})
            if records:
                with JUDGE_OUT.open("a", encoding="utf-8") as handle:
                    for record in records:
                        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                written += len(records)
            else:
                tag = "_".join(p["sample_id"] for p in batch)
                (JUDGE_RAW / f"{tag}.json").write_text(
                    json.dumps({"sample_ids": [p["sample_id"] for p in batch], "stats": stats,
                                "raw_text": raw_text[:4000]}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
            if len(batch) != sum(1 for v in verdicts if v is not None):
                tag = "_".join(p["sample_id"] for p in batch)
                (JUDGE_RAW / f"bad_{tag}.json").write_text(
                    json.dumps({"sample_ids": [p["sample_id"] for p in batch], "stats": stats,
                                "raw_text": raw_text[:4000]}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
            done_every = int(time.time() - started)
            print(f"[judge] {written}/{len(pending)} written ({done_every}s, "
                  f"batch {stats.get('seconds')}s)", flush=True)
    print(json.dumps({"rows": len(rows), "judge_records": written + len(done),
                      "new": written, "unparsed": failures,
                      "seconds": round(time.time() - started, 1)}, ensure_ascii=False, indent=2))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("mechanical", "judge"))
    parser.add_argument("--batch", type=int, default=BATCH)
    parser.add_argument("--workers", type=int, default=WORKERS)
    parsed = parser.parse_args()
    raise SystemExit(cmd_mechanical(parsed) if parsed.stage == "mechanical" else cmd_judge(parsed))
