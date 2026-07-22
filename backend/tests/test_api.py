"""API-layer integration tests (docs/07-api.md) — stubbed ingestion."""

from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import chess_coach.api.app as app_module
import chess_coach.api.routes as routes
from chess_coach.api import create_app
from chess_coach.config import (
    AppConfig,
    EngineConfig,
    OpeningsConfig,
    StorageConfig,
)
from chess_coach.domain import Game, GameAnalysis
from chess_coach.engine import EngineOptions, Progress, ProgressCallback
from chess_coach.ingestion import UnknownUserError
from chess_coach.storage import open_db, save_analysis, upsert_games
from tests.factories import make_analysis, make_game
from tests.http import get, post

TESTDATA = Path(__file__).parent / "testdata"


class StubPool:
    """Instant analyses with one progress event per game."""

    async def analyze_game(
        self,
        game: Game,
        opts: EngineOptions,
        on_progress: ProgressCallback | None = None,
    ) -> GameAnalysis:
        total = max(1, len(game.san_moves))
        if on_progress is not None:
            on_progress(Progress(game_id=game.id, ply=total, total_plies=total))
        return make_analysis(game_id=game.id, depth=opts.depth)

    async def close(self) -> None:
        pass


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "api.sqlite3"


@pytest.fixture
def client(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    async def fake_create_pool(bin_path: Path, workers: int) -> StubPool:
        return StubPool()

    monkeypatch.setattr(app_module, "create_pool", fake_create_pool)
    fake_bin = tmp_path / "stockfish"
    fake_bin.touch()

    config = AppConfig(
        engine=EngineConfig(bin_path=fake_bin),
        storage=StorageConfig(db_path=db_path),
        openings=OpeningsConfig(book_dir=TESTDATA / "minibook"),
        anthropic_api_key="sk-test",
    )
    with TestClient(create_app(config)) as test_client:
        yield test_client


def seed(db_path: Path, games: list[Game], analyzed: set[str] | None = None) -> None:
    db = open_db(db_path)
    upsert_games(db, games)
    for game_id in analyzed or set():
        save_analysis(db, make_analysis(game_id=game_id))
    db.close()


def test_games_list_with_filters(client: TestClient, db_path: Path) -> None:
    seed(
        db_path,
        [
            make_game(id="g-1", end_time=1, result="loss"),
            make_game(id="g-2", end_time=2, result="win"),
        ],
        analyzed={"g-2"},
    )

    listed: Any = get(client, "/api/players/TestUser/games").json()
    assert [g["id"] for g in listed] == ["g-2", "g-1"]

    wins: Any = get(
        client, "/api/players/testuser/games", params={"result": "win"}
    ).json()
    assert [g["id"] for g in wins] == ["g-2"]
    assert wins[0]["analyzed"] is True


def test_game_detail_includes_analysis(client: TestClient, db_path: Path) -> None:
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})

    detail: Any = get(client, "/api/games/g-1").json()
    assert detail["analysis"]["depth"] == 16
    assert detail["opening"] is None


def test_unknown_game_uses_error_envelope(client: TestClient) -> None:
    response = get(client, "/api/games/nope")
    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "http_404", "message": "unknown game: nope"}
    }


def test_sync_stores_games_and_reports_count(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetched = [
        make_game(id="g-new-1", end_time=10),
        make_game(id="g-new-2", end_time=20),
    ]

    def fake_sync(username: str, since: int | None = None) -> AsyncIterator[list[Game]]:
        assert username == "testuser"
        assert since is None

        async def batches() -> AsyncIterator[list[Game]]:
            yield fetched

        return batches()

    monkeypatch.setattr(routes, "sync_games", fake_sync)

    response = post(client, "/api/players/TestUser/sync")
    assert response.json() == {"games_synced": 2}

    listed: Any = get(client, "/api/players/testuser/games").json()
    assert [g["id"] for g in listed] == ["g-new-2", "g-new-1"]


SyncFn = Callable[[str, int | None], AsyncIterator[list[Game]]]


def fake_sync_yielding(*batches: list[Game]) -> SyncFn:
    def fake_sync(username: str, since: int | None = None) -> AsyncIterator[list[Game]]:
        async def generate() -> AsyncIterator[list[Game]]:
            for batch in batches:
                yield batch

        return generate()

    return fake_sync


def test_sync_classifies_new_games_and_backfills_old_ones(
    client: TestClient, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stored, unclassified game from before openings shipped.
    seed(db_path, [make_game(id="g-old", end_time=1, san_moves=["d4", "d5", "c4"])])

    ruy = make_game(
        id="g-ruy", end_time=50, san_moves=["e4", "e5", "Nf3", "Nc6", "Bb5", "a6"]
    )
    monkeypatch.setattr(routes, "sync_games", fake_sync_yielding([ruy]))
    post(client, "/api/players/testuser/sync")

    listed: Any = get(client, "/api/players/testuser/games").json()
    openings = {g["id"]: g["opening"] for g in listed}
    assert openings["g-ruy"]["eco"] == "C60"
    assert openings["g-ruy"]["name"] == "Ruy Lopez"
    assert openings["g-old"]["eco"] == "D06"  # backfilled


def test_openings_endpoint_aggregates_records(
    client: TestClient, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ruy_moves = ["e4", "e5", "Nf3", "Nc6", "Bb5"]
    seed(
        db_path,
        [
            make_game(id="r-win", end_time=1, result="win", san_moves=ruy_moves),
            make_game(id="r-loss", end_time=2, result="loss", san_moves=ruy_moves),
            make_game(
                id="q-draw", end_time=3, result="draw", san_moves=["d4", "d5", "c4"]
            ),
        ],
    )
    monkeypatch.setattr(routes, "sync_games", fake_sync_yielding())
    post(client, "/api/players/testuser/sync")  # classifies the backlog

    stats: Any = get(client, "/api/players/testuser/openings").json()
    assert [
        (s["eco"], s["games"], s["wins"], s["losses"], s["draws"]) for s in stats
    ] == [
        ("C60", 2, 1, 1, 0),
        ("D06", 1, 0, 0, 1),
    ]
    assert all(s["avg_cp_loss"] is None for s in stats)


def wait_until_analyzed(client: TestClient, username: str, expected: int) -> None:
    import time

    for _ in range(100):
        analyzed: Any = get(
            client,
            f"/api/players/{username}/games",
            params={"analyzed": "true"},
        ).json()
        if len(analyzed) == expected:
            return
        time.sleep(0.02)
    raise AssertionError(f"never reached {expected} analyzed games")


def test_analyze_runs_and_persists(client: TestClient, db_path: Path) -> None:
    seed(
        db_path,
        [make_game(id="g-1", end_time=1), make_game(id="g-2", end_time=2)],
    )

    response = post(client, "/api/players/testuser/analyze")
    assert response.status_code == 202
    assert response.json() == {"queued": 2}

    wait_until_analyzed(client, "testuser", 2)
    detail: Any = get(client, "/api/games/g-1").json()
    assert detail["analysis"]["depth"] == 16  # config default
    assert detail["analysis"]["overall_acpl"] == 2.5


def test_analyze_fills_opening_avg_cp_loss(
    client: TestClient, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ruy_moves = ["e4", "e5", "Nf3", "Nc6", "Bb5"]
    seed(db_path, [make_game(id="r-1", result="win", san_moves=ruy_moves)])
    monkeypatch.setattr(routes, "sync_games", fake_sync_yielding())
    post(client, "/api/players/testuser/sync")  # classify

    post(client, "/api/players/testuser/analyze")
    wait_until_analyzed(client, "testuser", 1)

    stats: Any = get(client, "/api/players/testuser/openings").json()
    assert stats[0]["avg_cp_loss"] == 2.5


def test_analyze_without_engine_binary_is_503(db_path: Path, tmp_path: Path) -> None:
    config = AppConfig(
        engine=EngineConfig(bin_path=tmp_path / "missing-stockfish"),
        storage=StorageConfig(db_path=db_path),
        openings=OpeningsConfig(book_dir=TESTDATA / "minibook"),
        anthropic_api_key="sk-test",
    )
    with TestClient(create_app(config)) as client:
        response = post(client, "/api/players/testuser/analyze")
    assert response.status_code == 503
    assert "make engine" in response.json()["error"]["message"]


def test_progress_stream_404s_without_a_run(client: TestClient) -> None:
    response = get(client, "/api/players/testuser/analyze/progress")
    assert response.status_code == 404


def test_unknown_user_maps_to_404_envelope(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_sync(username: str, since: int | None = None) -> AsyncIterator[list[Game]]:
        raise UnknownUserError(username)

    monkeypatch.setattr(routes, "sync_games", fake_sync)

    response = post(client, "/api/players/ghost/sync")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_user"
