"""Engine analysis logic tests (docs/04-engine.md) — stub evaluator."""

import chess
import pytest

from chess_coach.domain import Thresholds
from chess_coach.engine import EngineOptions, EvaluateFn, PositionEval, analyze_game
from tests.factories import make_game

OPTS = EngineOptions(depth=12, thresholds=Thresholds())


def stub_evaluator(per_ply: list[PositionEval]) -> EvaluateFn:
    """Evaluator returning canned evals keyed by call order (ply 0..N)."""
    calls: list[str] = []

    async def evaluate(fen: str) -> PositionEval:
        index = len(calls)
        calls.append(fen)
        return per_ply[index]

    return evaluate


def cp(value: int, best: str) -> PositionEval:
    return PositionEval(cp=value, mate=None, best_uci=best)


async def test_judgments_losses_and_player_only_aggregates() -> None:
    # White (the player) plays 1. e4 (best), black replies, then white
    # blunders a full 300 cp on move two.
    game = make_game(
        color="white", san_moves=["e4", "e5", "Nf3", "Nc6"], pgn="irrelevant"
    )
    evals = [
        cp(30, "e2e4"),  # start position: best move is e4
        cp(30, "g8f6"),  # after e4: stub prefers Nf6, so e5 is "good"
        cp(40, "g1f3"),  # after e5: white gained 10 -> black lost 10
        cp(-260, "g8f6"),  # after Nf3?? says the stub: white lost 300
        cp(-250, "d2d4"),  # after Nc6: black gave back 10
    ]

    analysis = await analyze_game(game, OPTS, stub_evaluator(evals))

    assert [e.judgment for e in analysis.evals] == [
        "best",  # e4 matches best_uci, loss 0
        "good",  # e5: loss 10 from black's POV, not the listed best
        "blunder",  # Nf3: 40 -> -260 is a 300 cp loss for white
        "good",  # Nc6: loss 10 for black
    ]
    assert [e.cp_loss for e in analysis.evals] == [0, 10, 300, 10]

    # Aggregates cover the player's (white's) moves only.
    assert analysis.overall_acpl == 150.0  # mean of 0 and 300
    assert analysis.judgment_counts == {
        "best": 1,
        "good": 0,
        "inaccuracy": 0,
        "mistake": 0,
        "blunder": 1,
    }
    assert analysis.acpl_by_phase == {
        "opening": 150.0,
        "middlegame": 0.0,
        "endgame": 0.0,
    }
    assert analysis.depth == 12
    assert analysis.game_id == game.id


async def test_black_player_perspective() -> None:
    # Same evals, but the player is black: only plies 2 and 4 count.
    game = make_game(
        color="black", san_moves=["e4", "e5", "Nf3", "Nc6"], pgn="irrelevant"
    )
    evals = [
        cp(30, "e2e4"),
        cp(30, "g8f6"),
        cp(40, "g1f3"),
        cp(-260, "g8f6"),
        cp(-250, "d2d4"),
    ]

    analysis = await analyze_game(game, OPTS, stub_evaluator(evals))

    assert analysis.overall_acpl == 10.0  # black's losses: 10 and 10
    assert analysis.judgment_counts["blunder"] == 0


async def test_mate_scores_clamp_for_loss_arithmetic() -> None:
    # White walks into a forced mate: eval goes from +50 to mate-in-2
    # for black. Loss clamps at 10000 + 50 -> blunder.
    game = make_game(color="white", san_moves=["f3", "e5"], pgn="irrelevant")
    evals = [
        cp(50, "e2e4"),
        PositionEval(cp=None, mate=-2, best_uci="e7e5"),  # after f3
        PositionEval(cp=None, mate=-1, best_uci="d8h4"),  # after e5
    ]

    analysis = await analyze_game(game, OPTS, stub_evaluator(evals))

    assert analysis.evals[0].cp_loss == 10_050
    assert analysis.evals[0].judgment == "blunder"
    assert analysis.evals[0].eval_mate == -2
    # Black keeping the mate loses nothing.
    assert analysis.evals[1].cp_loss == 0


async def test_evaluates_each_position_exactly_once() -> None:
    game = make_game(san_moves=["e4", "e5"], pgn="irrelevant")
    seen: list[str] = []

    async def evaluate(fen: str) -> PositionEval:
        seen.append(fen)
        return cp(0, "e2e4")

    await analyze_game(game, OPTS, evaluate)

    assert len(seen) == 3  # start + one per ply
    assert seen[0] == chess.STARTING_FEN
    assert len(set(seen)) == 3


async def test_illegal_san_raises() -> None:
    game = make_game(san_moves=["e4", "Qxh7"], pgn="irrelevant")

    async def evaluate(fen: str) -> PositionEval:
        return cp(0, "e2e4")

    with pytest.raises(chess.IllegalMoveError):
        await analyze_game(game, OPTS, evaluate)
