"""Score harness runs and compute the paired comparisons for the LoRA report.

Why this exists
---------------
``benchmarks/evaluate_bird.py`` (the shipped evaluator) creates a new SQLAlchemy
engine per question and connects with no timeout.  Over the SSH tunnel that is
the thing that hangs (measured: a ``bird_small`` run produced all 22 predictions
and then froze inside ``evaluate_bird.py:82`` until the tunnel's channel limit
was exhausted).  So the measurement path for this branch is:

    harness predictions -> dev_tools/fixed_eval.py (mode="local") -> this module

``fixed_eval`` already implements the shipped comparison (``set(pred) == set(gold)``
with the repo's own gold rewrite), EX/241, EX/211 (gold-executable) and a
per-question ledger.  This module adds the pieces the report needs:

* ``score bird``      — EX/241 + EX/211 + per-difficulty + per-question ledger,
  written as one JSON artifact;
* ``score ambrosia``  — ambiguity-detection counts next to the constant
  "never say ambiguous" floor (170/240 = 70.83 %), never called accuracy of the
  benchmark;
* ``compare``         — exact McNemar + seeded paired-bootstrap block between any
  two ledgers, plus the team's frozen baseline (``artifacts/pred_scores.json``,
  measured 42.32 % = 102/241) which contains per-question scores but no SQL.

Credentials come from ``repo/.env`` (``DB_CLIENT_HOST``/``DB_HOST``, ``DB_PORT``,
``DB_USER``, ``DB_PASS``); the password is passed to ``PostgresExecutor`` raw,
which percent-encodes it (it contains ``/``).

Commands::

    python -m dev_tools.score_runs score --kind bird \
        --predictions runs/t2/e1-bird-large/query_results.json \
        --answers data/bird_large.json \
        --out runs/t2/scores/e1-bird-large.json --db-url localhost:5444

    python -m dev_tools.score_runs score --kind ambrosia \
        --predictions runs/t2/e1-ambrosia-large/query_results.json \
        --answers data/ambrosia_large.json \
        --out runs/t2/scores/e1-ambrosia-large.json

    python -m dev_tools.score_runs compare \
        --current runs/t2/scores/e3-bird-large.json \
        --reference runs/t2/scores/e1-bird-large.json \
        --frozen ../artifacts/pred_scores.json \
        --out runs/t2/scores/paired-bird-e3-vs-e1.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

try:  # package import
    from . import fixed_eval, paths
except ImportError:  # pragma: no cover - direct script execution
    import fixed_eval  # type: ignore[no-redef]
    import paths  # type: ignore[no-redef]

__all__ = ["compare_ledgers", "load_env", "score_ambrosia", "score_bird"]

#: The "never say ambiguous" floor on Ambrosia large: 170 of 240 gold rows are not
#: ambiguous, so a tool that never answers "ambiguous" scores exactly this much.
AMBROSIA_NEVER_AMBIGUOUS = 170 / 240


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_env(env_path: Any = None) -> Dict[str, str]:
    """Read ``repo/.env`` (quoted values stripped) into a dict; does not mutate os.environ."""

    path = Path(env_path) if env_path else paths.DEFAULT_ENV
    values: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _checkpoint_args(env: Dict[str, str], db_url: Optional[str]):
    """Build a ``PostgresExecutor`` from the .env credentials (raw password)."""

    host, port = env.get("DB_CLIENT_HOST") or env.get("DB_HOST"), env.get("DB_PORT")
    user, password = env.get("DB_USER"), env.get("DB_PASS")
    missing = [name for name, value in (("host", host), ("port", port), ("user", user), ("password", password)) if not value]
    if missing:
        raise SystemExit(f"missing credentials in .env: {missing}")
    url = db_url or f"{host}:{port}"
    return fixed_eval.PostgresExecutor(url, user=user, password=password)


def _write(path: Any, payload: dict) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return target


def _ledger_items(report: dict) -> List[dict]:
    """Normalise any scored artifact to ``[{question_id, score}]`` for pairing."""

    if "items" in report:
        return [{"question_id": str(item["question_id"]), "score": int(item["score"])} for item in report["items"]]
    if "per_question" in report:
        return [
            {"question_id": str(item["question_id"]), "score": int(item["score"])}
            for item in report["per_question"]
        ]
    raise SystemExit("artifact has neither 'items' nor 'per_question'")


# ---------------------------------------------------------------------------
# BIRD: the shipped EX on the live PostgreSQL
# ---------------------------------------------------------------------------


def score_bird(
    predictions: Any,
    answers: Any,
    *,
    env: Dict[str, str],
    db_url: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict:
    """EX/241 + EX/211 for a predictions file, using ``fixed_eval`` mode="local".

    ``mode="local"`` is the shipped evaluator's semantics (gold through the repo's
    own ``sqlite_to_postgres`` regex), so the numbers are comparable with the
    team's frozen 42.32 % baseline.
    """

    answer_rows = fixed_eval._answers_rows(answers)
    if limit is not None:
        answer_rows = answer_rows[:limit]
    executor = _checkpoint_args(env, db_url)
    report = fixed_eval.score_predictions(
        predictions,
        answer_rows,
        mode="local",
        execute=executor,
        include_per_question=True,
    )
    report["kind"] = "bird"
    report["protocol"] = "course PostgreSQL harness semantics (benchmarks/evaluate_bird.py:90), scored by dev_tools/fixed_eval.py mode=local"
    report["items"] = _ledger_items(report)
    return report


# ---------------------------------------------------------------------------
# Ambrosia: ambiguity detection next to the constant floor
# ---------------------------------------------------------------------------


def score_ambrosia(predictions: Any, answers: Any, *, limit: Optional[int] = None) -> dict:
    """Ambiguity-detection table for Ambrosia (no SQL is executed — by design)."""

    answer_rows = fixed_eval._answers_rows(answers)
    if limit is not None:
        answer_rows = answer_rows[:limit]
    predictions_map = fixed_eval._load_json(predictions)
    if not isinstance(predictions_map, dict):
        raise SystemExit("predictions must be a mapping {question_id: sql-or-'ambiguous'}")

    per_question: List[dict] = []
    for row in answer_rows:
        qid = str(row.get("question_id"))
        gold_is_ambiguous = fixed_eval._is_ambiguous(row.get("SQL"))
        prediction = predictions_map.get(qid)
        predicted_is_ambiguous = fixed_eval._is_ambiguous(prediction)
        per_question.append(
            {
                "question_id": qid,
                "db_id": row.get("db_id"),
                "gold_ambiguous": gold_is_ambiguous,
                "predicted_ambiguous": predicted_is_ambiguous,
                "correct": int(gold_is_ambiguous == predicted_is_ambiguous),
                "missing_prediction": prediction is None,
            }
        )

    n = len(per_question)
    gold_ambiguous = sum(1 for item in per_question if item["gold_ambiguous"])
    predicted_ambiguous = sum(1 for item in per_question if item["predicted_ambiguous"])
    true_positive = sum(1 for item in per_question if item["gold_ambiguous"] and item["predicted_ambiguous"])
    correct = sum(item["correct"] for item in per_question)
    floor = (n - gold_ambiguous) / n if n else None
    return {
        "kind": "ambrosia",
        "protocol": "ambiguity detection (benchmarks/evaluate_ambrosia.py); no SQL is executed",
        "answers": str(answers) if isinstance(answers, (str, os.PathLike)) else "<in-memory>",
        "predictions": str(predictions) if isinstance(predictions, (str, os.PathLike)) else "<in-memory>",
        "n_questions": n,
        "gold_ambiguous": gold_ambiguous,
        "predicted_ambiguous": predicted_ambiguous,
        "true_positive": true_positive,
        "correct": correct,
        "detection_rate": (correct / n) if n else None,
        "never_ambiguous_floor": floor,
        "claims": {
            "gold_ambiguous_share": (gold_ambiguous / n) if n else None,
            "precision": (true_positive / predicted_ambiguous) if predicted_ambiguous else None,
            "recall": (true_positive / gold_ambiguous) if gold_ambiguous else None,
            "never_ambiguous_floor_documented": AMBROSIA_NEVER_AMBIGUOUS,
            "note": "never call this 'Ambrosia accuracy'; it is ambiguity detection vs a constant baseline",
        },
        "per_question": per_question,
        "items": [{"question_id": item["question_id"], "score": item["correct"]} for item in per_question],
    }


# ---------------------------------------------------------------------------
# Paired comparisons
# ---------------------------------------------------------------------------


def compare_ledgers(
    current: dict,
    reference: dict,
    *,
    n_boot: int = 10_000,
    seed: int = 0,
    labels: Sequence[str] = ("current", "reference"),
) -> dict:
    """b/c, exact McNemar p and paired-bootstrap CI between two ledgers."""

    current_items = _ledger_items(current)
    reference_items = _ledger_items(reference)
    current_by_id = {item["question_id"]: item["score"] for item in current_items}
    reference_by_id = {item["question_id"]: item["score"] for item in reference_items}
    shared = [item["question_id"] for item in current_items if item["question_id"] in reference_by_id]

    b = sum(1 for qid in shared if current_by_id[qid] == 1 and reference_by_id[qid] == 0)
    c = sum(1 for qid in shared if current_by_id[qid] == 0 and reference_by_id[qid] == 1)
    deltas = [current_by_id[qid] - reference_by_id[qid] for qid in shared]
    low, high = fixed_eval.paired_bootstrap_ci(deltas, n_boot=n_boot, seed=seed)

    current_correct = sum(current_by_id[qid] for qid in shared)
    reference_correct = sum(reference_by_id[qid] for qid in shared)
    return {
        "kind": "paired",
        "labels": list(labels),
        "n_paired": len(shared),
        "n_current_correct": current_correct,
        "n_reference_correct": reference_correct,
        "b": b,
        "c": c,
        "discordant": b + c,
        "mcnemar_exact_p": fixed_eval.mcnemar_exact_p(b, c),
        "delta_ex": (sum(deltas) / len(deltas)) if deltas else None,
        "bootstrap95": [low, high],
        "n_boot": n_boot,
        "seed": seed,
        "missing_in_reference": sorted(set(current_by_id) - set(reference_by_id)),
        "missing_in_current": sorted(set(reference_by_id) - set(current_by_id)),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dev_tools.score_runs",
        description="Score harness runs (BIRD EX on the live DB, Ambrosia ambiguity) and pair them.",
    )
    parser.add_argument("--env", default=str(paths.DEFAULT_ENV), help="credentials file (default: repo/.env)")
    sub = parser.add_subparsers(dest="command", required=True)

    score = sub.add_parser("score", help="score one predictions file")
    score.add_argument("--kind", choices=("bird", "ambrosia"), required=True)
    score.add_argument("--predictions", required=True, help="query_results.json of a harness run")
    score.add_argument("--answers", required=True, help="data/bird_large.json or data/ambrosia_large.json")
    score.add_argument("--out", required=True, help="artifact to write")
    score.add_argument("--db-url", default=None, help="host:port (default from .env)")
    score.add_argument("--limit", type=int, default=None, help="only the first N questions (debugging)")

    compare = sub.add_parser("compare", help="paired comparison between two scored artifacts")
    compare.add_argument("--current", required=True)
    compare.add_argument("--reference", required=True)
    compare.add_argument("--frozen", default=None, help="optional third ledger (e.g. artifacts/pred_scores.json)")
    compare.add_argument("--out", required=True)
    compare.add_argument("--n-boot", type=int, default=10_000)
    compare.add_argument("--seed", type=int, default=0)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    env = load_env(args.env)

    if args.command == "score":
        if args.kind == "bird":
            report = score_bird(
                args.predictions, args.answers, env=env, db_url=args.db_url, limit=args.limit
            )
        else:
            report = score_ambrosia(args.predictions, args.answers, limit=args.limit)
        target = _write(args.out, report)
        if args.kind == "bird":
            ex_all = report["ex_all"]
            ex_exec = report["ex_gold_executable"]
            print(
                f"EX/{ex_all['n']} = {ex_all['correct']}/{ex_all['n']} = {ex_all['percent']:.2f}%   "
                f"EX/{ex_exec['n']} (gold-executable) = {ex_exec['correct']}/{ex_exec['n']} = "
                f"{ex_exec['percent']:.2f}%   gold unexecutable={report['n_gold_unexecutable']}"
            )
        else:
            print(
                f"n={report['n_questions']} correct={report['correct']} "
                f"detection={report['detection_rate']:.4f} floor={report['never_ambiguous_floor']:.4f} "
                f"gold_ambiguous={report['gold_ambiguous']} predicted_ambiguous={report['predicted_ambiguous']}"
            )
        print(f"artifact: {target}")
        return 0

    current = json.loads(Path(args.current).read_text(encoding="utf-8"))
    reference = json.loads(Path(args.reference).read_text(encoding="utf-8"))
    paired = compare_ledgers(
        current,
        reference,
        n_boot=args.n_boot,
        seed=args.seed,
        labels=(str(args.current), str(args.reference)),
    )
    payload: Dict[str, Any] = {"kind": "paired-comparison", "primary": paired}
    if args.frozen:
        frozen = json.loads(Path(args.frozen).read_text(encoding="utf-8"))
        payload["against_frozen"] = compare_ledgers(
            current, frozen, n_boot=args.n_boot, seed=args.seed, labels=(str(args.current), str(args.frozen))
        )
    target = _write(args.out, payload)
    print(
        f"paired vs reference: n={paired['n_paired']} b={paired['b']} c={paired['c']} "
        f"p={paired['mcnemar_exact_p']:.6g} delta={paired['delta_ex']} CI={paired['bootstrap95']}"
    )
    if "against_frozen" in payload:
        frozen_paired = payload["against_frozen"]
        print(
            f"paired vs frozen  : n={frozen_paired['n_paired']} b={frozen_paired['b']} c={frozen_paired['c']} "
            f"p={frozen_paired['mcnemar_exact_p']:.6g} delta={frozen_paired['delta_ex']} CI={frozen_paired['bootstrap95']}"
        )
    print(f"artifact: {target}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


