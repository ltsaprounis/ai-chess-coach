"""API-layer integration tests (docs/07-api.md) — stubbed ingestion."""

import asyncio
import logging
import threading
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any, cast

import chess
import httpx
import pytest
from fastapi.testclient import TestClient

import chess_coach.api.app as app_module
import chess_coach.api.chat as chat_module
import chess_coach.api.routes as routes
from chess_coach.api import create_app
from chess_coach.api.runs import AnalysisRun
from chess_coach.coach import (
    PROFILE_PROMPT_VERSION,
    PROMPT_VERSION,
    ChatEvent,
    ChatToolkit,
    CoachProviderError,
    ExplainEvent,
    PositionAnalystFn,
    build_profile,
    build_report,
)
from chess_coach.config import (
    AppConfig,
    CoachConfig,
    EngineConfig,
    OpeningsConfig,
    StorageConfig,
)
from chess_coach.domain import (
    AnalyzedGame,
    ChatMessage,
    CoachAgent,
    Color,
    ComparisonGroup,
    EvalLine,
    Game,
    GameAnalysis,
    GameDetail,
    GameSearchPage,
    GameSummary,
    Opening,
    OpeningStats,
    PlayerProfile,
    RepertoireGame,
    ScanEventSpec,
    ScanOutcome,
    ScanSpec,
    TimeClass,
)
from chess_coach.engine import (
    ANALYSIS_VERSION,
    EngineError,
    EngineOptions,
    LiveEval,
    Progress,
    ProgressCallback,
)
from chess_coach.ingestion import UnknownUserError
from chess_coach.openings import OpeningBook, RepertoireNode
from chess_coach.storage import (
    Db,
    ReportKey,
    get_chat_thread,
    get_explanation,
    get_player_profile,
    list_chat_messages,
    open_db,
    save_analysis,
    save_explanation,
    save_player_profile,
    save_report,
    set_opening,
    upsert_games,
)
from tests.coach_scenario import scenario_games
from tests.factories import make_analysis, make_analyzed, make_game
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
        self.complete_toolkits: list[ChatToolkit | None] = []
        self.complete_error: CoachProviderError | None = None
        self.explain_calls = 0
        self.explain_error: CoachProviderError | None = None
        # One entry per `explain` call, so tests can inspect the prompt the
        # route built (e.g. whether a stored player-profile block opened
        # it) without reaching into the SSE body.
        self.explain_prompts: list[str] = []
        # Narration yielded *before* the engine call, as a real model
        # emits when it says what it is about to check. It must stream to
        # the panel but never reach the cached explanation
        # (docs/06-coach.md, "Providers"); None keeps the default shape,
        # where the tool event comes first and there is nothing to drop.
        self.explain_narration: str | None = None
        # --- chat ---
        # One entry per `chat` call, so tests can inspect what seed/history/
        # provider_state the route built without reaching into the SSE body.
        self.chat_calls: list[dict[str, object]] = []
        self.chat_reply = "Sure, here is more detail."
        self.chat_provider_state: str | None = "resumed-session-token"
        self.chat_error: CoachProviderError | None = None
        # Blocks the chat generator mid-stream until released -- lets a
        # test hold a reply open to exercise the one-reply-per-thread 409
        # guard, mirroring StubPool.analyze_release for the one-run-per-
        # player guard below.
        self.chat_release: threading.Event | None = None
        # Set the moment `chat` starts running, so a test can wait for the
        # first request to actually be in flight instead of racing it with
        # a fixed sleep.
        self.chat_started: threading.Event | None = None
        # A test-supplied probe run against the toolkit the route built,
        # so tests can assert on find_games/get_game scoping without a
        # real LLM in the loop.
        self.chat_toolkit_probe: Callable[[ChatToolkit], Awaitable[None]] | None = None

    async def complete(
        self,
        prompt: str,
        analyst: PositionAnalystFn | None = None,
        *,
        toolkit: ChatToolkit | None = None,
    ) -> str:
        self.prompts.append(prompt)
        self.complete_analysts.append(analyst)
        # One entry per call, so a test can assert the profile run got a
        # toolkit (agentic) while the report run got a bare analyst.
        self.complete_toolkits.append(toolkit)
        if self.complete_error is not None:
            raise self.complete_error
        return self.advice

    async def explain(
        self, prompt: str, analyst: PositionAnalystFn
    ) -> AsyncGenerator[ExplainEvent]:
        self.explain_calls += 1
        self.explain_prompts.append(prompt)
        if self.explain_narration is not None:
            yield ExplainEvent(type="text", text=self.explain_narration)
        # Calling the analyst once proves the API layer's engine-seam
        # wiring reaches this stub, without a real engine.
        lines = await analyst(chess.STARTING_FEN)
        yield ExplainEvent(type="tool", text=f"engine: {len(lines)} line(s)")
        yield ExplainEvent(type="text", text="This move ")
        if self.explain_error is not None:
            raise self.explain_error
        yield ExplainEvent(type="text", text="loses a pawn.")

    async def chat(
        self,
        *,
        system_context: str,
        history: list[ChatMessage],
        message: str,
        toolkit: ChatToolkit,
        provider_state: str | None = None,
    ) -> AsyncGenerator[ChatEvent]:
        self.chat_calls.append(
            {
                "system_context": system_context,
                "history": history,
                "message": message,
                "provider_state": provider_state,
            }
        )
        if self.chat_started is not None:
            self.chat_started.set()
        yield ChatEvent(type="tool", text="find_games: looking")
        if self.chat_toolkit_probe is not None:
            await self.chat_toolkit_probe(toolkit)
        if self.chat_release is not None:
            # Bridged via to_thread: tests release it from the TestClient
            # thread, not the app's own event loop.
            await asyncio.to_thread(self.chat_release.wait)
        yield ChatEvent(type="text", text=self.chat_reply)
        if self.chat_error is not None:
            raise self.chat_error
        yield ChatEvent(
            type="done", text=self.chat_reply, provider_state=self.chat_provider_state
        )


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
    async def fake_create_pool(
        bin_path: Path, workers: int, eval_timeout: float
    ) -> StubPool:
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


def seed(
    db_path: Path,
    games: list[Game],
    analyzed: set[str] | None = None,
    stale: set[str] | None = None,
) -> None:
    """`stale` (a subset of `analyzed`) saves those games one version
    behind `ANALYSIS_VERSION`, so a test can seed the "needs
    re-analysis because the engine moved on" scenario without a real
    engine version bump."""
    db = open_db(db_path)
    upsert_games(db, games)
    for game_id in analyzed or set():
        version = (
            ANALYSIS_VERSION - 1 if game_id in (stale or set()) else ANALYSIS_VERSION
        )
        save_analysis(db, make_analysis(game_id=game_id), version)
    db.close()


def seed_analyzed(db_path: Path, games: list[AnalyzedGame]) -> None:
    """Store `make_analyzed` games with their real (non-canned) analyses,
    for tests that need actual judgments (blunders, brilliancies) rather
    than `make_analysis`'s fixed two-move stub."""
    db = open_db(db_path)
    upsert_games(
        db,
        [
            Game.model_validate(g.model_dump(exclude={"analysis", "opening"}))
            for g in games
        ],
    )
    for g in games:
        save_analysis(db, g.analysis, ANALYSIS_VERSION)
    db.close()


def seed_profile(
    db_path: Path,
    username: str = "testuser",
    *,
    time_class: TimeClass | None = None,
    agent_id: str = "claude",
    prompt_version: str = "profile-v0",
    narrative: str = "zzz-stored-narrative-marker-zzz",
    games: list[AnalyzedGame] | None = None,
) -> PlayerProfile:
    """Persist a `player_profiles` row directly (bypassing the LLM and the
    POST route), for tests that only need a stored row already in place.
    Returns the facts snapshot it stored, narrative attached -- the same
    shape `get_player_profile` returns."""
    db = open_db(db_path)
    facts = build_profile(build_report(username, games or [], time_class=time_class))
    save_player_profile(
        db,
        username,
        time_class=time_class,
        agent_id=agent_id,
        prompt_version=prompt_version,
        facts=facts,
        narrative=narrative,
    )
    db.close()
    return facts.model_copy(update={"narrative": narrative})


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


def test_openings_tree_scopes_to_requested_color(
    client: TestClient, db_path: Path
) -> None:
    seed(
        db_path,
        [
            make_game(id="w1", color="white", san_moves=["e4", "e5"], end_time=1),
            make_game(id="w2", color="white", san_moves=["e4", "e5"], end_time=2),
            make_game(id="b1", color="black", san_moves=["e4", "e5"], end_time=3),
        ],
    )

    white_tree: Any = get(
        client, "/api/players/TestUser/openings/tree", params={"color": "white"}
    ).json()
    assert white_tree["username"] == "testuser"  # lowered, like sibling routes
    assert white_tree["color"] == "white"
    assert white_tree["games"] == 2
    assert white_tree["analyzed"] == 0
    assert white_tree["root"]["record"]["games"] == 2

    black_tree: Any = get(
        client, "/api/players/testuser/openings/tree", params={"color": "black"}
    ).json()
    assert black_tree["games"] == 1
    assert black_tree["root"]["record"]["games"] == 1


def test_openings_tree_shared_prefix_aggregates_into_one_node(
    client: TestClient, db_path: Path
) -> None:
    seed(
        db_path,
        [
            make_game(
                id="a", san_moves=["e4", "e5", "Nf3", "Nc6"], color="white", end_time=1
            ),
            make_game(
                id="b", san_moves=["e4", "e5", "Nf3", "Nf6"], color="white", end_time=2
            ),
        ],
    )

    # min_games=1 so neither one-off ply-4 branch is pruned away, letting
    # the shape of the divergence show through.
    tree: Any = get(
        client,
        "/api/players/testuser/openings/tree",
        params={"color": "white", "min_games": "1"},
    ).json()
    root = tree["root"]
    e4_node = root["children"][0]
    assert e4_node["san"] == "e4"
    assert e4_node["record"]["games"] == 2  # both games share this node
    e5_node = e4_node["children"][0]
    assert e5_node["san"] == "e5"
    assert e5_node["record"]["games"] == 2
    nf3_node = e5_node["children"][0]
    assert nf3_node["san"] == "Nf3"
    assert nf3_node["record"]["games"] == 2
    leaves = {c["san"]: c["record"]["games"] for c in nf3_node["children"]}
    assert leaves == {"Nc6": 1, "Nf6": 1}


def test_openings_tree_prunes_rare_branches_by_default(
    client: TestClient, db_path: Path
) -> None:
    seed(
        db_path,
        [
            make_game(
                id="a", san_moves=["e4", "e5", "Nf3", "Nc6"], color="white", end_time=1
            ),
            make_game(
                id="b", san_moves=["e4", "e5", "Nf3", "Nf6"], color="white", end_time=2
            ),
        ],
    )

    tree: Any = get(
        client, "/api/players/testuser/openings/tree", params={"color": "white"}
    ).json()
    assert tree["games"] == 2  # scope totals unaffected by pruning
    nf3_node = tree["root"]["children"][0]["children"][0]["children"][0]
    assert nf3_node["san"] == "Nf3"
    assert nf3_node["record"]["games"] == 2  # parent counts untouched
    assert nf3_node["children"] == []  # both 1-game branches pruned (min_games=2)


def test_openings_tree_scoping_by_window_and_time_class(
    client: TestClient, db_path: Path
) -> None:
    seed(
        db_path,
        [
            make_game(
                id="old",
                end_time=1,
                time_class="blitz",
                san_moves=["e4", "e5"],
            ),
            make_game(
                id="new",
                end_time=100,
                time_class="rapid",
                san_moves=["e4", "e5"],
            ),
        ],
    )

    since_tree: Any = get(
        client,
        "/api/players/testuser/openings/tree",
        params={"color": "white", "since": "50"},
    ).json()
    assert since_tree["games"] == 1  # only "new" (end_time=100 >= 50)

    until_tree: Any = get(
        client,
        "/api/players/testuser/openings/tree",
        params={"color": "white", "until": "50"},
    ).json()
    assert until_tree["games"] == 1  # only "old" (until is exclusive of 100)

    blitz_tree: Any = get(
        client,
        "/api/players/testuser/openings/tree",
        params={"color": "white", "time_class": "blitz"},
    ).json()
    assert blitz_tree["games"] == 1  # only "old" is blitz


def test_openings_tree_clamps_min_games_and_max_plies(
    client: TestClient, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(db_path, [make_game(id="g-1", san_moves=["e4", "e5"])])
    calls: list[dict[str, int]] = []
    real_build_repertoire = routes.build_repertoire

    def spy(
        book: OpeningBook,
        games: list[RepertoireGame],
        *,
        color: Color,
        min_games: int,
        max_plies: int,
    ) -> RepertoireNode:
        calls.append({"min_games": min_games, "max_plies": max_plies})
        return real_build_repertoire(
            book, games, color=color, min_games=min_games, max_plies=max_plies
        )

    monkeypatch.setattr(routes, "build_repertoire", spy)

    get(
        client,
        "/api/players/testuser/openings/tree",
        params={"color": "white", "min_games": "99", "max_plies": "1"},
    )
    assert calls[-1] == {"min_games": 10, "max_plies": 4}

    get(
        client,
        "/api/players/testuser/openings/tree",
        params={"color": "white", "min_games": "0", "max_plies": "1"},
    )
    assert calls[-1]["min_games"] == 1


def test_openings_tree_missing_color_is_422(client: TestClient, db_path: Path) -> None:
    seed(db_path, [make_game(id="g-1")])
    response = get(client, "/api/players/testuser/openings/tree")
    assert response.status_code == 422


def test_openings_tree_unknown_player_is_empty(client: TestClient) -> None:
    tree: Any = get(
        client, "/api/players/ghost/openings/tree", params={"color": "white"}
    ).json()
    assert tree["username"] == "ghost"
    assert tree["color"] == "white"
    assert tree["games"] == 0
    assert tree["analyzed"] == 0
    assert tree["root"]["record"] == {
        "games": 0,
        "wins": 0,
        "losses": 0,
        "draws": 0,
    }
    assert tree["root"]["children"] == []


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


def test_analyze_stale_analysis_version_counts_as_needing_reanalysis(
    client: TestClient, db_path: Path
) -> None:
    """A game saved under an older `analysis_version` still counts as
    needing analysis: the API layer threads `engine.ANALYSIS_VERSION`
    into both the enqueue and remaining-count queries, so an engine
    version bump re-queues stored games automatically (docs/07-api.md).
    A `limit: 0` probe surfaces this without spinning up a real run."""
    seed(
        db_path,
        [make_game(id="g-1", end_time=1, time_class="rapid")],
        analyzed={"g-1"},
        stale={"g-1"},
    )

    response = post(client, "/api/players/testuser/analyze", json={"limit": 0})
    assert response.status_code == 202
    assert response.json() == {"queued": 0, "remaining": 1}


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

    async def fake_create_pool(
        bin_path: Path, workers: int, eval_timeout: float
    ) -> RecordingPool:
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
    understate the analyzed span (docs/archive/fixes-2026-07/07)."""
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


def test_highlights_blunder_shows_up_with_game_id_and_ply(
    client: TestClient, db_path: Path
) -> None:
    ruy_moves = ["e4", "e5", "Nf3", "Nc6", "Bb5"]
    # Player is white: moves at ply 1 (e4), 3 (Nf3), 5 (Bb5); the third
    # loss (250, over the default 200 blunder cutoff) lands on ply 5.
    seed_analyzed(
        db_path,
        [make_analyzed("g-1", ruy_moves, losses=(0, 0, 250))],
    )

    body: Any = get(client, "/api/players/testuser/highlights").json()

    assert [b["game_id"] for b in body["blunders"]] == ["g-1"]
    assert body["blunders"][0]["ply"] == 5
    assert body["blunders"][0]["san"] == "Bb5"


def test_highlights_since_and_time_class_filters_scope_the_result(
    client: TestClient, db_path: Path
) -> None:
    ruy_moves = ["e4", "e5", "Nf3", "Nc6", "Bb5"]
    seed_analyzed(
        db_path,
        [
            make_analyzed(
                "old",
                ruy_moves,
                end_time=100,
                losses=(0, 0, 250),
                time_class="rapid",
            ),
            make_analyzed(
                "recent",
                ruy_moves,
                end_time=200,
                losses=(0, 0, 250),
                time_class="rapid",
            ),
        ],
    )

    full: Any = get(client, "/api/players/testuser/highlights").json()
    # Newest game first.
    assert [b["game_id"] for b in full["blunders"]] == ["recent", "old"]

    since = {"since": "150"}  # excludes the old game (end_time 100)
    windowed: Any = get(client, "/api/players/testuser/highlights", params=since).json()
    assert [b["game_id"] for b in windowed["blunders"]] == ["recent"]

    # Both seeded games are rapid, so a blitz filter empties both lists.
    blitz: Any = get(
        client, "/api/players/testuser/highlights", params={"time_class": "blitz"}
    ).json()
    assert blitz["blunders"] == []
    assert blitz["brilliancies"] == []


def test_highlights_unknown_player_returns_empty_lists(client: TestClient) -> None:
    """No stored games at all -- empty lists, not a 404, consistent with
    `/report` and `/openings`."""
    body: Any = get(client, "/api/players/nobody-here/highlights").json()

    assert body == {"blunders": [], "brilliancies": []}


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


def test_coach_appends_game_links_and_caches_the_post_processed_advice(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    """docs/06-coach.md "Game links": the route runs `append_game_links` on
    the provider's advice before caching (docs/07-api.md), so a `[gN]`
    citation for an offered handle resolves to a real `/games/{id}?ply=`
    link, and a cache hit serves that already-post-processed text verbatim
    rather than re-running the post-processing step on the cached path."""
    seed_analyzed(db_path, scenario_games())
    report = build_report("testuser", scenario_games())
    assert report.critical_positions  # the fixture actually has turning points
    turning_point = report.critical_positions[0]

    provider = stub_provider(stub_registry, "claude")
    provider.advice = "see [your 26...Nb6 in the June 14 game][g1] for the key moment"

    first: Any = post(client, "/api/players/testuser/coach").json()
    assert first["cached"] is False
    # The citation itself is untouched (g1 is an offered handle), and the
    # definition block is appended after it, separated by a blank line.
    assert first["advice"].startswith(provider.advice)
    tail = first["advice"][len(provider.advice) :]
    assert tail.startswith("\n\n")
    assert f"[g1]: /games/{turning_point.game_id}?ply={turning_point.ply}" in tail

    second: Any = post(client, "/api/players/testuser/coach").json()
    assert second["cached"] is True
    # Cached advice is the same post-processed string -- proof the cached
    # row already holds processed advice rather than being re-processed
    # (or double-processed) on the cache-hit read path.
    assert second["advice"] == first["advice"]


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


# --- Player profile (docs/07-api.md "Player profile"; docs/06-coach.md
# --- "Player profile" is the contract) -----------------------------------


def test_profile_get_unknown_player_returns_empty_facts_and_no_narrative(
    client: TestClient,
) -> None:
    """No stored games at all -- empty facts and no narrative, 200 like
    `/report` -- never a 404."""
    body: Any = get(client, "/api/players/nobody-here/profile").json()

    assert body["profile"]["username"] == "nobody-here"
    assert body["profile"]["games_covered"] == 0
    assert body["profile"]["narrative"] is None
    assert body["narrative"] is None


def test_profile_get_with_analyzed_games_and_no_stored_row_is_fresh_facts_only(
    client: TestClient, db_path: Path
) -> None:
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})

    body: Any = get(client, "/api/players/testuser/profile").json()

    assert body["profile"]["username"] == "testuser"
    assert body["profile"]["games_covered"] == 1
    assert body["profile"]["narrative"] is None
    assert body["narrative"] is None


def test_profile_get_attaches_the_stored_narrative_and_its_metadata(
    client: TestClient, db_path: Path
) -> None:
    """Facts are always fresh, but the narrative metadata's `games_covered`
    is the *stored* snapshot's, not the fresh figure -- generating over 1
    game and then analyzing a second must move `profile.games_covered` to
    2 while the narrative metadata stays pinned at 1, which is the "N
    generated; M now" delta the UI needs (docs/07-api.md)."""
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})

    generated: Any = post(client, "/api/players/testuser/profile").json()
    assert generated["narrative"] is not None
    assert generated["profile"]["games_covered"] == 1

    seed(db_path, [make_game(id="g-2", end_time=2)], analyzed={"g-2"})

    body: Any = get(client, "/api/players/testuser/profile").json()
    assert body["profile"]["games_covered"] == 2  # fresh: both games now
    assert body["profile"]["narrative"] == generated["profile"]["narrative"]
    assert body["narrative"] == generated["narrative"]
    assert body["narrative"]["games_covered"] == 1  # stored: pinned at generation
    assert body["narrative"]["generated_at"] == generated["narrative"]["generated_at"]


def test_profile_post_400s_on_unknown_agent(client: TestClient, db_path: Path) -> None:
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})

    response = post(client, "/api/players/testuser/profile", json={"agent_id": "nope"})
    assert response.status_code == 400
    assert "unknown coach agent" in response.json()["error"]["message"]


def test_profile_post_409s_without_analyzed_games(
    client: TestClient, db_path: Path
) -> None:
    seed(db_path, [make_game(id="g-1")])  # stored but unanalyzed

    response = post(client, "/api/players/testuser/profile")
    assert response.status_code == 409
    assert "analyze first" in response.json()["error"]["message"]


def test_profile_post_routes_to_the_requested_agent(
    client: TestClient, db_path: Path
) -> None:
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})

    body: Any = post(
        client, "/api/players/testuser/profile", json={"agent_id": "beta"}
    ).json()
    assert body["narrative"]["agent_id"] == "beta"
    assert body["profile"]["narrative"] == "advice from beta"


def test_profile_post_runs_agentically_with_the_full_toolkit(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    """docs/06-coach.md, "Narrative": the narrative run gets the
    read-only chat toolkit, so it can read the repertoire and pull games
    rather than paraphrasing the aggregates it was handed -- and the
    prompt says so, which is the whole point of the conditional clause.

    Its scope is pinned by the test below, which is the opposite of what
    this docstring claimed before d9580d7 reversed the decision.
    """
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})
    provider = stub_provider(stub_registry, "claude")

    post(client, "/api/players/testuser/profile", json={})

    assert provider.complete_toolkits[-1] is not None
    prompt = provider.prompts[-1]
    assert "Use the tools to check anything the summary rests on" in prompt
    assert "there are no tools on this run" not in prompt


def test_profile_post_scopes_the_toolkit_to_the_facts_window(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    """One document, one denominator (docs/06-coach.md, "Reading a
    comparison").

    The toolkit was unwindowed at first, on the reasoning that the
    narrative covers the control's whole history -- which confused the
    storage key with the content's scope. Live, `get_opening_stats`
    returned a 484-game London over the whole 1,925-game archive into a
    narrative whose every other figure covered 1,158, and
    `compare_groups` handed back a 968-game White split beside a facts
    block stating 576.
    """
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})
    provider = stub_provider(stub_registry, "claude")

    post(client, "/api/players/testuser/profile", json={})

    toolkit = provider.complete_toolkits[-1]
    assert toolkit is not None
    facts = get(client, "/api/players/testuser/profile", params={}).json()["profile"]
    # Through the seam, not the attribute: whatever the window turned
    # out to be, a comparison the model asks for must count the same
    # games the facts block states.
    left, right = asyncio.run(toolkit.compare_games(ComparisonGroup()))
    assert left.games + right.games == facts["games_in_scope"]


def test_profile_post_persists_and_a_subsequent_get_sees_it(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})
    provider = stub_provider(stub_registry, "claude")
    provider.advice = "You tend to misplay closed positions."

    generated: Any = post(client, "/api/players/testuser/profile").json()
    assert generated["profile"]["narrative"] == provider.advice
    assert generated["narrative"]["agent_id"] == "claude"
    assert generated["narrative"]["prompt_version"] == PROFILE_PROMPT_VERSION
    assert generated["narrative"]["games_covered"] == 1
    assert generated["narrative"]["generated_at"] > 0

    fetched: Any = get(client, "/api/players/testuser/profile").json()
    assert fetched == generated

    # The prompt is the profile-facts prompt (render_profile_prompt), not
    # /coach's coaching-brief prompt -- carries the player's own facts.
    assert len(provider.prompts) == 1
    assert "testuser" in provider.prompts[0]
    assert "# Player profile" in provider.prompts[0]
    # Never the engine analyst on this path (docs/06-coach.md, "Player
    # profile": a narrative summarizes aggregates and asserts no concrete
    # line, so there is nothing here for an engine to verify).
    assert provider.complete_analysts == [None]


def test_profile_post_502s_when_the_provider_fails(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})
    stub_provider(stub_registry, "claude").complete_error = CoachProviderError(
        "agent crashed"
    )

    response = post(client, "/api/players/testuser/profile")
    assert response.status_code == 502
    assert "agent crashed" in response.json()["error"]["message"]

    # Nothing persisted on a failed generation.
    db = open_db(db_path)
    assert get_player_profile(db, "testuser") is None
    db.close()


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
    failing search (docs/archive/codebase-scan-2026-07.md, finding 5)."""
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


def test_explain_caches_only_the_text_after_the_last_engine_call(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    """docs/06-coach.md, "Providers": text the model writes before a tool
    call is it narrating its plan, not the explanation. It still streams
    to the panel -- the student watches the coach work -- but the cached
    text, which is what every later reader sees, starts after the last
    engine call.
    """
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})
    provider = stub_provider(stub_registry, "claude")
    provider.explain_narration = "Let me check the position after the move."

    response = get(client, "/api/games/g-1/explain", params={"ply": "1"})
    assert response.status_code == 200
    body = response.text
    # Streamed live, so the panel shows the coach working...
    assert "Let me check the position after the move." in body
    # ...but the done event -- the cached text -- carries the answer alone.
    assert '"text":"This move loses a pawn."' in body

    db = open_db(db_path)
    assert get_explanation(db, "g-1", 1, "claude") == "This move loses a pawn."
    db.close()


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


def test_explain_embeds_the_stored_player_profile_when_one_exists(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    """docs/06-coach.md "Player profile", "Embedding": the explain prompt
    opens with the stored profile block (facts + narrative) when a
    `player_profiles` row exists for the game's player -- read fresh per
    call, never rebuilt."""
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})
    stored = seed_profile(db_path)

    response = get(client, "/api/games/g-1/explain", params={"ply": "1"})
    assert response.status_code == 200

    provider = stub_provider(stub_registry, "claude")
    assert len(provider.explain_prompts) == 1
    prompt = provider.explain_prompts[0]
    assert "## Student profile" in prompt
    assert stored.narrative is not None
    assert stored.narrative in prompt


def test_explain_prompt_has_no_profile_block_without_a_stored_row(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    """No stored row -> `profile=None` -> the prompt renders exactly as it
    did before the profile feature existed."""
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})

    response = get(client, "/api/games/g-1/explain", params={"ply": "1"})
    assert response.status_code == 200

    provider = stub_provider(stub_registry, "claude")
    assert len(provider.explain_prompts) == 1
    assert "## Student profile" not in provider.explain_prompts[0]


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


def test_analyze_game_ids_skips_other_players_games_and_duplicates(
    client: TestClient, db_path: Path
) -> None:
    """The game_ids path must only analyze the named player's games —
    a run registered under one username must not run another player's
    games — and a repeated id must not be analyzed twice (scan finding
    13, docs/archive/codebase-scan-2026-07.md)."""
    seed(
        db_path,
        [
            make_game(id="mine", username="testuser"),
            make_game(id="theirs", username="rival", opponent="testuser"),
        ],
    )

    response = post(
        client,
        "/api/players/testuser/analyze",
        json={"game_ids": ["mine", "mine", "theirs", "ghost"]},
    )

    assert response.status_code == 202
    assert response.json()["queued"] == 1
    wait_until_analyzed(client, "testuser", 1)
    rival_analyzed: Any = get(
        client, "/api/players/rival/games", params={"analyzed": "true"}
    ).json()
    assert rival_analyzed == []


# --- Chat (docs/07-api.md "Chat") ---------------------------------------


def create_thread(client: TestClient, username: str, **body: object) -> httpx.Response:
    return post(client, f"/api/players/{username}/chat/threads", json=body)


def delete_thread(client: TestClient, thread_id: str) -> httpx.Response:
    return cast(httpx.Response, client.delete(f"/api/chat/threads/{thread_id}"))  # pyright: ignore[reportUnknownMemberType]


# --- thread creation: validation matrix ---


def test_chat_thread_create_game_scope_without_game_id_is_400(
    client: TestClient,
) -> None:
    assert create_thread(client, "testuser", scope="game").status_code == 400


def test_chat_thread_create_game_scope_unknown_game_is_404(client: TestClient) -> None:
    response = create_thread(client, "testuser", scope="game", game_id="nope")
    assert response.status_code == 404


def test_chat_thread_create_game_scope_rejects_another_players_game(
    client: TestClient, db_path: Path
) -> None:
    """A game id from another player's perspective 404s exactly like an
    unknown one — a thread can never be created against a game outside
    {username}."""
    seed(
        db_path,
        [
            make_game(id="mine", username="testuser"),
            make_game(id="theirs", username="rival", opponent="testuser"),
        ],
    )
    response = create_thread(client, "testuser", scope="game", game_id="theirs")
    assert response.status_code == 404


def test_chat_thread_create_game_scope_ply_without_analysis_is_409(
    client: TestClient, db_path: Path
) -> None:
    seed(db_path, [make_game(id="g-1")])  # stored, unanalyzed
    response = create_thread(client, "testuser", scope="game", game_id="g-1", ply=1)
    assert response.status_code == 409


def test_chat_thread_create_game_scope_ply_out_of_range_is_400(
    client: TestClient, db_path: Path
) -> None:
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})  # a 2-ply game
    response = create_thread(client, "testuser", scope="game", game_id="g-1", ply=99)
    assert response.status_code == 400


def test_chat_thread_create_report_scope_rejects_game_id(
    client: TestClient, db_path: Path
) -> None:
    seed(db_path, [make_game(id="g-1")])
    response = create_thread(client, "testuser", scope="report", game_id="g-1")
    assert response.status_code == 400


def test_chat_thread_create_report_scope_rejects_ply(client: TestClient) -> None:
    response = create_thread(client, "testuser", scope="report", ply=1)
    assert response.status_code == 400


def test_chat_thread_create_unknown_agent_is_400(client: TestClient) -> None:
    response = create_thread(client, "testuser", scope="report", agent_id="nope")
    assert response.status_code == 400


def test_chat_thread_create_omitted_agent_uses_default(client: TestClient) -> None:
    body: Any = create_thread(client, "testuser", scope="report").json()
    assert body["agent_id"] == "claude"  # config's default_agent


def test_chat_thread_create_game_scope_success_returns_thread(
    client: TestClient, db_path: Path
) -> None:
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})
    body: Any = create_thread(
        client, "testuser", scope="game", game_id="g-1", ply=1, agent_id="beta"
    ).json()
    assert body["scope"] == "game"
    assert body["game_id"] == "g-1"
    assert body["ply"] == 1
    assert body["agent_id"] == "beta"
    assert body["since"] == 0
    assert body["until"] == 0
    assert body["time_class"] == ""
    assert body["provider_state"] is None
    assert body["created_at"] == body["updated_at"]
    assert body["id"]  # uuid4, minted by the route


def test_chat_thread_create_report_scope_success_stores_the_window(
    client: TestClient,
) -> None:
    body: Any = create_thread(
        client, "testuser", scope="report", since=100, until=200, time_class="blitz"
    ).json()
    assert body["scope"] == "report"
    assert body["game_id"] is None
    assert body["ply"] is None
    assert body["since"] == 100
    assert body["until"] == 200
    assert body["time_class"] == "blitz"


# --- list / get / delete ---


def test_chat_threads_list_most_recently_updated_first(
    client: TestClient, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})
    # A ticking clock (mirroring test_coach_generated_at_survives_a_clock_tick)
    # so the ordering assertion below cannot pass by same-second accident.
    clock = [1_700_000_000.0]

    def ticking_time() -> float:
        clock[0] += 1.0
        return clock[0]

    monkeypatch.setattr(time, "time", ticking_time)

    first: Any = create_thread(client, "testuser", scope="report").json()
    second: Any = create_thread(client, "testuser", scope="game", game_id="g-1").json()
    # Sending a message on the first thread moves its updated_at past the
    # second thread's creation time.
    post(client, f"/api/chat/threads/{first['id']}/messages", json={"text": "hi"})

    listed: Any = get(client, "/api/players/testuser/chat/threads").json()
    assert [t["id"] for t in listed] == [first["id"], second["id"]]
    assert listed[0]["title"] == "hi"
    assert listed[0]["messages"] == 2


def test_chat_thread_detail_returns_thread_and_transcript(
    client: TestClient,
) -> None:
    thread: Any = create_thread(client, "testuser", scope="report").json()
    post(client, f"/api/chat/threads/{thread['id']}/messages", json={"text": "hi"})

    detail: Any = get(client, f"/api/chat/threads/{thread['id']}").json()
    assert detail["id"] == thread["id"]
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][0]["content"] == "hi"
    assert detail["messages"][1]["content"] == "Sure, here is more detail."


def test_chat_thread_detail_404s_on_unknown_thread(client: TestClient) -> None:
    assert get(client, "/api/chat/threads/nope").status_code == 404


def test_chat_thread_delete_204_then_404(client: TestClient) -> None:
    thread: Any = create_thread(client, "testuser", scope="report").json()

    assert delete_thread(client, thread["id"]).status_code == 204
    assert get(client, f"/api/chat/threads/{thread['id']}").status_code == 404
    assert delete_thread(client, thread["id"]).status_code == 404


def test_chat_thread_delete_404s_on_unknown_thread(client: TestClient) -> None:
    assert delete_thread(client, "nope").status_code == 404


# --- send flow: end to end, error path, concurrency, the message cap ---


def test_chat_send_message_streams_events_then_persists_exchange(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})
    thread: Any = create_thread(client, "testuser", scope="game", game_id="g-1").json()

    response = post(
        client, f"/api/chat/threads/{thread['id']}/messages", json={"text": "What now?"}
    )
    assert response.status_code == 200
    body = response.text
    assert body.index("event: tool") < body.index("event: text")
    assert body.index("event: text") < body.index("event: done")
    assert '"text":"Sure, here is more detail."' in body

    provider = stub_provider(stub_registry, "claude")
    assert len(provider.chat_calls) == 1
    assert provider.chat_calls[0]["message"] == "What now?"
    assert provider.chat_calls[0]["provider_state"] is None  # a fresh thread

    db = open_db(db_path)
    persisted = list_chat_messages(db, thread["id"])
    stored_thread = get_chat_thread(db, thread["id"])
    db.close()
    assert [(m.role, m.content) for m in persisted] == [
        ("user", "What now?"),
        ("assistant", "Sure, here is more detail."),
    ]
    assert stored_thread is not None
    assert stored_thread.provider_state == "resumed-session-token"


def test_chat_send_message_second_call_resumes_with_the_stored_provider_state(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})
    thread: Any = create_thread(client, "testuser", scope="game", game_id="g-1").json()
    post(client, f"/api/chat/threads/{thread['id']}/messages", json={"text": "one"})
    post(client, f"/api/chat/threads/{thread['id']}/messages", json={"text": "two"})

    provider = stub_provider(stub_registry, "claude")
    assert provider.chat_calls[0]["provider_state"] is None
    assert provider.chat_calls[1]["provider_state"] == "resumed-session-token"
    # The stored transcript, not counting the ephemeral cached-turn prepend
    # (there is none here), is passed back as history on the second call.
    history = cast(list[ChatMessage], provider.chat_calls[1]["history"])
    assert [(m.role, m.content) for m in history] == [
        ("user", "one"),
        ("assistant", "Sure, here is more detail."),
    ]


def test_chat_send_message_provider_error_persists_nothing_and_clears_state(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})
    thread: Any = create_thread(client, "testuser", scope="game", game_id="g-1").json()
    provider = stub_provider(stub_registry, "claude")

    # First, a normal exchange establishes a provider_state, so the
    # assertion below proves the error path actually clears it rather
    # than it having simply never been set.
    post(client, f"/api/chat/threads/{thread['id']}/messages", json={"text": "hi"})
    db = open_db(db_path)
    before = get_chat_thread(db, thread["id"])
    db.close()
    assert before is not None
    assert before.provider_state == "resumed-session-token"

    provider.chat_error = CoachProviderError("agent crashed")
    response = post(
        client, f"/api/chat/threads/{thread['id']}/messages", json={"text": "again"}
    )
    assert response.status_code == 200
    body = response.text
    assert "event: error" in body
    assert '"message":"agent crashed"' in body
    assert "event: done" not in body

    db = open_db(db_path)
    persisted = list_chat_messages(db, thread["id"])
    after = get_chat_thread(db, thread["id"])
    db.close()
    assert len(persisted) == 2  # only the first exchange; nothing appended
    assert after is not None
    assert after.provider_state is None


def test_chat_send_message_409s_while_a_reply_is_already_streaming(
    client: TestClient, stub_registry: dict[str, object]
) -> None:
    thread: Any = create_thread(client, "testuser", scope="report").json()
    provider = stub_provider(stub_registry, "claude")
    provider.chat_release = threading.Event()
    provider.chat_started = threading.Event()

    results: list[httpx.Response] = []

    def send_first() -> None:
        results.append(
            post(
                client,
                f"/api/chat/threads/{thread['id']}/messages",
                json={"text": "first"},
            )
        )

    first_request = threading.Thread(target=send_first)
    first_request.start()
    try:
        # Deterministic handoff: wait for the stub to actually be running
        # (proving the route already marked the thread in-flight) rather
        # than racing a fixed sleep against the background thread.
        assert provider.chat_started.wait(timeout=5)
        second = post(
            client,
            f"/api/chat/threads/{thread['id']}/messages",
            json={"text": "second"},
        )
        assert second.status_code == 409
        assert "already streaming" in second.json()["error"]["message"]
    finally:
        provider.chat_release.set()
        first_request.join(timeout=5)

    assert results[0].status_code == 200


def test_chat_send_message_409s_at_the_message_cap(client: TestClient) -> None:
    thread: Any = create_thread(client, "testuser", scope="report").json()
    for i in range(20):  # 20 exchanges = 40 stored messages = the cap
        response = post(
            client,
            f"/api/chat/threads/{thread['id']}/messages",
            json={"text": f"message {i}"},
        )
        assert response.status_code == 200

    response = post(
        client, f"/api/chat/threads/{thread['id']}/messages", json={"text": "one more"}
    )
    assert response.status_code == 409
    assert "message cap" in response.json()["error"]["message"]


def test_chat_send_message_404s_on_unknown_thread(client: TestClient) -> None:
    response = post(client, "/api/chat/threads/nope/messages", json={"text": "hi"})
    assert response.status_code == 404


def test_chat_send_message_400s_on_blank_text(client: TestClient) -> None:
    thread: Any = create_thread(client, "testuser", scope="report").json()
    response = post(
        client, f"/api/chat/threads/{thread['id']}/messages", json={"text": "   "}
    )
    assert response.status_code == 400


# --- seeds and history ---


def test_chat_send_message_game_scope_with_ply_seeds_eval_lines(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})
    thread: Any = create_thread(
        client, "testuser", scope="game", game_id="g-1", ply=1
    ).json()

    post(client, f"/api/chat/threads/{thread['id']}/messages", json={"text": "why?"})

    assert len(stub_pool(stub_registry).eval_lines_calls) == 1  # seeded once

    provider = stub_provider(stub_registry, "claude")
    system_context = cast(str, provider.chat_calls[0]["system_context"])
    assert "## Candidate lines" in system_context
    assert "Engine analysis is available" in system_context


def test_chat_send_message_engine_error_seeding_eval_lines_is_502(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})
    thread: Any = create_thread(
        client, "testuser", scope="game", game_id="g-1", ply=1
    ).json()
    stub_pool(stub_registry).eval_lines_error = EngineError("engine died")

    response = post(
        client, f"/api/chat/threads/{thread['id']}/messages", json={"text": "why?"}
    )
    assert response.status_code == 502
    assert stub_provider(stub_registry, "claude").chat_calls == []


def test_chat_send_message_without_engine_pool_passes_engine_unavailable(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry: dict[str, object] = {}

    def fake_create_provider(cfg: CoachAgent, api_key: object = None) -> StubProvider:
        provider = StubProvider(advice=f"advice from {cfg.id}")
        registry[f"provider:{cfg.id}"] = provider
        return provider

    monkeypatch.setattr(app_module, "create_provider", fake_create_provider)
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})
    config = AppConfig(
        engine=EngineConfig(bin_path=tmp_path / "missing-stockfish"),
        storage=StorageConfig(db_path=db_path),
        openings=OpeningsConfig(book_dir=TESTDATA / "minibook"),
        anthropic_api_key="sk-test",
    )
    with TestClient(create_app(config)) as no_pool_client:
        thread: Any = create_thread(
            no_pool_client, "testuser", scope="game", game_id="g-1", ply=1
        ).json()
        post(
            no_pool_client,
            f"/api/chat/threads/{thread['id']}/messages",
            json={"text": "why?"},
        )
    provider = cast(StubProvider, registry["provider:claude"])
    system_context = cast(str, provider.chat_calls[0]["system_context"])
    assert "Engine analysis is not available" in system_context
    assert "## Candidate lines" not in system_context


def test_chat_send_message_report_scope_seeds_report_context(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    seed_analyzed(
        db_path,
        [make_analyzed("g-1", ["e4", "e5", "Nf3", "Nc6"], losses=[0, 40])],
    )
    thread: Any = create_thread(client, "testuser", scope="report").json()

    post(client, f"/api/chat/threads/{thread['id']}/messages", json={"text": "so?"})

    provider = stub_provider(stub_registry, "claude")
    system_context = cast(str, provider.chat_calls[0]["system_context"])
    assert "testuser" in system_context
    assert "Engine analysis is available" in system_context


def test_chat_send_message_game_scope_embeds_the_stored_player_profile(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    """docs/06-coach.md "Player profile", "Embedding": the game-scope seed
    opens with the stored profile block exactly as the explain prompt
    does, read fresh (stored row only) per message."""
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})
    stored = seed_profile(db_path)
    thread: Any = create_thread(client, "testuser", scope="game", game_id="g-1").json()

    post(client, f"/api/chat/threads/{thread['id']}/messages", json={"text": "hi"})

    provider = stub_provider(stub_registry, "claude")
    system_context = cast(str, provider.chat_calls[0]["system_context"])
    assert "## Student profile" in system_context
    assert stored.narrative is not None
    assert stored.narrative in system_context


def test_chat_send_message_report_scope_does_not_embed_the_player_profile(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    """The report seed never embeds the profile block -- the report is the
    profile's own source (docs/06-coach.md, "Player profile")."""
    seed_analyzed(
        db_path,
        [make_analyzed("g-1", ["e4", "e5", "Nf3", "Nc6"], losses=[0, 40])],
    )
    seed_profile(db_path)
    thread: Any = create_thread(client, "testuser", scope="report").json()

    post(client, f"/api/chat/threads/{thread['id']}/messages", json={"text": "so?"})

    provider = stub_provider(stub_registry, "claude")
    system_context = cast(str, provider.chat_calls[0]["system_context"])
    assert "## Student profile" not in system_context


def test_chat_send_message_prepends_cached_explanation_as_first_history_turn(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    seed(db_path, [make_game(id="g-1")], analyzed={"g-1"})
    db = open_db(db_path)
    save_explanation(db, "g-1", 1, "claude", "Cached explanation text.")
    db.close()
    thread: Any = create_thread(
        client, "testuser", scope="game", game_id="g-1", ply=1
    ).json()

    post(client, f"/api/chat/threads/{thread['id']}/messages", json={"text": "why?"})

    provider = stub_provider(stub_registry, "claude")
    history = cast(list[ChatMessage], provider.chat_calls[0]["history"])
    assert history[0].role == "assistant"
    assert history[0].content == "Cached explanation text."
    assert history[0].created_at == thread["created_at"]


def test_chat_send_message_prepends_cached_advice_as_first_history_turn(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    thread: Any = create_thread(client, "testuser", scope="report").json()
    db = open_db(db_path)
    key = ReportKey(
        username="testuser",
        agent_id="claude",
        prompt_version=PROMPT_VERSION,
        since=thread["since"],
        until=thread["until"],
        time_class=thread["time_class"],
    )
    save_report(db, key, "prompt text", "Cached advice text.", games_analyzed=5)
    db.close()

    post(client, f"/api/chat/threads/{thread['id']}/messages", json={"text": "so?"})

    provider = stub_provider(stub_registry, "claude")
    history = cast(list[ChatMessage], provider.chat_calls[0]["history"])
    assert history[0].role == "assistant"
    assert history[0].content == "Cached advice text."
    assert history[0].created_at == thread["created_at"]


# --- toolkit: cross-player guard, find_games/opening_stats scoping ---


def test_chat_toolkit_get_game_cross_player_guard(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    seed(
        db_path,
        [
            make_game(id="mine", username="testuser"),
            make_game(id="theirs", username="rival", opponent="testuser"),
        ],
    )
    thread: Any = create_thread(client, "testuser", scope="game", game_id="mine").json()
    provider = stub_provider(stub_registry, "claude")
    probed: list[GameDetail | None] = []

    async def probe(toolkit: ChatToolkit) -> None:
        probed.append(await toolkit.get_game("theirs"))
        probed.append(await toolkit.get_game("mine"))

    provider.chat_toolkit_probe = probe
    post(client, f"/api/chat/threads/{thread['id']}/messages", json={"text": "hi"})

    assert probed[0] is None  # another player's game -- refused
    assert probed[1] is not None
    assert probed[1].id == "mine"


def test_chat_toolkit_find_games_scoped_to_thread_username(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    seed(
        db_path,
        [
            make_game(id="mine-1", username="testuser", opponent="rival", result="win"),
            make_game(
                id="mine-2", username="testuser", opponent="hikaru", result="loss"
            ),
            make_game(id="theirs", username="rival", opponent="testuser"),
        ],
    )
    thread: Any = create_thread(client, "testuser", scope="report").json()
    provider = stub_provider(stub_registry, "claude")
    found: list[GameSummary] = []

    async def probe(toolkit: ChatToolkit) -> None:
        found.extend((await toolkit.find_games(opponent="rival")).games)

    provider.chat_toolkit_probe = probe
    post(client, f"/api/chat/threads/{thread['id']}/messages", json={"text": "hi"})

    assert [g.id for g in found] == ["mine-1"]


def test_chat_toolkit_find_games_limit_is_capped(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    seed(
        db_path,
        [make_game(id=f"g-{i}", username="testuser", end_time=i) for i in range(30)],
    )
    thread: Any = create_thread(client, "testuser", scope="report").json()
    provider = stub_provider(stub_registry, "claude")
    found: list[GameSummary] = []

    async def probe(toolkit: ChatToolkit) -> None:
        found.extend((await toolkit.find_games(limit=9999)).games)

    provider.chat_toolkit_probe = probe
    post(client, f"/api/chat/threads/{thread['id']}/messages", json={"text": "hi"})

    assert 0 < len(found) < 30  # capped well below the model's own ask


def test_chat_toolkit_find_games_total_and_offset_paging(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    """`total` counts beyond the page (docs/06-coach.md, "Chat"), and
    `offset` walks it: two pages of the same filters, newest first,
    together cover the whole match with no overlap."""
    seed(
        db_path,
        [make_game(id=f"g-{i}", username="testuser", end_time=i) for i in range(7)],
    )
    thread: Any = create_thread(client, "testuser", scope="report").json()
    provider = stub_provider(stub_registry, "claude")
    pages: list[GameSearchPage] = []

    async def probe(toolkit: ChatToolkit) -> None:
        pages.append(await toolkit.find_games(limit=3, offset=0))
        pages.append(await toolkit.find_games(limit=3, offset=3))

    provider.chat_toolkit_probe = probe
    post(client, f"/api/chat/threads/{thread['id']}/messages", json={"text": "hi"})

    first, second = pages
    assert first.total == 7
    assert second.total == 7
    assert first.offset == 0
    assert second.offset == 3
    assert [g.id for g in first.games] == ["g-6", "g-5", "g-4"]
    assert [g.id for g in second.games] == ["g-3", "g-2", "g-1"]


def test_chat_toolkit_opening_stats_uses_the_thread_window(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    old_opening = Opening(eco="C20", name="King's Pawn Game", ply=1)
    new_opening = Opening(eco="D00", name="Queen's Pawn Game", ply=1)
    seed_analyzed(
        db_path,
        [
            make_analyzed("g-old", ["e4", "e5"], end_time=100, opening=old_opening),
            make_analyzed("g-new", ["d4", "d5"], end_time=500, opening=new_opening),
        ],
    )
    # seed_analyzed stores games + analyses only; opening_stats reads from
    # storage's own classified-opening columns, set separately here (the
    # sync route's job in production, via classify_backlog).
    db = open_db(db_path)
    set_opening(db, "g-old", old_opening)
    set_opening(db, "g-new", new_opening)
    db.close()

    thread: Any = create_thread(client, "testuser", scope="report", since=200).json()
    provider = stub_provider(stub_registry, "claude")
    found: list[OpeningStats] = []

    async def probe(toolkit: ChatToolkit) -> None:
        found.extend(await toolkit.opening_stats())

    provider.chat_toolkit_probe = probe
    post(client, f"/api/chat/threads/{thread['id']}/messages", json={"text": "hi"})

    assert [o.eco for o in found] == ["D00"]


# --- toolkit: scan_games denominators, caps, cross-player guard ----------

# A short real game ending in White's kingside castle -- `castled` is a
# moves-only event (docs/06-coach.md, "Chat"), so it needs no stored evals
# and matches deterministically regardless of whether a game is analyzed.
_CASTLE_MOVES = ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5", "O-O"]


def test_chat_toolkit_scan_games_moves_only_denominators(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    """A moves-only spec scans analyzed and unanalyzed games alike: the
    unanalyzed ones count toward `unverified_scanned`, never
    `skipped_unanalyzed` (docs/06-coach.md, "Chat")."""
    games = [
        make_game(id=f"g-{i}", username="testuser", san_moves=_CASTLE_MOVES, end_time=i)
        for i in range(5)
    ]
    seed(db_path, games, analyzed={"g-0", "g-1", "g-2"})
    thread: Any = create_thread(client, "testuser", scope="report").json()
    provider = stub_provider(stub_registry, "claude")
    outcomes: list[ScanOutcome] = []

    async def probe(toolkit: ChatToolkit) -> None:
        spec = ScanSpec(match=[ScanEventSpec(event="castled")])
        outcomes.append(await toolkit.scan_games(spec))

    provider.chat_toolkit_probe = probe
    post(client, f"/api/chat/threads/{thread['id']}/messages", json={"text": "hi"})

    [outcome] = outcomes
    assert outcome.eligible == 5
    assert outcome.scanned == 5
    assert outcome.skipped_unanalyzed == 0
    assert outcome.unverified_scanned == 2  # g-3, g-4: not in `analyzed`
    assert outcome.truncated is False
    assert len(outcome.matches) == 5  # every game castles at ply 7


def test_chat_toolkit_scan_games_eval_reading_denominators(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    """An eval-reading spec (`comeback`) restricts the candidate fetch to
    analyzed games: the unanalyzed ones are excluded from `scanned` and
    counted as `skipped_unanalyzed` instead, and `unverified_scanned` is
    always 0 there since nothing unverified was fetched at all."""
    games = [
        make_game(id=f"g-{i}", username="testuser", san_moves=_CASTLE_MOVES, end_time=i)
        for i in range(5)
    ]
    seed(db_path, games, analyzed={"g-0", "g-1", "g-2"})
    thread: Any = create_thread(client, "testuser", scope="report").json()
    provider = stub_provider(stub_registry, "claude")
    outcomes: list[ScanOutcome] = []

    async def probe(toolkit: ChatToolkit) -> None:
        spec = ScanSpec(match=[ScanEventSpec(event="comeback")])
        outcomes.append(await toolkit.scan_games(spec))

    provider.chat_toolkit_probe = probe
    post(client, f"/api/chat/threads/{thread['id']}/messages", json={"text": "hi"})

    [outcome] = outcomes
    assert outcome.eligible == 5
    assert outcome.scanned == 3  # only the analyzed candidates were fetched
    assert outcome.skipped_unanalyzed == 2
    assert outcome.unverified_scanned == 0


def test_chat_toolkit_scan_games_truncated_flips_past_the_candidate_cap(
    client: TestClient,
    db_path: Path,
    stub_registry: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`truncated` tells the model the candidate cap actually cut the
    fetch short of `eligible` -- shrink the cap so a small fixture set
    can exercise it without seeding hundreds of games."""
    monkeypatch.setattr(chat_module, "_SCAN_CANDIDATE_CAP", 2)
    games = [make_game(id=f"g-{i}", username="testuser", end_time=i) for i in range(5)]
    seed(db_path, games)
    thread: Any = create_thread(client, "testuser", scope="report").json()
    provider = stub_provider(stub_registry, "claude")
    outcomes: list[ScanOutcome] = []

    async def probe(toolkit: ChatToolkit) -> None:
        spec = ScanSpec(match=[ScanEventSpec(event="castled")])
        outcomes.append(await toolkit.scan_games(spec))

    provider.chat_toolkit_probe = probe
    post(client, f"/api/chat/threads/{thread['id']}/messages", json={"text": "hi"})

    [outcome] = outcomes
    assert outcome.eligible == 5
    assert outcome.scanned == 2  # the shrunk cap, not the true eligible count
    assert outcome.truncated is True


def test_chat_toolkit_scan_games_not_truncated_under_the_cap(
    client: TestClient,
    db_path: Path,
    stub_registry: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(chat_module, "_SCAN_CANDIDATE_CAP", 5)
    games = [make_game(id=f"g-{i}", username="testuser", end_time=i) for i in range(5)]
    seed(db_path, games)
    thread: Any = create_thread(client, "testuser", scope="report").json()
    provider = stub_provider(stub_registry, "claude")
    outcomes: list[ScanOutcome] = []

    async def probe(toolkit: ChatToolkit) -> None:
        spec = ScanSpec(match=[ScanEventSpec(event="castled")])
        outcomes.append(await toolkit.scan_games(spec))

    provider.chat_toolkit_probe = probe
    post(client, f"/api/chat/threads/{thread['id']}/messages", json={"text": "hi"})

    [outcome] = outcomes
    assert outcome.eligible == 5
    assert outcome.scanned == 5
    assert outcome.truncated is False


def test_chat_toolkit_scan_games_match_limit_clamps_to_server_cap(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    """The model's own `limit` is capped server-side the same way
    `find_games`' is, independent of how many games actually match."""
    games = [
        make_game(id=f"g-{i}", username="testuser", san_moves=_CASTLE_MOVES, end_time=i)
        for i in range(30)
    ]
    seed(db_path, games)
    thread: Any = create_thread(client, "testuser", scope="report").json()
    provider = stub_provider(stub_registry, "claude")
    outcomes: list[ScanOutcome] = []

    async def probe(toolkit: ChatToolkit) -> None:
        spec = ScanSpec(match=[ScanEventSpec(event="castled")])
        outcomes.append(await toolkit.scan_games(spec, limit=9999))

    provider.chat_toolkit_probe = probe
    post(client, f"/api/chat/threads/{thread['id']}/messages", json={"text": "hi"})

    [outcome] = outcomes
    assert outcome.scanned == 30
    assert len(outcome.matches) == 25  # _SCAN_MATCH_CAP, not the model's ask


def test_chat_toolkit_scan_games_cross_player_guard(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    """A thread scoped to one player never scans another's games -- held
    by construction (`scan_candidates`/`game_record` take the toolkit's
    own username), unlike `get_game`'s explicit id check."""
    seed(
        db_path,
        [
            make_game(id="mine", username="testuser", san_moves=_CASTLE_MOVES),
            make_game(
                id="theirs",
                username="rival",
                opponent="testuser",
                san_moves=_CASTLE_MOVES,
            ),
        ],
    )
    thread: Any = create_thread(client, "testuser", scope="report").json()
    provider = stub_provider(stub_registry, "claude")
    outcomes: list[ScanOutcome] = []

    async def probe(toolkit: ChatToolkit) -> None:
        spec = ScanSpec(match=[ScanEventSpec(event="castled")])
        outcomes.append(await toolkit.scan_games(spec))

    provider.chat_toolkit_probe = probe
    post(client, f"/api/chat/threads/{thread['id']}/messages", json={"text": "hi"})

    [outcome] = outcomes
    assert outcome.eligible == 1
    assert [m.game.id for m in outcome.matches] == ["mine"]


def test_chat_toolkit_scan_games_logs_wall_time(
    client: TestClient,
    db_path: Path,
    stub_registry: dict[str, object],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The design doc gates a future cache decision on this number
    (docs/archive/coach-game-search.md), so it must actually
    be emitted."""
    seed(db_path, [make_game(id="g-1", username="testuser", san_moves=_CASTLE_MOVES)])
    thread: Any = create_thread(client, "testuser", scope="report").json()
    provider = stub_provider(stub_registry, "claude")

    async def probe(toolkit: ChatToolkit) -> None:
        spec = ScanSpec(match=[ScanEventSpec(event="castled")])
        await toolkit.scan_games(spec)

    provider.chat_toolkit_probe = probe
    with caplog.at_level(logging.INFO, logger="chess_coach.api.chat"):
        post(client, f"/api/chat/threads/{thread['id']}/messages", json={"text": "hi"})

    assert any("scan_games" in record.message for record in caplog.records)


# --- profile scoping (docs/07-api.md, "Player profile") ------------------


def test_profile_get_scopes_facts_by_time_class(
    client: TestClient, db_path: Path
) -> None:
    seed(
        db_path,
        [
            make_game(id="g-r1", time_class="rapid", end_time=1),
            make_game(id="g-r2", time_class="rapid", end_time=2),
            make_game(id="g-b1", time_class="bullet", end_time=3),
        ],
        analyzed={"g-r1", "g-r2", "g-b1"},
    )

    rapid: Any = get(
        client, "/api/players/testuser/profile", params={"time_class": "rapid"}
    ).json()
    mixed: Any = get(client, "/api/players/testuser/profile").json()

    assert rapid["profile"]["games_covered"] == 2
    assert rapid["profile"]["time_class"] == "rapid"
    assert mixed["profile"]["games_covered"] == 3
    assert mixed["profile"]["time_class"] is None


def test_profile_get_scopes_facts_by_window(client: TestClient, db_path: Path) -> None:
    seed(
        db_path,
        [
            make_game(id="g-old", end_time=1_000),
            make_game(id="g-new", end_time=100_000),
        ],
        analyzed={"g-old", "g-new"},
    )

    body: Any = get(
        client, "/api/players/testuser/profile", params={"since": "50000"}
    ).json()

    assert body["profile"]["games_covered"] == 1


def test_profile_narrative_is_read_per_time_class(
    client: TestClient, db_path: Path
) -> None:
    """The narrative is stored per time control, so a rapid request must
    not be served the bullet narrative (or vice versa)."""
    seed(db_path, [make_game(id="g-1", time_class="rapid")], analyzed={"g-1"})
    seed_profile(db_path, time_class="rapid", narrative="rapid-narrative")
    seed_profile(db_path, time_class="bullet", narrative="bullet-narrative")

    rapid: Any = get(
        client, "/api/players/testuser/profile", params={"time_class": "rapid"}
    ).json()
    bullet: Any = get(
        client, "/api/players/testuser/profile", params={"time_class": "bullet"}
    ).json()
    mixed: Any = get(client, "/api/players/testuser/profile").json()

    assert rapid["profile"]["narrative"] == "rapid-narrative"
    assert bullet["profile"]["narrative"] == "bullet-narrative"
    assert mixed["narrative"] is None  # nothing stored for the mixed scope


def test_profile_narrative_survives_a_window_filter(
    client: TestClient, db_path: Path
) -> None:
    """The window scopes facts but is deliberately not part of the
    narrative's key: keying on `since` -- which moves every day -- would
    strand the stored narrative overnight (docs/07-api.md)."""
    seed(
        db_path,
        [
            make_game(id="g-old", time_class="rapid", end_time=1_000),
            make_game(id="g-new", time_class="rapid", end_time=100_000),
        ],
        analyzed={"g-old", "g-new"},
    )
    seed_profile(db_path, time_class="rapid", narrative="rapid-narrative")

    body: Any = get(
        client,
        "/api/players/testuser/profile",
        params={"time_class": "rapid", "since": "50000"},
    ).json()

    assert body["profile"]["games_covered"] == 1  # facts windowed
    assert body["profile"]["narrative"] == "rapid-narrative"  # narrative kept
    # The staleness basis is the narrative's own scope (all rapid games),
    # not the windowed facts -- otherwise every windowed view reads stale.
    assert body["narrative_games_now"] == 2


def test_profile_post_stores_per_time_class(client: TestClient, db_path: Path) -> None:
    seed(
        db_path,
        [
            make_game(id="g-r1", time_class="rapid", end_time=1),
            make_game(id="g-b1", time_class="bullet", end_time=2),
        ],
        analyzed={"g-r1", "g-b1"},
    )

    rapid: Any = post(
        client, "/api/players/testuser/profile", json={"time_class": "rapid"}
    ).json()

    assert rapid["profile"]["time_class"] == "rapid"
    assert rapid["profile"]["games_covered"] == 1
    # Stored under rapid only: the bullet scope is still empty.
    bullet: Any = get(
        client, "/api/players/testuser/profile", params={"time_class": "bullet"}
    ).json()
    assert bullet["narrative"] is None


def test_profile_post_409s_when_the_requested_class_has_no_analyzed_games(
    client: TestClient, db_path: Path
) -> None:
    seed(db_path, [make_game(id="g-r1", time_class="rapid")], analyzed={"g-r1"})

    response = post(
        client, "/api/players/testuser/profile", json={"time_class": "bullet"}
    )

    assert response.status_code == 409
    assert "bullet" in response.json()["error"]["message"]


def test_profile_get_reports_both_denominators(
    client: TestClient, db_path: Path
) -> None:
    """Coverage stops being implied: two stored games, one analyzed, must
    read as 1 of 2 rather than presenting the analyzed game as the whole
    history (docs/06-coach.md, "Volume and quality")."""
    seed(
        db_path,
        [
            make_game(id="g-1", end_time=1),
            make_game(id="g-2", end_time=2),
        ],
        analyzed={"g-1"},
    )

    body: Any = get(client, "/api/players/testuser/profile").json()

    assert body["profile"]["games_covered"] == 1
    assert body["profile"]["games_in_scope"] == 2


def test_explain_embeds_the_profile_for_the_games_own_time_class(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    """A bullet game is explained to a coach who knows this student's
    bullet tendencies, not an average across controls they play very
    differently (docs/06-coach.md, "Embedding")."""
    seed(db_path, [make_game(id="g-1", time_class="bullet")], analyzed={"g-1"})
    seed_profile(db_path, time_class="bullet", narrative="zzz-bullet-read-zzz")
    seed_profile(db_path, time_class="rapid", narrative="zzz-rapid-read-zzz")

    assert get(client, "/api/games/g-1/explain", params={"ply": "1"}).status_code == 200

    prompt = stub_provider(stub_registry, "claude").explain_prompts[0]
    assert "zzz-bullet-read-zzz" in prompt
    assert "zzz-rapid-read-zzz" not in prompt


def test_explain_falls_back_to_the_all_classes_profile(
    client: TestClient, db_path: Path, stub_registry: dict[str, object]
) -> None:
    """No row for the game's control -- the mixed profile is better than
    nothing, and is what a student who only generated the unscoped
    profile has."""
    seed(db_path, [make_game(id="g-1", time_class="bullet")], analyzed={"g-1"})
    seed_profile(db_path, narrative="zzz-mixed-read-zzz")  # all-classes row only

    assert get(client, "/api/games/g-1/explain", params={"ply": "1"}).status_code == 200

    assert (
        "zzz-mixed-read-zzz"
        in stub_provider(stub_registry, "claude").explain_prompts[0]
    )
