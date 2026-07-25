"""Pure per-game analysis logic (docs/04-engine.md).

`evals` covers every move of the game (both sides — the frontend
graph needs them); cp-loss aggregates (overall/phase ACPL) and
judgment counts cover the player's moves only.
"""

from collections.abc import Awaitable, Callable

import chess
from pydantic import BaseModel

from chess_coach.domain import (
    ENDGAME_MATERIAL,
    OPENING_PLIES,
    PIECE_POINTS,
    Game,
    GameAnalysis,
    Judgment,
    MoveEval,
    Phase,
    Thresholds,
)
from chess_coach.engine.uci import PositionEval

EvaluateFn = Callable[[str], Awaitable[PositionEval]]

_JUDGMENTS: tuple[Judgment, ...] = (
    "best",
    "good",
    "inaccuracy",
    "mistake",
    "blunder",
)


class EngineOptions(BaseModel):
    depth: int
    thresholds: Thresholds  # domain type; values come from config


async def analyze_game(
    game: Game, opts: EngineOptions, evaluate: EvaluateFn
) -> GameAnalysis:
    """Evaluate every position once and judge every move."""
    board = chess.Board()
    previous = await evaluate(board.fen())

    evals: list[MoveEval] = []
    player_losses: list[int] = []
    phase_losses: dict[Phase, list[int]] = {
        "opening": [],
        "middlegame": [],
        "endgame": [],
    }
    judgment_counts: dict[Judgment, int] = {j: 0 for j in _JUDGMENTS}

    for ply, san in enumerate(game.san_moves, start=1):
        mover_is_white = board.turn == chess.WHITE
        phase = _phase(ply, board)
        move = board.parse_san(san)
        played_uci = move.uci()
        board.push(move)

        current = await evaluate(board.fen())
        loss = _cp_loss(previous, current, mover_is_white)
        judgment = _judge(loss, played_uci, previous.best_uci, opts.thresholds)
        evals.append(
            MoveEval(
                ply=ply,
                san=san,
                eval_cp=current.cp,
                eval_mate=current.mate,
                best_move=previous.best_uci or played_uci,
                cp_loss=loss,
                judgment=judgment,
            )
        )

        if (game.color == "white") == mover_is_white:
            player_losses.append(loss)
            phase_losses[phase].append(loss)
            judgment_counts[judgment] += 1
        previous = current

    return GameAnalysis(
        game_id=game.id,
        depth=opts.depth,
        evals=evals,
        overall_acpl=_mean(player_losses),
        acpl_by_phase={phase: _mean(v) for phase, v in phase_losses.items()},
        judgment_counts=judgment_counts,
    )


def _cp_loss(before: PositionEval, after: PositionEval, mover_is_white: bool) -> int:
    sign = 1 if mover_is_white else -1
    return max(0, sign * (before.clamped_cp - after.clamped_cp))


def _judge(
    loss: int, played_uci: str, best_uci: str | None, thresholds: Thresholds
) -> Judgment:
    if loss >= thresholds.blunder:
        return "blunder"
    if loss >= thresholds.mistake:
        return "mistake"
    if loss >= thresholds.inaccuracy:
        return "inaccuracy"
    return "best" if played_uci == best_uci else "good"


def _phase(ply: int, board_before: chess.Board) -> Phase:
    """The shared rule (domain constants) — mirrored by the coach.

    The coach re-derives phases when aggregating raw `evals`
    (docs/06-coach.md); `test_phase_rule_matches_coach` asserts the two
    stay in step.
    """
    if ply <= OPENING_PLIES:
        return "opening"
    if all(
        _material(board_before, color) <= ENDGAME_MATERIAL
        for color in (chess.WHITE, chess.BLACK)
    ):
        return "endgame"
    return "middlegame"


def _material(board: chess.Board, color: chess.Color) -> int:
    return sum(
        PIECE_POINTS.get(piece.symbol().lower(), 0)
        for piece in board.piece_map().values()
        if piece.color == color
    )


def _mean(values: list[int]) -> float:
    return round(sum(values) / len(values), 1) if values else 0.0
