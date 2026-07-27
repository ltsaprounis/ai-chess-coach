"""Engine worker pool (docs/04-engine.md)."""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing, suppress
from pathlib import Path

import chess
import chess.engine
from pydantic import BaseModel

from chess_coach.domain import EvalLine, Game, GameAnalysis
from chess_coach.engine.analysis import EngineOptions, analyze_game
from chess_coach.engine.uci import Engine, EngineError, PositionEval


class Progress(BaseModel):
    game_id: str
    ply: int
    total_plies: int


class LiveEval(BaseModel):
    """Snapshot of the current MultiPV candidate lines (docs/04-engine.md)."""

    lines: list[EvalLine]  # sorted by multipv rank


ProgressCallback = Callable[[Progress], None]

# The respawn seam create_pool wires up: opens a fresh engine process to
# replace a crashed worker. Injectable so stub pools in tests can omit it.
EngineFactory = Callable[[], Awaitable[Engine]]

_PV_SAN_CAP = 10  # plenty for display; PVs can run much longer

# Closing a worker whose process already died should return immediately,
# but it runs in request-path cleanup, so bound it just in case.
_CLOSE_TIMEOUT = 3.0


class AnalysisPool:
    """N engine processes behind a queue; analyze_game checks one out.

    A worker whose call raised `EngineError` is retired, never recycled:
    the process may be dead, and a dead engine back in the queue would
    fail every later checkout of that slot until the server restarts.
    Its replacement spawns lazily at the next checkout via `respawn`.
    """

    def __init__(
        self, engines: list[Engine], respawn: EngineFactory | None = None
    ) -> None:
        self._engines = list(engines)
        self._respawn = respawn
        # None marks the empty slot of a retired worker, respawned at
        # the next checkout.
        self._idle: asyncio.Queue[Engine | None] = asyncio.Queue()
        for engine in engines:
            self._idle.put_nowait(engine)

    async def _checkout(self) -> Engine:
        engine = await self._idle.get()
        if engine is not None:
            return engine
        # A retired worker's slot. Respawning at the point of use rather
        # than at retire time means a binary fixed after a crash comes
        # back without a restart. Slots only retire with a respawner set
        # (`_retire`), so this guard is belt-and-braces.
        if self._respawn is None:
            raise EngineError("engine worker died and no respawner is set")
        try:
            fresh = await self._respawn()
        except EngineError:
            self._idle.put_nowait(None)  # keep the slot for the next attempt
            raise
        self._engines.append(fresh)
        return fresh

    async def _retire(self, engine: Engine) -> None:
        """Take a worker whose call failed out of rotation.

        Best-effort close (the process is likely already dead), then an
        empty slot instead of the engine. Without a respawner the old
        recycle behavior stands — better a flaky worker than a pool that
        shrinks to nothing.
        """
        if self._respawn is None:
            self._idle.put_nowait(engine)
            return
        if engine in self._engines:
            self._engines.remove(engine)
        with suppress(Exception):
            await asyncio.wait_for(engine.close(), timeout=_CLOSE_TIMEOUT)
        self._idle.put_nowait(None)

    async def analyze_game(
        self,
        game: Game,
        opts: EngineOptions,
        on_progress: ProgressCallback | None = None,
    ) -> GameAnalysis:
        engine = await self._checkout()
        engine_failed = False
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
        except EngineError:
            engine_failed = True
            raise
        finally:
            if engine_failed:
                await self._retire(engine)
            else:
                self._idle.put_nowait(engine)

    def stream_eval(
        self, fen: str, depth: int, multipv: int = 1
    ) -> AsyncGenerator[LiveEval, None]:
        """Live MultiPV eval: one `LiveEval` per changed lines snapshot.

        Parses the FEN eagerly so an invalid one raises `ValueError`
        here, before any worker checkout or engine work. The generator
        contract (`aclose`) is part of the surface: closing it early
        stops the search and returns the worker.
        """
        board = chess.Board(fen)
        return self._stream_eval(board, depth, multipv)

    async def eval_lines(
        self, fen: str, depth: int, multipv: int = 1
    ) -> list[EvalLine]:
        """One-shot MultiPV eval: same search, returning the final snapshot.

        Parses the FEN eagerly, like `stream_eval`. Terminal positions
        return `[]`.
        """
        board = chess.Board(fen)
        last: LiveEval | None = None
        async with aclosing(self._stream_eval(board, depth, multipv)) as stream:
            async for live in stream:
                last = live
        return last.lines if last is not None else []

    async def _stream_eval(
        self, board: chess.Board, depth: int, multipv: int
    ) -> AsyncGenerator[LiveEval, None]:
        if board.outcome() is not None:  # mate/stalemate: nothing to search
            return
        engine = await self._checkout()
        engine_failed = False
        try:
            # aclosing: an early close of *this* generator (client gone,
            # position changed) must also stop the engine search, now.
            async with aclosing(engine.stream_infos(board, depth, multipv)) as infos:
                lines_by_rank: dict[int, EvalLine] = {}
                last_snapshot: list[EvalLine] | None = None
                async for info in infos:
                    line = _eval_line(info, board)
                    if line is None:
                        continue
                    lines_by_rank[line.multipv] = line
                    snapshot = [lines_by_rank[rank] for rank in sorted(lines_by_rank)]
                    if snapshot == last_snapshot:  # no change: skip emission
                        continue
                    last_snapshot = snapshot
                    yield LiveEval(lines=snapshot)
        except EngineError:
            engine_failed = True
            raise
        finally:
            if engine_failed:
                await self._retire(engine)
            else:
                self._idle.put_nowait(engine)

    async def close(self) -> None:
        for engine in self._engines:
            await engine.close()


async def create_pool(bin_path: Path, workers: int) -> AnalysisPool:
    engines = [await Engine.open(bin_path) for _ in range(workers)]

    async def respawn() -> Engine:
        return await Engine.open(bin_path)

    return AnalysisPool(engines, respawn=respawn)


def _eval_line(info: chess.engine.InfoDict, board: chess.Board) -> EvalLine | None:
    """Convert one raw per-line info to an `EvalLine`; None with no eval."""
    depth = info.get("depth")
    score = info.get("score")
    if depth is None or score is None:
        return None
    white = score.white()
    return EvalLine(
        multipv=info.get("multipv", 1),
        depth=depth,
        eval_cp=white.score(),
        eval_mate=white.mate(),
        pv_san=_pv_san(board, info.get("pv") or []),
    )


def _pv_san(board: chess.Board, pv: list[chess.Move]) -> list[str]:
    copy = board.copy(stack=False)
    return [copy.san_and_push(move) for move in pv[:_PV_SAN_CAP]]
