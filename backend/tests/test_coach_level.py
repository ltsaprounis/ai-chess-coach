"""The profile's level layer and comparison family (docs/06-coach.md,
"Window", "Trajectory", "Reading a comparison").

Numbers in the docstrings come from the reference archive the rules were
measured on: 1,925 rapid games running 185 to 1479 over two years.
"""

from datetime import UTC, datetime

from chess_coach.coach import (
    build_comparisons,
    build_trajectory,
    profile_window,
    window_spans_level_change,
)
from chess_coach.domain import ComparisonInput, GameSummary, MonthStats, Record
from tests.factories import make_game, summarize

_DAY = 86_400


def epoch(year: int, mon: int) -> int:
    return int(datetime(year, mon, 1, tzinfo=UTC).timestamp())


def month(name: str, *, median: int | None, games: int = 100) -> MonthStats:
    return MonthStats(
        month=name,
        games=games,
        rating_end=median,
        rating_median=median,
        acpl=None,
        blunder_rate=None,
    )


# --- profile_window --------------------------------------------------


def test_window_cuts_where_the_level_changes() -> None:
    """The reference archive's own shape: months holding 1391-1497,
    then a gap down to 1085. The cut belongs at the gap, and the drift
    bound is measured against the newest month, not month to month --
    a slow climb of 50 points a month would otherwise never trip it.
    """
    months = [
        month("2025-11", median=1085),
        month("2026-01", median=1305),
        month("2026-02", median=1310),
        month("2026-03", median=1391),
        month("2026-07", median=1468),
    ]

    since = profile_window(months)

    # 2026-01 is in (drift -163), 2025-11 is out (-383).
    assert since == epoch(2026, 1)


def test_window_keeps_the_whole_archive_when_the_level_never_moved() -> None:
    months = [month(f"2026-0{i}", median=1500 + i) for i in range(1, 6)]

    assert profile_window(months) is None


def test_window_is_not_set_by_a_thin_month() -> None:
    """A 12-game month's median is not an estimate of anything, so it
    cannot decide the scope of the whole profile -- it rides along on
    its neighbours instead."""
    months = [
        month("2026-04", median=1400),
        month("2026-05", median=1000, games=12),  # thin outlier
        month("2026-06", median=1420),
        month("2026-07", median=1430),
    ]

    since = profile_window(months)

    # The thin month did not cut the window at 2026-06.
    assert since is None


def test_window_widens_rather_than_reporting_a_thin_sample() -> None:
    """Below the sample floor the drift bound loses: a student mid-climb
    is better served by a flagged wide window than by 40 games whose
    every percentage is noise."""
    months = [
        month("2026-01", median=1000, games=200),
        month("2026-02", median=1100, games=200),
        month("2026-03", median=1250, games=200),
        month("2026-04", median=1500, games=20),  # only this one is in-drift
    ]

    since = profile_window(months)

    assert since is not None
    assert window_spans_level_change(months, since) is True


def test_window_never_spans_more_than_the_month_cap() -> None:
    """A settled player would otherwise get a window running back years,
    across a changed opponent pool and a changed repertoire. 20 flat
    months, so only the cap can decide where this one starts."""
    months = [month(f"2025-{m:02d}", median=1500) for m in range(1, 13)] + [
        month(f"2026-{m:02d}", median=1500) for m in range(1, 9)
    ]

    since = profile_window(months)

    # Newest is 2026-08, so a 12-month cap starts at 2025-09.
    assert since == epoch(2025, 9)


# --- build_trajectory ------------------------------------------------


def _curve(
    ratings: list[int], *, results: list[str] | None = None
) -> list[GameSummary]:
    """One game per day, oldest first, at the given ratings."""
    outcomes = results or ["win"] * len(ratings)
    return [
        summarize(
            make_game(
                id=f"g-{i}",
                end_time=1_780_000_000 + i * _DAY,
                player_rating=rating,
                result=outcome,
            )
        )
        for i, (rating, outcome) in enumerate(zip(ratings, outcomes, strict=True))
    ]


def test_trajectory_reports_direction_not_the_average() -> None:
    games = _curve([1000] + [1100] * 200 + [1479])

    trajectory = build_trajectory(games)

    assert trajectory is not None
    assert trajectory.rating_now == 1479
    thirty = trajectory.delta(30)
    assert thirty is not None
    assert thirty.delta > 0
    assert trajectory.improving is True


def test_trajectory_drops_a_span_the_archive_cannot_cover() -> None:
    """ "Up 443 over the last year" on an eight-month archive is a claim
    about a year that does not exist."""
    games = _curve([1400 + i for i in range(40)])  # 40 days

    trajectory = build_trajectory(games)

    assert trajectory is not None
    assert trajectory.delta(30) is not None
    assert trajectory.delta(365) is None


def test_trajectory_finds_the_drawdown_not_the_global_range() -> None:
    """A rise to 1574, a fall to 1329, then a partial recovery. The
    global max and min alone would describe a fall that never happened
    in that order; the running-maximum scan finds the real one.
    """
    games = _curve([1200, 1574, 1400, 1329, 1479])

    trajectory = build_trajectory(games)

    assert trajectory is not None
    drawdown = trajectory.drawdown
    assert drawdown is not None
    assert drawdown.peak == 1574
    assert drawdown.trough == 1329
    assert drawdown.depth == -245
    assert drawdown.recovered is False  # 1479 never reached 1574 again
    assert drawdown.since_record.games == 1


def test_trajectory_has_no_drawdown_on_a_curve_that_only_rises() -> None:
    trajectory = build_trajectory(_curve([100, 200, 300, 400]))

    assert trajectory is not None
    assert trajectory.drawdown is None


def test_peak_gap_is_suppressed_while_the_student_is_improving() -> None:
    """ "95 below peak" on a student up 443 on the year is a misread, and
    it is the one the first live narrative made."""
    climbing = _curve([1000] + [1200] * 200 + [1479])
    trajectory = build_trajectory(climbing)

    assert trajectory is not None
    assert trajectory.improving is True


# --- build_comparisons -----------------------------------------------


def record(wins: int, draws: int, losses: int) -> Record:
    return Record(games=wins + draws + losses, wins=wins, draws=draws, losses=losses)


def pair(label: str, left: Record, right: Record) -> ComparisonInput:
    return ComparisonInput(
        label=label, left_label="a", left=left, right_label="b", right=right
    )


def test_the_live_tilt_gap_does_not_survive() -> None:
    """The reference archive's after-a-loss bucket: 173-21-186 against
    391-44-343, a 4.8-point gap that reads as tilt and is 1.6 standard
    errors. Eighteen of those 380 results turning from loss to win would
    close it entirely -- which is the honest size of the "finding".
    """
    rows = build_comparisons([pair("Tilt", record(173, 21, 186), record(391, 44, 343))])

    assert len(rows) == 1
    assert rows[0].gap == -4.8
    assert rows[0].significant is False
    # The resolution is what the renderers state, and it is wider than
    # the gap -- which is the whole finding.
    assert rows[0].resolution > abs(rows[0].gap)


def test_a_real_gap_survives() -> None:
    rows = build_comparisons([pair("Tilt", record(60, 10, 230), record(230, 40, 60))])

    assert rows[0].significant is True


def test_the_family_is_judged_together_not_row_by_row() -> None:
    """Fourteen comparisons at an unadjusted threshold produce roughly
    one spurious tendency every other profile. Under BH a single
    borderline row among many nulls does not survive on its own.
    """
    borderline = pair("borderline", record(173, 21, 186), record(391, 44, 343))
    nulls = [pair(f"null-{i}", record(50, 5, 50), record(50, 5, 50)) for i in range(13)]

    rows = build_comparisons([borderline, *nulls])

    assert len(rows) == 14
    assert not any(row.significant for row in rows)


def test_an_unmeasurable_bucket_keeps_its_gap_and_never_signifies() -> None:
    """A bucket of one has no variance. The arithmetic is still true, so
    the gap stands; the verdict cannot be, so it is False -- and the row
    takes no slot in the BH family, since a comparison that could not be
    made was not a comparison that was tried."""
    rows = build_comparisons([pair("thin", record(1, 0, 0), record(50, 5, 45))])

    assert rows[0].measurable is False
    assert rows[0].significant is False


def test_an_empty_family_is_empty() -> None:
    assert build_comparisons([]) == []
