"""Storage component — see docs/03-storage.md."""

from chess_coach.storage.analyses import save_analysis
from chess_coach.storage.db import Db, open_db
from chess_coach.storage.explanations import get_explanation, save_explanation
from chess_coach.storage.games import (
    GameFilters,
    count_games,
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
from chess_coach.storage.repertoire import list_repertoire_games
from chess_coach.storage.reports import CachedReport, ReportKey, get_report, save_report

__all__ = [
    "CachedReport",
    "Db",
    "GameFilters",
    "ReportKey",
    "count_games",
    "count_games_needing_analysis",
    "games_missing_opening",
    "games_needing_analysis",
    "get_explanation",
    "get_game",
    "get_report",
    "latest_game_time",
    "list_analyzed_games",
    "list_games",
    "list_players",
    "list_repertoire_games",
    "open_db",
    "opening_stats",
    "save_analysis",
    "save_explanation",
    "save_report",
    "set_opening",
    "upsert_games",
]
