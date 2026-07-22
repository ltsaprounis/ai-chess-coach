"""HTTP routes (docs/07-api.md)."""

from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from chess_coach.domain import GameDetail, GameSummary, OpeningStats, Result, TimeClass
from chess_coach.ingestion import sync_games
from chess_coach.openings import OpeningBook
from chess_coach.storage import (
    Db,
    GameFilters,
    games_missing_opening,
    get_game,
    latest_game_time,
    list_games,
    opening_stats,
    set_opening,
    upsert_games,
)

router = APIRouter()


def _db(request: Request) -> Db:
    return cast(Db, request.app.state.db)


def _book(request: Request) -> OpeningBook:
    return cast(OpeningBook, request.app.state.book)


DbDep = Annotated[Db, Depends(_db)]
BookDep = Annotated[OpeningBook, Depends(_book)]


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
