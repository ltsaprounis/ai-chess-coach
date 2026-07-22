"""HTTP plumbing for the chess.com public API (docs/02-ingestion.md)."""

import asyncio
import logging

import httpx

BASE_URL = "https://api.chess.com/pub"
USER_AGENT = "ai-chess-coach (github.com/ltsaprounis/ai-chess-coach)"
_RETRYABLE_STATUSES = frozenset((429, 502, 503))
_MAX_ATTEMPTS = 4

logger = logging.getLogger(__name__)


class IngestionError(Exception):
    """A chess.com request kept failing after retries."""


class UnknownUserError(IngestionError):
    """The username does not exist on chess.com."""

    def __init__(self, username: str) -> None:
        super().__init__(f"chess.com user not found: {username}")
        self.username = username


def make_client() -> httpx.AsyncClient:
    """Client with the User-Agent chess.com requires to not throttle."""
    return httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=30.0)


async def get_json(client: httpx.AsyncClient, url: str) -> object:
    """GET a JSON document, retrying with backoff on 429/502/503."""
    for attempt in range(_MAX_ATTEMPTS):
        response = await client.get(url)
        if response.status_code in _RETRYABLE_STATUSES and attempt < _MAX_ATTEMPTS - 1:
            delay = float(response.headers.get("Retry-After", 2**attempt))
            logger.warning(
                "chess.com returned %d for %s; retrying in %.0fs",
                response.status_code,
                url,
                delay,
            )
            await asyncio.sleep(delay)
            continue
        response.raise_for_status()
        return response.json()
    raise IngestionError(f"unreachable: no response for {url}")
