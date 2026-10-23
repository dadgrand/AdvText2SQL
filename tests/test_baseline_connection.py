import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from baseline_connection import bounded_wait, build_tools, database_engine
import psycopg


class Engine:
    def __init__(self):
        self.disposed = False

    def dispose(self):
        self.disposed = True


class ConnectionTests(unittest.TestCase):
    def run_build(self, factory, attempts=3):
        with patch.dict(os.environ, {"DB_USER": "test", "DB_PASS": "a@b:c"}):
            return asyncio.run(build_tools(
                [{"db_id": "a"}, {"db_id": "b"}], factory, object(),
                lambda _: None, attempts=attempts, retry_delay=0,
            ))

    def test_reconnect_retries_only_failed_database(self):
        created = []

        class Tool:
            def __init__(self, db_uri, llm_client):
                self.db = db_uri.rsplit("/", 1)[-1]
                self.engine = Engine()
                created.append(self)

            async def build(self):
                if self.db == "b" and len(created) == 2:
                    raise OperationalError("read schema", {}, Exception("disconnected"))

        tools = self.run_build(Tool)
        self.assertEqual(set(tools), {"a", "b"})
        self.assertEqual([tool.db for tool in created], ["a", "b", "b"])
        self.assertTrue(created[1].engine.disposed)
        self.assertFalse(created[0].engine.disposed)

    def test_retry_limit_disposes_all_engines(self):
        created = []

        class Tool:
            def __init__(self, db_uri, llm_client):
                self.db = db_uri.rsplit("/", 1)[-1]
                self.engine = Engine()
                created.append(self)

            async def build(self):
                if self.db == "b":
                    raise OperationalError("read schema", {}, Exception("disconnected"))

        with self.assertRaisesRegex(RuntimeError, "after 3 attempts"):
            self.run_build(Tool)
        self.assertEqual(len(created), 4)
        self.assertTrue(all(tool.engine.disposed for tool in created))

    def test_programming_error_is_not_retried(self):
        created = []

        class Tool:
            def __init__(self, **kwargs):
                self.engine = Engine()
                created.append(self)

            async def build(self):
                raise ValueError("invalid schema")

        with self.assertRaises(ValueError):
            self.run_build(Tool)
        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].engine.disposed)

    def test_engine_has_timeouts_and_no_idle_pool(self):
        engine = database_engine("postgresql+psycopg://test:secret@127.0.0.1:5444/test")
        try:
            self.assertIsInstance(engine.pool, NullPool)
            with patch("baseline_connection.BoundedConnection.connect", side_effect=RuntimeError("offline")) as connect:
                with self.assertRaisesRegex(RuntimeError, "offline"):
                    engine.connect()
                kwargs = connect.call_args.kwargs
                self.assertEqual(kwargs["connect_timeout"], 10)
                self.assertIn("statement_timeout=30000", kwargs["options"])
                self.assertEqual(kwargs["tcp_user_timeout"], 20000)
        finally:
            engine.dispose()

    def test_read_timeout_closes_stalled_connection(self):
        def stalled():
            while True:
                yield 1

        closed = []
        with patch("baseline_connection.monotonic", side_effect=[0, 0, 41]):
            generator = bounded_wait(stalled(), 40, lambda: closed.append(True))
            self.assertEqual(next(generator), 1)
            with self.assertRaisesRegex(psycopg.OperationalError, "timed out"):
                generator.send(0)
        self.assertEqual(closed, [True])

    def test_read_wrapper_preserves_completed_result(self):
        def completed():
            yield 1
            return "schema"

        generator = bounded_wait(completed(), 40, lambda: self.fail("Unexpected close"))
        self.assertEqual(next(generator), 1)
        with self.assertRaises(StopIteration) as result:
            generator.send(1)
        self.assertEqual(result.exception.value, "schema")


if __name__ == "__main__":
    unittest.main()
