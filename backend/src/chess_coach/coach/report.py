"""Aggregate analyzed games into a PlayerReport (docs/06-coach.md).

Every ACPL figure here is total centipawn loss / total player moves over
the games in scope -- never a mean of per-game means -- computed from the
raw `evals` on each `AnalyzedGame`. Phases are re-derived while replaying
each game, applying the exact rule `engine/analysis.py:_phase` applies
(the constants live in `chess_coach.domain` for this reason -- the two
components cannot import each other, so the rule is shared through data,
not code).
"""

import contextlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

import chess

from chess_coach.domain import (
    ENDGAME_MATERIAL,
    MATE_SCORE,
    OPENING_PLIES,
    PIECE_POINTS,
    AnalyzedGame,
    Color,
    CriticalPosition,
    ErrorPattern,
    Judgment,
    MonthStats,
    MoveEval,
    OpeningStats,
    OpponentStats,
    Phase,
    PhaseStats,
    PlayerReport,
    Record,
    Result,
    TerminationStats,
    TimeClass,
    TimeClassStats,
)

_JUDGMENTS: tuple[Judgment, ...] = (
    "best",
    "good",
    "inaccuracy",
    "mistake",
    "blunder",
)
_PHASES: tuple[Phase, ...] = ("opening", "middlegame", "endgame")

# Losses this large can only come from a folded mate score (MATE_SCORE is
# the fold point); anything past this magnitude is "walked into a forced
# mate", not an ordinary blunder. Mirrors prompt.py's own _MATE_SCALE --
# both derive from the same domain constant, so they cannot drift.
_MATE_SCALE = MATE_SCORE - 1_000

# "±3 pawns" (docs/06-coach.md): the before-eval band a turning point must
# fall inside to count as "still contestable".
_CONTESTABLE_BAND = 300
# Splits the player's-POV eval into winning/equal/losing buckets so a
# turning point is a move that actually changed the character of the
# position, not just any big loss.
_DECISION_BOUNDARY = 150
_TOP_CRITICAL = 12
_LEADING_PLIES = 4
# No single phase/opening/time-class may claim more than this fraction of
# the turning-point slots, so the selection spreads out instead of a
# handful of blowout games filling the whole list.
_DIVERSITY_CAP_FRACTION = 0.34

# "+3 pawns" (docs/06-coach.md): the position must have been at least this
# good, in the player's own POV, to call losing it a "missed win".
_MISSED_WIN_THRESHOLD = 300

# ± rating points: opposition within this band of the player counts as
# "similar"; outside it, "stronger" or "weaker". A round, documented
# choice -- COACH-REPORT-IMPROVEMENTS.md suggests it as a sensible band.
_OPPONENT_BAND = 50

_ERROR_TAGS: tuple[str, ...] = (
    "hangs_piece",
    "hangs_piece_to_check",
    "back_rank",
    "missed_win",
    "walks_into_mate",
)
_ERROR_LABELS: dict[str, str] = {
    "hangs_piece": "Hung a piece",
    "hangs_piece_to_check": "Hung a piece to a check",
    "back_rank": "Back-rank vulnerability",
    "missed_win": "Let a winning position slip",
    "walks_into_mate": "Walked into a forced mate",
}


def build_report(
    username: str,
    games: list[AnalyzedGame],
    *,
    time_class: TimeClass | None = None,
    requested_since: int | None = None,
    requested_until: int | None = None,
    games_in_scope: int | None = None,
) -> PlayerReport:
    """Pure aggregation over analyzed games; every figure move-weighted.

    `requested_since`/`requested_until`/`games_in_scope` carry no
    aggregation logic of their own -- they are copied verbatim onto the
    report so the prompt can state coverage (docs/06-coach.md, "Coverage
    is stated, not implied"). All default to None.
    """
    summaries = [_summarize_game(g) for g in games]

    player_moves = sum(s.player_moves for s in summaries)
    player_loss = sum(s.player_loss for s in summaries)

    return PlayerReport(
        username=username,
        games_analyzed=len(games),
        player_moves=player_moves,
        window_start=min((g.end_time for g in games), default=None),
        window_end=max((g.end_time for g in games), default=None),
        time_class=time_class,
        requested_since=requested_since,
        requested_until=requested_until,
        games_in_scope=games_in_scope,
        record=_record(games),
        overall_acpl=round(player_loss / player_moves, 1) if player_moves else 0.0,
        phases=_phase_stats(summaries),
        judgment_counts={
            j: sum(s.judgment_counts[j] for s in summaries) for j in _JUDGMENTS
        },
        time_classes=_time_class_stats(games),
        months=_month_stats(games, summaries),
        terminations=_termination_stats(games),
        opponents=_opponent_stats(games),
        openings=_opening_stats(games, summaries),
        error_patterns=_error_patterns(games),
        critical_positions=_critical_positions(games),
    )


# --- one replay pass per game, shared by every aggregate below -------------


@dataclass
class _GameSummary:
    player_moves: int
    player_loss: int
    phase_moves: dict[Phase, int]
    phase_loss: dict[Phase, int]
    phase_judgments: dict[Phase, dict[Judgment, int]]
    judgment_counts: dict[Judgment, int]


def _summarize_game(game: AnalyzedGame) -> _GameSummary:
    """Replay the game once, tagging every player move with its phase.

    Mirrors `engine/analysis.py:_phase` exactly: phase is derived from the
    board *before* the move is pushed, using the shared domain constants.
    """
    player_is_white = game.color == "white"
    evals_by_ply = {e.ply: e for e in game.analysis.evals}
    board = chess.Board()

    phase_moves: dict[Phase, int] = {p: 0 for p in _PHASES}
    phase_loss: dict[Phase, int] = {p: 0 for p in _PHASES}
    phase_judgments: dict[Phase, dict[Judgment, int]] = {
        p: {j: 0 for j in _JUDGMENTS} for p in _PHASES
    }
    judgment_counts: dict[Judgment, int] = {j: 0 for j in _JUDGMENTS}
    player_moves = 0
    player_loss = 0

    for ply, san in enumerate(game.san_moves, start=1):
        mover_is_white = board.turn == chess.WHITE
        phase = _phase(ply, board)
        board.push_san(san)

        if mover_is_white != player_is_white:
            continue
        move_eval = evals_by_ply.get(ply)
        if move_eval is None:
            continue

        player_moves += 1
        player_loss += move_eval.cp_loss
        phase_moves[phase] += 1
        phase_loss[phase] += move_eval.cp_loss
        phase_judgments[phase][move_eval.judgment] += 1
        judgment_counts[move_eval.judgment] += 1

    return _GameSummary(
        player_moves=player_moves,
        player_loss=player_loss,
        phase_moves=phase_moves,
        phase_loss=phase_loss,
        phase_judgments=phase_judgments,
        judgment_counts=judgment_counts,
    )


def _phase(ply: int, board_before: chess.Board) -> Phase:
    """The shared rule (domain constants) -- mirrors engine/analysis.py."""
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


def _phase_stats(summaries: list[_GameSummary]) -> dict[Phase, PhaseStats]:
    stats: dict[Phase, PhaseStats] = {}
    for phase in _PHASES:
        moves = sum(s.phase_moves[phase] for s in summaries)
        loss = sum(s.phase_loss[phase] for s in summaries)
        stats[phase] = PhaseStats(
            moves=moves,
            acpl=round(loss / moves, 1) if moves else None,
            judgment_counts={
                j: sum(s.phase_judgments[phase][j] for s in summaries)
                for j in _JUDGMENTS
            },
        )
    return stats


def _record(games: list[AnalyzedGame]) -> Record:
    return Record(
        games=len(games),
        wins=sum(g.result == "win" for g in games),
        losses=sum(g.result == "loss" for g in games),
        draws=sum(g.result == "draw" for g in games),
    )


# --- time classes, months, terminations, opponents -------------------------


def _time_class_stats(games: list[AnalyzedGame]) -> list[TimeClassStats]:
    buckets: dict[TimeClass, list[AnalyzedGame]] = defaultdict(list)
    for g in games:
        buckets[g.time_class].append(g)

    ordered: list[tuple[int, TimeClassStats]] = []
    for tclass, members in buckets.items():
        by_time = sorted(members, key=lambda g: g.end_time)
        ratings = [g.player_rating for g in by_time]
        ordered.append(
            (
                by_time[0].end_time,
                TimeClassStats(
                    time_class=tclass,
                    record=_record(by_time),
                    rating_start=ratings[0],
                    rating_end=ratings[-1],
                    rating_min=min(ratings),
                    rating_max=max(ratings),
                ),
            )
        )
    ordered.sort(key=lambda item: item[0])
    return [stats for _, stats in ordered]


def _month_stats(
    games: list[AnalyzedGame], summaries: list[_GameSummary]
) -> list[MonthStats]:
    buckets: dict[str, list[int]] = defaultdict(list)
    for i, g in enumerate(games):
        month = datetime.fromtimestamp(g.end_time, tz=UTC).strftime("%Y-%m")
        buckets[month].append(i)

    result: list[MonthStats] = []
    for month in sorted(buckets):
        indices = buckets[month]
        month_games = [games[i] for i in indices]
        month_summaries = [summaries[i] for i in indices]
        latest = max(month_games, key=lambda g: g.end_time)
        moves = sum(s.player_moves for s in month_summaries)
        loss = sum(s.player_loss for s in month_summaries)
        blunders = sum(s.judgment_counts["blunder"] for s in month_summaries)
        result.append(
            MonthStats(
                month=month,
                games=len(month_games),
                rating_end=latest.player_rating,
                acpl=round(loss / moves, 1) if moves else None,
                blunder_rate=round(blunders / moves, 4) if moves else None,
            )
        )
    return result


def _termination_stats(games: list[AnalyzedGame]) -> list[TerminationStats]:
    counts: dict[tuple[Result, str], int] = defaultdict(int)
    for g in games:
        counts[(g.result, g.termination or "unknown")] += 1
    order = {"win": 0, "loss": 1, "draw": 2}
    items = sorted(counts.items(), key=lambda kv: (order[kv[0][0]], -kv[1], kv[0][1]))
    return [
        TerminationStats(result=result, termination=term, games=count)
        for (result, term), count in items
    ]


def _opponent_stats(games: list[AnalyzedGame]) -> OpponentStats | None:
    if not games:
        return None
    diffs = [g.player_rating - g.opponent_rating for g in games]
    stronger = [g for g, d in zip(games, diffs, strict=True) if d <= -_OPPONENT_BAND]
    weaker = [g for g, d in zip(games, diffs, strict=True) if d >= _OPPONENT_BAND]
    similar = [
        g
        for g, d in zip(games, diffs, strict=True)
        if -_OPPONENT_BAND < d < _OPPONENT_BAND
    ]
    return OpponentStats(
        avg_rating_diff=round(sum(diffs) / len(diffs), 1),
        vs_stronger=_record(stronger),
        vs_similar=_record(similar),
        vs_weaker=_record(weaker),
    )


# --- repertoire --------------------------------------------------------


def _opening_stats(
    games: list[AnalyzedGame], summaries: list[_GameSummary]
) -> list[OpeningStats]:
    groups: dict[tuple[Color, str, str], list[int]] = defaultdict(list)
    plies: dict[tuple[Color, str, str], list[int]] = defaultdict(list)
    for i, g in enumerate(games):
        if g.opening is not None:
            key = (g.color, g.opening.eco, g.opening.name)
            groups[key].append(i)
            plies[key].append(g.opening.ply)

    stats: list[OpeningStats] = []
    for (color, eco, name), indices in groups.items():
        members = [games[i] for i in indices]
        member_summaries = [summaries[i] for i in indices]
        opening_moves = sum(s.phase_moves["opening"] for s in member_summaries)
        opening_loss = sum(s.phase_loss["opening"] for s in member_summaries)
        total_moves = sum(s.player_moves for s in member_summaries)
        total_loss = sum(s.player_loss for s in member_summaries)
        system, first_moves = _representative_lines(members, color)
        group_plies = plies[(color, eco, name)]
        opponent_named = sum(1 for ply in group_plies if _is_opponent_named(ply, color))
        stats.append(
            OpeningStats(
                eco=eco,
                name=name,
                color=color,
                system=system,
                first_moves=first_moves,
                # Strict majority over the group's games; ties are chosen
                # (docs/06-coach.md, "Repertoire: keyed by the side the
                # player had"). Mirrors storage's `_is_opponent_named` +
                # majority computation exactly;
                # test_repertoire_agreement.py keeps the two honest.
                faced=opponent_named * 2 > len(group_plies),
                games=len(members),
                wins=sum(g.result == "win" for g in members),
                losses=sum(g.result == "loss" for g in members),
                draws=sum(g.result == "draw" for g in members),
                analyzed_games=len(members),
                opening_acpl=(
                    round(opening_loss / opening_moves, 1) if opening_moves else None
                ),
                avg_cp_loss=(
                    round(total_loss / total_moves, 1) if total_moves else None
                ),
                # The denominators behind the two columns above, so a
                # consumer rolling rows up into families can re-weight by
                # moves instead of falling back to game count
                # (docs/06-coach.md, "Family rollup").
                opening_moves=opening_moves,
                player_moves=total_moves,
            )
        )
    stats.sort(key=lambda s: (-_impact(s.games, s.wins, s.draws), -s.games, s.eco))
    return stats


def _is_opponent_named(ply: int, color: Color) -> bool:
    """True iff the book move that fixed the name (`Opening.ply`) was the
    opponent's -- White moves are odd plies, Black even (docs/06-coach.md,
    "Repertoire: keyed by the side the player had"). Mirrors storage's
    predicate exactly; test_repertoire_agreement.py keeps the two honest.
    """
    return (color == "white") == (ply % 2 == 0)


def _impact(games: int, wins: int, draws: int) -> float:
    """games x win-rate deficit -- so sample size, not raw rate, drives it."""
    score = (wins + draws / 2) / games if games else 0.0
    return games * (0.5 - score)


# How many plies the "most-played move sequence" is compared over, and how
# many of those are rendered in `first_moves` -- six plies is three full
# moves either side, per docs/06-coach.md's own examples.
_REPRESENTATIVE_PLIES = 6


def _representative_lines(members: list[AnalyzedGame], color: Color) -> tuple[str, str]:
    """The group's representative line, ties broken by lowest game id.

    `system` -- the key consumers roll rows up on -- is picked by the
    most-played **player** move sequence first: when one player system
    meets several different opponent setups, picking the representative
    by the most-played *full* line (opponent replies included) can hand
    `system` to a minority player line. `first_moves` is then the
    most-played full line among only the games that share that winning
    player sequence, same tie-break, so it still states a real line
    someone actually played rather than a Frankenstein of the two
    winners picked independently.
    """
    seq_counts: dict[tuple[str, ...], int] = defaultdict(int)
    seq_min_id: dict[tuple[str, ...], str] = {}
    for g in members:
        seq = tuple(_player_moves(g.san_moves[:_REPRESENTATIVE_PLIES], color))
        seq_counts[seq] += 1
        if seq not in seq_min_id or g.id < seq_min_id[seq]:
            seq_min_id[seq] = g.id
    best_seq = min(seq_counts, key=lambda s: (-seq_counts[s], seq_min_id[s]))

    line_counts: dict[tuple[str, ...], int] = defaultdict(int)
    line_min_id: dict[tuple[str, ...], str] = {}
    for g in members:
        prefix = tuple(g.san_moves[:_REPRESENTATIVE_PLIES])
        if tuple(_player_moves(list(prefix), color)) != best_seq:
            continue
        line_counts[prefix] += 1
        if prefix not in line_min_id or g.id < line_min_id[prefix]:
            line_min_id[prefix] = g.id
    best_line = min(line_counts, key=lambda p: (-line_counts[p], line_min_id[p]))

    return _format_system(list(best_seq), color), _format_first_moves(list(best_line))


def _player_moves(moves: list[str], color: Color) -> list[str]:
    """The player's own plies from a move prefix, in order.

    White's own plies are the even indices (0, 2, 4, ...) of a prefix
    starting at White's first move; Black's are the odd ones.
    """
    own_indices = (
        range(0, _REPRESENTATIVE_PLIES, 2)
        if color == "white"
        else range(1, _REPRESENTATIVE_PLIES, 2)
    )
    return [moves[i] for i in own_indices if i < len(moves)]


def _format_system(player_moves: list[str], color: Color) -> str:
    """The player's own first three moves, ply-numbered.

    `1.d4 2.Nf3 3.Bg5` as White, `1...d6 2...Nf6 3...g6` as Black --
    `player_moves` already holds only the player's own plies in order, so
    its position alone gives the move number.
    """
    sep = "." if color == "white" else "..."
    return " ".join(f"{i + 1}{sep}{san}" for i, san in enumerate(player_moves))


def _format_first_moves(moves: list[str]) -> str:
    """The same line with both sides answering: `1.d4 e5 2.dxe5 Nc6 ...`."""
    limit = min(len(moves), _REPRESENTATIVE_PLIES)
    parts: list[str] = []
    for i in range(0, limit, 2):
        chunk = f"{i // 2 + 1}.{moves[i]}"
        if i + 1 < limit:
            chunk += f" {moves[i + 1]}"
        parts.append(chunk)
    return " ".join(parts)


# --- error patterns ------------------------------------------------------


def _error_patterns(games: list[AnalyzedGame]) -> list[ErrorPattern]:
    """Tag every player blunder by static analysis -- never by a model.

    The refutation is the opponent's `best_move` at the following ply,
    which the stored evals already carry (docs/06-coach.md).
    """
    counts: dict[str, int] = {tag: 0 for tag in _ERROR_TAGS}
    # (game_id, ply, end_time, move_number) -- carries the same identity a
    # CriticalPosition does, so the prompt can cite "date, move N" rather
    # than the unfindable bare game id the citation rule exists to ban.
    examples: dict[str, tuple[str, int, int, int]] = {}
    total_blunders = 0

    for game in games:
        player_color = chess.WHITE if game.color == "white" else chess.BLACK
        player_is_white = game.color == "white"
        evals = game.analysis.evals
        board = chess.Board()

        for idx, san in enumerate(game.san_moves):
            mover_is_white = board.turn == chess.WHITE
            board.push_san(san)

            if mover_is_white != player_is_white or idx >= len(evals):
                continue
            move_eval = evals[idx]
            if move_eval.judgment != "blunder":
                continue
            total_blunders += 1

            before_cp, before_mate = _eval_before(evals, idx)
            before_pov = _pov_cp(before_cp, before_mate, player_is_white)
            after_pov = _pov_cp(move_eval.eval_cp, move_eval.eval_mate, player_is_white)
            refutation = _legal_refutation(board, evals, idx)
            tags = _classify_blunder(
                board, move_eval, before_pov, after_pov, refutation, player_color
            )
            for tag in tags:
                counts[tag] += 1
                examples.setdefault(
                    tag, (game.id, move_eval.ply, game.end_time, idx // 2 + 1)
                )

    if total_blunders == 0:
        return []

    patterns = [
        ErrorPattern(
            pattern=tag,
            label=_ERROR_LABELS[tag],
            count=counts[tag],
            share_of_blunders=round(counts[tag] / total_blunders, 3),
            example_game_id=examples[tag][0],
            example_ply=examples[tag][1],
            example_end_time=examples[tag][2],
            example_move_number=examples[tag][3],
        )
        for tag in _ERROR_TAGS
        if counts[tag] > 0
    ]
    patterns.sort(key=lambda p: -p.count)
    return patterns


def _classify_blunder(
    board_after: chess.Board,
    move_eval: MoveEval,
    before_pov: int,
    after_pov: int,
    refutation: chess.Move | None,
    player_color: chess.Color,
) -> set[str]:
    tags: set[str] = set()
    if move_eval.cp_loss >= _MATE_SCALE:
        tags.add("walks_into_mate")
    if before_pov >= _MISSED_WIN_THRESHOLD > after_pov:
        tags.add("missed_win")
    if refutation is not None:
        hangs = _hangs_piece(board_after, refutation, player_color)
        if hangs and board_after.gives_check(refutation):
            tags.add("hangs_piece_to_check")
        elif hangs:
            tags.add("hangs_piece")
        if _is_back_rank(board_after, refutation, player_color):
            tags.add("back_rank")
    return tags


def _legal_refutation(
    board_after: chess.Board, evals: list[MoveEval], idx: int
) -> chess.Move | None:
    if idx + 1 >= len(evals):
        return None
    try:
        move = chess.Move.from_uci(evals[idx + 1].best_move)
    except ValueError:
        return None
    if move not in board_after.legal_moves:
        return None
    return move


def _hangs_piece(
    board_after: chess.Board, refutation: chess.Move, player_color: chess.Color
) -> bool:
    """A player piece capturable at a net material loss (docs/06-coach.md).

    Either the captured piece outweighs the capturer outright -- a net
    gain for the opponent regardless of any recapture -- or it is simply
    undefended.
    """
    if not board_after.is_capture(refutation):
        return False
    captured_square = refutation.to_square
    if board_after.is_en_passant(refutation):
        captured_square = chess.square(
            chess.square_file(refutation.to_square),
            chess.square_rank(refutation.from_square),
        )
    captured = board_after.piece_at(captured_square)
    capturer = board_after.piece_at(refutation.from_square)
    if captured is None or capturer is None or captured.color != player_color:
        return False
    captured_value = PIECE_POINTS.get(captured.symbol().lower(), 0)
    capturer_value = PIECE_POINTS.get(capturer.symbol().lower(), 0)
    if captured_value > capturer_value:
        return True
    return not board_after.is_attacked_by(player_color, captured_square)


def _is_back_rank(
    board_after: chess.Board, refutation: chess.Move, player_color: chess.Color
) -> bool:
    """The refutation mates or wins material on the player's back rank."""
    back_rank = 0 if player_color == chess.WHITE else 7
    if chess.square_rank(refutation.to_square) != back_rank:
        return False
    mated = board_after.copy()
    mated.push(refutation)
    if mated.is_checkmate():
        return True
    return _hangs_piece(board_after, refutation, player_color)


def _eval_before(evals: list[MoveEval], idx: int) -> tuple[int | None, int | None]:
    if idx == 0:
        return None, None
    prev = evals[idx - 1]
    return prev.eval_cp, prev.eval_mate


def _pov_cp(cp: int | None, mate: int | None, player_is_white: bool) -> int:
    """Fold an eval to the player's own POV (mate folded to +/-MATE_SCORE)."""
    if mate is not None:
        folded = MATE_SCORE if mate > 0 else -MATE_SCORE
    elif cp is not None:
        folded = cp
    else:
        folded = 0
    return folded if player_is_white else -folded


# --- turning points (critical positions) ------------------------------


@dataclass
class _Candidate:
    game: AnalyzedGame
    idx: int  # 0-indexed position in san_moves / evals
    phase: Phase


def _critical_positions(games: list[AnalyzedGame]) -> list[CriticalPosition]:
    candidates = [c for c in (_game_candidates(g) for g in games) if c is not None]
    selected = _select_critical(candidates)
    return [_build_critical_position(c) for c in selected]


def _game_candidates(game: AnalyzedGame) -> _Candidate | None:
    """The single most instructive turning point in one game, if any.

    A turning point is a real error (judged mistake/blunder) that crossed
    a decision boundary (winning/equal/losing, in the player's own POV)
    while the position was still contestable (before-eval within
    roughly +/-3 pawns). Mate-scale losses are excluded outright -- a walk
    into forced mate is not "still contestable" afterward, and sorting on
    raw cp_loss is exactly the bug this replaces (docs/06-coach.md).
    """
    player_is_white = game.color == "white"
    evals = game.analysis.evals
    board = chess.Board()
    best: tuple[int, _Candidate] | None = None

    for idx, san in enumerate(game.san_moves):
        ply = idx + 1
        mover_is_white = board.turn == chess.WHITE
        phase = _phase(ply, board)
        board.push_san(san)

        if mover_is_white != player_is_white or idx >= len(evals):
            continue
        move_eval = evals[idx]
        if move_eval.judgment not in ("mistake", "blunder"):
            continue
        if move_eval.cp_loss >= _MATE_SCALE:
            continue

        before_cp, before_mate = _eval_before(evals, idx)
        before_pov = _pov_cp(before_cp, before_mate, player_is_white)
        if abs(before_pov) > _CONTESTABLE_BAND:
            continue
        after_pov = _pov_cp(move_eval.eval_cp, move_eval.eval_mate, player_is_white)
        if _bucket(before_pov) == _bucket(after_pov):
            continue

        candidate = _Candidate(game=game, idx=idx, phase=phase)
        if best is None or move_eval.cp_loss > best[0]:
            best = (move_eval.cp_loss, candidate)

    return best[1] if best else None


def _bucket(pov_cp: int) -> str:
    if pov_cp >= _DECISION_BOUNDARY:
        return "winning"
    if pov_cp <= -_DECISION_BOUNDARY:
        return "losing"
    return "equal"


def _select_critical(candidates: list[_Candidate]) -> list[_Candidate]:
    """Cap at `_TOP_CRITICAL`, spread across phase/opening/time class,
    weighted toward recent games (docs/06-coach.md)."""
    ordered = sorted(candidates, key=lambda c: (-c.game.end_time, c.game.id))
    cap = max(1, round(_TOP_CRITICAL * _DIVERSITY_CAP_FRACTION))
    phase_counts: dict[str, int] = defaultdict(int)
    opening_counts: dict[str, int] = defaultdict(int)
    time_class_counts: dict[str, int] = defaultdict(int)
    selected: list[_Candidate] = []
    deferred: list[_Candidate] = []

    for c in ordered:
        opening_key = c.game.opening.name if c.game.opening else "unclassified"
        time_class_key = c.game.time_class
        if (
            phase_counts[c.phase] >= cap
            or opening_counts[opening_key] >= cap
            or time_class_counts[time_class_key] >= cap
        ):
            deferred.append(c)
            continue
        selected.append(c)
        phase_counts[c.phase] += 1
        opening_counts[opening_key] += 1
        time_class_counts[time_class_key] += 1

    for c in deferred:
        if len(selected) >= _TOP_CRITICAL:
            break
        selected.append(c)

    selected.sort(key=lambda c: (-c.game.end_time, c.game.id))
    return selected[:_TOP_CRITICAL]


def _build_critical_position(c: _Candidate) -> CriticalPosition:
    game, idx = c.game, c.idx
    board = chess.Board()
    for san in game.san_moves[:idx]:
        board.push_san(san)
    move_eval = game.analysis.evals[idx]
    before_cp, before_mate = _eval_before(game.analysis.evals, idx)

    return CriticalPosition(
        game_id=game.id,
        end_time=game.end_time,
        time_class=game.time_class,
        color=game.color,
        opening_name=game.opening.name if game.opening else None,
        ply=idx + 1,
        move_number=idx // 2 + 1,
        fen=board.fen(),
        leading_up=game.san_moves[max(0, idx - _LEADING_PLIES) : idx],
        played=move_eval.san,
        best=_best_san(board, move_eval.best_move),
        cp_loss=move_eval.cp_loss,
        eval_before_cp=before_cp,
        eval_before_mate=before_mate,
        eval_after_cp=move_eval.eval_cp,
        eval_after_mate=move_eval.eval_mate,
    )


def _best_san(board: chess.Board, uci: str) -> str:
    """SAN when `uci` parses as legal in `board`, else the raw UCI."""
    best = uci
    with contextlib.suppress(ValueError):
        best = board.san(chess.Move.from_uci(uci))
    return best
