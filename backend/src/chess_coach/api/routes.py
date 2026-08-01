"""HTTP routes (docs/07-api.md)."""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from functools import partial
from typing import Annotated, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from starlette.concurrency import run_in_threadpool

from chess_coach.api.chat import (
    CHAT_MESSAGE_CAP,
    ApiChatToolkit,
    cached_assistant_turn,
    game_scope_context,
    profile_for_game,
    report_scope_context,
    time_class_or_none,
    window_or_none,
)
from chess_coach.api.runs import MAX_FINISHED_RUNS, AnalysisRun, evict_finished
from chess_coach.coach import (
    PROFILE_PROMPT_VERSION,
    PROMPT_VERSION,
    ChatToolkit,
    CoachProvider,
    CoachProviderError,
    PlayerHighlights,
    PositionAnalystFn,
    append_game_links,
    build_highlights,
    build_move_context,
    build_profile,
    build_report,
    build_trajectory,
    profile_window,
    render_explain_prompt,
    render_profile_prompt,
    render_prompt,
    window_spans_level_change,
)
from chess_coach.config import AppConfig
from chess_coach.domain import (
    ChatMessage,
    Color,
    EvalLine,
    Game,
    GameDetail,
    GameSummary,
    LlmProvider,
    OpeningStats,
    PlayerProfile,
    PlayerReport,
    PlayerSummary,
    Result,
    TimeClass,
)
from chess_coach.engine import (
    ANALYSIS_VERSION,
    AnalysisPool,
    EngineError,
    EngineOptions,
    Progress,
)
from chess_coach.ingestion import sync_games
from chess_coach.openings import OpeningBook, RepertoireNode, build_repertoire
from chess_coach.storage import (
    ChatScope,
    ChatThread,
    ChatThreadSummary,
    Db,
    GameFilters,
    ReportKey,
    append_chat_exchange,
    clear_chat_provider_state,
    count_analyzed_games,
    count_games_needing_analysis,
    create_chat_thread,
    delete_chat_thread,
    games_missing_opening,
    games_needing_analysis,
    get_chat_thread,
    get_explanation,
    get_game,
    get_player_profile,
    get_report,
    latest_game_time,
    list_analyzed_games,
    list_chat_messages,
    list_chat_threads,
    list_game_summaries,
    list_games,
    list_players,
    list_repertoire_games,
    opening_stats,
    save_analysis,
    save_explanation,
    save_player_profile,
    save_report,
    set_opening,
    upsert_games,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _db(request: Request) -> Db:
    return cast(Db, request.app.state.db)


def _book(request: Request) -> OpeningBook:
    return cast(OpeningBook, request.app.state.book)


def _cfg(request: Request) -> AppConfig:
    return cast(AppConfig, request.app.state.cfg)


def _pool(request: Request) -> AnalysisPool | None:
    return cast(AnalysisPool | None, request.app.state.pool)


def _runs(request: Request) -> dict[str, AnalysisRun]:
    return cast(dict[str, AnalysisRun], request.app.state.runs)


def _providers(request: Request) -> dict[str, CoachProvider]:
    return cast(dict[str, CoachProvider], request.app.state.providers)


def _chat_inflight(request: Request) -> set[str]:
    return cast(set[str], request.app.state.chat_inflight)


DbDep = Annotated[Db, Depends(_db)]
BookDep = Annotated[OpeningBook, Depends(_book)]
CfgDep = Annotated[AppConfig, Depends(_cfg)]
PoolDep = Annotated[AnalysisPool | None, Depends(_pool)]
RunsDep = Annotated[dict[str, AnalysisRun], Depends(_runs)]
ProvidersDep = Annotated[dict[str, CoachProvider], Depends(_providers)]
ChatInFlightDep = Annotated[set[str], Depends(_chat_inflight)]


def _build_analyst(pool: AnalysisPool, cfg: AppConfig) -> PositionAnalystFn:
    """The API layer's `PositionAnalystFn` implementation, wrapping the
    engine pool with config depth/multipv -- this is where coach meets
    engine; they never import each other. Shared by explain, coach, and
    chat (docs/07-api.md)."""

    async def _analyst(fen: str) -> list[EvalLine]:
        return await pool.eval_lines(fen, cfg.engine.depth, cfg.engine.multipv)

    return _analyst


class SyncResult(BaseModel):
    games_synced: int


@router.post("/players/{username}/sync")
async def sync_player(
    username: str, db: DbDep, book: BookDep, full: bool = False
) -> SyncResult:
    """Fetch new games from chess.com, store and classify them.

    `full=True` re-fetches the entire archive instead of just the games
    since the last sync — the upsert makes this idempotent — to backfill
    columns added after games were stored (currently `termination`); a
    normal sync never re-fetches a stored game, so only a full re-sync
    can pick up such a column for existing rows.
    """
    user = username.lower()
    since = None if full else await run_in_threadpool(latest_game_time, db, user)
    synced = 0
    async for batch in sync_games(user, since):
        # Threadpool: this endpoint is async for the sake of the
        # chess.com fetch, so its sync storage writes would otherwise
        # run on the event loop and stall every concurrent request and
        # SSE stream — a full re-sync writes the whole archive.
        await run_in_threadpool(upsert_games, db, batch)
        synced += len(batch)

    def classify_backlog() -> None:
        # One pass covers the new games and any stored-but-unclassified
        # backlog; book-less games simply stay unclassified.
        for game in games_missing_opening(db, user):
            opening = book.classify(game.san_moves)
            if opening is not None:
                set_opening(db, game.id, opening)

    # Threadpool for the same reason: classification replays up to 30
    # plies per unclassified game, which after a full re-sync is the
    # whole archive — minutes of CPU that must not block the loop.
    await run_in_threadpool(classify_backlog)
    return SyncResult(games_synced=synced)


@router.get("/players")
def list_all_players(db: DbDep) -> list[PlayerSummary]:
    """Every stored player, most games first — the saved-players picker."""
    return list_players(db)


@router.get("/players/{username}/openings")
def player_openings(
    username: str,
    db: DbDep,
    since: int | None = None,
    until: int | None = None,
    time_class: TimeClass | None = None,
) -> list[OpeningStats]:
    """Per-opening record over classified games, most-played first.

    `since`/`until` (epoch seconds) restrict to a time window;
    `time_class` restricts to one time control.
    """
    return opening_stats(
        db, username.lower(), since=since, until=until, time_class=time_class
    )


class RepertoireTree(BaseModel):
    username: str
    color: Color
    games: int  # scope totals for this color
    analyzed: int
    root: RepertoireNode


@router.get("/players/{username}/openings/tree")
def player_openings_tree(
    username: str,
    db: DbDep,
    book: BookDep,
    color: Color,
    since: int | None = None,
    until: int | None = None,
    time_class: TimeClass | None = None,
    min_games: int = 2,
    max_plies: int = 30,
) -> RepertoireTree:
    """Per-color repertoire move tree (docs/future-improvements/
    openings-explorer.md): drill from 1.e4 into any line, with games,
    score, eval, book status, and continuations at every node.

    `since`/`until`/`time_class` scope the games exactly like
    `/openings`. `min_games` (default 2, clamped 1-10) prunes one-off
    deviations; `max_plies` (default 30, clamped 4-40) caps tree depth,
    mirroring `classify`'s book-ply cap. Both clamps are silent
    (max/min, no 422), like `/api/eval`'s depth/multipv. An unknown
    player has no stored games, so this returns an empty tree
    (`games=0`, a childless root) rather than 404ing, consistent with
    `/openings` and `/report`.
    """
    user = username.lower()
    clamped_min_games = max(1, min(10, min_games))
    clamped_max_plies = max(4, min(40, max_plies))
    games = list_repertoire_games(
        db,
        user,
        max_plies=clamped_max_plies,
        since=since,
        until=until,
        time_class=time_class,
    )
    root = build_repertoire(
        book,
        games,
        color=color,
        min_games=clamped_min_games,
        max_plies=clamped_max_plies,
    )
    return RepertoireTree(
        username=user,
        color=color,
        games=root.record.games,
        analyzed=root.analyzed,
        root=root,
    )


@router.get("/players/{username}/games")
def player_games(
    username: str,
    db: DbDep,
    opening_eco: str | None = None,
    result: Result | None = None,
    time_class: TimeClass | None = None,
    analyzed: bool | None = None,
    # ge=0 at the edge so a negative value 422s instead of failing
    # GameFilters' own validation inside the handler (a 500); SQLite
    # reads a negative LIMIT as "unlimited", which would return the
    # whole table (docs/archive/codebase-scan-2026-07.md, finding 10).
    limit: Annotated[int, Query(ge=0)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[GameSummary]:
    filters = GameFilters(
        opening_eco=opening_eco,
        result=result,
        time_class=time_class,
        analyzed=analyzed,
        limit=limit,
        offset=offset,
    )
    return list_games(db, username.lower(), filters)


@router.get("/games/{game_id}")
def game_detail(game_id: str, db: DbDep) -> GameDetail:
    game = get_game(db, game_id)
    if game is None:
        raise HTTPException(status_code=404, detail=f"unknown game: {game_id}")
    return game


class AnalyzeRequest(BaseModel):
    game_ids: list[str] | None = None
    # Bulk path only; capped by config. ge=0 because SQLite reads a
    # negative LIMIT as "unlimited", which would bypass the cap; 0 is
    # the documented no-op probe.
    limit: int | None = Field(default=None, ge=0)
    # Bulk-path scope only (ignored when game_ids is set); same window
    # semantics as everywhere else (since inclusive, until exclusive).
    # Passed to both the enqueue and the remaining count so they always
    # describe the same scope.
    since: int | None = None
    until: int | None = None
    time_class: TimeClass | None = None


class AnalyzeResult(BaseModel):
    queued: int
    # Unanalyzed games not covered by this run. Exact on the bulk
    # path; the game_ids path subtracts the whole resolved list, so
    # re-analyzing already-analyzed games under-counts by that many
    # (floored at 0) — an accepted approximation.
    remaining: int


@router.post("/players/{username}/analyze", status_code=202)
async def analyze_player(
    username: str,
    db: DbDep,
    pool: PoolDep,
    cfg: CfgDep,
    runs: RunsDep,
    body: AnalyzeRequest | None = None,
) -> AnalyzeResult:
    """Queue engine analysis; follow progress via the SSE endpoint."""
    if pool is None:
        raise HTTPException(
            status_code=503,
            detail="engine binary not found — build it with `make engine`",
        )
    user = username.lower()
    active = runs.get(user)
    if active is not None and not active.finished:
        raise HTTPException(
            status_code=409, detail="analysis already running for this player"
        )

    if body is not None and body.game_ids:
        # Resolve ids defensively: unknown ids and other players' games
        # are dropped — a run registered under {username} must only
        # ever analyze that player's games — and duplicates collapse so
        # a repeated id isn't analyzed (and billed in engine time)
        # twice.
        games: list[Game] = []
        seen: set[str] = set()
        for game_id in body.game_ids:
            if game_id in seen:
                continue
            seen.add(game_id)
            game = get_game(db, game_id)
            if game is not None and game.username == user:
                games.append(game)
        remaining = max(
            0,
            count_games_needing_analysis(db, user, cfg.engine.depth, ANALYSIS_VERSION)
            - len(games),
        )
    else:
        since = body.since if body is not None else None
        until = body.until if body is not None else None
        time_class = body.time_class if body is not None else None
        limit = cfg.engine.analyze_limit
        if body is not None and body.limit is not None:
            limit = min(body.limit, cfg.engine.analyze_limit)
        games = games_needing_analysis(
            db,
            user,
            cfg.engine.depth,
            ANALYSIS_VERSION,
            limit,
            since=since,
            until=until,
            time_class=time_class,
        )
        remaining = max(
            0,
            count_games_needing_analysis(
                db,
                user,
                cfg.engine.depth,
                ANALYSIS_VERSION,
                since=since,
                until=until,
                time_class=time_class,
            )
            - len(games),
        )

    if not games:
        # Nothing to enqueue: answer 202 without touching `runs`, so no
        # run is started and no 409-blocking state is left behind. This
        # makes `limit: 0` a free probe and `queued=0, remaining=0` the
        # backfill's termination signal, per
        # docs/archive/fixes-2026-07/07-analysis-coverage.md. The active-run
        # 409 guard above already ran, so a probe against a genuinely
        # running player still 409s as usual.
        return AnalyzeResult(queued=0, remaining=remaining)

    # Bound the registry before adding this run: a long-lived process that
    # analyzes many distinct usernames (the player switcher) would
    # otherwise keep every finished run forever, since a dict entry is
    # only ever replaced by that same username starting another run.
    evict_finished(runs, keep=MAX_FINISHED_RUNS)
    run = AnalysisRun(len(games))
    runs[user] = run
    opts = EngineOptions(depth=cfg.engine.depth, thresholds=cfg.thresholds)
    run.task = asyncio.create_task(_run_analysis(db, pool, run, games, opts))
    return AnalyzeResult(queued=len(games), remaining=remaining)


async def _run_analysis(
    db: Db,
    pool: AnalysisPool,
    run: AnalysisRun,
    games: list[Game],
    opts: EngineOptions,
) -> None:
    async def analyze_one(game: Game) -> None:
        def on_progress(progress: Progress) -> None:
            run.publish(run.event("progress", progress))

        analysis = await pool.analyze_game(game, opts, on_progress)
        # Threadpool: this task shares the event loop with every SSE
        # stream; the write serializes a full per-move eval list.
        await run_in_threadpool(save_analysis, db, analysis, ANALYSIS_VERSION)
        run.games_done += 1
        run.publish(run.event("game_done"))

    results = await asyncio.gather(
        *(analyze_one(game) for game in games), return_exceptions=True
    )
    failures = [r for r in results if isinstance(r, BaseException)]
    run.mark_finished()
    if failures:
        logger.error(
            "analysis run: %d of %d game(s) failed: %r",
            len(failures),
            len(games),
            failures[0],
        )
        run.publish(run.event("run_failed"))
    else:
        run.publish(run.event("run_done"))


@router.get("/players/{username}/analyze/progress")
async def analyze_progress(username: str, runs: RunsDep) -> EventSourceResponse:
    """SSE stream for the player's current analysis run."""
    run = runs.get(username.lower())
    if run is None:
        raise HTTPException(status_code=404, detail=f"no analysis run for {username}")
    queue = run.subscribe()

    async def stream() -> AsyncIterator[dict[str, str]]:
        try:
            snapshot = run.event("snapshot")
            yield {"event": snapshot.type, "data": snapshot.model_dump_json()}
            if run.finished:
                return
            while True:
                event = await queue.get()
                yield {"event": event.type, "data": event.model_dump_json()}
                if event.finished:
                    return
        finally:
            run.unsubscribe(queue)

    return EventSourceResponse(stream())


class EvalError(BaseModel):
    """Mid-stream `engine_error` SSE payload — too late for an
    HTTPException once events are on the wire. Deliberately not named
    `error`: an EventSource client cannot tell a server-sent `error`
    event apart from the browser's own network-error event, which
    shares that type.
    """

    message: str


@router.get("/eval")
async def eval_position(
    pool: PoolDep,
    cfg: CfgDep,
    fen: str,
    depth: int | None = None,
    multipv: int | None = None,
) -> EventSourceResponse:
    """SSE live eval of one position: `eval` per depth, then `done`."""
    if pool is None:
        raise HTTPException(
            status_code=503,
            detail="engine binary not found — build it with `make engine`",
        )
    resolved_depth = cfg.engine.depth if depth is None else depth
    clamped_depth = max(1, min(40, resolved_depth))
    resolved_multipv = cfg.engine.multipv if multipv is None else multipv
    clamped_multipv = max(1, min(10, resolved_multipv))
    try:
        # stream_eval parses the FEN eagerly, so a bad one fails here
        # as a 400 — before any streaming response begins.
        evals = pool.stream_eval(fen, clamped_depth, clamped_multipv)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid FEN: {exc}") from exc

    async def stream() -> AsyncIterator[dict[str, str]]:
        # aclosing: a client disconnect closes this generator, which
        # must close the engine stream so the worker frees promptly.
        try:
            async with aclosing(evals) as events:
                async for event in events:
                    yield {"event": "eval", "data": event.model_dump_json()}
        except EngineError as exc:
            # Mirrors explain_move's mid-stream error event: the search
            # died after events were already on the wire, so the client
            # gets a terminal event instead of a bare connection drop
            # (which EventSource would answer by reconnecting and
            # re-running the same failing search).
            yield {
                "event": "engine_error",
                "data": EvalError(message=str(exc)).model_dump_json(),
            }
            return
        yield {"event": "done", "data": ""}

    return EventSourceResponse(stream())


class ExplainDone(BaseModel):
    """Terminal `done` SSE payload: the full markdown explanation."""

    text: str


class ExplainError(BaseModel):
    """Mid-stream `error` SSE payload — too late for an HTTPException,
    since the stream has already started.
    """

    message: str


@router.get("/games/{game_id}/explain")
async def explain_move(
    game_id: str,
    db: DbDep,
    cfg: CfgDep,
    pool: PoolDep,
    providers: ProvidersDep,
    ply: int,
    agent_id: str | None = None,
    refresh: bool = False,
) -> EventSourceResponse:
    """SSE coach explanation of one played move; cached per (game, ply, agent).

    `refresh=True` skips the cache read and regenerates, overwriting the
    cached row with the new result.
    """
    resolved_agent_id = cfg.coach.default_agent if agent_id is None else agent_id
    provider = providers.get(resolved_agent_id)
    if provider is None:
        raise HTTPException(
            status_code=400, detail=f"unknown coach agent: {resolved_agent_id}"
        )

    # Threadpool: async endpoint (the provider stream below), so these
    # sync storage reads — get_game deserializes the full eval list —
    # must not run on the event loop.
    game = await run_in_threadpool(get_game, db, game_id)
    if game is None:
        raise HTTPException(status_code=404, detail=f"unknown game: {game_id}")
    analysis = game.analysis
    if analysis is None:
        raise HTTPException(
            status_code=409,
            detail="no analysis for this game — analyze this game first",
        )

    cached = (
        None
        if refresh
        else await run_in_threadpool(
            get_explanation, db, game_id, ply, resolved_agent_id
        )
    )
    if cached is not None:

        async def cached_stream() -> AsyncIterator[dict[str, str]]:
            yield {"event": "done", "data": ExplainDone(text=cached).model_dump_json()}

        return EventSourceResponse(cached_stream())

    try:
        ctx = build_move_context(game, analysis, game.opening, ply)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if pool is None:
        raise HTTPException(
            status_code=503,
            detail="engine binary not found — build it with `make engine`",
        )

    try:
        lines = await pool.eval_lines(
            ctx.fen_before, cfg.engine.depth, cfg.engine.multipv
        )
    except EngineError as exc:
        raise HTTPException(status_code=502, detail=f"engine failure: {exc}") from exc
    # Stored row only, never a fresh aggregation (docs/06-coach.md, "Player
    # profile", "Embedding"): the stored facts+narrative pair is coherent
    # where fresh facts under an older narrative could contradict each
    # other, and rebuilding facts would put a full-archive aggregation on
    # every explain call. No row -> None -> the prompt renders unchanged.
    profile = await run_in_threadpool(
        profile_for_game, db, game.username, game.time_class
    )
    prompt = render_explain_prompt(ctx, lines, profile=profile)
    analyst = _build_analyst(pool, cfg)

    async def stream() -> AsyncIterator[dict[str, str]]:
        chunks: list[str] = []
        try:
            # aclosing: a client disconnect closes this generator, which
            # stops generation immediately and caches nothing.
            async with aclosing(provider.explain(prompt, analyst)) as events:
                async for event in events:
                    if event.type == "text":
                        chunks.append(event.text)
                    elif event.type == "tool":
                        # Text written before an engine call is the model
                        # narrating its plan, not the explanation
                        # (docs/06-coach.md, "Providers"). It still streams
                        # to the panel below, so the student watches the
                        # work; only what gets cached drops it. Safe to do
                        # on dequeue here, unlike the Copilot chat path:
                        # this loop is the sole consumer of a sequential
                        # generator, so no later text can have arrived yet.
                        chunks.clear()
                    yield {"event": event.type, "data": event.model_dump_json()}
        except CoachProviderError as exc:
            # Too late for an HTTPException: events are already on the
            # wire, so the failure becomes an SSE event instead.
            yield {
                "event": "error",
                "data": ExplainError(message=str(exc)).model_dump_json(),
            }
            return
        full_text = "".join(chunks)
        await run_in_threadpool(
            save_explanation, db, game_id, ply, resolved_agent_id, full_text
        )
        yield {"event": "done", "data": ExplainDone(text=full_text).model_dump_json()}

    return EventSourceResponse(stream())


class CoachAgentInfo(BaseModel):
    """Selectable agent as shown to the UI; no LLM knobs exposed."""

    id: str
    label: str
    provider: LlmProvider
    model: str


class CoachAgentsResponse(BaseModel):
    agents: list[CoachAgentInfo]
    default: str


@router.get("/coach/agents")
def coach_agents(cfg: CfgDep) -> CoachAgentsResponse:
    """The configured coach roster and the default agent id."""
    return CoachAgentsResponse(
        agents=[
            CoachAgentInfo(
                id=agent.id,
                label=agent.label,
                provider=agent.provider,
                model=agent.model,
            )
            for agent in cfg.coach.agents
        ],
        default=cfg.coach.default_agent,
    )


class CoachRequest(BaseModel):
    agent_id: str | None = None  # None -> config default agent
    # The same window/time-control filters `/report` takes, so the coach
    # reasons over the period the student is looking at rather than the
    # player's entire history.
    since: int | None = None
    until: int | None = None
    time_class: TimeClass | None = None
    refresh: bool = False  # bypass the cache read and regenerate


class CoachResponse(BaseModel):
    prompt: str
    advice: str
    agent_id: str
    cached: bool
    generated_at: int  # epoch seconds the served advice was generated
    games_analyzed: int  # games covered at generation time


@router.get("/players/{username}/report")
def player_report(
    username: str,
    db: DbDep,
    since: int | None = None,
    until: int | None = None,
    time_class: TimeClass | None = None,
) -> PlayerReport:
    """Aggregated stats over the player's games.

    `since`/`until` (epoch seconds) restrict to a time window;
    `time_class` restricts to one time control.

    Both lists go in (docs/06-coach.md, "Volume and quality"): the
    analyzed games carry ACPL, judgments and error patterns, the full
    scope carries ratings, records and repertoire counts. `all_games`
    also *is* the scope count, so it replaces the separate
    `count_games` query rather than adding to it.
    """
    user = username.lower()
    all_games = list_game_summaries(
        db, user, since=since, until=until, time_class=time_class
    )
    return build_report(
        user,
        list_analyzed_games(db, user, since=since, until=until, time_class=time_class),
        all_games=all_games,
        time_class=time_class,
        requested_since=since,
        requested_until=until,
        games_in_scope=len(all_games),
    )


@router.get("/players/{username}/highlights")
def player_highlights(
    username: str,
    db: DbDep,
    cfg: CfgDep,
    since: int | None = None,
    until: int | None = None,
    time_class: TimeClass | None = None,
) -> PlayerHighlights:
    """Blunders and brilliancies over the player's analyzed games.

    `since`/`until` (epoch seconds) restrict to a time window;
    `time_class` restricts to one time control — same as `/report`. An
    unknown player simply has no analyzed games, so this returns empty
    lists rather than 404ing, consistent with `/report`/`/openings`.
    """
    user = username.lower()
    return build_highlights(
        list_analyzed_games(db, user, since=since, until=until, time_class=time_class),
        thresholds=cfg.brilliant,
    )


# Docstring stays word-for-word the API's OpenAPI description (no HTTP
# surface change here) — the engine-tool wiring is explained in comments
# below instead of growing the docstring.
@router.post("/players/{username}/coach")
async def coach_player(
    username: str,
    db: DbDep,
    cfg: CfgDep,
    pool: PoolDep,
    providers: ProvidersDep,
    body: CoachRequest | None = None,
) -> CoachResponse:
    """Build the report, render the prompt, and ask the chosen agent.

    Coaching is the most expensive call the app makes, so — like
    `GET /games/{id}/explain` — it is cached: the window/time-class
    filters are part of the cache key alongside the agent and
    `coach.PROMPT_VERSION`, with a `refresh` escape hatch that skips the
    cache read and overwrites the cached row.
    """
    agent_id = cfg.coach.default_agent
    since: int | None = None
    until: int | None = None
    time_class: TimeClass | None = None
    refresh = False
    if body is not None:
        if body.agent_id is not None:
            agent_id = body.agent_id
        since = body.since
        until = body.until
        time_class = body.time_class
        refresh = body.refresh
    provider = providers.get(agent_id)
    if provider is None:
        raise HTTPException(status_code=400, detail=f"unknown coach agent: {agent_id}")

    user = username.lower()
    key = ReportKey(
        username=user,
        agent_id=agent_id,
        prompt_version=PROMPT_VERSION,
        since=since if since is not None else 0,
        until=until if until is not None else 0,
        time_class=time_class if time_class is not None else "",
    )

    if not refresh:
        cached = await run_in_threadpool(get_report, db, key)
        if cached is not None:
            # A cache hit serves without touching storage's games table
            # or the provider — a report for a player whose games were
            # since deleted still serves from here.
            return CoachResponse(
                prompt=cached.prompt,
                advice=cached.advice,
                agent_id=agent_id,
                cached=True,
                generated_at=cached.created_at,
                games_analyzed=cached.games_analyzed,
            )

    def _load_and_build() -> tuple[str, PlayerReport]:
        # Runs off the event loop: list_analyzed_games and count_games hit
        # storage and build_report replays every game with python-chess
        # several times over, which is not cheap at hundreds of games.
        games = list_analyzed_games(
            db, user, since=since, until=until, time_class=time_class
        )
        if not games:
            raise HTTPException(
                status_code=409,
                detail="no analyzed games yet — sync and analyze first",
            )
        all_games = list_game_summaries(
            db, user, since=since, until=until, time_class=time_class
        )
        report = build_report(
            user,
            games,
            all_games=all_games,
            time_class=time_class,
            requested_since=since,
            requested_until=until,
            games_in_scope=len(all_games),
        )
        return render_prompt(report), report

    prompt, report = await run_in_threadpool(_load_and_build)

    # Same wrapper explain_move builds around the engine pool — this is
    # where coach meets engine; they never import each other. When the
    # pool is up, the provider runs agentically against `analyze_position`
    # so it can verify concrete lines before asserting them. Unlike
    # analyze/eval, a missing pool is not fatal here: `None` degrades the
    # provider to its single-turn path and the report still generates.
    analyst: PositionAnalystFn | None = None
    if pool is not None:
        analyst = _build_analyst(pool, cfg)

    try:
        advice = await provider.complete(prompt, analyst)
    except CoachProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # Post-process before caching: the handles the model cited become real
    # links here (docs/06-coach.md, "Game links"), and the cached row must
    # hold this post-processed advice so a cache hit is self-contained and
    # never re-processed on the read path.
    advice = append_game_links(advice, report)

    generated_at = await run_in_threadpool(
        save_report, db, key, prompt, advice, report.games_analyzed
    )
    return CoachResponse(
        prompt=prompt,
        advice=advice,
        agent_id=agent_id,
        cached=False,
        generated_at=generated_at,
        games_analyzed=report.games_analyzed,
    )


# --- Player profile (docs/07-api.md "Player profile"; docs/06-coach.md
# --- "Player profile" is the contract) ----------------------------------


class ProfileNarrative(BaseModel):
    """Metadata for the profile's stored narrative: who generated it,
    under which prompt version, when, and how many games the *snapshot it
    described* covered. That last figure is deliberately the stored
    snapshot's `games_covered`, not the response's (always fresh)
    `profile.games_covered` -- together they let the UI say "narrative
    generated over N games; you have M now"."""

    agent_id: str
    prompt_version: str
    generated_at: int  # epoch seconds the narrative was generated
    games_covered: int  # the stored snapshot's games_covered, not fresh


class ProfileResponse(BaseModel):
    profile: PlayerProfile  # facts always fresh; narrative attached when stored
    narrative: ProfileNarrative | None = None  # None when nothing is stored yet
    # Analyzed games in the *narrative's* own scope (its time control over
    # the player's full history), which is not the response's scope
    # whenever a window filter is applied. The staleness hint compares
    # this against `narrative.games_covered`; comparing against
    # `profile.games_covered` instead would flag every windowed view as
    # stale, since a 30-day facts count can never match a full-history
    # narrative's. None when no narrative is stored.
    narrative_games_now: int | None = None
    # The prompt version the backend would generate under *now*, against
    # `narrative.prompt_version` for what the stored text was written
    # under. Without it a bump flags nothing, which is precisely what
    # docs/06-coach.md promises it does -- and a narrative written under
    # an older template can contradict the facts rendered beside it
    # ("drift downward from a high" under a trajectory reading +443 over
    # the year, which is what profile-v5 was for).
    prompt_version: str = PROFILE_PROMPT_VERSION


def _load_profile_facts(
    db: Db,
    username: str,
    *,
    since: int | None = None,
    until: int | None = None,
    time_class: TimeClass | None = None,
) -> PlayerProfile:
    """Fresh facts over the requested scope (docs/07-api.md, "Player
    profile"): `list_analyzed_games` + `list_game_summaries` ->
    `build_report` -> `build_profile`.

    Both lists, for the reason `/report` passes both: ratings, records
    and repertoire counts describe every game in scope, and restricting
    them to the analyzed subset reports a rating from whichever game the
    engine happened to reach last (docs/06-coach.md, "Volume and
    quality"). `narrative` is always None here -- callers attach the
    stored one, if any. An unknown player has no stored games, so this is
    simply the profile of an empty report, mirroring `/report`.
    """
    # Pass one: every stored game in the caller's scope. Light rows (no
    # PGN), and they carry both things only the *unwindowed* archive can
    # answer -- where the student's current level begins, and where they
    # are heading (docs/06-coach.md, "Window", "Trajectory").
    archive = list_game_summaries(
        db, username, since=since, until=until, time_class=time_class
    )
    # Trajectory ignores the caller's window as well as the level one:
    # both renderers say it covers "the whole archive in this time
    # control", and under a 90-day page filter that would have been an
    # overstatement of 90 days of games. Its own query, because it is
    # the one figure whose whole point is to outlive every window
    # (docs/06-coach.md, "Trajectory"); the rows are light and carry no
    # PGN, which is what makes a second pass affordable.
    trajectory = build_trajectory(
        archive
        if since is None and until is None
        else list_game_summaries(db, username, time_class=time_class)
    )
    archive_report = build_report(username, [], all_games=archive)
    level_since = profile_window(archive_report.months)
    spans_change = window_spans_level_change(archive_report.months, level_since)

    # Pass two, narrowed to that level. The bound only ever tightens the
    # caller's own window, never widens it: a request for last month must
    # not come back covering six.
    outcome_since = (
        max(x for x in (since, level_since) if x is not None)
        if (since is not None or level_since is not None)
        else None
    )

    games = list_analyzed_games(
        db, username, since=outcome_since, until=until, time_class=time_class
    )
    all_games = (
        archive
        if outcome_since is None
        else [g for g in archive if g.end_time >= outcome_since]
    )
    report = build_report(
        username,
        games,
        all_games=all_games,
        time_class=time_class,
        requested_since=outcome_since,
        requested_until=until,
        games_in_scope=len(all_games),
    )
    return build_profile(report, trajectory=trajectory, spans_level_change=spans_change)


@router.get("/players/{username}/profile")
def player_profile(
    username: str,
    db: DbDep,
    since: int | None = None,
    until: int | None = None,
    time_class: TimeClass | None = None,
) -> ProfileResponse:
    """The student profile (docs/06-coach.md, "Player profile"): facts are
    always freshly aggregated -- never an LLM call -- and the stored
    narrative, when one exists, is attached to the facts' `narrative`
    field alongside its own metadata.

    Facts honour all three filters. The narrative is looked up by
    `time_class` alone, because that is the only dimension it is stored
    under: `since` moves with the calendar, so keying the narrative on it
    would strand every stored row overnight. A windowed request therefore
    returns windowed facts beside a narrative describing that control's
    whole history -- `narrative_games_now` states the latter's own live
    count so the UI can label both honestly.

    An unknown player has no stored games, so this returns empty facts
    and no narrative, 200 like `/report`.
    """
    user = username.lower()
    facts = _load_profile_facts(
        db, user, since=since, until=until, time_class=time_class
    )
    cached = get_player_profile(db, user, time_class=time_class)
    if cached is None:
        return ProfileResponse(profile=facts)
    return ProfileResponse(
        profile=facts.model_copy(update={"narrative": cached.profile.narrative}),
        narrative=ProfileNarrative(
            agent_id=cached.agent_id,
            prompt_version=cached.prompt_version,
            generated_at=cached.created_at,
            games_covered=cached.profile.games_covered,
        ),
        # The narrative's own scope, which is the *level window* POST
        # generates it over -- not the request's window, and no longer
        # "no window at all". Counting unwindowed here compared a
        # windowed `games_covered` against an archive-wide total, so on
        # any player whose analysis reaches past the window the banner
        # was permanently on and regenerating never cleared it. The
        # stored facts carry the bound they were built with.
        narrative_games_now=count_analyzed_games(
            db,
            user,
            since=cached.profile.window_start,
            time_class=time_class,
        ),
    )


class ProfileGenerateRequest(BaseModel):
    agent_id: str | None = None  # None -> config default agent
    # Which time control to describe; None -> all controls mixed. The
    # narrative's only scope dimension, and its storage key.
    time_class: TimeClass | None = None


@router.post("/players/{username}/profile")
async def regenerate_player_profile(
    username: str,
    db: DbDep,
    cfg: CfgDep,
    pool: PoolDep,
    providers: ProvidersDep,
    body: ProfileGenerateRequest | None = None,
) -> ProfileResponse:
    """Regenerate the narrative (user-triggered -- LLM calls cost money;
    GET never generates): fresh facts -> `render_profile_prompt` -> the
    chosen agent's `complete` with the read-only chat toolkit
    (docs/06-coach.md, "Narrative") -> `save_player_profile`. Responds
    with the same shape as `GET`. 409 when there are no analyzed games
    to describe.

    Scoped to the student's current level, never to a caller window:
    the narrative is the durable artifact other prompts embed, and one
    written over "the last 30 days" would be silently wrong the moment
    those 30 days moved. The level window moves only when the student's
    level does (docs/06-coach.md, "Window"), and time control remains
    the one scope the stored row is keyed by.
    """
    agent_id = cfg.coach.default_agent
    if body is not None and body.agent_id is not None:
        agent_id = body.agent_id
    time_class = body.time_class if body is not None else None
    provider = providers.get(agent_id)
    if provider is None:
        raise HTTPException(status_code=400, detail=f"unknown coach agent: {agent_id}")

    user = username.lower()
    # Threadpool: list_analyzed_games hits storage and build_report replays
    # every game with python-chess, mirroring /coach's own offload of the
    # same pipeline.
    facts = await run_in_threadpool(
        partial(_load_profile_facts, db, user, time_class=time_class)
    )
    if facts.games_covered == 0:
        detail = (
            f"no analyzed {time_class} games yet -- sync and analyze first"
            if time_class is not None
            else "no analyzed games yet -- sync and analyze first"
        )
        raise HTTPException(status_code=409, detail=detail)
    # Agentic (docs/06-coach.md, "Narrative"): the toolkit is pre-scoped
    # to this student and this control, so the run can read the
    # repertoire and pull games rather than paraphrasing the aggregates
    # it was handed. It is the same read-only toolkit chat uses -- the
    # engine analyst rides along on it when the pool is up, and the
    # narrative simply does not ask for positions when it is not.
    # Scoped to the same window as the facts (docs/06-coach.md, "Reading
    # a comparison"). This was unwindowed at first on the reasoning that
    # the narrative covers the control's whole history -- which confused
    # the storage *key* (time control alone) with the content's scope.
    # The narrative describes the windowed facts, so an unwindowed tool
    # answers a different question from the one the document is about:
    # get_opening_stats returned a 484-game London over 1,925 games into
    # a narrative whose every other figure covered 1,158, and
    # compare_groups returned a 968-game White split beside a facts block
    # stating 576. One document, one denominator.
    toolkit: ChatToolkit = ApiChatToolkit(
        db,
        user,
        since=facts.window_start,
        until=None,
        time_class=time_class,
        analyst=_build_analyst(pool, cfg) if pool is not None else None,
        # Seeds the compare tool's BH family with the splits the facts
        # already judged, so a question the run asks is weighed
        # alongside them rather than in a family of its own
        # (docs/06-coach.md, "Reading a comparison").
        prior_comparisons=facts.comparisons,
    )
    prompt = render_profile_prompt(facts, has_tools=True)

    try:
        advice = await provider.complete(prompt, toolkit=toolkit)
    except CoachProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    generated_at = await run_in_threadpool(
        partial(
            save_player_profile,
            db,
            user,
            time_class=time_class,
            agent_id=agent_id,
            prompt_version=PROFILE_PROMPT_VERSION,
            facts=facts,
            narrative=advice,
        )
    )
    return ProfileResponse(
        profile=facts.model_copy(update={"narrative": advice}),
        narrative=ProfileNarrative(
            agent_id=agent_id,
            prompt_version=PROFILE_PROMPT_VERSION,
            generated_at=generated_at,
            games_covered=facts.games_covered,
        ),
        # Just generated over exactly this scope, so the two agree by
        # construction — the UI's staleness hint stays quiet until games
        # are actually added.
        narrative_games_now=facts.games_covered,
    )


# --- Chat (docs/07-api.md "Chat"; docs/future-improvements/coach-chat.md
# --- is the design record) ---------------------------------------------


class ChatThreadCreateRequest(BaseModel):
    scope: ChatScope
    agent_id: str | None = None  # None -> config default agent
    game_id: str | None = None  # scope="game" only
    ply: int | None = None  # scope="game" only; requires analysis
    # The same window/time-control scope /report and /coach take, pinned
    # for the thread's life.
    since: int | None = None
    until: int | None = None
    time_class: TimeClass | None = None


@router.post("/players/{username}/chat/threads")
def start_chat_thread(
    username: str,
    body: ChatThreadCreateRequest,
    db: DbDep,
    cfg: CfgDep,
    providers: ProvidersDep,
) -> ChatThread:
    """Create a chat thread anchored to a game (optionally a ply) or to
    the report window; mints a uuid4 thread id.

    `scope="game"` requires `game_id` (400 without; 404 for an unknown
    game, or one belonging to another player); a `ply` anchor additionally
    requires analysis (409 unanalyzed, 400 out of range, mirroring
    `/explain`). `scope="report"` rejects `game_id`/`ply` (400). An
    unknown `agent_id` is 400; omitted means the configured default agent.
    """
    user = username.lower()
    agent_id = cfg.coach.default_agent if body.agent_id is None else body.agent_id
    if providers.get(agent_id) is None:
        raise HTTPException(status_code=400, detail=f"unknown coach agent: {agent_id}")

    if body.scope == "report":
        if body.game_id is not None or body.ply is not None:
            raise HTTPException(
                status_code=400,
                detail="scope=report does not take a game_id or ply",
            )
    else:  # scope == "game"
        if body.game_id is None:
            raise HTTPException(status_code=400, detail="scope=game requires game_id")
        game = get_game(db, body.game_id)
        if game is None or game.username != user:
            # A game id from another player's perspective 404s exactly like
            # an unknown one — the thread can never be created against a
            # game outside {username}.
            raise HTTPException(status_code=404, detail=f"unknown game: {body.game_id}")
        if body.ply is not None:
            if game.analysis is None:
                raise HTTPException(
                    status_code=409,
                    detail="no analysis for this game — analyze this game first",
                )
            try:
                build_move_context(game, game.analysis, game.opening, body.ply)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    return create_chat_thread(
        db,
        thread_id=str(uuid4()),
        username=user,
        agent_id=agent_id,
        scope=body.scope,
        game_id=body.game_id,
        ply=body.ply,
        since=body.since if body.since is not None else 0,
        until=body.until if body.until is not None else 0,
        time_class=body.time_class if body.time_class is not None else "",
    )


@router.get("/players/{username}/chat/threads")
def player_chat_threads(username: str, db: DbDep) -> list[ChatThreadSummary]:
    """The player's threads, most recently updated first."""
    return list_chat_threads(db, username.lower())


class ChatThreadDetail(ChatThread):
    """Thread + full transcript, oldest first."""

    messages: list[ChatMessage]


@router.get("/chat/threads/{thread_id}")
def chat_thread_detail(thread_id: str, db: DbDep) -> ChatThreadDetail:
    thread = get_chat_thread(db, thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail=f"unknown chat thread: {thread_id}")
    messages = list_chat_messages(db, thread_id)
    return ChatThreadDetail(**thread.model_dump(), messages=messages)


@router.delete("/chat/threads/{thread_id}", status_code=204)
def remove_chat_thread(thread_id: str, db: DbDep) -> None:
    if not delete_chat_thread(db, thread_id):
        raise HTTPException(status_code=404, detail=f"unknown chat thread: {thread_id}")


class ChatMessageRequest(BaseModel):
    text: str


class ChatSseError(BaseModel):
    """Mid-stream `error` SSE payload — too late for an HTTPException,
    mirroring ExplainError/EvalError."""

    message: str


@router.post("/chat/threads/{thread_id}/messages")
async def send_chat_message(
    thread_id: str,
    body: ChatMessageRequest,
    db: DbDep,
    cfg: CfgDep,
    pool: PoolDep,
    providers: ProvidersDep,
    inflight: ChatInFlightDep,
) -> EventSourceResponse:
    """SSE reply to one chat message: `text`/`tool` events mirroring coach
    `ChatEvent` while the agent works, then `done` with the full reply —
    persisted (with the new `provider_state`) before `done` is emitted.
    `error` on a mid-stream `CoachProviderError`: nothing persisted,
    `provider_state` cleared. 404 unknown thread; 409 while a reply is
    already streaming for this thread, or the thread is at the message cap.
    """
    if thread_id in inflight:
        raise HTTPException(
            status_code=409,
            detail="a reply is already streaming for this thread",
        )
    inflight.add(thread_id)
    entered_stream = False
    try:
        text = body.text.strip()
        if not text:
            raise HTTPException(
                status_code=400, detail="message text must not be blank"
            )
        thread = await run_in_threadpool(get_chat_thread, db, thread_id)
        if thread is None:
            raise HTTPException(
                status_code=404, detail=f"unknown chat thread: {thread_id}"
            )
        history = await run_in_threadpool(list_chat_messages, db, thread_id)
        if len(history) >= CHAT_MESSAGE_CAP:
            raise HTTPException(
                status_code=409,
                detail="this thread is at the message cap — start a new chat",
            )
        provider = providers.get(thread.agent_id)
        if provider is None:
            raise HTTPException(
                status_code=400, detail=f"unknown coach agent: {thread.agent_id}"
            )

        analyst = _build_analyst(pool, cfg) if pool is not None else None
        engine_available = analyst is not None
        if thread.scope == "game":
            system_context = await game_scope_context(db, thread, analyst)
        else:
            system_context = await report_scope_context(db, thread, engine_available)
        cached_turn = await cached_assistant_turn(db, thread)
        full_history = history if cached_turn is None else [cached_turn, *history]

        toolkit: ChatToolkit = ApiChatToolkit(
            db,
            thread.username,
            since=window_or_none(thread.since),
            until=window_or_none(thread.until),
            time_class=time_class_or_none(thread.time_class),
            analyst=analyst,
        )
        entered_stream = True
    finally:
        if not entered_stream:
            inflight.discard(thread_id)

    async def stream() -> AsyncIterator[dict[str, str]]:
        persisted = False
        try:
            # aclosing: a client disconnect closes this generator, which
            # must abort generation now, not at GC time.
            async with aclosing(
                provider.chat(
                    system_context=system_context,
                    history=full_history,
                    message=text,
                    toolkit=toolkit,
                    provider_state=thread.provider_state,
                )
            ) as events:
                async for event in events:
                    if event.type == "done":
                        now = int(time.time())
                        await run_in_threadpool(
                            append_chat_exchange,
                            db,
                            thread_id,
                            ChatMessage(role="user", content=text, created_at=now),
                            ChatMessage(
                                role="assistant", content=event.text, created_at=now
                            ),
                            event.provider_state,
                        )
                        persisted = True
                        yield {"event": "done", "data": event.model_dump_json()}
                        return
                    yield {"event": event.type, "data": event.model_dump_json()}
        except CoachProviderError as exc:
            # Too late for an HTTPException: events are already on the
            # wire, so the failure becomes an SSE event instead.
            yield {
                "event": "error",
                "data": ChatSseError(message=str(exc)).model_dump_json(),
            }
            return
        finally:
            # Client disconnect (GeneratorExit, caught by neither branch
            # above) and the provider-error path both leave `persisted`
            # False: the discarded turn may have reached the provider's
            # warm session, so the next turn must replay from the stored
            # transcript rather than resume a diverged one. The two
            # cleanups are independent: freeing the slot is sync and
            # cannot fail, so it happens first — a state-clear failure
            # must not leave the thread answering 409 forever. The clear
            # itself runs shielded because the disconnect path delivers
            # cancellation right here, and an unshielded await would be
            # cancelled before the write lands — on exactly the path the
            # clear exists for.
            inflight.discard(thread_id)
            if not persisted:
                await asyncio.shield(
                    run_in_threadpool(clear_chat_provider_state, db, thread_id)
                )

    return EventSourceResponse(stream())
