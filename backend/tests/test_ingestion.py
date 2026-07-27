"""Ingestion component tests (docs/02-ingestion.md) — no live network."""

import json
from pathlib import Path

import httpx
import pytest

from chess_coach.domain import Game
from chess_coach.ingestion import UnknownUserError, get_archives, sync_games

TESTDATA = Path(__file__).parent / "testdata"

ARCHIVES_URL = "https://api.chess.com/pub/player/testuser/games/archives"
MAY_URL = "https://api.chess.com/pub/player/testuser/games/2026/05"
JUNE_URL = "https://api.chess.com/pub/player/testuser/games/2026/06"
JUNE_START = 1_780_272_000  # 2026-06-01 00:00 UTC

# A chess.com-style PGN for a custom-position game (e.g. a daily
# challenge that starts mid-position): `SetUp`/`FEN` headers fix the
# starting position instead of the standard one.
_CUSTOM_START_PGN = """[Event "Live Chess"]
[Site "Chess.com"]
[White "TestUser"]
[Black "Hikaru"]
[Result "1-0"]
[SetUp "1"]
[FEN "r1bqkb1r/pppp1ppp/2n2n2/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4"]

4. Ng5 d5 5. exd5 Na5 1-0"""


def fixture(name: str) -> object:
    return json.loads((TESTDATA / name).read_text())


def make_mock_client(requested: list[str] | None = None) -> httpx.AsyncClient:
    routes = {
        ARCHIVES_URL: fixture("archives.json"),
        MAY_URL: fixture("month_2026_05.json"),
        JUNE_URL: fixture("month_2026_06.json"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if requested is not None:
            requested.append(str(request.url))
        payload = routes.get(str(request.url))
        if payload is None:
            return httpx.Response(404, json={"message": "Not found"})
        return httpx.Response(200, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def make_single_game_client(pgn: str) -> httpx.AsyncClient:
    """A mock client whose one archive month holds a single game.

    Isolated from the shared archive fixtures (other tests assert exact
    contents against those), so a hand-built PGN — a custom starting
    position or a variant of one — can be dropped in without disturbing
    them.
    """
    month_payload = {
        "games": [
            {
                "uuid": "g-custom",
                "pgn": pgn,
                "time_control": "180",
                "time_class": "blitz",
                "rules": "chess",
                "end_time": 1_780_300_000,
                "white": {"username": "TestUser", "rating": 1510, "result": "win"},
                "black": {"username": "Hikaru", "rating": 1490, "result": "resigned"},
            }
        ]
    }
    routes = {
        ARCHIVES_URL: {"archives": [JUNE_URL]},
        JUNE_URL: month_payload,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        payload = routes.get(str(request.url))
        if payload is None:
            return httpx.Response(404, json={"message": "Not found"})
        return httpx.Response(200, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def batch_ids(
    username: str, since: int | None, client: httpx.AsyncClient
) -> list[list[str]]:
    return [
        [game.id for game in batch]
        async for batch in sync_games(username, since, client=client)
    ]


async def all_games(client: httpx.AsyncClient) -> dict[str, Game]:
    games = [
        game async for batch in sync_games("TestUser", client=client) for game in batch
    ]
    return {game.id: game for game in games}


async def test_get_archives_returns_month_urls() -> None:
    async with make_mock_client() as client:
        archives = await get_archives("TestUser", client=client)
    assert archives == [MAY_URL, JUNE_URL]


async def test_unknown_user_raises() -> None:
    async with make_mock_client() as client:
        with pytest.raises(UnknownUserError, match="ghost"):
            await get_archives("ghost", client=client)


async def test_sync_yields_one_batch_per_month_skipping_bad_games() -> None:
    async with make_mock_client() as client:
        ids = await batch_ids("TestUser", None, client)
    # Variant, unknown-result, and malformed entries are dropped.
    assert ids == [
        ["g-may-1"],
        ["g-june-timeout", "g-june-resigned", "g-june-1", "g-june-5"],
    ]


async def test_normalization_maps_colors_results_and_accuracy() -> None:
    async with make_mock_client() as client:
        games = await all_games(client)

    white_win = games["g-june-1"]
    assert (white_win.username, white_win.opponent) == ("testuser", "hikaru")
    assert (white_win.color, white_win.result) == ("white", "win")
    assert white_win.accuracy == 92.5
    assert white_win.san_moves == ["e4", "e5", "Nf3", "Nc6", "Bb5"]

    black_draw = games["g-june-5"]
    assert (black_draw.color, black_draw.result) == ("black", "draw")
    assert black_draw.accuracy is None

    assert games["g-may-1"].result == "loss"


async def test_sync_games_drops_custom_starting_position() -> None:
    # Downstream analysis always replays san_moves from chess.Board(),
    # the standard start; a game whose PGN sets a different starting
    # position via SetUp/FEN would silently mis-replay everywhere else,
    # so sync_games must drop it rather than let it through.
    async with make_single_game_client(_CUSTOM_START_PGN) as client:
        games = await all_games(client)
    assert games == {}


async def test_sync_games_drops_bare_fen_header_without_setup() -> None:
    # SetUp "1" + FEN is the conventional pairing, but a FEN header
    # alone is treated the same way rather than trusted by coincidence.
    bare_fen_pgn = _CUSTOM_START_PGN.replace('[SetUp "1"]\n', "")
    async with make_single_game_client(bare_fen_pgn) as client:
        games = await all_games(client)
    assert games == {}


async def test_sync_games_keeps_standard_starting_position() -> None:
    standard_pgn = "1. e4 e5 2. Nf3 Nc6 3. Bb5 1-0"
    async with make_single_game_client(standard_pgn) as client:
        games = await all_games(client)
    assert games["g-custom"].san_moves == ["e4", "e5", "Nf3", "Nc6", "Bb5"]


async def test_termination_keeps_the_raw_code_behind_result() -> None:
    async with make_mock_client() as client:
        games = await all_games(client)

    # win, draw, and loss all keep their raw per-player code verbatim.
    assert games["g-june-1"].termination == "win"
    assert games["g-june-5"].termination == "repetition"
    assert games["g-may-1"].termination == "checkmated"

    # The win/draw/loss collapse otherwise hides *how* a loss happened;
    # timeout and resignation both map to result="loss" but must remain
    # distinguishable via termination.
    timeout_loss = games["g-june-timeout"]
    resigned_loss = games["g-june-resigned"]
    assert timeout_loss.result == "loss"
    assert resigned_loss.result == "loss"
    assert timeout_loss.termination == "timeout"
    assert resigned_loss.termination == "resigned"
    assert timeout_loss.termination != resigned_loss.termination


async def test_since_skips_whole_months_without_fetching_them() -> None:
    requested: list[str] = []
    async with make_mock_client(requested) as client:
        ids = await batch_ids("TestUser", JUNE_START, client)
    assert ids == [["g-june-timeout", "g-june-resigned", "g-june-1", "g-june-5"]]
    assert MAY_URL not in requested


async def test_since_filters_games_inside_a_month() -> None:
    async with make_mock_client() as client:
        ids = await batch_ids("TestUser", 1_780_300_000, client)
    assert ids == [["g-june-5"]]


async def test_retries_on_429_then_succeeds() -> None:
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json=fixture("archives.json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        archives = await get_archives("TestUser", client=client)

    assert archives == [MAY_URL, JUNE_URL]
    assert attempts == 2
