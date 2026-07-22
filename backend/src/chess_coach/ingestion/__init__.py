"""Ingestion component — see docs/02-ingestion.md."""

from chess_coach.ingestion.client import IngestionError, UnknownUserError
from chess_coach.ingestion.service import get_archives, sync_games

__all__ = [
    "IngestionError",
    "UnknownUserError",
    "get_archives",
    "sync_games",
]
