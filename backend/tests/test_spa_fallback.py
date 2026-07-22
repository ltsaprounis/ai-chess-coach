"""SPA fallback behavior of the app factory (docs/07-api.md)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import chess_coach.api.app as app_module
from chess_coach.api import create_app
from chess_coach.config import AppConfig, OpeningsConfig, StorageConfig
from tests.http import get

TESTDATA = Path(__file__).parent / "testdata"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>spa shell</html>")
    (dist / "assets" / "app.js").write_text("console.log('app')")
    monkeypatch.setattr(app_module, "_WEB_DIST", dist)

    config = AppConfig(
        storage=StorageConfig(db_path=tmp_path / "spa.sqlite3"),
        openings=OpeningsConfig(book_dir=TESTDATA / "minibook"),
        anthropic_api_key="sk-test",
    )
    with TestClient(create_app(config)) as test_client:
        yield test_client


def test_root_serves_the_shell(client: TestClient) -> None:
    assert "spa shell" in get(client, "/").text


def test_client_side_routes_serve_the_shell(client: TestClient) -> None:
    response = get(client, "/players/someone/dashboard")
    assert response.status_code == 200
    assert "spa shell" in response.text


def test_real_files_are_served_directly(client: TestClient) -> None:
    assert "console.log" in get(client, "/assets/app.js").text


def test_api_404s_stay_json(client: TestClient) -> None:
    response = get(client, "/api/nonexistent")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "http_404"


def test_path_traversal_is_blocked(client: TestClient) -> None:
    # Escaping the dist dir must fall back to the shell, never the file.
    response = get(client, "/..%2F..%2Fetc%2Fpasswd")
    assert "spa shell" in response.text
