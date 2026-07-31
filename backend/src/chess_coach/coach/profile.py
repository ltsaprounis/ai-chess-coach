"""Distill a PlayerReport into a PlayerProfile (docs/06-coach.md,
"Player profile").

Pure projection -- the aggregation itself runs once, in
`report.py:build_report`; this module adds no second implementation of
any semantic, including the repertoire family rollup, which is shared
with the report prompt through `chess_coach.coach.repertoire`.
"""

from chess_coach.coach.comparisons import build_comparisons
from chess_coach.coach.repertoire import (
    REPERTOIRE_SAMPLE_FLOOR,
    FacedFamily,
    Family,
    family_impact,
    family_score,
    rollup_chosen_families,
    rollup_faced_families,
)
from chess_coach.domain import (
    Color,
    Comparison,
    ComparisonInput,
    PlayerProfile,
    PlayerReport,
    ProfileOpening,
    RatingTrajectory,
    Record,
)

# Trend rows kept in the profile -- enough to show direction without
# blowing render_profile_context's ~250-token budget (docs/06-coach.md).
_MONTHS_CAP = 6

# Repertoire rows kept per color: chosen capped by games (what the player
# actually plays), faced capped by impact (what actually hurts them) --
# the same distinction docs/06-coach.md draws for the profile's caps.
_CHOSEN_CAP = 3
_FACED_CAP = 2

_COLORS: tuple[Color, ...] = ("white", "black")


def build_profile(
    report: PlayerReport,
    *,
    trajectory: RatingTrajectory | None = None,
    spans_level_change: bool = False,
) -> PlayerProfile:
    """Pure distillation of an already-built report (docs/06-coach.md,
    "Player profile"). `narrative` stays None -- the API layer attaches
    the stored narrative when one exists.

    `trajectory` covers the **full archive** and so cannot be derived
    here: the report handed in has already been windowed to one level,
    which is precisely the information trajectory exists to report
    (docs/06-coach.md, "Trajectory"). The caller holds the unwindowed
    games and passes it; None simply omits the section.

    Total over an empty report: every field here is either a direct copy
    of a report field (already `[]`/`{}`/`None`/`0`/`0.0` for a report
    with no analyzed games) or a rollup over a report list that is
    already `[]`, so no branch below needs an explicit empty-report
    special case.
    """
    return PlayerProfile(
        username=report.username,
        time_class=report.time_class,
        games_covered=report.games_analyzed,
        # The volume layer's own denominator (docs/06-coach.md, "Volume
        # and quality"). `games_in_scope` is None on a report built
        # without it, in which case the analyzed count is all the scope
        # this profile can honestly claim.
        games_in_scope=(
            report.games_in_scope
            if report.games_in_scope is not None
            else report.games_analyzed
        ),
        window_start=report.window_start,
        window_end=report.window_end,
        player_moves=report.player_moves,
        overall_acpl=report.overall_acpl,
        judgment_counts=report.judgment_counts,
        phases=report.phases,
        time_classes=report.time_classes,
        months=report.months[-_MONTHS_CAP:],  # already oldest-first; slice keeps it so
        periods=report.periods,
        # Milestones and splits: direct copies, since the report already
        # computed them (docs/06-coach.md, "Milestones"). None is capped
        # -- the two records are single rows, and `terminations` is
        # bounded by chess.com's own result-code vocabulary at a handful
        # of rows. Slicing it would be worse than not: the renderer sums
        # each result's rows to state "62 losses: ...", and a capped list
        # would make that total disagree with the record above it.
        record=report.record,
        color_records=report.color_records,
        best_win=report.best_win,
        streaks=report.streaks,
        opponents=report.opponents,
        terminations=report.terminations,
        openings=_profile_openings(report),
        error_patterns=report.error_patterns,
        analyzed_window_start=report.analyzed_window_start,
        analyzed_window_end=report.analyzed_window_end,
        trajectory=trajectory,
        window_spans_level_change=spans_level_change,
        comparisons=_profile_comparisons(report),
        narrative=None,
    )


def _profile_comparisons(report: PlayerReport) -> list[Comparison]:
    """The profile's whole comparison family, judged together
    (docs/06-coach.md, "Reading a comparison").

    One call for all of them, because Benjamini-Hochberg is a property
    of the family and not of any row: building these in two places, or
    judging one on its own, reintroduces exactly the false-discovery
    rate the guard exists to hold down.
    """
    pairs: list[ComparisonInput] = []

    streaks = report.streaks
    if streaks is not None:
        # Matched baseline: games *not* after a loss, never the overall
        # record, which counts the after-loss games on both sides of its
        # own comparison and so understates the gap it is measuring.
        pairs.append(
            ComparisonInput(
                label="Tilt",
                left_label="within 2 hours of a loss",
                left=streaks.after_loss,
                right_label="every other game",
                right=_without(report.record, streaks.after_loss),
            )
        )

    white = report.color_records.get("white")
    black = report.color_records.get("black")
    if white is not None and black is not None:
        pairs.append(
            ComparisonInput(
                label="By color",
                left_label="as White",
                left=white,
                right_label="as Black",
                right=black,
            )
        )

    # Each chosen family against the student's other games with that
    # color -- their own baseline, not the overall score, since White
    # and Black score differently for everyone and comparing a defence
    # against a White system measures nothing.
    for color in _COLORS:
        color_record = report.color_records.get(color)
        if color_record is None:
            continue
        chosen = rollup_chosen_families(
            [o for o in report.openings if o.color == color and not o.faced]
        )
        for family in sorted(chosen, key=lambda f: -f.games)[:_CHOSEN_CAP]:
            if family.games < REPERTOIRE_SAMPLE_FLOOR:
                continue
            family_record = Record(
                games=family.games,
                wins=family.wins,
                losses=family.losses,
                draws=family.draws,
            )
            pairs.append(
                ComparisonInput(
                    label=f"{family.label} ({color})",
                    left_label="in this system",
                    left=family_record,
                    right_label=f"their other games as {color}",
                    right=_without(color_record, family_record),
                )
            )

    return build_comparisons(pairs)


def _without(whole: Record, part: Record) -> Record:
    """`whole` minus `part`, for the matched baseline of a bucket drawn
    from it. Clamped at zero: the two come from the same aggregation, so
    a negative count would be a bug rather than a state to represent,
    and a comparison against an empty record is simply not measurable.
    """
    return Record(
        games=max(0, whole.games - part.games),
        wins=max(0, whole.wins - part.wins),
        losses=max(0, whole.losses - part.losses),
        draws=max(0, whole.draws - part.draws),
    )


def _profile_openings(report: PlayerReport) -> list[ProfileOpening]:
    """Chosen + faced rows, capped per color (docs/06-coach.md, "Player
    profile"): the same family rollup the report prompt renders --
    partition by `faced`, chosen rolled up by (color, system), faced by
    (color, name root), the 5+ game sample floor applied per partition --
    then the top families per color: chosen by games played (what the
    player actually plays), faced by impact (games x win-rate deficit,
    the same sort the report tables use). Order: White chosen, White
    faced, Black chosen, Black faced.
    """
    rows: list[ProfileOpening] = []
    for color in _COLORS:
        color_rows = [o for o in report.openings if o.color == color]
        chosen = rollup_chosen_families([r for r in color_rows if not r.faced])
        faced = rollup_faced_families([r for r in color_rows if r.faced])
        chosen_main = [f for f in chosen if f.games >= REPERTOIRE_SAMPLE_FLOOR]
        faced_main = [f for f in faced if f.games >= REPERTOIRE_SAMPLE_FLOOR]

        top_chosen = sorted(
            chosen_main, key=lambda f: (-f.games, -family_impact(f), f.label)
        )[:_CHOSEN_CAP]
        top_faced = sorted(
            faced_main, key=lambda f: (-family_impact(f), -f.games, f.label)
        )[:_FACED_CAP]

        rows.extend(_chosen_opening(color, f) for f in top_chosen)
        rows.extend(_faced_opening(color, f) for f in top_faced)
    return rows


def _chosen_opening(color: Color, family: Family) -> ProfileOpening:
    return ProfileOpening(
        color=color,
        name=family.label,
        moves=family.system,
        games=family.games,
        score=family_score(family),
        faced=False,
        # Already move-weighted by the shared rollup; carried through
        # because score alone cannot distinguish a system that wins on
        # even positions from one whose openings the student survives
        # (docs/06-coach.md, "Player profile").
        opening_acpl=family.opening_acpl,
        avg_cp_loss=family.avg_cp_loss,
    )


def _faced_opening(color: Color, family: FacedFamily) -> ProfileOpening:
    return ProfileOpening(
        color=color,
        name=family.label,
        moves=family.first_moves,
        games=family.games,
        score=family_score(family),
        faced=True,
        opening_acpl=family.opening_acpl,
        avg_cp_loss=family.avg_cp_loss,
    )
