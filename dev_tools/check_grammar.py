"""Acceptance test for the GBNF SELECT grammar used by the guided proxy.

Two questions this script answers, both with the grammar *compiled by xgrammar*
(the same engine vLLM uses at serving time, so no second implementation can
disagree):

1. **Is the grammar usable?**  How long does it take to compile, and does it
   accept the SQL the benchmark itself considers gold?  A grammar that rejects a
   third of ``bird_large`` would be measuring the grammar's gaps, not the model.
2. **Did the decoder actually constrain?**  Replay every completion the model
   produced (``guided_proxy.py --log``) through the compiled grammar.  If any
   completion is rejected, the "guided" arm was not guided and the delta means
   nothing.

xgrammar is not a dependency of this repo (it ships with vLLM, on Linux), so the
script fails with one explicit sentence instead of an ImportError traceback.

Usage::

    # coverage + compile time
    python -m dev_tools.check_grammar --sql-file data/bird_large.json --mode postgres

    # did the guided arm stay inside the grammar?
    python -m dev_tools.check_grammar --proxy-log runs/<run-id>/llm_requests.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

GRAMMAR_PATH = Path(__file__).with_name("grammars") / "postgres_select.gbnf"


def _tokenizer_info(xgr: Any, tokenizer: Optional[str]) -> Any:
    """The vocabulary xgrammar compiles against.

    With ``--tokenizer`` the real serving tokenizer is used (what vLLM will use,
    so the acceptance result cannot disagree with the server).  Without it a
    byte-level stand-in is built: xgrammar needs *a* vocabulary to compile, but
    string acceptance is a property of the grammar, not of the vocabulary.
    """

    if tokenizer:
        from transformers import AutoTokenizer  # noqa: PLC0415 (only needed here)

        hf = AutoTokenizer.from_pretrained(tokenizer)
        return xgr.TokenizerInfo.from_huggingface(hf, vocab_size=len(hf))
    return xgr.TokenizerInfo([bytes([b]) for b in range(256)], xgr.VocabType.BYTE_LEVEL)


def _load_grammar(path: Path, tokenizer: Optional[str] = None) -> Any:
    try:
        import xgrammar as xgr  # noqa: PLC0415 (optional, Linux/vLLM only)
    except ImportError as exc:  # pragma: no cover - depends on the machine
        raise SystemExit(
            "xgrammar is not installed in this environment: the grammar checker must run where "
            f"vLLM does (pip install xgrammar, or use the vLLM venv). Original error: {exc}"
        ) from exc
    started = time.time()
    grammar = xgr.Grammar.from_ebnf(path.read_text(encoding="utf-8"))
    compiled = xgr.GrammarCompiler(_tokenizer_info(xgr, tokenizer)).compile_grammar(grammar)
    return compiled, xgr, time.time() - started


def accepts(grammar: Any, xgr: Any, text: str) -> bool:
    """True when ``text`` is a complete sentence of the grammar."""
    matcher = xgr.GrammarMatcher(grammar)
    try:
        if not matcher.accept_string(text):
            return False
        return bool(matcher.is_completed())
    except Exception:
        return False


def _gold_sql(sql: str, mode: str) -> str:
    """Gold in the form the harness executes (``local``/``postgres``) or verbatim."""
    if mode == "raw":
        return sql
    from . import fixed_eval  # noqa: PLC0415

    if mode == "postgres":  # exactly the shipped evaluator's rewrite
        return fixed_eval.repo_rewrite(sql)
    return fixed_eval.sqlite_to_postgres_safe(sql)


def check_sql_file(grammar: Any, xgr: Any, path: Path, mode: str, limit: int) -> Dict[str, Any]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    results: List[Dict[str, Any]] = []
    for row in rows:
        sql = row.get("SQL") or ""
        try:
            candidate = _gold_sql(sql, mode)
        except Exception as exc:
            results.append({"question_id": row.get("question_id"), "ok": False, "error": f"rewrite: {exc}"})
            continue
        ok = accepts(grammar, xgr, candidate)
        results.append({"question_id": row.get("question_id"), "ok": ok, "sql": candidate})
    accepted = sum(1 for item in results if item["ok"])
    return {
        "source": str(path),
        "mode": mode,
        "n": len(results),
        "accepted": accepted,
        "rejected": results[:0] + [item for item in results if not item["ok"]][:limit],
        "rejected_total": len(results) - accepted,
    }


def check_proxy_log(grammar: Any, xgr: Any, path: Path, kind: str, limit: int) -> Dict[str, Any]:
    entries: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    selected = [entry for entry in entries if entry.get("kind") == kind and entry.get("ok")]
    failures: List[Dict[str, Any]] = []
    for entry in selected:
        text = entry.get("completion") or ""
        if not accepts(grammar, xgr, text):
            failures.append({"ts": entry.get("ts"), "completion": text[:400]})
    return {
        "source": str(path),
        "kind": kind,
        "n_requests": len(selected),
        "n_guided": sum(1 for entry in selected if entry.get("guided")),
        "n_inside_grammar": len(selected) - len(failures),
        "n_outside_grammar": len(failures),
        "outside": failures[:limit],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dev_tools.check_grammar",
        description="Compile the SELECT grammar with xgrammar and measure what it accepts.",
    )
    parser.add_argument("--grammar", default=str(GRAMMAR_PATH))
    parser.add_argument(
        "--tokenizer",
        default=None,
        help="HF tokenizer (path or id) to compile the grammar against; default is a "
        "byte-level stand-in. Use the serving tokenizer to match vLLM exactly.",
    )
    parser.add_argument("--sql-file", help="JSON rows with a SQL field, e.g. data/bird_large.json")
    parser.add_argument(
        "--mode",
        choices=("raw", "postgres", "safe"),
        default="postgres",
        help="postgres = the shipped evaluator's rewrite (what the harness runs); raw = verbatim",
    )
    parser.add_argument("--proxy-log", help="guided_proxy JSONL; checks every kind==--kind completion")
    parser.add_argument("--kind", default="sql")
    parser.add_argument("--limit", type=int, default=10, help="how many rejects to print")
    parser.add_argument("--out", help="write the JSON result here")
    parser.add_argument(
        "--min-accept",
        type=float,
        default=None,
        help="exit 1 when the gold acceptance rate is below this (e.g. 0.95)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    grammar, xgr, compile_s = _load_grammar(Path(args.grammar), args.tokenizer)
    print(f"grammar {args.grammar}: compiled in {compile_s:.2f} s (xgrammar)")

    payload: Dict[str, Any] = {"grammar": args.grammar, "compile_s": round(compile_s, 3)}
    code = 0

    if args.sql_file:
        result = check_sql_file(grammar, xgr, Path(args.sql_file), args.mode, args.limit)
        payload["gold"] = result
        rate = result["accepted"] / result["n"] if result["n"] else 0.0
        print(
            f"gold acceptance ({args.mode}): {result['accepted']}/{result['n']} = {rate:.1%}"
        )
        for item in result["rejected"]:
            print(f"  rejected id {item.get('question_id')}: {item.get('sql') or item.get('error')}")
        if args.min_accept is not None and rate < args.min_accept:
            print(f"FAIL: acceptance {rate:.1%} < --min-accept {args.min_accept:.1%}")
            code = 1

    if args.proxy_log:
        result = check_proxy_log(grammar, xgr, Path(args.proxy_log), args.kind, args.limit)
        payload["decoder"] = result
        print(
            f"decoder check ({result['kind']} requests): {result['n_inside_grammar']}/"
            f"{result['n_requests']} completions inside the grammar "
            f"({result['n_guided']} of them requested guided)"
        )
        for item in result["outside"]:
            print(f"  outside: {item['completion']}")
        if result["n_requests"] and result["n_inside_grammar"] != result["n_requests"]:
            print("FAIL: at least one completion is outside the grammar, so the arm was not guided")
            code = 1

    if args.out:
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"written to {args.out}")
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
