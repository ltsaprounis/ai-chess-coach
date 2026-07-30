"""Player profile storage tests (docs/03-storage.md, docs/06-coach.md,
"Player profile") — migration 010 and the `player_profiles` cache
(`get_player_profile`/`save_player_profile`), which mirrors
`storage/reports.py`'s cache semantics except that the profile is
singular per player rather than keyed per agent.
"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from chess_coach.domain import PlayerProfile
from chess_coach.storage import (
    CachedProfile,
    Db,
    get_player_profile,
    open_db,
    save_player_profile,
)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Db]:
    connection = open_db(tmp_path / "test.sqlite3")
    yield connection
    connection.close()


def _profile(**overrides: object) -> PlayerProfile:
    """A minimal-but-valid `PlayerProfile`; only the fields a given test
    cares about need overriding."""
    judgment_counts = {
        "best": 10,
        "good": 8,
        "inaccuracy": 4,
        "mistake": 2,
        "blunder": 1,
    }
    base: dict[str, object] = {
        "username": "testuser",
        "games_covered": 42,
        "window_start": 1_000,
        "window_end": 2_000,
        "player_moves": 500,
        "overall_acpl": 25.5,
        "judgment_counts": judgment_counts,
        "phases": {
            "opening": {
                "moves": 200,
                "acpl": 12.0,
                "judgment_counts": judgment_counts,
            },
            "middlegame": {
                "moves": 250,
                "acpl": 35.0,
                "judgment_counts": judgment_counts,
            },
            "endgame": {
                "moves": 50,
                "acpl": 20.0,
                "judgment_counts": judgment_counts,
            },
        },
        "time_classes": [
            {
                "time_class": "blitz",
                "record": {"games": 42, "wins": 20, "losses": 15, "draws": 7},
                "rating_start": 1400,
                "rating_end": 1500,
                "rating_min": 1380,
                "rating_max": 1520,
            }
        ],
        "months": [
            {
                "month": "2026-07",
                "games": 42,
                "rating_end": 1500,
                "acpl": 25.5,
                "blunder_rate": 0.02,
            }
        ],
        "openings": [
            {
                "color": "white",
                "name": "Italian Game",
                "moves": "1.e4 2.Nf3 3.Bc4",
                "games": 10,
                "score": 0.6,
                "faced": False,
            }
        ],
        "error_patterns": [],
        "narrative": None,
    }
    return PlayerProfile.model_validate({**base, **overrides})


# --- migration 010 ---------------------------------------------------


def test_migration_010_creates_player_profiles_table_and_bumps_user_version(
    db: Db,
) -> None:
    tables = {
        row["name"]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert "player_profiles" in tables

    row = db.execute("PRAGMA user_version").fetchone()
    assert row is not None
    assert row[0] == 10


# --- save / get round trip --------------------------------------------


def test_get_player_profile_misses_when_absent(db: Db) -> None:
    assert get_player_profile(db, "ghost") is None


def test_save_and_get_player_profile_round_trip(db: Db) -> None:
    facts = _profile()
    narrative = "You favor sharp tactical positions and tend to rush endgames."

    created_at = save_player_profile(
        db,
        "testuser",
        agent_id="coach-a",
        prompt_version="v1",
        facts=facts,
        narrative=narrative,
    )

    cached = get_player_profile(db, "testuser")
    assert cached == CachedProfile(
        profile=facts.model_copy(update={"narrative": narrative}),
        agent_id="coach-a",
        prompt_version="v1",
        created_at=created_at,
    )


def test_save_player_profile_returns_the_created_at_it_persisted(db: Db) -> None:
    created_at = save_player_profile(
        db,
        "testuser",
        agent_id="coach-a",
        prompt_version="v1",
        facts=_profile(),
        narrative="narrative text",
    )

    assert created_at > 0
    cached = get_player_profile(db, "testuser")
    assert cached is not None
    assert cached.created_at == created_at


# --- upsert -------------------------------------------------------------


def test_save_player_profile_upsert_replaces_the_row(
    db: Db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One row per player: a regeneration under a *different* agent still
    overwrites the existing row (the profile is singular, not per-agent
    like `reports`), and `created_at` moves to the new save's reading."""
    monkeypatch.setattr("chess_coach.storage.profiles.time.time", lambda: 1_000.0)
    save_player_profile(
        db,
        "testuser",
        agent_id="coach-a",
        prompt_version="v1",
        facts=_profile(games_covered=10),
        narrative="first narrative",
    )

    monkeypatch.setattr("chess_coach.storage.profiles.time.time", lambda: 2_000.0)
    save_player_profile(
        db,
        "testuser",
        agent_id="coach-b",
        prompt_version="v2",
        facts=_profile(games_covered=20),
        narrative="second narrative",
    )

    cached = get_player_profile(db, "testuser")
    assert cached is not None
    assert cached.agent_id == "coach-b"
    assert cached.prompt_version == "v2"
    assert cached.profile.games_covered == 20
    assert cached.profile.narrative == "second narrative"
    assert cached.created_at == 2_000  # moved to the second save's reading

    (row,) = db.execute(
        "SELECT COUNT(*) AS n FROM player_profiles WHERE username = 'testuser'"
    ).fetchall()
    assert row["n"] == 1  # replaced, never duplicated


def test_save_player_profile_upsert_is_scoped_to_its_username(db: Db) -> None:
    save_player_profile(
        db,
        "alice",
        agent_id="coach-a",
        prompt_version="v1",
        facts=_profile(username="alice"),
        narrative="alice's narrative",
    )
    save_player_profile(
        db,
        "bob",
        agent_id="coach-a",
        prompt_version="v1",
        facts=_profile(username="bob"),
        narrative="bob's narrative",
    )

    alice = get_player_profile(db, "alice")
    bob = get_player_profile(db, "bob")
    assert alice is not None and alice.profile.narrative == "alice's narrative"
    assert bob is not None and bob.profile.narrative == "bob's narrative"


# --- facts/narrative split -----------------------------------------------


def test_facts_column_excludes_the_narrative_text(db: Db) -> None:
    distinctive = "zzz-distinctive-narrative-marker-zzz"
    save_player_profile(
        db,
        "testuser",
        agent_id="coach-a",
        prompt_version="v1",
        facts=_profile(),
        narrative=distinctive,
    )

    (row,) = db.execute(
        "SELECT facts, narrative FROM player_profiles WHERE username = ?",
        ("testuser",),
    ).fetchall()
    assert distinctive not in row["facts"]
    assert "narrative" not in json.loads(row["facts"])
    assert row["narrative"] == distinctive
