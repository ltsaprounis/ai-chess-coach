"""Backfill engine analysis through the public HTTP API.

An HTTP client only — no database access, no chess_coach imports;
every decision is delegated to POST /api/players/{u}/analyze and its
window filters (docs/fixes-2026-07/07-analysis-coverage.md). Run it
via `make backfill ARGS="..."`, which also keeps a Mac awake for
overnight jobs.

The loop: 202 starts a batch (the server caps its size at
engine.analyze_limit), 409 means a batch is still running (poll
again), and 202 with queued=0 means the scope is drained. --dry-run
sends limit=0, which the server treats as a pure probe: nothing is
enqueued, no run starts, and `remaining` answers "how much is left
in this scope?".
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import cast

_TIME_CLASSES = ("rapid", "blitz", "bullet", "daily")


def _epoch(day: str) -> int:
    """YYYY-MM-DD → epoch seconds at UTC midnight (deterministic
    regardless of the machine's timezone; a few hours of boundary
    skew is irrelevant at backfill scale)."""
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze a player's stored games through the running "
        "backend, in batches, until the requested scope is drained.",
    )
    parser.add_argument("username", help="chess.com username, as stored")
    parser.add_argument("--since", help="YYYY-MM-DD (inclusive, UTC midnight)")
    parser.add_argument("--until", help="YYYY-MM-DD (exclusive, UTC midnight)")
    parser.add_argument("--time-class", choices=_TIME_CLASSES)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=10.0,
        help="delay between polls while a batch is running (default 10)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report how many games the scope still needs analyzed, enqueue nothing",
    )
    return parser.parse_args(argv)


def _post(url: str, body: dict[str, object]) -> tuple[int, dict[str, object]]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, cast(
                "dict[str, object]", json.loads(response.read())
            )
    except urllib.error.HTTPError as err:
        try:
            payload = cast("dict[str, object]", json.loads(err.read()))
        except (ValueError, OSError):
            payload = {}
        return err.code, payload


def _as_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    return value if isinstance(value, int) else 0


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    url = f"{args.base_url.rstrip('/')}/api/players/{args.username}/analyze"
    scope: dict[str, object] = {}
    if args.since is not None:
        scope["since"] = _epoch(args.since)
    if args.until is not None:
        scope["until"] = _epoch(args.until)
    if args.time_class is not None:
        scope["time_class"] = args.time_class

    try:
        if args.dry_run:
            status, payload = _post(url, {**scope, "limit": 0})
            if status == 409:
                print("an analysis run is already active for this player")
                return 1
            if status != 202:
                print(f"unexpected response {status}: {payload}", file=sys.stderr)
                return 1
            print(f"{_as_int(payload, 'remaining')} game(s) in scope need analysis")
            return 0

        started = time.monotonic()
        batches = 0
        analyzed = 0
        while True:
            status, payload = _post(url, dict(scope))
            if status == 409:  # a batch is still running — wait it out
                time.sleep(args.poll_seconds)
                continue
            if status != 202:
                print(f"unexpected response {status}: {payload}", file=sys.stderr)
                return 1
            queued = _as_int(payload, "queued")
            remaining = _as_int(payload, "remaining")
            if queued == 0:
                minutes = (time.monotonic() - started) / 60
                if remaining:
                    print(
                        f"nothing enqueued but {remaining} game(s) remain — "
                        "another client may hold the scope; re-run to resume",
                        file=sys.stderr,
                    )
                    return 1
                print(
                    f"done: {analyzed} game(s) analyzed in {batches} "
                    f"batch(es) over {minutes:.0f} min"
                )
                return 0
            batches += 1
            analyzed += queued
            print(f"batch {batches}: analyzing {queued}, {remaining} left after it")
            time.sleep(args.poll_seconds)
    except urllib.error.URLError as err:
        print(
            f"backend unreachable at {args.base_url} ({err.reason}) — "
            "is `make dev-api` running? Re-run to resume; finished "
            "batches are already saved.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print(
            "\ninterrupted — the current batch continues server-side and "
            "its results are saved; re-run to resume where it left off."
        )
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
