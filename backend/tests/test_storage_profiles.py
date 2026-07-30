"""Player profile storage tests (docs/03-storage.md, docs/06-coach.md,
"Player profile") — migration 011 and the `player_profiles` cache
(`get_player_profile`/`save_player_profile`), which mirrors
`storage/reports.py`'s cache semantics except that the profile is keyed
by (player, time control) rather than per agent.
"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from chess_coach.domain import PlayerProfile, TimeClass
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


# --- migration 011 ---------------------------------------------------


def test_migration_011_creates_player_profiles_table_and_bumps_user_version(
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
    assert row[0] == 11


def test_migration_011_keys_player_profiles_by_username_and_time_class(
    db: Db,
) -> None:
    """The re-key is the migration's whole point: `username` alone was the
    primary key through 010, so without the composite key a second time
    control's narrative would overwrite the first's."""
    key_columns = {
        row["name"]
        for row in db.execute("PRAGMA table_info(player_profiles)").fetchall()
        if row["pk"]
    }
    assert key_columns == {"username", "time_class"}


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


def test_profiles_are_kept_apart_per_time_class(db: Db) -> None:
    """One row per (player, time control): a rapid regeneration must not
    overwrite the same player's bullet narrative, and each scope reads
    back its own (docs/06-coach.md, "Player profile")."""
    scopes: tuple[tuple[TimeClass | None, str], ...] = (
        ("rapid", "patient in rapid"),
        ("bullet", "reckless in bullet"),
        (None, "mixed across controls"),
    )
    for time_class, narrative in scopes:
        save_player_profile(
            db,
            "testuser",
            time_class=time_class,
            agent_id="coach-a",
            prompt_version="profile-v2",
            facts=_profile(time_class=time_class),
            narrative=narrative,
        )

    rapid = get_player_profile(db, "testuser", time_class="rapid")
    bullet = get_player_profile(db, "testuser", time_class="bullet")
    mixed = get_player_profile(db, "testuser")
    assert rapid is not None and rapid.profile.narrative == "patient in rapid"
    assert bullet is not None and bullet.profile.narrative == "reckless in bullet"
    assert mixed is not None and mixed.profile.narrative == "mixed across controls"

    (row,) = db.execute(
        "SELECT COUNT(*) AS n FROM player_profiles WHERE username = 'testuser'"
    ).fetchall()
    assert row["n"] == 3


def test_all_classes_profile_is_stored_under_the_empty_sentinel(db: Db) -> None:
    """`time_class=None` persists as '' — the same sentinel `ReportKey`
    uses — so the mixed profile is a real row rather than a NULL that
    would never match an equality lookup."""
    save_player_profile(
        db,
        "testuser",
        time_class=None,
        agent_id="coach-a",
        prompt_version="profile-v2",
        facts=_profile(),
        narrative="mixed",
    )

    (row,) = db.execute(
        "SELECT time_class FROM player_profiles WHERE username = 'testuser'"
    ).fetchall()
    assert row["time_class"] == ""


def test_a_missing_time_class_does_not_fall_back_to_another(db: Db) -> None:
    """A stored rapid profile is not served for a bullet request: the
    embed paths choose their own fallback deliberately, so storage must
    report the miss rather than quietly substituting a scope."""
    save_player_profile(
        db,
        "testuser",
        time_class="rapid",
        agent_id="coach-a",
        prompt_version="profile-v2",
        facts=_profile(time_class="rapid"),
        narrative="patient in rapid",
    )

    assert get_player_profile(db, "testuser", time_class="bullet") is None
    assert get_player_profile(db, "testuser") is None  # nor the mixed slot


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
