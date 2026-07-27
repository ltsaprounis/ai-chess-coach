"""HTTP routes (docs/07-api.md)."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from starlette.concurrency import run_in_threadpool

from chess_coach.api.runs import MAX_FINISHED_RUNS, AnalysisRun, evict_finished
from chess_coach.coach import (
    PROMPT_VERSION,
    CoachProvider,
    CoachProviderError,
    PositionAnalystFn,
    build_move_context,
    build_report,
    render_explain_prompt,
    render_prompt,
)
from chess_coach.config import AppConfig
from chess_coach.domain import (
    EvalLine,
    Game,
    GameDetail,
    GameSummary,
    LlmProvider,
    OpeningStats,
    PlayerReport,
    PlayerSummary,
    Result,
    TimeClass,
)
from chess_coach.engine import AnalysisPool, EngineError, EngineOptions, Progress
from chess_coach.ingestion import sync_games
from chess_coach.openings import OpeningBook
from chess_coach.storage import (
    Db,
    GameFilters,
    ReportKey,
    count_games,
    count_games_needing_analysis,
    games_missing_opening,
    games_needing_analysis,
    get_explanation,
    get_game,
    get_report,
    latest_game_time,
    list_analyzed_games,
    list_games,
    list_players,
    opening_stats,
    save_analysis,
    save_explanation,
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


DbDep = Annotated[Db, Depends(_db)]
BookDep = Annotated[OpeningBook, Depends(_book)]
CfgDep = Annotated[AppConfig, Depends(_cfg)]
PoolDep = Annotated[AnalysisPool | None, Depends(_pool)]
RunsDep = Annotated[dict[str, AnalysisRun], Depends(_runs)]
ProvidersDep = Annotated[dict[str, CoachProvider], Depends(_providers)]


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
    # whole table (docs/CODEBASE-SCAN-2026-07.md, finding 10).
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
        games: list[Game] = [
            game
            for game_id in body.game_ids
            if (game := get_game(db, game_id)) is not None
        ]
        remaining = max(
            0, count_games_needing_analysis(db, user, cfg.engine.depth) - len(games)
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
        # docs/fixes-2026-07/07-analysis-coverage.md. The active-run
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
        await run_in_threadpool(save_analysis, db, analysis)
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
    prompt = render_explain_prompt(ctx, lines)

    # The API layer's PositionAnalystFn implementation, wrapping the engine
    # pool — this is where coach meets engine; they never import each other.
    async def _analyst(fen: str) -> list[EvalLine]:
        return await pool.eval_lines(fen, cfg.engine.depth, cfg.engine.multipv)

    analyst: PositionAnalystFn = _analyst

    async def stream() -> AsyncIterator[dict[str, str]]:
        chunks: list[str] = []
        try:
            # aclosing: a client disconnect closes this generator, which
            # stops generation immediately and caches nothing.
            async with aclosing(provider.explain(prompt, analyst)) as events:
                async for event in events:
                    if event.type == "text":
                        chunks.append(event.text)
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
    """Aggregated stats over the player's analyzed games.

    `since`/`until` (epoch seconds) restrict to a time window;
    `time_class` restricts to one time control.
    """
    user = username.lower()
    games_in_scope = count_games(
        db, user, since=since, until=until, time_class=time_class
    )
    return build_report(
        user,
        list_analyzed_games(db, user, since=since, until=until, time_class=time_class),
        time_class=time_class,
        requested_since=since,
        requested_until=until,
        games_in_scope=games_in_scope,
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
        games_in_scope = count_games(
            db, user, since=since, until=until, time_class=time_class
        )
        report = build_report(
            user,
            games,
            time_class=time_class,
            requested_since=since,
            requested_until=until,
            games_in_scope=games_in_scope,
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

        async def _analyst(fen: str) -> list[EvalLine]:
            return await pool.eval_lines(fen, cfg.engine.depth, cfg.engine.multipv)

        analyst = _analyst

    try:
        advice = await provider.complete(prompt, analyst)
    except CoachProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

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
