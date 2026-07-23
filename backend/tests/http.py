"""Typed wrappers around TestClient requests.

starlette's TestClient methods are partially untyped under pyright
strict; these confine the casts to one place.
"""

from typing import cast

import httpx
from fastapi.testclient import TestClient


def get(
    client: TestClient, url: str, params: dict[str, str] | None = None
) -> httpx.Response:
    return cast(httpx.Response, client.get(url, params=params))  # pyright: ignore[reportUnknownMemberType]


def post(
    client: TestClient, url: str, json: dict[str, object] | None = None
) -> httpx.Response:
    return cast(httpx.Response, client.post(url, json=json))  # pyright: ignore[reportUnknownMemberType]
