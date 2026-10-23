"""Retry only connection failures, without regenerating any predictions."""

from datetime import datetime, timezone
import contextlib
import io
import json
import os
from pathlib import Path
import sys

from dotenv import dotenv_values


RUN_DIR = Path(__file__).resolve().parent
ROOT = RUN_DIR.parents[1]
config = dotenv_values(ROOT / ".env.openrouter")
for key in ("DB_USER", "DB_PASS"):
    os.environ[key] = config[key]
sys.path.insert(0, str(ROOT))

from benchmarks import evaluate_bird


original_create_engine = evaluate_bird.create_engine


def bounded_engine(*args, **kwargs):
    kwargs["connect_args"] = {"connect_timeout": 5}
    return original_create_engine(*args, **kwargs)


evaluate_bird.create_engine = bounded_engine
report = json.loads((RUN_DIR / "report.json").read_text(encoding="utf-8"))
predictions = json.loads((RUN_DIR / "query_results.json").read_text(encoding="utf-8"))
retry_ids = [
    row["question_id"]
    for row in report["results"]
    if "ConnectionTimeout" in row.get("error", "")
]
print("Re-evaluating connection failures only:", retry_ids, flush=True)
output_dir = RUN_DIR / "reevaluation_ipv4"
output_dir.mkdir(exist_ok=True)
os.chdir(output_dir)
captured = io.StringIO()
with contextlib.redirect_stdout(captured):
    retry_report = evaluate_bird.run_evaluation(
        {qid: predictions[qid] for qid in retry_ids},
        str(ROOT / "data" / "bird_small.json"),
        "127.0.0.1:5444",
    )
log = captured.getvalue()
for key in ("DB_PASS", "LLM_API_KEY"):
    if config.get(key):
        log = log.replace(config[key], "[REDACTED]")
(output_dir / "evaluation.log").write_text(log, encoding="utf-8")
replacement = {row["question_id"]: row for row in retry_report["results"]}
for qid in retry_ids:
    row = replacement[qid]
    print("Question", qid, "score", row["score"], "error", row.get("error", "none").splitlines()[0])

report["results"] = [replacement.get(row["question_id"], row) for row in report["results"]]
report["overall_accuracy"] = 100 * sum(row["score"] for row in report["results"]) / report["total"]
for difficulty in report["accuracy_by_difficulty"]:
    rows = [row for row in report["results"] if row["difficulty"] == difficulty]
    report["accuracy_by_difficulty"][difficulty] = 100 * sum(row["score"] for row in rows) / len(rows)
report["reevaluated_question_ids"] = retry_ids
report["reevaluation_db_url"] = "127.0.0.1:5444"
(RUN_DIR / "report_final.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
)
metadata_path = RUN_DIR / "metadata.json"
metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
metadata["initial_accuracy"] = metadata["accuracy"]
metadata["accuracy"] = report["overall_accuracy"]
metadata["final_report"] = "report_final.json"
metadata["reevaluated_at_utc"] = datetime.now(timezone.utc).isoformat()
metadata["reevaluated_question_ids"] = retry_ids
metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
evaluate_bird.print_evaluation_report(report)
