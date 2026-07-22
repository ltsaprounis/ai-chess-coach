"""API-layer integration tests (docs/07-api.md) — stubbed ingestion."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient

import chess_coach.api.routes as routes
from chess_coach.api import create_app
from chess_coach.config import AppConfig, StorageConfig
from chess_coach.domain import Game
from chess_coach.ingestion import UnknownUserError
from chess_coach.storage import open_db, save_analysis, upsert_games
from tests.factories import make_analysis, make_game


def get(
    client: TestClient, url: str, params: dict[str, str] | None = None
) -> httpx.Response:
    return cast(httpx.Response, client.get(url, params=params))  # pyright: ignore[reportUnknownMemberType]


def post(client: TestClient, url: str) -> httpx.Response:
    return cast(httpx.Response, client.post(url))  # pyright: ignore[reportUnknownMemberType]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "api.sqlite3"


@pytest.fixture
def client(db_path: Path) -> Iterator[TestClient]:
    config = AppConfig(
        storage=StorageConfig(db_path=db_path), anthropic_api_key="sk-test"
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


def test_unknown_user_maps_to_404_envelope(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_sync(username: str, since: int | None = None) -> AsyncIterator[list[Game]]:
        raise UnknownUserError(username)

    monkeypatch.setattr(routes, "sync_games", fake_sync)

    response = post(client, "/api/players/ghost/sync")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_user"
