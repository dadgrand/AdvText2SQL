"""Semantic audit of accepted synthetic pairs with the local model as judge.

The automated gate (literals, answer shape, 5-gram duplicates) never checks that
the question actually matches the SQL.  This run sends the schema excerpt for the
tables a pair uses, the SQL and the question to the local endpoint and asks for a
strict per-pair verdict, following the criteria in cluster/judge/METHODOLOGY.md
(G1..G8) while emitting the verdict/defect vocabulary fixed by the t6 contract.

Read-only over the corpus: nothing here rewrites questions, SQL or corpus files.

    python3 cluster/judge/run_judge.py                    # 200 accepted pairs
    python3 cluster/judge/run_judge.py --control          # known-defect control
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
ROOT = HERE.parent.parent                      # research/advtext2sql
sys.path.insert(0, str(ROOT))

ENDPOINT = "http://127.0.0.1:8081/v1/chat/completions"
MODEL = "qwen3.8-flash-next-iq3_xxs"
SEED = 20261125

CORPUS = ROOT / "delivery" / "out" / "synth" / "synthetic_bird_like.jsonl"
DROPPED = ROOT / "delivery" / "out" / "synth" / "dropped_truncated.jsonl"
SCHEMA_PATH = ROOT / "delivery" / "bird_pg_dataset" / "schemas" / "schema.json"

VERDICTS = HERE / "verdicts.jsonl"
CONTROL = HERE / "control.jsonl"
RAW_DIR = HERE / "raw"
REPORT = HERE / "report.json"

VERDICT_VALUES = ("match", "mismatch", "unclear")
DEFECT_VALUES = ("unstated_filter", "wrong_entity", "wrong_aggregate", "ambiguous", "other")

SYSTEM = """You are a strict semantic auditor for a PostgreSQL text-to-SQL dataset.
For each pair decide whether the SQL answers EXACTLY the question, given the schema.
Judge in this order and stop at the first hard failure (criterion IDs from METHODOLOGY.md):
G1 scope: no filter, join or projection in the SQL that the question does not state, and none missing.
G2 aggregates match the words: "how many"/number of -> COUNT, average -> AVG, total -> SUM, each/distinct -> DISTINCT.
G3 every literal in the SQL appears in the question with the same meaning (same entity, number, case, unit).
G4 counting unit: rows counted after a join must not be inflated by fan-out.
@G5@
G6 time window: year/month/day, inclusive vs exclusive bounds.
G7 answerable: the question presumes data that this schema can hold.
G8 exactly one reasonable reading of the question.
"evidence" is helper text for a code or abbreviation; if it contradicts the SQL that is G1.
Do NOT judge executability, dialect or result correctness: the pipeline already executed the SQL.
A fluent question or a pretty SQL is NOT a match. A hidden filter, an unnamed LIMIT that
actually cut rows, a wrong aggregate, an entity swap or an ambiguous reading is a mismatch.

Verdicts:
- "match": all of G1..G8 pass.
- "mismatch": at least one criterion fails and the failure can be named.
- "unclear": only when the record lacks the text needed to decide (e.g. a table used by
  the SQL is missing from the schema excerpt). Never use "unclear" as a dodge.

defect (non-null ONLY for "mismatch"; null for "match" and "unclear") - exactly one of:
- "unstated_filter": SQL filters/caps/projection scope the question does not state, or a stated one is missing (G1, G5, G6).
- "wrong_entity": a table, column, literal or number refers to something else than the question says (G3, G4).
- "wrong_aggregate": COUNT/SUM/AVG/MIN/MAX/DISTINCT does not match the words (G2).
- "ambiguous": the question admits several readings, so this SQL is not uniquely justified (G8).
- "other": any other semantic defect (e.g. hidden LIMIT cap not nameable, unanswerable question).

Output format, strict: reply with ONLY a JSON array, one object per pair in the same order.
Each object has EXACTLY these keys and nothing else:
{"sample_id": "<copy the given sample_id>", "verdict": "match" | "mismatch" | "unclear",
 "defect": "unstated_filter" | "wrong_entity" | "wrong_aggregate" | "ambiguous" | "other" | null,
 "reason": "<= 12 words, no punctuation games>"}
No markdown, no code fences, no commentary, no reasoning trace, no extra keys."""


def sha256_of(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def used_tables(schema_db: dict, sql: str) -> list[str]:
    """Schema tables referenced by the SQL (quoted or bare, word-boundary match)."""
    out = []
    for table in schema_db.get("tables", []):
        name = table["name"]
        if re.search(rf'\b{re.escape(name)}\b', sql):
            out.append(name)
    return out


def schema_excerpt(schema_db: dict, sql: str, limit: int = 2600) -> tuple[str, list[str]]:
    tables = schema_db.get("tables", [])
    names = used_tables(schema_db, sql)
    picked = [t for t in tables if t["name"] in names] or tables
    lines: list[str] = []
    for table in picked:
        cols = []
        for column in table.get("columns", []):
            flag = " NOT NULL" if column.get("not_null") else ""
            cols.append(f'{column["name"]} {column.get("type", "")}{flag}')
        pk = ",".join(table.get("primary_key") or [])
        fks = "; ".join(
            f'{fk.get("column")}->{fk.get("references", {}).get("table")}.{fk.get("references", {}).get("column")}'
            for fk in (table.get("foreign_keys") or [])
        )
        head = f'{table["name"]}({", ".join(cols)})'
        if pk:
            head += f' [PK {pk}]'
        if fks:
            head += f' [FK {fks}]'
        head += f' [~{table.get("row_count_estimate", "?")} rows]'
        lines.append(head)
        if sum(len(x) for x in lines) > limit:
            lines[-1] = f'{table["name"]}[...columns truncated...]'
            break
    return "\n".join(lines)[:limit], [t["name"] for t in picked]


def pair_block(index: int, item: dict, schema_db: dict) -> str:
    excerpt, picked = schema_excerpt(schema_db, item["pg_gold_sql"])
    referenced = re.findall(r'(?:FROM|JOIN)\s+"([^"]+)"', item["pg_gold_sql"])
    missing = sorted({name for name in referenced if name not in picked})
    parts = [
        f'### PAIR {index} sample_id={item["sample_id"]} variant={item.get("variant") or 1}',
        f'database: {item["db_id"]}',
        f'schema excerpt (tables used by the SQL):\n{excerpt}',
        f'question:\n{item["question"]}',
        f'evidence: {item.get("evidence") or "(none)"}',
        f'SQL:\n{item["pg_gold_sql"]}',
        f'executed result size: {item.get("rows", "?")} rows x {item.get("columns", "?")} columns '
        f'(the SQL ran; only its wording is under audit)',
    ]
    if missing:
        parts.append(f'WARNING: tables used by the SQL but absent from the excerpt: {", ".join(missing)}')
    return "\n".join(parts)


G5_LINE_NO_OP = """G5 row cap: a LIMIT must be named in the question. Each record states the executed row count;
   a LIMIT that is >= that count could not cut anything and is NOT a defect on its own."""
G5_LINE_STRICT = """G5 row cap: a LIMIT must be named in the question. Each record states the executed row count:
   if the count is smaller than the LIMIT the cap could not have cut rows and is not a defect;
   if the count equals or exceeds the LIMIT the cap DID cut rows and an unnamed cap IS a defect."""


def system_prompt(g5_line: str) -> str:
    return SYSTEM.replace("@G5@", g5_line)


def call(prompt: str, max_tokens: int, system: str, timeout: float = 180.0) -> str:
    payload = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "top_p": 1.0,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    request = urllib.request.Request(
        ENDPOINT, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read())
    message = (data.get("choices") or [{}])[0].get("message", {})
    text = message.get("content") or ""
    reasoning = message.get("reasoning") or message.get("reasoning_content") or ""
    return str(text), str(reasoning)


def strip_noise(text: str) -> str:
    text = re.sub(r"<\|?think[^|>]*\|?>.*?<\|?/\s*think[^|>]*\|?>", "", text, flags=re.S)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"```[a-zA-Z]*", "", text)
    return text.strip("` \n\t")


def json_array(text: str) -> list | None:
    """Extract the outermost JSON array; fall back to independent objects."""
    body = strip_noise(text)
    start = body.find("[")
    if start >= 0:
        depth = 0
        in_string = False
        escape = False
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
            elif char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(body[start:i + 1])
                        if isinstance(parsed, list):
                            return parsed
                    except json.JSONDecodeError:
                        break
    objects = []
    for match in re.finditer(r"\{[^{}]*\}", body, re.S):
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                objects.append(parsed)
        except json.JSONDecodeError:
            continue
    return objects or None


def normalise(raw: dict, want_id: str) -> dict | None:
    verdict = str(raw.get("verdict", "")).strip().lower()
    if verdict not in VERDICT_VALUES:
        return None
    defect = raw.get("defect", None)
    if defect in (None, "", "null", "none", "None"):
        defect = None
    else:
        defect = str(defect).strip().lower()
        if defect not in DEFECT_VALUES:
            defect = "other"
    if verdict != "mismatch":
        defect = None
    reason = " ".join(str(raw.get("reason", "")).split())
    if len(reason.split()) > 12:                      # keep the contract's "<= 12 words"
        words = reason.split()
        fillers = {"the", "a", "an", "for", "that", "with", "does", "it", "is", "are", "of", "to"}
        while len(words) > 12:
            drop = next((i for i, w in enumerate(words) if w.lower().strip(",.;:") in fillers), None)
            words.pop(drop if drop is not None else len(words) - 1)
        reason = " ".join(words)
    reason = reason[:160]
    return {"verdict": verdict, "defect": defect, "reason": reason}


def judge_batch(items: list[dict], schema: dict, max_tokens: int, system: str) -> tuple[list, str, dict]:
    """Return (verdicts aligned to items, last raw text, stats)."""
    prompt = "\n\n".join(pair_block(i + 1, item, schema[item["db_id"]])
                         for i, item in enumerate(items))
    prompt += (f"\n\nJudge all {len(items)} pairs above. Reply with ONE JSON array of "
               f"{len(items)} objects, each with exactly sample_id, verdict, defect, reason.")
    stats = {"prompt_chars": len(prompt)}
    last_text = ""
    for attempt in range(3):
        started = time.time()
        try:
            text, reasoning = call(prompt, max_tokens, system)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            stats.setdefault("errors", []).append(f"{type(error).__name__}: {error}")
            time.sleep(3 + 3 * attempt)
            continue
        last_text = text
        stats["seconds"] = round(time.time() - started, 2)
        stats["reasoning_chars"] = len(reasoning)
        parsed = json_array(text)
        if parsed:
            by_id: dict[str, dict] = {}
            ordered: list[dict] = []
            for entry in parsed:
                if not isinstance(entry, dict):
                    continue
                entry_id = str(entry.get("sample_id", "")).strip()
                verdict = normalise(entry, entry_id)
                if verdict is None:
                    continue
                ordered.append(verdict)
                if entry_id:
                    by_id[entry_id] = verdict
            aligned: list = []
            for item in items:
                sid = item["sample_id"]
                got = by_id.get(sid)
                if got is None and len(ordered) == len(items):
                    got = ordered[len(aligned)]
                aligned.append(got)
            if all(v is not None for v in aligned):
                return aligned, last_text, stats
        stats.setdefault("parse_problems", []).append(f"attempt {attempt + 1}: bad payload")
        time.sleep(1)
    return [None] * len(items), last_text, stats


def wilson(successes: int, total: int, z: float = 1.959963985) -> tuple[float, float]:
    if total == 0:
        return (0.0, 0.0)
    p = successes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = (z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))) / denom
    return (max(0.0, round(centre - half, 6)), min(1.0, round(centre + half, 6)))


def read_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: pathlib.Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                    encoding="utf-8")


def run(items: list[dict], schema: dict, batch_size: int, workers: int,
        done: dict[str, dict], tag: str, system: str) -> tuple[list[dict], list[dict]]:
    pending = [item for item in items if item["sample_id"] not in done]
    batches = [pending[i:i + batch_size] for i in range(0, len(pending), batch_size)]
    results: list[dict] = []
    failures: list[dict] = []
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    def handle(batch: list[dict], max_tokens: int):
        return judge_batch(batch, schema, max_tokens, system)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(handle, batch, max(300, 110 * len(batch))): batch for batch in batches}
        for future in concurrent.futures.as_completed(futures):
            batch = futures[future]
            verdicts, raw_text, stats = future.result()
            RAW_DIR.joinpath(f"{tag}_{'_'.join(i['sample_id'] for i in batch)}.json").write_text(
                json.dumps({"tag": tag, "sample_ids": [i["sample_id"] for i in batch],
                            "stats": stats, "raw_text": raw_text[:6000]}, ensure_ascii=False, indent=2),
                encoding="utf-8")
            for item, verdict in zip(batch, verdicts):
                if verdict is None:
                    failures.append({"sample_id": item["sample_id"], "stats": stats})
                    continue
                row = {"sample_id": item["sample_id"], "variant": item.get("variant") or 1,
                       "verdict": verdict["verdict"], "defect": verdict["defect"],
                       "reason": verdict["reason"]}
                results.append(row)
                done[row["sample_id"]] = row
            print(f"[{tag}] batch of {len(batch)} done ({stats.get('seconds')}s)", flush=True)
    return results, failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=200)
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--control", action="store_true",
                        help="judge the known-defect control pool instead of the accepted sample")
    parser.add_argument("--control-n", type=int, default=24)
    parser.add_argument("--out", default="", help="verdicts jsonl path (default: verdicts.jsonl)")
    parser.add_argument("--tag", default="main")
    parser.add_argument("--g5", choices=("no_op", "strict"), default="no_op",
                        help="how the prompt defines an unnamed LIMIT (see METHODOLOGY.md G5)")
    args = parser.parse_args()

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    rng = random.Random(SEED)
    verdicts_path = pathlib.Path(args.out) if args.out else VERDICTS
    system = system_prompt(G5_LINE_STRICT if args.g5 == "strict" else G5_LINE_NO_OP)
    HERE.mkdir(parents=True, exist_ok=True)
    (HERE / f"prompt_{args.tag or 'main'}_system.txt").write_text(system, encoding="utf-8")

    if args.control:
        pool_rows = [r for r in read_jsonl(DROPPED) if r.get("audit_reason")
                     and r.get("pg_gold_sql") and r.get("db_id") in schema]
        sample = rng.sample(pool_rows, min(args.control_n, len(pool_rows)))
        sample.sort(key=lambda r: r["sample_id"])
        done_by_id = {r["sample_id"]: r for r in read_jsonl(verdicts_path)}
        results, failures = run(sample, schema, args.batch, args.workers, done_by_id,
                                args.tag or "control", system)
        all_rows = sorted(done_by_id.values(), key=lambda r: r["sample_id"])
        write_jsonl(verdicts_path, all_rows)
        print(json.dumps({"control_rows": len(all_rows), "new": len(results),
                          "unparsed": len(failures), "failures": failures[:3]}, ensure_ascii=False))
        return 0 if len(all_rows) >= len(sample) else 1

    corpus = read_jsonl(CORPUS)
    indices = sorted(rng.sample(range(len(corpus)), min(args.n, len(corpus))))
    sample = [corpus[i] for i in indices]
    manifest = {"seed": SEED, "corpus": str(CORPUS),
                "corpus_lines": len(corpus), "corpus_sha256": sha256_of(CORPUS),
                "draw": "random.Random(seed).sample(range(len(corpus)), n), indices sorted ascending",
                "n": len(sample),
                "sample_ids": [r["sample_id"] for r in sample]}
    (HERE / "sample_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                               encoding="utf-8")

    done_by_id = {r["sample_id"]: r for r in read_jsonl(verdicts_path)}
    results, failures = run(sample, schema, args.batch, args.workers, done_by_id,
                            args.tag or "main", system)
    all_rows = [done_by_id[r["sample_id"]] for r in sample if r["sample_id"] in done_by_id]
    write_jsonl(verdicts_path, all_rows)
    print(json.dumps({"sampled": len(sample), "with_verdict": len(all_rows),
                      "new": len(results), "unparsed": len(failures),
                      "failures": failures[:3]}, ensure_ascii=False))
    return 0 if len(all_rows) == len(sample) else 1


if __name__ == "__main__":
    sys.exit(main())
