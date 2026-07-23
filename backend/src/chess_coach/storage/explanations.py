"""Coach explanation cache (docs/03-storage.md)."""

import time

from chess_coach.storage.db import Db


def get_explanation(db: Db, game_id: str, ply: int, agent_id: str) -> str | None:
    row = db.execute(
        """
        SELECT text FROM explanations
        WHERE game_id = ? AND ply = ? AND agent_id = ?
        """,
        (game_id, ply, agent_id),
    ).fetchone()
    return None if row is None else str(row["text"])


def save_explanation(db: Db, game_id: str, ply: int, agent_id: str, text: str) -> None:
    """Upsert: overwrites any existing row and resets created_at to now."""
    with db:
        db.execute(
            """
            INSERT INTO explanations (game_id, ply, agent_id, text, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (game_id, ply, agent_id) DO UPDATE SET
                text = excluded.text,
                created_at = excluded.created_at
            """,
            (game_id, ply, agent_id, text, int(time.time())),
        )
