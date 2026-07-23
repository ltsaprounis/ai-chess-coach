"""HTTP routes (docs/07-api.md)."""

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from chess_coach.api.runs import AnalysisRun
from chess_coach.coach import (
    CoachProvider,
    CoachProviderError,
    build_report,
    render_prompt,
)
from chess_coach.config import AppConfig
from chess_coach.domain import (
    Game,
    GameDetail,
    GameSummary,
    OpeningStats,
    PlayerReport,
    Result,
    TimeClass,
)
from chess_coach.engine import AnalysisPool, EngineOptions, Progress
from chess_coach.ingestion import sync_games
from chess_coach.openings import OpeningBook
from chess_coach.storage import (
    Db,
    GameFilters,
    count_games_needing_analysis,
    games_missing_opening,
    games_needing_analysis,
    get_game,
    latest_game_time,
    list_analyzed_games,
    list_games,
    opening_stats,
    save_analysis,
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


def _provider(request: Request) -> CoachProvider:
    return cast(CoachProvider, request.app.state.provider)


DbDep = Annotated[Db, Depends(_db)]
BookDep = Annotated[OpeningBook, Depends(_book)]
CfgDep = Annotated[AppConfig, Depends(_cfg)]
PoolDep = Annotated[AnalysisPool | None, Depends(_pool)]
RunsDep = Annotated[dict[str, AnalysisRun], Depends(_runs)]
ProviderDep = Annotated[CoachProvider, Depends(_provider)]


class SyncResult(BaseModel):
    games_synced: int


@router.post("/players/{username}/sync")
async def sync_player(username: str, db: DbDep, book: BookDep) -> SyncResult:
    """Fetch new games from chess.com, store and classify them."""
    user = username.lower()
    since = latest_game_time(db, user)
    synced = 0
    async for batch in sync_games(user, since):
        upsert_games(db, batch)
        synced += len(batch)
    # One pass covers the new games and any stored-but-unclassified
    # backlog; book-less games simply stay unclassified.
    for game in games_missing_opening(db, user):
        opening = book.classify(game.san_moves)
        if opening is not None:
            set_opening(db, game.id, opening)
    return SyncResult(games_synced=synced)


@router.get("/players/{username}/openings")
def player_openings(username: str, db: DbDep) -> list[OpeningStats]:
    """Per-opening record over classified games, most-played first."""
    return opening_stats(db, username.lower())


@router.get("/players/{username}/games")
def player_games(
    username: str,
    db: DbDep,
    opening_eco: str | None = None,
    result: Result | None = None,
    time_class: TimeClass | None = None,
    analyzed: bool | None = None,
    limit: int = 100,
    offset: int = 0,
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
    limit: int | None = None  # bulk path only; capped by config


class AnalyzeResult(BaseModel):
    queued: int
    remaining: int  # unanalyzed games not covered by this run


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
    else:
        limit = cfg.engine.analyze_limit
        if body is not None and body.limit is not None:
            limit = min(body.limit, cfg.engine.analyze_limit)
        games = games_needing_analysis(db, user, cfg.engine.depth, limit)

    remaining = max(
        0, count_games_needing_analysis(db, user, cfg.engine.depth) - len(games)
    )
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
        save_analysis(db, analysis)
        run.games_done += 1
        run.publish(run.event("game_done"))

    results = await asyncio.gather(
        *(analyze_one(game) for game in games), return_exceptions=True
    )
    failures = [r for r in results if isinstance(r, BaseException)]
    run.finished = True
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


class CoachResponse(BaseModel):
    prompt: str
    advice: str


@router.get("/players/{username}/report")
def player_report(username: str, db: DbDep) -> PlayerReport:
    """Aggregated stats over the player's analyzed games."""
    user = username.lower()
    return build_report(user, list_analyzed_games(db, user))


@router.post("/players/{username}/coach")
async def coach_player(
    username: str, db: DbDep, provider: ProviderDep
) -> CoachResponse:
    """Build the report, render the prompt, and ask the coach LLM."""
    user = username.lower()
    report = build_report(user, list_analyzed_games(db, user))
    if report.games_analyzed == 0:
        raise HTTPException(
            status_code=409,
            detail="no analyzed games yet — sync and analyze first",
        )
    prompt = render_prompt(report)
    try:
        advice = await provider.complete(prompt)
    except CoachProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return CoachResponse(prompt=prompt, advice=advice)
