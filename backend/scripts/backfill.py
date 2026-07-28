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

Progress reporting has two sources, and only one of them is trusted:

- The SSE stream the Coach page already watches
  (GET .../analyze/progress) supplies the live x/X inside a batch —
  one `game_done` event per finished game. It is a convenience: if it
  drops, the loop falls back to polling the analyze endpoint, whose
  409 is the authoritative "still running".
- The batch-boundary counts come from the endpoint itself. Every
  request reports `queued + remaining` = games still needing analysis
  in this scope, so the drop between two batches is exactly how many
  games were *saved* — not how many were enqueued. Failed games stay
  in the scope and get re-enqueued, so counting enqueues would
  over-report, and a scope that never drains would otherwise spin
  until morning (hence the stall guard).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import cast

_TIME_CLASSES = ("rapid", "blitz", "bullet", "daily")

# sse-starlette sends a keepalive comment every 15s even mid-search, so
# silence this long means the connection died, not that a game is slow.
_STREAM_TIMEOUT = 60.0

# Plain requests are answered immediately (the analysis itself runs in
# the background), so a slow reply means something is wrong.
_REQUEST_TIMEOUT = 30.0

# Rewrite the live line in place: carriage return + erase to end of line.
_CLEAR = "\r\x1b[K"


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
    parser.add_argument(
        "--max-games",
        type=int,
        help="stop after this many games instead of draining the scope "
        "(a smoke test; the rest of the scope stays for a later run)",
    )
    parser.add_argument(
        "--log-every",
        type=float,
        default=30.0,
        help="seconds between progress lines when stdout is a file "
        "(default 30; a terminal updates one line live instead)",
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
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT) as response:
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


def _duration(seconds: float) -> str:
    hours, rest = divmod(int(max(seconds, 0)), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _scope_label(args: argparse.Namespace) -> str:
    parts: list[str] = [cast("str", args.username)]
    if args.time_class is not None:
        parts.append(cast("str", args.time_class))
    if args.since is not None:
        parts.append(f"since {args.since}")
    if args.until is not None:
        parts.append(f"until {args.until}")
    if args.since is None and args.until is None:
        parts.append("whole archive")
    return " · ".join(parts)


class _Reporter:
    """Progress for a job that can run for hours, in both places it is
    read: a terminal (one line, rewritten live) and a log file (the
    same line, throttled to --log-every so a night's run stays
    readable). Every write is flushed — overnight output is usually
    piped to `tee`, where block buffering would hide progress for
    hours.
    """

    def __init__(self, total: int, log_every: float) -> None:
        self.total = total  # games needing analysis when we started
        self.done = 0  # server-confirmed, from the batch-boundary deltas
        self.batch = 0
        self.batch_size = 0
        self.batch_done = 0  # from the SSE stream; reset each batch
        self._log_every = log_every
        self._tty = sys.stdout.isatty()
        self._started = time.monotonic()
        self._batch_started = self._started
        self._last_write = 0.0
        self._live_line = False

    def log(self, text: str) -> None:
        """A line that stays: batch boundaries, warnings, the summary."""
        if self._live_line:
            print(_CLEAR, end="")
            self._live_line = False
        print(f"[{datetime.now():%H:%M:%S}] {text}", flush=True)
        self._last_write = time.monotonic()

    def update(self) -> None:
        """The live x/X line. On a terminal it replaces itself once a
        second; redirected, it prints a fresh line every --log-every."""
        now = time.monotonic()
        if now - self._last_write < (1.0 if self._tty else self._log_every):
            return
        self._last_write = now
        stamp = f"[{datetime.now():%H:%M:%S}] {self._status()}"
        if self._tty:
            print(f"{_CLEAR}{stamp}", end="", flush=True)
            self._live_line = True
        else:
            print(stamp, flush=True)

    def start_batch(self, number: int, size: int, remaining: int) -> None:
        self.batch = number
        self.batch_size = size
        self.batch_done = 0
        self._batch_started = time.monotonic()
        self.log(f"batch {number}: analyzing {size} · {remaining:,} left after it")

    def finish_batch(self, completed: int) -> None:
        """`completed` is the server-confirmed drop in the scope, which
        is below the batch size when games failed — say so, because the
        next batch will pick those same games up again."""
        elapsed = time.monotonic() - self._batch_started
        self.done += max(completed, 0)
        self.batch_done = 0
        failed = self.batch_size - completed
        detail = (
            f"{completed} analyzed"
            if failed <= 0
            else (
                f"{completed} of {self.batch_size} analyzed, {failed} still unanalyzed"
            )
        )
        self.log(
            f"batch {self.batch} done: {detail} in {_duration(elapsed)} · "
            f"{self._overall()}"
        )

    def sync(self, games_done: int, games_total: int) -> None:
        """Adopt the run's own counters from an SSE snapshot (the stream
        may attach after the first games are already finished)."""
        self.batch_done = games_done
        if games_total:
            self.batch_size = games_total

    def game_done(self) -> None:
        self.batch_done += 1
        self.update()

    def summary(self, batches: int) -> str:
        return (
            f"done: {self.done:,} game(s) analyzed in {batches} batch(es) "
            f"over {_duration(time.monotonic() - self._started)}"
        )

    def _status(self) -> str:
        batch = f"batch {self.batch} · games {self.batch_done}/{self.batch_size}"
        return f"{batch} · {self._overall()}"

    def _overall(self) -> str:
        done = min(self.done + self.batch_done, self.total)
        percent = 100 * done // self.total if self.total else 100
        text = f"overall {done:,}/{self.total:,} ({percent}%)"
        # Three games in is enough for the average to stop being noise,
        # and the first ETA is the number the user is actually waiting
        # for ("is this a two-hour job or a two-day one?").
        if done >= 3:
            per_game = (time.monotonic() - self._started) / done
            eta = _duration(per_game * (self.total - done))
            text += f" · {per_game:.1f}s/game · ETA {eta}"
        return text


def _sse_events(lines: Iterable[bytes]) -> Iterator[tuple[str, dict[str, object]]]:
    """Minimal text/event-stream reader: yields (event name, data)."""
    name = ""
    data = ""
    for raw in lines:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:  # blank line terminates an event
            if data:
                try:
                    payload = cast("dict[str, object]", json.loads(data))
                except ValueError:
                    payload = {}
                yield name or "message", payload
            name, data = "", ""
        elif line.startswith(":"):  # keepalive comment
            continue
        elif line.startswith("event:"):
            name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data += line[len("data:") :].strip()


def _follow_batch(base_url: str, username: str, reporter: _Reporter) -> bool:
    """Print per-game progress from the run's SSE stream.

    Returns True once the stream says the run finished, False if the
    stream was never available or dropped — the caller then falls back
    to polling the analyze endpoint, which is the authoritative signal.
    """
    url = f"{base_url}/api/players/{username}/analyze/progress"
    request = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
    try:
        with urllib.request.urlopen(request, timeout=_STREAM_TIMEOUT) as response:
            for name, data in _sse_events(response):
                if name == "game_done":
                    reporter.game_done()
                elif name == "progress":
                    reporter.update()  # keeps the clock moving mid-game
                elif name == "snapshot":
                    reporter.sync(
                        _as_int(data, "games_done"), _as_int(data, "games_total")
                    )
                    if data.get("finished") is True:
                        return True
                elif name == "run_done":
                    return True
                elif name == "run_failed":
                    reporter.log(
                        "the server reported failures in this batch — see the "
                        "API log; the games it could not analyze stay in scope"
                    )
                    return True
    except OSError as err:  # includes HTTPError (404) and read timeouts
        reporter.log(f"progress stream unavailable ({err}) — polling instead")
    return False


def _wait_for_batch(
    url: str, scope: dict[str, object], reporter: _Reporter, poll_seconds: float
) -> None:
    """Fallback when the SSE stream is gone: the analyze endpoint's 409
    is the batch's own liveness signal."""
    while True:
        time.sleep(poll_seconds)
        status, _ = _post(url, {**scope, "limit": 0})
        if status != 409:
            return
        reporter.update()


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    base_url = cast("str", args.base_url).rstrip("/")
    url = f"{base_url}/api/players/{args.username}/analyze"
    scope: dict[str, object] = {}
    if args.since is not None:
        scope["since"] = _epoch(cast("str", args.since))
    if args.until is not None:
        scope["until"] = _epoch(cast("str", args.until))
    if args.time_class is not None:
        scope["time_class"] = args.time_class

    try:
        # The limit=0 probe costs nothing and gives the run a
        # denominator, so every later line can say x/X and an ETA.
        status, payload = _post(url, {**scope, "limit": 0})
        if args.dry_run:
            if status == 409:
                print("an analysis run is already active for this player")
                return 1
            if status != 202:
                print(f"unexpected response {status}: {payload}", file=sys.stderr)
                return 1
            print(f"{_as_int(payload, 'remaining'):,} game(s) in scope need analysis")
            return 0
        waited = False
        while status == 409:  # someone else's run (the UI, another shell)
            if not waited:
                print("an analysis run is already active — waiting for it", flush=True)
                waited = True
            time.sleep(args.poll_seconds)
            status, payload = _post(url, {**scope, "limit": 0})
        if status != 202:
            print(f"unexpected response {status}: {payload}", file=sys.stderr)
            return 1

        total = _as_int(payload, "remaining")
        reporter = _Reporter(total, cast("float", args.log_every))
        reporter.log(f"scope: {_scope_label(args)} · {base_url}")
        if total == 0:
            reporter.log("nothing to do — every game in this scope is analyzed")
            return 0
        reporter.log(f"{total:,} game(s) need analysis")

        batches = 0
        stalls = 0
        in_scope = total
        budget = cast("int | None", args.max_games)
        while True:
            body = dict(scope)
            if budget is not None:
                body["limit"] = budget
            status, payload = _post(url, body)
            if status == 409:  # a batch is still running — wait it out
                time.sleep(args.poll_seconds)
                continue
            if status != 202:
                print(f"unexpected response {status}: {payload}", file=sys.stderr)
                return 1
            queued = _as_int(payload, "queued")
            remaining = _as_int(payload, "remaining")

            # Everything still needing analysis in this scope, measured
            # by the server: what it dropped since the last request is
            # what actually got saved.
            was_in_scope, in_scope = in_scope, queued + remaining
            if batches:
                reporter.finish_batch(was_in_scope - in_scope)
                stalls = stalls + 1 if in_scope >= was_in_scope else 0
                if stalls >= 2:
                    print(
                        "two batches in a row analyzed nothing — the server is "
                        "failing these games (check the API log) — stopping "
                        "rather than retrying them all night",
                        file=sys.stderr,
                    )
                    return 1

            if queued == 0:
                if remaining:
                    print(
                        f"nothing enqueued but {remaining} game(s) remain — "
                        "another client may hold the scope; re-run to resume",
                        file=sys.stderr,
                    )
                    return 1
                reporter.log(reporter.summary(batches))
                return 0

            batches += 1
            reporter.start_batch(batches, queued, remaining)
            if not _follow_batch(base_url, cast("str", args.username), reporter):
                _wait_for_batch(url, scope, reporter, args.poll_seconds)
            if budget is not None:
                budget -= queued
                if budget <= 0:
                    status, payload = _post(url, {**scope, "limit": 0})
                    if status == 202:
                        reporter.finish_batch(in_scope - _as_int(payload, "remaining"))
                    reporter.log(f"{reporter.summary(batches)} (--max-games reached)")
                    return 0
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
