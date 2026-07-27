"""SQLite connection and migrations (docs/03-storage.md)."""

import sqlite3
import threading
from collections.abc import Iterable, Mapping, Sequence
from importlib import resources
from pathlib import Path
from types import TracebackType
from typing import Self

_SqlParams = Sequence[object] | Mapping[str, object]


class FetchedRows:
    """A statement's rows, materialized while the connection lock was
    held — the live cursor never escapes the lock, so no other thread
    can race its stepping or the statement cache behind it."""

    def __init__(self, rows: list[sqlite3.Row]) -> None:
        self._rows = rows
        self._next = 0

    def fetchall(self) -> list[sqlite3.Row]:
        remaining = self._rows[self._next :]
        self._next = len(self._rows)
        return remaining

    def fetchone(self) -> sqlite3.Row | None:
        if self._next >= len(self._rows):
            return None
        row = self._rows[self._next]
        self._next += 1
        return row


class Db:
    """The one shared connection, plus the lock that makes sharing safe.

    CPython's serialized sqlite3 (threadsafety 3) guards each C-level
    call, but that is not enough for a connection shared across
    FastAPI's worker threads: a transaction is several calls (two
    threads inside `with db:` interleave BEGIN/COMMIT and abort with
    "bad parameter or other API misuse"), and even a lone read races
    another thread's statements in pysqlite's per-connection statement
    cache — observed as interpreter segfaults at connection close once
    writes moved off the event loop. So one re-entrant lock serializes
    *everything*: each statement runs and is fully fetched under it
    (`FetchedRows`), and the context manager holds it across a whole
    write transaction, commit included. Re-entrant, so statements
    inside `with db:` nest under the transaction's hold.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._lock = threading.RLock()

    def execute(self, sql: str, parameters: _SqlParams = ()) -> FetchedRows:
        with self._lock:
            cursor = self._connection.execute(sql, parameters)
            # description is None for statements that return no rows
            # (INSERT/UPDATE), where fetchall would raise.
            rows = cursor.fetchall() if cursor.description is not None else []
            return FetchedRows(rows)

    def executemany(self, sql: str, parameters: Iterable[_SqlParams]) -> None:
        with self._lock:
            self._connection.executemany(sql, parameters)

    def __enter__(self) -> Self:
        self._lock.acquire()
        self._connection.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        # The connection's own __exit__ commits (or rolls back); the
        # lock must outlive that commit, not just the statements.
        try:
            return self._connection.__exit__(exc_type, exc, traceback)
        finally:
            self._lock.release()

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def open_db(db_path: Path) -> Db:
    """Open (creating if needed) the database with migrations applied.

    The one connection is shared across FastAPI's worker threads:
    safe only because CPython's sqlite3 is built serialized
    (threadsafety 3) — SQLite then locks around every call — and
    because `Db` serializes write transactions on top of that.
    """
    if sqlite3.threadsafety != 3:
        raise RuntimeError("sqlite3 must be built with serialized threading")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    _apply_migrations(connection)
    return Db(connection)


def _apply_migrations(db: sqlite3.Connection) -> None:
    applied = int(db.execute("PRAGMA user_version").fetchone()[0])
    for number, sql in _migrations():
        if number <= applied:
            continue
        db.executescript(sql)
        db.execute(f"PRAGMA user_version = {number}")
        db.commit()


def _migrations() -> list[tuple[int, str]]:
    directory = resources.files("chess_coach.storage") / "migrations"
    numbered = [
        (int(entry.name.split("_", 1)[0]), entry.read_text())
        for entry in directory.iterdir()
        if entry.name.endswith(".sql")
    ]
    return sorted(numbered)
