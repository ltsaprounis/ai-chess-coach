"""Durable player-profile cache (docs/03-storage.md).

The profile (docs/06-coach.md, "Player profile") is one row per
player, upserted on regeneration under any agent -- singular, not
per-agent, which is the one way this module diverges from
`storage/reports.py` it otherwise mirrors. `facts` stores the
deterministic snapshot with its `narrative` field excluded (the LLM
layer rides beside it in its own column); `get_player_profile`
re-attaches the two into one `PlayerProfile`.
"""

import time

from pydantic import BaseModel

from chess_coach.domain import PlayerProfile
from chess_coach.storage.db import Db


class CachedProfile(BaseModel):
    profile: PlayerProfile  # the facts snapshot, narrative attached
    agent_id: str
    prompt_version: str
    created_at: int


def get_player_profile(db: Db, username: str) -> CachedProfile | None:
    row = db.execute(
        """
        SELECT agent_id, prompt_version, facts, narrative, created_at
        FROM player_profiles
        WHERE username = ?
        """,
        (username,),
    ).fetchone()
    if row is None:
        return None
    facts = PlayerProfile.model_validate_json(row["facts"])
    return CachedProfile(
        profile=facts.model_copy(update={"narrative": row["narrative"]}),
        agent_id=row["agent_id"],
        prompt_version=row["prompt_version"],
        created_at=row["created_at"],
    )


def save_player_profile(
    db: Db,
    username: str,
    *,
    agent_id: str,
    prompt_version: str,
    facts: PlayerProfile,
    narrative: str,
) -> int:
    """Upsert: one row per player, replaced by any agent's regeneration.

    Returns the `created_at` it persisted. Storage is the single place
    that reads this clock, so callers must use the returned value
    rather than reading `time.time()` a second time -- the same
    single-reading rule as `save_report`.
    """
    created_at = int(time.time())
    with db:
        db.execute(
            """
            INSERT INTO player_profiles
                (username, agent_id, prompt_version, facts, narrative, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (username) DO UPDATE SET
                agent_id = excluded.agent_id,
                prompt_version = excluded.prompt_version,
                facts = excluded.facts,
                narrative = excluded.narrative,
                created_at = excluded.created_at
            """,
            (
                username,
                agent_id,
                prompt_version,
                facts.model_dump_json(exclude={"narrative"}),
                narrative,
                created_at,
            ),
        )
    return created_at
