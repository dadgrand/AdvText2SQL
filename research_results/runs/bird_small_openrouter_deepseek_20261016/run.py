"""Run the existing BIRD pipeline on small, with isolated baseline artifacts."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv


RUN_DIR = Path(__file__).resolve().parent
ROOT = RUN_DIR.parents[1]
load_dotenv(ROOT / ".env.openrouter", override=True)
secrets = [os.environ.get(key, "") for key in ("DB_PASS", "LLM_API_KEY")]


def redact(value):
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    return value


class RedactedTee:
    def __init__(self, stream, log):
        self.stream = stream
        self.log = log

    def write(self, value):
        safe = redact(value)
        self.stream.write(safe)
        self.log.write(safe)
        self.flush()
        return len(value)

    def flush(self):
        self.stream.flush()
        self.log.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


os.environ["DATASET_SIZE"] = "small"
for number in range(1, 37):
    os.environ[f"FEAT_{number}"] = "false"
os.environ["OPTIMISTIC_AMBIGUITY_FALLBACK"] = "false"

sys.path[:0] = [str(ROOT), str(ROOT / "src")]
os.chdir(RUN_DIR)
log = (RUN_DIR / "bird.log").open("a", encoding="utf-8")
sys.stdout = RedactedTee(sys.stdout, log)
sys.stderr = RedactedTee(sys.stderr, log)

from benchmarks.bird import BenchmarkBIRD
from benchmarks.evaluate_bird import print_evaluation_report
from adv_text2sql.mcp_servers.text2sql_tool.src.text2sql_implementation import (
    Text2SQLGenerator,
    setup_logging,
)


from autogen_ext.models.openai import OpenAIChatCompletionClient

setup_logging()
dataset = ROOT / "data" / "bird_small.json"
metadata = {
    "status": "running",
    "started_at_utc": datetime.now(timezone.utc).isoformat(),
    "git_commit": subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip(),
    "model": os.environ["LLM_MODEL_NAME"],
    "base_url": os.environ["LLM_BASE_URL"],
    "dataset": "data/bird_small.json",
    "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
    "questions": 22,
    "feature_flags": {f"FEAT_{number}": False for number in range(1, 37)},
    "use_evidence": True,
    "temperature": 0,
    "max_output_tokens": 2048,
    "reasoning_enabled": False,
    "provider_routing": "OpenRouter automatic",
    "env_file": ".env.openrouter",
    "max_retries_effective": 1,
    "base_interval_seconds": float(os.environ.get("LLM_BASE_INTERVAL", "2")),
    "checkpoint": "query_results.json",
}


def save_metadata():
    (RUN_DIR / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )


save_metadata()
print(f"BIRD small baseline: {metadata['model']}, 22 questions, all FEAT flags false.")
print(f"Artifacts: {RUN_DIR}")
client = OpenAIChatCompletionClient(
    model=os.environ["LLM_MODEL_NAME"],
    base_url=os.environ["LLM_BASE_URL"],
    api_key=os.environ["LLM_API_KEY"],
    temperature=0,
    max_tokens=2048,
    max_retries=0,
    timeout=60,
    extra_body={"reasoning": {"enabled": False}},
    model_info={
        "json_output": False,
        "function_calling": True,
        "vision": False,
        "family": "unknown",
        "structured_output": False,
    },
)
benchmark = BenchmarkBIRD(
    db_url="localhost:5444",
    query_file=str(dataset),
    answer_file=str(dataset),
    use_evidence=True,
    llm_client=client,
)

async def run():
    try:
        return await benchmark.run(Text2SQLGenerator)
    finally:
        await client.close()


try:
    report = asyncio.run(run())
    (RUN_DIR / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print_evaluation_report(report)
    usage = benchmark.llm_client.get_usage()
    metadata["usage"] = usage
    metadata["prompt_plus_completion_tokens"] = (
        usage["prompt_tokens"] + usage["completion_tokens"]
    )
    metadata["status"] = "completed"
    metadata["accuracy"] = report["overall_accuracy"]
    print("Token usage:", usage)
except BaseException as error:
    metadata["status"] = "failed"
    metadata["error"] = redact(str(error))
    raise
finally:
    metadata["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    save_metadata()
