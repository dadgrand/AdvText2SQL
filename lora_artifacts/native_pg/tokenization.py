"""Tokenisation and batch construction mirroring ``cluster/train_lora.py``.

The real trainer is the reference for truncation and masking, so the audit in
``renderer_parity.py`` and any future training/eval code use this one implementation:

* prompt ids are the rendered chat prefix (no BOS: Qwen2.5 does not add one);
* the completion is ``pg_gold_sql`` tokenised as-is, then one ``<|im_end|>`` (EOS);
* if the prompt is longer than ``max_len - 8`` its tail is kept;
* the answer is cut to the remaining room;
* labels are ``-100`` on every prompt token and the real answer ids afterwards;
* padding is filled with the pad token, but labels are masked to ``-100`` on pad
  *positions*, never by comparing ids (Qwen's pad ``<|endoftext|>`` is distinct from EOS
  ``<|im_end|>``, yet a future tokenizer might reuse them).

The underlying ``tokenizers`` object is the Qwen2.5-Coder tokenizer snapshot found in
``repo/runs/a100-14b/adapter/tokenizer.json`` (vocab 151,665; selected ids were verified
against the cluster's ``AutoTokenizer`` for the same strings).
"""

from __future__ import annotations

import pathlib
import sys

try:
    from tokenizers import Tokenizer
except ImportError:  # allow plain python3 when only .venv-tools has deps
    _PROJECT = pathlib.Path(__file__).resolve().parents[2]
    _site = next(iter(sorted((_PROJECT.parents[1] / ".venv-tools").glob("lib/python3*/site-packages"))), None)
    if _site is not None:
        sys.path.append(str(_site))
    from tokenizers import Tokenizer

HERE = pathlib.Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
#: Exact Qwen2.5-Coder-14B-Instruct tokenizer snapshot copied from the cluster model
#: (``~/models/Qwen2.5-Coder-14B-Instruct/tokenizer.json``); the repo adapter snapshot is
#: the fallback for checkouts without the local data cache. Both reproduce the same ids.
_MODEL_TOKENIZER = PROJECT / "data_cache" / "qwen14b_tokenizer" / "tokenizer.json"
_ADAPTER_TOKENIZER = PROJECT / "repo" / "runs" / "a100-14b" / "adapter" / "tokenizer.json"
TOKENIZER_PATH = _MODEL_TOKENIZER if _MODEL_TOKENIZER.is_file() else _ADAPTER_TOKENIZER

PAD_ID = 151643  # <|endoftext|>
EOS_ID = 151645  # <|im_end|>
IM_START_ID = 151644  # <|im_start|>

DEFAULT_MAX_LEN = 2048
PROMPT_RESERVE = 8  # cluster/train_lora.py keeps max_len - 8 tokens for the prompt


def load_tokenizer(path: pathlib.Path | str | None = None) -> Tokenizer:
    return Tokenizer.from_file(str(path or TOKENIZER_PATH))


def encode(tokenizer: Tokenizer, text: str) -> list[int]:
    """Encode without adding special tokens; Qwen adds neither BOS nor EOS here."""
    return tokenizer.encode(text, add_special_tokens=False).ids


def decode(tokenizer: Tokenizer, ids: list[int], keep_special: bool = True) -> str:
    return tokenizer.decode(ids, skip_special_tokens=not keep_special)


def build_example(prompt_ids: list[int], completion_ids: list[int], max_len: int = DEFAULT_MAX_LEN,
                  eos_id: int = EOS_ID) -> dict:
    """Build one training example exactly like ``CompletionOnlyDataset.__getitem__``."""
    prompt_before = len(prompt_ids)
    answer_before = len(completion_ids) + 1  # + the end-of-turn token
    prompt_fed = prompt_ids
    if len(prompt_ids) >= max_len - PROMPT_RESERVE:
        prompt_fed = prompt_ids[-(max_len - PROMPT_RESERVE):]
    room = max_len - len(prompt_fed)
    answer_fed = (completion_ids + [eos_id])[:room]
    input_ids = prompt_fed + answer_fed
    labels = [-100] * len(prompt_fed) + list(answer_fed)
    return {
        "input_ids": input_ids,
        "labels": labels,
        "prompt_before": prompt_before,
        "answer_before": answer_before,
        "prompt_after": len(prompt_fed),
        "answer_after": len(answer_fed),
        "prompt_truncated": len(prompt_fed) < prompt_before,
        "answer_truncated": len(answer_fed) < answer_before,
        "answer_lost_tokens": answer_before - len(answer_fed),
        "zero_loss_tokens": not any(label != -100 for label in labels),
    }


def collate(examples: list[dict], pad_id: int = PAD_ID) -> dict:
    """Pad a batch by real positions; pad labels stay ``-100``, attention stays 0."""
    width = max(len(item["input_ids"]) for item in examples)
    input_ids = [[pad_id] * width for _ in examples]
    labels = [[-100] * width for _ in examples]
    attention = [[0] * width for _ in examples]
    for row, item in enumerate(examples):
        length = len(item["input_ids"])
        if len(item["labels"]) != length:
            raise AssertionError("input_ids and labels differ in length")
        input_ids[row][:length] = item["input_ids"]
        labels[row][:length] = item["labels"]
        attention[row][:length] = [1] * length
    return {"input_ids": input_ids, "labels": labels, "attention_mask": attention}


def decode_labels(tokenizer: Tokenizer, labels: list[int]) -> str:
    """Decode only the supervised positions, special tokens kept (shows the end-of-turn)."""
    return decode(tokenizer, [label for label in labels if label != -100], keep_special=True)
