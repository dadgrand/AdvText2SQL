#!/usr/bin/env python3
"""Reusable G1..G8 semantic filter for text-to-SQL pairs.

Implements research/advtext2sql/cluster/judge/METHODOLOGY.md literally:
criteria G1..G8 in order (stop at the first hard failure), verdicts
accept / repair / reject (plus `unclear`, counted as a reject for the rate but
reported separately), strict JSON output, `reason` at most 12 words, a
question-side `fix_hint` only for `repair`.

The filter NEVER edits the corpus, the questions or the SQL: it reads pairs,
asks the local model for a verdict and writes verdict records to its own files.

Usage
    python3 judge_filter.py judge --input PAIRS.jsonl [--input MORE.jsonl] \
            --out VERDICTS.jsonl [--batch 8] [--workers 3] [--sample 30] [--limit N]
    python3 judge_filter.py report --verdicts VERDICTS.jsonl --report REPORT.json \
            [--input PAIRS.jsonl ...] [--examples 10] \
            [--gate-accepted ACCEPTED.jsonl --gate-rejected REJECTED.jsonl] \
            [--hand-labels HAND.json]

Endpoint: http://127.0.0.1:8081/v1/chat/completions, model
qwen3.8-flash-next-iq3_xxs, temperature 0, chat_template_kwargs
{"enable_thinking": false}, batches of up to 8 pairs.
Idempotent: a pair already present in --out (same sample_id + variant + pair
hash) is skipped.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import pathlib
import random
import re
import sys
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent                       # research/advtext2sql
sys.path.insert(0, str(ROOT))

ENDPOINT = "http://127.0.0.1:8081/v1/chat/completions"
MODEL = "qwen3.8-flash-next-iq3_xxs"
SCHEMA_PATH = ROOT / "delivery" / "bird_pg_dataset" / "schemas" / "schema.json"

BATCH_MAX = 8
SEED = 20261125

VERDICTS = ("accept", "repair", "reject", "unclear")
DEFECTS = ("unstated_filter", "missing_filter", "wrong_aggregate", "fanout_inflation",
           "hidden_limit", "period_off_by_one", "wrong_entity", "unanswerable",
           "ambiguous", "other")
CRITERIA = ("G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8")

SYSTEM = """You are a strict semantic judge for a PostgreSQL text-to-SQL corpus.
You classify pairs; you never rewrite SQL. Judge ONLY meaning, never executability.

For each pair answer the criteria in this order and STOP at the first hard failure:
G1 Does the SQL answer exactly the question asked - no more, no less? Hard failure: a filter,
   join or projection the question does not state, or a stated one is missing.
G2 Do the aggregates match the words? "how many"/number of -> COUNT, average -> AVG,
   total -> SUM, each/distinct -> DISTINCT. Hard failure: wrong aggregate or missing DISTINCT.
G3 Does every literal in the SQL appear in the question with the same meaning?
   Hard failure: entity/number mismatch, wrong case or wrong unit.
G4 Is the counting unit right? Hard failure: rows counted after a join are inflated by fan-out.
G5 Is the row cap honest? Hard failure: a LIMIT that the question does not name and that is
   not provably a no-op (a hidden LIMIT). A LIMIT is honest when the question names the number,
   or when the shown result preview proves the cap did not truncate (row count below the LIMIT).
G6 Is the time window right (year/month/day, inclusive vs exclusive bounds)?
   Hard failure: an off-by-one period.
G7 Is the question answerable at all with this data? Hard failure: it presumes rows the
   schema cannot hold.
G8 Is there exactly one reasonable reading? Hard failure: two valid SQLs, underspecified.

"evidence" is supporting text; if it contradicts the SQL that is G1.
Dialect, executability and result correctness are OUT OF SCOPE - the pipeline already
executed the SQL twice. Never accept a pair because the SQL looks neat or the question
reads fluently. Table and column names are shown to you on purpose so you can verify
mappings; the question itself must not leak them.

Verdicts:
- "accept": all of G1..G8 pass.
- "repair": answerable, but the QUESTION must change (for example a missing clause).
  "fix_hint" states the smallest question-side change, in question wording, never SQL.
- "reject": no honest question/schema pairing exists, or the data cannot satisfy the
  question (G7), or the intent is unclear beyond repair (G8).
- "unclear": ONLY when the record lacks the text needed to decide (for example a table
  used by the SQL is missing from the schema excerpt). Never use it as a dodge.

"failed" lists the criterion IDs that failed, first failure first; [] when verdict is
"accept" or "unclear".
"defect" is exactly one of: unstated_filter, missing_filter, wrong_aggregate,
fanout_inflation, hidden_limit, period_off_by_one, wrong_entity, unanswerable,
ambiguous, other - or null when verdict is "accept" or "unclear".
"fix_hint" is a string only for "repair"; null for accept, reject and unclear.
"reason" is at most 12 words.

Reply with ONLY a JSON array, one object per pair in the same order, each object with
EXACTLY these keys and nothing else:
{"sample_id": "<copy the given sample_id>", "variant": <int>,
 "verdict": "accept"|"repair"|"reject"|"unclear", "failed": ["G1"],
 "defect": <defect label or null>, "reason": "<= 12 words>", "fix_hint": <string or null>}
No markdown, no code fences, no commentary, no reasoning trace, no extra keys."""


# --------------------------------------------------------------------------- helpers

def sha256_of(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pair_hash(item: dict) -> str:
    blob = json.dumps([item.get("sample_id"), item.get("variant"), item.get("db_id"),
                       item.get("question", ""), item.get("evidence", ""),
                       item.get("pg_gold_sql", "")], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def key_of(item: dict) -> str:
    """Resume key: sample_id + variant + hash of the judged content.

    Verdict records carry `pair_key` so a resume run can recognise them without
    the original question/SQL text.
    """
    stored = item.get("pair_key")
    if stored:
        return str(stored)
    return f'{item.get("sample_id")}|{item.get("variant") or 1}|{pair_hash(item)[:12]}'



def required_fields(item: dict) -> tuple[bool, list[str]]:
    missing = [f for f in ("sample_id", "db_id", "question", "pg_gold_sql")
               if not str(item.get(f) or "").strip()]
    return (not missing), missing


SQL_WORDS = {"select", "from", "where", "join", "left", "right", "inner", "outer", "full",
             "on", "as", "group", "by", "order", "limit", "offset", "having", "union",
             "all", "distinct", "and", "or", "not", "in", "exists", "case", "when",
             "then", "else", "end", "with", "cross", "natural", "using", "lateral",
             "values", "unnest", "array", "interval", "date", "timestamp", "cast",
             "asc", "desc", "nulls", "first", "last", "fetch", "next", "rows", "only"}


def sql_cte_names(sql: str) -> set[str]:
    return {m.group(1).strip('"').lower()
            for m in re.finditer(r'\bWITH(?:\s+RECURSIVE)?\s+([A-Za-z_"][\w"]*)', sql, re.I)}


def sql_referenced_tables(sql: str) -> set[str]:
    """Tables the SQL reads: quoted or bare identifiers directly after FROM/JOIN."""
    names: set[str] = set()
    for match in re.finditer(r'\b(?:FROM|JOIN)\s+("([^"]+)"|([A-Za-z_][\w$]*))', sql, re.I):
        name = (match.group(2) or match.group(3)).strip()
        if name.lower() in SQL_WORDS:
            continue
        names.add(name)
    return names


def used_tables(schema_db: dict, sql: str) -> list[str]:
    """Schema tables the SQL references, plus a word-boundary sweep as a safety net."""
    names = sql_referenced_tables(sql)
    out: list[str] = []
    for table in schema_db.get("tables", []):
        table_name = table["name"]
        if table_name in names or table_name.lower() in {n.lower() for n in names} \
                or re.search(rf'\b{re.escape(table_name)}\b', sql):
            out.append(table_name)
    return out


def schema_excerpt(schema_db: dict, sql: str, limit: int = 1900) -> tuple[str, list[str]]:
    tables = schema_db.get("tables", [])
    by_name = {t["name"]: t for t in tables}
    names = used_tables(schema_db, sql)
    picked = [by_name[n] for n in names] or tables          # fall back to the whole db
    lines: list[str] = []
    for table in picked:
        cols = [f'{c["name"]} {c.get("type", "")}{" NOT NULL" if c.get("not_null") else ""}'
                for c in table.get("columns", [])]
        pk = ",".join(table.get("primary_key") or [])
        fks = "; ".join(f'{fk.get("from_column")}->{fk.get("to_table")}.{fk.get("to_column")}'
                        for fk in (table.get("foreign_keys") or []))
        head = f'{table["name"]}({", ".join(cols)})'
        if pk:
            head += f" [PK {pk}]"
        if fks:
            head += f" [FK {fks}]"
        head += f' [~{table.get("row_count_estimate", "?")} rows]'
        if sum(len(x) for x in lines) + len(head) > limit:
            lines.append(f'{table["name"]}[... {len(cols)} columns truncated ...]')
            break
        lines.append(head)
    return "\n".join(lines)[:limit], [t["name"] for t in picked]


def pair_block(index: int, item: dict, schema_db: dict) -> tuple[str, list[str]]:
    excerpt, picked = schema_excerpt(schema_db, item["pg_gold_sql"])
    picked_ci = {n.lower() for n in picked}
    cte = {n.lower() for n in sql_cte_names(item["pg_gold_sql"])}
    missing = sorted({n for n in sql_referenced_tables(item["pg_gold_sql"])
                      if n.lower() not in picked_ci and n.lower() not in cte})
    parts = [
        f'### PAIR {index}  sample_id={item.get("sample_id")}  variant={item.get("variant") or 1}',
        f'database: {item.get("db_id")}',
        f'schema excerpt (tables the SQL uses):\n{excerpt}',
        f'question:\n{item.get("question", "")}',
        f'evidence: {item.get("evidence") or "(none)"}',
        f'SQL:\n{item.get("pg_gold_sql", "")}',
    ]
    rows, columns = item.get("rows"), item.get("columns")
    if isinstance(rows, int) and isinstance(columns, int):
        parts.append(f'result preview of the executed SQL: {rows} row(s) x {columns} column(s)')
    if missing:
        parts.append("WARNING: the SQL reads tables absent from the excerpt: "
                     + ", ".join(missing) + " -> verdict must be \"unclear\"")
    return "\n".join(parts), missing


def attach_result_shape(items: list[dict], reference_paths: list[str]) -> int:
    """Copy the recorded result shape (rows x columns) from reference JSONLs.

    Only used when the reference row has the same sample_id, variant and byte-equal
    pg_gold_sql, so the preview always describes the very SQL being judged.
    """
    reference: dict[tuple, tuple] = {}
    for raw in reference_paths:
        for row in read_jsonl(pathlib.Path(raw)):
            if isinstance(row.get("rows"), int) and isinstance(row.get("columns"), int):
                reference[(str(row.get("sample_id")), row.get("variant") or 1,
                           row.get("pg_gold_sql"))] = (row["rows"], row["columns"])
    attached = 0
    for item in items:
        got = reference.get((str(item.get("sample_id")), item.get("variant") or 1,
                             item.get("pg_gold_sql")))
        if got:
            item["rows"], item["columns"] = got
            attached += 1
    return attached



# --------------------------------------------------------------------- model client

def call(prompt: str, max_tokens: int, timeout: float = 300.0) -> tuple[str, str]:
    payload = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "top_p": 1.0,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    request = urllib.request.Request(ENDPOINT, data=payload,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read())
    message = (data.get("choices") or [{}])[0].get("message", {})
    return str(message.get("content") or ""), str(
        message.get("reasoning") or message.get("reasoning_content") or "")


def strip_noise(text: str) -> str:
    text = re.sub(r"<\|?think[^|>]*\|?>.*?<\|?/\s*think[^|>]*\|?>", "", text, flags=re.S)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"```[a-zA-Z]*", "", text)
    return text.strip("` \n\t")


def _balanced(body: str, start: int) -> str | None:
    """Return the balanced JSON value starting at body[start] ('[' or '{')."""
    pairs = {"[": "]", "{": "}"}
    closer = {"]": "[", "}": "{"}
    stack: list[str] = []
    in_string, escape = False, False
    for i in range(start, len(body)):
        char = body[i]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in pairs:
            stack.append(char)
        elif char in closer:
            if not stack or stack[-1] != closer[char]:
                return None
            stack.pop()
            if not stack:
                return body[start:i + 1]
    return None


def extract_json_array(text: str) -> list | None:
    """Pull the verdict list out of a model reply.

    Order: whole reply as JSON, then the first balanced value that parses to a
    list of objects or to a single verdict object (a bare object is a legal, if
    discouraged, reply for a one-pair batch), then independent flat objects.
    """
    body = strip_noise(text)
    try:
        parsed = json.loads(body)
        if isinstance(parsed, list) and any(isinstance(x, dict) for x in parsed):
            return parsed
        if isinstance(parsed, dict) and "verdict" in parsed:
            return [parsed]
    except json.JSONDecodeError:
        pass
    for match in re.finditer(r"[\[{]", body):
        chunk = _balanced(body, match.start())
        if not chunk:
            continue
        try:
            parsed = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list) and any(isinstance(x, dict) for x in parsed):
            return parsed
        if isinstance(parsed, dict) and "verdict" in parsed:
            return [parsed]
    objects = []
    for match in re.finditer(r"\{[^{}]*\}", body, re.S):
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict) and "verdict" in parsed:
                objects.append(parsed)
        except json.JSONDecodeError:
            continue
    return objects or None


def normalise(raw: dict) -> dict | None:
    verdict = str(raw.get("verdict", "")).strip().lower()
    if verdict not in VERDICTS:
        return None
    failed = raw.get("failed") or []
    if isinstance(failed, str):
        failed = [failed]
    failed = sorted({str(f).strip().upper() for f in failed if str(f).strip().upper() in CRITERIA})
    defect = raw.get("defect", None)
    if defect in (None, "", "null", "none", "None"):
        defect = None
    else:
        defect = str(defect).strip().lower()
        if defect not in DEFECTS:
            defect = "other"
    if verdict in ("accept", "unclear"):
        defect = None
    if verdict == "accept":
        failed = []
    hint = raw.get("fix_hint", None)
    if hint in (None, "", "null", "none", "None"):
        hint = None
    else:
        hint = " ".join(str(hint).split())[:400]
    if verdict != "repair":
        hint = None
    words = " ".join(str(raw.get("reason", "")).split())
    truncated = len(words.split()) > 12
    if truncated:
        words = " ".join(words.split()[:12])
    row = {"verdict": verdict, "failed": failed, "defect": defect,
           "reason": words[:160], "fix_hint": hint}
    if truncated:
        row["reason_truncated"] = True
    return row


def judge_batch(items: list[dict], schema: dict, max_tokens: int) -> tuple[list, str, dict]:
    blocks, missing_tables = [], {}
    for i, item in enumerate(items):
        block, missing = pair_block(i + 1, item, schema.get(item.get("db_id"), {}) or {})
        blocks.append(block)
        missing_tables[item.get("sample_id")] = missing
    prompt = "\n\n".join(blocks) + (
        f"\n\nJudge all {len(items)} pairs above in order. Reply with ONE JSON array of "
        f"{len(items)} objects, each with exactly sample_id, variant, verdict, failed, "
        f"defect, reason, fix_hint.")
    stats = {"prompt_chars": len(prompt), "missing_schema_tables":
             {k: v for k, v in missing_tables.items() if v}}
    last_text = ""
    for attempt in range(3):
        started = time.time()
        try:
            text, reasoning = call(prompt, max_tokens)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            stats.setdefault("errors", []).append(f"{type(error).__name__}: {error}"[:200])
            time.sleep(3 + 3 * attempt)
            continue
        last_text = text
        stats["seconds"] = round(time.time() - started, 2)
        stats["reasoning_chars"] = len(reasoning)
        parsed = extract_json_array(text)
        if parsed:
            by_id: dict[str, dict] = {}
            ordered: list[dict] = []
            for entry in parsed:
                if not isinstance(entry, dict):
                    continue
                normalised = normalise(entry)
                if normalised is None:
                    continue
                ordered.append(normalised)
                entry_id = str(entry.get("sample_id", "")).strip()
                if entry_id:
                    by_id.setdefault(entry_id, normalised)
            aligned: list = []
            for position, item in enumerate(items):
                got = by_id.get(str(item.get("sample_id")))
                if got is None and len(ordered) == len(items):
                    got = ordered[position]
                aligned.append(got)
            if all(v is not None for v in aligned):
                return aligned, last_text, stats
        stats.setdefault("parse_problems", []).append(f"attempt {attempt + 1}: bad payload")
        time.sleep(1)
    return [None] * len(items), last_text, stats


# ---------------------------------------------------------------------------- I/O

def read_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def append_jsonl(path: pathlib.Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def wilson(successes: int, total: int, z: float = 1.959963985) -> tuple[float, float]:
    if total == 0:
        return (0.0, 0.0)
    p = successes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = (z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))) / denom
    return (max(0.0, round(centre - half, 6)), min(1.0, round(centre + half, 6)))


# ---------------------------------------------------------------------------- judge

def cmd_judge(args) -> int:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    out_path = pathlib.Path(args.out)
    items: list[dict] = []
    sources: list[dict] = []
    for raw in args.input:
        path = pathlib.Path(raw)
        rows = read_jsonl(path)
        sources.append({"path": str(path), "exists": path.exists(), "lines": len(rows),
                        "sha256": sha256_of(path) if path.exists() else None})
        for row in rows:
            row.setdefault("_source", str(path))
        items.extend(rows)
    if args.limit:
        items = items[:args.limit]
    attached_shape = attach_result_shape(items, args.shape_join)
    if args.sample:
        rng = random.Random(args.seed)
        items = sorted(rng.sample(items, min(args.sample, len(items))),
                       key=lambda r: str(r.get("sample_id")))
        rng.shuffle(items)
    done = {key_of(r): r for r in read_jsonl(out_path)}
    pending, incomplete = [], []
    for item in items:
        ok, missing = required_fields(item)
        if not ok:
            incomplete.append({"sample_id": item.get("sample_id"), "missing_fields": missing})
            continue
        if key_of(item) in done:
            continue
        pending.append(item)

    batch_size = max(1, min(args.batch, BATCH_MAX))
    batches = [pending[i:i + batch_size] for i in range(0, len(pending), batch_size)]
    print(f"[judge] pairs={len(items)} already_judged={len(items) - len(pending) - len(incomplete)}"
          f" to_judge={len(pending)} batches={len(batches)} batch_size={batch_size}", flush=True)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    written: list[dict] = []
    failures: list[dict] = []
    raw_dir = out_path.parent / f"raw_{out_path.stem}"
    raw_dir.mkdir(parents=True, exist_ok=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(judge_batch, batch, schema, max(320, 130 * len(batch))): batch
                   for batch in batches}
        for future in concurrent.futures.as_completed(futures):
            batch = futures[future]
            verdicts, raw_text, stats = future.result()
            tag = "_".join(str(i.get("sample_id")) for i in batch)
            (raw_dir / f"{tag}.json").write_text(
                json.dumps({"sample_ids": [i.get("sample_id") for i in batch],
                            "stats": stats, "raw_text": raw_text[:8000]},
                           ensure_ascii=False, indent=2), encoding="utf-8")
            rows = []
            for item, verdict in zip(batch, verdicts):
                if verdict is None:
                    failures.append({"sample_id": item.get("sample_id"),
                                     "stats": {k: v for k, v in stats.items() if k != "prompt_chars"}})
                    continue
                row = {"sample_id": item.get("sample_id"), "variant": item.get("variant") or 1,
                       "db_id": item.get("db_id"), "source": item.get("_source"),
                       "pair_key": key_of(item), **verdict}
                if isinstance(item.get("rows"), int) and isinstance(item.get("columns"), int):
                    row["result_shape"] = f'{item["rows"]}x{item["columns"]}'
                if stats["missing_schema_tables"].get(str(item.get("sample_id"))):
                    row["missing_schema_tables"] = stats["missing_schema_tables"][str(item["sample_id"])]
                rows.append(row)
            if rows:
                append_jsonl(out_path, rows)                 # idempotent resume
                written.extend(rows)
            print(f"[judge] batch {tag} ok={len(rows)}/{len(batch)} "
                  f"({stats.get('seconds')}s, prompt {stats['prompt_chars']} chars)", flush=True)

    counts = {}
    for row in read_jsonl(out_path):
        counts[row.get("verdict")] = counts.get(row.get("verdict"), 0) + 1
    summary = {"out": str(out_path), "pairs_seen": len(items), "new_verdicts": len(written),
               "skipped_existing": len(items) - len(pending) - len(incomplete),
               "result_shape_attached": attached_shape,
               "unjudgeable_missing_fields": len(incomplete), "unparsed_batches": len(failures),
               "verdict_counts_total_in_file": counts,
               "new_verdict_counts": {v: sum(1 for r in written if r["verdict"] == v) for v in VERDICTS},
               "sources": sources,
               "endpoint": {"url": ENDPOINT, "model": MODEL, "temperature": 0,
                            "enable_thinking": False, "batch_max": batch_size},
               "failures_sample": failures[:3]}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not failures and not incomplete else 1


# --------------------------------------------------------------------------- report

def cmd_report(args) -> int:
    verdicts_path = pathlib.Path(args.verdicts)
    verdicts = read_jsonl(verdicts_path)
    pairs: dict[str, dict] = {}
    sources: list[dict] = []
    for raw in args.input:
        path = pathlib.Path(raw)
        rows = read_jsonl(path)
        sources.append({"path": str(path), "exists": path.exists(), "lines": len(rows),
                        "sha256": sha256_of(path) if path.exists() else None})
        for row in rows:
            pairs[key_of(row)] = row

    total = len(verdicts)
    counts = {v: sum(1 for r in verdicts if r.get("verdict") == v) for v in VERDICTS}
    mismatch = counts["repair"] + counts["reject"] + counts["unclear"]   # unclear counts as reject
    rate = round(mismatch / total, 6) if total else 0.0
    low, high = wilson(mismatch, total)

    criterion_counts = {c: 0 for c in CRITERIA}
    first_failure_counts: dict[str, int] = {}
    for row in verdicts:
        failed = row.get("failed") or []
        for c in failed:
            criterion_counts[c] = criterion_counts.get(c, 0) + 1
        if failed:
            first = min(failed)
            first_failure_counts[first] = first_failure_counts.get(first, 0) + 1
    defect_counts: dict[str, int] = {}
    for row in verdicts:
        if row.get("defect"):
            defect_counts[row["defect"]] = defect_counts.get(row["defect"], 0) + 1

    per_source: dict[str, dict] = {}
    for row in verdicts:
        bucket = per_source.setdefault(row.get("source") or "unknown",
                                       {"total": 0, **{v: 0 for v in VERDICTS}})
        bucket["total"] += 1
        bucket[row.get("verdict")] = bucket.get(row.get("verdict"), 0) + 1

    # Examples: hardest failures first, hard criteria first, one per defect label where possible.
    severity = {"reject": 0, "repair": 1, "unclear": 2, "accept": 3}
    ranked = sorted((r for r in verdicts if r.get("verdict") != "accept"),
                    key=lambda r: (severity.get(r.get("verdict"), 9),
                                   min(r.get("failed") or ["G9"]),
                                   str(r.get("defect") or ""), str(r.get("sample_id"))))
    seen_defects: dict[str, int] = {}
    picked: list[dict] = []
    for row in ranked:
        label = row.get("defect") or "none"
        if seen_defects.get(label, 0) >= 3 and len(picked) < args.examples:
            continue
        seen_defects[label] = seen_defects.get(label, 0) + 1
        pair = pairs.get(key_of(row), {})
        picked.append({
            "sample_id": row.get("sample_id"), "variant": row.get("variant"),
            "db_id": row.get("db_id"), "source": row.get("source"),
            "verdict": row.get("verdict"), "failed": row.get("failed"),
            "defect": row.get("defect"), "reason": row.get("reason"),
            "fix_hint": row.get("fix_hint"), "result_shape": row.get("result_shape"),
            "question": pair.get("question"), "evidence": pair.get("evidence"),
            "pg_gold_sql": pair.get("pg_gold_sql"),
        })
        if len(picked) >= args.examples:
            break

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "methodology": "cluster/judge/METHODOLOGY.md (G1..G8, accept/repair/reject, unclear counts as reject)",
        "model": {"endpoint": ENDPOINT, "model": MODEL, "temperature": 0,
                  "enable_thinking": False, "batch_max": BATCH_MAX},
        "inputs": sources,
        "missing_inputs": [s["path"] for s in sources if not s["exists"]],
        "verdicts_file": str(verdicts_path),
        "verdicts_sha256": sha256_of(verdicts_path) if verdicts_path.exists() else None,
        "total_judged": total,
        "verdict_counts": counts,
        "mismatch": {"definition": "verdict != accept (repair + reject + unclear)",
                     "count": mismatch, "rate": rate,
                     "wilson95": {"low": low, "high": high},
                     "accept_rate": round(counts["accept"] / total, 6) if total else 0.0,
                     "accept_wilson95": wilson(counts["accept"], total)},
        "criterion_failure_counts": criterion_counts,
        "first_failure_counts": first_failure_counts,
        "defect_counts": defect_counts,
        "per_source": per_source,
        "unclear_note": "unclear = record lacked the text needed to decide; counted as a "
                        "reject for the rate, reported separately here",
        "examples": picked,
    }

    # Optional calibration sections.
    if args.gate_accepted or args.gate_rejected:
        accepted_ids: set[str] = set()
        accepted_path = pathlib.Path(args.gate_accepted) if args.gate_accepted else None
        if accepted_path and accepted_path.exists():
            for row in read_jsonl(accepted_path):
                accepted_ids.add(str(row.get("sample_id") or row.get("synth_id")))
        rejected: dict[str, str] = {}
        rejected_path = pathlib.Path(args.gate_rejected) if args.gate_rejected else None
        if rejected_path and rejected_path.exists():
            for row in read_jsonl(rejected_path):
                sid = str(row.get("sample_id") or row.get("synth_id"))
                rejected[sid] = str(row.get("reason") or row.get("audit_reason") or "unknown")
        rows = []
        agree = 0
        disagreements: list[dict] = []
        for row in verdicts:
            sid = str(row.get("sample_id"))
            if sid in accepted_ids:
                gate = "accept"
            elif sid in rejected:
                gate = "reject"
            else:
                gate = "unknown"
            model_side = "accept" if row.get("verdict") == "accept" else "mismatch"
            gate_side = "accept" if gate == "accept" else ("mismatch" if gate == "reject" else "unknown")
            ok = (gate_side == "unknown") or (gate_side == model_side)
            agree += int(ok)
            record = {"sample_id": sid, "variant": row.get("variant"), "db_id": row.get("db_id"),
                      "gate": gate, "gate_reason": rejected.get(sid),
                      "judge_verdict": row.get("verdict"), "judge_defect": row.get("defect"),
                      "judge_failed": row.get("failed"), "judge_reason": row.get("reason"),
                      "agree": ok}
            rows.append(record)
            if not ok:
                disagreements.append(record)
            pair = pairs.get(key_of(row), {})
            record["question"] = pair.get("question")
            record["pg_gold_sql"] = pair.get("pg_gold_sql")
        judged_gate = sum(1 for r in rows if r["gate"] in ("accept", "reject"))
        sev = {"reject": 0, "unclear": 1, "repair": 2}
        disagreements.sort(key=lambda r: (sev.get(r["judge_verdict"], 9),
                                          min(r["judge_failed"] or ["G9"]), r["sample_id"]))
        report["gate_agreement"] = {
            "gate_labelled_pairs": judged_gate,
            "gate_accepted": sum(1 for r in rows if r["gate"] == "accept"),
            "gate_rejected": sum(1 for r in rows if r["gate"] == "reject"),
            "agreeing": agree, "disagreeing": len(disagreements),
            "agreement_rate": round(agree / judged_gate, 6) if judged_gate else 0.0,
            "agreement_wilson95": wilson(agree, judged_gate),
            "false_negatives_gate_accepted_but_judge_mismatch": sum(
                1 for r in rows if r["gate"] == "accept" and r["judge_verdict"] != "accept"),
            "false_positives_gate_rejected_but_judge_accept": sum(
                1 for r in rows if r["gate"] == "reject" and r["judge_verdict"] == "accept"),
            "per_pair": rows,
            "clearest_disagreements": disagreements[:5],
        }

    if args.hand_labels:
        hand_path = pathlib.Path(args.hand_labels)
        hand = read_jsonl(hand_path) if hand_path.exists() else []
        by_id = {str(r.get("sample_id")): r for r in hand}
        rows, agree = [], 0
        for row in verdicts:
            sid = str(row.get("sample_id"))
            if sid not in by_id:
                continue
            hand_verdict = str(by_id[sid].get("verdict"))
            model_side = "accept" if row.get("verdict") == "accept" else "mismatch"
            hand_side = "accept" if hand_verdict == "accept" else "mismatch"
            ok = hand_side == model_side
            agree += int(ok)
            rows.append({"sample_id": sid, "hand_verdict": hand_verdict,
                         "hand_defect": by_id[sid].get("defect"),
                         "hand_note": by_id[sid].get("note"),
                         "judge_verdict": row.get("verdict"), "judge_defect": row.get("defect"),
                         "judge_failed": row.get("failed"), "agree": ok})
        report["hand_label_agreement"] = {
            "labelled_pairs": len(rows), "agreeing": agree,
            "agreement_rate": round(agree / len(rows), 6) if rows else 0.0,
            "per_pair": rows}

    out_path = pathlib.Path(args.report)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    slim = {k: report[k] for k in ("total_judged", "verdict_counts", "mismatch",
                                   "criterion_failure_counts", "defect_counts")}
    slim["report"] = str(out_path)
    slim["examples"] = len(report["examples"])
    print(json.dumps(slim, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description="G1..G8 semantic filter for text-to-SQL pairs")
    sub = parser.add_subparsers(dest="command", required=True)

    j = sub.add_parser("judge", help="judge pairs into a verdicts JSONL (idempotent)")
    j.add_argument("--input", action="append", required=True, help="pairs JSONL (repeatable)")
    j.add_argument("--shape-join", action="append", default=[],
                   help="reference JSONL(s) to copy rows x columns from (same SQL only)")
    j.add_argument("--out", required=True, help="verdicts JSONL to append to")
    j.add_argument("--batch", type=int, default=BATCH_MAX)
    j.add_argument("--workers", type=int, default=3)
    j.add_argument("--limit", type=int, default=0)
    j.add_argument("--sample", type=int, default=0, help="random subsample before judging")
    j.add_argument("--seed", type=int, default=SEED)
    j.set_defaults(func=cmd_judge)

    r = sub.add_parser("report", help="aggregate a verdicts JSONL into a report JSON")
    r.add_argument("--verdicts", required=True)
    r.add_argument("--report", required=True)
    r.add_argument("--input", action="append", default=[], help="pairs JSONL (for quoted examples)")
    r.add_argument("--examples", type=int, default=10)
    r.add_argument("--gate-accepted", default="")
    r.add_argument("--gate-rejected", default="")
    r.add_argument("--hand-labels", default="")
    r.set_defaults(func=cmd_report)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
