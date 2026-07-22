"""SQLite connection and migrations (docs/03-storage.md)."""

import sqlite3
from importlib import resources
from pathlib import Path

Db = sqlite3.Connection


def open_db(db_path: Path) -> Db:
    """Open (creating if needed) the database with migrations applied."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    _apply_migrations(db)
    return db


def _apply_migrations(db: Db) -> None:
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
