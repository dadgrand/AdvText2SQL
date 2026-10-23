"""Isolated baseline or fixed hybrid; launch through local/run_openrouter_baseline.sh."""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import urllib.request
from urllib.parse import quote

from dotenv import load_dotenv
from feature_presets import feature_flags
from research_paths import configurations_match, resolve_artifact_path

ROOT = Path(__file__).resolve().parents[1]


class Tee:
    def __init__(self, stream, log, secrets):
        self.stream, self.log, self.secrets = stream, log, secrets

    def write(self, value):
        for secret in self.secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        self.stream.write(value)
        self.log.write(value)
        self.flush()
        return len(value)

    def flush(self):
        self.stream.flush()
        self.log.flush()


def save(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    temporary.replace(path)


def key_usage():
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/key",
        headers={"Authorization": "Bearer " + os.environ["LLM_API_KEY"]},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.load(response)["data"]
    return {key: data.get(key) for key in ("usage", "limit", "limit_remaining")}


async def run(args):
    load_dotenv(ROOT / ".env.openrouter", override=True)
    if os.environ.get("LLM_BASE_URL", "").rstrip("/") != "https://openrouter.ai/api/v1":
        raise RuntimeError("Set LLM_BASE_URL=https://openrouter.ai/api/v1 in .env.openrouter")
    flags = feature_flags(args.configuration)
    for flag, enabled in flags.items():
        os.environ[flag] = str(enabled).lower()
    retries = 5 if args.configuration != "baseline" else 1
    os.environ.update(OPTIMISTIC_AMBIGUITY_FALLBACK="false", MAX_RETRIES=str(retries),
                      LLM_BASE_INTERVAL="3", LLM_MIN_INTERVAL="3")
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]

    from autogen_core.models import UserMessage
    from autogen_ext.models.openai import OpenAIChatCompletionClient
    from benchmarks.bird import BenchmarkBIRD
    from benchmarks.ambrosia import BenchmarkAmbrosia
    from benchmarks.token_tracking import TokenTrackingClient
    from baseline_connection import build_tools, database_engine
    from ambrosia_subset import prepare_subset
    from adv_text2sql.mcp_servers.text2sql_tool.src import text2sql_implementation as implementation

    prompt_info = None
    if args.ambiguity_prompt == "main":
        from prompt_versions import load_main_prompt
        prompt, prompt_info = load_main_prompt(ROOT)
        implementation.AMBIGUITY_PROMPT_SIMPLE = prompt

    class BaselineGenerator(implementation.Text2SQLGenerator):
        def __init__(self, db_uri, llm_client):
            super().__init__(db_uri=db_uri, llm_client=llm_client)
            self.engine.dispose()
            self.engine = database_engine(db_uri)

    default_output = {
        "baseline": "research_results/runs/gpt54mini_baseline_large_20261016",
        "hybrid": "research_results/runs/gpt54mini_hybrid_large_20261016",
        "hybrid_no32_33": ("research_results/runs/gpt54mini_hybrid_bird_no32_33_50_20261023"
                           if args.bird_ambiguous_source
                           else "research_results/runs/gpt54mini_hybrid_bird_no32_33_full_20261023"),
    }[args.configuration]
    output = resolve_artifact_path(ROOT, args.output or default_output).resolve()
    if not args.dry_run:
        output.mkdir(parents=True, exist_ok=True)
    # Individual benchmark folders must never share their checkpoint.
    names = ["bird", "ambrosia"] if args.benchmark == "both" else [args.benchmark]
    for name in names:
        subset = None
        bird_selection = None
        if name == "ambrosia" and args.ambrosia_subset == "available227":
            dataset, subset = prepare_subset(ROOT)
            directory = output / "ambrosia_227"
        else:
            dataset = ROOT / "data" / f"{name}_large.json"
            directory = output / name
        if not args.dry_run:
            directory.mkdir(exist_ok=True)
        if name == "bird" and args.bird_ambiguous_source:
            from bird_subset import select_ambiguous
            items, bird_selection = select_ambiguous(ROOT, args.bird_ambiguous_source)
            source_config = bird_selection["source_configuration"]
            if args.model != source_config["model"] or args.ambiguity_prompt != "current":
                raise ValueError("Diagnostic must preserve source model and current prompt")
            dataset = directory / "dataset.json"
            dataset_content = json.dumps(items, ensure_ascii=False, indent=2)
            dataset_sha256 = hashlib.sha256(dataset_content.encode("utf-8")).hexdigest()
        else:
            items = json.loads(dataset.read_text(encoding="utf-8"))
            dataset_sha256 = hashlib.sha256(dataset.read_bytes()).hexdigest()
        identity = {
            "model": args.model, "reasoning_effort": "none", "temperature": 0,
            "max_output_tokens": 4096,
            "dataset_sha256": dataset_sha256,
            "feature_flags": flags,
            "use_evidence": name == "bird",
        }
        if prompt_info is not None:
            identity["ambiguity_prompt"] = prompt_info
        if args.configuration != "baseline":
            identity["max_retries"] = retries
        if bird_selection is not None:
            identity["diagnostic_selection"] = bird_selection
        if args.dry_run:
            print(json.dumps({"benchmark": name, "configuration": args.configuration,
                              "questions": len(items), "databases": len({x['db_id'] for x in items}),
                              "model": args.model, "output": str(directory),
                              "enabled_features": [f for f, enabled in flags.items() if enabled],
                              "max_retries": retries, "ambiguity_prompt": args.ambiguity_prompt}, indent=2))
            continue
        metadata_path = directory / "metadata.json"
        if metadata_path.exists():
            previous = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
            if not configurations_match(ROOT, previous["configuration"], identity):
                raise RuntimeError("Output configuration differs; choose a new --output folder")
            if previous["status"] == "completed":
                print(f"{name}: already completed; skipping", flush=True)
                continue
            # Preserve billing and failure information from every prior attempt.
            history = directory / "attempt_history.jsonl"
            with history.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(previous, ensure_ascii=False) + "\n")
        elif (directory / "query_results.json").exists():
            raise RuntimeError("Checkpoint without metadata; choose a new --output folder")
        if bird_selection is not None:
            dataset.write_bytes(dataset_content.encode("utf-8"))
            save(directory / "selection_manifest.json", bird_selection)

        metadata = {"configuration": identity, "status": "running", "questions": len(items),
                    "started_at_utc": datetime.now(timezone.utc).isoformat(),
                    "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()}
        metadata["dataset"] = str(dataset.relative_to(ROOT))
        metadata["feature_configuration"] = args.configuration
        metadata["max_retries"] = retries
        metadata["base_interval_seconds"] = 3
        metadata["min_interval_seconds"] = 3
        if subset is not None:
            metadata["dataset_coverage"] = subset
        if prompt_info is not None:
            save(directory / "ambiguity_prompt.json", {**prompt_info, "template": prompt})
        stdout, stderr = sys.stdout, sys.stderr
        with (directory / f"{name}.log").open("a", encoding="utf-8") as log:
            secrets = [os.environ.get(key, "") for key in ("DB_PASS", "LLM_API_KEY")]
            secrets += [quote(value, safe="") for value in secrets if value]
            sys.stdout, sys.stderr = Tee(stdout, log, secrets), Tee(stderr, log, secrets)
            client = OpenAIChatCompletionClient(
                model=args.model, base_url=os.environ["LLM_BASE_URL"], api_key=os.environ["LLM_API_KEY"],
                temperature=0, reasoning_effort="none", max_tokens=4096, max_retries=2, timeout=90,
                model_info={"json_output": False, "function_calling": True, "vision": False,
                            "family": "unknown", "structured_output": False},
            )
            benchmark = None
            tools = {}

            class RecordingClient:
                async def create(self, *call_args, **call_kwargs):
                    response = await client.create(*call_args, **call_kwargs)
                    messages = call_args[0] if call_args else call_kwargs.get("messages", [])
                    record = {
                        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                        "messages": [{"type": message.type, "content": message.content} for message in messages],
                        "raw_response": response.content,
                        "prompt_tokens": response.usage.prompt_tokens,
                        "completion_tokens": response.usage.completion_tokens,
                    }
                    with (directory / "raw_responses.jsonl").open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                    return response

            os.chdir(directory)
            try:
                implementation.setup_logging()
                metadata["key_before"] = key_usage()
                save(metadata_path, metadata)
                print(f"Starting {name}: {len(items)} questions, {args.model}, {args.configuration}", flush=True)
                print("Enabled features:", [f for f, enabled in flags.items() if enabled], flush=True)
                cls = BenchmarkBIRD if name == "bird" else BenchmarkAmbrosia
                kwargs = {"use_evidence": True} if name == "bird" else {}
                benchmark = cls(db_url="127.0.0.1:5444", query_file=str(dataset),
                                answer_file=str(dataset), llm_client=client, **kwargs)
                benchmark.llm_client = TokenTrackingClient(RecordingClient())

                def progress(update):
                    metadata.update(update)
                    save(metadata_path, metadata)

                tools = await build_tools(items, BaselineGenerator, benchmark.llm_client, progress)
                # Schema preparation is free of LLM calls; test the provider only once it succeeds.
                await client.create([UserMessage(content="Reply with exactly OK.", source="user")],
                                    extra_create_args={"max_tokens": 16})
                progress({"phase": "predicting"})
                predictions = await benchmark.predict(tools)
                if set(predictions) != {str(item["question_id"]) for item in items}:
                    raise RuntimeError("Prediction coverage differs from dataset")
                progress({"phase": "evaluating"})
                report = await benchmark.evaluate(predictions)
                report["generation_errors"] = sum(value == "error" for value in predictions.values())
                if name == "ambrosia":
                    gold = {str(item["question_id"]): item["SQL"] == "ambiguous" for item in items}
                    tp = sum(gold[q] and v == "ambiguous" for q, v in predictions.items())
                    fp = sum(not gold[q] and v == "ambiguous" for q, v in predictions.items())
                    fn = sum(gold[q] and v != "ambiguous" for q, v in predictions.items())
                    tn = sum(not gold[q] and v != "ambiguous" for q, v in predictions.items())
                    report["ambiguity_metrics"] = {
                        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
                        "precision": tp / (tp + fp) if tp + fp else None,
                        "recall": tp / (tp + fn) if tp + fn else None,
                        "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0,
                        "balanced_accuracy": (tp / (tp + fn) + tn / (tn + fp)) / 2,
                    }
                if subset is not None:
                    report["dataset_coverage"] = {
                        "source_questions": subset["source_questions"],
                        "evaluated_questions": len(predictions),
                        "excluded_questions": len(subset["excluded"]),
                        "dataset_sha256": subset["dataset_sha256"],
                    }
                save(directory / "report.json", report)
                if bird_selection is not None:
                    from bird_subset import compare_subset
                    save(directory / "comparison.json", compare_subset(ROOT, directory, bird_selection, report))
                metadata["status"] = "completed"
                metadata["phase"] = "completed"
                print("Report:", {k: v for k, v in report.items() if k != "results"}, flush=True)
            except BaseException as error:
                message = str(error)
                for secret in secrets:
                    if secret:
                        message = message.replace(secret, "[REDACTED]")
                metadata.update(status="failed", error_type=type(error).__name__, error_message=message)
                print(f"Baseline stopped: {type(error).__name__}: {message}", flush=True)
                raise
            finally:
                if benchmark is not None and hasattr(benchmark.llm_client, "get_usage"):
                    usage = benchmark.llm_client.get_usage()
                    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
                    metadata["usage_this_attempt"] = usage
                try:
                    metadata["key_after"] = key_usage()
                    metadata["cost_usd_this_attempt"] = metadata["key_after"]["usage"] - metadata["key_before"]["usage"]
                except Exception as error:
                    metadata["cost_snapshot_error"] = type(error).__name__
                metadata["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
                save(metadata_path, metadata)
                for tool in tools.values():
                    tool.engine.dispose()
                await client.close()
                logger = logging.getLogger("text2sql_tool")
                for handler in list(logger.handlers):
                    logger.removeHandler(handler)
                    handler.close()
                sys.stdout, sys.stderr = stdout, stderr
                os.chdir(ROOT)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", choices=["bird", "ambrosia", "both"], default="both")
    parser.add_argument("--model", default="openai/gpt-5.4-mini")
    parser.add_argument("--configuration", choices=["baseline", "hybrid", "hybrid_no32_33"], default="baseline")
    parser.add_argument("--bird-ambiguous-source", default=None,
                        help="Completed hybrid BIRD folder; rerun exactly its 50 ambiguous predictions")
    parser.add_argument("--dry-run", action="store_true", help="Print configuration without API or database calls")
    parser.add_argument("--ambiguity-prompt", choices=["current", "main"], default="current",
                        help="Change only the ambiguity prompt; preserve other current baseline settings")
    parser.add_argument("--ambrosia-subset", choices=["available227", "full240"], default="available227",
                        help="Ambrosia defaults to the explicit historical 227-question subset")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    if args.bird_ambiguous_source and (args.benchmark != "bird" or args.configuration != "hybrid_no32_33"):
        parser.error("--bird-ambiguous-source requires --benchmark bird --configuration hybrid_no32_33")
    if args.configuration == "hybrid_no32_33" and args.benchmark != "bird":
        parser.error("hybrid_no32_33 is a BIRD experiment; use --benchmark bird")
    asyncio.run(run(args))
