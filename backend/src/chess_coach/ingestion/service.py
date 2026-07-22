"""Public ingestion operations (docs/02-ingestion.md)."""

import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
from pydantic import ValidationError

from chess_coach.domain import Game
from chess_coach.ingestion.client import (
    BASE_URL,
    UnknownUserError,
    get_json,
    make_client,
)
from chess_coach.ingestion.models import ArchiveIndex, MonthArchive, RawGame
from chess_coach.ingestion.normalize import normalize_game

logger = logging.getLogger(__name__)

_MONTH_RE = re.compile(r"/games/(\d{4})/(\d{2})$")


async def get_archives(
    username: str, *, client: httpx.AsyncClient | None = None
) -> list[str]:
    """Monthly archive URLs for a user, oldest first."""
    if client is None:
        async with make_client() as owned:
            return await get_archives(username, client=owned)

    url = f"{BASE_URL}/player/{username.lower()}/games/archives"
    try:
        payload = await get_json(client, url)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise UnknownUserError(username) from exc
        raise
    return ArchiveIndex.model_validate(payload).archives


async def sync_games(
    username: str,
    since: int | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> AsyncIterator[list[Game]]:
    """Yield normalized game batches, one per monthly archive.

    `since` (epoch seconds) skips months that ended before it and,
    within newer months, games that ended at or before it.
    """
    if client is None:
        async with make_client() as owned:
            async for batch in sync_games(username, since, client=owned):
                yield batch
        return

    for archive_url in await get_archives(username, client=client):
        month_end = _month_end(archive_url)
        if since is not None and month_end is not None and month_end <= since:
            continue
        month = MonthArchive.model_validate(await get_json(client, archive_url))
        batch = _normalize_month(month, username, since, archive_url)
        if batch:
            yield batch


def _normalize_month(
    month: MonthArchive, username: str, since: int | None, archive_url: str
) -> list[Game]:
    batch: list[Game] = []
    for entry in month.games:
        try:
            raw = RawGame.model_validate(entry)
        except ValidationError:
            logger.warning("malformed game entry in %s; skipping", archive_url)
            continue
        game = normalize_game(raw, username)
        if game is None:
            continue
        if since is not None and game.end_time <= since:
            continue
        batch.append(game)
    return batch


def _month_end(archive_url: str) -> int | None:
    """Epoch seconds of the first instant after the archive's month."""
    match = _MONTH_RE.search(archive_url)
    if match is None:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return int(datetime(year, month, 1, tzinfo=UTC).timestamp())
