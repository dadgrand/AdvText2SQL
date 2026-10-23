"""PYTHONPATH shim: stub ``fastmcp`` when the MCP runtime is not installed.

Why
---
``src/adv_text2sql/mcp_servers/__init__.py`` imports ``fastmcp`` and does nothing
else with it: the MCP transport is a *serving* dependency, not a benchmark one.
The harness (``bird_benchmark.py``, ``ambrosia_benchmark.py``) and the benchmark
entrypoint import ``adv_text2sql.mcp_servers.text2sql_tool.src.text2sql_implementation``
only for the ``Text2SQLGenerator`` class, so on a machine without ``fastmcp`` the
import fails before any benchmark code runs.

Put this directory first on ``PYTHONPATH`` and Python imports ``sitecustomize``
at startup, which installs a minimal in-memory stand-in:

    set PYTHONPATH=src;dev_tools/fastmcp_shim
    python bird_benchmark.py

The stub is only inserted when the real package is missing, so it never shadows a
working install. Nothing in ``src/`` is edited (the project is teacher-owned) and
the tool under test is the real class — only the unused MCP transport object is
faked. The same technique is used by ``tests/test_tool.py`` and
``dev_tools/dump_schemas.py``.
"""

from __future__ import annotations

import os
import sys
import types


def _install_stub() -> None:
    try:
        import fastmcp  # noqa: F401  (a real install wins)
        return
    except ImportError:
        pass

    stub = types.ModuleType("fastmcp")

    class FastMCP:  # pragma: no cover - never exercised by the benchmark path
        """Minimal stand-in: enough to construct, register nothing, never run."""

        def __init__(self, *args: object, **kwargs: object) -> None:
            self.args = args
            self.kwargs = kwargs

        def tool(self, *args: object, **kwargs: object):
            def decorator(function):
                return function

            return decorator

        def run(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("the fastmcp shim cannot serve MCP; install fastmcp for that")

    stub.FastMCP = FastMCP  # type: ignore[attr-defined]
    sys.modules["fastmcp"] = stub


def _install_stack_dumper() -> None:
    """Dump every thread's stack every N seconds when debugging a hang.

    A benchmark run that stalls with no CPU and no open connections is otherwise
    opaque (the harness buffers stdout and prints nothing).  Set
    ``ADVTEXT2SQL_FAULTHANDLER_SECONDS=30`` to get a stack trace on stderr.
    """

    seconds = os.environ.get("ADVTEXT2SQL_FAULTHANDLER_SECONDS")
    if not seconds:
        return
    try:
        import faulthandler

        faulthandler.dump_traceback_later(float(seconds), repeat=True)
    except Exception:  # pragma: no cover - diagnostics only
        pass


_install_stub()
_install_stack_dumper()
