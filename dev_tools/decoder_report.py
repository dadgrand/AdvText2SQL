"""Assemble the decoder numbers from the artifacts a grammar arm leaves behind.

Three different questions must not be conflated, and this script keeps them apart:

1. **Did the decoder constrain generation?**  Answered from the proxy log
   (``guided_proxy.py --log``): was every SQL request marked ``guided``, and does
   every completion sit inside the grammar (``check_grammar.py --proxy-log``)?
2. **Was the SQL usable?**  Two independent levels, because a grammar can only fix
   the first:
   * *parse validity* — does ``sqlglot`` parse the prediction as PostgreSQL
     (the tool's own gate), and did the tool have to give up (``"error"``)?
     A grammar makes this ~100 % by construction.
   * *execution* — did PostgreSQL actually run it (``n_pred_unexecutable`` from
     ``dev_tools.fixed_eval``)?  A grammar cannot fix a hallucinated column, so
     this number is expected to move much less.
3. **What did it cost?**  Latency per request (mean/p50/p95) and completion tokens,
   from the proxy log, next to the harness wall clock.

EX numbers are *not* recomputed here: they come from ``dev_tools/fixed_eval``
(same evaluator, same ``mode="local"`` semantics as every other number on this
branch), and the paired test comes from the same report when it was run with a
``--baseline``.

Usage::

    python -m dev_tools.decoder_report \
        --score norep=eval/r2-15-norep.json \
        --score grammar=eval/r2-15-grammar.json \
        --predictions norep=runs/t2/r2-15-norep-awq/query_results.json \
        --predictions grammar=runs/t2/r2-15-grammar-awq/query_results.json \
        --proxy-log norep=runs/t2/r2-15-norep-awq/llm_requests.jsonl \
        --proxy-log grammar=runs/t2/r2-15-grammar-awq/llm_requests.jsonl \
        --out eval/decoder_report.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

SQL_START = re.compile(r"^\s*(SELECT|WITH)\b")
SENTINELS = {"error", "ambiguous"}
FENCE = re.compile(r"```")
LOWERCASE_KEYWORD = re.compile(r"\b(select|from|where|group|order|by|join|as|and|or|having|limit)\b")


def _percentile(values: Sequence[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[index]


def _parses(sql: str) -> bool:
    import sqlglot  # noqa: PLC0415

    try:
        sqlglot.parse_one(sql, dialect="postgres")
        return True
    except Exception:
        return False


def parse_validity(predictions: Dict[str, Any]) -> Dict[str, Any]:
    """How many predictions are one parseable PostgreSQL statement (tool's gate)."""

    n = len(predictions)
    sentinel = {key: value for key, value in predictions.items() if value in SENTINELS}
    candidates = {key: value for key, value in predictions.items() if value not in SENTINELS}
    parse_fail = [key for key, value in candidates.items() if not isinstance(value, str) or not _parses(value)]
    return {
        "n_predictions": n,
        "n_error_sentinel": sum(1 for value in predictions.values() if value == "error"),
        "n_ambiguous_sentinel": sum(1 for value in predictions.values() if value == "ambiguous"),
        "n_parse_fail": len(parse_fail),
        "parse_fail_ids": parse_fail[:20],
        "parse_valid_rate": (len(candidates) - len(parse_fail)) / n if n else None,
        "invalid_sql_rate": (len(sentinel) + len(parse_fail)) / n if n else None,
        "definition": (
            "invalid = the tool returned the 'error'/'ambiguous' sentinel, or the prediction "
            "is not one sqlglot-parseable PostgreSQL statement; rate is over all questions"
        ),
    }


def decode_facts(log_path: Path) -> Dict[str, Any]:
    """What the model actually emitted, straight from the proxy's request log."""

    entries: List[Dict[str, Any]] = []
    with open(log_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    sql = [entry for entry in entries if entry.get("kind") == "sql"]
    ok = [entry for entry in sql if entry.get("ok")]
    completions = [entry.get("completion") or "" for entry in ok]
    latencies = [float(entry["latency_s"]) for entry in ok if entry.get("latency_s") is not None]
    tokens = [int(entry.get("completion_tokens") or 0) for entry in ok]
    return {
        "n_requests": len(entries),
        "n_sql_requests": len(sql),
        "n_sql_guided": sum(1 for entry in sql if entry.get("guided")),
        "n_sql_failed": len(sql) - len(ok),
        "n_finish_length": sum(1 for entry in ok if entry.get("finish_reason") == "length"),
        "n_with_fence": sum(1 for text in completions if FENCE.search(text)),
        "n_contains_semicolon": sum(1 for text in completions if ";" in text),
        "n_trailing_semicolon": sum(1 for text in completions if text.strip().endswith(";")),
        "n_not_starting_with_select": sum(1 for text in completions if not SQL_START.match(text)),
        "n_lowercase_keyword": sum(1 for text in completions if LOWERCASE_KEYWORD.search(text)),
        "completion_tokens_mean": (sum(tokens) / len(tokens)) if tokens else None,
        "completion_tokens_max": max(tokens) if tokens else None,
        "latency_s_mean": (sum(latencies) / len(latencies)) if latencies else None,
        "latency_s_p50": _percentile(latencies, 0.50),
        "latency_s_p95": _percentile(latencies, 0.95),
        "examples": {
            "fenced": [text[:120] for text in completions if FENCE.search(text)][:2],
            "prose": [text[:120] for text in completions if not SQL_START.match(text)][:2],
        },
    }


def arm_block(score_path: Path, predictions_path: Path, log_path: Optional[Path]) -> Dict[str, Any]:
    score = json.loads(score_path.read_text(encoding="utf-8"))
    predictions = json.loads(predictions_path.read_text(encoding="utf-8"))
    block: Dict[str, Any] = {
        "score_source": str(score_path),
        "predictions_source": str(predictions_path),
        "mode": score.get("mode"),
        "n_questions": score.get("n_questions"),
        "ex_all": score.get("ex_all"),
        "ex_gold_executable": score.get("ex_gold_executable"),
        "n_gold_unexecutable": score.get("n_gold_unexecutable"),
        "n_pred_unexecutable": score.get("n_pred_unexecutable"),
        "n_missing_predictions": score.get("n_missing_predictions"),
        "n_ambiguous": score.get("n_ambiguous"),
        "per_difficulty": score.get("per_difficulty"),
        "soft_f1_mean": score.get("soft_f1_mean"),
        "paired": score.get("paired"),
        "parse": parse_validity(predictions),
    }
    if log_path is not None:
        block["decode"] = decode_facts(log_path)
    return block


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dev_tools.decoder_report",
        description="Assemble decoder-side numbers (parse validity, decode facts, latency) per arm.",
    )
    parser.add_argument("--score", action="append", default=[], help="NAME=path to a fixed_eval report")
    parser.add_argument("--predictions", action="append", default=[], help="NAME=path to query_results.json")
    parser.add_argument("--proxy-log", action="append", default=[], help="NAME=path to the proxy JSONL")
    parser.add_argument("--out", help="write the assembled JSON here")
    return parser


def _pairs(items: Sequence[str]) -> Dict[str, str]:
    pairs: Dict[str, str] = {}
    for item in items:
        name, _, path = item.partition("=")
        if not path:
            raise SystemExit(f"expected NAME=path, got {item!r}")
        pairs[name] = path
    return pairs


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    scores = _pairs(args.score)
    predictions = _pairs(args.predictions)
    logs = _pairs(args.proxy_log)
    if not scores:
        raise SystemExit("at least one --score NAME=path is required")

    result: Dict[str, Any] = {"tool": "dev_tools.decoder_report", "arms": {}}
    for name, score_path in scores.items():
        if name not in predictions:
            raise SystemExit(f"arm {name!r} has a score but no --predictions {name}=...")
        log_path = Path(logs[name]) if name in logs else None
        result["arms"][name] = arm_block(Path(score_path), Path(predictions[name]), log_path)

    for name, block in result["arms"].items():
        ex = block.get("ex_all") or {}
        exe = block.get("ex_gold_executable") or {}
        parse = block["parse"]
        decode = block.get("decode") or {}
        print(f"=== arm {name} ===")
        print(f"  EX/241 {ex.get('correct')}/{ex.get('n')} = {_pct(ex.get('value'))}"
              f"   EX/209 {exe.get('correct')}/{exe.get('n')} = {_pct(exe.get('value'))}")
        print(f"  invalid SQL {parse['n_error_sentinel']} sentinel + {parse['n_parse_fail']} unparseable"
              f" = {_pct(parse['invalid_sql_rate'])}"
              f"   (POSTGRES-unexecutable: {block.get('n_pred_unexecutable')})")
        if decode:
            print(f"  requests {decode['n_requests']} (sql {decode['n_sql_requests']},"
                  f" guided {decode['n_sql_guided']}, failed {decode['n_sql_failed']})")
            print(f"  fences {decode['n_with_fence']}, prose-prefix {decode['n_not_starting_with_select']},"
                  f" ';' inside {decode['n_contains_semicolon']}, trailing ';'"
                  f" {decode['n_trailing_semicolon']}, lower-case keywords"
                  f" {decode['n_lowercase_keyword']}, hit cap {decode['n_finish_length']}")
            print(f"  latency mean {decode['latency_s_mean']:.2f} s, p50 {decode['latency_s_p50']:.2f} s,"
                  f" p95 {decode['latency_s_p95']:.2f} s; completion tokens mean"
                  f" {decode['completion_tokens_mean']:.1f}")
        if block.get("paired"):
            paired = block["paired"]
            # fixed_eval's block spells these `mcnemar_exact_p`, `delta_ex` and
            # `bootstrap95`; older reports used the shorter names, so accept both.
            p_value = paired.get("mcnemar_exact_p", paired.get("mcnemar_p"))
            delta = paired.get("delta_ex", paired.get("delta"))
            ci = paired.get("bootstrap95", paired.get("ci"))
            print(f"  paired vs {paired.get('baseline')}: b={paired.get('b')} c={paired.get('c')}"
                  f" p={p_value} delta={delta} ci={ci}")
    return 0


def _pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{100.0 * value:.1f}%"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
