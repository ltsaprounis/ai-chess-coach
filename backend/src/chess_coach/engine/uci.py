"""One UCI engine process via python-chess (docs/04-engine.md)."""

import asyncio
from pathlib import Path

import chess
import chess.engine
from pydantic import BaseModel

from chess_coach.domain import MATE_SCORE


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
            info = await self._engine.analyse(board, chess.engine.Limit(depth=depth))
        except chess.engine.EngineError as exc:
            raise EngineError(f"analysis failed for {fen}: {exc}") from exc
        score = info.get("score")
        if score is None:
            raise EngineError(f"engine returned no score for {fen}")
        white = score.white()
        pv = info.get("pv")
        best = pv[0].uci() if pv else None
        return PositionEval(cp=white.score(), mate=white.mate(), best_uci=best)

    async def close(self) -> None:
        await self._engine.quit()


def _terminal_eval(outcome: chess.Outcome) -> PositionEval:
    if outcome.winner is None:  # stalemate or other draw
        return PositionEval(cp=0, mate=None, best_uci=None)
    mate = 1 if outcome.winner == chess.WHITE else -1
    return PositionEval(cp=None, mate=mate, best_uci=None)
