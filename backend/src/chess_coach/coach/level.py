"""The profile's level layer: which games describe the student *now*,
and where they are heading (docs/06-coach.md, "Window", "Trajectory").

Two functions that answer opposite questions over the same archive.
`profile_window` finds the cut point past which the games stop
describing the current player, so the outcome rates can be scoped to
one level. `build_trajectory` deliberately ignores that cut and reads
the whole curve, because direction is exactly what a level-scoped
window cannot show.

Both are pure and take only what storage already returns.
"""

from collections import Counter
from datetime import UTC, datetime

from chess_coach.domain import (
    Drawdown,
    GameSummary,
    MonthStats,
    RatingDelta,
    RatingTrajectory,
    Record,
)

# How far a month's median rating may sit from the newest month's before
# its games stop describing the same student. 200 points is the user's
# call, and it is the only real knob: on the reference archive 150 cuts
# at March and 200 at January, and the boundary itself lands in the same
# November/January gap either way, because that is where the real regime
# break is (docs/06-coach.md, "Window").
WINDOW_DRIFT_POINTS = 200

# A settled player would otherwise get a window spanning a changed
# opponent pool, a changed repertoire, and a changed engine baseline.
WINDOW_MAX_MONTHS = 12

# Below this many games the window keeps extending regardless of drift
# and the profile is flagged as spanning a change in level: a student
# mid-climb is better served by a caveat than by a 40-game window whose
# every percentage is noise.
WINDOW_MIN_GAMES = 150

# A month thinner than this cannot set the boundary on its own median --
# 12 games is not an estimate of anything. Such months are still *in*
# the window; they simply do not decide where it starts.
_THIN_MONTH = 30

_SCORE = {"win": 1.0, "draw": 0.5, "loss": 0.0}

# The trailing spans the trajectory reports, ascending.
_DELTA_DAYS = (30, 90, 180, 365)
_DAY = 86_400


def profile_window(months: list[MonthStats]) -> int | None:
    """The epoch second the outcome layer should start at, or None for
    "no cut -- the whole archive is one level" (docs/06-coach.md,
    "Window").

    Walks back in whole months from the newest, extending while the
    month's median rating stays within `WINDOW_DRIFT_POINTS` of the
    newest month's, and stops at the first month that does not. The
    returned bound is a *time* bound: every game after it is in, wins
    losses and drawdowns alike. Selecting games by their own rating
    instead would be selection on the outcome -- it deletes precisely
    the stretches where the student was losing.

    `months` is oldest-first, as `PlayerReport.months` is. Months with
    no rating contribute nothing (they cannot be compared) but do not
    stop the walk.
    """
    dated = [m for m in months if m.rating_median is not None]
    if len(dated) < 2:
        return None

    newest = dated[-1]
    assert newest.rating_median is not None  # filtered above
    anchor = newest.rating_median

    kept: list[MonthStats] = []
    for month in reversed(dated):
        if len(kept) >= WINDOW_MAX_MONTHS:
            break
        median = month.rating_median
        assert median is not None  # filtered above
        # A thin month rides along on its neighbours' verdict rather
        # than setting the boundary with a median over a dozen games.
        if month.games >= _THIN_MONTH and abs(median - anchor) > WINDOW_DRIFT_POINTS:
            break
        kept.append(month)

    if not kept or len(kept) == len(dated):
        return None  # the whole archive is one level; no cut to make

    # Too thin to say anything: widen rather than report noise. The
    # caller learns this happened by comparing the returned bound
    # against the drift-only one -- see `window_spans_level_change`.
    if sum(m.games for m in kept) < WINDOW_MIN_GAMES:
        widened = dated[-WINDOW_MAX_MONTHS:]
        if sum(m.games for m in widened) < WINDOW_MIN_GAMES:
            return None
        kept = list(reversed(widened))

    return _month_start(kept[-1].month)


def window_spans_level_change(months: list[MonthStats], since: int | None) -> bool:
    """True when the window had to reach past the drift bound to find a
    usable sample, so its outcome rates necessarily cover more than one
    level (docs/06-coach.md, "Window").
    """
    if since is None:
        return False
    dated = [m for m in months if m.rating_median is not None]
    if not dated:
        return False
    newest = dated[-1].rating_median
    assert newest is not None
    inside = [m for m in dated if _month_start(m.month) >= since]
    return any(
        m.rating_median is not None
        and abs(m.rating_median - newest) > WINDOW_DRIFT_POINTS
        for m in inside
    )


def _month_start(month: str) -> int:
    """ "2026-01" -> the epoch second the month begins, UTC. Storage's
    window filters are epoch-second comparisons, so the boundary has to
    be one too."""
    year, mon = month.split("-")
    return int(datetime(int(year), int(mon), 1, tzinfo=UTC).timestamp())


def build_trajectory(games: list[GameSummary]) -> RatingTrajectory | None:
    """Full-archive direction (docs/06-coach.md, "Trajectory") --
    deliberately *not* windowed, since a window holds the level roughly
    constant and direction is the thing that survives it.

    None on an archive with no games. Extremes are dated at the *first*
    game reaching them: `max()` over a chronological list returns the
    earliest, which is when the student got there, and "peaked in March
    and has not passed it since" is only true of the first date.
    """
    if not games:
        return None
    ordered = sorted(games, key=lambda g: (g.end_time, g.id))
    ratings = [g.player_rating for g in ordered]
    newest = ordered[-1]

    best = max(ratings)
    worst = min(ratings)
    return RatingTrajectory(
        rating_now=newest.player_rating,
        deltas=_deltas(ordered),
        rating_max=best,
        rating_max_at=ordered[ratings.index(best)].end_time,
        rating_min=worst,
        rating_min_at=ordered[ratings.index(worst)].end_time,
        games=len(ordered),
        window_start=ordered[0].end_time,
        window_end=newest.end_time,
        drawdown=_drawdown(ordered),
    )


def _deltas(ordered: list[GameSummary]) -> list[RatingDelta]:
    """Rating movement over each trailing span, anchored to the most
    recent game rather than to the clock -- a student who stopped
    playing three months ago would otherwise get four empty rows and a
    trajectory that says nothing (the same anchoring rule `periods`
    follows).

    A span reaching past the archive's own start is dropped rather than
    reported against the oldest game: "up 443 over the last year" on an
    eight-month archive is a claim about a year that does not exist.
    """
    now = ordered[-1].end_time
    rating_now = ordered[-1].player_rating
    out: list[RatingDelta] = []
    for days in _DELTA_DAYS:
        cutoff = now - days * _DAY
        if ordered[0].end_time > cutoff:
            continue
        # The last game at or before the cutoff -- where they stood then.
        prior = [g for g in ordered if g.end_time <= cutoff]
        if not prior:
            continue
        then = prior[-1].player_rating
        out.append(
            RatingDelta(
                days=days,
                rating_then=then,
                delta=rating_now - then,
                games=len(ordered) - len(prior),
            )
        )
    return out


def _drawdown(ordered: list[GameSummary]) -> Drawdown | None:
    """The largest peak-to-trough fall in the archive, with what came
    after it (docs/06-coach.md, "Trajectory").

    Running-maximum scan: for each game, how far below the best rating
    seen so far it sits. The deepest such point is the trough, and its
    peak is the running maximum at that moment -- which is what makes
    this a *drawdown* rather than just the distance between the global
    max and min, two points that may well be in the wrong order.

    None when the curve never falls, which is a real answer for a
    student who has only ever climbed.
    """
    peak = ordered[0].player_rating
    peak_at = ordered[0].end_time
    peak_index = 0
    best_peak, best_peak_at, best_peak_index = peak, peak_at, 0
    trough = peak
    trough_at = peak_at
    trough_index = 0
    depth = 0

    for i, game in enumerate(ordered):
        rating = game.player_rating
        if rating > peak:
            peak, peak_at, peak_index = rating, game.end_time, i
        if rating - peak < depth:
            depth = rating - peak
            best_peak, best_peak_at, best_peak_index = peak, peak_at, peak_index
            trough, trough_at, trough_index = rating, game.end_time, i

    if depth == 0:
        return None

    fall = ordered[best_peak_index : trough_index + 1]
    after = ordered[trough_index + 1 :]
    return Drawdown(
        peak=best_peak,
        peak_at=best_peak_at,
        trough=trough,
        trough_at=trough_at,
        record=_record(fall),
        since_record=_record(after),
        recovered=any(g.player_rating >= best_peak for g in after),
    )


def _record(games: list[GameSummary]) -> Record:
    counts = Counter(g.result for g in games)
    return Record(
        games=len(games),
        wins=counts["win"],
        losses=counts["loss"],
        draws=counts["draw"],
    )


def score_of(record: Record) -> float | None:
    """(wins + draws/2) / games, or None with no games -- the one
    definition of "score" every renderer and every comparison reads."""
    if not record.games:
        return None
    return (record.wins + record.draws / 2) / record.games


def score_variance(record: Record) -> float | None:
    """Per-game variance of the 1 / 1/2 / 0 score, from W/D/L alone.

    Computed rather than assumed Bernoulli: draws pull it below 0.25
    (0.236 on the reference archive), and a comparison that assumed the
    coin-flip value would overstate its own error bars. `Record` is all
    the input any of this needs, which is why the comparison family
    costs no new aggregation.
    """
    n = record.games
    if n < 2:
        return None
    mean = (record.wins + record.draws / 2) / n
    # E[S^2] over the three outcomes: wins contribute 1, draws 1/4.
    mean_sq = (record.wins + record.draws * 0.25) / n
    return max(0.0, (mean_sq - mean * mean) * n / (n - 1))
