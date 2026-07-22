"""Storage component — see docs/03-storage.md."""

from chess_coach.storage.analyses import list_analyses, save_analysis
from chess_coach.storage.db import Db, open_db
from chess_coach.storage.games import (
    GameFilters,
    games_needing_analysis,
    get_game,
    latest_game_time,
    list_games,
    set_opening,
    upsert_games,
)

__all__ = [
    "Db",
    "GameFilters",
    "games_needing_analysis",
    "get_game",
    "latest_game_time",
    "list_analyses",
    "list_games",
    "open_db",
    "save_analysis",
    "set_opening",
    "upsert_games",
]
