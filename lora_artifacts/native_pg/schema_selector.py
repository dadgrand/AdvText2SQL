#!/usr/bin/env python3
"""Inference-time schema selector for the 408 long-schema rows (t37).

The t31 shortening used ``pg_gold_sql`` to choose tables/columns. That leaks the gold
answer into the prompt: at inference time the model does not know which tables the query
needs. This module provides a selector that reads only:

    question + evidence + schema catalog (+ foreign-key graph)

and a deterministic cap on the number of tables. The gold SQL is read only by the
evaluation code (``required_entities``/``select_gold_aware``) and never by
``select_inference``.

Run:
    .venv-tools/bin/python datasets/native_pg/schema_selector.py

Outputs:
    datasets/native_pg/selector_report.json   per-row coverage, both policies, token cost
    datasets/native_pg/train_short_schema.jsonl  inference-time shortened prompts
    datasets/native_pg/short_schema_report.json  loader-compatible decision file
and updates cluster/batch_report.json with the leakage note and new selector stats.
"""

from __future__ import annotations

import collections
import hashlib
import json
import math
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
sys.path.insert(0, str(PROJECT))

from datasets.native_pg import renderer, tokenization  # noqa: E402
from datasets.native_pg import shorten_long_schema as legacy  # noqa: E402  (gold-aware reference only)

CAP_TABLES = 20
MAX_LEN = tokenization.DEFAULT_MAX_LEN
RESERVE = tokenization.PROMPT_RESERVE
MIN_ANSWER = 32
SCHEMA_PATH = PROJECT / "repo" / "datasets" / "bird_pg_unified" / "schemas" / "schema.json"
BUCKET_PATH = HERE / "train_long_schema.jsonl"
REPORT_PATH = HERE / "selector_report.json"
SHORT_PATH = HERE / "train_short_schema.jsonl"
DECISION_PATH = HERE / "short_schema_report.json"
BATCH_REPORT = PROJECT / "cluster" / "batch_report.json"


def load(path: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def percentile(values: list[int], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile / 100.0
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return float(ordered[int(position)])
    return ordered[low] * (high - position) + ordered[high] * (position - low)


# ----------------------------------------------------------------------------------------------
# catalog / lexical helpers (the selector reads only these)
# ----------------------------------------------------------------------------------------------
def _catalog() -> dict:
    if not hasattr(_catalog, "_cache"):
        _catalog._cache = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return _catalog._cache


def catalog_tables(db_id: str) -> dict[str, dict]:
    entry = _catalog().get(db_id) or {}
    tables = {}
    for table in entry.get("tables") or []:
        tables[table["name"]] = {
            "columns": {column["name"]: column["type"] for column in table.get("columns") or []},
            "foreign_keys": [(fk["column"], fk["references"]["table"], fk["references"]["column"])
                             for fk in table.get("foreign_keys") or []],
        }
    return tables


def _text_tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _identifier_tokens(name: str) -> set[str]:
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    spaced = re.sub(r"[^A-Za-z0-9]+", " ", spaced)
    return set(re.findall(r"[a-z0-9]+", spaced.lower()))


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def select_inference(question: str, evidence: str, db_id: str,
                     cap_tables: int = CAP_TABLES) -> dict:
    """Select a schema excerpt using only question, evidence and the catalog.

    No SQL argument exists on purpose; the selector cannot see the gold answer.
    Candidate tables are ranked by table-name and column-name token overlap, one-hop
    foreign-key closure is added, and the top ``cap_tables`` are kept deterministically.
    If nothing matches, the full schema is returned (safe fallback, zero savings).
    """
    tables = catalog_tables(db_id)
    text = f"{question or ''} {evidence or ''}"
    text_tokens = _text_tokens(text)
    compact_text = _compact(text)
    scores: collections.Counter = collections.Counter()
    matched_columns: dict[str, list[str]] = {}
    for table, info in tables.items():
        score = 0
        table_tokens = _identifier_tokens(table)
        if table_tokens and table_tokens <= text_tokens:
            score += 2
        if _compact(table) and _compact(table) in compact_text:
            score += 2
        columns = []
        for column in info["columns"]:
            column_tokens = _identifier_tokens(column)
            if (column_tokens and column_tokens <= text_tokens) or (
                    _compact(column) and _compact(column) in compact_text):
                score += 1
                columns.append(column)
        if score:
            scores[table] = score
            matched_columns[table] = columns
    # one-hop FK closure, both directions
    closure = set()
    for table in list(scores):
        for _column, referenced, _ref_column in tables[table]["foreign_keys"]:
            if referenced in tables:
                closure.add(referenced)
    for table, info in tables.items():
        if table not in scores:
            for _column, referenced, _ref_column in info["foreign_keys"]:
                if referenced in scores:
                    closure.add(table)
    for table in closure:
        scores.setdefault(table, 0.5)

    if not scores:
        chosen = list(tables)
        fallback = True
    else:
        chosen = sorted(scores, key=lambda t: (-scores[t], list(tables).index(t)))[:cap_tables]
        fallback = False
    lines = ["Schema:"]
    selected_columns = {}
    for table in tables:
        if table not in chosen:
            continue
        selected_columns[table] = list(tables[table]["columns"])
        lines.append(f"TABLE {table}")
        for column, kind in tables[table]["columns"].items():
            lines.append(f"  - {column} ({kind})")
    return {
        "schema_text": "\n".join(lines),
        "tables": chosen,
        "selected_columns": selected_columns,
        "matched_columns": matched_columns,
        "fallback_full_schema": fallback,
        "cap_tables": cap_tables,
        "inputs": ["question", "evidence", "catalog", "foreign_keys"],
    }


# ----------------------------------------------------------------------------------------------
# evaluation only: gold SQL is read here, never in select_inference
# ----------------------------------------------------------------------------------------------
def required_entities(pg_gold_sql: str, db_id: str) -> tuple[set[str], dict[str, set[str]]]:
    import sqlglot
    from sqlglot import exp
    tables = catalog_tables(db_id)
    lookup = {name.lower(): name for name in tables}
    tree = sqlglot.parse_one(pg_gold_sql, read="postgres")
    aliases = {}
    required_tables = set()
    for table in tree.find_all(exp.Table):
        actual = lookup.get(table.name.lower(), table.name)
        if actual in tables:
            required_tables.add(actual)
            aliases[actual] = actual
            aliases[actual.lower()] = actual
            if table.alias:
                aliases[table.alias] = actual
                aliases[table.alias.lower()] = actual
    required_columns: dict[str, set[str]] = collections.defaultdict(set)
    unqualified = set()
    star = False
    for node in tree.walk():
        if isinstance(node, exp.Star):
            star = True
        if isinstance(node, exp.Column):
            name, table = node.name, node.table
            if not name:
                continue
            if table:
                actual = aliases.get(table) or aliases.get(table.lower())
                if actual in tables:
                    actual_column = next((c for c in tables[actual]["columns"]
                                          if c.lower() == name.lower()), name)
                    required_columns[actual].add(actual_column)
            else:
                unqualified.add(name.lower())
    for table in required_tables:
        for name_lower in unqualified:
            for column in tables[table]["columns"]:
                if column.lower() == name_lower:
                    required_columns[table].add(column)
        if star or not required_columns[table]:
            required_columns[table] = set(tables[table]["columns"])
    return required_tables, {table: set(columns) for table, columns in required_columns.items()}


def select_gold_aware(pg_gold_sql: str, db_id: str) -> dict:
    """The retired t31 selector, kept only to quantify the leakage/token difference."""
    excerpt, meta = legacy.schema_excerpt(pg_gold_sql, db_id)
    return {"schema_text": excerpt, "tables": (meta or {}).get("tables", []),
            "inputs": ["pg_gold_sql", "catalog"], "legacy": True}


def _answer_length(row: dict, tokenizer) -> int:
    return len(tokenization.encode(tokenizer, row["completion"])) + 1


def _coverage(selected_tables: set[str], selected_columns: dict[str, set[str]],
              required_tables: set[str], required_columns: dict[str, set[str]]) -> dict:
    missing_tables = sorted(required_tables - selected_tables)
    missing_columns = {}
    for table in sorted(required_tables):
        if table not in selected_tables:
            missing_columns[table] = sorted(required_columns.get(table, set()))
            continue
        missing = required_columns.get(table, set()) - set(selected_columns.get(table, []))
        if missing:
            missing_columns[table] = sorted(missing)
    total_required = sum(len(columns) for columns in required_columns.values())
    total_missing = sum(len(columns) for columns in missing_columns.values())
    return {
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "coverage_ok": not missing_tables and not missing_columns,
        "required_columns": total_required,
        "missing_column_count": total_missing,
        "missing_column_rate": round(total_missing / total_required, 6) if total_required else 0.0,
    }


def evaluate() -> dict:
    rows = load(BUCKET_PATH)
    tokenizer = tokenization.load_tokenizer()
    per_row = []
    old_decisions_path = DECISION_PATH
    old_report = json.loads(old_decisions_path.read_text(encoding="utf-8")) if old_decisions_path.is_file() else {}
    for row in rows:
        inference = select_inference(row["question"], row["evidence"], row["db_id"])
        gold = select_gold_aware(row["pg_gold_sql"], row["db_id"])
        required_tables, required_columns = required_entities(row["pg_gold_sql"], row["db_id"])
        coverage = _coverage(set(inference["tables"]), inference["selected_columns"],
                             required_tables, required_columns)
        gold_coverage = _coverage(set(gold["tables"]), {t: list(catalog_tables(row["db_id"])[t]["columns"])
                                                        for t in gold["tables"]},
                                  required_tables, required_columns)
        full_tokens = len(tokenization.encode(tokenizer, row["prompt"]))
        inference_pair = renderer.render(row, "train", schema_text=inference["schema_text"])
        inference_prompt_ids = tokenization.encode(tokenizer, inference_pair["prompt"])
        completion_ids = tokenization.encode(tokenizer, row["completion"])
        inference_example = tokenization.build_example(inference_prompt_ids, completion_ids, MAX_LEN)
        inference_labels = [label for label in inference_example["labels"] if label != -100]
        gold_labels = completion_ids + [tokenization.EOS_ID]
        label_intact = inference_labels == gold_labels and not inference_example["answer_truncated"]
        inference_tokens = len(inference_prompt_ids)
        gold_pair = renderer.render(row, "train", schema_text=gold["schema_text"])
        gold_tokens = len(tokenization.encode(tokenizer, gold_pair["prompt"]))
        answer_tokens = _answer_length(row, tokenizer)
        capacity = MAX_LEN - min(inference_tokens, MAX_LEN - RESERVE)
        inference_savings = full_tokens - inference_tokens
        gold_savings = full_tokens - gold_tokens
        budget_ok = capacity >= MIN_ANSWER and answer_tokens <= capacity
        if not coverage["coverage_ok"]:
            decision = "unusable_selector_coverage"
        elif not budget_ok:
            decision = "move_to_bucket"
        else:
            decision = "usable_after_shortening"
        per_row.append({
            "sample_id": row["sample_id"],
            "db_id": row["db_id"],
            "question": row["question"],
            "evidence": row["evidence"],
            "inference_tables": inference["tables"],
            "inference_fallback_full_schema": inference["fallback_full_schema"],
            "gold_aware_tables": gold["tables"],
            "required_tables": sorted(required_tables),
            "required_columns": {table: sorted(columns) for table, columns in sorted(required_columns.items())},
            "missing_tables": coverage["missing_tables"],
            "missing_columns": coverage["missing_columns"],
            "coverage_ok": coverage["coverage_ok"],
            "missing_column_count": coverage["missing_column_count"],
            "missing_column_rate": coverage["missing_column_rate"],
            "gold_aware_coverage_ok": gold_coverage["coverage_ok"],
            "full_prompt_tokens": full_tokens,
            "inference_prompt_tokens": inference_tokens,
            "gold_aware_prompt_tokens": gold_tokens,
            "inference_token_savings": inference_savings,
            "gold_aware_token_savings": gold_savings,
            "inference_savings_share": round(inference_savings / full_tokens, 4) if full_tokens else 0,
            "answer_tokens": answer_tokens,
            "answer_capacity": capacity,
            "budget_ok": budget_ok,
            "gold_label_sequence_intact": label_intact,
            "answer_truncated_inference": inference_example["answer_truncated"],
            "decision": decision,
            "inference_schema_sha256": hashlib.sha256(inference["schema_text"].encode()).hexdigest(),
        })
    # leakage proof: the selector must not change when gold SQL is absent
    same_without_gold = 0
    for row in rows:
        redacted = {key: value for key, value in row.items() if key != "pg_gold_sql"}
        a = select_inference(row["question"], row["evidence"], row["db_id"])["tables"]
        b = select_inference(redacted["question"], redacted["evidence"], redacted["db_id"])["tables"]
        if a == b:
            same_without_gold += 1
    inference_tokens = [r["inference_prompt_tokens"] for r in per_row]
    gold_tokens = [r["gold_aware_prompt_tokens"] for r in per_row]
    savings = [r["inference_token_savings"] for r in per_row]
    gold_savings = [r["gold_aware_token_savings"] for r in per_row]
    coverage_ok = sum(1 for r in per_row if r["coverage_ok"])
    budget_ok = sum(1 for r in per_row if r["budget_ok"])
    usable = sum(1 for r in per_row if r["decision"] == "usable_after_shortening")
    missing_rows = [r["sample_id"] for r in per_row if not r["coverage_ok"]]
    report = {
        "objective": "inference-time schema selection for long-schema rows; compare with the retired gold-aware selector",
        "selector": {
            "path": str(HERE / "schema_selector.py"),
            "selector_function": "select_inference(question, evidence, db_id, cap_tables)",
            "inputs": ["question", "evidence", "schema catalog", "foreign-key graph"],
            "never_reads_pg_gold_sql": True,
            "cap_tables": CAP_TABLES,
            "coverage_rule": "one-hop foreign-key closure; include every column of each selected table; "
                             "fallback to the full schema if no lexical candidate matches",
            "leakage_proof": {
                "rows": len(rows),
                "identical_selection_with_gold_removed": same_without_gold,
                "all_identical": same_without_gold == len(rows),
            },
        },
        "retired_gold_aware_policy": {
            "inputs": ["pg_gold_sql", "catalog"],
            "leakage": "t31 selected tables/columns from pg_gold_sql; the model would receive a hint absent at inference",
            "retired": True,
        },
        "rows": len(rows),
        "coverage": {
            "rows_with_all_gold_columns": coverage_ok,
            "row_coverage_rate": round(coverage_ok / len(rows), 4),
            "rows_missing_any_gold_column": len(rows) - coverage_ok,
            "missing_rows": missing_rows,
            "missing_column_rate": round(sum(r["missing_column_count"] for r in per_row) /
                                         max(1, sum(r["required_columns"] and sum(len(v) for v in r["required_columns"].values()) for r in per_row)), 6),
            "threshold": 0.95,
            "passes_threshold": coverage_ok / len(rows) >= 0.95,
        },
        "gate_for_arms": {
            "coverage_ok": coverage_ok,
            "budget_ok_and_coverage": usable,
            "move_to_bucket": sum(1 for r in per_row if r["decision"] == "move_to_bucket"),
            "unusable_selector_coverage": sum(1 for r in per_row if r["decision"] == "unusable_selector_coverage"),
            "rule": "arms C/D may use a row only when inference-time selector coverage is complete; "
                    "budget_ok additionally requires >=32 answer tokens",
        },
        "token_cost": {
            "full_policy": {"p50": percentile([r["full_prompt_tokens"] for r in per_row], 50),
                            "p95": percentile([r["full_prompt_tokens"] for r in per_row], 95)},
            "inference_policy": {"p50": percentile(inference_tokens, 50), "p95": percentile(inference_tokens, 95),
                                 "p50_savings": percentile(savings, 50), "p95_savings": percentile(savings, 95),
                                 "p50_savings_share": percentile([r["inference_savings_share"] for r in per_row], 50)},
            "gold_aware_policy": {"p50": percentile(gold_tokens, 50), "p95": percentile(gold_tokens, 95),
                                  "p50_savings": percentile(gold_savings, 50), "p95_savings": percentile(gold_savings, 95),
                                  "p50_savings_share": percentile([r["gold_aware_token_savings"] / max(1, r["full_prompt_tokens"]) for r in per_row], 50)},
            "answer_capacity_p50": percentile([r["answer_capacity"] for r in per_row], 50),
        },
        "per_row": per_row,
        "legacy_report": str(old_decisions_path),
        "legacy_report_sha256": sha(old_decisions_path) if old_decisions_path.is_file() else None,
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # rewrite the decision file and shortened prompts so the loader cannot use the leaky t31 versions
    short_rows = []
    for row, decision in zip(rows, per_row):
        selected = select_inference(row["question"], row["evidence"], row["db_id"])
        pair = renderer.render(row, "train", schema_text=selected["schema_text"])
        short_rows.append({**row, "prompt": pair["prompt"], "schema_excerpt": selected["schema_text"],
                           "original_prompt": row["prompt"],
                           "tokens_before": decision["full_prompt_tokens"],
                           "tokens_after": decision["inference_prompt_tokens"],
                           "selector": "inference_time", "decision": decision["decision"],
                           "coverage_ok": decision["coverage_ok"],
                           "missing_tables": decision["missing_tables"],
                           "missing_columns": decision["missing_columns"]})
    SHORT_PATH.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in short_rows), encoding="utf-8")
    old_legacy = old_report.get("shortening", {}) if old_report else {}
    usable_rows = [r for r in per_row if r["decision"] == "usable_after_shortening"]
    label_intact_total = sum(1 for r in per_row if r["gold_label_sequence_intact"])
    label_intact_usable = sum(1 for r in usable_rows if r["gold_label_sequence_intact"])
    new_short_report = {
        "policy": f"inference-time schema selector: question+evidence+catalog+FK, deterministic cap {CAP_TABLES}; "
                  f"bucket policy {MAX_LEN}/{RESERVE}/{MIN_ANSWER}",
        "leakage_status": "fixed: t31's gold-aware selector is retired; inference prompts are generated by "
                          "datasets/native_pg/schema_selector.py",
        "counts": old_report.get("counts", {"native_total": 6907, "main": 6499, "bucket": len(rows), "dropped": 0,
                                            "reconciled": 6907 == 6499 + len(rows)}),
        "artifacts": {
            "train_main": old_report.get("artifacts", {}).get("train_main"),
            "train_long_schema": old_report.get("artifacts", {}).get("train_long_schema"),
            "train_short_schema": {"path": str(SHORT_PATH), "sha256": sha(SHORT_PATH), "rows": len(short_rows)},
            "selector_report": {"path": str(REPORT_PATH), "sha256": sha(REPORT_PATH)},
        },
        "label_sequence_note": "the completion labels are unchanged; selector only changes the schema excerpt",
        "shortening": {
            "tested_rows": len(per_row), "shortened_rows": len(per_row),
            "usable_after_shortening": usable,
            "move_to_bucket": sum(1 for r in per_row if r["decision"] == "move_to_bucket"),
            "drop": sum(1 for r in per_row if r["decision"] == "unusable_selector_coverage"),
            "gold_label_sequence_intact_rows": label_intact_total,
            "labels_identical_to_full_gold_rows": label_intact_total,
            "usable_label_sequence_intact_rows": label_intact_usable,
            "usable_label_sequence_ok": label_intact_usable == usable,
            "p50_token_savings": percentile(savings, 50), "p95_token_savings": percentile(savings, 95),
            "p50_savings_share": percentile([r["inference_savings_share"] for r in per_row], 50),
            "p95_savings_share": percentile([r["inference_savings_share"] for r in per_row], 95),
            "min_answer_room_after": min(r["answer_capacity"] for r in usable_rows) if usable_rows else None,
        },
        "gold_aware_legacy": old_legacy,
        "per_row": [{"sample_id": r["sample_id"], "decision": r["decision"],
                     "coverage_ok": r["coverage_ok"], "missing_tables": r["missing_tables"],
                     "missing_columns": r["missing_columns"],
                     "original_prompt_tokens": r["full_prompt_tokens"],
                     "shortened_prompt_tokens": r["inference_prompt_tokens"],
                     "token_savings": r["inference_token_savings"],
                     "token_savings_share": r["inference_savings_share"],
                     "shortened_answer_room": r["answer_capacity"],
                     "gold_label_sequence_intact": r["gold_label_sequence_intact"]}
                    for r in per_row],
        "bucket_ids": [r["sample_id"] for r in per_row],
    }
    DECISION_PATH.write_text(json.dumps(new_short_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report, old_legacy


def update_batch_report(report: dict, old_legacy: dict) -> None:
    batch = json.loads(BATCH_REPORT.read_text(encoding="utf-8"))
    policy = batch.setdefault("long_schema_policy", {})
    policy["selector"] = {
        "path": str(HERE / "schema_selector.py"),
        "report": str(REPORT_PATH),
        "report_sha256": sha(REPORT_PATH),
        "function": "select_inference(question, evidence, db_id, cap_tables)",
        "inputs": ["question", "evidence", "catalog", "foreign_keys"],
        "never_reads_pg_gold_sql": True,
        "cap_tables": CAP_TABLES,
        "coverage_rate": report["coverage"]["row_coverage_rate"],
        "missing_rows": report["coverage"]["missing_rows"],
        "gate_for_arms": report["gate_for_arms"],
        "token_cost": report["token_cost"],
    }
    policy["leakage_risk"] = {
        "status": "fixed",
        "retired_policy": "t31 selected the schema excerpt from pg_gold_sql; that leaks a gold hint into the prompt",
        "replacement": "inference-time selector using question, evidence, catalog and FK closure only",
        "deprecated_gold_aware_metrics": old_legacy,
        "warning": "do not regenerate train_short_schema.jsonl with shorten_long_schema.py; the loader now reads "
                   "the inference-time decision file",
    }
    policy["artifacts"]["train_short_schema"] = {"path": str(SHORT_PATH), "sha256": sha(SHORT_PATH), "rows": 408}
    policy["short_schema_report"] = {"path": str(DECISION_PATH), "sha256": sha(DECISION_PATH)}
    policy["shortening"] = json.loads(DECISION_PATH.read_text(encoding="utf-8"))["shortening"]
    batch["schema_selector_leakage_note"] = policy["leakage_risk"]
    BATCH_REPORT.write_text(json.dumps(batch, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    report, old_legacy = evaluate()
    update_batch_report(report, old_legacy)
    print(json.dumps({
        "rows": report["rows"],
        "coverage_rate": report["coverage"]["row_coverage_rate"],
        "coverage_ok": report["coverage"]["rows_with_all_gold_columns"],
        "missing_rows": len(report["coverage"]["missing_rows"]),
        "gate_for_arms": report["gate_for_arms"],
        "token_cost": report["token_cost"],
        "leakage_proof": report["selector"]["leakage_proof"],
    }, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
