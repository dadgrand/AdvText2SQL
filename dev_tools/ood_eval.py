"""One command for an out-of-distribution evaluation: report plus per-database table.

Why this exists: OOD numbers were being produced by hand — copy a run, write a
throw-away script, remember that ``mode="local"`` re-transpiles PostgreSQL gold,
then rebuild a per-database table in a notebook. That is three chances to publish a
number nobody can reproduce. This runner does it in one call, and it reuses the
shipped scoring path (``score_runs.score_bird`` -> ``fixed_eval.score_predictions``)
rather than re-implementing EX.

    python -m dev_tools.ood_eval --predictions runs/ood/ood-scale7b-v2/query_results.json \
        --gold runs/ood/ood_heldout.json --out runs/ood/scores/ood-scale7b-v2.json

It writes the ``fixed_eval`` report (per-question records included) plus a
``per_database`` block, and prints the markdown table. With no database available,
``--predictions``/``--gold`` can be scored by passing an executor from a test
(``evaluate(..., executor=...)``), which is how ``tests/test_ood_eval.py`` stays
offline.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

if __package__ in (None, ""):  # ``python dev_tools/ood_eval.py`` as a script
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev_tools import fixed_eval, paths, score_runs  # noqa: E402

__all__ = ["evaluate", "per_database", "render_markdown", "main", "build_parser"]


def _rows(report: dict) -> list[dict]:
    rows = report.get("per_question")
    if rows is None:
        raise SystemExit("the fixed_eval report has no per_question records")
    return rows


def per_database(report: dict) -> list[dict]:
    """Aggregate one fixed_eval report per database, in a stable order."""

    buckets: Dict[str, dict] = {}
    for row in _rows(report):
        db = row["db_id"]
        bucket = buckets.setdefault(
            db,
            {"db_id": db, "n": 0, "correct": 0, "gold_executable": 0, "correct_gold_executable": 0,
             "pred_unexecutable": 0, "gold_unexecutable": 0, "soft_f1_sum": 0.0, "soft_f1_n": 0},
        )
        score = int(row["score"])
        bucket["n"] += 1
        bucket["correct"] += score
        if row.get("gold_ok"):
            bucket["gold_executable"] += 1
            bucket["correct_gold_executable"] += score
        elif row.get("status") == "gold_error":
            bucket["gold_unexecutable"] += 1
        if row.get("status") == "pred_error":
            bucket["pred_unexecutable"] += 1
        if row.get("soft_f1") is not None:
            bucket["soft_f1_sum"] += float(row["soft_f1"])
            bucket["soft_f1_n"] += 1

    table = []
    for db in sorted(buckets):
        bucket = buckets[db]
        bucket["ex_percent"] = 100.0 * bucket["correct"] / bucket["n"] if bucket["n"] else None
        bucket["ex_percent_gold_executable"] = (
            100.0 * bucket["correct_gold_executable"] / bucket["gold_executable"]
            if bucket["gold_executable"] else None
        )
        bucket["soft_f1_mean"] = bucket["soft_f1_sum"] / bucket["soft_f1_n"] if bucket["soft_f1_n"] else None
        table.append(bucket)
    return table


def render_markdown(table: Sequence[dict], *, title: str = "") -> str:
    """The per-database table as markdown, ready to paste into a report."""

    lines = []
    if title:
        lines += [f"**{title}**", ""]
    lines.append("| database | n | EX | EX % | EX % (gold-executable) | unexec. preds | soft-F1 |")
    lines.append("|---|---|---|---|---|---|---|")
    for row in table:
        ex_pct = f"{row['ex_percent']:.1f}" if row["ex_percent"] is not None else "—"
        ex_exec = f"{row['ex_percent_gold_executable']:.1f}" if row["ex_percent_gold_executable"] is not None else "—"
        f1 = f"{row['soft_f1_mean']:.4f}" if row["soft_f1_mean"] is not None else "—"
        lines.append(
            f"| {row['db_id']} | {row['n']} | {row['correct']}/{row['n']} | {ex_pct} | {ex_exec} | "
            f"{row['pred_unexecutable']} | {f1} |"
        )
    return "\n".join(lines)


def evaluate(
    predictions: Any,
    gold: Any,
    *,
    env: Optional[Dict[str, str]] = None,
    db_url: Optional[str] = None,
    mode: str = "local",
    executor: Optional[Callable[[str, Optional[str]], list]] = None,
) -> dict:
    """Score ``predictions`` against ``gold`` and add the per-database block.

    ``executor`` is for tests and for callers that already hold a connection; without
    it the executor is built from ``.env`` exactly like every other scored run.
    """

    if executor is None:
        env = env if env is not None else score_runs.load_env(paths.DEFAULT_ENV)
    report = score_runs.score_bird(
        predictions, gold, env=env or {}, db_url=db_url, mode=mode, executor=executor
    )
    table = per_database(report)
    if not table:
        raise SystemExit("the gold file produced no scorable questions (empty file or unmatched ids)")
    report["per_database"] = table
    report["ex_all_n"] = len(_rows(report))
    report["ex_all_correct"] = sum(int(row["score"]) for row in _rows(report))
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--predictions", required=True, help="predictions JSON: {question_id: sql}")
    parser.add_argument("--gold", required=True, help="gold JSON: rows with question_id, db_id, question, SQL")
    parser.add_argument("--out", default=None, help="write the report here (default: alongside the predictions)")
    parser.add_argument("--markdown", default=None, help="also write the per-database markdown table here")
    parser.add_argument("--db-url", default=None, help="default: host:port from .env")
    parser.add_argument("--mode", default="local", choices=("local", "normalized", "fixed"),
                        help="fixed_eval rewrite mode for gold and predictions (default local)")
    parser.add_argument("--env", default=None, help="path to .env (default: the repo's)")
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    executor: Optional[Callable[[str, Optional[str]], list]] = None,
) -> int:
    """CLI entry point. ``executor`` exists so tests can run the real argparse path offline."""

    args = build_parser().parse_args(argv)
    env = score_runs.load_env(args.env or paths.DEFAULT_ENV) if executor is None else {}
    report = evaluate(
        args.predictions, args.gold, env=env, db_url=args.db_url, mode=args.mode, executor=executor
    )

    out = Path(args.out) if args.out else Path(args.predictions).with_name(
        f"{Path(args.predictions).stem}.ood-eval.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
                   encoding="utf-8")

    heading = f"{Path(args.gold).name} ({args.mode})"
    table = render_markdown(report["per_database"])
    print(f"**{heading}**\n")
    print(table)
    ex_all = report["ex_all"]
    ex_exec = report["ex_gold_executable"]
    print(f"\nEX {ex_all['correct']}/{ex_all['n']} = {ex_all['percent']:.2f} %   "
          f"EX/gold-executable {ex_exec['correct']}/{ex_exec['n']} = {ex_exec['percent']:.2f} %")
    if args.markdown:
        Path(args.markdown).parent.mkdir(parents=True, exist_ok=True)
        Path(args.markdown).write_text(table + "\n", encoding="utf-8")
    print(f"artifact: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
