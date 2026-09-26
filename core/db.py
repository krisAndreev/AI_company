"""
Shared SQLite connection and transaction helper.

All stores (tasks, projects, events) share one Database so they live in the
same file and can be changed together in a single transaction.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Database:
    def __init__(self, path: str | Path = "data/company.db"):
        self.path = str(path)
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: transactions are managed explicitly in transaction().
        # check_same_thread=False: the dashboard guards each connection with its own lock.
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout = 10000")  # wait for the other writer, don't fail
        if str(path) != ":memory:":
            # WAL: the dashboard can read while the worker loop writes.
            self.conn.execute("PRAGMA journal_mode = WAL")

    def execute(self, sql: str, args=()) -> sqlite3.Cursor:
        return self.conn.execute(sql, args)

    def executescript(self, sql: str) -> None:
        self.conn.executescript(sql)

    @contextmanager
    def transaction(self):
        """One write transaction. Nested calls join the outer transaction."""
        if self.conn.in_transaction:
            yield
            return
        self.conn.execute("BEGIN IMMEDIATE")  # take the write lock now
        try:
            yield
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self.conn.close()
