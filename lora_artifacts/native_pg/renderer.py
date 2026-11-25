"""One renderer for training and serving the native-PostgreSQL text-to-SQL task.

Both modes go through the model's own Qwen2.5 chat template (read from the local
tokenizer snapshot) and the same system prompt the shipped corpus was built with, so a
train pair and a serve prompt share a byte-identical prefix:

    train: system -> user (schema + question + evidence) -> assistant (gold SQL)
    serve: system -> user (same) -> assistant generation prompt

Only learner-visible fields (schema, question, evidence) are composed into the prompt;
the loader's service metadata never enters this function.

    from datasets.native_pg.renderer import render
    train = render(record, mode="train")   # {"prompt", "completion", "text"}
    serve = render(record, mode="serve")   # {"prompt", "text"}

The renderer is deliberately text-only: tokenisation lives in ``tokenization.py`` so the
corpus can be built without the model stack installed.
"""

from __future__ import annotations

import functools
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
PROJECT = HERE.parents[1]  # research/advtext2sql

try:  # pragma: no cover - the workspace tool env is a convenience fallback
    import jinja2
except ImportError:  # allow plain python3 for the audit when only .venv-tools has deps
    _site = next(iter(sorted((PROJECT.parents[1] / ".venv-tools").glob("lib/python3*/site-packages"))), None)
    if _site is not None:
        sys.path.append(str(_site))
    import jinja2
PACKAGE = PROJECT / "repo" / "datasets" / "bird_pg_unified"
TOKENIZER_DIR = PROJECT / "repo" / "runs" / "a100-14b" / "adapter"
CHAT_TEMPLATE_PATH = TOKENIZER_DIR / "chat_template.jinja"
SCHEMA_PATH = PACKAGE / "schemas" / "schema.json"

#: Identical to ``datasets/bird_pg_unified/tools/to_sft.py`` (the prompt the shipped
#: ``cluster/sft_all*.jsonl`` corpus was rendered with).
SYSTEM = (
    "You are a PostgreSQL expert. Translate the question into ONE SELECT statement.\n"
    "Rules:\n"
    "- Use only tables and columns from the schema below. Quote identifiers exactly as written.\n"
    "- PostgreSQL dialect: single quotes for strings, double quotes only for identifiers, "
    "no backticks, no IFNULL/IIF/STRFTIME, no trailing semicolon.\n"
    "- Use x / NULLIF(y, 0) for every division whose denominator is a column.\n"
    "- Never add comments, markdown fences, or explanations.\n"
    "Output: SQL only."
)

MODES = ("train", "serve")


@functools.lru_cache(maxsize=1)
def schema_registry() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@functools.lru_cache(maxsize=1)
def chat_template() -> jinja2.Template:
    return jinja2.Template(CHAT_TEMPLATE_PATH.read_text(encoding="utf-8"))


def schema_block(db_id: str, max_chars: int = 6000) -> str:
    """Same schema text the package converter (and therefore the shipped corpus) uses."""
    description = schema_registry().get(db_id)
    if not description or not description.get("tables"):
        raise KeyError(f"no captured schema for db_id={db_id!r}")
    lines = ["Schema:"]
    for table in description["tables"]:
        lines.append(f"TABLE {table['name']}")
        for column in table["columns"]:
            lines.append(f"  - {column['name']} ({column['type']})")
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[:max_chars] + "\n... (truncated)"


def user_content(record: dict, max_schema_chars: int = 6000,
                 schema_text: str | None = None) -> str:
    """Learner-visible user turn: schema + question (+ evidence). Nothing else."""
    block = schema_text if schema_text is not None else schema_block(record["db_id"], max_schema_chars)
    parts = [block, "", f"Question: {record['question']}"]
    evidence = (record.get("evidence") or "").strip()
    if evidence:
        parts.append(f"Evidence: {evidence}")
    return "\n".join(parts).strip()


def messages(record: dict, mode: str, max_schema_chars: int = 6000,
             schema_text: str | None = None) -> list[dict]:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user_content(record, max_schema_chars, schema_text)},
    ]
    if mode == "train":
        sql = (record.get("pg_gold_sql") or "").strip()
        if not sql:
            raise ValueError(f"train mode needs pg_gold_sql: {record.get('sample_id')!r}")
        messages.append({"role": "assistant", "content": sql})
    return messages


def render(record: dict, mode: str = "train", max_schema_chars: int = 6000,
           schema_text: str | None = None) -> dict:
    """Render one record in ``train`` or ``serve`` mode through the model template.

    Returns ``{"prompt", "completion", "text", "template_applied_times", "mode"}``.
    ``prompt`` is the full prefix up to and including ``<|im_start|>assistant\\n``; it is
    identical in both modes. ``text`` adds the assistant answer (train only).
    """
    template = chat_template()
    prompt = template.render(messages=messages(record, "serve", max_schema_chars, schema_text),
                             add_generation_prompt=True, tools=None)
    if mode == "serve":
        return {"prompt": prompt, "completion": None, "text": prompt,
                "template_applied_times": 1, "mode": mode}
    conversation = template.render(messages=messages(record, "train", max_schema_chars, schema_text),
                                   add_generation_prompt=False, tools=None)
    if not conversation.startswith(prompt):
        raise AssertionError("train rendering does not start with the serve prompt")
    completion = (record.get("pg_gold_sql") or "").strip()
    expected_tail = completion + "<|im_end|>\n"
    if conversation[len(prompt):] != expected_tail:
        raise AssertionError("chat template tail does not match the gold SQL + one end-of-turn")
    return {"prompt": prompt, "completion": completion, "text": prompt + completion,
            "template_applied_times": 1, "mode": mode}


def render_pair(record: dict, max_schema_chars: int = 6000) -> dict:
    """Train and serve renderings of the same record, for parity tests."""
    return {"train": render(record, "train", max_schema_chars),
            "serve": render(record, "serve", max_schema_chars)}


def serialise(record: dict) -> str:
    """Chat text for one training row (prompt + answer), the trainer's input string."""
    return render(record, "train")["text"]
