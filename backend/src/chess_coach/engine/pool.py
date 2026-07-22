"""Engine worker pool (docs/04-engine.md)."""

import asyncio
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from chess_coach.domain import Game, GameAnalysis
from chess_coach.engine.analysis import EngineOptions, analyze_game
from chess_coach.engine.uci import Engine, PositionEval


class Progress(BaseModel):
    game_id: str
    ply: int
    total_plies: int


ProgressCallback = Callable[[Progress], None]


class AnalysisPool:
    """N engine processes behind a queue; analyze_game checks one out."""

    def __init__(self, engines: list[Engine]) -> None:
        self._engines = engines
        self._idle: asyncio.Queue[Engine] = asyncio.Queue()
        for engine in engines:
            self._idle.put_nowait(engine)

    async def analyze_game(
        self,
        game: Game,
        opts: EngineOptions,
        on_progress: ProgressCallback | None = None,
    ) -> GameAnalysis:
        engine = await self._idle.get()
        try:
            total = len(game.san_moves)
            seen = 0

            async def evaluate(fen: str) -> PositionEval:
                nonlocal seen
                result = await engine.evaluate(fen, opts.depth)
                seen += 1
                if on_progress is not None:
                    # seen counts positions (plies + 1); clamp for display
                    on_progress(
                        Progress(
                            game_id=game.id,
                            ply=min(seen, total),
                            total_plies=total,
                        )
                    )
                return result

            return await analyze_game(game, opts, evaluate)
        finally:
            self._idle.put_nowait(engine)

    async def close(self) -> None:
        for engine in self._engines:
            await engine.close()


async def create_pool(bin_path: Path, workers: int) -> AnalysisPool:
    engines = [await Engine.open(bin_path) for _ in range(workers)]
    return AnalysisPool(engines)
