"""Move-explanation context aggregation (docs/06-coach.md).

Pure data shaping: given an analyzed game and a ply, replay the game with
python-chess to the positions before/after that ply and pull the matching
MoveEval. No LLM or engine calls happen here.
"""

import contextlib

import chess
from pydantic import BaseModel

from chess_coach.domain import Color, Game, GameAnalysis, Judgment, Opening


class MoveContext(BaseModel):
    """Everything render_explain_prompt needs to explain one played move."""

    username: str
    color: Color
    opening_name: str | None
    ply: int  # 1-based, matches MoveEval.ply
    san: str  # the played move
    fen_before: str
    fen_after: str
    best_move: str  # SAN when convertible on fen_before, else the raw UCI
    cp_loss: int
    judgment: Judgment


def build_move_context(
    game: Game, analysis: GameAnalysis, opening: Opening | None, ply: int
) -> MoveContext:
    """Replay `game` to `ply`'s positions and pull that ply's MoveEval.

    Raises ValueError when `ply` is out of range for `game.san_moves`, or
    when `analysis` has no MoveEval recorded for that ply.
    """
    if ply < 1 or ply > len(game.san_moves):
        raise ValueError(
            f"ply {ply} is out of range for a {len(game.san_moves)}-ply game"
        )
    move_eval = next((e for e in analysis.evals if e.ply == ply), None)
    if move_eval is None:
        raise ValueError(f"no MoveEval recorded for ply {ply}")

    board = chess.Board()
    for san in game.san_moves[: ply - 1]:
        board.push_san(san)
    fen_before = board.fen()

    # MoveEval.best_move is UCI; SAN reads better in an LLM prompt. Fall back
    # to the raw UCI string if it does not parse as a legal move here.
    best_move = move_eval.best_move
    with contextlib.suppress(ValueError):
        best_move = board.san(chess.Move.from_uci(move_eval.best_move))

    board.push_san(game.san_moves[ply - 1])
    fen_after = board.fen()

    return MoveContext(
        username=game.username,
        color=game.color,
        opening_name=opening.name if opening else None,
        ply=ply,
        san=move_eval.san,
        fen_before=fen_before,
        fen_after=fen_after,
        best_move=best_move,
        cp_loss=move_eval.cp_loss,
        judgment=move_eval.judgment,
    )
