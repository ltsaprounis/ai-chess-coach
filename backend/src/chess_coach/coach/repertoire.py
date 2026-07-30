"""Shared repertoire family rollup (docs/06-coach.md, "Family rollup").

Partitions `OpeningStats` rows by `faced`, then collapses each partition
into families -- chosen rolled up by (color, system), faced by (color,
name root) -- move-weighted throughout. Pure data, with exactly one
implementation: the report prompt's repertoire tables (`prompt.py`) and
the player profile's repertoire rows (`profile.py`, docs/06-coach.md
"Player profile") each sort, cap and render this output their own way,
but neither re-derives the rollup itself.
"""

from collections import defaultdict
from dataclasses import dataclass

from chess_coach.domain import OpeningStats

# The report's main tables and the profile's capped rows both require a
# family to clear this many games before it counts as more than long
# tail (docs/06-coach.md, "Sample floor and sort").
REPERTOIRE_SAMPLE_FLOOR = 5


@dataclass
class FamilyRecord:
    """Fields shared by both rollup partitions -- enough for impact/score."""

    games: int
    wins: int
    losses: int
    draws: int
    opening_acpl: float | None
    avg_cp_loss: float | None


@dataclass
class Family(FamilyRecord):
    """A chosen-partition family: one (color, system) rolled up."""

    label: str
    system: str
    first_moves: str


@dataclass
class FacedFamily(FamilyRecord):
    """A faced-partition family: one (color, name root) rolled up.

    No `system` -- for faced lines the name is the opponent's choice, and
    the player's own reply (hence `system`) varies member to member, so
    there is no single system to show (docs/06-coach.md, "Family
    rollup").
    """

    label: str
    first_moves: str


def rollup_chosen_families(rows: list[OpeningStats]) -> list[Family]:
    """Collapse the chosen partition by (color, system) -- rows already
    share one color and must be pre-filtered to `not faced`.

    Labels the family with its most-played member's name root (the name
    up to the first colon); ties broken by games, then eco, then name for
    determinism. Both ACPL columns re-weight by moves, not by games:
    `opening_acpl` by each row's `opening_moves`, `avg_cp_loss` by its
    `player_moves` -- the exact denominators `OpeningStats` carries for
    this reason. Weighting by game count instead would rebuild the
    mean-of-per-game-means one level above the row it was just removed
    from (docs/06-coach.md, "Family rollup").
    """
    groups: dict[str, list[OpeningStats]] = defaultdict(list)
    for row in rows:
        groups[row.system].append(row)

    families: list[Family] = []
    for system, members in groups.items():
        lead = min(members, key=lambda r: (-r.games, r.eco, r.name))
        families.append(
            Family(
                label=lead.name.split(":")[0].strip(),
                system=system,
                first_moves=lead.first_moves,
                games=sum(r.games for r in members),
                wins=sum(r.wins for r in members),
                losses=sum(r.losses for r in members),
                draws=sum(r.draws for r in members),
                opening_acpl=_weighted_mean(
                    [
                        (r.opening_acpl, r.opening_moves)
                        for r in members
                        if r.opening_acpl is not None
                    ]
                ),
                avg_cp_loss=_weighted_mean(
                    [
                        (r.avg_cp_loss, r.player_moves)
                        for r in members
                        if r.avg_cp_loss is not None
                    ]
                ),
            )
        )
    return families


def rollup_faced_families(rows: list[OpeningStats]) -> list[FacedFamily]:
    """Collapse the faced partition by (color, name root) -- rows already
    share one color and must be pre-filtered to `faced`.

    For faced lines the name *is* the opponent's choice, while the
    player's own system varies with their replies, so keying on `system`
    (as the chosen partition does) would split one opposing gambit across
    as many families as the player has tried answers to it. Summing and
    the move-weighted ACPL re-weighting are otherwise identical to
    `rollup_chosen_families` -- only the key differs (docs/06-coach.md,
    "Family rollup").
    """
    groups: dict[str, list[OpeningStats]] = defaultdict(list)
    for row in rows:
        groups[row.name.split(":")[0].strip()].append(row)

    families: list[FacedFamily] = []
    for label, members in groups.items():
        lead = min(members, key=lambda r: (-r.games, r.eco, r.name))
        families.append(
            FacedFamily(
                label=label,
                first_moves=lead.first_moves,
                games=sum(r.games for r in members),
                wins=sum(r.wins for r in members),
                losses=sum(r.losses for r in members),
                draws=sum(r.draws for r in members),
                opening_acpl=_weighted_mean(
                    [
                        (r.opening_acpl, r.opening_moves)
                        for r in members
                        if r.opening_acpl is not None
                    ]
                ),
                avg_cp_loss=_weighted_mean(
                    [
                        (r.avg_cp_loss, r.player_moves)
                        for r in members
                        if r.avg_cp_loss is not None
                    ]
                ),
            )
        )
    return families


def _weighted_mean(pairs: list[tuple[float, int]]) -> float | None:
    total_weight = sum(w for _, w in pairs)
    if not total_weight:
        return None
    return round(sum(v * w for v, w in pairs) / total_weight, 1)


def family_impact(f: FamilyRecord) -> float:
    """games x win-rate deficit -- so sample size, not raw rate, drives it
    (docs/06-coach.md, "Sample floor and sort"). Shared by the report
    prompt's table sort and the profile's faced-row cap.
    """
    score = (f.wins + f.draws / 2) / f.games if f.games else 0.0
    return f.games * (0.5 - score)


def family_score(f: FamilyRecord) -> float:
    """(wins + draws/2) / games, 0-1 -- `ProfileOpening.score`'s own
    shape. The report prompt rounds this into a percent string itself.
    """
    return (f.wins + f.draws / 2) / f.games if f.games else 0.0
