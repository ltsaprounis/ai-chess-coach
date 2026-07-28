"""One UCI engine process via python-chess (docs/04-engine.md)."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import suppress
from pathlib import Path
from typing import Final

import chess
import chess.engine
from pydantic import BaseModel

from chess_coach.domain import MATE_SCORE

# What a stored eval means; bump on any change to how an eval is
# produced so storage can mark older rows stale (docs/04-engine.md).
#   1 = carried-state searches (pre-fix): the engine's transposition
#       table and history heuristics persisted across positions,
#       making evals depend on unrelated search history and not
#       reproducible on re-analysis.
#   2 = per-position cleared state: a fresh `ucinewgame` before every
#       search, so an eval is a pure function of (position, depth,
#       multipv, binary).
ANALYSIS_VERSION: Final[int] = 2


class EngineError(Exception):
    """The engine process failed to start or misbehaved."""


class PositionEval(BaseModel):
    """White-POV evaluation of one position."""

    cp: int | None  # centipawns; None when a mate score is given
    mate: int | None  # moves to mate (±); ±1 marks an already-mated board
    best_uci: str | None  # engine's best move; None on terminal positions

    @property
    def clamped_cp(self) -> int:
        """Centipawns with mate folded to ±MATE_SCORE."""
        if self.mate is not None:
            return MATE_SCORE if self.mate > 0 else -MATE_SCORE
        return self.cp if self.cp is not None else 0


class Engine:
    """Owns one engine process; use `await Engine.open(...)`."""

    def __init__(
        self, transport: asyncio.SubprocessTransport, engine: chess.engine.UciProtocol
    ) -> None:
        self._transport = transport
        self._engine = engine

    @classmethod
    async def open(cls, bin_path: Path) -> "Engine":
        try:
            transport, engine = await chess.engine.popen_uci(str(bin_path))
        except (OSError, chess.engine.EngineError) as exc:
            raise EngineError(f"could not start engine at {bin_path}: {exc}") from exc
        return cls(transport, engine)

    async def evaluate(self, fen: str, depth: int) -> PositionEval:
        board = chess.Board(fen)
        outcome = board.outcome()
        if outcome is not None:
            return _terminal_eval(outcome)
        try:
            # A fresh `game` object every call makes python-chess send
            # `ucinewgame` before every search: the engine's transposition
            # table and history never carry state between positions
            # (docs/archive/engine-search-hangs.md).
            info = await self._engine.analyse(
                board, chess.engine.Limit(depth=depth), game=object()
            )
        except chess.engine.EngineError as exc:
            raise EngineError(f"analysis failed for {fen}: {exc}") from exc
        score = info.get("score")
        if score is None:
            raise EngineError(f"engine returned no score for {fen}")
        white = score.white()
        pv = info.get("pv")
        best = pv[0].uci() if pv else None
        return PositionEval(cp=white.score(), mate=white.mate(), best_uci=best)

    async def stream_infos(
        self, board: chess.Board, depth: int, multipv: int = 1
    ) -> AsyncGenerator[chess.engine.InfoDict, None]:
        """Stream raw per-line search infos live, up to a depth-limited
        MultiPV search.

        Closing the generator early stops the underlying search. The
        engine reports fewer than `multipv` lines when the position has
        fewer legal moves.
        """
        try:
            # Same fresh-token requirement as `evaluate`: without it the
            # live board and the coach's engine tool share carried search
            # state across unrelated positions.
            analysis = await self._engine.analysis(
                board, chess.engine.Limit(depth=depth), multipv=multipv, game=object()
            )
        except chess.engine.EngineError as exc:
            raise EngineError(f"analysis failed for {board.fen()}: {exc}") from exc
        with analysis:  # __exit__ stops the search if we leave early
            async for info in analysis:
                yield info

    async def close(self) -> None:
        await self._engine.quit()

    def kill(self) -> None:
        """Force-terminate the process (SIGKILL) via the transport.

        For a worker wedged mid-search that will never answer `quit`:
        closing its pipes makes python-chess's protocol error out
        instead of leaving a caller waiting forever, and stops the
        process from burning a core indefinitely
        (docs/archive/engine-search-hangs.md). Safe to call
        on an already-dead process.
        """
        with suppress(ProcessLookupError):
            self._transport.kill()


def _terminal_eval(outcome: chess.Outcome) -> PositionEval:
    if outcome.winner is None:  # stalemate or other draw
        return PositionEval(cp=0, mate=None, best_uci=None)
    mate = 1 if outcome.winner == chess.WHITE else -1
    return PositionEval(cp=None, mate=mate, best_uci=None)
