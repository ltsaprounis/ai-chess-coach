"""HTTP routes (docs/07-api.md)."""

from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from chess_coach.domain import GameDetail, GameSummary, Result, TimeClass
from chess_coach.ingestion import sync_games
from chess_coach.storage import (
    Db,
    GameFilters,
    get_game,
    latest_game_time,
    list_games,
    upsert_games,
)

router = APIRouter()


def _db(request: Request) -> Db:
    return cast(Db, request.app.state.db)


DbDep = Annotated[Db, Depends(_db)]


class SyncResult(BaseModel):
    games_synced: int


@router.post("/players/{username}/sync")
async def sync_player(username: str, db: DbDep) -> SyncResult:
    """Fetch new games from chess.com and store them."""
    user = username.lower()
    since = latest_game_time(db, user)
    synced = 0
    async for batch in sync_games(user, since):
        upsert_games(db, batch)
        synced += len(batch)
    return SyncResult(games_synced=synced)


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
