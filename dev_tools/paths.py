"""Filesystem locations shared by the offline tooling.

Everything here is derived from this file's own location, so the tools work from
any working directory.  The big downloaded data lives *outside* the git repo
(``research/advtext2sql/data_cache``) and is ignored by the ``.gitignore`` inside
that directory; nothing here ever writes into ``data/`` or ``benchmarks/``.

Environment overrides (useful when the cache lives on another disk):
``ADVTEXT2SQL_DATA_CACHE`` and ``ADVTEXT2SQL_SQLITE_DIR``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

REPO_ROOT: Path = Path(__file__).resolve().parents[1]
TOPIC_ROOT: Path = REPO_ROOT.parent

DATA_CACHE: Path = Path(os.environ.get("ADVTEXT2SQL_DATA_CACHE") or (TOPIC_ROOT / "data_cache"))
SQLITE_DIR: Path = Path(os.environ.get("ADVTEXT2SQL_SQLITE_DIR") or (DATA_CACHE / "dev_databases"))
ARTIFACTS_DIR: Path = DATA_CACHE / "artifacts"

DATA_DIR: Path = REPO_ROOT / "data"
DEFAULT_ENV: Path = REPO_ROOT / ".env"
DEFAULT_SCHEMAS: Path = DATA_CACHE / "db_schemas.json"

#: The 8 BIRD dev databases referenced by ``data/*.json`` (the other three in
#: the mini_dev package — formula_1, european_football_2, thrombosis_prediction —
#: are not part of this course's benchmark).
BIRD_DATABASES: tuple[str, ...] = (
    "california_schools",
    "financial",
    "toxicology",
    "card_games",
    "codebase_community",
    "superhero",
    "student_club",
    "debit_card_specializing",
)


def sqlite_candidates(db_id: str, sqlite_dir: Optional[Path] = None) -> list[Path]:
    """Where a SQLite copy of ``db_id`` may live, in mini_dev layout order."""

    root = Path(sqlite_dir) if sqlite_dir else SQLITE_DIR
    return [
        root / db_id / f"{db_id}.sqlite",
        root / f"{db_id}.sqlite",
        root / db_id / f"{db_id}.db",
    ]


def find_sqlite(db_id: str, sqlite_dir: Optional[Path] = None) -> Optional[Path]:
    """Return the existing SQLite file for ``db_id`` or ``None`` (never downloads)."""

    for candidate in sqlite_candidates(db_id, sqlite_dir):
        if candidate.is_file():
            return candidate
    return None


def sqlite_dsn(path: Path) -> str:
    """SQLAlchemy DSN for a local SQLite file (absolute, forward slashes)."""

    return "sqlite:///" + str(Path(path).resolve()).replace(os.sep, "/")


def sqlite_ro_uri(path: Path) -> str:
    """``sqlite3.connect`` URI that opens the file read-only and lock-free.

    ``immutable=1`` is safe here: the extracted databases are static artefacts of
    the benchmark, and it avoids creating ``-wal``/``-journal`` files next to them.
    """

    return "file:" + str(Path(path).resolve()).replace(os.sep, "/") + "?mode=ro&immutable=1"
