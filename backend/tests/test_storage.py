"""Storage component tests (docs/03-storage.md)."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from chess_coach.domain import Opening
from chess_coach.storage import (
    Db,
    GameFilters,
    games_needing_analysis,
    get_game,
    latest_game_time,
    list_analyses,
    list_games,
    open_db,
    save_analysis,
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


def test_games_needing_analysis_respects_depth(db: Db) -> None:
    upsert_games(db, [make_game()])
    assert [g.id for g in games_needing_analysis(db, "testuser", 16)] == ["game-1"]

    save_analysis(db, make_analysis(depth=10))
    assert [g.id for g in games_needing_analysis(db, "testuser", 16)] == ["game-1"]

    save_analysis(db, make_analysis(depth=16))
    assert games_needing_analysis(db, "testuser", 16) == []


def test_reopen_persists_data_and_migrations_are_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "persist.sqlite3"
    first = open_db(path)
    upsert_games(first, [make_game()])
    first.close()

    second = open_db(path)  # migrations must be a no-op here
    assert [g.id for g in list_games(second, "testuser", GameFilters())] == ["game-1"]
    second.close()
