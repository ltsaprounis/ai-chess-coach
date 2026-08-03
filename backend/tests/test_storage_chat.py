"""Coach chat storage tests (docs/03-storage.md, docs/archive/
coach-chat.md) — migration 009, the thread/message repo, and the
`GameFilters` additions the chat toolkit's `find_games` tool needs.
"""

import sqlite3
from collections.abc import Iterator
from importlib import resources
from pathlib import Path

import pytest

from chess_coach.domain import ChatMessage, Opening
from chess_coach.storage import (
    Db,
    GameFilters,
    append_chat_exchange,
    clear_chat_provider_state,
    create_chat_thread,
    delete_chat_thread,
    get_chat_thread,
    list_chat_messages,
    list_chat_threads,
    list_games,
    open_db,
    set_opening,
    upsert_games,
)
from tests.factories import make_game


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Db]:
    connection = open_db(tmp_path / "test.sqlite3")
    yield connection
    connection.close()


# --- migration 009 -----------------------------------------------------


def test_migration_009_creates_chat_tables_and_index_on_fresh_open(db: Db) -> None:
    tables = {
        row["name"]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {"chat_threads", "chat_messages"} <= tables

    indexes = {
        row["name"]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    assert "idx_chat_threads_username_updated_at" in indexes


def _apply_migrations_through(connection: sqlite3.Connection, max_number: int) -> None:
    """Build a database as it would exist right after migration
    `max_number`, using the real migration files — the same source
    `open_db` reads — rather than a hand-copied schema snapshot."""
    directory = resources.files("chess_coach.storage") / "migrations"
    numbered = sorted(
        (int(entry.name.split("_", 1)[0]), entry.read_text())
        for entry in directory.iterdir()
        if entry.name.endswith(".sql")
    )
    for number, sql in numbered:
        if number > max_number:
            continue
        connection.executescript(sql)
    connection.execute(f"PRAGMA user_version = {max_number}")
    connection.commit()


def test_migration_009_applies_on_top_of_migrations_1_through_8(
    tmp_path: Path,
) -> None:
    """A database carrying only migrations 1-8 (no chat tables yet):
    opening it must add the chat tables without disturbing existing
    data, exactly like migration 006/007/008's own re-open tests."""
    path = tmp_path / "pre_chat.sqlite3"
    legacy = sqlite3.connect(path)
    _apply_migrations_through(legacy, max_number=8)
    legacy.execute(
        "INSERT INTO games (id, username, color, pgn, san_moves,"
        " time_control, time_class, result, end_time, opponent,"
        " player_rating, opponent_rating, chesscom_uuid) VALUES"
        " ('uuid-1:alice', 'alice', 'white', '1. e4 *', '[\"e4\"]',"
        " '600', 'rapid', 'win', 100, 'bob', 1500, 1490, 'uuid-1')"
    )
    legacy.commit()
    legacy.close()

    migrated = open_db(path)  # applies only migration 009
    tables = {
        row["name"]
        for row in migrated.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {"chat_threads", "chat_messages"} <= tables
    # pre-existing data survives the migration untouched
    assert [g.id for g in list_games(migrated, "alice", GameFilters())] == [
        "uuid-1:alice"
    ]
    # and the new tables are immediately usable
    thread = create_chat_thread(
        migrated, thread_id="t1", username="alice", agent_id="coach-a", scope="report"
    )
    assert get_chat_thread(migrated, "t1") == thread
    migrated.close()


# --- create / get / list threads ----------------------------------------


def test_create_chat_thread_reads_the_clock_once(db: Db) -> None:
    thread = create_chat_thread(
        db, thread_id="t1", username="alice", agent_id="coach-a", scope="report"
    )
    assert thread.created_at == thread.updated_at
    assert thread.created_at > 0
    assert thread.provider_state is None
    assert thread.since == 0
    assert thread.until == 0
    assert thread.time_class == ""
    assert thread.game_id is None
    assert thread.ply is None


def test_create_and_get_chat_thread_round_trip(db: Db) -> None:
    created = create_chat_thread(
        db,
        thread_id="t1",
        username="alice",
        agent_id="coach-a",
        scope="game",
        game_id="game-1",
        ply=12,
        since=100,
        until=200,
        time_class="blitz",
    )
    assert get_chat_thread(db, "t1") == created


def test_get_chat_thread_misses_when_absent(db: Db) -> None:
    assert get_chat_thread(db, "missing") is None


def test_list_chat_threads_orders_most_recently_updated_first(db: Db) -> None:
    create_chat_thread(
        db, thread_id="older", username="alice", agent_id="coach-a", scope="report"
    )
    create_chat_thread(
        db, thread_id="newer", username="alice", agent_id="coach-a", scope="report"
    )
    # bump "older" well past "newer"'s creation-time updated_at
    append_chat_exchange(
        db,
        "older",
        ChatMessage(role="user", content="hi", created_at=9_999_999_999),
        ChatMessage(role="assistant", content="hello", created_at=9_999_999_999),
        provider_state=None,
    )
    assert [t.id for t in list_chat_threads(db, "alice")] == ["older", "newer"]


def test_list_chat_threads_is_scoped_to_username(db: Db) -> None:
    create_chat_thread(
        db, thread_id="t1", username="alice", agent_id="coach-a", scope="report"
    )
    create_chat_thread(
        db, thread_id="t2", username="bob", agent_id="coach-a", scope="report"
    )
    assert [t.id for t in list_chat_threads(db, "alice")] == ["t1"]
    assert [t.id for t in list_chat_threads(db, "bob")] == ["t2"]


def test_list_chat_threads_title_and_message_count(db: Db) -> None:
    create_chat_thread(
        db, thread_id="empty", username="alice", agent_id="coach-a", scope="report"
    )
    create_chat_thread(
        db, thread_id="t1", username="alice", agent_id="coach-a", scope="report"
    )
    long_message = "x" * 200
    append_chat_exchange(
        db,
        "t1",
        ChatMessage(role="user", content=long_message, created_at=1000),
        ChatMessage(role="assistant", content="reply", created_at=1001),
        provider_state=None,
    )

    summaries = {s.id: s for s in list_chat_threads(db, "alice")}
    assert summaries["empty"].title == ""  # no messages yet
    assert summaries["empty"].messages == 0
    assert summaries["t1"].title == long_message[:80]
    assert len(summaries["t1"].title) == 80
    assert summaries["t1"].messages == 2  # user + assistant rows


def test_list_chat_threads_title_is_the_first_user_message_not_assistant(
    db: Db,
) -> None:
    create_chat_thread(
        db, thread_id="t1", username="alice", agent_id="coach-a", scope="report"
    )
    append_chat_exchange(
        db,
        "t1",
        ChatMessage(role="user", content="what about the endgame?", created_at=1),
        ChatMessage(role="assistant", content="glad you asked", created_at=2),
        provider_state=None,
    )
    append_chat_exchange(
        db,
        "t1",
        ChatMessage(role="user", content="a follow-up", created_at=3),
        ChatMessage(role="assistant", content="another reply", created_at=4),
        provider_state=None,
    )
    (summary,) = list_chat_threads(db, "alice")
    assert summary.title == "what about the endgame?"
    assert summary.messages == 4


# --- delete ---------------------------------------------------------------


def test_delete_chat_thread_returns_false_when_missing(db: Db) -> None:
    assert delete_chat_thread(db, "missing") is False


def test_delete_chat_thread_removes_thread_and_messages(db: Db) -> None:
    create_chat_thread(
        db, thread_id="t1", username="alice", agent_id="coach-a", scope="report"
    )
    append_chat_exchange(
        db,
        "t1",
        ChatMessage(role="user", content="hi", created_at=1),
        ChatMessage(role="assistant", content="hello", created_at=2),
        provider_state="token",
    )

    assert delete_chat_thread(db, "t1") is True
    assert get_chat_thread(db, "t1") is None
    assert list_chat_messages(db, "t1") == []
    (row,) = db.execute(
        "SELECT COUNT(*) AS n FROM chat_messages WHERE thread_id = 't1'"
    ).fetchall()
    assert row["n"] == 0
    assert delete_chat_thread(db, "t1") is False  # already gone


def test_delete_chat_thread_does_not_touch_other_threads(db: Db) -> None:
    create_chat_thread(
        db, thread_id="keep", username="alice", agent_id="coach-a", scope="report"
    )
    create_chat_thread(
        db, thread_id="gone", username="alice", agent_id="coach-a", scope="report"
    )
    append_chat_exchange(
        db,
        "keep",
        ChatMessage(role="user", content="hi", created_at=1),
        ChatMessage(role="assistant", content="hello", created_at=2),
        provider_state=None,
    )

    assert delete_chat_thread(db, "gone") is True
    assert get_chat_thread(db, "keep") is not None
    assert len(list_chat_messages(db, "keep")) == 2


# --- messages / append_chat_exchange --------------------------------------


def test_list_chat_messages_is_empty_for_a_fresh_thread(db: Db) -> None:
    create_chat_thread(
        db, thread_id="t1", username="alice", agent_id="coach-a", scope="report"
    )
    assert list_chat_messages(db, "t1") == []


def test_append_chat_exchange_orders_seq_ascending(db: Db) -> None:
    create_chat_thread(
        db, thread_id="t1", username="alice", agent_id="coach-a", scope="report"
    )
    append_chat_exchange(
        db,
        "t1",
        ChatMessage(role="user", content="first question", created_at=100),
        ChatMessage(role="assistant", content="first answer", created_at=101),
        provider_state="tok-1",
    )
    append_chat_exchange(
        db,
        "t1",
        ChatMessage(role="user", content="second question", created_at=200),
        ChatMessage(role="assistant", content="second answer", created_at=201),
        provider_state="tok-2",
    )

    messages = list_chat_messages(db, "t1")
    assert [m.content for m in messages] == [
        "first question",
        "first answer",
        "second question",
        "second answer",
    ]
    assert [m.role for m in messages] == ["user", "assistant", "user", "assistant"]

    seqs = [
        row["seq"]
        for row in db.execute(
            "SELECT seq FROM chat_messages WHERE thread_id = 't1' ORDER BY seq"
        ).fetchall()
    ]
    assert seqs == [1, 2, 3, 4]


def test_append_chat_exchange_sets_provider_state_and_updated_at(db: Db) -> None:
    thread = create_chat_thread(
        db, thread_id="t1", username="alice", agent_id="coach-a", scope="report"
    )
    append_chat_exchange(
        db,
        "t1",
        ChatMessage(role="user", content="q", created_at=100),
        ChatMessage(role="assistant", content="a", created_at=555),
        provider_state="resume-token",
    )

    updated = get_chat_thread(db, "t1")
    assert updated is not None
    assert updated.provider_state == "resume-token"
    assert updated.updated_at == 555  # the assistant message's created_at
    assert updated.created_at == thread.created_at  # never moves


def test_append_chat_exchange_none_clears_provider_state(db: Db) -> None:
    create_chat_thread(
        db, thread_id="t1", username="alice", agent_id="coach-a", scope="report"
    )
    append_chat_exchange(
        db,
        "t1",
        ChatMessage(role="user", content="q1", created_at=100),
        ChatMessage(role="assistant", content="a1", created_at=101),
        provider_state="tok-1",
    )
    append_chat_exchange(
        db,
        "t1",
        ChatMessage(role="user", content="q2", created_at=200),
        ChatMessage(role="assistant", content="a2", created_at=201),
        provider_state=None,
    )

    updated = get_chat_thread(db, "t1")
    assert updated is not None
    assert updated.provider_state is None


def test_clear_chat_provider_state(db: Db) -> None:
    create_chat_thread(
        db, thread_id="t1", username="alice", agent_id="coach-a", scope="report"
    )
    append_chat_exchange(
        db,
        "t1",
        ChatMessage(role="user", content="q", created_at=100),
        ChatMessage(role="assistant", content="a", created_at=101),
        provider_state="tok",
    )

    clear_chat_provider_state(db, "t1")

    updated = get_chat_thread(db, "t1")
    assert updated is not None
    assert updated.provider_state is None


def test_clear_chat_provider_state_is_scoped_to_its_thread(db: Db) -> None:
    create_chat_thread(
        db, thread_id="t1", username="alice", agent_id="coach-a", scope="report"
    )
    create_chat_thread(
        db, thread_id="t2", username="alice", agent_id="coach-a", scope="report"
    )
    append_chat_exchange(
        db,
        "t1",
        ChatMessage(role="user", content="q", created_at=1),
        ChatMessage(role="assistant", content="a", created_at=2),
        provider_state="tok-1",
    )
    append_chat_exchange(
        db,
        "t2",
        ChatMessage(role="user", content="q", created_at=1),
        ChatMessage(role="assistant", content="a", created_at=2),
        provider_state="tok-2",
    )

    clear_chat_provider_state(db, "t1")

    t1 = get_chat_thread(db, "t1")
    t2 = get_chat_thread(db, "t2")
    assert t1 is not None and t1.provider_state is None
    assert t2 is not None and t2.provider_state == "tok-2"


# --- GameFilters additions (find_games tool) -------------------------------


def test_game_filters_opponent_is_case_insensitive_exact_match(db: Db) -> None:
    upsert_games(db, [make_game(id="g1", opponent="Magnus")])

    def ids(opponent: str) -> list[str]:
        return [
            g.id for g in list_games(db, "testuser", GameFilters(opponent=opponent))
        ]

    assert ids("Magnus") == ["g1"]
    assert ids("magnus") == ["g1"]
    assert ids("MAGNUS") == ["g1"]
    assert ids("magnu") == []  # exact, not substring


def test_game_filters_opening_name_like_is_case_insensitive_substring(
    db: Db,
) -> None:
    upsert_games(
        db,
        [
            make_game(id="g1", end_time=100),
            make_game(id="g2", end_time=200),
        ],
    )
    set_opening(db, "g1", Opening(eco="C60", name="Ruy Lopez: Morphy Defense", ply=3))
    set_opening(db, "g2", Opening(eco="B90", name="Sicilian Defense: Najdorf", ply=2))

    def ids(opening_name_like: str) -> set[str]:
        return {
            g.id
            for g in list_games(
                db, "testuser", GameFilters(opening_name_like=opening_name_like)
            )
        }

    assert ids("ruy") == {"g1"}
    assert ids("RUY LOPEZ") == {"g1"}
    assert ids("defense") == {"g1", "g2"}
    assert ids("najdorf") == {"g2"}
    assert ids("caro-kann") == set()


def test_game_filters_opening_name_like_escapes_wildcards(db: Db) -> None:
    """A literal '%' or '_' in the search term must not act as a
    wildcard — the input can come verbatim from a chat message."""
    upsert_games(
        db,
        [
            make_game(id="g1", end_time=100),
            make_game(id="g2", end_time=200),
        ],
    )
    set_opening(db, "g1", Opening(eco="A00", name="Weird_Opening", ply=1))
    # Differs from g1 only where g1 has a literal underscore; an
    # unescaped "_" (a single-char wildcard) would match this too.
    set_opening(db, "g2", Opening(eco="A00", name="WeirdXOpening", ply=1))

    assert [
        g.id
        for g in list_games(db, "testuser", GameFilters(opening_name_like="Weird_Open"))
    ] == ["g1"]
    # "%" alone would match everything if not escaped; it must match nothing
    assert list_games(db, "testuser", GameFilters(opening_name_like="%")) == []


def test_game_filters_since_until_window(db: Db) -> None:
    """Mirrors `list_analyzed_games`'s window semantics exactly: `since`
    inclusive, `until` exclusive."""
    upsert_games(
        db,
        [
            make_game(id="old", end_time=100),
            make_game(id="recent", end_time=200),
        ],
    )

    def ids(*, since: int | None = None, until: int | None = None) -> list[str]:
        filters = GameFilters(since=since, until=until)
        return [g.id for g in list_games(db, "testuser", filters)]

    assert ids(since=150) == ["recent"]
    assert ids(until=150) == ["old"]
    assert ids(since=200) == ["recent"]  # since is inclusive
    assert ids(until=200) == ["old"]  # until is exclusive


def test_game_filters_existing_fields_still_work_alongside_new_ones(db: Db) -> None:
    """Existing filters (result, time_class, analyzed) keep working
    unchanged now that new fields sit alongside them."""
    win = make_game(id="g-win", result="win", time_class="blitz", end_time=1)
    loss = make_game(id="g-loss", result="loss", time_class="rapid", end_time=2)
    upsert_games(db, [win, loss])

    assert [g.id for g in list_games(db, "testuser", GameFilters(result="win"))] == [
        "g-win"
    ]
    assert [
        g.id for g in list_games(db, "testuser", GameFilters(time_class="rapid"))
    ] == ["g-loss"]
    assert [g.id for g in list_games(db, "testuser", GameFilters(analyzed=False))] == [
        "g-loss",
        "g-win",
    ]
