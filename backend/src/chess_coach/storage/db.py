"""SQLite connection and migrations (docs/03-storage.md)."""

import sqlite3
import threading
from collections.abc import Iterable, Mapping, Sequence
from importlib import resources
from pathlib import Path
from types import TracebackType
from typing import Self

_SqlParams = Sequence[object] | Mapping[str, object]


class Db:
    """The one shared connection, plus the lock its writers need.

    CPython's serialized sqlite3 (threadsafety 3) makes each *call* on
    a shared connection thread-safe, but a transaction is several
    calls: two threads entering `with db:` at once interleave their
    BEGIN/COMMIT and abort with "bad parameter or other API misuse".
    The context manager therefore holds a lock for the whole write
    transaction — writers serialize, reads stay lock-free (safe per
    call, exactly as before).
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._write_lock = threading.Lock()

    def execute(self, sql: str, parameters: _SqlParams = ()) -> sqlite3.Cursor:
        return self._connection.execute(sql, parameters)

    def executemany(self, sql: str, parameters: Iterable[_SqlParams]) -> sqlite3.Cursor:
        return self._connection.executemany(sql, parameters)

    def __enter__(self) -> Self:
        self._write_lock.acquire()
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
            self._write_lock.release()

    def close(self) -> None:
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
