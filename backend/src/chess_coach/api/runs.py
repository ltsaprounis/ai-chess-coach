"""Per-player analysis run tracking and SSE fanout (docs/07-api.md)."""

import asyncio
import time
from typing import Literal

from pydantic import BaseModel

from chess_coach.engine import Progress

RunEventType = Literal["snapshot", "progress", "game_done", "run_done", "run_failed"]

# Finished runs kept in the registry across all usernames before a sweep
# starts evicting the oldest ones (see `evict_finished`). Generous relative
# to how many distinct usernames a single process realistically sees
# between restarts (the player switcher), so a run that just finished is
# never at real risk of eviction before a client's next poll.
MAX_FINISHED_RUNS = 50


class RunEvent(BaseModel):
    type: RunEventType
    games_total: int
    games_done: int
    finished: bool = False
    progress: Progress | None = None


class AnalysisRun:
    """State and subscriber queues for one player's analysis run."""

    def __init__(self, games_total: int) -> None:
        self.games_total = games_total
        self.games_done = 0
        self.finished = False
        # Set together with `finished` by `mark_finished`; None while the
        # run is active. The registry's eviction sweep orders by this to
        # decide which finished runs are oldest.
        self.finished_at: float | None = None
        self.task: asyncio.Task[None] | None = None
        self._subscribers: set[asyncio.Queue[RunEvent]] = set()

    def mark_finished(self) -> None:
        """Flip to finished and stamp when, for the registry's eviction
        sweep (`evict_finished`) to age out old entries."""
        self.finished = True
        self.finished_at = time.time()

    def event(self, type_: RunEventType, progress: Progress | None = None) -> RunEvent:
        return RunEvent(
            type=type_,
            games_total=self.games_total,
            games_done=self.games_done,
            finished=self.finished,
            progress=progress,
        )

    def publish(self, event: RunEvent) -> None:
        for queue in self._subscribers:
            queue.put_nowait(event)

    def subscribe(self) -> asyncio.Queue[RunEvent]:
        queue: asyncio.Queue[RunEvent] = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[RunEvent]) -> None:
        self._subscribers.discard(queue)


def evict_finished(runs: dict[str, AnalysisRun], keep: int = MAX_FINISHED_RUNS) -> None:
    """Drop the oldest finished runs once more than `keep` are held.

    Keeps `app.state.runs` bounded across a long-lived process even as
    many distinct usernames get analyzed (the player switcher) — without
    this, a finished run lingers under its username key forever, since a
    dict key is only ever replaced by that same username starting another
    run.

    Active runs (`not run.finished`) are never touched, no matter how far
    over `keep` the registry grows: a run whose task is still going must
    stay queryable, and the one-run-per-player guard in `analyze_player`
    depends on it still being there. Call this right before registering a
    new run (`routes.analyze_player`); a run that isn't finished yet has
    nothing to sweep.
    """
    finished = [(username, run) for username, run in runs.items() if run.finished]
    if len(finished) <= keep:
        return
    finished.sort(key=lambda item: item[1].finished_at or 0.0)
    for username, _ in finished[: len(finished) - keep]:
        del runs[username]
