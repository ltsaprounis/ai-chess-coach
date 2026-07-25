"""Shared test object factories."""

from collections.abc import Sequence

import chess

from chess_coach.domain import (
    AnalyzedGame,
    Color,
    Game,
    GameAnalysis,
    Judgment,
    MoveEval,
    Opening,
    Result,
    Thresholds,
    TimeClass,
)


def make_game(**overrides: object) -> Game:
    base: dict[str, object] = {
        "id": "game-1",
        "username": "testuser",
        "color": "white",
        "pgn": "1. e4 e5 *",
        "san_moves": ["e4", "e5"],
        "time_control": "600",
        "time_class": "rapid",
        "result": "win",
        "end_time": 1_780_300_000,
        "opponent": "hikaru",
        "player_rating": 1500,
        "opponent_rating": 1490,
    }
    return Game.model_validate({**base, **overrides})


def make_analysis(game_id: str = "game-1", depth: int = 16) -> GameAnalysis:
    return GameAnalysis(
        game_id=game_id,
        depth=depth,
        evals=[
            MoveEval(
                ply=1,
                san="e4",
                eval_cp=30,
                eval_mate=None,
                best_move="e2e4",
                cp_loss=0,
                judgment="best",
            ),
            MoveEval(
                ply=2,
                san="e5",
                eval_cp=25,
                eval_mate=None,
                best_move="c7c5",
                cp_loss=5,
                judgment="good",
            ),
        ],
        overall_acpl=2.5,
        acpl_by_phase={"opening": 2.5, "middlegame": 0.0, "endgame": 0.0},
        judgment_counts={
            "best": 1,
            "good": 1,
            "inaccuracy": 0,
            "mistake": 0,
            "blunder": 0,
        },
    )


# --- Realistic analyzed games, for report/prompt tests -----------------
#
# `make_analysis` above is a two-move stub; report aggregation and the
# coaching prompt need games that actually replay on a board: real SAN,
# coherent evals either side of each move, and a player whose cp losses
# are chosen per test. `make_analyzed` builds one from a move list plus
# the losses to attribute to the player's moves.

_START_EVAL = 20  # a hair for White, the usual engine opinion of 1.e4


def make_analyzed(
    game_id: str,
    san_moves: Sequence[str],
    *,
    color: Color = "white",
    result: Result = "win",
    opening: Opening | None = None,
    losses: Sequence[int] = (),
    time_class: TimeClass = "blitz",
    end_time: int = 1_780_300_000,
    player_rating: int = 1500,
    opponent_rating: int = 1500,
    termination: str | None = None,
    depth: int = 16,
) -> AnalyzedGame:
    """An analyzed game whose player moves take `losses` in order.

    Losses shorter than the player's move count are padded with zeros;
    the opponent always plays perfectly, which keeps the eval track
    readable and every non-zero swing attributable to the player.
    """
    game = make_game(
        id=game_id,
        color=color,
        result=result,
        san_moves=list(san_moves),
        time_class=time_class,
        end_time=end_time,
        player_rating=player_rating,
        opponent_rating=opponent_rating,
        termination=termination,
        pgn=_pgn(san_moves, result, color),
    )
    return AnalyzedGame.model_validate(
        {
            **game.model_dump(),
            "opening": opening,
            "analysis": _analysis(game, losses, depth),
        }
    )


def _analysis(game: Game, losses: Sequence[int], depth: int) -> GameAnalysis:
    thresholds = Thresholds()
    board = chess.Board()
    evals: list[MoveEval] = []
    player_losses: list[int] = []
    phase_losses: dict[str, list[int]] = {
        "opening": [],
        "middlegame": [],
        "endgame": [],
    }
    counts: dict[Judgment, int] = {
        "best": 0,
        "good": 0,
        "inaccuracy": 0,
        "mistake": 0,
        "blunder": 0,
    }
    remaining = list(losses)
    current = _START_EVAL

    for ply, san in enumerate(game.san_moves, start=1):
        mover_is_white = board.turn == chess.WHITE
        is_player = mover_is_white == (game.color == "white")
        loss = (remaining.pop(0) if remaining else 0) if is_player else 0
        move = board.parse_san(san)
        best = _best_uci(board, move, loss)
        # Losses are always from the mover's point of view; evals are
        # always from White's, so Black's losses push the eval up.
        current += -loss if mover_is_white else loss
        judgment = _judge(loss, move.uci(), best, thresholds)
        board.push(move)
        evals.append(
            MoveEval(
                ply=ply,
                san=san,
                eval_cp=current,
                eval_mate=None,
                best_move=best,
                cp_loss=loss,
                judgment=judgment,
            )
        )
        if is_player:
            player_losses.append(loss)
            phase_losses["opening" if ply <= 20 else "middlegame"].append(loss)
            counts[judgment] += 1

    return GameAnalysis(
        game_id=game.id,
        depth=depth,
        evals=evals,
        overall_acpl=_mean(player_losses),
        acpl_by_phase={
            "opening": _mean(phase_losses["opening"]),
            "middlegame": _mean(phase_losses["middlegame"]),
            "endgame": _mean(phase_losses["endgame"]),
        },
        judgment_counts=counts,
    )


def _best_uci(board: chess.Board, played: chess.Move, loss: int) -> str:
    """The played move when it lost nothing, else another legal move.

    Prefers a capture, then a check, then anything — an arbitrary-but-
    legal stand-in reads as engine output ("preferred Bxc6") where the
    first move in UCI order would read as noise ("preferred a3").
    """
    if loss == 0:
        return played.uci()
    others = [move for move in board.legal_moves if move != played]
    if not others:
        return played.uci()  # a forced reply; nothing else to prefer
    ranked = sorted(
        others,
        key=lambda move: (
            not board.is_capture(move),
            not board.gives_check(move),
            move.uci(),
        ),
    )
    return ranked[0].uci()


def _judge(loss: int, played: str, best: str, thresholds: Thresholds) -> Judgment:
    if loss >= thresholds.blunder:
        return "blunder"
    if loss >= thresholds.mistake:
        return "mistake"
    if loss >= thresholds.inaccuracy:
        return "inaccuracy"
    return "best" if played == best else "good"


def _mean(values: list[int]) -> float:
    return round(sum(values) / len(values), 1) if values else 0.0


_PGN_RESULTS: dict[tuple[Result, Color], str] = {
    ("win", "white"): "1-0",
    ("win", "black"): "0-1",
    ("loss", "white"): "0-1",
    ("loss", "black"): "1-0",
    ("draw", "white"): "1/2-1/2",
    ("draw", "black"): "1/2-1/2",
}


def _pgn(san_moves: Sequence[str], result: Result, color: Color) -> str:
    tokens: list[str] = []
    for index, san in enumerate(san_moves):
        if index % 2 == 0:
            tokens.append(f"{index // 2 + 1}.")
        tokens.append(san)
    tokens.append(_PGN_RESULTS[(result, color)])
    return " ".join(tokens)
