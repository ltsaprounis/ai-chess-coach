"""API-layer integration tests (docs/07-api.md) — stubbed ingestion."""

import asyncio
import threading
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any, cast

import chess
import pytest
from fastapi.testclient import TestClient

import chess_coach.api.app as app_module
import chess_coach.api.routes as routes
from chess_coach.api import create_app
from chess_coach.api.runs import AnalysisRun
from chess_coach.coach import CoachProviderError, ExplainEvent, PositionAnalystFn
from chess_coach.config import (
    AppConfig,
    CoachConfig,
    EngineConfig,
    OpeningsConfig,
    StorageConfig,
)
from chess_coach.domain import (
    AnalyzedGame,
    CoachAgent,
    EvalLine,
    Game,
    GameAnalysis,
    TimeClass,
)
from chess_coach.engine import (
    EngineError,
    EngineOptions,
    LiveEval,
    Progress,
    ProgressCallback,
)
from chess_coach.ingestion import UnknownUserError
from chess_coach.storage import (
    Db,
    get_explanation,
    open_db,
    save_analysis,
    save_explanation,
    upsert_games,
)
from tests.factories import make_analysis, make_game
from tests.http import get, post

TESTDATA = Path(__file__).parent / "testdata"


class StubProvider:
    """Canned coach advice; records the prompt it was given."""

    def __init__(self, advice: str = "Practice rook endgames.") -> None:
        self.advice = advice
        self.prompts: list[str] = []
        # One entry per `complete` call, so tests can see whether that
        # call carried a working analyst (pool up) or None (pool down).
        self.complete_analysts: list[PositionAnalystFn | None] = []
        self.explain_calls = 0
        self.explain_error: CoachProviderError | None = None

    async def complete(
        self, prompt: str, analyst: PositionAnalystFn | None = None
    ) -> str:
        self.prompts.append(prompt)
        self.complete_analysts.append(analyst)
        return self.advice

    async def explain(
        self, prompt: str, analyst: PositionAnalystFn
    ) -> AsyncGenerator[ExplainEvent]:
        self.explain_calls += 1
        # Calling the analyst once proves the API layer's engine-seam
        # wiring reaches this stub, without a real engine.
        lines = await analyst(chess.STARTING_FEN)
        yield ExplainEvent(type="tool", text=f"engine: {len(lines)} line(s)")
        yield ExplainEvent(type="text", text="This move ")
        if self.explain_error is not None:
            raise self.explain_error
        yield ExplainEvent(type="text", text="loses a pawn.")


class StubPool:
    """Instant analyses with one progress event per game."""

    def __init__(self) -> None:
        self.stream_eval_calls: list[tuple[str, int, int]] = []
        self.eval_lines_calls: list[tuple[str, int, int]] = []
        self.eval_lines_error: Exception | None = None
        # When set, the live-eval stream raises it after its first
        # snapshot — an engine dying mid-search.
        self.stream_eval_error: Exception | None = None
        # When set, analyze_game blocks until the event fires, so a test
        # can hold a run open and exercise the one-run-per-player 409
        # guard. threading.Event (bridged via to_thread) because tests
        # release it from the TestClient thread, not the app's loop.
        self.analyze_release: threading.Event | None = None
        # Narrows which games analyze_release gates: empty (the default)
        # means every game blocks, matching the original all-or-nothing
        # behavior above. A test that needs one run to stay active while
        # others complete normally (e.g. the runs-registry eviction
        # sweep) lists just the game id(s) it wants held.
        self.held_game_ids: set[str] = set()

    async def analyze_game(
        self,
        game: Game,
        opts: EngineOptions,
        on_progress: ProgressCallback | None = None,
    ) -> GameAnalysis:
        if self.analyze_release is not None and (
            not self.held_game_ids or game.id in self.held_game_ids
        ):
            await asyncio.to_thread(self.analyze_release.wait)
        total = max(1, len(game.san_moves))
        if on_progress is not None:
            on_progress(Progress(game_id=game.id, ply=total, total_plies=total))
        return make_analysis(game_id=game.id, depth=opts.depth)

    def stream_eval(
        self, fen: str, depth: int, multipv: int = 1
    ) -> AsyncIterator[LiveEval]:
        chess.Board(fen)  # same eager ValueError on a bad FEN as the pool
        self.stream_eval_calls.append((fen, depth, multipv))
        return self._live_evals(depth)

    async def _live_evals(self, depth: int) -> AsyncIterator[LiveEval]:
        # The final event echoes the requested depth so tests can see
        # what the route resolved (default and clamping).
        yield LiveEval(
            lines=[
                EvalLine(multipv=1, depth=1, eval_cp=20, eval_mate=None, pv_san=["e4"])
            ]
        )
        if self.stream_eval_error is not None:
            raise self.stream_eval_error
        yield LiveEval(
            lines=[
                EvalLine(
                    multipv=1,
                    depth=depth,
                    eval_cp=35,
                    eval_mate=None,
                    pv_san=["e4", "e5"],
                )
            ]
        )

    async def eval_lines(
        self, fen: str, depth: int, multipv: int = 1
    ) -> list[EvalLine]:
        chess.Board(fen)  # same eager ValueError on a bad FEN as the pool
        self.eval_lines_calls.append((fen, depth, multipv))
        if self.eval_lines_error is not None:
            raise self.eval_lines_error
        return [
            EvalLine(
                multipv=1,
                depth=depth,
                eval_cp=-40,
                eval_mate=None,
                pv_san=["Nf3", "Nc6"],
            )
        ]

    async def close(self) -> None:
        pass


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "api.sqlite3"


@pytest.fixture
def stub_registry() -> dict[str, object]:
    """Instances created by fake_create_pool/fake_create_provider below,
    so tests can assert on stub state without reaching into ASGI app
    internals (which pyright strict can't type through `TestClient.app`).
    """
    return {}


def stub_pool(registry: dict[str, object]) -> StubPool:
    return cast(StubPool, registry["pool"])


def stub_provider(registry: dict[str, object], agent_id: str) -> StubProvider:
    return cast(StubProvider, registry[f"provider:{agent_id}"])


@pytest.fixture
def client(
    db_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_registry: dict[str, object],
) -> Iterator[TestClient]:
    async def fake_create_pool(bin_path: Path, workers: int) -> StubPool:
        pool = StubPool()
        stub_registry["pool"] = pool
        return pool

    def fake_create_provider(cfg: CoachAgent, api_key: object = None) -> StubProvider:
        # Advice names the agent so tests can see who answered.
        provider = StubProvider(advice=f"advice from {cfg.id}")
        stub_registry[f"provider:{cfg.id}"] = provider
        return provider

    monkeypatch.setattr(app_module, "create_pool", fake_create_pool)
    monkeypatch.setattr(app_module, "create_provider", fake_create_provider)
    fake_bin = tmp_path / "stockfish"
    fake_bin.touch()

    config = AppConfig(
        # analyze_limit is small so the cap is easy to exercise.
        engine=EngineConfig(bin_path=fake_bin, analyze_limit=2),
        storage=StorageConfig(db_path=db_path),
        openings=OpeningsConfig(book_dir=TESTDATA / "minibook"),
        coach=CoachConfig(
            agents=[
                CoachAgent(id="claude", label="Claude"),
                CoachAgent(id="beta", label="Beta", model="claude-sonnet-4-5"),
            ]
        ),
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


def test_games_list_row_is_the_slim_game_summary_shape(
    client: TestClient, db_path: Path
) -> None:
    """Pins the list row to `GameSummary`: fails if `pgn`/`san_moves`
    reappear on a list row, or if `first_plies` disappears."""
    seed(db_path, [make_game(id="g-1")])

    listed: Any = get(client, "/api/players/testuser/games").json()
    assert set(listed[0].keys()) == {
        "id",
        "color",
        "time_class",
        "result",
        "end_time",
        "opponent",
        "player_rating",
        "opponent_rating",
        "accuracy",
        "termination",
        "first_plies",
        "opening",
        "analyzed",
    }
    assert isinstance(listed[0]["first_plies"], list)


def test_players_endpoint_lists_saved_players(
    client: TestClient, db_path: Path
) -> None:
    seed(
        db_path,
        [
            make_game(id="a1", username="alice", end_time=10),
            make_game(id="a2", username="alice", end_time=20),
            make_game(id="b1", username="bob", end_time=15),
        ],
    )
    players: Any = get(client, "/api/players").json()
    assert [(p["username"], p["games"], p["last_played"]) for p in players] == [
        ("alice", 2, 20),
        ("bob", 1, 15),
    ]


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


def test_sync_full_passes_since_none_even_with_stored_games(
    client: TestClient, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(db_path, [make_game(id="g-1", end_time=50)])
    seen_since: list[int | None] = []

    def fake_sync(username: str, since: int | None = None) -> AsyncIterator[list[Game]]:
        seen_since.append(since)

        async def batches() -> AsyncIterator[list[Game]]:
            yield []

        return batches()

    monkeypatch.setattr(routes, "sync_games", fake_sync)

    response = post(client, "/api/players/testuser/sync?full=true")
    assert response.status_code == 200
    assert seen_since == [None]


def test_sync_without_full_passes_the_latest_stored_time(
    client: TestClient, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(db_path, [make_game(id="g-1", end_time=50)])
    seen_since: list[int | None] = []

    def fake_sync(username: str, since: int | None = None) -> AsyncIterator[list[Game]]:
        seen_since.append(since)

        async def batches() -> AsyncIterator[list[Game]]:
            yield []

        return batches()

    monkeypatch.setattr(routes, "sync_games", fake_sync)

    response = post(client, "/api/players/testuser/sync")
    assert response.status_code == 200
    assert seen_since == [50]


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
    assert all(s["analyzed_games"] == 0 for s in stats)


def wait_until_analyzed(client: TestClient, username: str, expected: int) -> None:
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


def wait_until_run_finished(client: TestClient, username: str) -> None:
    """Poll a zero-limit probe until `username`'s run flips to finished.

    A probe never mutates `runs` when there's nothing left to enqueue
    (docs/07-api.md), so this is safe to call repeatedly without
    disturbing whatever registry state a test is asserting on -- unlike
    `wait_until_analyzed`, it is synchronized on the run's own `finished`
    flag (via the same 409-vs-202 check `analyze_player` itself uses)
    rather than on the DB write that happens a step earlier, which is
    what the runs-registry eviction sweep needs: the sweep only counts a
    run as finished once this would return.
    """
    for _ in range(200):
        probe = post(client, f"/api/players/{username}/analyze", json={"limit": 0})
        if probe.status_code == 202:
            return
        time.sleep(0.02)
    raise AssertionError(f"{username}'s run never finished")


def test_analyze_runs_and_persists(client: TestClient, db_path: Path) -> None:
    seed(
        db_path,
        [make_game(id="g-1", end_time=1), make_game(id="g-2", end_time=2)],
    )

    response = post(client, "/api/players/testuser/analyze")
    assert response.status_code == 202
    assert response.json() == {"queued": 2, "remaining": 0}

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
    # avg_cp_loss is move-weighted over the player's own plies only
    # (storage's opening_stats); StubPool's canned analysis credits the
    # player's one recorded ply (white, ply 1) with zero loss, so
    # "filled in" here means "no longer None" -- a plain `== 0.0` cannot
    # tell a computed zero apart from an unfilled None collapsing to
    # zero, which is exactly the bug this assertion exists to catch.
    assert stats[0]["avg_cp_loss"] is not None
    assert stats[0]["avg_cp_loss"] == 0.0
    assert stats[0]["analyzed_games"] == 1


def test_analyze_limits_to_newest_and_reports_remaining(
    client: TestClient, db_path: Path
) -> None:
    seed(
        db_path,
        [
            make_game(id="g-old", end_time=1),
            make_game(id="g-mid", end_time=2),
            make_game(id="g-new", end_time=3),
        ],
    )

    # Explicit limit below the config cap.
    response = post(client, "/api/players/testuser/analyze", json={"limit": 1})
    assert response.json() == {"queued": 1, "remaining": 2}
    wait_until_analyzed(client, "testuser", 1)
    analyzed: Any = get(
        client, "/api/players/testuser/games", params={"analyzed": "true"}
    ).json()
    assert [g["id"] for g in analyzed] == ["g-new"]  # newest first

    # No body: config analyze_limit (2) applies to what's left.
    response = post(client, "/api/players/testuser/analyze")
    assert response.json() == {"queued": 2, "remaining": 0}
    wait_until_analyzed(client, "testuser", 3)


def test_analyze_limit_is_capped_by_config(client: TestClient, db_path: Path) -> None:
    seed(
        db_path,
        [make_game(id=f"g-{n}", end_time=n) for n in range(1, 5)],
    )

    # Asking for 999 still yields at most the configured cap of 2.
    response = post(client, "/api/players/testuser/analyze", json={"limit": 999})
    assert response.json() == {"queued": 2, "remaining": 2}


def test_analyze_scoped_request_enqueues_only_in_scope_games(
    client: TestClient, db_path: Path
) -> None:
    seed(
        db_path,
        [
            make_game(id="g-in-1", end_time=10, time_class="rapid"),
            make_game(id="g-in-2", end_time=12, time_class="rapid"),
            make_game(id="g-in-3", end_time=14, time_class="rapid"),
            make_game(id="g-before", end_time=5, time_class="rapid"),  # out of window
            make_game(id="g-blitz", end_time=13, time_class="blitz"),  # wrong class
        ],
    )

    # since inclusive, until exclusive: [10, 15) rapid -> 3 in-scope games.
    # config analyze_limit is 2 (see the `client` fixture), so one is left.
    response = post(
        client,
        "/api/players/testuser/analyze",
        json={"since": 10, "until": 15, "time_class": "rapid"},
    )
    assert response.status_code == 202
    assert response.json() == {"queued": 2, "remaining": 1}

    wait_until_analyzed(client, "testuser", 2)
    analyzed: Any = get(
        client, "/api/players/testuser/games", params={"analyzed": "true"}
    ).json()
    # Newest-first within scope: neither the out-of-window game nor the
    # wrong-time-class game was touched.
    assert [g["id"] for g in analyzed] == ["g-in-3", "g-in-2"]


def test_analyze_zero_limit_is_a_probe_that_starts_no_run(
    client: TestClient, db_path: Path
) -> None:
    seed(
        db_path,
        [make_game(id="g-1", end_time=1), make_game(id="g-2", end_time=2)],
    )

    response = post(client, "/api/players/testuser/analyze", json={"limit": 0})
    assert response.status_code == 202
    assert response.json() == {"queued": 0, "remaining": 2}

    # No run was started by the probe, so a real request right after
    # succeeds instead of 409ing against a run that was never started.
    response = post(client, "/api/players/testuser/analyze")
    assert response.status_code == 202
    assert response.json() == {"queued": 2, "remaining": 0}


def test_analyze_409s_while_a_run_is_active_including_probes(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    """The one-run-per-player guard is protocol: the backfill CLI reads
    409 as "batch still running", so it must fire for real requests AND
    for limit-0 probes while a run is active."""
    seed(
        db_path,
        [make_game(id="g-1", end_time=1), make_game(id="g-2", end_time=2)],
    )
    pool = stub_pool(stub_registry)
    pool.analyze_release = threading.Event()
    try:
        response = post(client, "/api/players/testuser/analyze", json={"limit": 1})
        assert response.status_code == 202
        assert response.json()["queued"] == 1

        # While the batch is held open, both a real request and a probe
        # hit the guard.
        assert post(client, "/api/players/testuser/analyze").status_code == 409
        probe = post(client, "/api/players/testuser/analyze", json={"limit": 0})
        assert probe.status_code == 409
    finally:
        pool.analyze_release.set()

    # Released: the run finishes and a probe soon answers 202 again,
    # reporting the game the limited batch did not cover.
    for _ in range(200):
        probe = post(client, "/api/players/testuser/analyze", json={"limit": 0})
        if probe.status_code == 202:
            break
        time.sleep(0.02)
    assert probe.status_code == 202
    assert probe.json() == {"queued": 0, "remaining": 1}


def test_analyze_negative_limit_is_rejected(client: TestClient, db_path: Path) -> None:
    """SQLite reads a negative LIMIT as unlimited, which would bypass
    the engine.analyze_limit cap — pydantic must reject it instead."""
    seed(db_path, [make_game(id="g-1", end_time=1)])
    response = post(client, "/api/players/testuser/analyze", json={"limit": -1})
    assert response.status_code == 422


def test_analyze_fully_analyzed_scope_returns_zero_and_zero(
    client: TestClient, db_path: Path
) -> None:
    seed(
        db_path,
        [make_game(id="g-1", end_time=1, time_class="rapid")],
        analyzed={"g-1"},
    )

    response = post(
        client, "/api/players/testuser/analyze", json={"time_class": "rapid"}
    )
    assert response.status_code == 202
    assert response.json() == {"queued": 0, "remaining": 0}

    # No 409-blocking run was left behind by the zero-game result.
    assert get(client, "/api/players/testuser/analyze/progress").status_code == 404


def test_analyze_game_ids_request_ignores_scope_fields(
    client: TestClient, db_path: Path
) -> None:
    seed(
        db_path,
        [
            make_game(id="g-blitz", end_time=1, time_class="blitz"),
            make_game(id="g-rapid", end_time=2, time_class="rapid"),
        ],
    )

    # time_class="rapid" would exclude g-blitz on the bulk path, but an
    # explicit game_ids list ignores the scope fields entirely.
    response = post(
        client,
        "/api/players/testuser/analyze",
        json={"game_ids": ["g-blitz"], "time_class": "rapid"},
    )
    assert response.status_code == 202
    assert response.json() == {"queued": 1, "remaining": 1}

    wait_until_analyzed(client, "testuser", 1)
    analyzed: Any = get(
        client, "/api/players/testuser/games", params={"analyzed": "true"}
    ).json()
    assert [g["id"] for g in analyzed] == ["g-blitz"]


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


async def test_shutdown_awaits_cancelled_analysis_tasks_before_closing_the_pool(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GUIDELINES.md: every asyncio.Task is awaited or tracked and
    cancelled on shutdown. A run still mid-analysis at shutdown must
    actually finish unwinding its cancellation -- not just have
    `.cancel()` called on it -- before pool.close() runs, since close()
    quits the same engine workers a task can be mid-`analyse` on
    (CODEBASE-ASSESSMENT.md finding 1). Drives the lifespan directly
    (rather than through TestClient) so the test controls exactly when
    the run task starts and observes ordering without any HTTP layer in
    between.
    """
    order: list[str] = []

    class RecordingPool:
        async def close(self) -> None:
            order.append("pool_close")

    async def fake_create_pool(bin_path: Path, workers: int) -> RecordingPool:
        return RecordingPool()

    def fake_create_provider(cfg: CoachAgent, api_key: object = None) -> StubProvider:
        return StubProvider()

    monkeypatch.setattr(app_module, "create_pool", fake_create_pool)
    monkeypatch.setattr(app_module, "create_provider", fake_create_provider)

    async def blocked_forever() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # A real analysis task has real cleanup between the
            # cancellation landing and the coroutine actually returning
            # (the engine call unwinding). Another await point here
            # stands in for that, so an implementation that cancels but
            # never awaits the task can't accidentally pass: it would
            # reach pool_close before the event loop ever gets back here.
            await asyncio.sleep(0)
            order.append("task_cancelled")
            raise

    fake_bin = tmp_path / "stockfish"
    fake_bin.touch()
    config = AppConfig(
        engine=EngineConfig(bin_path=fake_bin),
        storage=StorageConfig(db_path=db_path),
        openings=OpeningsConfig(book_dir=TESTDATA / "minibook"),
        anthropic_api_key="sk-test",
    )

    app = create_app(config)
    async with app.router.lifespan_context(app):
        run = AnalysisRun(games_total=1)
        run.task = asyncio.create_task(blocked_forever())
        cast(dict[str, AnalysisRun], app.state.runs)["testuser"] = run
        await asyncio.sleep(0)  # let the task actually start awaiting

    assert order == ["task_cancelled", "pool_close"]


def test_runs_registry_never_evicts_an_active_run(
    client: TestClient,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_registry: dict[str, object],
) -> None:
    """The eviction sweep (CODEBASE-ASSESSMENT.md finding 6) only ever
    touches finished runs: a run whose task is still going must stay
    queryable -- and keep guarding the one-run-per-player 409 -- no
    matter how many other runs finish and get swept around it."""
    monkeypatch.setattr(routes, "MAX_FINISHED_RUNS", 1)
    seed(
        db_path,
        [make_game(id="blocked-1", username="blockeduser", end_time=1)]
        + [
            make_game(id=f"g-{n}", username=f"user{n}", end_time=n) for n in range(2, 5)
        ],
    )
    pool = stub_pool(stub_registry)
    pool.analyze_release = threading.Event()
    pool.held_game_ids = {"blocked-1"}
    try:
        held = post(client, "/api/players/blockeduser/analyze")
        assert held.status_code == 202

        # Finish runs for more users than the keep=1 cap just handed to
        # the sweep, so it fires repeatedly around the still-active run.
        for n in range(2, 5):
            response = post(client, f"/api/players/user{n}/analyze")
            assert response.status_code == 202
            wait_until_run_finished(client, f"user{n}")

        # Still active: 409 (not "gone, so this starts a fresh run")
        # proves the registry entry survived every sweep above.
        assert post(client, "/api/players/blockeduser/analyze").status_code == 409
    finally:
        pool.analyze_release.set()
    wait_until_run_finished(client, "blockeduser")


def test_runs_registry_evicts_the_oldest_finished_run_past_the_cap(
    client: TestClient, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once more finished runs pile up than the cap, the sweep drops the
    oldest one first -- proving `evict_finished` is actually wired into
    `analyze_player`, not just correct in isolation (see test_runs.py for
    the unit-level coverage of the eviction rule itself)."""
    monkeypatch.setattr(routes, "MAX_FINISHED_RUNS", 2)
    seed(
        db_path,
        [make_game(id=f"g-{n}", username=f"user{n}", end_time=n) for n in range(1, 5)],
    )

    for n in range(1, 5):
        response = post(client, f"/api/players/user{n}/analyze")
        assert response.status_code == 202
        wait_until_run_finished(client, f"user{n}")

    # user1 finished first, so it's the oldest -- evicted once user4's
    # run pushed the finished count to 3 > keep=2.
    assert get(client, "/api/players/user1/analyze/progress").status_code == 404
    # The two most recently finished runs are still queryable.
    assert get(client, "/api/players/user3/analyze/progress").status_code == 200
    assert get(client, "/api/players/user4/analyze/progress").status_code == 200


def test_report_aggregates_analyzed_games(client: TestClient, db_path: Path) -> None:
    seed(
        db_path,
        [make_game(id="g-1", end_time=1), make_game(id="g-2", end_time=2)],
        analyzed={"g-1", "g-2"},
    )

    report: Any = get(client, "/api/players/TestUser/report").json()
    assert report["username"] == "testuser"
    assert report["games_analyzed"] == 2
    # overall_acpl is move-weighted over the player's own plies only
    # (coach's build_report); make_analysis's canned analysis credits
    # the player's one recorded ply per game (white, ply 1) with zero
    # loss, so this is the two games' combined average, not a per-game
    # echo of GameAnalysis.overall_acpl.
    assert report["overall_acpl"] == 0.0


def test_report_and_openings_respect_time_window(
    client: TestClient, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ruy_moves = ["e4", "e5", "Nf3", "Nc6", "Bb5"]
    seed(
        db_path,
        [
            make_game(id="old", end_time=100, result="win", san_moves=ruy_moves),
            make_game(id="recent", end_time=200, result="loss", san_moves=ruy_moves),
        ],
        analyzed={"old", "recent"},
    )
    monkeypatch.setattr(routes, "sync_games", fake_sync_yielding())
    post(client, "/api/players/testuser/sync")  # classify the openings

    full: Any = get(client, "/api/players/testuser/report").json()
    assert full["games_analyzed"] == 2

    since = {"since": "150"}  # excludes the old game (end_time 100)
    windowed: Any = get(client, "/api/players/testuser/report", params=since).json()
    assert windowed["games_analyzed"] == 1

    openings: Any = get(client, "/api/players/testuser/openings", params=since).json()
    assert [(o["eco"], o["games"], o["losses"]) for o in openings] == [("C60", 1, 1)]

    # Both seeded games are rapid, so a blitz filter empties the report.
    blitz: Any = get(
        client, "/api/players/testuser/report", params={"time_class": "blitz"}
    ).json()
    assert blitz["games_analyzed"] == 0
    # The applied filter is recorded on the report even with no games --
    # PlayerReport.time_class is the scope of the numbers, not a summary
    # of the games found.
    assert blitz["time_class"] == "blitz"


def test_report_states_coverage_over_a_window_with_unanalyzed_games(
    client: TestClient, db_path: Path
) -> None:
    """games_in_scope counts every stored game in the window, analyzed or
    not, and requested_since/requested_until echo the query -- the "N of
    M" coverage the prompt and (later) the UI rely on to never silently
    understate the analyzed span (docs/fixes-2026-07/07)."""
    seed(
        db_path,
        [
            make_game(id="before-window", end_time=50),
            make_game(id="in-window-analyzed", end_time=150),
            make_game(id="in-window-unanalyzed", end_time=180),
            make_game(id="after-window", end_time=300),
        ],
        analyzed={"in-window-analyzed"},
    )

    report: Any = get(
        client,
        "/api/players/testuser/report",
        params={"since": "100", "until": "200"},
    ).json()

    assert report["requested_since"] == 100
    assert report["requested_until"] == 200
    # Both in-window games count, analyzed or not; the two out-of-window
    # games (before/after) are excluded from the denominator.
    assert report["games_in_scope"] == 2
    assert report["games_analyzed"] == 1


def test_report_without_filters_still_states_full_history_coverage(
    client: TestClient, db_path: Path
) -> None:
    """A filter-less request still gets a denominator -- games_in_scope
    is the full stored history, not left None just because the caller
    passed no since/until/time_class."""
    seed(
        db_path,
        [make_game(id="analyzed-1"), make_game(id="unanalyzed-1", end_time=2)],
        analyzed={"analyzed-1"},
    )

    report: Any = get(client, "/api/players/testuser/report").json()

    assert report["requested_since"] is None
    assert report["requested_until"] is None
    assert report["games_in_scope"] == 2
    assert report["games_analyzed"] == 1


def test_coach_cache_miss_prompt_states_coverage_when_games_are_unanalyzed(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    """A cache-miss /coach run passes the same games_in_scope/requested_*
    through to build_report as /report does, so the prompt the provider
    receives states coverage instead of presenting the analyzed games as
    the whole story."""
    seed(
        db_path,
        [make_game(id="analyzed-1"), make_game(id="unanalyzed-1", end_time=2)],
        analyzed={"analyzed-1"},
    )

    body: Any = post(client, "/api/players/testuser/coach").json()
    assert body["cached"] is False

    provider = stub_provider(stub_registry, "claude")
    assert len(provider.prompts) == 1
    prompt = provider.prompts[0]
    assert "- Coverage: 1 of 2 games in scope is analyzed" in prompt
    assert "the other 1 game in scope is not engine-analyzed" in prompt


def test_coach_agents_lists_roster_and_default(client: TestClient) -> None:
    body: Any = get(client, "/api/coach/agents").json()
    assert body["default"] == "claude"
    # Exact match pins the exposed fields to exactly these four.
    assert body["agents"] == [
        {
            "id": "claude",
            "label": "Claude",
            "provider": "claude-agent-sdk",
            "model": "claude-opus-4-8",
        },
        {
            "id": "beta",
            "label": "Beta",
            "provider": "claude-agent-sdk",
            "model": "claude-sonnet-4-5",
        },
    ]


def test_coach_uses_default_agent_without_a_body(
    client: TestClient, db_path: Path
) -> None:
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})

    body: Any = post(client, "/api/players/testuser/coach").json()
    assert body["agent_id"] == "claude"
    assert body["advice"] == "advice from claude"
    assert "# Coaching brief -- testuser" in body["prompt"]
    assert body["cached"] is False
    assert body["games_analyzed"] == 1


def test_coach_routes_to_the_requested_agent(client: TestClient, db_path: Path) -> None:
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})

    body: Any = post(
        client, "/api/players/testuser/coach", json={"agent_id": "beta"}
    ).json()
    assert body["agent_id"] == "beta"
    assert body["advice"] == "advice from beta"


def test_coach_400s_on_unknown_agent(client: TestClient, db_path: Path) -> None:
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})

    response = post(client, "/api/players/testuser/coach", json={"agent_id": "nope"})
    assert response.status_code == 400
    assert "unknown coach agent" in response.json()["error"]["message"]


def test_coach_409s_without_analyzed_games(client: TestClient, db_path: Path) -> None:
    seed(db_path, [make_game(id="g-1")])  # stored but unanalyzed

    response = post(client, "/api/players/testuser/coach")
    assert response.status_code == 409
    assert "analyze first" in response.json()["error"]["message"]


def test_coach_caches_and_a_repeat_is_a_cache_hit(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})

    first: Any = post(client, "/api/players/testuser/coach").json()
    assert first["cached"] is False
    assert first["games_analyzed"] == 1
    provider = stub_provider(stub_registry, "claude")
    assert len(provider.prompts) == 1

    second: Any = post(client, "/api/players/testuser/coach").json()
    assert second["cached"] is True
    assert second["prompt"] == first["prompt"]
    assert second["advice"] == first["advice"]
    assert second["games_analyzed"] == 1
    assert second["generated_at"] == first["generated_at"]
    # No further provider invocation on the cache hit, and the engine
    # pool is never touched either -- the cache short-circuits before
    # the analyst wrapper is even built.
    assert len(provider.prompts) == 1
    assert stub_pool(stub_registry).eval_lines_calls == []


def test_coach_generated_at_survives_a_clock_tick(
    client: TestClient, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """generated_at must be the single clock read storage's save_report
    persists, not a second, independent time.time() read in the API
    layer -- two independent reads can straddle a second boundary and
    disagree, which a same-second test run would never catch. Here every
    time.time() call advances the clock by a full second, so any second,
    independent read would necessarily disagree with the one persisted."""
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})
    clock = [1_700_000_000.0]

    def ticking_time() -> float:
        clock[0] += 1.0
        return clock[0]

    monkeypatch.setattr(time, "time", ticking_time)

    first: Any = post(client, "/api/players/testuser/coach").json()
    assert first["cached"] is False

    second: Any = post(client, "/api/players/testuser/coach").json()
    assert second["cached"] is True
    assert second["generated_at"] == first["generated_at"]


def test_coach_refresh_bypasses_the_cache_and_regenerates(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})

    post(client, "/api/players/testuser/coach")
    provider = stub_provider(stub_registry, "claude")
    assert len(provider.prompts) == 1

    refreshed: Any = post(
        client, "/api/players/testuser/coach", json={"refresh": True}
    ).json()
    assert refreshed["cached"] is False
    assert len(provider.prompts) == 2  # refresh bypasses the cache read

    # A plain repeat is now a cache hit on the freshly-generated advice.
    repeat: Any = post(client, "/api/players/testuser/coach").json()
    assert repeat["cached"] is True
    assert len(provider.prompts) == 2


def test_coach_refresh_reinvokes_the_provider_with_the_analyst_again(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})

    post(client, "/api/players/testuser/coach")
    provider = stub_provider(stub_registry, "claude")
    assert len(provider.complete_analysts) == 1
    assert provider.complete_analysts[0] is not None

    post(client, "/api/players/testuser/coach", json={"refresh": True})
    assert len(provider.complete_analysts) == 2
    assert provider.complete_analysts[1] is not None


def test_coach_pool_present_passes_a_working_analyst_to_the_provider(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    """On a cache miss with the engine pool up, `complete` gets a real
    analyst -- and calling it reaches the stub pool's `eval_lines` with
    the injector's (config's) depth/multipv, not a caller's choice."""
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})

    response = post(client, "/api/players/testuser/coach")
    assert response.status_code == 200

    provider = stub_provider(stub_registry, "claude")
    assert len(provider.complete_analysts) == 1
    analyst = provider.complete_analysts[0]
    assert analyst is not None

    async def call_analyst() -> list[EvalLine]:
        return await analyst(chess.STARTING_FEN)

    lines = asyncio.run(call_analyst())
    assert len(lines) == 1
    pool = stub_pool(stub_registry)
    assert pool.eval_lines_calls == [(chess.STARTING_FEN, 16, 5)]


def test_coach_without_engine_pool_passes_no_analyst_and_still_succeeds(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlike analyze/eval, a missing engine is not fatal here -- the
    report still generates, degraded to the provider's single-turn path."""
    registry: dict[str, object] = {}

    def fake_create_provider(cfg: CoachAgent, api_key: object = None) -> StubProvider:
        provider = StubProvider(advice=f"advice from {cfg.id}")
        registry[f"provider:{cfg.id}"] = provider
        return provider

    monkeypatch.setattr(app_module, "create_provider", fake_create_provider)
    config = AppConfig(
        engine=EngineConfig(bin_path=tmp_path / "missing-stockfish"),
        storage=StorageConfig(db_path=db_path),
        openings=OpeningsConfig(book_dir=TESTDATA / "minibook"),
        anthropic_api_key="sk-test",
    )
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})

    with TestClient(create_app(config)) as coach_client:
        response = post(coach_client, "/api/players/testuser/coach")

    assert response.status_code == 200
    body: Any = response.json()
    assert body["advice"] == "advice from claude"
    provider = stub_provider(registry, "claude")
    assert provider.complete_analysts == [None]


def test_coach_cache_key_separates_windows_and_agents(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    """Two coach runs over different scopes are different reports, not a
    cache hit -- the window/time-class filters and the agent are all
    part of the cache key, and the all-time (no window) case is the
    sentinel most likely to collide with itself if since/until/time_class
    default to None instead of the documented 0/""."""
    seed(
        db_path,
        [
            make_game(id="g-old", end_time=100),
            make_game(id="g-new", end_time=200),
        ],
        analyzed={"g-old", "g-new"},
    )
    provider = stub_provider(stub_registry, "claude")

    all_time: Any = post(client, "/api/players/testuser/coach").json()
    assert all_time["cached"] is False
    assert all_time["games_analyzed"] == 2
    assert len(provider.prompts) == 1

    # A repeat all-time call is a cache hit -- the sentinel case.
    repeat_all_time: Any = post(client, "/api/players/testuser/coach").json()
    assert repeat_all_time["cached"] is True
    assert len(provider.prompts) == 1

    # A windowed call is a different cache key -> another provider call.
    windowed: Any = post(
        client, "/api/players/testuser/coach", json={"since": 150}
    ).json()
    assert windowed["cached"] is False
    assert windowed["games_analyzed"] == 1
    assert len(provider.prompts) == 2

    # Repeating the same window is a cache hit.
    repeat_windowed: Any = post(
        client, "/api/players/testuser/coach", json={"since": 150}
    ).json()
    assert repeat_windowed["cached"] is True
    assert len(provider.prompts) == 2

    # A different agent over the same (all-time) scope is a third key.
    other_agent: Any = post(
        client, "/api/players/testuser/coach", json={"agent_id": "beta"}
    ).json()
    assert other_agent["cached"] is False
    assert len(provider.prompts) == 2
    assert len(stub_provider(stub_registry, "beta").prompts) == 1


def test_coach_filters_reach_list_analyzed_games_and_the_prompt(
    client: TestClient, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(
        db_path,
        [
            make_game(id="g-old", end_time=100, time_class="blitz"),
            make_game(id="g-new", end_time=200, time_class="rapid"),
        ],
        analyzed={"g-old", "g-new"},
    )
    calls: list[dict[str, object]] = []
    original = routes.list_analyzed_games

    def spy(
        db: Db,
        username: str,
        *,
        since: int | None = None,
        until: int | None = None,
        time_class: TimeClass | None = None,
    ) -> list[AnalyzedGame]:
        calls.append({"since": since, "until": until, "time_class": time_class})
        return original(db, username, since=since, until=until, time_class=time_class)

    monkeypatch.setattr(routes, "list_analyzed_games", spy)

    body: Any = post(
        client,
        "/api/players/testuser/coach",
        json={"since": 150, "time_class": "rapid"},
    ).json()

    assert calls == [{"since": 150, "until": None, "time_class": "rapid"}]
    assert body["games_analyzed"] == 1
    assert "Scope: rapid only" in body["prompt"]


def test_eval_streams_eval_events_then_done(client: TestClient) -> None:
    response = get(client, "/api/eval", params={"fen": chess.STARTING_FEN})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    body = response.text
    assert "event: eval" in body
    assert '"depth":1' in body
    assert '"depth":16' in body  # engine.depth config default applied
    assert '"pv_san":["e4","e5"]' in body
    # The done event closes the stream, after the last eval.
    assert body.index("event: done") > body.rindex("event: eval")


def test_eval_clamps_depth(client: TestClient) -> None:
    response = get(
        client, "/api/eval", params={"fen": chess.STARTING_FEN, "depth": "99"}
    )
    assert '"depth":40' in response.text


def test_eval_passes_an_in_range_multipv_through(
    client: TestClient, stub_registry: dict[str, object]
) -> None:
    response = get(
        client, "/api/eval", params={"fen": chess.STARTING_FEN, "multipv": "3"}
    )
    assert response.status_code == 200
    assert stub_pool(stub_registry).stream_eval_calls[-1] == (chess.STARTING_FEN, 16, 3)


def test_eval_clamps_out_of_range_multipv(
    client: TestClient, stub_registry: dict[str, object]
) -> None:
    response = get(
        client, "/api/eval", params={"fen": chess.STARTING_FEN, "multipv": "99"}
    )
    assert response.status_code == 200
    assert stub_pool(stub_registry).stream_eval_calls[-1] == (
        chess.STARTING_FEN,
        16,
        10,
    )


def test_eval_400s_on_invalid_fen(client: TestClient) -> None:
    response = get(client, "/api/eval", params={"fen": "not a fen"})
    assert response.status_code == 400
    assert "invalid FEN" in response.json()["error"]["message"]


def test_eval_reports_mid_stream_engine_failure_as_terminal_event(
    client: TestClient, stub_registry: dict[str, object]
) -> None:
    """An engine dying mid-search must end the stream with a terminal
    `engine_error` event — not a bare connection drop, which an
    EventSource client answers by reconnecting and re-running the same
    failing search (docs/CODEBASE-SCAN-2026-07.md, finding 5)."""
    stub_pool(stub_registry).stream_eval_error = EngineError("engine died")

    response = get(client, "/api/eval", params={"fen": chess.STARTING_FEN})

    assert response.status_code == 200
    body = response.text
    assert "event: eval" in body  # the pre-crash snapshot still arrived
    assert "event: engine_error" in body
    assert "engine died" in body
    assert "event: done" not in body


def test_eval_without_engine_binary_is_503(db_path: Path, tmp_path: Path) -> None:
    config = AppConfig(
        engine=EngineConfig(bin_path=tmp_path / "missing-stockfish"),
        storage=StorageConfig(db_path=db_path),
        openings=OpeningsConfig(book_dir=TESTDATA / "minibook"),
        anthropic_api_key="sk-test",
    )
    with TestClient(create_app(config)) as client:
        response = get(client, "/api/eval", params={"fen": chess.STARTING_FEN})
    assert response.status_code == 503
    assert "make engine" in response.json()["error"]["message"]


def test_explain_cache_hit_streams_done_without_calling_the_provider(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})
    db = open_db(db_path)
    save_explanation(db, "g-1", 1, "claude", "Cached explanation text.")
    db.close()

    response = get(client, "/api/games/g-1/explain", params={"ply": "1"})
    assert response.status_code == 200
    body = response.text
    assert "event: done" in body
    assert '"text":"Cached explanation text."' in body
    assert "event: text" not in body
    assert "event: tool" not in body
    assert stub_provider(stub_registry, "claude").explain_calls == 0


def test_explain_streams_then_caches_and_a_repeat_is_a_cache_hit(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})

    response = get(client, "/api/games/g-1/explain", params={"ply": "1"})
    assert response.status_code == 200
    body = response.text
    # Events arrive in the order the stub yields them: tool, then text.
    assert body.index("event: tool") < body.index("event: text")
    assert body.index("event: text") < body.index("event: done")
    assert '"text":"This move loses a pawn."' in body  # full concatenated text

    provider = stub_provider(stub_registry, "claude")
    assert provider.explain_calls == 1

    db = open_db(db_path)
    assert get_explanation(db, "g-1", 1, "claude") == "This move loses a pawn."
    db.close()

    # A repeat request is now a cache hit: no further LLM call.
    repeat = get(client, "/api/games/g-1/explain", params={"ply": "1"})
    assert "event: text" not in repeat.text
    assert '"text":"This move loses a pawn."' in repeat.text
    assert provider.explain_calls == 1


def test_explain_refresh_bypasses_the_cache_and_regenerates(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})
    db = open_db(db_path)
    save_explanation(db, "g-1", 1, "claude", "Cached explanation text.")
    db.close()

    response = get(
        client, "/api/games/g-1/explain", params={"ply": "1", "refresh": "true"}
    )
    assert response.status_code == 200
    body = response.text
    # refresh skips the cache read, so the stub actually streams (tool/text
    # events) instead of returning a single cached `done`.
    assert body.index("event: tool") < body.index("event: text")
    assert '"text":"This move loses a pawn."' in body

    provider = stub_provider(stub_registry, "claude")
    assert provider.explain_calls == 1

    db = open_db(db_path)
    # save_explanation is an upsert: the stale cached row is now overwritten
    # by the freshly generated text.
    assert get_explanation(db, "g-1", 1, "claude") == "This move loses a pawn."
    db.close()


def test_explain_after_a_refresh_a_plain_repeat_is_a_cache_hit_on_the_new_text(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})
    db = open_db(db_path)
    save_explanation(db, "g-1", 1, "claude", "Cached explanation text.")
    db.close()

    refreshed = get(
        client, "/api/games/g-1/explain", params={"ply": "1", "refresh": "true"}
    )
    assert refreshed.status_code == 200
    provider = stub_provider(stub_registry, "claude")
    assert provider.explain_calls == 1

    # Without refresh, this is now a cache hit on the new text — no further
    # provider call.
    repeat = get(client, "/api/games/g-1/explain", params={"ply": "1"})
    assert repeat.status_code == 200
    body = repeat.text
    assert "event: text" not in body
    assert "event: tool" not in body
    assert '"text":"This move loses a pawn."' in body
    assert provider.explain_calls == 1


def test_explain_404s_on_unknown_game(client: TestClient) -> None:
    response = get(client, "/api/games/nope/explain", params={"ply": "1"})
    assert response.status_code == 404


def test_explain_409s_without_analysis(client: TestClient, db_path: Path) -> None:
    seed(db_path, [make_game(id="g-1")])  # stored but unanalyzed

    response = get(client, "/api/games/g-1/explain", params={"ply": "1"})
    assert response.status_code == 409


def test_explain_400s_on_a_ply_out_of_range(client: TestClient, db_path: Path) -> None:
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})  # a 2-ply game

    response = get(client, "/api/games/g-1/explain", params={"ply": "99"})
    assert response.status_code == 400


def test_explain_400s_on_unknown_agent(client: TestClient, db_path: Path) -> None:
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})

    response = get(
        client, "/api/games/g-1/explain", params={"ply": "1", "agent_id": "nope"}
    )
    assert response.status_code == 400


def test_explain_502s_when_the_engine_fails(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})
    stub_pool(stub_registry).eval_lines_error = EngineError("engine died")

    response = get(client, "/api/games/g-1/explain", params={"ply": "1"})
    assert response.status_code == 502
    assert stub_provider(stub_registry, "claude").explain_calls == 0


def test_explain_provider_error_mid_stream_is_an_sse_error_and_caches_nothing(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})
    stub_provider(stub_registry, "claude").explain_error = CoachProviderError(
        "engine tool failed"
    )

    response = get(client, "/api/games/g-1/explain", params={"ply": "1"})
    assert response.status_code == 200
    body = response.text
    assert "event: error" in body
    assert '"message":"engine tool failed"' in body
    assert "event: done" not in body

    db = open_db(db_path)
    assert get_explanation(db, "g-1", 1, "claude") is None
    db.close()


def test_unknown_user_maps_to_404_envelope(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_sync(username: str, since: int | None = None) -> AsyncIterator[list[Game]]:
        raise UnknownUserError(username)

    monkeypatch.setattr(routes, "sync_games", fake_sync)

    response = post(client, "/api/players/ghost/sync")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_user"


def test_games_list_rejects_negative_paging(client: TestClient) -> None:
    """SQLite reads a negative LIMIT as "unlimited" — the same reason
    AnalyzeRequest.limit is ge=0 — so negative paging 422s at the edge
    instead of returning the whole table (scan finding 10)."""
    for params in ({"limit": "-1"}, {"offset": "-1"}):
        response = get(client, "/api/players/testuser/games", params=params)
        assert response.status_code == 422
