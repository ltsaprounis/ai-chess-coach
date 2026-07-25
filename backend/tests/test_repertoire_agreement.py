"""The two `OpeningStats` producers must agree.

`storage.opening_stats` (SQL, over classified games) and the coach's
`build_report` (Python, over analyzed games) independently build the
same domain type for the same player. The component boundary makes the
duplication structural — coach cannot import storage — so
docs/COACH-REPORT-IMPROVEMENTS.md settles it by defining the semantics
once, in docs/06-coach.md, and having both implement against that
definition. Nothing but this test checks that they actually did.

The formats matter beyond the numbers: `web/src/openings.ts` groups
repertoire rows by `system` as an **opaque key**, so a one-character
divergence between the two producers would split one family into two on
the Dashboard without failing anything else.
"""

from pathlib import Path

from chess_coach.coach import build_report
from chess_coach.domain import AnalyzedGame, Opening, OpeningStats
from chess_coach.storage import (
    open_db,
    opening_stats,
    save_analysis,
    set_opening,
    upsert_games,
)
from tests.coach_scenario import scenario_games
from tests.factories import make_analyzed

# Every field both producers populate. `analyzed_games` is included
# deliberately: it is a real sub-count in SQL and trivially equal to
# `games` in the coach path (which only ever sees analyzed games), and
# this fixture analyzes everything, so the two must still line up.
# `faced` is included for the same reason: both producers derive it from
# the same `Opening.ply` parity-majority rule
# (docs/fixes-2026-07/03-faced-openings.md), and a one-bit divergence
# would split one family across the wrong side of the chosen/faced
# rollup on the Dashboard without failing anything else.
COMPARED = (
    "system",
    "first_moves",
    "faced",
    "games",
    "wins",
    "losses",
    "draws",
    "analyzed_games",
    "opening_acpl",
    "avg_cp_loss",
)


_Key = tuple[str, str, str]  # (color, eco, name)


def _both_producers(
    games: list[AnalyzedGame], db_path: Path
) -> tuple[dict[_Key, OpeningStats], dict[_Key, OpeningStats]]:
    db = open_db(db_path)
    upsert_games(db, list(games))
    for game in games:
        if game.opening is not None:
            set_opening(db, game.id, game.opening)
        save_analysis(db, game.analysis)
    from_sql = {(o.color, o.eco, o.name): o for o in opening_stats(db, "testuser")}
    from_coach = {
        (o.color, o.eco, o.name): o for o in build_report("testuser", games).openings
    }
    return from_sql, from_coach


# One opening name, reached by two different player move orders. System A
# is the more-played *system* (4 games) but its opponents answered three
# different ways, so no single full line of A beats system B's 3 games
# played identically. Picking the representative by full line in one pass
# therefore reports B — the minority system — as this player's repertoire.
_SYSTEM_A = [
    "d4 d5 Nf3 Nf6 Bf4 e6",
    "d4 d5 Nf3 Nf6 Bf4 e6",
    "d4 d5 Nf3 c5 Bf4 Nc6",
    "d4 Nf6 Nf3 d5 Bf4 e6",
]
_SYSTEM_B = ["Nf3 d5 d4 Nf6 Bf4 e6"] * 3
_LONDON = Opening(eco="D02", name="Queen's Pawn Game: London System", ply=6)


def test_both_producers_pick_the_player_system_not_the_commonest_full_line(
    tmp_path: Path,
) -> None:
    games = [
        make_analyzed(
            f"g-{index}", line.split(), color="white", result="win", opening=_LONDON
        )
        for index, line in enumerate(_SYSTEM_A + _SYSTEM_B)
    ]
    from_sql, from_coach = _both_producers(games, tmp_path / "transposition.db")

    key: _Key = ("white", "D02", "Queen's Pawn Game: London System")
    for rows in (from_sql, from_coach):
        row = rows[key]
        assert row.system == "1.d4 2.Nf3 3.Bf4"
        assert row.first_moves == "1.d4 d5 2.Nf3 Nf6 3.Bf4 e6"


# One (color, eco, name) group reached by three different move orders
# (transpositions), each classifying the name at a different ply. Two of
# the three land on an opponent's (even, for White) ply, one on the
# player's own (odd) ply -- 2 of 3 is a strict majority, so both
# producers must resolve the row to faced (docs/06-coach.md,
# "Repertoire: keyed by the side the player had";
# docs/fixes-2026-07/03-faced-openings.md).
_MAJORITY_FACED_GAMES = [
    ("mf-1", "d4 e5", 2),  # opponent-named (Black's 1...e5)
    ("mf-2", "d4 Nf6 c4 e5", 4),  # opponent-named (Black's 2...e5)
    ("mf-3", "d4 d5 c4", 3),  # player-named (White's 2.c4)
]


def test_both_producers_resolve_faced_by_majority_across_transpositions(
    tmp_path: Path,
) -> None:
    games = [
        make_analyzed(
            game_id,
            line.split(),
            color="white",
            result="win",
            opening=Opening(eco="A40", name="Englund Gambit", ply=ply),
        )
        for game_id, line, ply in _MAJORITY_FACED_GAMES
    ]
    from_sql, from_coach = _both_producers(games, tmp_path / "faced-majority.db")

    key: _Key = ("white", "A40", "Englund Gambit")
    for rows in (from_sql, from_coach):
        row = rows[key]
        assert row.games == 3
        assert row.faced is True  # the rule the majority must resolve to
    assert from_sql[key].faced == from_coach[key].faced


# An even split (1 opponent-named, 1 player-named) is not a strict
# majority, so both producers must resolve the row to chosen, never
# faced -- "ties are chosen" (docs/06-coach.md).
_TIE_GAMES = [
    ("tf-1", "d4 e5", 2),  # opponent-named (Black's 1...e5)
    ("tf-2", "d4 d5", 1),  # player-named (White's 1.d4)
]


def test_both_producers_resolve_faced_ties_as_chosen(tmp_path: Path) -> None:
    games = [
        make_analyzed(
            game_id,
            line.split(),
            color="white",
            result="loss",
            opening=Opening(eco="A40", name="Englund Gambit", ply=ply),
        )
        for game_id, line, ply in _TIE_GAMES
    ]
    from_sql, from_coach = _both_producers(games, tmp_path / "faced-tie.db")

    key: _Key = ("white", "A40", "Englund Gambit")
    for rows in (from_sql, from_coach):
        row = rows[key]
        assert row.games == 2
        assert row.faced is False  # ties are chosen, never faced
    assert from_sql[key].faced == from_coach[key].faced


def test_storage_and_coach_build_the_same_repertoire(tmp_path: Path) -> None:
    games = scenario_games()
    db = open_db(tmp_path / "agreement.db")
    upsert_games(db, list(games))
    for game in games:
        if game.opening is not None:
            set_opening(db, game.id, game.opening)
        save_analysis(db, game.analysis)

    from_sql = {(o.color, o.eco, o.name): o for o in opening_stats(db, "testuser")}
    from_coach = {
        (o.color, o.eco, o.name): o for o in build_report("testuser", games).openings
    }

    assert from_sql.keys() == from_coach.keys()
    assert from_sql  # a fixture that classified nothing would pass vacuously
    for key in from_sql:
        sql_row, coach_row = from_sql[key], from_coach[key]
        for field in COMPARED:
            assert getattr(sql_row, field) == getattr(coach_row, field), (
                f"{key} disagrees on {field}"
            )
