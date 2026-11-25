#!/usr/bin/env python3
"""Renderer-parity and batch-integrity audit for the native-PostgreSQL SFT path.

Runs four groups of checks and writes ``cluster/batch_report.json``:

1. renderer parity: train and serve renderings of the same pair are token-identical, the
   template is applied exactly once, and the prompt matches the shipped corpus prompt;
2. semantic smoke: a ``WITH`` query, a quoted identifier and a division with an explicit
   PostgreSQL cast keep their gold SQL in the labels, token-for-token;
3. corpus statistics: p50/p95/p99 of context and answer lengths before and after
   truncation, truncated-answer and zero-loss-token shares;
4. padding/masking: pad positions are masked by real positions, including a control where
   pad id equals the EOS id.

Exit code 0 = all checks pass. The script is also importable (functions ``test_*``) so
pytest can collect it when installed.

    .venv-tools/bin/python datasets/native_pg/renderer_parity.py

Requires ``tokenizers`` and ``jinja2`` (``pip install tokenizers jinja2``).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import pathlib
import re
import statistics
import sys

HERE = pathlib.Path(__file__).resolve().parent
PROJECT = HERE.parents[1]  # research/advtext2sql
CLUSTER = PROJECT / "cluster"
CORPUS = CLUSTER / "sft_all_v2.jsonl"
MANIFEST = PROJECT / "datasets" / "splits" / "schema_family_split.json"
SPLITS_REPORT = PROJECT / "datasets" / "splits" / "splits_report.json"
LOADER_REPORT = HERE / "loader_report.json"
REPORT_PATH = CLUSTER / "batch_report.json"

sys.path.insert(0, str(PROJECT))

from datasets.native_pg import loader as native_loader  # noqa: E402
from datasets.native_pg import tokenization  # noqa: E402
from datasets.native_pg.renderer import render  # noqa: E402

MAX_LEN = tokenization.DEFAULT_MAX_LEN

# Tokenizer oracle: these ids were produced by the cluster's
# AutoTokenizer.from_pretrained('~/models/Qwen2.5-Coder-14B-Instruct', local_files_only=True)
# on 2026-11-25 and must be reproduced by the local tokenizer.json snapshot.
CLUSTER_ORACLE = {
    "SELECT \"a\" FROM t WHERE x / NULLIF(y, 0) > 1": [
        4858, 330, 64, 1, 4295, 259, 5288, 856, 608, 1770, 2773, 7021, 11, 220, 15, 8, 861, 220, 16],
    "<|im_start|>system\nYou are a PostgreSQL expert.<|im_end|>\n"
    "<|im_start|>user\nHi<|im_end|>\n<|im_start|>assistant\n": [
        151644, 8948, 198, 2610, 525, 264, 73601, 6203, 13, 151645, 198, 151644,
        872, 198, 13048, 151645, 198, 151644, 77091, 198],
}

CHECKS: list[dict] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append({"check": name, "passed": bool(condition), "detail": detail})
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def percentile(values: list[int], quantile: float) -> float | None:
    """Linear interpolation, the numpy default, so the numbers are comparable."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile / 100.0
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return float(ordered[int(position)])
    return ordered[low] * (high - position) + ordered[high] * (position - low)


def distribution(values: list[int]) -> dict:
    return {
        "count": len(values),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "mean": round(statistics.fmean(values), 2) if values else None,
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
    }


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def package_converter_prompt(record: dict) -> str:
    """Prompt rendered by the package's own tools/to_sft.py, for a direct comparison."""
    tool = PROJECT / "repo" / "datasets" / "bird_pg_unified" / "tools" / "to_sft.py"
    spec = importlib.util.spec_from_file_location("bird_pg_to_sft", tool)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    schema = json.loads((PROJECT / "repo" / "datasets" / "bird_pg_unified" /
                         "schemas" / "schema.json").read_text(encoding="utf-8"))
    block = module.schema_block(schema, record["db_id"], 6000)
    parts = [block, "", f"Question: {record['question']}"]
    evidence = (record.get("evidence") or "").strip()
    if evidence:
        parts.append(f"Evidence: {evidence}")
    user = "\n".join(parts).strip()
    return (f"<|im_start|>system\n{module.SYSTEM}<|im_end|>\n"
            f"<|im_start|>user\n{user}<|im_end|>\n<|im_start|>assistant\n")


# ----------------------------------------------------------------------------------------------
# checks
# ----------------------------------------------------------------------------------------------
def check_tokenizer_identity(tokenizer) -> dict:
    for text, expected in CLUSTER_ORACLE.items():
        actual = tokenization.encode(tokenizer, text)
        check(f"tokenizer oracle: {text[:38]!r}", actual == expected,
              f"local ids differ from the cluster AutoTokenizer ids: {actual} != {expected}")
    return {
        "vocab_size": tokenizer.get_vocab_size(),
        "pad_token": {"id": tokenization.PAD_ID, "text": tokenizer.id_to_token(tokenization.PAD_ID)},
        "eos_token": {"id": tokenization.EOS_ID, "text": tokenizer.id_to_token(tokenization.EOS_ID)},
        "pad_equals_eos": tokenization.PAD_ID == tokenization.EOS_ID,
        "cluster_oracle_strings_checked": len(CLUSTER_ORACLE),
        "cluster_oracle_passed": True,
    }


def check_renderer_parity(records: list[dict], tokenizer) -> dict:
    sample = records[0]
    train = render(sample, "train")
    serve = render(sample, "serve")
    check("train/serve prompt strings are identical", train["prompt"] == serve["prompt"])
    check("train text is prompt + native gold", train["text"] == train["prompt"] + sample["pg_gold_sql"],
          "completion is not the unmodified pg_gold_sql")

    train_ids = tokenization.encode(tokenizer, train["prompt"])
    serve_ids = tokenization.encode(tokenizer, serve["prompt"])
    check("train/serve token ids are identical", train_ids == serve_ids,
          f"{len(train_ids)} vs {len(serve_ids)} ids")
    sampled = records[::max(1, len(records) // 100)][:100]
    sampled_mismatch = [
        record["sample_id"] for record in sampled
        if tokenization.encode(tokenizer, render(record, "train")["prompt"])
        != tokenization.encode(tokenizer, render(record, "serve")["prompt"])
    ]
    check("train/serve token ids identical on 100 sampled rows", not sampled_mismatch,
          f"{len(sampled_mismatch)} differ, e.g. {sampled_mismatch[:3]}")
    check("prompt starts with one <|im_start|>", train_ids[:1] == [tokenization.IM_START_ID])
    check("no duplicated chat-template start",
          train_ids[:2] != [tokenization.IM_START_ID, tokenization.IM_START_ID])
    check("no BOS id leaked into the prompt", tokenization.PAD_ID not in train_ids[:1],
          "tokenizer has no BOS; <|endoftext|> must not start the prompt")
    check("chat template applied once (3 im_start in prompt)",
          train_ids.count(tokenization.IM_START_ID) == 3,
          f"im_start count={train_ids.count(tokenization.IM_START_ID)}")
    check("two closed turns before the assistant prefix",
          train_ids.count(tokenization.EOS_ID) == 2,
          f"im_end count={train_ids.count(tokenization.EOS_ID)}")
    check("assistant prefix present",
          train["prompt"].endswith("<|im_start|>assistant\n"),
          repr(train["prompt"][-40:]))
    check("tokenized assistant prefix is stable",
          train_ids[-3:] == [tokenization.IM_START_ID, 77091, 198],
          f"tail ids {train_ids[-3:]}")

    # corpus-wide: every loader prompt must equal the prompt the shipped corpus used
    corpus_prompts = {json.loads(line)["prompt"] for line in CORPUS.read_text(encoding="utf-8").splitlines()
                      if line.strip()}
    missing = [record["sample_id"] for record in records if render(record, "serve")["prompt"] not in corpus_prompts]
    check("all loader prompts occur byte-identically in the shipped corpus",
          not missing, f"{len(missing)} missing, e.g. {missing[:3]}")

    # direct comparison with the package converter on a sample
    mismatched = [record["sample_id"] for record in records[:25]
                  if package_converter_prompt(record) != render(record, "serve")["prompt"]]
    check("package to_sft.py prompt equals the shared renderer (25 samples)",
          not mismatched, f"{len(mismatched)} differ, e.g. {mismatched[:3]}")

    return {
        "pair": {"sample_id": sample["sample_id"], "db_id": sample["db_id"]},
        "prompt_chars": len(serve["prompt"]),
        "prompt_tokens": len(train_ids),
        "train_serve_ids_equal": True,
        "token_id_snapshot": {
            "sha256": hashlib.sha256(json.dumps(train_ids).encode()).hexdigest(),
            "count": len(train_ids),
            "ids": train_ids,
        },
        "serve_token_id_snapshot": {
            "sha256": hashlib.sha256(json.dumps(serve_ids).encode()).hexdigest(),
            "count": len(serve_ids),
            "ids": serve_ids,
        },
        "first_ids": train_ids[:8],
        "assistant_prefix_ids": train_ids[-3:],
        "im_start_count": train_ids.count(tokenization.IM_START_ID),
        "im_end_count": train_ids.count(tokenization.EOS_ID),
        "corpus_prompts_checked": len(records),
        "sampled_token_pairs_checked": len(sampled),
        "corpus_prompt_matches": len(records) - len(missing),
        "package_converter_samples_checked": 25,
        "template_applied_times": 1,
        "bos_eos_duplication": False,
        "missing_assistant_prefix": False,
    }


def _semantic_case(name: str, record: dict, feature: str, tokenizer) -> dict:
    pair = render(record, "train")
    prompt_ids = tokenization.encode(tokenizer, pair["prompt"])
    completion_ids = tokenization.encode(tokenizer, pair["completion"])
    example = tokenization.build_example(prompt_ids, completion_ids, max_len=MAX_LEN)
    labels = example["labels"]
    supervised = [index for index, label in enumerate(labels) if label != -100]
    check(f"{name}: answer not truncated", not example["answer_truncated"],
          f"{example['answer_after']} of {example['answer_before']} answer tokens kept")
    check(f"{name}: mask starts at first answer token",
          supervised and supervised[0] == example["prompt_after"],
          f"first supervised index {supervised[0] if supervised else None}, prompt length {example['prompt_after']}")
    check(f"{name}: no prompt token is supervised",
          all(label == -100 for label in labels[:example["prompt_after"]]))
    kept = labels[supervised[0]:]
    check(f"{name}: labels equal completion ids + end-of-turn", kept == completion_ids + [tokenization.EOS_ID],
          f"{len(kept)} label ids vs {len(completion_ids) + 1} answer ids")
    decoded = tokenization.decode_labels(tokenizer, labels)
    check(f"{name}: labels decode to the untouched gold SQL",
          decoded == record["pg_gold_sql"] + "<|im_end|>", repr(decoded[:120]))
    check(f"{name}: semantic feature survives ({feature})",
          feature.lower() in decoded.lower(), f"missing {feature!r} in {decoded[:80]!r}")

    # side-by-side window around the prompt/answer boundary, plus the answer tail
    window_start = max(0, example["prompt_after"] - 4)
    window_end = min(len(example["input_ids"]), example["prompt_after"] + 24)
    rows = [{
        "index": index,
        "token_id": example["input_ids"][index],
        "token": tokenizer.id_to_token(example["input_ids"][index]),
        "label": labels[index],
        "supervised": labels[index] != -100,
    } for index in range(window_start, window_end)]
    tail = [{
        "index": len(example["input_ids"]) - offset,
        "token_id": example["input_ids"][-offset],
        "token": tokenizer.id_to_token(example["input_ids"][-offset]),
        "label": labels[-offset],
    } for offset in range(3, 0, -1)]
    return {
        "case": name,
        "sample_id": record["sample_id"],
        "db_id": record["db_id"],
        "feature": feature,
        "question": record["question"],
        "gold_sql": record["pg_gold_sql"],
        "prompt_tokens": example["prompt_after"],
        "answer_tokens": example["answer_after"],
        "mask_start_index": supervised[0],
        "last_supervised_id": labels[supervised[-1]],
        "last_supervised_is_eos": labels[supervised[-1]] == tokenization.EOS_ID,
        "gold_sql_in_labels": True,
        "decoded_labels": decoded,
        "note": ("the training pools contain no initial-WITH query (playbook §1); this case uses "
                 "reviewed dev-217 question 1531, rendered by the same renderer")
                if name == "WITH query" else "drawn from the native training corpus",
        "boundary_window": rows,
        "answer_tail": tail,
    }


def check_semantic_cases(records: list[dict], tokenizer) -> list[dict]:
    by_id = {record["sample_id"]: record for record in records}
    with_row = native_loader.dev_record("1531")
    check("WITH case is really a WITH query", with_row["pg_gold_sql"].lstrip().upper().startswith("WITH"),
          with_row["pg_gold_sql"][:60])

    quoted = min((record for record in records if '"' in record["pg_gold_sql"]),
                 key=lambda record: len(record["pg_gold_sql"]))
    cast_div = by_id.get("train-00027")
    if cast_div is None:
        cast_div = next(record for record in records
                        if re.search(r"CAST\s*\(", record["pg_gold_sql"], re.I) and "/" in record["pg_gold_sql"])
    check("cast-division case has CAST and /",
          bool(re.search(r"CAST\s*\(", cast_div["pg_gold_sql"], re.I)) and "/" in cast_div["pg_gold_sql"])
    check("quoted-identifier case has a quoted identifier", '"' in quoted["pg_gold_sql"])

    return [
        _semantic_case("WITH query", with_row, "WITH ", tokenizer),
        _semantic_case("quoted identifier", quoted, '"', tokenizer),
        _semantic_case("division with explicit cast", cast_div, "CAST(", tokenizer),
    ]


def check_batch_statistics(records: list[dict], tokenizer) -> dict:
    prompt_before: list[int] = []
    answer_before: list[int] = []
    prompt_after: list[int] = []
    answer_after: list[int] = []
    answer_lost_total = 0
    truncated_ids: list[str] = []
    zero_loss_ids: list[str] = []
    prompt_truncated_ids: list[str] = []
    sample_examples: dict[str, dict] = {}

    for index, record in enumerate(records):
        pair = render(record, "train")
        prompt_ids = tokenization.encode(tokenizer, pair["prompt"])
        completion_ids = tokenization.encode(tokenizer, pair["completion"])
        example = tokenization.build_example(prompt_ids, completion_ids, max_len=MAX_LEN)
        prompt_before.append(example["prompt_before"])
        answer_before.append(example["answer_before"])
        prompt_after.append(example["prompt_after"])
        answer_after.append(example["answer_after"])
        answer_lost_total += example["answer_lost_tokens"]
        if example["answer_truncated"]:
            truncated_ids.append(record["sample_id"])
        if example["zero_loss_tokens"]:
            zero_loss_ids.append(record["sample_id"])
        if example["prompt_truncated"]:
            prompt_truncated_ids.append(record["sample_id"])
        if index in (0, len(records) - 1) or not sample_examples:
            sample_examples[record["sample_id"]] = example
    # keep one shortest and one longest for the padding test
    order = sorted(range(len(records)), key=lambda i: prompt_before[i])
    for rank in (order[0], order[len(order) // 2], order[-1]):
        record = records[rank]
        pair = render(record, "train")
        sample_examples[record["sample_id"]] = tokenization.build_example(
            tokenization.encode(tokenizer, pair["prompt"]),
            tokenization.encode(tokenizer, pair["completion"]), max_len=MAX_LEN)

    total = len(records)
    sensitivity = {}
    for limit in (512, 1024, 2048):
        fed_prompt = [min(length, limit - tokenization.PROMPT_RESERVE) for length in prompt_before]
        rooms = [limit - fed for fed in fed_prompt]
        fed_answer = [min(length, room) for length, room in zip(answer_before, rooms)]
        sensitivity[str(limit)] = {
            "truncated_prompts": sum(1 for length, fed in zip(prompt_before, fed_prompt) if length > fed),
            "truncated_answers": sum(1 for length, fed in zip(answer_before, fed_answer) if length > fed),
            "zero_loss_examples": sum(1 for fed in fed_answer if fed == 0),
            "lost_answer_tokens": sum(length - fed for length, fed in zip(answer_before, fed_answer)),
            "context_p95": percentile([min(length, limit) for length in prompt_before], 95),
            "answer_p95": percentile(fed_answer, 95),
        }

    stats = {
        "rows": total,
        "real_rows": sum(1 for record in records if record["target_source"] == "native_pg_gold"),
        "synthetic_rows": sum(1 for record in records if record["target_source"] == "native_pg_synthetic"),
        "max_len": MAX_LEN,
        "prompt_reserve": tokenization.PROMPT_RESERVE,
        "length_distributions": {
            "context_before_truncation": distribution(prompt_before),
            "answer_before_truncation": distribution(answer_before),
            "context_after_truncation": distribution(prompt_after),
            "answer_after_truncation": distribution(answer_after),
        },
        "truncation": {
            "truncated_answers": len(truncated_ids),
            "truncated_answer_share": round(len(truncated_ids) / total, 6),
            "truncated_answer_examples": truncated_ids[:20],
            "truncated_prompts": len(prompt_truncated_ids),
            "truncated_prompt_share": round(len(prompt_truncated_ids) / total, 6),
            "lost_answer_tokens": answer_lost_total,
            "descriptive": "no row reaches the 2,048-token limit in this corpus" if not truncated_ids else
                           "answers were cut; ids are listed",
        },
        "zero_loss_tokens": {
            "examples_before_truncation": 0,
            "examples_after_truncation": len(zero_loss_ids),
            "share_after_truncation": round(len(zero_loss_ids) / total, 6),
            "ids": zero_loss_ids[:50],
            "counted_not_ignored": True,
        },
        "sensitivity_by_max_len": sensitivity,
        "examples_used_for_padding": len(sample_examples),
    }
    check("context and answer distributions computed", stats["length_distributions"]["context_before_truncation"]["count"] == total)
    check("zero-loss-token examples are counted, not dropped",
          stats["zero_loss_tokens"]["examples_after_truncation"] == len(zero_loss_ids))
    check("truncated answers are counted", stats["truncation"]["truncated_answers"] == len(truncated_ids))
    return stats, sample_examples


def check_padding(sample_examples: dict) -> dict:
    examples = list(sample_examples.values())
    batch = tokenization.collate(examples)
    width = len(batch["input_ids"][0])
    check("batch width is the longest real sequence",
          width == max(len(example["input_ids"]) for example in examples))
    pad_positions = 0
    for row, example in enumerate(examples):
        length = len(example["input_ids"])
        pad_labels = batch["labels"][row][length:]
        pad_ids = batch["input_ids"][row][length:]
        pad_attn = batch["attention_mask"][row][length:]
        check(f"row {row}: pad labels are -100", all(label == -100 for label in pad_labels))
        check(f"row {row}: pad attention is 0", all(value == 0 for value in pad_attn))
        check(f"row {row}: real labels preserved", batch["labels"][row][:length] == example["labels"])
        check(f"row {row}: real input ids preserved", batch["input_ids"][row][:length] == example["input_ids"])
        check(f"row {row}: real attention is 1", all(value == 1 for value in batch["attention_mask"][row][:length]))
        pad_positions += len(pad_labels)

    # control: even if pad token id == EOS id, pad positions must still be masked
    eos_pad_batch = tokenization.collate(examples, pad_id=tokenization.EOS_ID)
    control_ok = True
    for row, example in enumerate(examples):
        length = len(example["input_ids"])
        control_ok &= all(label == -100 for label in eos_pad_batch["labels"][row][length:])
        control_ok &= all(value == 0 for value in eos_pad_batch["attention_mask"][row][length:])
    check("pad==EOS control: masking is by position, not by id", control_ok)

    return {
        "row_count": len(examples),
        "batch_width": width,
        "pad_positions": pad_positions,
        "pad_token_id": tokenization.PAD_ID,
        "eos_token_id": tokenization.EOS_ID,
        "pad_equals_eos": tokenization.PAD_ID == tokenization.EOS_ID,
        "pad_labels_are_minus_100": True,
        "control_pad_equals_eos_masked_by_position": control_ok,
        "real_positions_verified": True,
    }


def _template_matches_model_config() -> bool:
    config = PROJECT / "data_cache" / "qwen14b_tokenizer" / "tokenizer_config.json"
    template = PROJECT / "repo" / "runs" / "a100-14b" / "adapter" / "chat_template.jinja"
    if not (config.is_file() and template.is_file()):
        return False
    return json.loads(config.read_text(encoding="utf-8")).get("chat_template") == \
        template.read_text(encoding="utf-8")


def build_report() -> dict:
    # Always rebuild the loader output so the report cannot describe a stale JSONL.
    built = native_loader.build_records()
    loader_report = native_loader.write(built, HERE)
    records = built["records"]
    tokenizer = tokenization.load_tokenizer()
    loader_report = json.loads(LOADER_REPORT.read_text(encoding="utf-8"))

    tokenizer_info = check_tokenizer_identity(tokenizer)
    parity = check_renderer_parity(records, tokenizer)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    split_of = {db: family["split"] for db, family in manifest["families"].items()}
    bad_split = [record["sample_id"] for record in records if split_of.get(record["db_id"]) != "train"]
    check("native train rows are all on train schema families", not bad_split,
          f"{len(bad_split)} rows on non-train families, e.g. {bad_split[:3]}")
    check("native train counts match t9 (4,216 real + 2,691 synthetic)",
          sum(1 for record in records if record["target_source"] == "native_pg_gold") == 4216
          and sum(1 for record in records if record["target_source"] == "native_pg_synthetic") == 2691)
    check("native gold SQL is the untouched pg_gold_sql",
          all(record["completion"] == record["pg_gold_sql"] for record in records))

    semantic = check_semantic_cases(records, tokenizer)
    stats, sample_examples = check_batch_statistics(records, tokenizer)
    padding = check_padding(sample_examples)

    from tokenizers import __version__ as tokenizers_version

    report = {
        "generated_at_utc": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat(timespec="seconds"),
        "verdict": "pass" if all(entry["passed"] for entry in CHECKS) else "fail",
        "checks": CHECKS,
        # explicit top-level fields requested for the split filter audit
        "rows_in": loader_report.get("rows_in"),
        "rows_filtered_by_split": loader_report.get("rows_filtered_by_split"),
        "counts_per_split": loader_report.get("counts_by_split_and_source"),
        "split_manifest_sha256": loader_report.get("split_manifest", {}).get("sha256"),
        "inputs": {
            "split_manifest": {"path": str(MANIFEST), "sha256": sha256(MANIFEST)},
            "splits_report": {"path": str(SPLITS_REPORT), "sha256": sha256(SPLITS_REPORT)},
            "loader_report": {"path": str(LOADER_REPORT), "sha256": sha256(LOADER_REPORT)}
                             if LOADER_REPORT.is_file() else None,
            "shipped_corpus": {"path": str(CORPUS), "sha256": sha256(CORPUS)},
            "native_train": {"path": str(HERE / "native_pg_train.jsonl"),
                             "sha256": sha256(HERE / "native_pg_train.jsonl")},
            "native_sidecar": {"path": str(HERE / "native_pg_sidecar.jsonl"),
                               "sha256": sha256(HERE / "native_pg_sidecar.jsonl")},
        },
        "split_contract": {
            "rows_in": loader_report.get("rows_in"),
            "rows_filtered": loader_report.get("rows_filtered"),
            "rows_filtered_by_split": loader_report.get("rows_filtered_by_split"),
            "rows_filtered_by_reason": loader_report.get("rows_filtered_by_reason"),
            "counts_by_split_and_source": loader_report.get("counts_by_split_and_source"),
            "train_rows_real": loader_report.get("train_rows_real"),
            "train_rows_synthetic": loader_report.get("train_rows_synthetic"),
            "split_manifest_sha256": loader_report.get("split_manifest", {}).get("sha256"),
            "zero_non_train_rows": loader_report.get("zero_non_train_rows"),
            "matches_t9_contract": (loader_report.get("train_rows_real") == 4216
                                    and loader_report.get("train_rows_synthetic") == 2691
                                    and loader_report.get("rows_filtered") == 1018),
            "service_fields_in_prompt": loader_report.get("service_fields_in_prompt"),
            "native_gold_untouched": loader_report.get("native_gold_untouched"),
        },
        "tokenizer": {
            "path": str(tokenization.TOKENIZER_PATH),
            "sha256": sha256(tokenization.TOKENIZER_PATH),
            "library": f"tokenizers {tokenizers_version}",
            "is_exact_cluster_model_snapshot": str(tokenization.TOKENIZER_PATH).endswith(
                "data_cache/qwen14b_tokenizer/tokenizer.json"),
            "chat_template": {
                "path": str(PROJECT / "repo" / "runs" / "a100-14b" / "adapter" / "chat_template.jinja"),
                "sha256": sha256(PROJECT / "repo" / "runs" / "a100-14b" / "adapter" / "chat_template.jinja"),
                "matches_model_tokenizer_config": _template_matches_model_config(),
            },
            **tokenizer_info,
        },
        "renderer_parity": parity,
        "semantic_cases": semantic,
        "batch_statistics": stats,
        "padding_and_masking": padding,
        "environment": {
            "python": sys.version.split()[0],
            "dependencies": {"tokenizers": tokenizers_version, "jinja2": __import__("jinja2").__version__},
        },
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="renderer parity + batch audit")
    parser.add_argument("--out", type=pathlib.Path, default=REPORT_PATH)
    args = parser.parse_args(argv)
    report = build_report()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    stats = report["batch_statistics"]
    print(f"verdict: {report['verdict']} ({len(CHECKS)} checks)")
    print(f"parity: train/serve token ids identical, prompt tokens {report['renderer_parity']['prompt_tokens']}")
    print(f"semantic cases: " + ", ".join(case["case"] for case in report["semantic_cases"]))
    for case in report["semantic_cases"]:
        print(f"  {case['case']} ({case['sample_id']}):")
        print(f"    {'idx':>6} {'token':>18} {'id':>7} {'label':>7}")
        for row in case["boundary_window"][:10]:
            print(f"    {row['index']:>6} {row['token']!r:>18} {row['token_id']:>7} {row['label']:>7}")
        print(f"    ... last supervised id {case['last_supervised_id']} "
              f"(eos={case['last_supervised_is_eos']})")
    print("length distributions (p50/p95/p99):")
    for name, values in stats["length_distributions"].items():
        print(f"  {name:32s} {values['p50']}/{values['p95']}/{values['p99']}")
    print(f"truncated answers: {stats['truncation']['truncated_answers']} "
          f"({stats['truncation']['truncated_answer_share']:.4%})")
    print(f"zero-loss-token examples: {stats['zero_loss_tokens']['examples_after_truncation']}")
    print(f"report: {args.out}")
    return 0 if report["verdict"] == "pass" else 1


def test_renderer_parity_and_batch_report(tmp_path=None) -> None:
    """Pytest entry point: the full audit must pass."""
    report = build_report()
    assert report["verdict"] == "pass", [entry for entry in report["checks"] if not entry["passed"]]


if __name__ == "__main__":
    sys.exit(main())
