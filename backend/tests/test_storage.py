"""Storage component tests (docs/03-storage.md)."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from chess_coach.domain import Opening
from chess_coach.storage import (
    Db,
    GameFilters,
    count_games_needing_analysis,
    games_missing_opening,
    games_needing_analysis,
    get_explanation,
    get_game,
    latest_game_time,
    list_analyses,
    list_analyzed_games,
    list_games,
    open_db,
    opening_stats,
    save_analysis,
    save_explanation,
    set_opening,
    upsert_games,
)
from tests.factories import make_analysis, make_game

RUY_LOPEZ = Opening(eco="C60", name="Ruy Lopez", ply=5)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Db]:
    connection = open_db(tmp_path / "test.sqlite3")
    yield connection
    connection.close()


def test_upsert_and_list_round_trip(db: Db) -> None:
    older = make_game(id="g-old", end_time=100)
    newer = make_game(id="g-new", end_time=200)
    upsert_games(db, [older, newer])

    games = list_games(db, "testuser", GameFilters())
    assert [g.id for g in games] == ["g-new", "g-old"]  # newest first
    assert games[0].analyzed is False
    assert games[0].opening is None
    assert games[0].san_moves == ["e4", "e5"]


def test_upsert_is_idempotent_and_refreshes_accuracy(db: Db) -> None:
    upsert_games(db, [make_game(accuracy=None)])
    upsert_games(db, [make_game(accuracy=91.2)])

    games = list_games(db, "testuser", GameFilters())
    assert len(games) == 1
    assert games[0].accuracy == 91.2


def test_filters(db: Db) -> None:
    win = make_game(id="g-win", result="win", time_class="blitz", end_time=1)
    loss = make_game(id="g-loss", result="loss", time_class="rapid", end_time=2)
    upsert_games(db, [win, loss])
    set_opening(db, "g-win", RUY_LOPEZ)
    save_analysis(db, make_analysis(game_id="g-loss"))

    def ids(filters: GameFilters) -> list[str]:
        return [g.id for g in list_games(db, "testuser", filters)]

    assert ids(GameFilters(result="win")) == ["g-win"]
    assert ids(GameFilters(time_class="rapid")) == ["g-loss"]
    assert ids(GameFilters(opening_eco="C60")) == ["g-win"]
    assert ids(GameFilters(analyzed=True)) == ["g-loss"]
    assert ids(GameFilters(analyzed=False)) == ["g-win"]
    assert ids(GameFilters(limit=1)) == ["g-loss"]  # newest first
    assert ids(GameFilters(limit=1, offset=1)) == ["g-win"]


def test_latest_game_time(db: Db) -> None:
    assert latest_game_time(db, "testuser") is None
    upsert_games(db, [make_game(id="a", end_time=10), make_game(id="b", end_time=99)])
    assert latest_game_time(db, "testuser") == 99
    assert latest_game_time(db, "someone-else") is None


def test_set_opening_shows_up_in_detail_and_list(db: Db) -> None:
    upsert_games(db, [make_game()])
    set_opening(db, "game-1", RUY_LOPEZ)

    detail = get_game(db, "game-1")
    assert detail is not None
    assert detail.opening == RUY_LOPEZ
    assert detail.analysis is None
    assert list_games(db, "testuser", GameFilters())[0].opening == RUY_LOPEZ


def test_get_game_returns_none_for_unknown_id(db: Db) -> None:
    assert get_game(db, "nope") is None


def test_analysis_round_trip(db: Db) -> None:
    upsert_games(db, [make_game()])
    analysis = make_analysis()
    save_analysis(db, analysis)

    detail = get_game(db, "game-1")
    assert detail is not None
    assert detail.analysis == analysis
    assert list_analyses(db, "testuser") == [analysis]


def test_games_needing_analysis_limit_takes_newest(db: Db) -> None:
    upsert_games(
        db,
        [
            make_game(id="g-1", end_time=1),
            make_game(id="g-2", end_time=2),
            make_game(id="g-3", end_time=3),
        ],
    )
    assert [g.id for g in games_needing_analysis(db, "testuser", 16, limit=2)] == [
        "g-3",
        "g-2",
    ]
    assert count_games_needing_analysis(db, "testuser", 16) == 3

    save_analysis(db, make_analysis(game_id="g-3"))
    assert count_games_needing_analysis(db, "testuser", 16) == 2


def test_games_needing_analysis_respects_depth(db: Db) -> None:
    upsert_games(db, [make_game()])
    assert [g.id for g in games_needing_analysis(db, "testuser", 16)] == ["game-1"]

    save_analysis(db, make_analysis(depth=10))
    assert [g.id for g in games_needing_analysis(db, "testuser", 16)] == ["game-1"]

    save_analysis(db, make_analysis(depth=16))
    assert games_needing_analysis(db, "testuser", 16) == []


def test_opening_stats_aggregates_records_most_played_first(db: Db) -> None:
    upsert_games(
        db,
        [
            make_game(id="r1", result="win", end_time=1),
            make_game(id="r2", result="loss", end_time=2),
            make_game(id="r3", result="draw", end_time=3),
            make_game(id="q1", result="win", end_time=4),
            make_game(id="n1", result="win", end_time=5),  # stays unclassified
        ],
    )
    for game_id in ("r1", "r2", "r3"):
        set_opening(db, game_id, RUY_LOPEZ)
    set_opening(db, "q1", Opening(eco="D06", name="Queen's Gambit", ply=3))

    stats = opening_stats(db, "testuser")
    assert [(s.eco, s.games, s.wins, s.losses, s.draws) for s in stats] == [
        ("C60", 3, 1, 1, 1),
        ("D06", 1, 1, 0, 0),
    ]
    assert all(s.avg_cp_loss is None for s in stats)
    assert all(s.analyzed_games == 0 for s in stats)


def test_opening_stats_time_window(db: Db) -> None:
    upsert_games(
        db,
        [
            make_game(id="old", result="win", end_time=100),
            make_game(id="recent", result="loss", end_time=200),
        ],
    )
    for game_id in ("old", "recent"):
        set_opening(db, game_id, RUY_LOPEZ)

    def record(
        *, since: int | None = None, until: int | None = None
    ) -> tuple[int, int, int]:
        (stat,) = opening_stats(db, "testuser", since=since, until=until)
        return stat.games, stat.wins, stat.losses

    assert record(since=150) == (1, 0, 1)  # only the recent loss
    assert record(until=150) == (1, 1, 0)  # only the old win
    assert record(since=200) == (1, 0, 1)  # since is inclusive
    assert record(until=200) == (1, 1, 0)  # until is exclusive


def test_list_analyzed_games_time_window(db: Db) -> None:
    upsert_games(
        db,
        [make_game(id="old", end_time=100), make_game(id="recent", end_time=200)],
    )
    save_analysis(db, make_analysis(game_id="old"))
    save_analysis(db, make_analysis(game_id="recent"))

    def ids(*, since: int | None = None, until: int | None = None) -> list[str]:
        return [
            g.id for g in list_analyzed_games(db, "testuser", since=since, until=until)
        ]

    assert ids() == ["recent", "old"]  # newest first
    assert ids(since=150) == ["recent"]
    assert ids(until=150) == ["old"]
    assert ids(since=200) == ["recent"]  # since is inclusive
    assert ids(until=200) == ["old"]  # until is exclusive


def test_time_class_filter(db: Db) -> None:
    upsert_games(
        db,
        [
            make_game(id="rapid", time_class="rapid"),
            make_game(id="blitz", time_class="blitz"),
        ],
    )
    save_analysis(db, make_analysis(game_id="rapid"))
    save_analysis(db, make_analysis(game_id="blitz"))
    for game_id in ("rapid", "blitz"):
        set_opening(db, game_id, RUY_LOPEZ)

    assert [g.id for g in list_analyzed_games(db, "testuser", time_class="rapid")] == [
        "rapid"
    ]
    (stat,) = opening_stats(db, "testuser", time_class="blitz")
    assert stat.games == 1
    assert stat.analyzed_games == 1


def test_games_missing_opening(db: Db) -> None:
    upsert_games(db, [make_game(id="a", end_time=1), make_game(id="b", end_time=2)])
    set_opening(db, "a", RUY_LOPEZ)
    assert [g.id for g in games_missing_opening(db, "testuser")] == ["b"]


def test_reopen_persists_data_and_migrations_are_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "persist.sqlite3"
    first = open_db(path)
    upsert_games(first, [make_game()])
    first.close()

    second = open_db(path)  # migrations must be a no-op here
    assert [g.id for g in list_games(second, "testuser", GameFilters())] == ["game-1"]
    second.close()


def test_get_explanation_misses_when_absent(db: Db) -> None:
    upsert_games(db, [make_game()])
    assert get_explanation(db, "game-1", 1, "coach-a") is None


def test_save_and_get_explanation_round_trip(db: Db) -> None:
    upsert_games(db, [make_game()])
    save_explanation(db, "game-1", 1, "coach-a", "1. e4 is a strong opening move.")

    assert (
        get_explanation(db, "game-1", 1, "coach-a") == "1. e4 is a strong opening move."
    )
    # A different ply, agent, or game is a distinct cache entry.
    assert get_explanation(db, "game-1", 2, "coach-a") is None
    assert get_explanation(db, "game-1", 1, "coach-b") is None


def test_save_explanation_overwrites_existing_text(db: Db) -> None:
    upsert_games(db, [make_game()])
    save_explanation(db, "game-1", 1, "coach-a", "first draft")
    save_explanation(db, "game-1", 1, "coach-a", "revised explanation")

    assert get_explanation(db, "game-1", 1, "coach-a") == "revised explanation"


def test_explanation_survives_close_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "explanations.sqlite3"
    first = open_db(path)
    upsert_games(first, [make_game()])
    save_explanation(first, "game-1", 3, "coach-a", "castle early for safety")
    first.close()

    second = open_db(path)  # migration 003 must be a no-op here
    assert get_explanation(second, "game-1", 3, "coach-a") == "castle early for safety"
    second.close()
