r"""LoRA SFT data build: BIRD-train -> PostgreSQL targets, Ambrosia ambiguity head,
teacher distillation and feature-augmented prompts.

Ported from ``research/advtext2sql/r3-scratch/build_lora_data.py`` (the version the captain
executed and fixed) into the branch, then hardened:

  * ``--self-test`` -- offline end-to-end run on ``repo/data`` only, regression-asserted against
    the captain-measured baseline (dropped=14, usable=343, train/valid=291/52,
    ambrosia built=438 (394/44), combined=685).  No network, no tokenizer download.
  * schemas -- a real build requires ``--schemas db_schemas.json`` (a harness build run writes it,
    ``benchmarks/base.py:_dump_db_schemas_json``).  Missing db_ids abort the build unless
    ``--allow-missing-schemas`` is passed.  The smoke path may synthesise a schema corpus from the
    db's own gold SQL (``--allow-synth-schemas`` / ``--self-test``) -- that is labelled
    ``synth-from-gold`` everywhere, because it leaks the query's columns into the prompt.
  * ``--teacher`` -- distill: keep only teacher SQL that parses as PostgreSQL and matches gold
    execution (``training/teacher_filter.py``; execution is injected, never opened here).
  * ``--feature-prompt`` -- train the adapter under the same prompt surface the team uses at
    inference: the feature block is inserted verbatim into the user prompt.
  * ``--no-tokenizer`` / local-only tokenizer -- prompts fall back to a chars/3.6 token estimate
    and say so; nothing is ever downloaded.
  * determinism -- one ``random.Random(seed)``, fixed stage order, explicit ``\n`` newlines and
    no paths in ``stats.json``: two runs with the same seed produce byte-identical jsonl files.

Pipeline stages (the COUNT TABLE prints every stage):

  [1] BIRD train -> drop what the repo's own ``sqlite_to_postgres`` (a verbatim copy of
      ``benchmarks/evaluate_bird.py:15-30``) destroys: 14 queries it makes unparseable, plus the
      ones it silently *changes* while still parsing (measured: 1 -- the regex fires inside the
      quoted identifier "Charter School (Y/N)").  Survivors are canonicalised to PostgreSQL.
  [2] dedupe by question string and by normalised SQL within a db_id
  [3] split by db_id, seed 42, longest-SQL groups force-included in valid
  [4] Ambrosia ambiguity head: 146 ambiguous + negatives at 2:1 -> OK / "AMBIGUOUS: ..."
  [5] optional teacher distillation and feature block
  [6] serialise with the model chat template (only if a local tokenizer exists) + token stats
  [7] write jsonl + stats.json + dropped.json (+ teacher_report.json)

Usage (from the repo root, see ``training/README.md``)::

    python training/build_data.py --self-test
    python training/build_data.py --schemas db_schemas.json --out training/out
    python training/build_data.py --teacher training/fixtures/teacher_predictions.json \
        --teacher-results training/fixtures/teacher_results.json
    python training/build_data.py --feature-prompt training/feature_block.txt

Requires: ``sqlglot==30.23.0`` (hard version check; the measured baseline is a function of it).
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp, parse_one

_TRAINING_DIR = Path(__file__).resolve().parent
if str(_TRAINING_DIR) not in sys.path:  # so `import teacher_filter` works for script and package use
    sys.path.insert(0, str(_TRAINING_DIR))

import teacher_filter  # noqa: E402  (stdlib-only at import time)

REPO_ROOT = _TRAINING_DIR.parent
DATA_DIR = REPO_ROOT / "data"

SQLGLOT_PIN = "30.23.0"
# Measured baseline.  Two damage classes come out of the evaluator's own rewrite
# (research/advtext2sql/reconcile_damage.py for bird_large; the train file behaves the same way):
#   * 14 targets no longer parse as PostgreSQL at all -> dropped;
#   * 1 target (question_id 2) still parses but is silently changed -- the naive NULLIF regex fires
#     inside the quoted identifier "Charter School (Y/N)" and rewrites it to
#     "Charter School (Y / NULLIF(N,0))" -> also dropped.
# The self-test asserts both, so a silent drift in sqlglot, the data files or this pipeline is loud.
EXPECTED_BIRD_DROP = 14
EXPECTED_BIRD_MUTATED = 1
EXPECTED = {
    "bird_usable": 342,
    "bird_mutated": 1,
    "ambrosia_pos": 146,
    "ambrosia_neg_natural": 334,
    "ambrosia_neg_kept": 292,
    "ambrosia_built": 438,
    "ambrosia_train": 394,
    "ambrosia_valid": 44,
}

DEV_FILES_BIRD = ("bird_small.json", "bird_large.json")
DEV_FILES_AMBROSIA = ("ambrosia_small.json", "ambrosia_large.json")

# ------------------------------------------------------------------------------------------------
# Stage [1]: the evaluator's own rewrite, copied verbatim from benchmarks/evaluate_bird.py:15-30.
# Kept byte-compatible on purpose: this is exactly what the evaluator does to gold, therefore it is
# also exactly the test for "would this training target survive the evaluator?".  The only change
# is the stream of the parse-error print (stderr, so stdout stays machine-parseable).
# ------------------------------------------------------------------------------------------------
def sqlite_to_postgres(query: str) -> str:
    # Strip ```sql``` and comments first if needed
    query = re.sub(r"```sql|```", "", query, flags=re.IGNORECASE)
    query = re.sub(r"/\*.*?\*/", "", query, flags=re.DOTALL).strip()

    # Transpile using sqlglot
    try:
        query_pg = sqlglot.transpile(query, read="sqlite", write="postgres")[0]
    except Exception as error:  # noqa: BLE001 - the evaluator swallows this too
        print("SQLGlot parse error:", error, file=sys.stderr)
        query_pg = query

    # Fix division by zero
    query_pg = re.sub(r"(\b\w+\b)\s*/\s*(\b\w+\b)", r"\1 / NULLIF(\2,0)", query_pg)

    return query_pg


def transpile_only(query: str) -> str | None:
    """The same preparation and transpile as ``sqlite_to_postgres``, WITHOUT the NULLIF regex.

    This is the reference the rewrite is measured against: any difference that survives
    canonicalisation is damage the regex introduced, not the intended division-by-zero fix.
    Kept as a separate function (instead of splitting ``sqlite_to_postgres``) because that one is a
    byte-for-byte copy of the evaluator's code and must stay readable as such.
    """
    query = re.sub(r"```sql|```", "", query, flags=re.IGNORECASE)
    query = re.sub(r"/\*.*?\*/", "", query, flags=re.DOTALL).strip()
    try:
        return sqlglot.transpile(query, read="sqlite", write="postgres")[0]
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------------------------------------
# SQL canonicalisation
# ------------------------------------------------------------------------------------------------
def parse_pg_or_raise(sql: str) -> None:
    """Raise if ``sql`` is not parseable as PostgreSQL (the drop test's predicate)."""
    parse_one(sql, dialect="postgres")


def parses_as_pg(sql: str) -> bool:
    try:
        parse_pg_or_raise(sql)
        return True
    except Exception:  # noqa: BLE001 - any parser exception means "not valid PostgreSQL"
        return False


def canonicalise_sql(sql: str) -> str:
    """PostgreSQL-canonical target: NULLIF on column denominators, trailing semicolon removed."""
    tree = parse_one(sql, dialect="postgres")
    # x / col -> x / NULLIF(col, 0), only when the denominator is a bare column reference.
    for node in list(tree.find_all(exp.Div)):
        right = node.args.get("expression")
        if isinstance(right, exp.Column):
            node.set("expression", exp.Nullif(this=right.copy(), expression=exp.Literal.number(0)))
    return tree.sql(dialect="postgres", comments=False).strip().rstrip(";")


def normalized_key(sql: str) -> str:
    try:
        return parse_one(sql, dialect="postgres").sql(
            dialect="postgres", normalize=True, comments=False
        ).lower()
    except Exception:  # noqa: BLE001
        return re.sub(r"\s+", " ", sql).strip().lower()


# ------------------------------------------------------------------------------------------------
# Schema synthesis (Ambrosia's DBs are not in the schema corpus; the smoke path uses this for BIRD)
# ------------------------------------------------------------------------------------------------
UNKNOWN_SCHEMA = "TABLE unknown\n  - unknown (unknown)"


def synth_schema_from_sql(sql: str) -> str:
    """Best-effort light schema (table names + column names) extracted from one SQL statement.

    Ambrosia is a *variance* benchmark: its SQL is never executed, and the ambiguity is designed to
    persist with the database context, so a schema reconstructed from the gold SQL is legitimate
    there.  For BIRD it is a smoke-test stand-in only (see ``schema_basis``).
    """
    if not sql or not sql.strip():
        return UNKNOWN_SCHEMA
    tree = None
    try:
        tree = parse_one(sql, dialect="sqlite", error_level=sqlglot.ErrorLevel.IGNORE)
    except Exception:  # noqa: BLE001 - fall through to the regex extractor
        tree = None

    tables: set[str] = set()
    columns: dict[str, set[str]] = {}
    if tree is not None:
        for table in tree.find_all(exp.Table):
            if table.name:
                tables.add(table.name)
                columns.setdefault(table.name, set())
        for column in tree.find_all(exp.Column):
            owner = column.table or (next(iter(tables)) if len(tables) == 1 else None)
            if owner:
                columns.setdefault(owner, set()).add(column.name)

    if not tables:
        # Last resort: substring extraction.  Only reached when sqlglot returns nothing usable.
        for match in re.finditer(r"\bFROM\s+([A-Za-z_]\w*)", sql, re.IGNORECASE):
            tables.add(match.group(1))
            columns.setdefault(match.group(1), set())
        for match in re.finditer(r"\bJOIN\s+([A-Za-z_]\w*)", sql, re.IGNORECASE):
            tables.add(match.group(1))
            columns.setdefault(match.group(1), set())
        select = re.search(r"\bSELECT\s+(.*?)\s+FROM\b", sql, re.IGNORECASE | re.DOTALL)
        if select and len(tables) == 1:
            owner = next(iter(tables))
            for raw in select.group(1).split(","):
                token = raw.strip().split()
                if token and re.fullmatch(r"[A-Za-z_]\w*", token[0]):
                    columns[owner].add(token[0])

    if not tables:
        return UNKNOWN_SCHEMA

    lines = []
    for name in sorted(tables):
        lines.append(f"TABLE {name}")
        for column in sorted(columns.get(name, set())):
            lines.append(f"  - {column} (unknown)")
        lines.append("")
    return "\n".join(lines).strip()


def synth_schemas_for_bird(items: list[dict]) -> dict:
    """One synthesised schema per db_id, from the union of that db's own gold targets."""
    per_db: dict[str, list[str]] = defaultdict(list)
    for item in items:
        per_db[item["db_id"]].append(item["_target"])
    schemas = {}
    for db_id, queries in per_db.items():
        tables: dict[str, set[str]] = {}
        for query in queries:
            fragment = synth_schema_from_sql(query)
            current = None
            for line in fragment.splitlines():
                if line.startswith("TABLE "):
                    current = line[len("TABLE "):].strip()
                    tables.setdefault(current, set())
                elif line.strip().startswith("- ") and current:
                    tables[current].add(line.strip()[2:].split(" (")[0])
        if not tables:
            schemas[db_id] = UNKNOWN_SCHEMA
            continue
        lines = []
        for name in sorted(tables):
            lines.append(f"TABLE {name}")
            for column in sorted(tables[name]):
                lines.append(f"  - {column} (unknown)")
            lines.append("")
        schemas[db_id] = "\n".join(lines).strip()
    return schemas


# ------------------------------------------------------------------------------------------------
# Ambrosia's `ambig_queries`: a Python repr of a list, not JSON -> literal_eval fallback chain
# ------------------------------------------------------------------------------------------------
def parse_ambig_queries(raw: Any, _depth: int = 0) -> tuple[list[str], str]:
    """Return ``(list_of_sql_strings, basis)``.

    The field is a Python ``repr`` (``"['SELECT ...', ...]"``): ``json.loads`` raises on it, which
    silently gave 129/146 ambiguous rows the "TABLE unknown" stub before the captain's audit
    (defect R4-2).  Chain: already-a-list -> ``ast.literal_eval`` -> ``json.loads`` ->
    (double-encoded) recurse once -> regex salvage -> empty.
    """
    if isinstance(raw, (list, tuple)):
        items = [str(value) for value in raw if isinstance(value, str) and str(value).strip()]
        return items, "already-list"
    if not isinstance(raw, str) or not raw.strip():
        return [], "empty"

    text = raw.strip()
    for basis, loader in (("literal_eval", ast.literal_eval), ("json", json.loads)):
        try:
            value = loader(text)
        except Exception:  # noqa: BLE001 - try the next loader
            continue
        if isinstance(value, str) and _depth < 2:
            inner, inner_basis = parse_ambig_queries(value, _depth=_depth + 1)
            if inner:
                return inner, f"{basis}->{inner_basis}"
        if isinstance(value, (list, tuple)):
            items = [str(entry) for entry in value if isinstance(entry, str) and str(entry).strip()]
            if items:
                return items, basis
    matches = re.findall(r"'((?:SELECT|WITH)[^']*)'", text, re.IGNORECASE | re.DOTALL)
    if matches:
        salvaged = []
        for match in matches:
            try:
                salvaged.append(match.encode("utf-8").decode("unicode_escape"))
            except Exception:  # noqa: BLE001
                salvaged.append(match)
        return salvaged, "regex"
    return [], "none"


# ------------------------------------------------------------------------------------------------
# Serialisation
# ------------------------------------------------------------------------------------------------
SYS_SQL = (
    "You are a PostgreSQL expert. Translate the question into ONE SELECT statement.\n"
    "Rules:\n"
    "- Use only tables and columns from the schema below. Quote identifiers exactly as written.\n"
    "- PostgreSQL dialect: single quotes for strings, double quotes only for identifiers, "
    "no backticks, no IFNULL/IIF/STRFTIME, no trailing semicolon.\n"
    "- Use x / NULLIF(y, 0) for every division whose denominator is a column.\n"
    "- Never add comments, markdown fences, or explanations.\n"
    "Output: SQL only."
)

SYS_AMB = (
    "You decide whether a question can be answered by exactly one SQL query over the given schema.\n"
    'Answer exactly "OK" if it can. Otherwise answer "AMBIGUOUS: <one clause naming what is unclear>".\n'
    'Everything you write that is not exactly "OK" is treated as ambiguous, so never explain an OK.'
)

AMBIGUOUS_TARGET = "AMBIGUOUS: the question has more than one reasonable interpretation"


def build_prompt(tokenizer, system: str, user: str) -> str:
    """System+user as training text, ending with the assistant header.

    ``add_generation_prompt=True`` is deliberate: with TRL's prompt/completion columns the rendered
    prompt must end with the assistant role header, otherwise the completion is trained as if it
    were user text.  (Design doc §2.1 says ``False`` for training; that would drop the role token --
    the executed scratch pipeline, which produced the baseline numbers, uses ``True``.)
    """
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    if tokenizer is not None:
        return tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    return (
        f"<|im_start|>system\n{system}<|im_end|>\n"
        f"<|im_start|>user\n{user}<|im_end|>\n<|im_start|>assistant\n"
    )


def render_sql_user(schema: str, evidence: str, question: str, feature_block: str | None = None) -> str:
    parts = [f"Schema:\n{schema}"]
    if feature_block:
        # Verbatim: no re-indent, no wrapping, no placeholder substitution.
        parts.append(f"Feature block:\n{feature_block}")
    if evidence:
        parts.append(f"Evidence: {evidence}")
    parts.append(f"Question: {question}")
    return "\n".join(parts)


def render_amb_user(schema: str, question: str) -> str:
    return f"Schema:\n{schema}\nQuestion: {question}"


def est_tokens(text: str) -> int:
    """Char/3.6 estimate, used only when no local tokenizer is available (labelled in stats)."""
    return max(1, int(round(len(text) / 3.6)))


def pct(values: list[int], q: float):
    if not values:
        return None
    values = sorted(values)
    index = min(len(values) - 1, max(0, int(round(q * (len(values) - 1)))))
    return values[index]


def load_tokenizer(model_name: str | None):
    """Return ``(tokenizer_or_None, basis)``.  Never downloads: local files only."""
    if model_name is None:
        return None, "chars/3.6 estimate (tokenizer disabled)"
    try:
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(model_name, use_fast=True, local_files_only=True)
        return tokenizer, f"tokenizer:{model_name}"
    except Exception as error:  # noqa: BLE001 - missing package or missing local snapshot
        print(
            f"[tokenizer] unavailable ({type(error).__name__}: {str(error)[:160]}); "
            f"using chars/3.6 token estimate",
            file=sys.stderr,
        )
        return None, "chars/3.6 estimate (no local tokenizer)"


# ------------------------------------------------------------------------------------------------
# I/O helpers (byte-stable: explicit \n, sha256 of what actually landed on disk)
# ------------------------------------------------------------------------------------------------
def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dump_jsonl(path: str, rows: list[dict]) -> str:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def write_json(path: str, payload: Any) -> str:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    return path


def read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


# ------------------------------------------------------------------------------------------------
# Guards
# ------------------------------------------------------------------------------------------------
class BuildError(RuntimeError):
    """A user/data error: printed as ``ERROR: ...`` and turned into exit code 2."""


def check_sqlglot_version(allow_drift: bool) -> str:
    version = getattr(sqlglot, "__version__", "unknown")
    if version != SQLGLOT_PIN and not allow_drift:
        raise BuildError(
            f"sqlglot {version} != pinned {SQLGLOT_PIN}. Every count in this pipeline (dropped=14, "
            f"the 241->211 gold ceiling, the split) was measured with {SQLGLOT_PIN}, and the "
            f"evaluator's transpile behaviour is a function of it. Install sqlglot=={SQLGLOT_PIN} "
            f"or pass --allow-version-drift and re-derive the numbers."
        )
    if version != SQLGLOT_PIN:
        print(
            f"WARNING: sqlglot {version} != pinned {SQLGLOT_PIN}; counts are NOT comparable to the "
            f"measured baseline (--allow-version-drift).",
            file=sys.stderr,
        )
    return version


def normalise_question(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _load_dev(data_dir: Path, names: tuple[str, ...]) -> dict:
    ids: set[str] = set()
    questions: set[str] = set()
    db_ids: set[str] = set()
    per_file = {}
    for name in names:
        path = data_dir / name
        if not path.exists():
            per_file[name] = {"present": False}
            continue
        items = read_json(str(path))
        per_file[name] = {"present": True, "rows": len(items)}
        for item in items:
            ids.add(str(item.get("question_id")))
            questions.add((item.get("question") or ""))
            if item.get("db_id"):
                db_ids.add(str(item["db_id"]))
    return {
        "ids": ids,
        "questions": questions,
        "norms": {normalise_question(text) for text in questions},
        "db_ids": db_ids,
        "files": per_file,
    }


def check_leakage(bird_rows: list[dict], amb_rows: list[dict], data_dir: Path) -> dict:
    """Assert that no evaluation-set question reaches training.  Raises ``BuildError`` on overlap.

    The check is per benchmark on purpose: question ids are only unique *inside* a benchmark
    (``ambrosia_train`` id 1 and ``bird_large`` id 1 are unrelated questions), so a global id set
    would report false leakage.  For Ambrosia we also assert database disjointness: the benchmark
    is a transfer test, and sharing a db_id would turn it into memorisation.
    """
    dev = {
        "bird": _load_dev(data_dir, DEV_FILES_BIRD),
        "ambrosia": _load_dev(data_dir, DEV_FILES_AMBROSIA),
    }
    report: dict[str, Any] = {"files": {}, "status": "clean"}
    problems = []

    for benchmark, rows in (("bird", bird_rows), ("ambrosia", amb_rows)):
        reference = dev[benchmark]
        report["files"].update(reference["files"])
        ids = {str(row["question_id"]) for row in rows}
        questions = {(row.get("question") or "") for row in rows}
        norms = {normalise_question(row.get("question") or "") for row in rows}
        checks = {
            "dev_question_ids": len(reference["ids"]),
            "train_question_ids": len(ids),
            "id_overlap": sorted(ids & reference["ids"]),
            "question_overlap": sorted(questions & reference["questions"]),
            "normalised_overlap": sorted(norms & reference["norms"]),
        }
        if benchmark == "ambrosia":
            train_db_ids = {str(row["db_id"]) for row in rows}
            checks["db_id_overlap"] = sorted(train_db_ids & reference["db_ids"])
            checks["train_db_ids"] = len(train_db_ids)
        report[benchmark] = checks
        for key in ("id_overlap", "question_overlap", "normalised_overlap", "db_id_overlap"):
            if checks.get(key):
                problems.append(f"{benchmark}.{key}={checks[key][:5]} ({len(checks[key])})")

    report["problems"] = problems
    if problems:
        report["status"] = "LEAK"
        raise BuildError(
            "evaluation-set leakage into the training rows: " + "; ".join(problems) +
            ". Training rows must never come from bird_small/bird_large/ambrosia_small/"
            "ambrosia_large."
        )
    return report


# ------------------------------------------------------------------------------------------------
# The build
# ------------------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build LoRA SFT data from repo/data (BIRD + Ambrosia).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--train", default=None, help="repo/data/train_queries.json")
    parser.add_argument("--ambrosia", default=None, help="repo/data/ambrosia_train.json")
    parser.add_argument("--schemas", default=None,
                        help="JSON {db_id: schema_string} dumped by a harness build run "
                             "(benchmarks/base.py). Required for a real build.")
    parser.add_argument("--out", default=None, help="output directory")
    parser.add_argument("--self-test", action="store_true",
                        help="offline end-to-end run on repo/data with the measured baseline asserted")
    parser.add_argument("--valid-frac", type=float, default=0.15)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--ambrosia-neg-ratio", type=float, default=2.0,
                        help="negatives per positive for the ambiguity head (0 = keep natural)")
    parser.add_argument("--ambrosia-valid-frac", type=float, default=0.10)
    parser.add_argument("--tokenizer-model", default=None,
                        help="load this model's chat template from the local HF cache "
                             "(never downloads); off by default in --self-test")
    parser.add_argument("--no-tokenizer", action="store_true",
                        help="never touch transformers; use the chars/3.6 token estimate")
    parser.add_argument("--allow-missing-schemas", action="store_true",
                        help="emit a stub schema for db_ids absent from --schemas (dry run only)")
    parser.add_argument("--allow-synth-schemas", action="store_true",
                        help="SMOKE ONLY: synthesise BIRD schemas from gold SQL when --schemas is "
                             "absent (leaks the query's own columns into the prompt)")
    parser.add_argument("--allow-version-drift", action="store_true",
                        help=f"continue even if sqlglot != {SQLGLOT_PIN} (counts stop being comparable)")
    parser.add_argument("--allow-drop-mismatch", action="store_true",
                        help=f"continue even if dropped != {EXPECTED_BIRD_DROP}")
    parser.add_argument("--teacher", default=None,
                        help="teacher predictions (JSON object/list or JSONL) -> distillation mode")
    parser.add_argument("--teacher-results", default=None,
                        help="recorded execution results for --teacher (offline replay executor)")
    parser.add_argument("--teacher-require-exec", action="store_true",
                        help="with --teacher and no --teacher-results: keep nothing (verified "
                             "distillation was demanded, no executor was injected)")
    parser.add_argument("--teacher-mode", choices=("fallback", "strict"), default="fallback",
                        help="fallback: gold target where the teacher was dropped; "
                             "strict: keep only teacher-verified rows")
    parser.add_argument("--feature-prompt", default=None,
                        help="text file inserted verbatim into the SQL prompt (feature-augmented mode)")
    return parser


def resolve_paths(args: argparse.Namespace) -> None:
    data_dir = DATA_DIR
    if args.train is None:
        args.train = str(data_dir / "train_queries.json")
    if args.ambrosia is None:
        args.ambrosia = str(data_dir / "ambrosia_train.json")
    if args.out is None:
        args.out = str(_TRAINING_DIR / ("self_test_out" if args.self_test else "out"))
    if args.self_test and args.tokenizer_model is None:
        args.no_tokenizer = True  # self-test is offline by contract: no download, no HF cache reads


def stage_bird(args: argparse.Namespace) -> dict:
    """Stages [1]-[2]: rewrite/drop test and dedupe."""
    bird = read_json(args.train)
    if not isinstance(bird, list):
        raise BuildError(f"--train must be a JSON list of items: {args.train}")
    n_raw = len(bird)
    n_backtick = sum(1 for item in bird if "`" in (item.get("SQL") or ""))
    sqlite_fn = re.compile(r"IIF\s*\(|STRFTIME\s*\(|IFNULL\s*\(|DATETIME\s*\(|JULIANDAY\s*\(", re.IGNORECASE)
    n_sqlite_fn = sum(1 for item in bird if sqlite_fn.search(item.get("SQL") or ""))

    kept: list[dict] = []
    dropped: list[dict] = []
    mutated: list[dict] = []
    for item in bird:
        sql = (item.get("SQL") or "").strip()
        if not sql:
            dropped.append({"question_id": item.get("question_id"), "db_id": item.get("db_id"),
                            "reason": "empty SQL"})
            continue
        query_pg = sqlite_to_postgres(sql)  # exactly what the evaluator does to gold
        try:
            parse_pg_or_raise(query_pg)
        except Exception as error:  # noqa: BLE001
            dropped.append({
                "question_id": item.get("question_id"), "db_id": item.get("db_id"),
                "reason": "unparseable after repo sqlite_to_postgres",
                "detail": f"{type(error).__name__}: {error}",
                "gold": sql, "rewritten": query_pg,
            })
            continue
        try:
            target = canonicalise_sql(query_pg)
        except Exception as error:  # noqa: BLE001
            dropped.append({
                "question_id": item.get("question_id"), "db_id": item.get("db_id"),
                "reason": "canonicalise-failed", "detail": f"{type(error).__name__}: {error}",
                "gold": sql,
            })
            continue

        # Second damage class: the regex changed the query but it still parses.  Compare against
        # the plain sqlite->postgres transpile *after canonicalisation*, so a formatting difference
        # or the intended `x / col -> x / NULLIF(col, 0)` rewrite is not mistaken for damage.
        plain_pg = transpile_only(sql)
        if plain_pg is not None:
            try:
                plain_target = canonicalise_sql(plain_pg)
            except Exception:  # noqa: BLE001 - cannot prove damage; keep the target
                plain_target = None
            if plain_target is not None and plain_target != target:
                mutated.append({
                    "question_id": item.get("question_id"), "db_id": item.get("db_id"),
                    "reason": "mutated-but-parseable",
                    "detail": "the evaluator's NULLIF regex changed the query without breaking the "
                              "parse; the target would teach the regex's damage",
                    "gold": sql, "rewritten": query_pg, "plain_transpile": plain_pg,
                    "mutated_target": target, "plain_target": plain_target,
                })
                continue
        kept.append({**item, "_target": target, "_target_source": "gold"})

    print(f"[BIRD] raw={n_raw} backticks={n_backtick} sqlite_fns={n_sqlite_fn} "
          f"unparseable={len(dropped)} mutated={len(mutated)} kept={len(kept)}")
    if mutated:
        print(f"[BIRD] mutated ids={[row['question_id'] for row in mutated]} "
              f"(dropped: a silently altered target teaches the regex's damage)")
    if len(dropped) != EXPECTED_BIRD_DROP and not args.allow_drop_mismatch:
        raise BuildError(
            f"expected {EXPECTED_BIRD_DROP} items destroyed by the repo's own sqlite_to_postgres "
            f"(measured with sqlglot {SQLGLOT_PIN}), got {len(dropped)}. Re-derive the numbers or "
            f"pass --allow-drop-mismatch."
        )
    if len(mutated) != EXPECTED_BIRD_MUTATED and not args.allow_drop_mismatch:
        raise BuildError(
            f"expected {EXPECTED_BIRD_MUTATED} item silently changed but still parseable by the "
            f"repo's NULLIF regex (question_id 2, identifier \"Charter School (Y/N)\"), got "
            f"{len(mutated)}: {[row['question_id'] for row in mutated]}. Re-derive the numbers or "
            f"pass --allow-drop-mismatch."
        )

    seen_questions: set[str] = set()
    seen_sql: set[str] = set()
    deduped: list[dict] = []
    n_dup_question = n_dup_sql = 0
    for item in kept:
        question = item["question"].strip().lower()
        key = (item.get("db_id"), normalized_key(item["_target"]))
        if question in seen_questions:
            n_dup_question += 1
            continue
        if key in seen_sql:
            n_dup_sql += 1
            continue
        seen_questions.add(question)
        seen_sql.add(key)
        deduped.append(item)
    print(f"[dedupe] dropped duplicate questions={n_dup_question} "
          f"duplicate normalized SQL={n_dup_sql} -> {len(deduped)}")
    return {
        "n_raw": n_raw,
        "n_backtick": n_backtick,
        "n_sqlite_fn": n_sqlite_fn,
        "kept": kept,
        "deduped": deduped,
        "dropped": dropped,
        "mutated": mutated,
        "n_dup_question": n_dup_question,
        "n_dup_sql": n_dup_sql,
    }


def stage_split(args: argparse.Namespace, rng: random.Random, deduped: list[dict]) -> dict:
    """Stage [3]: split by db_id, stratified, longest-SQL groups force-included in valid."""
    by_db: dict[str, list[dict]] = defaultdict(list)
    for item in deduped:
        by_db[item["db_id"]].append(item)

    train_rows: list[dict] = []
    valid_rows: list[dict] = []
    split_table = []
    for db_id in sorted(by_db):
        items = by_db[db_id]
        n = len(items)
        quota = max(1, int(round(n * args.valid_frac))) if n > 1 else 0
        quota = min(quota, n - 1) if n > 1 else 0
        ranked = sorted(items, key=lambda row: len(row["_target"]), reverse=True)
        chosen: list[dict] = []
        chosen_ids: set[int] = set()
        seen_lengths: set[str] = set()
        for item in ranked:
            if len(chosen) >= min(3, quota):
                break
            key = normalized_key(item["_target"])[:80]
            if key in seen_lengths:
                continue
            seen_lengths.add(key)
            chosen.append(item)
            chosen_ids.add(id(item))
        rest = [item for item in items if id(item) not in chosen_ids]
        rng.shuffle(rest)
        while len(chosen) < quota and rest:
            chosen.append(rest.pop())
        valid_rows.extend(chosen)
        train_rows.extend(rest)
        split_table.append({"db_id": db_id, "n": n, "train": len(rest), "valid": len(chosen)})

    print(f"[split] train={len(train_rows)} valid={len(valid_rows)} over {len(by_db)} dbs")
    for row in split_table:
        print(f"        {row['db_id']:<28} n={row['n']:<4} train={row['train']:<4} "
              f"valid={row['valid']}")
    return {"train": train_rows, "valid": valid_rows, "table": split_table}


def stage_ambrosia(args: argparse.Namespace, rng: random.Random) -> dict:
    """Stage [4]: the ambiguity head."""
    ambrosia = read_json(args.ambrosia)
    if not isinstance(ambrosia, list):
        raise BuildError(f"--ambrosia must be a JSON list of items: {args.ambrosia}")
    positives = [item for item in ambrosia if (item.get("SQL") or "").strip() == "ambiguous"]
    negatives = [item for item in ambrosia if (item.get("SQL") or "").strip() != "ambiguous"]
    n_pos, n_neg_natural = len(positives), len(negatives)
    if args.ambrosia_neg_ratio and args.ambrosia_neg_ratio > 0:
        want = int(round(n_pos * args.ambrosia_neg_ratio))
        if want < n_neg_natural:
            negatives = rng.sample(negatives, want)
    n_neg_kept = len(negatives)
    print(f"[ambrosia] rows={len(ambrosia)} ambiguous={n_pos} unambiguous(natural)={n_neg_natural} "
          f"negatives kept={n_neg_kept} (ratio {args.ambrosia_neg_ratio})")

    rows: list[dict] = []
    skipped = 0
    basis_counter: Counter = Counter()
    unknown_schema: list[str] = []
    for item in positives + negatives:
        is_ambiguous = (item.get("SQL") or "").strip() == "ambiguous"
        if is_ambiguous:
            target = AMBIGUOUS_TARGET
            parsed, basis = parse_ambig_queries(item.get("ambig_queries"))
            basis_counter[basis] += 1
            schema_sql = parsed[0] if parsed else ""
            schema = synth_schema_from_sql(schema_sql) if schema_sql else UNKNOWN_SCHEMA
        else:
            try:
                query_pg = sqlglot.transpile(item["SQL"], read="sqlite", write="postgres")[0]
                target = canonicalise_sql(query_pg)
            except Exception:  # noqa: BLE001 - an un-transpilable negative is dropped, not repaired
                skipped += 1
                continue
            schema = synth_schema_from_sql(item["SQL"])
            basis_counter["negative-gold-sql"] += 1
        if schema == UNKNOWN_SCHEMA:
            unknown_schema.append(str(item.get("question_id")))
        rows.append({
            "question_id": item.get("question_id"),
            "db_id": item.get("db_id"),
            "question": item["question"],
            "schema": schema,
            "target": target,
            "label": "ambiguous" if is_ambiguous else "ok",
        })

    rng.shuffle(rows)
    n_valid = max(1, int(round(len(rows) * args.ambrosia_valid_frac))) if rows else 0
    valid_rows, train_rows = rows[:n_valid], rows[n_valid:]
    print(f"[ambrosia] built={len(rows)} (skipped {skipped}) train={len(train_rows)} "
          f"valid={len(valid_rows)}; schema basis={dict(sorted(basis_counter.items()))}")
    if unknown_schema:
        print(f"WARNING: {len(unknown_schema)} Ambrosia rows still have the 'TABLE unknown' stub "
              f"(ids {unknown_schema[:5]})", file=sys.stderr)
    return {
        "total": len(ambrosia),
        "pos": n_pos,
        "neg_natural": n_neg_natural,
        "neg_kept": n_neg_kept,
        "built": rows,
        "skipped": skipped,
        "train": train_rows,
        "valid": valid_rows,
        "basis": dict(basis_counter),
        "unknown_schema_ids": unknown_schema,
    }


def resolve_schemas(args: argparse.Namespace, deduped: list[dict]) -> tuple[dict, str]:
    """Return ``(schemas, basis)`` or raise.  The prompt must show the harness schema, not a guess."""
    db_ids = sorted({item["db_id"] for item in deduped})
    if args.schemas:
        if not os.path.exists(args.schemas):
            raise BuildError(f"--schemas file not found: {args.schemas}")
        schemas = read_json(args.schemas)
        if not isinstance(schemas, dict):
            raise BuildError(f"--schemas must be a JSON object {{db_id: schema_string}}: {args.schemas}")
        missing = [db_id for db_id in db_ids if db_id not in schemas]
        if missing and not args.allow_missing_schemas:
            raise BuildError(
                f"no schema for {len(missing)}/{len(db_ids)} db_id(s) in {args.schemas}: "
                f"{missing[:8]}{' ...' if len(missing) > 8 else ''}. The prompt must contain the "
                f"same schema text the harness shows at inference. Produce db_schemas.json with one "
                f"harness build run (benchmarks/base.py), or pass --allow-missing-schemas for a "
                f"dry run that emits stub schemas."
            )
        for db_id in missing:
            schemas[db_id] = f"TABLE unknown  -- STUB, db_id={db_id} (DRY RUN)"
        if missing:
            return schemas, "file+stub"
        return schemas, "file"

    if args.self_test or args.allow_synth_schemas:
        print("[schemas] SYNTH-FROM-GOLD: smoke-only corpus (leaks the query's own columns into "
              "the prompt); a real build must pass --schemas db_schemas.json")
        return synth_schemas_for_bird(deduped), "synth-from-gold"

    raise BuildError(
        "--schemas is required: without the harness schema dump the prompt cannot match the "
        "inference surface (BIRD db_ids: " + ", ".join(db_ids[:4]) + ", ...). Pass "
        "--schemas db_schemas.json, or --allow-synth-schemas for a smoke-only build. "
        "(--allow-missing-schemas only fills db_ids absent from an *existing* --schemas file; "
        "it does not create a corpus -- see captain-verified-numbers.md defect D-R3-2.)"
    )


def stage_teacher(args: argparse.Namespace, bird: dict, train_rows: list[dict]) -> dict | None:
    """Stage [5a]: distillation.  Returns the report (or None when not requested)."""
    if not args.teacher:
        return None
    if not os.path.exists(args.teacher):
        raise BuildError(f"--teacher file not found: {args.teacher}")
    predictions = teacher_filter.load_predictions(args.teacher)
    gold = {str(item["question_id"]): item["_target"] for item in bird["kept"]}

    execute = None
    replay_file = None
    if args.teacher_results:
        if not os.path.exists(args.teacher_results):
            raise BuildError(f"--teacher-results file not found: {args.teacher_results}")
        replay_file = os.path.basename(args.teacher_results)
        execute = teacher_filter.ReplayExecutor(
            gold=gold, recorded=teacher_filter.load_replay_results(args.teacher_results)
        )
    result = teacher_filter.filter_predictions(
        predictions, gold,
        execute=execute,
        parse=parse_pg_or_raise,
        canonicalise=canonicalise_sql,
        require_exec=args.teacher_require_exec,
    )

    print(f"[teacher] file={os.path.basename(args.teacher)} mode="
          f"{'exec-verified' if result.exec_checked else 'parse-only'} "
          f"attempted={result.attempted} kept={len(result.kept)} dropped={len(result.dropped)} "
          f"not_attempted={result.not_attempted}")
    for reason, count in sorted(result.by_reason.items()):
        print(f"        {reason:<22} {count}")

    kept_sql = result.kept_sql_by_id()
    applied = 0
    for row in train_rows:
        teacher_sql = kept_sql.get(str(row["question_id"]))
        if teacher_sql is not None:
            row["_target"] = teacher_sql
            row["_target_source"] = "teacher"
            applied += 1
    if args.teacher_mode == "strict":
        before = len(train_rows)
        train_rows[:] = [row for row in train_rows if row["_target_source"] == "teacher"]
        print(f"[teacher] strict mode: dropped {before - len(train_rows)} train rows without a "
              f"verified teacher target")
    print(f"[teacher] applied to {applied} BIRD train rows (valid rows keep gold targets)")

    report = result.as_dict()
    report["predictions_file"] = os.path.basename(args.teacher)
    report["replay_file"] = replay_file
    report["mode_flag"] = args.teacher_mode
    report["applied_to_train_rows"] = applied
    return report


def load_feature_block(path: str) -> str:
    if not os.path.exists(path):
        raise BuildError(f"--feature-prompt file not found: {path}")
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    text = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    if not text.strip():
        raise BuildError(f"--feature-prompt file is empty: {path}")
    return text


def serialise_bird(rows: list[dict], schemas: dict, tokenizer, feature_block: str | None) -> list[dict]:
    out = []
    for item in rows:
        db_id = item["db_id"]
        schema = schemas[db_id]
        evidence = item.get("evidence") or ""
        user = render_sql_user(schema, evidence, item["question"], feature_block)
        out.append({
            "prompt": build_prompt(tokenizer, SYS_SQL, user),
            "completion": item["_target"],
            "task": "sql",
            "db_id": db_id,
            "question_id": str(item.get("question_id")),
            "target_source": item.get("_target_source", "gold"),
        })
    return out


def serialise_ambrosia(rows: list[dict], tokenizer) -> list[dict]:
    out = []
    for item in rows:
        user = render_amb_user(item["schema"], item["question"])
        out.append({
            "prompt": build_prompt(tokenizer, SYS_AMB, user),
            "completion": item["target"],
            "task": "ambiguity",
            "db_id": item["db_id"],
            "question_id": str(item["question_id"]),
            "target_source": "gold",
            "label": item["label"],
        })
    return out


def count_table(stats: dict) -> str:
    lines = [
        "=================== COUNT TABLE ===================",
        f"schema_basis={stats['schema_basis']}  seed={stats['seed']}  "
        f"valid_frac={stats['valid_frac']}  token_basis={stats['token_basis']}",
        f"dropped={stats['bird_dropped']}  mutated={stats['bird_mutated']}  "
        f"usable={stats['bird_usable']}  train/valid={stats['bird_train']}/{stats['bird_valid']}",
        f"ambrosia built={stats['ambrosia_built']} "
        f"({stats['ambrosia_train']}/{stats['ambrosia_valid']})  "
        f"combined={stats['combined_train']}",
        "---------------------- detail ---------------------",
        f"sqlglot                      {stats['sqlglot']}",
        f"BIRD raw                     {stats['n_raw_bird']}",
        f"  with backticks             {stats['bird_backtick']}",
        f"  with sqlite-only fns       {stats['bird_sqlite_fns']}",
        f"  unparseable after rewrite  {stats['bird_dropped']}",
        f"  mutated (semantics changed){stats['bird_mutated']:>4}   "
        f"-> parseable {stats['bird_parseable']}, usable {stats['bird_usable']}",
        f"  usable                     {stats['bird_usable']}",
        f"  duplicate questions        {stats['dup_questions']}",
        f"  duplicate normalized SQL   {stats['dup_sql']}",
        f"  train/valid                {stats['bird_train']}/{stats['bird_valid']}",
        f"Ambrosia rows                {stats['ambrosia_total']} (ambiguous "
        f"{stats['ambrosia_pos']}, ok {stats['ambrosia_neg_natural']})",
        f"  negatives kept             {stats['ambrosia_neg_kept']}",
        f"  built                      {stats['ambrosia_built']} "
        f"(skipped {stats['ambrosia_skipped']})",
        f"  train/valid                {stats['ambrosia_train']}/{stats['ambrosia_valid']}",
        f"combined training rows       {stats['combined_train']}",
        f"sequence length {stats['token_basis']}",
        f"  p50/p95/p99/max            {stats['seq_p50']}/{stats['seq_p95']}/"
        f"{stats['seq_p99']}/{stats['seq_max']}",
        f"leakage                      {stats['leakage']['status']} "
        f"(bird qids {stats['leakage']['bird']['train_question_ids']} vs dev "
        f"{stats['leakage']['bird']['dev_question_ids']}, "
        f"ambrosia dbs {stats['leakage']['ambrosia'].get('train_db_ids')} vs dev, "
        f"overlaps 0)",
        "===================================================",
    ]
    return "\n".join(lines)


def check_baseline(stats: dict) -> None:
    """Assert the damage counters the captain reconciled, plus internal consistency.

    The split (train/valid) is *not* hard-coded: it is a function of which db the mutated item
    belongs to and is printed/recorded instead.  The damage counters and the Ambrosia head are
    hard-coded because they are what every downstream number depends on.
    """
    mismatches = []
    observed = {
        "bird_usable": stats["bird_usable"],
        "bird_mutated": stats["bird_mutated"],
        "ambrosia_pos": stats["ambrosia_pos"],
        "ambrosia_neg_natural": stats["ambrosia_neg_natural"],
        "ambrosia_neg_kept": stats["ambrosia_neg_kept"],
        "ambrosia_built": stats["ambrosia_built"],
        "ambrosia_train": stats["ambrosia_train"],
        "ambrosia_valid": stats["ambrosia_valid"],
    }
    for key, expected in EXPECTED.items():
        if observed[key] != expected:
            mismatches.append(f"{key}: expected {expected}, got {observed[key]}")
    if stats["bird_usable"] != stats["bird_train"] + stats["bird_valid"]:
        mismatches.append(
            f"split does not add up: train {stats['bird_train']} + valid {stats['bird_valid']} "
            f"!= usable {stats['bird_usable']}"
        )
    if stats["bird_dropped"] != EXPECTED_BIRD_DROP:
        mismatches.append(f"bird_dropped: expected {EXPECTED_BIRD_DROP}, got {stats['bird_dropped']}")
    if stats["combined_train"] != stats["bird_train"] + stats["ambrosia_train"]:
        mismatches.append(
            f"combined {stats['combined_train']} != bird_train {stats['bird_train']} + "
            f"ambrosia_train {stats['ambrosia_train']}"
        )
    if mismatches:
        raise BuildError(
            "--self-test did not reproduce the measured baseline:\n  " + "\n  ".join(mismatches)
            + "\nEvery downstream number (steps, cost, EX comparison) assumes these counts. "
              "Re-derive them before training; do not paper over the drift."
        )


def build(args: argparse.Namespace) -> int:
    resolve_paths(args)
    version = check_sqlglot_version(args.allow_version_drift)
    os.makedirs(args.out, exist_ok=True)
    rng = random.Random(args.seed)

    tokenizer, token_basis = load_tokenizer(None if args.no_tokenizer else args.tokenizer_model)
    if tokenizer is not None:
        print(f"[tokenizer] {args.tokenizer_model} (local files only)")
    else:
        print(f"[tokenizer] none: {token_basis}")

    bird = stage_bird(args)
    split = stage_split(args, rng, bird["deduped"])
    schemas, schema_basis = resolve_schemas(args, bird["deduped"])
    ambrosia = stage_ambrosia(args, rng)

    # Leakage is asserted on the *source items* (the serialised rows no longer carry the raw
    # question string) and before anything is written: a build that leaks must leave no artifact.
    leakage = check_leakage(
        [{"question_id": item.get("question_id"), "question": item["question"],
          "db_id": item.get("db_id")}
         for item in split["train"] + split["valid"]],
        [{"question_id": item.get("question_id"), "question": item["question"],
          "db_id": item.get("db_id")}
         for item in ambrosia["train"] + ambrosia["valid"]],
        DATA_DIR,
    )

    feature_block = load_feature_block(args.feature_prompt) if args.feature_prompt else None
    if feature_block is not None:
        print(f"[feature-prompt] {os.path.basename(args.feature_prompt)} "
              f"({len(feature_block)} chars) inserted verbatim into every SQL prompt")

    teacher_report = stage_teacher(args, bird, split["train"])

    bird_train_rows = serialise_bird(split["train"], schemas, tokenizer, feature_block)
    bird_valid_rows = serialise_bird(split["valid"], schemas, tokenizer, feature_block)
    amb_train_rows = serialise_ambrosia(ambrosia["train"], tokenizer)
    amb_valid_rows = serialise_ambrosia(ambrosia["valid"], tokenizer)

    combined = bird_train_rows + amb_train_rows
    rng.shuffle(combined)  # so the two tasks alternate inside every batch

    files = {
        "sft_bird_train.jsonl": bird_train_rows,
        "sft_bird_valid.jsonl": bird_valid_rows,
        "sft_ambrosia_train.jsonl": amb_train_rows,
        "sft_ambrosia_valid.jsonl": amb_valid_rows,
        "sft_all_train.jsonl": combined,
    }
    file_stats = {}
    for name, rows in files.items():
        path = dump_jsonl(os.path.join(args.out, name), rows)
        file_stats[name] = {"rows": len(rows), "sha256_16": sha256_file(path)[:16]}
        print(f"[write] {name:<26} rows={len(rows):<5} sha256_16={file_stats[name]['sha256_16']}")
    write_json(os.path.join(args.out, "dropped.json"), bird["dropped"] + bird["mutated"])
    print(f"[write] dropped.json              rows={len(bird['dropped']) + len(bird['mutated'])} "
          f"({len(bird['dropped'])} unparseable + {len(bird['mutated'])} mutated)")

    lengths = []
    for row in combined:
        text = row["prompt"] + row["completion"]
        lengths.append(len(tokenizer(text, add_special_tokens=False)["input_ids"])
                       if tokenizer is not None else est_tokens(text))

    stats = {
        "generated_by": "training/build_data.py",
        "self_test": bool(args.self_test),
        "sqlglot": version,
        "sqlglot_pin": SQLGLOT_PIN,
        "seed": args.seed,
        "valid_frac": args.valid_frac,
        "ambrosia_neg_ratio": args.ambrosia_neg_ratio,
        "ambrosia_valid_frac": args.ambrosia_valid_frac,
        "schema_basis": schema_basis,
        "token_basis": token_basis,
        "n_raw_bird": bird["n_raw"],
        "bird_backtick": bird["n_backtick"],
        "bird_sqlite_fns": bird["n_sqlite_fn"],
        "bird_dropped": len(bird["dropped"]),
        "bird_mutated": len(bird["mutated"]),
        "bird_mutated_ids": [str(row["question_id"]) for row in bird["mutated"]],
        "bird_parseable": len(bird["kept"]) + len(bird["mutated"]),
        "bird_usable": len(bird["deduped"]),
        "dup_questions": bird["n_dup_question"],
        "dup_sql": bird["n_dup_sql"],
        "bird_train": len(bird_train_rows),
        "bird_valid": len(bird_valid_rows),
        "split": split["table"],
        "ambrosia_total": ambrosia["total"],
        "ambrosia_pos": ambrosia["pos"],
        "ambrosia_neg_natural": ambrosia["neg_natural"],
        "ambrosia_neg_kept": ambrosia["neg_kept"],
        "ambrosia_built": len(ambrosia["built"]),
        "ambrosia_skipped": ambrosia["skipped"],
        "ambrosia_train": len(amb_train_rows),
        "ambrosia_valid": len(amb_valid_rows),
        "ambrosia_ambig_parse_basis": ambrosia["basis"],
        "ambrosia_unknown_schema_rows": len(ambrosia["unknown_schema_ids"]),
        "combined_train": len(combined),
        "seq_p50": pct(lengths, 0.50),
        "seq_p95": pct(lengths, 0.95),
        "seq_p99": pct(lengths, 0.99),
        "seq_max": max(lengths) if lengths else None,
        "files": file_stats,
        "leakage": leakage,
        "feature_prompt": (None if feature_block is None else {
            "file": os.path.basename(args.feature_prompt),
            "chars": len(feature_block),
            "sha256_16": hashlib.sha256(feature_block.encode("utf-8")).hexdigest()[:16],
            "applied_to": "sql" if feature_block is not None else None,
        }),
        "teacher": teacher_report,
    }
    write_json(os.path.join(args.out, "stats.json"), stats)
    if teacher_report is not None:
        write_json(os.path.join(args.out, "teacher_report.json"), teacher_report)

    print()
    print(count_table(stats))
    print()

    if args.self_test:
        check_baseline(stats)
        print("[self-test] baseline reproduced: 14 unparseable + 1 mutated (question_id 2) -> 342 "
              "usable; the reconciled damage classes agree with "
              "research/advtext2sql/reconcile_damage.py and the counts in "
              "research/advtext2sql/captain-verified-numbers.md (which predates the mutation class, "
              "so its usable=343 is the superseded number).")
    if schema_basis != "file":
        print(f"WARNING: schema_basis={schema_basis} -> this dataset is a SMOKE dataset. Passing it "
              f"to train_qlora.py is refused unless --allow-smoke-data is given.", file=sys.stderr)
    print(f"[done] {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return build(args)
    except BuildError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
