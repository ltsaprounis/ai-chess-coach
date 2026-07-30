"""Distill a PlayerReport into a PlayerProfile (docs/06-coach.md,
"Player profile").

Pure projection -- the aggregation itself runs once, in
`report.py:build_report`; this module adds no second implementation of
any semantic, including the repertoire family rollup, which is shared
with the report prompt through `chess_coach.coach.repertoire`.
"""

from chess_coach.coach.repertoire import (
    REPERTOIRE_SAMPLE_FLOOR,
    FacedFamily,
    Family,
    family_impact,
    family_score,
    rollup_chosen_families,
    rollup_faced_families,
)
from chess_coach.domain import Color, PlayerProfile, PlayerReport, ProfileOpening

# Trend rows kept in the profile -- enough to show direction without
# blowing render_profile_context's ~250-token budget (docs/06-coach.md).
_MONTHS_CAP = 6

# Repertoire rows kept per color: chosen capped by games (what the player
# actually plays), faced capped by impact (what actually hurts them) --
# the same distinction docs/06-coach.md draws for the profile's caps.
_CHOSEN_CAP = 3
_FACED_CAP = 2

_COLORS: tuple[Color, ...] = ("white", "black")


def build_profile(report: PlayerReport) -> PlayerProfile:
    """Pure distillation of an already-built report (docs/06-coach.md,
    "Player profile"). `narrative` stays None -- the API layer attaches
    the stored narrative when one exists.

    Total over an empty report: every field here is either a direct copy
    of a report field (already `[]`/`0`/`0.0` for a report with no
    analyzed games) or a rollup over `report.openings` (already `[]`), so
    no branch below needs an explicit empty-report special case.
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
        openings=_profile_openings(report),
        error_patterns=report.error_patterns,
        narrative=None,
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
    )


def _faced_opening(color: Color, family: FacedFamily) -> ProfileOpening:
    return ProfileOpening(
        color=color,
        name=family.label,
        moves=family.first_moves,
        games=family.games,
        score=family_score(family),
        faced=True,
    )
