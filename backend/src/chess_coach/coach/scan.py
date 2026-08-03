"""The `scan_games` event library (docs/06-coach.md, "Chat").

Pure functions over `ScanCandidate`: no I/O, no engine calls. The
sacrifice event reuses highlights' SEE machinery verbatim (same
component) -- `best_exchange_gain`, `best_exchange_gain_target`,
`captured_piece`, `captured_value`, `pov_cp`, `eval_before` -- so the
numbers agree with the Dashboard's brilliancy detector rather than
drifting into a second implementation.

`run_scan` walks an ordered `ScanSpec.match` sequence and returns the
first full chain per game, one `ScanHit` per step, in order:
`sacrifice` (the SEE-gated piece offer), `eval_swing` and `comeback`
(stored-eval readers, so both skip unanalyzed candidates outright --
recall-first does not apply where there is nothing to read),
`delivered_mate` and `castled` (moves-only, so they match unanalyzed
games too). `_EVENT_DETECTORS` is the whole extension point: the
chain-matching logic above it knows nothing about any one event.
"""

from collections.abc import Callable
from dataclasses import dataclass

import chess

from chess_coach.coach.highlights import (
    best_exchange_gain_target,
    captured_piece,
    eval_before,
    pov_cp,
)
from chess_coach.domain import (
    PIECE_POINTS,
    MoveEval,
    ScanCandidate,
    ScanEventName,
    ScanEventSpec,
    ScanHit,
    ScanMatch,
    ScanSpec,
)

# The events whose detectors read stored evals. The API layer asks
# this (via `spec_needs_evals`) to restrict a scan's candidate fetch
# and its denominators to analyzed games when -- and only when -- the
# sequence needs them (docs/07-api.md, "Chat"); moves-only sequences
# scan everything. Lives here so event semantics have one owner.
EVENTS_NEEDING_EVALS: frozenset[ScanEventName] = frozenset({"comeback", "eval_swing"})


def spec_needs_evals(spec: ScanSpec) -> bool:
    """Whether any step of `spec` reads stored evals."""
    return any(step.event in EVENTS_NEEDING_EVALS for step in spec.match)


# The escalation gate's floor (docs/06-coach.md, "Chat"): the offer after
# the move must exceed the opponent's best offer before it by at least
# this many points. Fixed, not injected -- unlike `BrilliantThresholds`,
# a scan is a retrieval tool the model parameterizes per call, and this
# gate is what keeps a hanging piece from re-firing every ply it sits
# untaken, not a coaching cutoff a deployment would want to tune.
_ESCALATION_MIN = 2

# Material must drop by at least this many points, player POV, for a
# sacrifice to be "realized" rather than "declined" (docs/06-coach.md).
_REALIZATION_DROP = 2

_PIECE_NAMES: dict[str, str] = {
    "q": "queen",
    "r": "rook",
    "b": "bishop",
    "n": "knight",
    "p": "pawn",
}
_PIECE_TIERS: dict[str, str] = {
    "q": "queen",
    "r": "rook",
    "b": "minor",
    "n": "minor",
    "p": "pawn",
}


# comeback's threshold (docs/06-coach.md, "Chat"): player-POV eval at
# or below this at some ply, in a game the player went on to win.
_COMEBACK_THRESHOLD_CP = -300


class UnsupportedScanEventError(ValueError):
    """`run_scan` was asked to match an event `_EVENT_DETECTORS` has no
    entry for. Every `ScanEventName` value is registered by the end of
    this module, so in practice this is a defensive backstop against a
    domain literal widening ahead of its detector, not a path a
    well-formed request should reach -- the tool schema is what keeps
    the model itself from ever asking for one.
    """

    def __init__(self, event: str) -> None:
        super().__init__(f"scan event {event!r} is not implemented")


@dataclass
class _Occurrence:
    """One event match within a game, before it is spliced into a
    chain: the ply it happened at (for sequencing subsequent steps)
    plus the rendered hit."""

    ply: int
    hit: ScanHit


def run_scan(candidates: list[ScanCandidate], spec: ScanSpec) -> list[ScanMatch]:
    """Every candidate whose game contains a full match of `spec.match`,
    in game order encountered, each carrying the first such chain
    (docs/06-coach.md, "Chat"): one `ScanHit` per step, strictly
    increasing in ply.
    """
    matches: list[ScanMatch] = []
    for candidate in candidates:
        hits = _match_candidate(candidate, spec.match)
        if hits is not None:
            matches.append(ScanMatch(game=candidate.summary, hits=hits))
    return matches


def _match_candidate(
    candidate: ScanCandidate, steps: list[ScanEventSpec]
) -> list[ScanHit] | None:
    if not steps:
        return None
    occurrences_by_step = [_event_occurrences(candidate, step) for step in steps]

    chosen: list[_Occurrence] = []
    prev_ply = 0
    for step_idx, occurrences in enumerate(occurrences_by_step):
        step = steps[step_idx]
        within = step.within_plies if step_idx > 0 else None
        match = _first_after(occurrences, prev_ply, within)
        if match is None:
            return None
        chosen.append(match)
        prev_ply = match.ply
    return [occ.hit for occ in chosen]


def _first_after(
    occurrences: list[_Occurrence], prev_ply: int, within_plies: int | None
) -> _Occurrence | None:
    """The earliest occurrence strictly after `prev_ply`. When
    `within_plies` is set, that occurrence's gap from `prev_ply` must
    respect it -- occurrences are ply-ascending, so if the nearest one
    already exceeds the bound, no later one can satisfy it either.
    """
    for occ in occurrences:
        if occ.ply <= prev_ply:
            continue
        if within_plies is not None and occ.ply - prev_ply > within_plies:
            return None
        return occ
    return None


def _event_occurrences(
    candidate: ScanCandidate, step: ScanEventSpec
) -> list[_Occurrence]:
    detector = _EVENT_DETECTORS.get(step.event)
    if detector is None:
        raise UnsupportedScanEventError(step.event)
    return detector(candidate, step)


# --- sacrifice (docs/06-coach.md, "Chat") -----------------------------


def _sacrifice_occurrences(
    candidate: ScanCandidate, step: ScanEventSpec
) -> list[_Occurrence]:
    player_is_white = candidate.summary.color == "white"
    evals = candidate.evals
    san_moves = candidate.san_moves
    board = chess.Board()
    occurrences: list[_Occurrence] = []

    for idx, san in enumerate(san_moves):
        try:
            move = board.parse_san(san)
        except ValueError:
            break  # malformed SAN stops the replay, keeping prior findings

        mover_is_white = board.turn == chess.WHITE
        is_player_move = mover_is_white == player_is_white
        if is_player_move and move.promotion is None:
            hit = _check_sacrifice(
                board,
                move,
                idx,
                san,
                san_moves[idx + 1 :],
                evals,
                player_is_white,
                step,
            )
            if hit is not None:
                occurrences.append(_Occurrence(ply=idx + 1, hit=hit))
        board.push(move)

    return occurrences


def _check_sacrifice(
    board_before: chess.Board,
    move: chess.Move,
    idx: int,
    san: str,
    remaining_san: list[str],
    evals: list[MoveEval] | None,
    player_is_white: bool,
    step: ScanEventSpec,
) -> ScanHit | None:
    """`board_before` sits at the position before `move` (`board.turn` is
    the player). Mutated with real push/pop, like highlights' SEE, so the
    lookahead sees actual legal-move generation."""
    fen_before = board_before.fen()
    captured = (
        captured_piece(board_before, move) if board_before.is_capture(move) else None
    )
    captured_by_move = PIECE_POINTS.get(captured.symbol().lower(), 0) if captured else 0
    gain_before = 0 if board_before.is_check() else _offer_before(board_before)
    baseline_material = _material(board_before, player_is_white)

    board_before.push(move)
    try:
        opponent_gain, target_square = best_exchange_gain_target(board_before)
        offer_after = opponent_gain - captured_by_move
        if offer_after - gain_before < _ESCALATION_MIN or target_square is None:
            return None

        offered = board_before.piece_at(target_square)
        if offered is None:
            return None
        tier = _PIECE_TIERS[offered.symbol().lower()]
        if not _tier_matches(tier, step.piece):
            return None

        mate_on_move = board_before.is_checkmate()
        realize_board = board_before.copy()
    finally:
        board_before.pop()

    piece_name = _PIECE_NAMES[offered.symbol().lower()]
    captured_name = _PIECE_NAMES[captured.symbol().lower()] if captured else None

    realizes = (
        0
        if mate_on_move
        else _realization(
            realize_board, remaining_san, player_is_white, baseline_material
        )
    )

    analyzed = evals is not None
    sound = True
    balanced_before = True
    before_str = after_str = ""
    if evals is not None and idx < len(evals):
        before_cp, before_mate = eval_before(evals, idx)
        before_pov = pov_cp(before_cp, before_mate, player_is_white)
        balanced_before = before_pov <= 200
        move_eval = evals[idx]
        after_pov = pov_cp(move_eval.eval_cp, move_eval.eval_mate, player_is_white)
        sound = after_pov >= 0
        before_str = _pov_eval_str(before_cp, before_mate, player_is_white)
        after_str = _pov_eval_str(
            move_eval.eval_cp, move_eval.eval_mate, player_is_white
        )
    else:
        analyzed = False

    if step.sound_only and analyzed and not sound:
        return None

    detail = _render_sac_detail(
        piece_name=piece_name,
        net_points=offer_after,
        captured_name=captured_name,
        realizes=realizes,
        analyzed=analyzed,
        sound=sound,
        balanced_before=balanced_before,
        before_str=before_str,
        after_str=after_str,
    )
    return ScanHit(ply=idx + 1, san=san, fen_before=fen_before, detail=detail)


def _tier_matches(offered_tier: str, requested: str) -> bool:
    """`piece` filters by minimum tier, except pawn-tier offers never
    match at all (docs/06-coach.md, "Chat")."""
    if offered_tier == "pawn":
        return False
    if requested == "queen":
        return offered_tier == "queen"
    if requested == "rook":
        return offered_tier in ("rook", "queen")
    return offered_tier in ("minor", "rook", "queen")  # requested == "minor"


def _offer_before(board: chess.Board) -> int:
    """The opponent's best SEE gain immediately before the move, via a
    null move that flips the turn without changing the position. The
    caller handles the in-check case (docs/06-coach.md, "Chat"): a null
    move while the mover is in check has no coherent chess meaning, so
    that case counts as gain 0 instead of reaching here.
    """
    board.push(chess.Move.null())
    try:
        gain, _ = best_exchange_gain_target(board)
        return gain
    finally:
        board.pop()


def _material(board: chess.Board, player_is_white: bool) -> int:
    """The player's own total piece value on the board, kings excluded --
    the same per-side convention `ENDGAME_MATERIAL` uses (domain.py)."""
    color = chess.WHITE if player_is_white else chess.BLACK
    total = 0
    for piece_type in (chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT, chess.PAWN):
        symbol = chess.piece_symbol(piece_type)
        total += len(board.pieces(piece_type, color)) * PIECE_POINTS[symbol]
    return total


def _realization(
    board: chess.Board,
    remaining_san: list[str],
    player_is_white: bool,
    baseline_material: int,
) -> int | None:
    """The smallest ply offset (1-based, counted from the offering move)
    at which the player's own material drops `_REALIZATION_DROP` below
    `baseline_material`, or the player delivers mate; `None` ("declined")
    if neither ever happens (docs/06-coach.md, "Chat"). `board` already
    sits at the position right after the offering move -- a copy, so the
    caller's own replay is untouched.
    """
    for offset, san in enumerate(remaining_san, start=1):
        try:
            move = board.parse_san(san)
        except ValueError:
            break  # malformed SAN: stop looking, the offer stands declined
        mover_is_white = board.turn == chess.WHITE
        board.push(move)
        if mover_is_white == player_is_white and board.is_checkmate():
            return offset
        if _material(board, player_is_white) <= baseline_material - _REALIZATION_DROP:
            return offset
    return None


def _pov_eval_str(cp: int | None, mate: int | None, player_is_white: bool) -> str:
    """Player-POV eval, rendered in pawns or `#N`/`#-N` -- unlike
    `pov_cp`, mate is not folded to `MATE_SCORE` here, since the point of
    this string is to say "#5", not "+100.00".
    """
    if mate is not None:
        player_mate = mate if player_is_white else -mate
        return f"#{player_mate}"
    if cp is not None:
        pov = cp if player_is_white else -cp
        return f"{pov / 100:+.2f}"
    return "n/a"


def _render_sac_detail(
    *,
    piece_name: str,
    net_points: int,
    captured_name: str | None,
    realizes: int | None,
    analyzed: bool,
    sound: bool,
    balanced_before: bool,
    before_str: str,
    after_str: str,
) -> str:
    captured_phrase = f"a {captured_name}" if captured_name else "nothing"
    parts = [
        f"{piece_name} sac, net {net_points} "
        f"(gave the {piece_name} for {captured_phrase})",
        f"realizes in {realizes}" if realizes is not None else "declined",
    ]
    if analyzed:
        parts.append("sound" if sound else "unsound")
        parts.append("balanced before" if balanced_before else "already winning before")
        parts.append(f"eval {before_str} -> {after_str}")
    else:
        parts.append("unverified (unanalyzed)")
    return "; ".join(parts)


# --- shared replay: eval_swing, comeback, delivered_mate, castled ------
#
# The four slice-2 events need a position (for `fen_before`) and, for
# `castled`, the move's castling flags -- but never SEE, so they share
# one plain forward replay rather than each hand-rolling `chess.Board()`
# bookkeeping. `sacrifice` above keeps its own bespoke loop: its
# real push/pop mutation for SEE is a different shape entirely.


@dataclass
class _Ply:
    ply: int  # 1-based, matches MoveEval.ply
    san: str
    move: chess.Move
    board_before: chess.Board  # not mutated further by the caller
    mover_is_white: bool

    @property
    def fen_before(self) -> str:
        return self.board_before.fen()


def _replay(san_moves: list[str]) -> tuple[list[_Ply], chess.Board]:
    """Every ply up to the first malformed SAN (the highlights
    tolerance: stop that game's replay, keep prior findings), plus the
    final board reached -- which is what `delivered_mate` checks for
    checkmate.
    """
    board = chess.Board()
    plies: list[_Ply] = []
    for idx, san in enumerate(san_moves):
        try:
            move = board.parse_san(san)
        except ValueError:
            break
        plies.append(
            _Ply(
                ply=idx + 1,
                san=san,
                move=move,
                board_before=board.copy(),
                mover_is_white=board.turn == chess.WHITE,
            )
        )
        board.push(move)
    return plies, board


def _delivered_mate_occurrences(
    candidate: ScanCandidate, step: ScanEventSpec
) -> list[_Occurrence]:
    """Win and the final replayed board is checkmate; match ply is the
    final one (docs/06-coach.md, "Chat"). Moves-only, so this matches
    unanalyzed games too.
    """
    if candidate.summary.result != "win":
        return []
    plies, final_board = _replay(candidate.san_moves)
    if not plies or not final_board.is_checkmate():
        return []
    last = plies[-1]
    hit = ScanHit(
        ply=last.ply,
        san=last.san,
        fen_before=last.fen_before,
        detail="delivered checkmate",
    )
    return [_Occurrence(ply=last.ply, hit=hit)]


def _castled_occurrences(
    candidate: ScanCandidate, step: ScanEventSpec
) -> list[_Occurrence]:
    """The player's O-O / O-O-O, exact from replay; `side` filters
    short/long/any (docs/06-coach.md, "Chat"). Moves-only.
    """
    player_is_white = candidate.summary.color == "white"
    plies, _ = _replay(candidate.san_moves)
    occurrences: list[_Occurrence] = []
    for p in plies:
        if p.mover_is_white != player_is_white:
            continue
        if not p.board_before.is_castling(p.move):
            continue
        side = "short" if p.board_before.is_kingside_castling(p.move) else "long"
        if step.side != "any" and step.side != side:
            continue
        side_word = "kingside" if side == "short" else "queenside"
        hit = ScanHit(
            ply=p.ply,
            san=p.san,
            fen_before=p.fen_before,
            detail=f"castled {side_word} ({side})",
        )
        occurrences.append(_Occurrence(ply=p.ply, hit=hit))
    return occurrences


def _eval_swing_occurrences(
    candidate: ScanCandidate, step: ScanEventSpec
) -> list[_Occurrence]:
    """Consecutive-ply player-POV stored-eval delta at or past
    `min_swing_pawns` in `direction`, mate folded, the game's first move
    counting as equal (docs/06-coach.md, "Chat"). Reads stored evals, so
    it skips unanalyzed candidates outright rather than annotating an
    unverified match -- there is nothing here to retrieve without them.
    """
    if candidate.evals is None:
        return []
    player_is_white = candidate.summary.color == "white"
    plies_by_ply = {p.ply: p for p in _replay(candidate.san_moves)[0]}
    threshold = round(step.min_swing_pawns * 100)

    occurrences: list[_Occurrence] = []
    prev_cp: int | None = 0  # first move = equal
    prev_mate: int | None = None
    prev_pov = 0
    for move_eval in candidate.evals:
        p = plies_by_ply.get(move_eval.ply)
        if p is None:
            break  # replay stopped before this ply (malformed SAN)
        pov = pov_cp(move_eval.eval_cp, move_eval.eval_mate, player_is_white)
        delta = pov - prev_pov
        matched = (
            delta >= threshold if step.direction == "gained" else delta <= -threshold
        )
        if matched:
            before_str = _pov_eval_str(prev_cp, prev_mate, player_is_white)
            after_str = _pov_eval_str(
                move_eval.eval_cp, move_eval.eval_mate, player_is_white
            )
            detail = (
                f"eval swing {delta / 100:+.2f} pawns (player POV): "
                f"{before_str} -> {after_str}"
            )
            hit = ScanHit(
                ply=move_eval.ply, san=p.san, fen_before=p.fen_before, detail=detail
            )
            occurrences.append(_Occurrence(ply=move_eval.ply, hit=hit))
        prev_cp, prev_mate, prev_pov = move_eval.eval_cp, move_eval.eval_mate, pov
    return occurrences


def _comeback_occurrences(
    candidate: ScanCandidate, step: ScanEventSpec
) -> list[_Occurrence]:
    """Win, with player-POV eval at or below `_COMEBACK_THRESHOLD_CP` at
    some ply; match ply is the worst one (docs/06-coach.md, "Chat").
    Reads stored evals, so unanalyzed candidates never match.
    """
    if candidate.evals is None or candidate.summary.result != "win":
        return []
    player_is_white = candidate.summary.color == "white"
    plies_by_ply = {p.ply: p for p in _replay(candidate.san_moves)[0]}

    worst: tuple[int, MoveEval] | None = None
    for move_eval in candidate.evals:
        if move_eval.ply not in plies_by_ply:
            break
        pov = pov_cp(move_eval.eval_cp, move_eval.eval_mate, player_is_white)
        if worst is None or pov < worst[0]:
            worst = (pov, move_eval)

    if worst is None or worst[0] > _COMEBACK_THRESHOLD_CP:
        return []
    _, worst_eval = worst
    p = plies_by_ply[worst_eval.ply]
    eval_str = _pov_eval_str(worst_eval.eval_cp, worst_eval.eval_mate, player_is_white)
    detail = f"comeback: down to {eval_str} (player POV) here, before winning"
    hit = ScanHit(ply=worst_eval.ply, san=p.san, fen_before=p.fen_before, detail=detail)
    return [_Occurrence(ply=worst_eval.ply, hit=hit)]


_EVENT_DETECTORS: dict[
    ScanEventName, Callable[[ScanCandidate, ScanEventSpec], list[_Occurrence]]
] = {
    "sacrifice": _sacrifice_occurrences,
    "eval_swing": _eval_swing_occurrences,
    "comeback": _comeback_occurrences,
    "delivered_mate": _delivered_mate_occurrences,
    "castled": _castled_occurrences,
}
