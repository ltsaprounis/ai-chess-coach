"""Engine worker pool (docs/04-engine.md)."""

import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from pathlib import Path

import chess
import chess.engine
from pydantic import BaseModel

from chess_coach.domain import Game, GameAnalysis
from chess_coach.engine.analysis import EngineOptions, analyze_game
from chess_coach.engine.uci import Engine, PositionEval


class Progress(BaseModel):
    game_id: str
    ply: int
    total_plies: int


class LiveEval(BaseModel):
    """One live-analysis event per depth reached (docs/04-engine.md)."""

    depth: int
    eval_cp: int | None  # white's perspective, like MoveEval
    eval_mate: int | None  # signed moves to mate, white's view
    pv_san: list[str]  # principal variation from this position, SAN


ProgressCallback = Callable[[Progress], None]

_PV_SAN_CAP = 10  # plenty for display; PVs can run much longer


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

    def stream_eval(self, fen: str, depth: int) -> AsyncGenerator[LiveEval, None]:
        """Live single-position eval: one `LiveEval` per new depth.

        Parses the FEN eagerly so an invalid one raises `ValueError`
        here, before any worker checkout or engine work. The generator
        contract (`aclose`) is part of the surface: closing it early
        stops the search and returns the worker.
        """
        board = chess.Board(fen)
        return self._stream_eval(board, depth)

    async def _stream_eval(
        self, board: chess.Board, depth: int
    ) -> AsyncGenerator[LiveEval, None]:
        if board.outcome() is not None:  # mate/stalemate: nothing to search
            return
        engine = await self._idle.get()
        try:
            # aclosing: an early close of *this* generator (client gone,
            # position changed) must also stop the engine search, now.
            async with aclosing(engine.stream_infos(board, depth)) as infos:
                last_depth = 0
                async for info in infos:
                    live = _live_eval(info, board)
                    if live is None or live.depth <= last_depth:
                        continue
                    last_depth = live.depth
                    yield live
        finally:
            self._idle.put_nowait(engine)

    async def close(self) -> None:
        for engine in self._engines:
            await engine.close()


async def create_pool(bin_path: Path, workers: int) -> AnalysisPool:
    engines = [await Engine.open(bin_path) for _ in range(workers)]
    return AnalysisPool(engines)


def _live_eval(info: chess.engine.InfoDict, board: chess.Board) -> LiveEval | None:
    """Convert one raw info to a `LiveEval`; None when it carries no eval."""
    depth = info.get("depth")
    score = info.get("score")
    if depth is None or score is None:
        return None
    white = score.white()
    return LiveEval(
        depth=depth,
        eval_cp=white.score(),
        eval_mate=white.mate(),
        pv_san=_pv_san(board, info.get("pv") or []),
    )


def _pv_san(board: chess.Board, pv: list[chess.Move]) -> list[str]:
    copy = board.copy(stack=False)
    return [copy.san_and_push(move) for move in pv[:_PV_SAN_CAP]]
