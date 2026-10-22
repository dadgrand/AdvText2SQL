"""pytest bootstrap for the offline evaluation layer.

Two jobs:

1. Put ``repo/src`` and ``repo/dev_tools`` (and the repo root, so that
   ``import dev_tools.fixed_eval`` works) on ``sys.path``.  That is what makes
   ``python -m pytest tests/test_eval.py`` work from the repo root without an
   editable install of the project.
2. Provide :class:`FakeDB` — an in-process stand-in for the benchmark
   PostgreSQL.  Every test in this directory must run without a database, a
   network connection or an LLM key; SQL is "executed" by validating it as
   PostgreSQL and deriving deterministic rows from its canonical text.  Two
   identical SQL strings therefore return identical rows (so EX can be
   asserted), and an invalid or SQLite-only query raises
   :class:`FakeDBError` (so the harness-ceiling behaviour can be reproduced).
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest
import sqlglot

REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (REPO_ROOT / "src", REPO_ROOT / "dev_tools"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DATA_DIR = REPO_ROOT / "data"


class FakeDBError(RuntimeError):
    """Raised when a query cannot be executed by :class:`FakeDB`."""


class FakeDB:
    """Deterministic in-process executor: ``execute(sql, db_id) -> list[tuple]``.

    Rules, in order:

    1. SQL containing a backtick is rejected — PostgreSQL does not support
       MySQL/SQLite identifier quoting, which is exactly the dialect penalty the
       shipped harness inflicts on untranspiled predictions.
    2. SQL that ``sqlglot`` cannot parse as PostgreSQL is rejected — this is the
       same signal as "PostgreSQL would refuse to execute it", and it is what a
       destroyed gold query looks like.
    3. Otherwise the row set is a single one-column row whose value is a hash of
       the whitespace-normalised SQL, so equal SQL gives equal rows and
       different SQL gives different rows.
    """

    def __init__(self, aliases: Optional[Dict[str, str]] = None) -> None:
        self.aliases = dict(aliases or {})
        self.calls: List[Tuple[Optional[str], str]] = []
        self.errors: List[Tuple[Optional[str], str, str]] = []

    @staticmethod
    def _canonical(sql: str) -> str:
        return " ".join(sql.split())

    def rows_for(self, sql: str) -> List[Tuple[Any, ...]]:
        canonical = self._canonical(self.aliases.get(sql, sql))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
        return [(digest,)]

    def __call__(self, sql: str, db_id: Optional[str]) -> List[Tuple[Any, ...]]:
        self.calls.append((db_id, sql))
        try:
            if "`" in sql:
                raise FakeDBError("syntax error: backtick identifier is not valid PostgreSQL")
            try:
                sqlglot.parse_one(sql, dialect="postgres")
            except Exception as exc:
                raise FakeDBError(f"{type(exc).__name__}: {exc}") from exc
        except FakeDBError as exc:
            self.errors.append((db_id, sql, str(exc)))
            raise
        return self.rows_for(sql)


def load_json(name: str) -> Any:
    with open(DATA_DIR / name, "r", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture()
def fake_db() -> FakeDB:
    return FakeDB()


@pytest.fixture(scope="session")
def bird_large() -> List[dict]:
    return load_json("bird_large.json")


@pytest.fixture(scope="session")
def bird_small() -> List[dict]:
    return load_json("bird_small.json")


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def data_dir() -> Path:
    return DATA_DIR
