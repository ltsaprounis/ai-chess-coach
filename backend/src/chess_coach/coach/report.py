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
from statistics import median

import chess

from chess_coach.domain import (
    ENDGAME_MATERIAL,
    MATE_SCORE,
    OPENING_PLIES,
    PIECE_POINTS,
    AnalyzedGame,
    BestWin,
    Color,
    CriticalPosition,
    ErrorPattern,
    GameSummary,
    Judgment,
    MonthStats,
    MoveEval,
    Opening,
    OpeningStats,
    OpponentStats,
    PeriodStats,
    Phase,
    PhaseStats,
    PlayerReport,
    Record,
    Result,
    StreakStats,
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
# choice -- docs/archive/coach-report-improvements.md suggests it as a sensible band.
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


@dataclass
class _VolumeGame:
    """One game as the volume layer sees it (docs/06-coach.md, "Volume
    and quality").

    The volume aggregates -- record, ratings, monthly counts,
    terminations, opposition, repertoire counts -- need nothing an
    engine produces, so they must not be restricted to analyzed games:
    doing so reports a rating from whichever game happened to be
    analyzed last and a win rate over a biased subsample. This row is
    the common shape both inputs reduce to, so those aggregates have
    exactly one implementation regardless of which they were handed.

    `san_moves` holds only the opening prefix, which is all
    `_representative_lines` reads -- `GameSummary.first_plies` is that
    prefix already, and `_FIRST_PLIES` is pinned to
    `_REPRESENTATIVE_PLIES` in storage precisely so the two agree.
    """

    id: str
    color: Color
    time_class: TimeClass
    result: Result
    end_time: int
    opponent: str
    player_rating: int
    opponent_rating: int
    termination: str | None
    opening: Opening | None
    san_moves: list[str]
    analyzed: bool


def _volume_from_analyzed(game: AnalyzedGame) -> _VolumeGame:
    return _VolumeGame(
        id=game.id,
        color=game.color,
        time_class=game.time_class,
        result=game.result,
        end_time=game.end_time,
        opponent=game.opponent,
        player_rating=game.player_rating,
        opponent_rating=game.opponent_rating,
        termination=game.termination,
        opening=game.opening,
        san_moves=game.san_moves[:_REPRESENTATIVE_PLIES],
        analyzed=True,
    )


def _volume_from_summary(game: GameSummary) -> _VolumeGame:
    return _VolumeGame(
        id=game.id,
        color=game.color,
        time_class=game.time_class,
        result=game.result,
        end_time=game.end_time,
        opponent=game.opponent,
        player_rating=game.player_rating,
        opponent_rating=game.opponent_rating,
        termination=game.termination,
        opening=game.opening,
        san_moves=game.first_plies,
        analyzed=game.analyzed,
    )


def build_report(
    username: str,
    games: list[AnalyzedGame],
    *,
    all_games: list[GameSummary] | None = None,
    time_class: TimeClass | None = None,
    requested_since: int | None = None,
    requested_until: int | None = None,
    games_in_scope: int | None = None,
) -> PlayerReport:
    """Pure aggregation; every ACPL figure move-weighted.

    Two layers with two denominators (docs/06-coach.md, "Volume and
    quality"). `games` are the analyzed games and carry the quality
    layer -- ACPL, judgments, phases, error patterns, turning points --
    which nothing but an engine can produce. `all_games` is every stored
    game in the same scope, analyzed or not, and carries the volume
    layer: record, ratings, monthly counts, terminations, opposition,
    and the repertoire's game counts.

    `all_games` defaults to None, which aggregates volume over the
    analyzed games alone -- the behaviour before the split, kept so a
    caller that genuinely has only analyzed games (and every existing
    test) reads unchanged. Callers with the full list should pass it:
    with a partly-analyzed archive the two differ sharply, and the
    volume figures are simply wrong without it.

    `requested_since`/`requested_until`/`games_in_scope` carry no
    aggregation logic of their own -- they are copied verbatim onto the
    report so the prompt can state coverage (docs/06-coach.md, "Coverage
    is stated, not implied"). All default to None.
    """
    summaries = [_summarize_game(g) for g in games]
    quality_by_id = dict(zip((g.id for g in games), summaries, strict=True))
    volume = (
        [_volume_from_summary(g) for g in all_games]
        if all_games is not None
        else [_volume_from_analyzed(g) for g in games]
    )

    player_moves = sum(s.player_moves for s in summaries)
    player_loss = sum(s.player_loss for s in summaries)

    return PlayerReport(
        username=username,
        games_analyzed=len(games),
        player_moves=player_moves,
        # The covered span is the scope's span, not the analyzed
        # subset's -- with `all_games` omitted the two are identical.
        window_start=min((g.end_time for g in volume), default=None),
        window_end=max((g.end_time for g in volume), default=None),
        # ...and the analyzed subset's own span beside it. On a
        # partly-analyzed archive the two differ by more than a count:
        # the reference archive's volume runs 22 months while the engine
        # has reached only the last seven, which made "average loss
        # across the whole span" a claim about this year wearing two
        # years' clothes (docs/06-coach.md, "Coverage is stated").
        analyzed_window_start=min((g.end_time for g in games), default=None),
        analyzed_window_end=max((g.end_time for g in games), default=None),
        time_class=time_class,
        requested_since=requested_since,
        requested_until=requested_until,
        games_in_scope=games_in_scope,
        record=_record(volume),
        overall_acpl=round(player_loss / player_moves, 1) if player_moves else 0.0,
        phases=_phase_stats(summaries),
        judgment_counts={
            j: sum(s.judgment_counts[j] for s in summaries) for j in _JUDGMENTS
        },
        time_classes=_time_class_stats(volume),
        months=_month_stats(volume, quality_by_id),
        periods=_period_stats(volume, quality_by_id),
        terminations=_termination_stats(volume),
        opponents=_opponent_stats(volume),
        color_records=_color_records(volume),
        best_win=_best_win(volume),
        streaks=_streaks(volume),
        openings=_opening_stats(volume, quality_by_id),
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


def _record(games: list[_VolumeGame]) -> Record:
    return Record(
        games=len(games),
        wins=sum(g.result == "win" for g in games),
        losses=sum(g.result == "loss" for g in games),
        draws=sum(g.result == "draw" for g in games),
    )


# --- time classes, months, terminations, opponents -------------------------


def _time_class_stats(games: list[_VolumeGame]) -> list[TimeClassStats]:
    """Rating movement per control, each extreme stamped with the date
    it was first reached (docs/06-coach.md, "Milestones").

    First, not last: `max`/`min` over a chronological list return the
    earliest game holding the value, which is when the student got
    there -- the fact "peaked in March and has been below it since"
    rests on. Both are extremes of the games in scope, never chess.com's
    own all-time best, which the archive does not carry.
    """
    buckets: dict[TimeClass, list[_VolumeGame]] = defaultdict(list)
    for g in games:
        buckets[g.time_class].append(g)

    ordered: list[tuple[int, TimeClassStats]] = []
    for tclass, members in buckets.items():
        by_time = sorted(members, key=lambda g: g.end_time)
        ratings = [g.player_rating for g in by_time]
        # max()/min() return the first extreme element, and `by_time` is
        # chronological -- that is the "first reached" rule above.
        peak = max(by_time, key=lambda g: g.player_rating)
        low = min(by_time, key=lambda g: g.player_rating)
        ordered.append(
            (
                by_time[0].end_time,
                TimeClassStats(
                    time_class=tclass,
                    record=_record(by_time),
                    rating_start=ratings[0],
                    rating_end=ratings[-1],
                    rating_min=low.player_rating,
                    rating_max=peak.player_rating,
                    rating_max_at=peak.end_time,
                    rating_min_at=low.end_time,
                ),
            )
        )
    ordered.sort(key=lambda item: item[0])
    return [stats for _, stats in ordered]


def _month_stats(
    games: list[_VolumeGame], quality_by_id: dict[str, _GameSummary]
) -> list[MonthStats]:
    """`games`/`rating_end` over every game in the month, ACPL and
    blunder rate over the analyzed ones (docs/06-coach.md, "Volume and
    quality"): a month whose games are half analyzed still played all of
    them, and its closing rating is the real one.

    A month with games but no analysis yet reads acpl/blunder_rate as
    None -- absent, which is what it is, not 0.0.
    """
    buckets: dict[str, list[_VolumeGame]] = defaultdict(list)
    for g in games:
        month = datetime.fromtimestamp(g.end_time, tz=UTC).strftime("%Y-%m")
        buckets[month].append(g)

    result: list[MonthStats] = []
    for month in sorted(buckets):
        month_games = buckets[month]
        month_summaries = [
            s for g in month_games if (s := quality_by_id.get(g.id)) is not None
        ]
        latest = max(month_games, key=lambda g: g.end_time)
        moves = sum(s.player_moves for s in month_summaries)
        loss = sum(s.player_loss for s in month_summaries)
        blunders = sum(s.judgment_counts["blunder"] for s in month_summaries)
        result.append(
            MonthStats(
                month=month,
                games=len(month_games),
                rating_end=latest.player_rating,
                # The median, not the closing rating, is what the
                # profile window's drift rule reads (docs/06-coach.md,
                # "Window"): a month's last game is one outcome and
                # swings with it, and a window boundary decided by a
                # single game is not a boundary.
                rating_median=round(median(g.player_rating for g in month_games)),
                acpl=round(loss / moves, 1) if moves else None,
                blunder_rate=round(blunders / moves, 4) if moves else None,
            )
        )
    return result


# Trailing windows the profile reports performance over, narrowest
# first (docs/06-coach.md, "Recent form"). Nested rather than disjoint:
# a thin recent window is still backed by the wider ones behind it, so
# a narrative always has *some* window with a real sample that is more
# recent than the whole span.
_PERIOD_DAYS: tuple[int, ...] = (30, 90)
_DAY_SECONDS = 86_400


def _period_stats(
    games: list[_VolumeGame], quality_by_id: dict[str, _GameSummary]
) -> list[PeriodStats]:
    """Performance over trailing windows ending at the most recent game
    in scope (docs/06-coach.md, "Recent form").

    Anchored to the newest game rather than to `now` so the windows
    describe the data rather than the clock: a player who stopped
    playing three months ago would otherwise get three empty windows
    and a profile that says nothing. Windows that would restate the
    whole span are dropped -- with two months of history, "last 90
    days" and "all time" are the same row, and showing both invites a
    narrative to read a difference that cannot exist.
    """
    if not games:
        return []

    newest = max(g.end_time for g in games)
    oldest = min(g.end_time for g in games)
    span_days = (newest - oldest) / _DAY_SECONDS

    periods = [
        _period(
            games, quality_by_id, label=f"last {days} days", days=days, newest=newest
        )
        for days in _PERIOD_DAYS
        if days < span_days
    ]
    # "whole span", not "all time": these facts may already be scoped to
    # a window the caller chose, and calling that "all time" would state
    # something false about the player's history.
    periods.append(
        _period(games, quality_by_id, label="whole span", days=None, newest=newest)
    )
    return periods


def _period(
    games: list[_VolumeGame],
    quality_by_id: dict[str, _GameSummary],
    *,
    label: str,
    days: int | None,
    newest: int,
) -> PeriodStats:
    members = (
        games
        if days is None
        else [g for g in games if g.end_time >= newest - days * _DAY_SECONDS]
    )
    summaries = [s for g in members if (s := quality_by_id.get(g.id)) is not None]
    moves = sum(s.player_moves for s in summaries)
    loss = sum(s.player_loss for s in summaries)
    blunders = sum(s.judgment_counts["blunder"] for s in summaries)
    latest = max(members, key=lambda g: g.end_time) if members else None
    return PeriodStats(
        label=label,
        days=days,
        games=len(members),
        record=_record(members),
        analyzed_games=len(summaries),
        player_moves=moves,
        acpl=round(loss / moves, 1) if moves else None,
        blunder_rate=round(blunders / moves, 4) if moves else None,
        rating_end=latest.player_rating if latest is not None else None,
    )


def _termination_stats(games: list[_VolumeGame]) -> list[TerminationStats]:
    counts: dict[tuple[Result, str], int] = defaultdict(int)
    for g in games:
        counts[(g.result, g.termination or "unknown")] += 1
    order = {"win": 0, "loss": 1, "draw": 2}
    items = sorted(counts.items(), key=lambda kv: (order[kv[0][0]], -kv[1], kv[0][1]))
    return [
        TerminationStats(result=result, termination=term, games=count)
        for (result, term), count in items
    ]


# --- milestones (docs/06-coach.md, "Milestones") -----------------------
#
# Volume-layer, every one of them: beating a 1900, losing four in a row
# and scoring worse as Black are facts about the games played, not about
# which of them an engine has reached. Computing any of these over the
# analyzed subset alone would reproduce, one level down, the bug the
# volume/quality split exists to stamp out.

# How close together two games must be to count as the same sitting.
# Two hours is a round, documented choice, like `_OPPONENT_BAND`: long
# enough that a 30-minute rapid game still chains to the next one,
# short enough that "came back the following evening" does not read as
# a rebound from the loss before it. Games are stamped at their end, so
# the gap is measured end to end.
_SESSION_GAP = 7_200


def _color_records(games: list[_VolumeGame]) -> dict[Color, Record]:
    """Score as White against score as Black.

    Both keys always present: a player who never had Black in scope is
    a `Record` of zero games, which reads as "no sample", where a
    missing key would have every consumer guess.
    """
    return {
        color: _record([g for g in games if g.color == color])
        for color in ("white", "black")
    }


def _best_win(games: list[_VolumeGame]) -> BestWin | None:
    """The biggest **upset** -- the win over the opponent furthest above
    the student at the time (docs/06-coach.md, "Trajectory"). Ties break
    toward the recent game, because of two comparable wins the newer is
    the one the student remembers.

    Not the highest-rated opponent beaten, which is what this was and
    which measures nothing: chess.com pairs by rating, so that figure is
    structurally the student's own peak (1559 against a 1574 peak in
    rapid, 1172 against 1162 in blitz) and merely restates the ratings
    table two rows above -- while on a tightly-paired archive naming an
    opponent *weaker* than the student, rendered as a milestone.

    None when no win in scope beat a higher-rated opponent. That is the
    honest answer for a rating-matched archive, and it is common: over
    1,925 reference rapid games the student faced someone 50+ points
    stronger nine times and never beat one.
    """
    wins = [
        g for g in games if g.result == "win" and g.opponent_rating > g.player_rating
    ]
    if not wins:
        return None
    best = max(wins, key=lambda g: (g.opponent_rating - g.player_rating, g.end_time))
    return BestWin(
        game_id=best.id,
        end_time=best.end_time,
        time_class=best.time_class,
        color=best.color,
        opponent=best.opponent,
        opponent_rating=best.opponent_rating,
        player_rating=best.player_rating,
    )


def _streaks(games: list[_VolumeGame]) -> StreakStats | None:
    """Current and longest runs, plus the same-sitting rebound after a
    loss (docs/06-coach.md, "Milestones").

    A run is consecutive games with the same result in chronological
    order; the "current" one is the run the most recent game belongs
    to. `after_loss` counts each game whose immediately preceding game
    in scope was a loss played within `_SESSION_GAP` -- the next game
    of the same sitting, which is the one tilt shows up in. Chained
    losses each seed the next game, so a six-game slide contributes
    five games here, not one.
    """
    if not games:
        return None
    by_time = sorted(games, key=lambda g: (g.end_time, g.id))

    longest: dict[Result, int] = {"win": 0, "loss": 0, "draw": 0}
    run_result = by_time[0].result
    run_length = 0
    after_loss: list[_VolumeGame] = []
    for index, game in enumerate(by_time):
        if game.result == run_result:
            run_length += 1
        else:
            run_result, run_length = game.result, 1
        longest[run_result] = max(longest[run_result], run_length)

        previous = by_time[index - 1] if index else None
        if (
            previous is not None
            and previous.result == "loss"
            and game.end_time - previous.end_time <= _SESSION_GAP
        ):
            after_loss.append(game)

    return StreakStats(
        current_result=run_result,
        current_length=run_length,
        longest_win=longest["win"],
        longest_loss=longest["loss"],
        after_loss=_record(after_loss),
    )


def _opponent_stats(games: list[_VolumeGame]) -> OpponentStats | None:
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
    games: list[_VolumeGame], quality_by_id: dict[str, _GameSummary]
) -> list[OpeningStats]:
    """Repertoire rows over every classified game, with the ACPL columns
    over the analyzed members only (docs/06-coach.md, "Volume and
    quality").

    `games` and the win/loss/draw columns are volume: an opening's score
    is a fact about every game played in it, and computing it over the
    analyzed subset alone made a repertoire's win rate an artifact of
    which games the engine had reached. `analyzed_games` is the ACPL
    columns' own sample -- the field has always been declared as "how
    many of `games` have engine analysis" and only now can differ.
    """
    groups: dict[tuple[Color, str, str], list[_VolumeGame]] = defaultdict(list)
    plies: dict[tuple[Color, str, str], list[int]] = defaultdict(list)
    for g in games:
        if g.opening is not None:
            key = (g.color, g.opening.eco, g.opening.name)
            groups[key].append(g)
            plies[key].append(g.opening.ply)

    stats: list[OpeningStats] = []
    for (color, eco, name), members in groups.items():
        member_summaries = [
            s for g in members if (s := quality_by_id.get(g.id)) is not None
        ]
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
                analyzed_games=len(member_summaries),
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


def _representative_lines(members: list[_VolumeGame], color: Color) -> tuple[str, str]:
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
    # (game_id, ply, end_time, move_number, opponent) -- carries the same
    # identity a CriticalPosition does, so the prompt can cite "in your
    # game against marko77 on <date>, move N" rather than the unfindable
    # bare game id the citation rule exists to ban. The leading
    # (game_id, ply) pair doubles as the key prompt.py's
    # `_game_link_handles` groups on to mint this example's `[gN]` link
    # handle (docs/06-coach.md, "Game links").
    examples: dict[str, tuple[str, int, int, int, str]] = {}
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
                    tag,
                    (
                        game.id,
                        move_eval.ply,
                        game.end_time,
                        idx // 2 + 1,
                        game.opponent,
                    ),
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
            example_opponent=examples[tag][4],
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
        opponent=game.opponent,
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
