r"""QLoRA SFT for the LoRA track: PEFT LoRA + 4-bit NF4 on the data built by ``build_data.py``.

Ported from ``research/advtext2sql/r3-scratch/train_qlora.py`` (the recipe the captain executed)
and hardened so that *planning* never needs a GPU or a training stack:

  * **lazy imports** -- importing this module requires only the standard library.  ``torch``,
    ``trl``, ``transformers``, ``peft`` and ``datasets`` are imported inside the training path and
    inside the TRL probe, never at module import time.  A reviewer without the training stack can
    still run ``--plan``.
  * ``--plan`` -- dry run: prints the fully resolved config (every CLI default folded in) and the
    TRL API-compatibility decision, writing ``provenance.json``.  Does not import torch, does not
    probe CUDA, does not load a model.
  * **TRL compatibility probe** with an actionable error: ``SFTConfig`` field names moved between
    releases (``max_seq_length`` -> ``max_length``, ``evaluation_strategy`` -> ``eval_strategy``),
    and ``completion_only_loss`` (TRL >= 0.16) is mandatory here: without it the schema block is
    trained on and the experiment silently differs from the design.
  * ``provenance.json`` -- model id, data hashes, seeds, sqlglot/TRL pins and the git commit
    (``repo/.git`` exists, unlike the research snapshot).
  * **smoke-data guard** -- a dataset built with synthesised or stub schemas
    (``schema_basis != "file"``) may be planned but not trained, unless ``--allow-smoke-data``.

Usage (from the repo root)::

    python training/train_qlora.py --plan --data training/self_test_out
    python training/train_qlora.py --data training/out --out training/runs/e3-r16-alllinear

Prerequisites for the real path: CUDA, ``torch``, ``trl>=0.16,<0.21``, ``peft``, ``transformers``,
``datasets``, ``bitsandbytes``; and a ``db_schemas.json``-based (``schema_basis="file"``) dataset.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_TRAINING_DIR = Path(__file__).resolve().parent
REPO_ROOT = _TRAINING_DIR.parent

SQLGLOT_PIN = "30.23.0"
TRL_REQUIRED = ">=0.16,<0.21"
SMOKE_SCHEMA_BASES = ("synth-from-gold", "file+stub")


# ------------------------------------------------------------------------------------------------
# CLI + config resolution
# ------------------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="QLoRA SFT for the Text2SQL adapter (PEFT + TRL).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--model", default="Qwen/Qwen2.5-Coder-7B-Instruct",
                        help="HF id or a local snapshot directory")
    parser.add_argument("--model-id", default=None,
                        help="HF id recorded in provenance when --model is a local snapshot")
    parser.add_argument("--data", default=str(_TRAINING_DIR / "self_test_out"),
                        help="directory produced by build_data.py")
    parser.add_argument("--train-file", default="sft_all_train.jsonl")
    parser.add_argument("--valid-file", default="sft_bird_valid.jsonl",
                        help="BIRD-only valid: the track EX is reported on")
    parser.add_argument("--out", default=str(_TRAINING_DIR / "runs" / "qlora"))
    # PEFT
    parser.add_argument("--r", type=int, default=16)
    parser.add_argument("--alpha", type=int, default=32)
    parser.add_argument("--dropout", type=float, default=0.05)
    parser.add_argument("--target-modules", default="all-linear",
                        help="'all-linear' | 'q,k,v,o' | explicit comma list")
    parser.add_argument("--bias", default="none")
    # optimisation
    parser.add_argument("--epochs", type=float, default=3)
    parser.add_argument("--lr", type=float, default=1.5e-4)
    parser.add_argument("--max-seq-len", type=int, default=3072)
    parser.add_argument("--per-device-batch", type=int, default=1)
    parser.add_argument("--grad-accum", type=int, default=16)
    parser.add_argument("--warmup-ratio", type=float, default=0.03)
    parser.add_argument("--lr-scheduler", default="cosine")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--packing", action="store_true", help="off by default (design §4.1)")
    parser.add_argument("--save-steps", type=int, default=50)
    parser.add_argument("--eval-steps", type=int, default=50)
    parser.add_argument("--logging-steps", type=int, default=5)
    parser.add_argument("--report-to", default="tensorboard",
                        choices=["tensorboard", "wandb", "none"])
    parser.add_argument("--max-grad-norm", type=float, default=1.0)
    parser.add_argument("--early-stopping-patience", type=int, default=3,
                        help="stop when eval_loss rises this many evaluations in a row "
                             "(0 disables; the best checkpoint is kept either way)")
    parser.add_argument("--sample-index", type=int, default=0,
                        help="after training, greedy-decode one training prompt (row index; -1 = off)")
    parser.add_argument("--resume", action="store_true", help="resume from the last checkpoint in --out")
    parser.add_argument("--no-4bit", action="store_true",
                        help="bf16 LoRA instead of QLoRA (only for <=3B smoke models)")
    parser.add_argument("--allow-smoke-data", action="store_true",
                        help="allow training on a synth/stub-schema dataset (smoke runs only)")
    parser.add_argument("--plan", action="store_true",
                        help="dry run: resolved config + TRL decision + provenance.json, no GPU")
    return parser


def resolve_target_modules(spec: str):
    if spec == "all-linear":
        return "all-linear"
    if spec == "q,k,v,o":
        return ["q_proj", "k_proj", "v_proj", "o_proj"]
    return [item.strip() for item in spec.split(",") if item.strip()]


def resolve_config(args: argparse.Namespace) -> dict:
    """Fold every CLI default into one JSON-able config.  This is what ``--plan`` prints."""
    target_modules = resolve_target_modules(args.target_modules)
    patience = max(0, int(args.early_stopping_patience))
    eval_steps = args.eval_steps
    save_steps = args.save_steps
    if patience:
        # transformers requires save_steps to be a multiple of eval_steps when
        # load_best_model_at_end=True; make them equal instead of failing at trainer construction.
        save_steps = eval_steps
    config = {
        "model": args.model,
        "model_id": args.model_id or args.model,
        "data": os.path.normpath(args.data),
        "train_file": args.train_file,
        "valid_file": args.valid_file,
        "out": os.path.normpath(args.out),
        "r": args.r,
        "alpha": args.alpha,
        "lora_scaling": args.alpha / args.r if args.r else None,
        "dropout": args.dropout,
        "target_modules": target_modules,
        "bias": args.bias,
        "epochs": args.epochs,
        "lr": args.lr,
        "max_seq_len": args.max_seq_len,
        "per_device_batch": args.per_device_batch,
        "grad_accum": args.grad_accum,
        "effective_batch": args.per_device_batch * args.grad_accum,
        "warmup_ratio": args.warmup_ratio,
        "lr_scheduler": args.lr_scheduler,
        "seed": args.seed,
        "data_seed": args.seed,
        "packing": bool(args.packing),
        "save_steps": save_steps,
        "eval_steps": eval_steps,
        "logging_steps": args.logging_steps,
        "report_to": args.report_to,
        "resume": bool(args.resume),
        "max_grad_norm": args.max_grad_norm,
        "early_stopping_patience": patience,
        "metric_for_best_model": "eval_loss" if patience else None,
        "load_best_model_at_end": bool(patience),
        "sample_index": args.sample_index,
        "quantization": ("none (bf16 LoRA)" if args.no_4bit else
                         "4bit-nf4 double-quant, compute bf16"),
        "attn_implementation": "sdpa",
        "completion_only_loss": True,
        "allow_smoke_data": bool(args.allow_smoke_data),
    }
    return config


# ------------------------------------------------------------------------------------------------
# probes: pins, git, data
# ------------------------------------------------------------------------------------------------
def probe_sqlglot() -> dict:
    installed = None
    detail = None
    try:
        import sqlglot  # noqa: F401  (lightweight; no torch involved)

        installed = getattr(sqlglot, "__version__", "unknown")
    except Exception as error:  # noqa: BLE001
        detail = f"{type(error).__name__}: {error}"
    return {
        "pin": SQLGLOT_PIN,
        "installed": installed,
        "match": installed == SQLGLOT_PIN,
        "detail": detail,
        "note": "the evaluator transpiles gold with this version; counts are a function of it",
    }


def probe_trl() -> dict:
    """Probe the installed TRL without importing torch at module level.

    Returns a dict with ``decision`` in ``{"ok", "blocked"}`` and, when blocked, an ``action``
    string that says exactly what to install.  Never raises.
    """
    probe = {
        "required": TRL_REQUIRED,
        "installed": None,
        "sftconfig_fields": None,
        "length_field": None,
        "eval_field": None,
        "completion_only_loss": None,
        "decision": "blocked",
        "action": f"pip install 'trl{TRL_REQUIRED}'",
        "detail": None,
    }
    try:
        import importlib.util

        if importlib.util.find_spec("trl") is None:
            raise ModuleNotFoundError("No module named 'trl'")
        import trl
        from trl import SFTConfig

        probe["installed"] = getattr(trl, "__version__", "unknown")
        fields = sorted(getattr(SFTConfig, "__dataclass_fields__", {}))
        probe["sftconfig_fields"] = fields
        probe["length_field"] = next(
            (name for name in ("max_length", "max_seq_length") if name in fields), None
        )
        probe["eval_field"] = next(
            (name for name in ("eval_strategy", "evaluation_strategy") if name in fields), None
        )
        probe["completion_only_loss"] = "completion_only_loss" in fields
    except Exception as error:  # noqa: BLE001 - any failure means "blocked", with the reason kept
        probe["detail"] = f"{type(error).__name__}: {error}"
        return probe

    missing = []
    if probe["length_field"] is None:
        missing.append("SFTConfig has neither `max_length` nor `max_seq_length`")
    if probe["eval_field"] is None:
        missing.append("SFTConfig has neither `eval_strategy` nor `evaluation_strategy`")
    if not probe["completion_only_loss"]:
        missing.append(
            "SFTConfig has no `completion_only_loss` (added in TRL 0.16); without it the schema "
            "block would be trained on and the experiment would differ from the design"
        )
    if missing:
        probe["detail"] = "; ".join(missing)
        return probe
    probe["decision"] = "ok"
    probe["action"] = None
    return probe


def git_state() -> dict:
    def run(*args: str) -> str | None:
        try:
            completed = subprocess.run(
                ["git", *args], cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=15
            )
            if completed.returncode != 0:
                return None
            return completed.stdout.strip()
        except Exception:  # noqa: BLE001 - git may be absent; provenance degrades gracefully
            return None

    commit = run("rev-parse", "HEAD")
    status = run("status", "--porcelain")
    return {
        "commit": commit,
        "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": None if status is None else bool(status),
        "available": commit is not None,
    }


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def count_jsonl_rows(path: str) -> int:
    with open(path, "r", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def data_fingerprint(data_dir: str, train_file: str, valid_file: str) -> dict:
    """Hashes plus the build's own stats: what exactly is being trained on."""
    fingerprint = {"dir": os.path.normpath(data_dir), "files": {}, "data_hash": None,
                   "schema_basis": None, "build_self_test": None, "errors": []}
    digests = {}
    for name in (train_file, valid_file):
        path = os.path.join(data_dir, name)
        if not os.path.exists(path):
            fingerprint["errors"].append(f"missing data file: {path}")
            continue
        digest = sha256_file(path)
        digests[name] = digest
        fingerprint["files"][name] = {
            "path": os.path.normpath(path),
            "rows": count_jsonl_rows(path),
            "sha256": digest,
            "sha256_16": digest[:16],
        }
    if digests:
        joined = "".join(f"{name}:{digests[name]}\n" for name in sorted(digests))
        fingerprint["data_hash"] = hashlib.sha256(joined.encode("utf-8")).hexdigest()

    stats_path = os.path.join(data_dir, "stats.json")
    if os.path.exists(stats_path):
        try:
            with open(stats_path, "r", encoding="utf-8") as handle:
                stats = json.load(handle)
            fingerprint["schema_basis"] = stats.get("schema_basis")
            fingerprint["build_self_test"] = stats.get("self_test")
            fingerprint["build"] = {
                "sqlglot": stats.get("sqlglot"),
                "seed": stats.get("seed"),
                "combined_train": stats.get("combined_train"),
                "feature_prompt": stats.get("feature_prompt"),
                "teacher": None if stats.get("teacher") is None else {
                    "mode": stats["teacher"].get("mode"),
                    "kept": stats["teacher"].get("kept"),
                    "dropped": stats["teacher"].get("dropped"),
                },
            }
        except Exception as error:  # noqa: BLE001
            fingerprint["errors"].append(f"unreadable stats.json: {type(error).__name__}: {error}")
    return fingerprint


def check_completions(path: str) -> dict:
    """The data contract: every completion must be SQL, OK or AMBIGUOUS: ..."""
    bad = []
    total = 0
    with open(path, "r", encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            if not line.strip():
                continue
            total += 1
            completion = json.loads(line)["completion"].strip()
            ok = (completion.startswith("SELECT") or completion.startswith("WITH")
                  or completion == "OK" or completion.startswith("AMBIGUOUS:"))
            if not ok:
                bad.append({"line": index + 1, "completion": completion[:120]})
    return {"rows": total, "invalid": bad}


def smoke_guard(fingerprint: dict, allow: bool) -> str | None:
    """Return an error message when the data is not fit for a real training run."""
    basis = fingerprint.get("schema_basis")
    if basis is not None and basis not in SMOKE_SCHEMA_BASES:
        return None
    if basis is None:
        return ("stats.json is missing, so the dataset's schema basis is unknown; a real build "
                "writes it. Regenerate with `python training/build_data.py` (no --allow-synth-schemas).")
    if allow:
        return None
    return (
        f"schema_basis={basis!r}: this dataset was built with synthesised or stub schemas, so its "
        f"prompts do not match the harness inference surface. Planning is fine; training on it is "
        f"not, because the resulting EX would measure a leaked-schema prompt. Rebuild with "
        f"`--schemas db_schemas.json`, or pass --allow-smoke-data for a deliberate smoke run."
    )


# ------------------------------------------------------------------------------------------------
# provenance
# ------------------------------------------------------------------------------------------------
def write_provenance(out_dir: str, payload: dict) -> str:
    """Write provenance.json, never clobbering a *training* record with a plan record.

    The acceptance workflow runs ``--plan --out runs/e3-r16`` both before and after the real run.
    A plan record must not overwrite the measured training record, so it goes to
    ``provenance.plan.json`` when one already exists, and the training record keeps the canonical
    name.
    """
    os.makedirs(out_dir, exist_ok=True)
    name = "provenance.json"
    existing = os.path.join(out_dir, name)
    if payload.get("mode") != "train" and os.path.exists(existing):
        try:
            with open(existing, "r", encoding="utf-8") as handle:
                previous = json.load(handle)
        except Exception:  # noqa: BLE001 - unreadable previous record: keep the default name
            previous = {}
        if previous.get("mode") == "train":
            name = "provenance.plan.json"
    path = os.path.join(out_dir, name)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    return path


def provenance_payload(mode: str, config: dict, fingerprint: dict, trl: dict,
                       sqlglot: dict, extra: dict | None = None) -> dict:
    payload = {
        "created_by": "training/train_qlora.py",
        "mode": mode,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": {
            "id": config["model_id"],
            "path": config["model"],
            "quantization": config["quantization"],
            "attn_implementation": config["attn_implementation"],
        },
        "data": fingerprint,
        "seeds": {"data": config["seed"], "train": config["seed"], "data_seed": config["data_seed"]},
        "pins": {
            "sqlglot": {"pin": sqlglot["pin"], "installed": sqlglot["installed"],
                        "match": sqlglot["match"]},
            "trl": {"required": trl["required"], "installed": trl["installed"],
                    "decision": trl["decision"], "length_field": trl["length_field"],
                    "eval_field": trl["eval_field"],
                    "completion_only_loss": trl["completion_only_loss"]},
        },
        "git": git_state(),
        "config": config,
        "env": {"python": platform.python_version(), "platform": platform.platform()},
        "trl": trl,
    }
    if extra:
        payload.update(extra)
    return payload


# ------------------------------------------------------------------------------------------------
# plan (no GPU, no training stack)
# ------------------------------------------------------------------------------------------------
def print_trl_decision(trl: dict) -> None:
    print(f"[trl] installed={trl['installed']} required={trl['required']} "
          f"decision={trl['decision']}")
    if trl["decision"] == "ok":
        print(f"[trl] SFTConfig: length_field={trl['length_field']!r} "
              f"eval_field={trl['eval_field']!r} completion_only_loss=yes")
        return
    print(f"[trl] detail: {trl['detail']}", file=sys.stderr)
    print(f"[trl] action: {trl['action']}", file=sys.stderr)
    print("[trl] field names moved between releases (`max_seq_length`->`max_length`, "
          "`evaluation_strategy`->`eval_strategy`) and `completion_only_loss` only exists from "
          "TRL 0.16; the pinned range is the version this recipe was validated against.",
          file=sys.stderr)


def run_plan(args: argparse.Namespace) -> int:
    config = resolve_config(args)
    fingerprint = data_fingerprint(config["data"], config["train_file"], config["valid_file"])
    sqlglot = probe_sqlglot()
    trl = probe_trl()

    config["train_rows"] = fingerprint["files"].get(config["train_file"], {}).get("rows")
    config["valid_rows"] = fingerprint["files"].get(config["valid_file"], {}).get("rows")
    if config["train_rows"]:
        per_epoch = max(1, (config["train_rows"] + config["effective_batch"] - 1)
                        // config["effective_batch"])
        config["steps_per_epoch"] = per_epoch
        config["total_steps"] = int(round(per_epoch * config["epochs"]))

    print("==================== RESOLVED CONFIG (--plan) ====================")
    print(json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True))
    print("==================== COMPATIBILITY / PINS =======================")
    print(f"[sqlglot] installed={sqlglot['installed']} pin={sqlglot['pin']} "
          f"match={sqlglot['match']}")
    print_trl_decision(trl)
    print(f"[data] dir={fingerprint['dir']} data_hash={fingerprint['data_hash']} "
          f"schema_basis={fingerprint['schema_basis']} rows="
          f"train={config['train_rows']} valid={config['valid_rows']}")
    for error in fingerprint["errors"]:
        print(f"[data] WARNING: {error}", file=sys.stderr)
    guard = smoke_guard(fingerprint, args.allow_smoke_data)
    if guard:
        print(f"[data] WARNING (training would be refused): {guard}", file=sys.stderr)
    print("[gpu] not probed: --plan never imports torch")

    payload = provenance_payload("plan", config, fingerprint, trl, sqlglot, extra={
        "lora": {"trainable_params": None,
                 "note": "computed after the base model is loaded; --plan does not load a model"},
        "plan": {"trl_decision": trl["decision"], "trl_action": trl["action"],
                 "training_would_be": ("refused: " + guard) if guard else "allowed"},
    })
    path = write_provenance(config["out"], payload)
    print(f"[provenance] {path}")
    print(f"[plan] TRL decision={trl['decision']}; no model, no GPU, no training")
    return 0


def generate_sample(model, tokenizer, train_file: str, index: int,
                    max_new_tokens: int = 256) -> dict:
    """Greedy-decode one training prompt with the trained adapter (the SQLite-vs-PostgreSQL check)."""
    import torch

    with open(train_file, "r", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    if not rows:
        raise RuntimeError(f"no rows in {train_file}")
    # The sanity check is "does it emit PostgreSQL SQL", so index among SQL rows: row 0 of the
    # shuffled training file may well be an Ambrosia OK/AMBIGUOUS example.
    sql_positions = [position for position, row in enumerate(rows) if row.get("task") == "sql"]
    positions = sql_positions or list(range(len(rows)))
    row_index = positions[max(0, min(index, len(positions) - 1))]
    row = rows[row_index]
    model.eval()
    model.config.use_cache = True  # gradient checkpointing turns it off; generation needs it
    inputs = tokenizer(row["prompt"], return_tensors="pt").to("cuda")
    with torch.no_grad():
        output = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            do_sample=False,
            pad_token_id=tokenizer.eos_token_id,
        )
    generated = tokenizer.decode(
        output[0][inputs["input_ids"].shape[-1]:], skip_special_tokens=True
    ).strip()
    markers = [marker for marker in ("`", "IIF(", "IFNULL(", "STRFTIME(", "JULIANDAY(")
               if marker in generated]
    return {
        "row": row_index,
        "question_id": row.get("question_id"),
        "prompt": row["prompt"],
        "gold": row["completion"],
        "generated": generated,
        "sqlite_markers": markers,
        "looks_postgres": bool(generated.upper().startswith("SELECT") and not markers),
    }


# ------------------------------------------------------------------------------------------------
# train (the real path: heavy imports happen here)
# ------------------------------------------------------------------------------------------------
def run_train(args: argparse.Namespace) -> int:
    config = resolve_config(args)
    fingerprint = data_fingerprint(config["data"], config["train_file"], config["valid_file"])
    sqlglot = probe_sqlglot()

    # Data guards run *before* the GPU check: a bad dataset must fail on any machine, and the
    # failure must be about the data, not about CUDA.
    guard = smoke_guard(fingerprint, args.allow_smoke_data)
    if guard:
        print(f"ERROR: {guard}", file=sys.stderr)
        return 2
    if fingerprint["errors"]:
        print(f"ERROR: data problems: {fingerprint['errors']}", file=sys.stderr)
        return 2

    completion_check = check_completions(
        os.path.join(config["data"], config["train_file"])
    )
    if completion_check["invalid"]:
        print(f"ERROR: {len(completion_check['invalid'])} training completions do not start with "
              f"SELECT/WITH/OK/AMBIGUOUS: {completion_check['invalid'][:3]}", file=sys.stderr)
        return 2

    trl = probe_trl()
    print_trl_decision(trl)
    if trl["decision"] != "ok":
        print(f"ERROR: TRL is not usable for this recipe. {trl['action']}", file=sys.stderr)
        return 2

    try:
        import torch
        from datasets import load_dataset
        from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
        from transformers import (AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig,
                                  EarlyStoppingCallback)
        from trl import SFTConfig, SFTTrainer
    except Exception as error:  # noqa: BLE001
        print(f"ERROR: training stack import failed: {type(error).__name__}: {error}\n"
              f"Install the training extra: torch (CUDA build), trl{TRL_REQUIRED}, peft, "
              f"transformers, datasets, bitsandbytes, tensorboard.", file=sys.stderr)
        return 2

    if not torch.cuda.is_available():
        print("ERROR: no CUDA device visible. On Windows check the driver; on WSL2 check "
              "--gpus all / CUDA passthrough.", file=sys.stderr)
        return 2
    print(f"[gpu] {torch.cuda.get_device_name(0)} "
          f"{torch.cuda.get_device_properties(0).total_memory / 2**30:.1f} GiB")

    os.makedirs(config["out"], exist_ok=True)
    tokenizer = AutoTokenizer.from_pretrained(config["model"], use_fast=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    quant_config = None
    if not args.no_4bit:
        quant_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.bfloat16,
        )
    model = AutoModelForCausalLM.from_pretrained(
        config["model"],
        quantization_config=quant_config,
        torch_dtype=torch.bfloat16,
        device_map={"": 0},
        attn_implementation=config["attn_implementation"],
    )
    model.config.use_cache = False
    if quant_config is not None:
        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)

    lora = LoraConfig(
        r=config["r"],
        lora_alpha=config["alpha"],
        lora_dropout=config["dropout"],
        target_modules=config["target_modules"],
        bias=config["bias"],
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, lora)
    trainable = sum(parameter.numel() for parameter in model.parameters() if parameter.requires_grad)
    total = sum(parameter.numel() for parameter in model.parameters())
    print(f"[lora] trainable={trainable:,} ({100 * trainable / total:.3f}% of {total:,})")
    model.print_trainable_parameters()

    data_files = {
        "train": os.path.join(config["data"], config["train_file"]),
        "valid": os.path.join(config["data"], config["valid_file"]),
    }
    dataset = load_dataset("json", data_files=data_files)
    print(f"[data] train={len(dataset['train'])} valid={len(dataset['valid'])} "
          f"hash={fingerprint['data_hash'][:16]}")

    planned_steps = max(1, math.ceil(len(dataset["train"]) / config["effective_batch"]))
    planned_steps = int(round(planned_steps * config["epochs"]))
    sft_config_kwargs = {
        "output_dir": config["out"],
        "num_train_epochs": config["epochs"],
        "per_device_train_batch_size": config["per_device_batch"],
        "per_device_eval_batch_size": 1,
        "gradient_accumulation_steps": config["grad_accum"],
        "gradient_checkpointing": True,
        "gradient_checkpointing_kwargs": {"use_reentrant": False},
        "learning_rate": config["lr"],
        "lr_scheduler_type": config["lr_scheduler"],
        "warmup_ratio": config["warmup_ratio"],
        "max_grad_norm": config["max_grad_norm"],
        "bf16": True,
        "optim": "paged_adamw_8bit",
        trl["length_field"]: config["max_seq_len"],
        "packing": config["packing"],
        "completion_only_loss": True,
        "save_strategy": "steps",
        "save_steps": config["save_steps"],
        "save_total_limit": 2,
        trl["eval_field"]: "steps",
        "eval_steps": config["eval_steps"],
        "logging_steps": config["logging_steps"],
        "report_to": ([] if config["report_to"] == "none" else [config["report_to"]]),
        "seed": config["seed"],
        "data_seed": config["data_seed"],
    }
    if config["early_stopping_patience"]:
        # Keep the best checkpoint, not the last one: eval_loss is the only honest model-selection
        # signal we have offline (BIRD EX needs the DB tunnel).
        sft_config_kwargs.update({
            "load_best_model_at_end": True,
            "metric_for_best_model": config["metric_for_best_model"],
            "greater_is_better": False,
        })
    sft_config = SFTConfig(**sft_config_kwargs)

    trainer_kwargs = {"args": sft_config, "train_dataset": dataset["train"],
                      "eval_dataset": dataset["valid"]}
    if config["early_stopping_patience"]:
        trainer_kwargs["callbacks"] = [
            EarlyStoppingCallback(early_stopping_patience=config["early_stopping_patience"])
        ]
    try:  # TRL >= 0.16 renamed tokenizer= to processing_class=
        trainer = SFTTrainer(model=model, processing_class=tokenizer, **trainer_kwargs)
    except TypeError:
        trainer = SFTTrainer(model=model, tokenizer=tokenizer, **trainer_kwargs)

    torch.cuda.reset_peak_memory_stats(0)
    torch.cuda.synchronize(0)
    started = time.monotonic()
    trainer.train(resume_from_checkpoint=True if config["resume"] else None)
    torch.cuda.synchronize(0)
    wall_clock_s = time.monotonic() - started
    peak_allocated = torch.cuda.max_memory_allocated(0) / 2**30
    peak_reserved = torch.cuda.max_memory_reserved(0) / 2**30
    steps_run = int(trainer.state.global_step)
    stopped_early = bool(planned_steps) and steps_run < planned_steps
    print(f"[measured] wall={wall_clock_s:.1f}s steps={steps_run}/{planned_steps} "
          f"peak_vram_allocated={peak_allocated:.2f}GiB peak_vram_reserved={peak_reserved:.2f}GiB "
          f"stopped_early={stopped_early}")

    adapter_dir = os.path.join(config["out"], "adapter")
    trainer.model.save_pretrained(adapter_dir)
    tokenizer.save_pretrained(adapter_dir)

    eval_history = [
        {"step": entry.get("step"), "epoch": entry.get("epoch"),
         "eval_loss": entry.get("eval_loss")}
        for entry in trainer.state.log_history if "eval_loss" in entry
    ]
    losses = [entry["loss"] for entry in trainer.state.log_history if "loss" in entry]

    sample = None
    if config["sample_index"] is not None and config["sample_index"] >= 0:
        sample = generate_sample(
            trainer.model, tokenizer, data_files["train"], config["sample_index"]
        )
        print(f"[sample] row={sample['row']} question_id={sample['question_id']}")
        print(f"[sample] prompt_tail: ...{sample['prompt'][-160:]!r}")
        print(f"[sample] gold: {sample['gold']}")
        print(f"[sample] adapter: {sample['generated']}")
        with open(os.path.join(config["out"], "sample_generation.json"), "w",
                  encoding="utf-8", newline="\n") as handle:
            json.dump(sample, handle, ensure_ascii=False, indent=2)
            handle.write("\n")

    measured = {
        "wall_clock_s": round(wall_clock_s, 1),
        "wall_clock_human": f"{int(wall_clock_s // 60)}m{wall_clock_s % 60:04.1f}s",
        "peak_vram_allocated_gib": round(peak_allocated, 3),
        "peak_vram_reserved_gib": round(peak_reserved, 3),
        "steps_run": steps_run,
        "planned_steps": planned_steps,
        "stopped_early": stopped_early,
        "best_model_checkpoint": trainer.state.best_model_checkpoint,
        "best_metric": trainer.state.best_metric,
        "eval_history": eval_history,
        "train_loss_first": losses[0] if losses else None,
        "train_loss_last": losses[-1] if losses else None,
        "sample": (None if sample is None else {
            "row": sample["row"], "question_id": sample["question_id"],
            "generated": sample["generated"], "gold": sample["gold"],
            "file": os.path.join(config["out"], "sample_generation.json"),
        }),
        "gpu": {
            "name": torch.cuda.get_device_name(0),
            "total_gib": round(torch.cuda.get_device_properties(0).total_memory / 2**30, 2),
        },
        "torch": torch.__version__,
    }
    payload = provenance_payload("train", config, fingerprint, trl, sqlglot, extra={
        "lora": {"trainable_params": trainable, "total_params": total,
                 "share": trainable / total if total else None, "adapter_dir": adapter_dir},
        "train": {"rows": len(dataset["train"]), "valid_rows": len(dataset["valid"]),
                  "steps": steps_run, "loss_first": losses[0] if losses else None,
                  "loss_last": losses[-1] if losses else None},
        "measured": measured,
    })
    path = write_provenance(config["out"], payload)
    with open(os.path.join(config["out"], "train_summary.json"), "w", encoding="utf-8",
              newline="\n") as handle:
        json.dump({"config": config, "measured": measured}, handle, ensure_ascii=False,
                  indent=2, sort_keys=True)
        handle.write("\n")
    print(f"[done] adapter -> {adapter_dir}")
    print(f"[done] provenance -> {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.plan:
        return run_plan(args)
    return run_train(args)


if __name__ == "__main__":
    raise SystemExit(main())
