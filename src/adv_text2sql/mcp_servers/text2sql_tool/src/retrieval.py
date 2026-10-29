"""Optional retrieval few-shot bank for the Text2SQL tool - a procedure-only arm.

Why this module exists
----------------------
The round-2 matrix asks a narrow question: does work *outside* the adapter move
BIRD more than the adapter did? This module is the retrieval arm of that
question. When enabled (it is OFF by default) the tool embeds the incoming
question, retrieves the ``k`` most similar **training** questions of the same
database, and injects their gold SQL into the SQL prompt as worked examples.

Honesty constraints, each enforced here and pinned by ``tests/test_procedures.py``
-----------------------------------------------------------------------------------
1. **Bank provenance.** A bank may be built only from ``data/train_queries.json``
   (the 357 BIRD train rows that ship with the repo) or from the official BIRD
   train split (``data_cache/bird_train/*.jsonl``). :func:`is_allowed_bank_source`
   refuses anything else by name, so ``bird_small.json``, ``bird_large.json`` and
   the official ``mini_dev`` dev files cannot become a bank even if a caller
   passes the path. That is the *first* layer.
2. **Leakage guard.** The second layer is content-based: every candidate row is
   dropped when its ``question_id`` is a dev id or when its exact question string
   (whitespace-normalised, case-folded) is a dev question. Renaming a dev file to
   ``train_queries.json`` therefore yields an empty bank instead of a leak; the
   drop counters are reported in the bank metadata.
3. **Fully offline.** The default embedder is a local sentence-transformer loaded
   with ``local_files_only=True``; the alternative is a dependency-free hashing
   embedder. No API key is read and no network request is made. When the weights
   are absent the retriever becomes *unavailable* with a reason and the tool sends
   its normal prompt - retrieval never raises into ``query``.
4. **Visible in the artefact.** The rendered block is returned to the caller and
   written to the procedure log, so a run's record shows the exact text the model
   saw (``TEXT2SQL_PROCEDURE_LOG``).

Retrieval is lexical-or-semantic, never a lookup of the answer: the bank is
train-only and the retrieved SQL belongs to a *different* question.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence

import sqlglot
from sqlglot import exp

logger = logging.getLogger("text2sql_tool.retrieval")

__all__ = [
    "BankRow",
    "DEFAULT_BANK",
    "DEFAULT_DEV_GUARD",
    "DEFAULT_K",
    "DEFAULT_MODEL",
    "DevGuard",
    "EMBEDDER_NAMES",
    "Embedder",
    "EmbedderUnavailable",
    "Example",
    "FewShotBank",
    "HashEmbedder",
    "HARD_CAP_K",
    "Retriever",
    "SentenceTransformerEmbedder",
    "build_bank",
    "build_retriever",
    "filter_bank_rows",
    "is_allowed_bank_source",
    "load_bank_rows",
    "load_dev_guard",
    "make_embedder",
    "normalise_question",
    "render_examples_block",
    "split_question_evidence",
    "transpile_gold_to_postgres",
]

REPO_ROOT: Path = Path(__file__).resolve().parents[5]
"""``research/advtext2sql/repo`` - five levels above ``src/.../src/retrieval.py``."""

DEFAULT_BANK: Path = REPO_ROOT / "data" / "train_queries.json"
"""The 357 BIRD *train* rows that ship with the repo. Never a dev file."""

DEFAULT_DEV_GUARD: tuple[Path, ...] = (
    REPO_ROOT / "data" / "bird_large.json",
    REPO_ROOT / "data" / "bird_small.json",
)
"""Dev question ids and strings the bank must never contain. Read, never written."""

DEFAULT_K: int = 3
"""Three worked examples cost roughly 600-900 prompt tokens; more is rarely better."""

HARD_CAP_K: int = 10
"""A ceiling even when configured higher: each example is prompt tokens *per call*."""

DEFAULT_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"
"""Small local encoder (384 dims, ~90 MB). Loaded from local weights only."""

EMBEDDER_NAMES: tuple[str, ...] = ("sentence-transformer", "hash")
"""``hash`` is the dependency-free lexical fallback, see :class:`HashEmbedder`."""


class EmbedderUnavailable(RuntimeError):
    """The requested embedding backend cannot be used in this environment."""


# --------------------------------------------------------------------------- text


_WHITESPACE_RE = re.compile(r"\s+")
_EVIDENCE_SUFFIX_RE = re.compile(
    r"^\s*question:\s*(?P<question>.*?),\s*evidence\s*\(may be empty\)\s*:\s*(?P<evidence>.*)$",
    re.DOTALL | re.IGNORECASE,
)
"""``benchmarks/bird.py`` builds exactly this string when ``use_evidence`` is on."""


def normalise_question(text: Any) -> str:
    """Whitespace-collapsed, case-folded question key used for the leakage guard."""

    return _WHITESPACE_RE.sub(" ", str(text or "")).strip().casefold()


def split_question_evidence(text: str) -> tuple[str, str]:
    """Split the harness's ``question: ..., evidence (may be empty): ...`` form.

    The harness hands the tool one string; when evidence is enabled that string
    carries both parts. Retrieval needs them separately only to decide whether
    evidence participates in the similarity query. Anything that does not match
    the harness format is treated as a plain question with no evidence, so a
    normal question can never be mangled.
    """

    match = _EVIDENCE_SUFFIX_RE.match(str(text or ""))
    if match is None:
        return str(text or ""), ""
    return match.group("question").strip(), match.group("evidence").strip()


# ------------------------------------------------------------------------ sources


def is_allowed_bank_source(path: str | Path) -> tuple[bool, str]:
    """Gate a bank source path against the train-only allowlist.

    Two accepted shapes, nothing else:

    * a file named ``train_queries.json`` (the repo's 357-row train file);
    * a JSONL/JSON file of the official BIRD train split - its name starts with
      ``bird23_train`` or it lives in a ``bird_train`` directory.

    Dev files are refused *by name* here, which is the first of the two leakage
    layers; :func:`filter_bank_rows` is the content-based second layer.
    """

    candidate = Path(path)
    name = candidate.name.lower()
    if name in {"bird_small.json", "bird_large.json"} or name.startswith("mini_dev"):
        return False, (
            f"{candidate.name} is a BIRD *dev* file; the few-shot bank is train-only "
            "(data/train_queries.json or the BIRD train split)"
        )
    if name == "train_queries.json":
        return True, "train_queries"
    parents = {part.lower() for part in candidate.parts[:-1]}
    if name.startswith("bird23_train") and candidate.suffix.lower() in {".jsonl", ".json"}:
        return True, "bird_train_split"
    if "bird_train" in parents and candidate.suffix.lower() in {".jsonl", ".json"}:
        return True, "bird_train_split"
    return False, (
        f"refusing {candidate} as a few-shot bank source: allowed sources are "
        "data/train_queries.json and the official BIRD train split"
    )


@dataclass(frozen=True)
class BankRow:
    """One training question with its gold SQL, already transpiled to PostgreSQL."""

    source_question_id: int | None
    db_id: str
    question: str
    evidence: str
    sql: str
    difficulty: str = ""


@dataclass(frozen=True)
class DevGuard:
    """Dev questions the bank must never contain (ids *and* exact strings)."""

    question_ids: frozenset[int]
    questions: frozenset[str]
    db_question_pairs: frozenset[tuple[str, str]]
    sources: tuple[str, ...] = ()

    def leak_reason(self, row: BankRow) -> str:
        """Return the guard that fires for ``row``, or ``""`` when it is clean.

        Both checks are independent on purpose: an id collision without a text
        match and a text match without an id collision are each enough to reject
        the row, which is what "no dev question can enter the bank" means.
        """

        key = normalise_question(row.question)
        if row.source_question_id is not None and row.source_question_id in self.question_ids:
            return "dev_question_id"
        if key and key in self.questions:
            return "dev_question_text"
        if key and (row.db_id, key) in self.db_question_pairs:
            return "dev_question_text"
        return ""


def _read_rows(source: Path) -> list[dict[str, Any]]:
    """Read a JSON list or a JSONL file of question/SQL records; never downloads."""

    text = source.read_text(encoding="utf-8-sig")
    if source.suffix.lower() == ".jsonl":
        rows: list[dict[str, Any]] = []
        for number, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as error:  # pragma: no cover - corrupt input
                raise ValueError(f"{source}:{number} is not valid JSON: {error}") from error
            if isinstance(item, dict):
                rows.append(item)
        return rows
    payload = json.loads(text)
    if not isinstance(payload, list):
        raise ValueError(f"{source} must contain a JSON array of question records")
    return [item for item in payload if isinstance(item, dict)]


def _row_from_record(record: dict[str, Any]) -> BankRow | None:
    """Map one input record to :class:`BankRow`; ``None`` when it is not usable."""

    question = str(record.get("question") or "").strip()
    sql = str(record.get("SQL") or record.get("sql") or "").strip()
    db_id = str(record.get("db_id") or "").strip()
    evidence = str(record.get("evidence") or "").strip()
    raw_id = record.get("question_id")
    try:
        question_id = int(raw_id) if raw_id is not None else None
    except (TypeError, ValueError):
        question_id = None
    if not question or not db_id:
        return None
    return BankRow(
        source_question_id=question_id,
        db_id=db_id,
        question=question,
        evidence=evidence,
        sql=sql,
        difficulty=str(record.get("difficulty") or ""),
    )


def transpile_gold_to_postgres(sql: str) -> str | None:
    """SQLite gold -> PostgreSQL text, or ``None`` when it cannot be represented.

    BIRD gold SQL is SQLite (backticks, ``CAST`` differences). Injecting it
    verbatim as a "worked example" would teach the model the identifier quoting
    the label harness executes as a silent zero, so examples are transpiled with
    the same ``sqlglot`` read/write pair ``training/build_data.py`` uses, then
    re-parsed as PostgreSQL and dropped when they do not survive.
    """

    text = re.sub(r"```sql|```", "", sql or "", flags=re.IGNORECASE).strip().rstrip(";").strip()
    if not text:
        return None
    try:
        converted = sqlglot.transpile(text, read="sqlite", write="postgres")
    except Exception:  # noqa: BLE001 - any parser failure means "not a usable example"
        return None
    if not converted:
        return None
    candidate = converted[0].strip().rstrip(";").strip()
    if not candidate:
        return None
    try:
        parsed = sqlglot.parse_one(candidate, read="postgres")
    except Exception:  # noqa: BLE001 - the example must parse in the target dialect
        return None
    if not isinstance(parsed, exp.Query):
        return None
    return candidate


def load_dev_guard(paths: Iterable[str | Path] | None = None) -> DevGuard:
    """Load the dev ids and exact question strings from the repo's dev files.

    A missing file is logged and skipped: in a fresh clone without ``data/`` the
    retriever degrades to unavailable rather than raising. The returned guard is
    the only thing the content layer trusts.
    """

    sources = tuple(Path(path) for path in (paths or DEFAULT_DEV_GUARD))
    ids: set[int] = set()
    questions: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    loaded: list[str] = []
    for path in sources:
        if not path.is_file():
            logger.warning("Dev guard file is missing, skipping it: %s", path)
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            logger.exception("Could not read dev guard file %s; skipping it", path)
            continue
        if not isinstance(payload, list):
            continue
        for item in payload:
            if not isinstance(item, dict):
                continue
            raw_id = item.get("question_id")
            try:
                if raw_id is not None:
                    ids.add(int(raw_id))
            except (TypeError, ValueError):
                pass
            key = normalise_question(item.get("question"))
            if key:
                questions.add(key)
                pairs.add((str(item.get("db_id") or ""), key))
        loaded.append(str(path))
    return DevGuard(frozenset(ids), frozenset(questions), frozenset(pairs), tuple(loaded))


_DROP_ORDER: tuple[str, ...] = (
    "dev_question_id",
    "dev_question_text",
    "no_gold_sql",
    "untranspilable",
    "duplicate",
)
"""Reasons a candidate row never reaches the bank, in the order they are checked."""


def filter_bank_rows(
    rows: Sequence[BankRow], guard: DevGuard
) -> tuple[list[BankRow], dict[str, int]]:
    """Apply the leakage, gold-SQL and duplicate filters; return rows and counters.

    The guard runs *before* transpilation so a leaked dev row is never even
    parsed, and duplicates are removed on ``(db_id, question)`` and
    ``(db_id, normalised SQL)`` so the same worked example cannot fill all ``k``
    slots. The counters are part of the bank metadata, which is what makes the
    guard auditable from an artefact instead of from a test run.
    """

    kept: list[BankRow] = []
    drops = dict.fromkeys(_DROP_ORDER, 0)
    seen_questions: set[tuple[str, str]] = set()
    seen_sql: set[tuple[str, str]] = set()
    for row in rows:
        reason = guard.leak_reason(row)
        if reason:
            drops[reason] += 1
            continue
        if not row.sql or row.sql.strip().casefold() == "ambiguous":
            drops["no_gold_sql"] += 1
            continue
        converted = transpile_gold_to_postgres(row.sql)
        if converted is None:
            drops["untranspilable"] += 1
            continue
        question_key = (row.db_id, normalise_question(row.question))
        sql_key = (row.db_id, normalise_question(converted))
        if question_key in seen_questions or sql_key in seen_sql:
            drops["duplicate"] += 1
            continue
        seen_questions.add(question_key)
        seen_sql.add(sql_key)
        kept.append(
            BankRow(
                source_question_id=row.source_question_id,
                db_id=row.db_id,
                question=row.question,
                evidence=row.evidence,
                sql=converted,
                difficulty=row.difficulty,
            )
        )
    return kept, drops


def load_bank_rows(
    source: str | Path | None = None,
    *,
    guard_paths: Iterable[str | Path] | None = None,
) -> tuple[list[BankRow], dict[str, Any]]:
    """Read, guard and transpile a bank source; return rows plus a provenance block.

    Raises ``ValueError`` for a refused source (dev file, unknown file): a caller
    that asked for a leak must see a loud failure, not a silently empty bank.
    """

    path = Path(source) if source is not None else DEFAULT_BANK
    allowed, kind = is_allowed_bank_source(path)
    if not allowed:
        raise ValueError(kind)
    if not path.is_file():
        raise FileNotFoundError(f"few-shot bank source does not exist: {path}")

    records = _read_rows(path)
    rows = [row for row in (_row_from_record(record) for record in records) if row is not None]
    guard = load_dev_guard(guard_paths)
    kept, drops = filter_bank_rows(rows, guard)
    payload = path.read_bytes()
    metadata: dict[str, Any] = {
        "source": str(path),
        "source_kind": kind,
        "source_sha256": hashlib.sha256(payload).hexdigest(),
        "records_read": len(records),
        "records_mapped": len(rows),
        "rows_built": len(kept),
        "drops": drops,
        "dev_guard_files": list(guard.sources),
        "dev_questions_guarded": len(guard.questions),
        "dev_question_ids_guarded": len(guard.question_ids),
    }
    return kept, metadata


# ---------------------------------------------------------------------- embedders


class Embedder(Protocol):
    """Minimal embedding surface: deterministic, offline, batched."""

    name: str
    model: str
    default_min_similarity: float

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]: ...


_WORD_RE = re.compile(r"[a-z0-9_]+")


class HashEmbedder:
    """Dependency-free lexical embedder (hashed word unigrams + bigrams).

    This is the honest offline fallback, not a semantic model: two paraphrases
    with no shared words score near zero. It exists because the local
    sentence-transformer weights may be absent (this machine has none), and a
    procedure arm must not be unmeasurable just because a download is
    impossible. Its similarity scale differs from a sentence-transformer's,
    which is why the threshold default is per-embedder.
    """

    name = "hash"
    default_min_similarity = 0.20
    """"BM25-free" lexical cosine: unrelated questions of one database land ~0.05."""

    def __init__(self, dimensions: int = 512) -> None:
        if dimensions < 8:
            raise ValueError("HashEmbedder needs at least 8 dimensions")
        self.dimensions = int(dimensions)
        self.model = f"hash-{self.dimensions}"

    @staticmethod
    def _tokens(text: str) -> list[str]:
        return _WORD_RE.findall(str(text or "").casefold())

    def _bucket(self, feature: str) -> int:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        return int.from_bytes(digest, "big") % self.dimensions

    def _vector(self, text: str) -> tuple[float, ...]:
        tokens = self._tokens(text)
        counts: dict[int, float] = {}
        for token in tokens:
            index = self._bucket(token)
            counts[index] = counts.get(index, 0.0) + 1.0
        for left, right in zip(tokens, tokens[1:]):
            index = self._bucket(f"{left} {right}")
            counts[index] = counts.get(index, 0.0) + 1.0
        values = [0.0] * self.dimensions
        for index, count in counts.items():
            values[index] = count
        norm = sum(value * value for value in values) ** 0.5
        if norm == 0.0:
            return tuple(values)
        return tuple(value / norm for value in values)

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        return [self._vector(text) for text in texts]


class SentenceTransformerEmbedder:
    """Local sentence-transformer loaded from already-downloaded weights only.

    ``local_files_only=True`` is the offline guarantee: a missing model raises
    instead of reaching the Hub, and :func:`make_embedder`'s caller turns that
    into an unavailable retriever with a recorded reason.
    """

    name = "sentence-transformer"
    default_min_similarity = 0.40
    """MiniLM cosine over same-database questions: unrelated ~0.2, similar > 0.5."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        device: str = "cpu",
        local_files_only: bool = True,
    ) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as error:  # pragma: no cover - depends on the environment
            raise EmbedderUnavailable(
                "sentence-transformers is not installed; use the 'hash' embedder "
                "or install the optional local encoder"
            ) from error
        try:
            self._model = SentenceTransformer(
                model, device=device, local_files_only=local_files_only
            )
        except Exception as error:  # noqa: BLE001 - absent weights, bad device, bad id
            raise EmbedderUnavailable(
                f"could not load local sentence-transformer weights for {model!r}: {error}"
            ) from error
        self.model = model
        self.device = device

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        vectors = self._model.encode(
            list(texts),
            batch_size=32,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [tuple(float(value) for value in vector) for vector in vectors]


def make_embedder(name: str | None, model: str | None = None) -> Embedder:
    """Construct an embedder by name; ``EmbedderUnavailable`` when unusable."""

    key = (name or "sentence-transformer").strip().lower()
    if key == "hash":
        return HashEmbedder()
    if key in {"sentence-transformer", "sentence_transformer", "st"}:
        return SentenceTransformerEmbedder(model or DEFAULT_MODEL)
    raise EmbedderUnavailable(
        f"unknown embedder {name!r}; expected one of {EMBEDDER_NAMES}"
    )


# ------------------------------------------------------------------------ bank


@dataclass(frozen=True)
class Example:
    """One retrieved training question, with the score that selected it."""

    source_question_id: int | None
    db_id: str
    question: str
    evidence: str
    sql: str
    score: float
    difficulty: str = ""

    def metadata(self) -> dict[str, Any]:
        """The fields an artefact needs; the SQL text is the point of the record."""

        return {
            "source_question_id": self.source_question_id,
            "db_id": self.db_id,
            "question": self.question,
            "score": round(float(self.score), 6),
            "sql": self.sql,
            "difficulty": self.difficulty,
        }


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    return float(sum(a * b for a, b in zip(left, right)))


@dataclass
class FewShotBank:
    """Guarded training rows, their embeddings, and a per-database lookup."""

    rows: tuple[BankRow, ...]
    vectors: tuple[tuple[float, ...], ...]
    embedder: Embedder
    metadata: dict[str, Any] = field(default_factory=dict)
    _by_db: dict[str, tuple[int, ...]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self._by_db:
            index: dict[str, list[int]] = {}
            for position, row in enumerate(self.rows):
                index.setdefault(row.db_id, []).append(position)
            self._by_db = {key: tuple(value) for key, value in index.items()}

    @property
    def databases(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_db))

    def retrieve(
        self,
        question: str,
        *,
        db_id: str,
        k: int = DEFAULT_K,
        min_similarity: float | None = None,
        include_evidence: bool = True,
        evidence: str = "",
    ) -> tuple[Example, ...]:
        """Top-``k`` most similar *training* questions of ``db_id``.

        Bank rows are embedded as ``question + evidence`` (the richer text); the
        ``include_evidence`` switch governs the *query* side and the rendered block,
        so a no-evidence arm still compares against the full training question.

        Deterministic: ties are broken by ``source_question_id`` then by question
        text, so two runs over one bank retrieve the same examples in the same
        order. A question from a database with no bank rows returns nothing - the
        arm is then a no-op, never a cross-database example.
        """

        positions = self._by_db.get(db_id, ())
        if not positions or k < 1:
            return ()
        query_text = question if not include_evidence or not evidence else f"{question} {evidence}"
        query_vector = self.embedder.embed([query_text])[0]
        threshold = (
            self.embedder.default_min_similarity if min_similarity is None else float(min_similarity)
        )
        scored: list[tuple[float, int, BankRow]] = []
        for position in positions:
            row = self.rows[position]
            score = _dot(query_vector, self.vectors[position])
            if score + 1e-12 < threshold:
                continue
            scored.append((score, position, row))
        scored.sort(key=lambda item: (-item[0], item[2].source_question_id or 0, item[2].question))
        return tuple(
            Example(
                source_question_id=row.source_question_id,
                db_id=row.db_id,
                question=row.question,
                evidence=row.evidence,
                sql=row.sql,
                score=score,
                difficulty=row.difficulty,
            )
            for score, _position, row in scored[:k]
        )


_BANK_CACHE: dict[tuple[Any, ...], FewShotBank] = {}
"""One embedded bank per (source, source bytes, embedder, model) per process.

``benchmarks/base.py`` builds one tool per ``db_id``; without this cache the
357-row bank would be re-read and re-embedded eight times per run.
"""

_BANK_FAILURES: dict[tuple[Any, ...], str] = {}
"""Negative cache: an absent model is attempted once per process, not eight times."""


def _cache_key(source: Path, embedder_name: str, model: str | None) -> tuple[Any, ...]:
    try:
        stat = source.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = (0, 0)
    return (str(source.resolve()) if source.exists() else str(source), stamp, embedder_name, model or "")


def build_bank(
    *,
    source: str | Path | None = None,
    embedder: str = "sentence-transformer",
    model: str | None = None,
    guard_paths: Iterable[str | Path] | None = None,
) -> FewShotBank:
    """Build (or return the cached) bank; raises ``EmbedderUnavailable``/``ValueError``."""

    path = Path(source) if source is not None else DEFAULT_BANK
    key = _cache_key(path, embedder, model)
    cached_failure = _BANK_FAILURES.get(key)
    if cached_failure is not None:
        raise EmbedderUnavailable(cached_failure)
    cached = _BANK_CACHE.get(key)
    if cached is not None:
        return cached

    rows, metadata = load_bank_rows(path, guard_paths=guard_paths)
    if not rows:
        reason = (
            f"no usable training rows in {path}: every record was filtered out "
            f"({metadata.get('drops')})"
        )
        _BANK_FAILURES[key] = reason
        raise EmbedderUnavailable(reason)

    try:
        embedder_instance = make_embedder(embedder, model)
        vectors = embedder_instance.embed(
            [row.question if not row.evidence else f"{row.question} {row.evidence}" for row in rows]
        )
    except EmbedderUnavailable as error:
        _BANK_FAILURES[key] = str(error)
        raise

    metadata = {
        **metadata,
        "embedder": embedder_instance.name,
        "embedding_model": embedder_instance.model,
        "dimensions": len(vectors[0]) if vectors else 0,
        "default_min_similarity": embedder_instance.default_min_similarity,
        "databases": sorted({row.db_id for row in rows}),
    }
    bank = FewShotBank(rows=tuple(rows), vectors=tuple(vectors), embedder=embedder_instance, metadata=metadata)
    _BANK_CACHE[key] = bank
    return bank


# -------------------------------------------------------------------- rendering


def render_examples_block(examples: Sequence[Example], *, include_evidence: bool = True) -> str:
    """Render retrieved examples as the prompt block; deterministic, no timestamps.

    Numbers and separators are fixed so the block (and its hash, recorded in the
    procedure log) is comparable between runs.
    """

    if not examples:
        return ""
    parts: list[str] = []
    for number, example in enumerate(examples, start=1):
        block = [f"Example {number} (similarity {example.score:.3f}, database {example.db_id})"]
        block.append(f"Question: {example.question}")
        if include_evidence and example.evidence:
            block.append(f"Evidence: {example.evidence}")
        block.append(f"SQL: {example.sql}")
        parts.append("\n".join(block))
    return "\n\n".join(parts)


# -------------------------------------------------------------------- retriever


@dataclass
class Retriever:
    """The tool-facing wrapper: a bank plus the knobs, or a recorded reason why not."""

    bank: FewShotBank | None = None
    k: int = DEFAULT_K
    min_similarity: float | None = None
    include_evidence: bool = True
    reason: str = ""
    requested_embedder: str = "sentence-transformer"

    @property
    def available(self) -> bool:
        return self.bank is not None

    def retrieve(self, question: str, *, db_id: str, evidence: str = "") -> tuple[Example, ...]:
        """Never raises: an unavailable or failing bank yields no examples."""

        if self.bank is None:
            return ()
        try:
            return self.bank.retrieve(
                question,
                db_id=db_id,
                k=self.k,
                min_similarity=self.min_similarity,
                include_evidence=self.include_evidence,
                evidence=evidence,
            )
        except Exception:  # noqa: BLE001 - retrieval must never cost a benchmark item
            logger.exception("Few-shot retrieval failed; continuing without examples")
            return ()

    def metadata(self) -> dict[str, Any]:
        """Config + bank provenance; ``available=false`` carries the reason."""

        block: dict[str, Any] = {
            "enabled": True,
            "available": self.available,
            "requested_embedder": self.requested_embedder,
            "k": self.k,
            "min_similarity": self.min_similarity,
            "include_evidence_in_query": self.include_evidence,
            "reason": self.reason,
        }
        if self.bank is not None:
            block.update(self.bank.metadata)
            block["threshold_used"] = (
                self.bank.embedder.default_min_similarity
                if self.min_similarity is None
                else float(self.min_similarity)
            )
        return block


def build_retriever(
    *,
    source: str | Path | None = None,
    embedder: str = "sentence-transformer",
    model: str | None = None,
    k: int = DEFAULT_K,
    min_similarity: float | None = None,
    include_evidence: bool = True,
    guard_paths: Iterable[str | Path] | None = None,
) -> Retriever:
    """Build a retriever, degrading to ``available=False`` instead of raising.

    Everything that can go wrong (absent weights, dev-only source, empty bank,
    unreadable file) is a *reason string* here: an optional procedure arm must
    never abort a benchmark run, and a run must still say why the arm was a no-op.
    """

    capped_k = max(1, min(int(k), HARD_CAP_K))
    try:
        bank = build_bank(
            source=source, embedder=embedder, model=model, guard_paths=guard_paths
        )
    except Exception as error:  # noqa: BLE001 - optional arm, never fatal
        reason = f"{type(error).__name__}: {error}"
        logger.warning("Few-shot retrieval is unavailable: %s", reason)
        return Retriever(
            bank=None,
            k=capped_k,
            min_similarity=min_similarity,
            include_evidence=include_evidence,
            reason=reason,
            requested_embedder=embedder,
        )
    return Retriever(
        bank=bank,
        k=capped_k,
        min_similarity=min_similarity,
        include_evidence=include_evidence,
        requested_embedder=embedder,
    )
