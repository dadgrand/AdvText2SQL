"""Bounded database operations for the isolated baseline launcher."""

import asyncio
import logging
import os
from time import monotonic

import psycopg

from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.exc import InterfaceError, OperationalError, TimeoutError
from sqlalchemy.pool import NullPool

logger = logging.getLogger("text2sql_tool")


def bounded_wait(generator, timeout, close):
    """Bound network reads even when a live SSH socket stops forwarding replies."""
    deadline = monotonic() + timeout
    try:
        pending = next(generator)
        while True:
            if monotonic() >= deadline:
                close()
                raise psycopg.OperationalError(f"Database response timed out after {timeout}s")
            ready = yield pending
            pending = generator.send(ready)
    except StopIteration as result:
        return result.value


class BoundedConnection(psycopg.Connection):
    def wait(self, gen, interval=0.1):
        return super().wait(bounded_wait(gen, 40, self.close), interval=interval)


def database_engine(db_uri):
    # Do not keep an idle PostgreSQL session for every Ambrosia database.
    engine = create_engine(
        db_uri, poolclass=NullPool,
        connect_args={
            "connect_timeout": 10,
            "keepalives": 1,
            "keepalives_idle": 10,
            "keepalives_interval": 5,
            "keepalives_count": 3,
            "tcp_user_timeout": 20000,
            "options": "-c statement_timeout=30000 -c lock_timeout=10000",
        },
    )

    @event.listens_for(engine, "do_connect")
    def connect(dialect, connection_record, args, kwargs):
        return BoundedConnection.connect(*args, **kwargs)

    return engine


async def build_tools(items, tool_cls, client, progress, attempts=5, retry_delay=5):
    tools = {}
    db_ids = sorted({item["db_id"] for item in items})
    try:
        for index, db_id in enumerate(db_ids, 1):
            uri = URL.create(
                "postgresql+psycopg", username=os.environ["DB_USER"],
                password=os.environ["DB_PASS"], host="127.0.0.1", port=5444, database=db_id,
            ).render_as_string(hide_password=False)
            for attempt in range(1, attempts + 1):
                progress({"phase": "building", "databases_ready": len(tools),
                          "databases_total": len(db_ids), "current_database": db_id,
                          "build_attempt": attempt})
                logger.info("[DB %s/%s] Building %s (attempt %s/%s)",
                            index, len(db_ids), db_id, attempt, attempts)
                tool = None
                try:
                    tool = tool_cls(db_uri=uri, llm_client=client)
                    await tool.build()
                except (OperationalError, InterfaceError, TimeoutError) as error:
                    if tool is not None:
                        tool.engine.dispose()
                    logger.warning("Database %s disconnected (%s); attempt %s/%s",
                                   db_id, type(error).__name__, attempt, attempts)
                    if attempt == attempts:
                        raise RuntimeError(
                            f"Database {db_id} unavailable after {attempts} attempts; "
                            "no questions skipped. Fix connection and repeat the command."
                        ) from None
                    await asyncio.sleep(retry_delay)
                except BaseException:
                    if tool is not None:
                        tool.engine.dispose()
                    raise
                else:
                    tools[db_id] = tool
                    logger.info("[DB %s/%s] Ready: %s", index, len(db_ids), db_id)
                    break
        progress({"phase": "ready", "databases_ready": len(tools),
                  "databases_total": len(db_ids)})
        return tools
    except BaseException:
        for tool in tools.values():
            tool.engine.dispose()
        raise
