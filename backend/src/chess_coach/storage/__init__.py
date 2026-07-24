"""Storage component — see docs/03-storage.md."""

from chess_coach.storage.analyses import list_analyses, save_analysis
from chess_coach.storage.db import Db, open_db
from chess_coach.storage.explanations import get_explanation, save_explanation
from chess_coach.storage.games import (
    GameFilters,
    count_games_needing_analysis,
    games_missing_opening,
    games_needing_analysis,
    get_game,
    latest_game_time,
    list_analyzed_games,
    list_games,
    list_players,
    opening_stats,
    set_opening,
    upsert_games,
)

__all__ = [
    "Db",
    "GameFilters",
    "count_games_needing_analysis",
    "games_missing_opening",
    "games_needing_analysis",
    "get_explanation",
    "get_game",
    "latest_game_time",
    "list_analyses",
    "list_analyzed_games",
    "list_games",
    "list_players",
    "open_db",
    "opening_stats",
    "save_analysis",
    "save_explanation",
    "set_opening",
    "upsert_games",
]
