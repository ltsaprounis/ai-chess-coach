"""Per-player analysis run tracking and SSE fanout (docs/07-api.md)."""

import asyncio
from typing import Literal

from pydantic import BaseModel

from chess_coach.engine import Progress

RunEventType = Literal["snapshot", "progress", "game_done", "run_done", "run_failed"]


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
        self.task: asyncio.Task[None] | None = None
        self._subscribers: set[asyncio.Queue[RunEvent]] = set()

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
