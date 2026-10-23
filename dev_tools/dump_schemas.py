"""Build ``db_schemas.json`` — the schema dump the training pipeline requires.

The prompt a model is trained on must contain **exactly** the schema text the
tool prints at inference, otherwise training and serving drift silently.  The
canonical renderer is ``Text2SQLGenerator._get_db_schema_light`` in
``src/adv_text2sql/mcp_servers/text2sql_tool/src/text2sql_implementation.py``::

    TABLE <table>
      - <column> (<type>)
    <blank line>
    TABLE <next table>
    ...

This module re-implements that renderer step by step (same inspector calls, same
``schema="public"`` fallback, same joining and stripping) and ships a
byte-comparison against the real class (:func:`tool_light_schema`), so a change
on either side fails a test instead of quietly degrading every prompt.

Two sources
-----------
``postgres`` (default)
    The course benchmark PostgreSQL through the SSH tunnel.  Credentials come
    from ``repo/.env`` (``DB_CLIENT_HOST``, ``DB_PORT``, ``DB_USER``,
    ``DB_PASS``; ``DB_HOST``/``BENCHMARK_DB_URL`` are accepted as fallbacks).
    The password contains ``/``, so it **must** be percent-encoded before it can
    be put in a URL — :func:`postgres_dsn` does that.

``sqlite`` (``--sqlite-dir``)
    The official BIRD SQLite copies (``data_cache/dev_databases/<db>/<db>.sqlite``),
    for when the tunnel is down.  Same renderer, but the reflected types are the
    SQLite declared types the tool would print against SQLite — not the
    PostgreSQL ones the course harness prints.  Use it to keep working offline,
    and regenerate from PostgreSQL before a training run whose adapter will be
    served by the course harness.

Usage
-----
    # live database (default): every db_id referenced by data/*.json
    python -m dev_tools.dump_schemas --out ../data_cache/db_schemas.json

    # only the 8 BIRD dev databases
    python -m dev_tools.dump_schemas --bird-only --out ../data_cache/db_schemas.json

    # offline fallback from the SQLite copies
    python -m dev_tools.dump_schemas --sqlite-dir ../data_cache/dev_databases \
        --out ../data_cache/db_schemas_sqlite.json

    # prove the renderer is byte-identical to the tool's, for one database
    python -m dev_tools.dump_schemas --verify-against-tool california_schools
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

from sqlalchemy import create_engine, inspect

try:  # package import (``python -m dev_tools.dump_schemas``)
    from . import paths
except ImportError:  # pragma: no cover - direct script execution
    import paths  # type: ignore[no-redef]

__all__ = [
    "ENV_KEYS",
    "build_schemas",
    "db_ids_from_data_dir",
    "load_credentials",
    "postgres_dsn",
    "read_metadata",
    "render_light_schema",
    "sqlite_dsn",
    "tool_light_schema",
]

ENV_KEYS: Tuple[str, ...] = (
    "DB_CLIENT_HOST",
    "DB_HOST",
    "DB_PORT",
    "DB_USER",
    "DB_PASS",
    "BENCHMARK_DB_URL",
)

TableColumns = Tuple[str, List[Tuple[str, str]]]


# ---------------------------------------------------------------------------
# Credentials and DSNs
# ---------------------------------------------------------------------------


def load_credentials(env_path: Any = None) -> Dict[str, str]:
    """Read the benchmark credentials from ``repo/.env``.

    ``.env`` stores values quoted (``DB_PORT="5444"``), so quotes are stripped.
    A missing required key raises ``RuntimeError`` naming the key — never a
    partial connection attempt with an empty password.
    """

    path = Path(env_path) if env_path else paths.DEFAULT_ENV
    if not path.is_file():
        raise RuntimeError(f"credentials file not found: {path} (pass --env)")
    values: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")

    host = values.get("DB_CLIENT_HOST") or values.get("DB_HOST")
    port = values.get("DB_PORT")
    user = values.get("DB_USER")
    password = values.get("DB_PASS")
    missing = [
        name
        for name, value in (
            ("DB_CLIENT_HOST/DB_HOST", host),
            ("DB_PORT", port),
            ("DB_USER", user),
            ("DB_PASS", password),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"missing credential key(s) in {path}: {', '.join(missing)}")
    return {"host": str(host), "port": str(port), "user": str(user), "password": str(password)}


def postgres_dsn(db_id: str, *, host: str, port: Any, user: str, password: str) -> str:
    """SQLAlchemy DSN with percent-encoded credentials.

    The benchmark password contains ``/`` (and may contain ``@``/``:``/``#``), all
    of which are illegal in the userinfo part of a URL.  ``quote(..., safe="")``
    encodes every reserved character; SQLAlchemy decodes them again when it
    parses the DSN, so the driver receives the literal password.
    """

    return (
        f"postgresql+psycopg://{quote(str(user), safe='')}:{quote(str(password), safe='')}"
        f"@{host}:{port}/{quote(str(db_id), safe='')}"
    )


def sqlite_dsn(path: Any) -> str:
    """SQLAlchemy DSN for a SQLite file (delegates to :mod:`dev_tools.paths`)."""

    return paths.sqlite_dsn(Path(path))


# ---------------------------------------------------------------------------
# The renderer — byte-identical to Text2SQLGenerator._get_db_schema_light
# ---------------------------------------------------------------------------


def read_metadata(engine: Any) -> List[TableColumns]:
    """Reflect ``[(table, [(column, type), ...]), ...]`` like the tool does.

    Mirrors ``_get_db_schema_light`` + ``_columns`` exactly: try the ``public``
    schema first (PostgreSQL), fall back to the default schema (SQLite), and fall
    back to the default when reading a single table's columns fails.
    """

    inspector = inspect(engine)
    try:
        tables = inspector.get_table_names(schema="public")
    except Exception:
        tables = []
    if not tables:
        tables = inspector.get_table_names()

    metadata: List[TableColumns] = []
    for table in tables:
        try:
            columns = inspector.get_columns(table, schema="public")
        except Exception:
            columns = inspector.get_columns(table)
        metadata.append((table, [(str(column["name"]), str(column["type"])) for column in columns]))
    return metadata


def render_light_schema(metadata: Sequence[TableColumns]) -> str:
    """Render the light schema exactly as the tool does.

    ``TABLE <name>`` then ``  - <column> (<type>)`` per column, one blank line
    after each table, joined with ``\\n`` and stripped at both ends.
    """

    parts: List[str] = []
    for table, columns in metadata:
        parts.append(f"TABLE {table}")
        for name, type_text in columns:
            parts.append(f"  - {name} ({type_text})")
        parts.append("")
    return "\n".join(parts).strip()


def _schema_fingerprint(schema: str) -> str:
    import hashlib

    return hashlib.sha256(schema.encode("utf-8")).hexdigest()


def parse_light_schema(schema: str) -> Dict[str, List[Tuple[str, str]]]:
    """Parse a light schema back into ``{table: [(column, type), ...]}``.

    The parser is strict about the two line shapes the tool emits, so any dump
    produced elsewhere (a teammate's artifact, a stale file) can be validated
    before it is fed to the training pipeline.
    """

    tables: Dict[str, List[Tuple[str, str]]] = {}
    current: Optional[str] = None
    for raw_line in schema.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        if line.startswith("TABLE "):
            current = line[len("TABLE ") :].strip()
            if not current:
                raise ValueError("empty TABLE name")
            tables.setdefault(current, [])
            continue
        stripped = line.lstrip()
        if stripped.startswith("- ") and current is not None:
            body = stripped[2:]
            if not body.endswith(")") or " (" not in body:
                raise ValueError(f"column line is not '  - <name> (<type>)': {raw_line!r}")
            name, type_text = body.rsplit(" (", 1)
            tables[current].append((name, type_text[:-1]))
            continue
        raise ValueError(f"unexpected line in schema: {raw_line!r}")
    return tables


def validate_schema_dump(schemas: Any) -> List[str]:
    """Return a list of problems with a ``{db_id: schema}`` document (empty = valid).

    Checks: it is an object of strings; every string parses with
    :func:`parse_light_schema`; no ``TABLE unknown`` stub escaped into the dump;
    at least one table and one column per database; no duplicate table names.
    """

    problems: List[str] = []
    if not isinstance(schemas, dict):
        return [f"dump is {type(schemas).__name__}, expected an object {{db_id: schema}}"]
    for db_id, schema in schemas.items():
        if not isinstance(schema, str):
            problems.append(f"{db_id}: schema is {type(schema).__name__}, expected str")
            continue
        if "TABLE unknown" in schema or "STUB" in schema:
            problems.append(f"{db_id}: contains a stub schema")
            continue
        try:
            tables = parse_light_schema(schema)
        except ValueError as exc:
            problems.append(f"{db_id}: {exc}")
            continue
        if not tables:
            problems.append(f"{db_id}: no tables")
            continue
        for table, columns in tables.items():
            if not columns:
                problems.append(f"{db_id}.{table}: no columns")
        duplicates = schema.count("\nTABLE ") + (1 if schema.startswith("TABLE ") else 0) - len(tables)
        if duplicates > 0:
            problems.append(f"{db_id}: {duplicates} duplicate TABLE header(s)")
    return problems


def compare_schema_dumps(
    ours: Dict[str, str],
    theirs: Dict[str, str],
    *,
    ours_label: str = "ours",
    theirs_label: str = "theirs",
) -> dict:
    """Structural drift between two schema dumps.

    Reports three classes: databases present in only one dump, tables/columns
    present in only one dump (compared case-sensitively *and* case-insensitively,
    because PostgreSQL folds unquoted identifiers to lower case while SQLite keeps
    the declared spelling), and columns whose type text differs.
    """

    only_ours = sorted(set(ours) - set(theirs))
    only_theirs = sorted(set(theirs) - set(ours))
    table_diffs: List[dict] = []
    column_diffs: List[dict] = []
    type_diffs: List[dict] = []
    casing_only = 0

    for db_id in sorted(set(ours) & set(theirs)):
        try:
            left = parse_light_schema(ours[db_id])
            right = parse_light_schema(theirs[db_id])
        except ValueError as exc:
            table_diffs.append({"db_id": db_id, "error": str(exc)})
            continue
        left_lower = {name.lower(): name for name in left}
        right_lower = {name.lower(): name for name in right}
        for table in sorted(set(left) - set(right)):
            if table.lower() in right_lower:
                casing_only += 1
                table_diffs.append(
                    {
                        "db_id": db_id,
                        "table": table,
                        ours_label: table,
                        theirs_label: right_lower[table.lower()],
                        "kind": "casing",
                    }
                )
            else:
                table_diffs.append({"db_id": db_id, "table": table, "kind": f"only-{ours_label}"})
        for table in sorted(set(right) - set(left)):
            if table.lower() not in left_lower:
                table_diffs.append({"db_id": db_id, "table": table, "kind": f"only-{theirs_label}"})
        for table in sorted(set(left) & set(right)):
            left_columns = dict(left[table])
            right_columns = dict(right[table])
            left_lower_cols = {name.lower(): name for name in left_columns}
            right_lower_cols = {name.lower(): name for name in right_columns}
            for column in sorted(set(left_columns) - set(right_columns)):
                if column.lower() in right_lower_cols:
                    casing_only += 1
                    column_diffs.append(
                        {
                            "db_id": db_id,
                            "table": table,
                            "column": column,
                            ours_label: column,
                            theirs_label: right_lower_cols[column.lower()],
                            "kind": "casing",
                        }
                    )
                else:
                    column_diffs.append(
                        {"db_id": db_id, "table": table, "column": column, "kind": f"only-{ours_label}"}
                    )
            for column in sorted(set(right_columns) - set(left_columns)):
                if column.lower() not in left_lower_cols:
                    column_diffs.append(
                        {"db_id": db_id, "table": table, "column": column, "kind": f"only-{theirs_label}"}
                    )
            for column in sorted(set(left_columns) & set(right_columns)):
                if left_columns[column] != right_columns[column]:
                    type_diffs.append(
                        {
                            "db_id": db_id,
                            "table": table,
                            "column": column,
                            ours_label: left_columns[column],
                            theirs_label: right_columns[column],
                        }
                    )

    return {
        "ours": ours_label,
        "theirs": theirs_label,
        "n_databases_ours": len(ours),
        "n_databases_theirs": len(theirs),
        "only_ours": only_ours,
        "only_theirs": only_theirs,
        "n_casing_differences": casing_only,
        "table_diffs": table_diffs,
        "column_diffs": column_diffs,
        "type_diffs": type_diffs,
        "identical": not (only_ours or only_theirs or table_diffs or column_diffs or type_diffs),
    }


# ---------------------------------------------------------------------------
# The tool's own renderer (verification only)
# ---------------------------------------------------------------------------


def _ensure_tool_importable() -> None:
    """Make ``adv_text2sql.mcp_servers...`` importable offline.

    ``mcp_servers/__init__.py`` imports ``fastmcp`` (an MCP-server runtime
    dependency, not a benchmark one).  When it is not installed we insert a
    minimal stub so the *production* class can be imported without installing
    the world — the same technique ``tests/test_tool.py`` uses.
    """

    for entry in (str(paths.REPO_ROOT), str(paths.REPO_ROOT / "src")):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    try:
        import fastmcp  # noqa: F401
    except ImportError:
        stub = types.ModuleType("fastmcp")
        stub.FastMCP = type("FastMCP", (), {})  # type: ignore[attr-defined]
        sys.modules["fastmcp"] = stub


def tool_light_schema(dsn: str) -> str:
    """The schema string the shipped tool builds for ``dsn`` (verification path)."""

    _ensure_tool_importable()
    from adv_text2sql.mcp_servers.text2sql_tool.src.text2sql_implementation import (  # noqa: PLC0415
        Text2SQLGenerator,
    )

    tool = Text2SQLGenerator(db_uri=dsn, llm_client=None)
    tool.build()
    return tool.db_schema


def verify_against_tool(db_id: str, dsn: str) -> Tuple[bool, str, str]:
    """Compare our renderer with the tool's for one database.

    Returns ``(equal, ours, theirs)``; the caller decides how loud to be.
    """

    metadata = read_metadata(create_engine(dsn))
    ours = render_light_schema(metadata)
    theirs = tool_light_schema(dsn)
    return ours == theirs, ours, theirs


# ---------------------------------------------------------------------------
# Building the schema dump
# ---------------------------------------------------------------------------


def db_ids_from_data_dir(data_dir: Any = None) -> List[str]:
    """Every ``db_id`` referenced by the question files in ``data/``."""

    directory = Path(data_dir) if data_dir else paths.DATA_DIR
    db_ids: set[str] = set()
    for path in sorted(directory.glob("*.json")):
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("db_id"):
                    db_ids.add(str(row["db_id"]))
    return sorted(db_ids)


def _build_from_dsn_map(
    db_ids: Sequence[str],
    dsn_for: Callable[[str], str],
    *,
    source: str,
) -> Tuple[Dict[str, str], Dict[str, dict], List[dict]]:
    schemas: Dict[str, str] = {}
    manifest: Dict[str, dict] = {}
    missing: List[dict] = []
    for db_id in db_ids:
        try:
            engine = create_engine(dsn_for(db_id))
            with engine.connect():  # fail fast (a DSN typo must not look like an empty schema)
                pass
        except Exception as exc:
            missing.append({"db_id": db_id, "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
            continue
        try:
            metadata = read_metadata(engine)
        except Exception as exc:
            missing.append({"db_id": db_id, "error": f"inspect failed: {type(exc).__name__}: {str(exc)[:300]}"})
            continue
        finally:
            try:
                engine.dispose()
            except Exception:
                pass
        if not metadata:
            missing.append({"db_id": db_id, "error": "no tables found"})
            continue
        schema = render_light_schema(metadata)
        schemas[db_id] = schema
        manifest[db_id] = {
            "source": source,
            "tables": len(metadata),
            "columns": sum(len(columns) for _, columns in metadata),
            "chars": len(schema),
            "sha256": _schema_fingerprint(schema),
        }
    return schemas, manifest, missing


def build_schemas(
    db_ids: Sequence[str],
    *,
    sqlite_dir: Any = None,
    env_path: Any = None,
    host: Optional[str] = None,
    port: Optional[Any] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
) -> Tuple[Dict[str, str], Dict[str, dict], List[dict], str]:
    """Build ``{db_id: schema_text}`` from PostgreSQL (default) or SQLite.

    Returns ``(schemas, manifest, missing, source)``; ``missing`` is never filled
    with a stub — a db_id that cannot be reflected is reported, not invented.
    """

    if sqlite_dir is not None:
        root = Path(sqlite_dir)

        def dsn_for(db_id: str) -> str:
            found = paths.find_sqlite(db_id, root)
            if found is None:
                raise FileNotFoundError(f"no SQLite file for {db_id} under {root}")
            return paths.sqlite_dsn(found)

        schemas, manifest, missing = _build_from_dsn_map(db_ids, dsn_for, source="sqlite")
        return schemas, manifest, missing, "sqlite"

    credentials = load_credentials(env_path) if not (host and port and user and password) else {
        "host": str(host),
        "port": str(port),
        "user": str(user),
        "password": str(password),
    }
    endpoint = f"{credentials['host']}:{credentials['port']} (user {credentials['user']})"

    def pg_dsn(db_id: str) -> str:
        return postgres_dsn(
            db_id,
            host=credentials["host"],
            port=credentials["port"],
            user=credentials["user"],
            password=credentials["password"],
        )

    schemas, manifest, missing = _build_from_dsn_map(db_ids, pg_dsn, source="postgres")
    return schemas, manifest, missing, f"postgres {endpoint}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dev_tools.dump_schemas",
        description=(
            "Build db_schemas.json ({db_id: light schema text}) for the training pipeline. "
            "The renderer is byte-identical to the tool's _get_db_schema_light."
        ),
    )
    parser.add_argument(
        "--out",
        default=str(paths.DEFAULT_SCHEMAS),
        help=f"where to write the schema dump (default: {paths.DEFAULT_SCHEMAS})",
    )
    parser.add_argument(
        "--manifest-out",
        default=None,
        help="where to write the provenance manifest (default: <out>.manifest.json)",
    )
    parser.add_argument(
        "--db-id",
        action="append",
        default=None,
        help="restrict to this db_id (repeatable); default: every db_id in --data-dir",
    )
    parser.add_argument(
        "--data-dir",
        default=str(paths.DATA_DIR),
        help=f"question files whose db_ids are needed (default: {paths.DATA_DIR})",
    )
    parser.add_argument(
        "--bird-only",
        action="store_true",
        help="restrict to the 8 BIRD dev databases (ignores Ambrosia db_ids)",
    )
    parser.add_argument(
        "--sqlite-dir",
        default=None,
        help="offline fallback: read the official SQLite copies from this directory "
        f"(default layout: {paths.SQLITE_DIR})",
    )
    parser.add_argument(
        "--env",
        default=str(paths.DEFAULT_ENV),
        help=f"credentials file for the PostgreSQL path (default: {paths.DEFAULT_ENV})",
    )
    parser.add_argument("--host", default=None, help="override DB_CLIENT_HOST")
    parser.add_argument("--port", default=None, help="override DB_PORT")
    parser.add_argument("--user", default=None, help="override DB_USER")
    parser.add_argument(
        "--password",
        default=None,
        help="override DB_PASS (prefer the .env file: a password on the command line is "
        "visible in the shell history)",
    )
    parser.add_argument(
        "--verify-against-tool",
        metavar="DB_ID",
        default=None,
        help="build that database's schema twice (ours and the tool's) and fail on any "
        "byte difference; also proves the tool import path works",
    )
    parser.add_argument(
        "--compare-with",
        default=None,
        metavar="PATH",
        help="compare the dump we just built with another dump (e.g. the live-PostgreSQL one) "
        "and write <out>.drift.json; prints a summary",
    )
    parser.add_argument(
        "--validate",
        default=None,
        metavar="PATH",
        help="only validate an existing db_schemas.json and exit (no database access)",
    )
    parser.add_argument(
        "--skip-missing",
        action="store_true",
        help="write the schemas that worked even if some db_ids are missing "
        "(default: exit non-zero, because a missing schema silently degrades a prompt)",
    )
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.validate:
        target = Path(args.validate)
        problems = validate_schema_dump(json.loads(target.read_text(encoding="utf-8")))
        if problems:
            print(f"INVALID schema dump: {target}", file=sys.stderr)
            for problem in problems:
                print(f"  {problem}", file=sys.stderr)
            return 4
        if not args.quiet:
            payload = json.loads(target.read_text(encoding="utf-8"))
            print(f"valid: {target} ({len(payload)} database(s), light format)")
        return 0

    if args.db_id:
        db_ids = list(args.db_id)
    else:
        db_ids = db_ids_from_data_dir(args.data_dir)
        if args.bird_only:
            db_ids = [db_id for db_id in db_ids if db_id in paths.BIRD_DATABASES]
    if not db_ids:
        parser.error(f"no db_ids: --data-dir {args.data_dir} references none")

    overrides = (args.host, args.port, args.user, args.password)
    schemas, manifest, missing, source = build_schemas(
        db_ids,
        sqlite_dir=args.sqlite_dir,
        env_path=args.env,
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
    )

    if args.verify_against_tool:
        db_id = args.verify_against_tool
        if args.sqlite_dir:
            found = paths.find_sqlite(db_id, Path(args.sqlite_dir))
            if found is None:
                parser.error(f"--verify-against-tool: no SQLite file for {db_id}")
            dsn = paths.sqlite_dsn(found)
        else:
            credentials = load_credentials(args.env) if not all(overrides) else {
                "host": args.host, "port": args.port, "user": args.user, "password": args.password
            }
            dsn = postgres_dsn(
                db_id,
                host=str(credentials["host"]),
                port=credentials["port"],
                user=str(credentials["user"]),
                password=str(credentials["password"]),
            )
        equal, ours, theirs = verify_against_tool(db_id, dsn)
        if not equal:
            print(f"SCHEMA MISMATCH for {db_id}", file=sys.stderr)
            print(f"--- ours ({len(ours)} chars) ---\n{ours[:2000]}", file=sys.stderr)
            print(f"--- tool ({len(theirs)} chars) ---\n{theirs[:2000]}", file=sys.stderr)
            return 3
        if not args.quiet:
            print(f"renderer parity OK: {db_id} ({len(ours)} chars, {ours.count(chr(10)) + 1} lines)")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {db_id: schemas[db_id] for db_id in sorted(schemas)}

    problems = validate_schema_dump(payload)
    if problems:
        print(f"ERROR: the dump we just built is malformed ({len(problems)} problem(s)):", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 4

    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    manifest_path = Path(args.manifest_out) if args.manifest_out else out_path.with_suffix(".manifest.json")
    manifest_payload = {
        "tool": "dev_tools.dump_schemas",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "renderer": "Text2SQLGenerator._get_db_schema_light (byte-identical, verified by tests)",
        "sqlite_dir": args.sqlite_dir,
        "n_databases": len(payload),
        "missing": missing,
        "databases": {db_id: manifest[db_id] for db_id in sorted(manifest)},
    }
    manifest_path.write_text(
        json.dumps(manifest_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    if not args.quiet:
        print(f"source: {source}")
        print(f"wrote {len(payload)} schema(s) to {out_path}")
        for db_id in sorted(payload):
            info = manifest[db_id]
            print(f"  {db_id:26} tables={info['tables']:>3} columns={info['columns']:>4} chars={info['chars']:>6}")
        if missing:
            print(f"missing {len(missing)} db_id(s):")
            for item in missing:
                print(f"  {item['db_id']}: {item['error']}")
    if missing and not args.skip_missing:
        print(
            f"ERROR: {len(missing)} db_id(s) could not be reflected; refusing to leave a partial "
            f"schema dump without --skip-missing",
            file=sys.stderr,
        )
        return 2
    if args.compare_with:
        other_path = Path(args.compare_with)
        theirs = json.loads(other_path.read_text(encoding="utf-8"))
        drift = compare_schema_dumps(payload, theirs, ours_label=source, theirs_label=str(other_path))
        drift_path = out_path.with_name(out_path.stem + ".drift.json")
        drift_path.write_text(
            json.dumps(drift, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        if not args.quiet:
            print(
                f"drift vs {other_path}: {len(drift['only_ours'])} only-ours, "
                f"{len(drift['only_theirs'])} only-theirs, {drift['n_casing_differences']} casing, "
                f"{len(drift['table_diffs'])} table, {len(drift['column_diffs'])} column, "
                f"{len(drift['type_diffs'])} type difference(s) -> {drift_path}"
            )
            for diff in drift["type_diffs"][:10]:
                print(f"  type {diff['db_id']}.{diff['table']}.{diff['column']}: "
                      f"{diff[source]} vs {diff[str(other_path)]}")
        if drift["only_theirs"] or drift["only_ours"]:
            print(
                f"WARNING: the two dumps cover different databases "
                f"(only ours: {drift['only_ours'][:5]}, only theirs: {drift['only_theirs'][:5]})",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
