"""Dashboard highlights: blunders and brilliancies (docs/06-coach.md).

Pure static analysis over already-stored analyses -- no re-analysis, no
engine calls, no LLM -- so this stays out of `PlayerReport`, whose numbers
feed the coaching prompt rather than a per-move Dashboard list.

Blunders are simply the player's moves the engine already judged
`"blunder"`; brilliancies approximate chess.com's post-2021 "sound
sacrifice" definition over single-PV analyses (github issue #1), scored
by a recursive static-exchange evaluation (SEE) played out with real
`chess.Board` pushes/pops so pins and legality come from python-chess
rather than a hand-rolled attacker count. The known divergence -- a sac
that was the position's *only* good move is also awarded here, since
single-PV storage cannot see the road not taken -- is accepted for v1 and
recorded in docs/06-coach.md, not fixed here.
"""

import chess
from pydantic import BaseModel

from chess_coach.domain import (
    MATE_SCORE,
    PIECE_POINTS,
    AnalyzedGame,
    BrilliantThresholds,
    Color,
    MoveEval,
    Result,
    TimeClass,
)


class HighlightMove(BaseModel):
    """One linkable move for the Dashboard's blunders/brilliancies lists."""

    game_id: str
    end_time: int
    time_class: TimeClass
    color: Color
    result: Result
    opponent: str
    opening_name: str | None
    ply: int  # 1-based, matches MoveEval.ply
    move_number: int  # the "26" in "26...Nb6"
    san: str
    cp_loss: int
    eval_after_cp: int | None  # after the move, white POV like MoveEval
    eval_after_mate: int | None


class PlayerHighlights(BaseModel):
    blunders: list[HighlightMove]  # newest game first, then ply
    brilliancies: list[HighlightMove]  # same order


def build_highlights(
    games: list[AnalyzedGame], *, thresholds: BrilliantThresholds
) -> PlayerHighlights:
    """Every stored blunder, plus sound sacrifices, sorted newest-first.

    Pure aggregation: no re-analysis, no engine calls, no LLM. Sorting is
    newest game first (`end_time` desc, ties by `game_id` asc), then
    ascending `ply` within a game (docs/06-coach.md, "Highlights").
    """
    blunders: list[HighlightMove] = []
    brilliancies: list[HighlightMove] = []
    for game in games:
        game_blunders, game_brilliancies = _game_highlights(game, thresholds)
        blunders.extend(game_blunders)
        brilliancies.extend(game_brilliancies)

    blunders.sort(key=_sort_key)
    brilliancies.sort(key=_sort_key)
    return PlayerHighlights(blunders=blunders, brilliancies=brilliancies)


def _sort_key(highlight: HighlightMove) -> tuple[int, str, int]:
    return (-highlight.end_time, highlight.game_id, highlight.ply)


def _game_highlights(
    game: AnalyzedGame, thresholds: BrilliantThresholds
) -> tuple[list[HighlightMove], list[HighlightMove]]:
    """One game's blunders and brilliancies, replaying it with python-chess.

    Stops at the first SAN that fails to parse -- the same tolerance
    `openings/book.py` applies to a malformed continuation -- keeping
    whatever was collected up to that point.
    """
    player_is_white = game.color == "white"
    evals = game.analysis.evals
    board = chess.Board()
    blunders: list[HighlightMove] = []
    brilliancies: list[HighlightMove] = []

    for idx, san in enumerate(game.san_moves):
        if idx >= len(evals):
            break
        try:
            move = board.parse_san(san)
        except ValueError:
            break  # malformed/illegal continuation: keep what we have

        mover_is_white = board.turn == chess.WHITE
        if mover_is_white != player_is_white:
            board.push(move)
            continue

        move_eval = evals[idx]
        if move_eval.judgment == "blunder":
            blunders.append(_highlight(game, move_eval, idx))
        if _is_brilliant(board, move, idx, evals, player_is_white, thresholds):
            brilliancies.append(_highlight(game, move_eval, idx))
        board.push(move)

    return blunders, brilliancies


def _highlight(game: AnalyzedGame, move_eval: MoveEval, idx: int) -> HighlightMove:
    ply = idx + 1
    return HighlightMove(
        game_id=game.id,
        end_time=game.end_time,
        time_class=game.time_class,
        color=game.color,
        result=game.result,
        opponent=game.opponent,
        opening_name=game.opening.name if game.opening else None,
        ply=ply,
        move_number=(ply + 1) // 2,
        san=move_eval.san,
        cp_loss=move_eval.cp_loss,
        eval_after_cp=move_eval.eval_cp,
        eval_after_mate=move_eval.eval_mate,
    )


# --- brilliancy criteria (docs/06-coach.md, "Highlights") -----------------


def _is_brilliant(
    board_before: chess.Board,
    move: chess.Move,
    idx: int,
    evals: list[MoveEval],
    player_is_white: bool,
    thresholds: BrilliantThresholds,
) -> bool:
    """All four criteria hold: engine-best, a real sacrifice, not already
    winning beforehand, and still sound afterwards."""
    move_eval = evals[idx]
    if move_eval.cp_loss > thresholds.best_tolerance_cp:
        return False

    before_cp, before_mate = _eval_before(evals, idx)
    before_pov = _pov_cp(before_cp, before_mate, player_is_white)
    if before_pov > thresholds.winning_cap_cp:
        return False

    after_pov = _pov_cp(move_eval.eval_cp, move_eval.eval_mate, player_is_white)
    if after_pov < thresholds.sound_floor_cp:
        return False

    return _is_real_sacrifice(board_before, move, thresholds)


def _eval_before(evals: list[MoveEval], idx: int) -> tuple[int | None, int | None]:
    """The previous ply's stored eval; the game's first move counts as
    equal (docs/06-coach.md)."""
    if idx == 0:
        return None, None
    prev = evals[idx - 1]
    return prev.eval_cp, prev.eval_mate


def _pov_cp(cp: int | None, mate: int | None, player_is_white: bool) -> int:
    """Fold a white-POV eval to the player's own POV, mate at +/-MATE_SCORE."""
    if mate is not None:
        folded = MATE_SCORE if mate > 0 else -MATE_SCORE
    elif cp is not None:
        folded = cp
    else:
        folded = 0
    return folded if player_is_white else -folded


def _is_real_sacrifice(
    board_before: chess.Board, move: chess.Move, thresholds: BrilliantThresholds
) -> bool:
    """The opponent's best static-exchange gain, anywhere on the board,
    nets at least `sac_points` more than `move` itself just captured.

    `board_before` is mutated with a real push/pop around the check so
    the recursive SEE below sees actual legal-move generation (pins
    handled for free) rather than a hand-rolled attacker count.
    """
    captured_by_move = (
        _captured_value(board_before, move) if board_before.is_capture(move) else 0
    )
    board_before.push(move)
    try:
        opponent_gain = _best_exchange_gain(board_before)
    finally:
        board_before.pop()
    return opponent_gain - captured_by_move >= thresholds.sac_points


def _best_exchange_gain(board: chess.Board) -> int:
    """`board.turn`'s best net gain from any capture target on the board,
    each exchange played out by `_see_gain`. Zero with no legal capture --
    declining every exchange is itself always an option."""
    targets = {m.to_square for m in board.legal_moves if board.is_capture(m)}
    if not targets:
        return 0
    return max(_see_gain(board, square) for square in targets)


def _see_gain(board: chess.Board, square: chess.Square) -> int:
    """Net material `board.turn` gains from an optimal capture sequence on
    `square`: always the least valuable attacker first, and either side
    may decline to continue when that nets more (docs/06-coach.md)."""
    move = _least_valuable_capture(board, square)
    if move is None:
        return 0
    gained = _captured_value(board, move)
    board.push(move)
    net = gained - _see_gain(board, square)
    board.pop()
    return max(0, net)


def _least_valuable_capture(
    board: chess.Board, square: chess.Square
) -> chess.Move | None:
    """The legal capture onto `square` by the side to move's least
    valuable attacker (a king attacker counts as value 0, per
    docs/06-coach.md), or None with no such capture. Filtering through
    `board.legal_moves` handles pins for free."""
    candidates = [
        m for m in board.legal_moves if m.to_square == square and board.is_capture(m)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda m: _attacker_value(board, m))


def _attacker_value(board: chess.Board, move: chess.Move) -> int:
    piece = board.piece_at(move.from_square)
    return PIECE_POINTS.get(piece.symbol().lower(), 0) if piece else 0


def _captured_value(board: chess.Board, move: chess.Move) -> int:
    """The value of the piece `move` captures. En passant displaces the
    captured pawn off `to_square`; a promotion capture simply counts the
    captured piece's own value, not the promoted piece's."""
    if board.is_en_passant(move):
        captured_square = chess.square(
            chess.square_file(move.to_square), chess.square_rank(move.from_square)
        )
    else:
        captured_square = move.to_square
    captured = board.piece_at(captured_square)
    return PIECE_POINTS.get(captured.symbol().lower(), 0) if captured else 0
