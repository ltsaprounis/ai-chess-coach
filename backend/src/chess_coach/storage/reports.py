"""Whole-report coaching cache (docs/03-storage.md).

A whole-report coaching run is the most expensive call the app makes;
house policy is "user-triggered and cached" exactly as it is for move
explanations (`storage/explanations.py`), which this module mirrors.
"""

import time

from pydantic import BaseModel

from chess_coach.storage.db import Db


class ReportKey(BaseModel):
    """Every field is part of the cache key.

    `since`/`until` default to 0 (open-ended) and `time_class` to ""
    (all controls) rather than None: SQLite treats NULLs as distinct in
    a primary key, so a nullable window would make every all-time
    report a cache miss that silently inserts a duplicate row and
    re-bills the model.
    """

    username: str
    agent_id: str
    prompt_version: str
    since: int = 0
    until: int = 0
    time_class: str = ""


class CachedReport(BaseModel):
    prompt: str
    advice: str
    games_analyzed: int
    created_at: int


def get_report(db: Db, key: ReportKey) -> CachedReport | None:
    row = db.execute(
        """
        SELECT prompt, advice, games_analyzed, created_at
        FROM reports
        WHERE username = ? AND agent_id = ? AND since = ? AND until = ?
              AND time_class = ? AND prompt_version = ?
        """,
        (
            key.username,
            key.agent_id,
            key.since,
            key.until,
            key.time_class,
            key.prompt_version,
        ),
    ).fetchone()
    if row is None:
        return None
    return CachedReport(
        prompt=row["prompt"],
        advice=row["advice"],
        games_analyzed=row["games_analyzed"],
        created_at=row["created_at"],
    )


def save_report(
    db: Db, key: ReportKey, prompt: str, advice: str, games_analyzed: int
) -> int:
    """Upsert: overwrites any existing row and resets created_at to now.

    Returns the `created_at` it persisted. Storage is the single place
    that reads this clock, so callers (the API layer builds its own
    response with a `generated_at`) must use the returned value rather
    than reading `time.time()` a second time — two independent reads
    can straddle a second boundary and disagree.
    """
    created_at = int(time.time())
    with db:
        db.execute(
            """
            INSERT INTO reports
                (username, agent_id, since, until, time_class,
                 prompt_version, prompt, advice, games_analyzed, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (username, agent_id, since, until, time_class,
                         prompt_version) DO UPDATE SET
                prompt = excluded.prompt,
                advice = excluded.advice,
                games_analyzed = excluded.games_analyzed,
                created_at = excluded.created_at
            """,
            (
                key.username,
                key.agent_id,
                key.since,
                key.until,
                key.time_class,
                key.prompt_version,
                prompt,
                advice,
                games_analyzed,
                created_at,
            ),
        )
    return created_at
