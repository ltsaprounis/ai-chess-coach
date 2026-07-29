"""Storage component tests (docs/03-storage.md)."""

import json
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from chess_coach.domain import GameAnalysis, MoveEval, Opening
from chess_coach.storage import (
    CachedReport,
    Db,
    GameFilters,
    ReportKey,
    count_games,
    count_games_needing_analysis,
    games_missing_opening,
    games_needing_analysis,
    get_explanation,
    get_game,
    get_report,
    latest_game_time,
    list_analyzed_games,
    list_games,
    list_players,
    list_repertoire_games,
    open_db,
    opening_stats,
    save_analysis,
    save_explanation,
    save_report,
    set_opening,
    upsert_games,
)
from tests.factories import make_analysis, make_game

RUY_LOPEZ = Opening(eco="C60", name="Ruy Lopez", ply=5)


def _move_eval(ply: int, cp_loss: int) -> MoveEval:
    """A minimal player-move eval — only ply and cp_loss matter here."""
    return MoveEval(
        ply=ply,
        san="m",
        eval_cp=0,
        eval_mate=None,
        best_move="m",
        cp_loss=cp_loss,
        judgment="best",
    )


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Db]:
    connection = open_db(tmp_path / "test.sqlite3")
    yield connection
    connection.close()


def test_upsert_and_list_round_trip(db: Db) -> None:
    older = make_game(id="g-old", end_time=100)
    newer = make_game(id="g-new", end_time=200)
    upsert_games(db, [older, newer])

    games = list_games(db, "testuser", GameFilters())
    assert [g.id for g in games] == ["g-new", "g-old"]  # newest first
    assert games[0].analyzed is False
    assert games[0].opening is None
    assert games[0].first_plies == ["e4", "e5"]  # shorter than 6, kept whole


def test_list_games_first_plies_is_capped_at_six_and_matches_the_prefix(
    db: Db,
) -> None:
    """`first_plies` is the exact prefix `playerSystem()` needs client-
    side — at most 6 SAN plies, and never anything but the game's own
    opening moves in order (docs/03-storage.md, "GameSummary")."""
    long_moves = ["d4", "Nf6", "c4", "e6", "Nc3", "Bb4", "e3", "O-O", "Bd3", "d5"]
    upsert_games(db, [make_game(san_moves=long_moves)])

    (summary,) = list_games(db, "testuser", GameFilters())
    assert summary.first_plies == long_moves[:6]
    assert len(summary.first_plies) == 6


def test_upsert_is_idempotent_and_refreshes_accuracy(db: Db) -> None:
    upsert_games(db, [make_game(accuracy=None)])
    upsert_games(db, [make_game(accuracy=91.2)])

    games = list_games(db, "testuser", GameFilters())
    assert len(games) == 1
    assert games[0].accuracy == 91.2


def test_filters(db: Db) -> None:
    win = make_game(id="g-win", result="win", time_class="blitz", end_time=1)
    loss = make_game(id="g-loss", result="loss", time_class="rapid", end_time=2)
    upsert_games(db, [win, loss])
    set_opening(db, "g-win", RUY_LOPEZ)
    save_analysis(db, make_analysis(game_id="g-loss"), version=1)

    def ids(filters: GameFilters) -> list[str]:
        return [g.id for g in list_games(db, "testuser", filters)]

    assert ids(GameFilters(result="win")) == ["g-win"]
    assert ids(GameFilters(time_class="rapid")) == ["g-loss"]
    assert ids(GameFilters(opening_eco="C60")) == ["g-win"]
    assert ids(GameFilters(analyzed=True)) == ["g-loss"]
    assert ids(GameFilters(analyzed=False)) == ["g-win"]
    assert ids(GameFilters(limit=1)) == ["g-loss"]  # newest first
    assert ids(GameFilters(limit=1, offset=1)) == ["g-win"]


def test_list_players(db: Db) -> None:
    assert list_players(db) == []
    upsert_games(
        db,
        [
            make_game(id="a1", username="alice", end_time=10),
            make_game(id="a2", username="alice", end_time=20),
            make_game(id="b1", username="bob", end_time=15),
        ],
    )
    assert [(p.username, p.games, p.last_played) for p in list_players(db)] == [
        ("alice", 2, 20),  # most games first
        ("bob", 1, 15),
    ]


def test_latest_game_time(db: Db) -> None:
    assert latest_game_time(db, "testuser") is None
    upsert_games(db, [make_game(id="a", end_time=10), make_game(id="b", end_time=99)])
    assert latest_game_time(db, "testuser") == 99
    assert latest_game_time(db, "someone-else") is None


def test_set_opening_shows_up_in_detail_and_list(db: Db) -> None:
    upsert_games(db, [make_game()])
    set_opening(db, "game-1", RUY_LOPEZ)

    detail = get_game(db, "game-1")
    assert detail is not None
    assert detail.opening == RUY_LOPEZ
    assert detail.analysis is None
    assert list_games(db, "testuser", GameFilters())[0].opening == RUY_LOPEZ


def test_get_game_returns_none_for_unknown_id(db: Db) -> None:
    assert get_game(db, "nope") is None


def test_analysis_round_trip(db: Db) -> None:
    upsert_games(db, [make_game()])
    analysis = make_analysis()
    save_analysis(db, analysis, version=1)

    detail = get_game(db, "game-1")
    assert detail is not None
    assert detail.analysis == analysis
    assert [g.analysis for g in list_analyzed_games(db, "testuser")] == [analysis]


def test_save_analysis_round_trips_version(db: Db) -> None:
    """`version` is not part of `GameAnalysis` (the API layer injects it,
    like depth/thresholds), so it is read back with a raw query."""
    upsert_games(db, [make_game()])
    save_analysis(db, make_analysis(), version=3)

    (row,) = db.execute(
        "SELECT analysis_version FROM analyses WHERE game_id = ?", ("game-1",)
    ).fetchall()
    assert row["analysis_version"] == 3

    save_analysis(db, make_analysis(), version=4)  # upsert overwrites
    (row,) = db.execute(
        "SELECT analysis_version FROM analyses WHERE game_id = ?", ("game-1",)
    ).fetchall()
    assert row["analysis_version"] == 4


def test_games_needing_analysis_limit_takes_newest(db: Db) -> None:
    upsert_games(
        db,
        [
            make_game(id="g-1", end_time=1),
            make_game(id="g-2", end_time=2),
            make_game(id="g-3", end_time=3),
        ],
    )
    assert [g.id for g in games_needing_analysis(db, "testuser", 16, 1, limit=2)] == [
        "g-3",
        "g-2",
    ]
    assert count_games_needing_analysis(db, "testuser", 16, 1) == 3

    save_analysis(db, make_analysis(game_id="g-3"), version=1)
    assert count_games_needing_analysis(db, "testuser", 16, 1) == 2


def test_games_needing_analysis_respects_depth(db: Db) -> None:
    upsert_games(db, [make_game()])
    assert [g.id for g in games_needing_analysis(db, "testuser", 16, 1)] == ["game-1"]

    save_analysis(db, make_analysis(depth=10), version=1)
    assert [g.id for g in games_needing_analysis(db, "testuser", 16, 1)] == ["game-1"]

    save_analysis(db, make_analysis(depth=16), version=1)
    assert games_needing_analysis(db, "testuser", 16, 1) == []


def test_games_needing_analysis_respects_version(db: Db) -> None:
    """A game analysed at the configured depth but under an older
    `analysis_version` still needs (re-)analysis; one saved at the
    current version does not (docs/archive/engine-search-hangs.md,
    "Re-analysing the existing rows")."""
    upsert_games(db, [make_game()])
    save_analysis(db, make_analysis(depth=16), version=1)
    assert games_needing_analysis(db, "testuser", 16, 1) == []
    assert count_games_needing_analysis(db, "testuser", 16, 1) == 0

    # depth still meets the bar, but the row was saved under version 1
    # and the caller now wants version 2.
    assert [g.id for g in games_needing_analysis(db, "testuser", 16, 2)] == ["game-1"]
    assert count_games_needing_analysis(db, "testuser", 16, 2) == 1

    save_analysis(db, make_analysis(depth=16), version=2)
    assert games_needing_analysis(db, "testuser", 16, 2) == []
    assert count_games_needing_analysis(db, "testuser", 16, 2) == 0


def test_games_needing_analysis_time_window_edges(db: Db) -> None:
    """Mirrors `list_analyzed_games`'s window semantics: `since`
    inclusive, `until` exclusive."""
    upsert_games(
        db,
        [make_game(id="old", end_time=100), make_game(id="recent", end_time=200)],
    )

    def ids(*, since: int | None = None, until: int | None = None) -> list[str]:
        return [
            g.id
            for g in games_needing_analysis(
                db, "testuser", 16, 1, since=since, until=until
            )
        ]

    assert ids() == ["recent", "old"]  # newest first
    assert ids(since=150) == ["recent"]
    assert ids(until=150) == ["old"]
    assert ids(since=200) == ["recent"]  # since is inclusive
    assert ids(until=200) == ["old"]  # until is exclusive

    assert count_games_needing_analysis(db, "testuser", 16, 1, since=150) == 1
    assert count_games_needing_analysis(db, "testuser", 16, 1, until=150) == 1
    assert count_games_needing_analysis(db, "testuser", 16, 1, since=200) == 1
    assert count_games_needing_analysis(db, "testuser", 16, 1, until=200) == 1


def test_games_needing_analysis_time_class_filter(db: Db) -> None:
    upsert_games(
        db,
        [
            make_game(id="rapid", time_class="rapid"),
            make_game(id="blitz", time_class="blitz"),
        ],
    )

    assert [
        g.id for g in games_needing_analysis(db, "testuser", 16, 1, time_class="rapid")
    ] == ["rapid"]
    assert count_games_needing_analysis(db, "testuser", 16, 1, time_class="rapid") == 1
    assert count_games_needing_analysis(db, "testuser", 16, 1, time_class="blitz") == 1


def test_games_needing_analysis_and_count_agree_on_a_mixed_fixture(db: Db) -> None:
    """A mix of analyzed/unanalyzed games in and out of a scoped window:
    the list's length and the dedicated count must describe the same
    scope, with no `limit` to truncate either side."""
    upsert_games(
        db,
        [
            make_game(id="old-unanalyzed", end_time=50, time_class="rapid"),
            make_game(id="old-analyzed", end_time=60, time_class="rapid"),
            make_game(id="in-window-unanalyzed", end_time=150, time_class="rapid"),
            make_game(id="in-window-analyzed", end_time=160, time_class="rapid"),
            make_game(id="in-window-shallow", end_time=170, time_class="rapid"),
            make_game(id="in-window-other-class", end_time=180, time_class="blitz"),
            make_game(id="future-unanalyzed", end_time=300, time_class="rapid"),
        ],
    )
    save_analysis(db, make_analysis(game_id="old-analyzed", depth=16), version=1)
    save_analysis(db, make_analysis(game_id="in-window-analyzed", depth=16), version=1)
    save_analysis(db, make_analysis(game_id="in-window-shallow", depth=8), version=1)

    found = games_needing_analysis(
        db, "testuser", 16, 1, since=100, until=200, time_class="rapid"
    )
    count = count_games_needing_analysis(
        db, "testuser", 16, 1, since=100, until=200, time_class="rapid"
    )

    assert {g.id for g in found} == {
        "in-window-unanalyzed",
        "in-window-shallow",
    }
    assert len(found) == count


def test_games_needing_analysis_filters_compose_with_limit(db: Db) -> None:
    """`limit` applies after the window/time-class scoping, not before."""
    upsert_games(
        db,
        [
            make_game(id="out-of-window", end_time=50, time_class="rapid"),
            make_game(id="wrong-class", end_time=150, time_class="blitz"),
            make_game(id="in-scope-1", end_time=160, time_class="rapid"),
            make_game(id="in-scope-2", end_time=170, time_class="rapid"),
            make_game(id="in-scope-3", end_time=180, time_class="rapid"),
        ],
    )

    result = games_needing_analysis(
        db, "testuser", 16, 1, limit=2, since=100, until=200, time_class="rapid"
    )
    assert [g.id for g in result] == ["in-scope-3", "in-scope-2"]


def test_opening_stats_aggregates_records_most_played_first(db: Db) -> None:
    upsert_games(
        db,
        [
            make_game(id="r1", result="win", end_time=1),
            make_game(id="r2", result="loss", end_time=2),
            make_game(id="r3", result="draw", end_time=3),
            make_game(id="q1", result="win", end_time=4),
            make_game(id="n1", result="win", end_time=5),  # stays unclassified
        ],
    )
    for game_id in ("r1", "r2", "r3"):
        set_opening(db, game_id, RUY_LOPEZ)
    set_opening(db, "q1", Opening(eco="D06", name="Queen's Gambit", ply=3))

    stats = opening_stats(db, "testuser")
    assert [(s.eco, s.games, s.wins, s.losses, s.draws) for s in stats] == [
        ("C60", 3, 1, 1, 1),
        ("D06", 1, 1, 0, 0),
    ]
    assert all(s.avg_cp_loss is None for s in stats)
    assert all(s.analyzed_games == 0 for s in stats)


def test_opening_stats_time_window(db: Db) -> None:
    upsert_games(
        db,
        [
            make_game(id="old", result="win", end_time=100),
            make_game(id="recent", result="loss", end_time=200),
        ],
    )
    for game_id in ("old", "recent"):
        set_opening(db, game_id, RUY_LOPEZ)

    def record(
        *, since: int | None = None, until: int | None = None
    ) -> tuple[int, int, int]:
        (stat,) = opening_stats(db, "testuser", since=since, until=until)
        return stat.games, stat.wins, stat.losses

    assert record(since=150) == (1, 0, 1)  # only the recent loss
    assert record(until=150) == (1, 1, 0)  # only the old win
    assert record(since=200) == (1, 0, 1)  # since is inclusive
    assert record(until=200) == (1, 1, 0)  # until is exclusive


def test_opening_stats_splits_by_color(db: Db) -> None:
    """Two colors of one opening are two rows, never merged.

    Grouping by (eco, name) alone would fold the games the player chose
    the Ruy Lopez in together with the games where they only faced it
    as Black — the Englund-Gambit-shaped bug this whole table exists to
    fix (docs/06-coach.md, "Repertoire: keyed by the side the player
    had").
    """
    white_game = make_game(id="w1", color="white", result="win")
    black_game = make_game(id="b1", color="black", result="loss")
    upsert_games(db, [white_game, black_game])
    set_opening(db, "w1", RUY_LOPEZ)
    set_opening(db, "b1", RUY_LOPEZ)

    stats = opening_stats(db, "testuser")
    assert {(s.color, s.games, s.wins, s.losses) for s in stats} == {
        ("white", 1, 1, 0),
        ("black", 1, 0, 1),
    }


ENGLUND = Opening(eco="A40", name="Englund Gambit", ply=2)
LONDON = Opening(eco="D02", name="London System", ply=3)


def test_opening_stats_faced_true_for_englund_as_white(db: Db) -> None:
    """The Englund is named by 1...e5 (ply 2, Black's move) — a White
    player's Englund rows are opponent-named, i.e. faced (docs/06-coach.md,
    "Repertoire: keyed by the side the player had")."""
    upsert_games(db, [make_game(id="w1", color="white", san_moves=["d4", "e5"])])
    set_opening(db, "w1", ENGLUND)

    (stat,) = opening_stats(db, "testuser")
    assert stat.faced is True


def test_opening_stats_faced_is_false_for_a_chosen_system(db: Db) -> None:
    """The London is named at ply 3 (White's own move) — a White
    player's London rows are player-named, i.e. chosen, never faced."""
    upsert_games(db, [make_game(id="w1", color="white", san_moves=["d4", "d5", "Bf4"])])
    set_opening(db, "w1", LONDON)

    (stat,) = opening_stats(db, "testuser")
    assert stat.faced is False


def test_opening_stats_faced_follows_majority_across_transpositions(db: Db) -> None:
    """Transpositions can reach one (color, eco, name) group at different
    plies; `faced` follows the strict majority of the group's games, not
    any single game's parity."""
    upsert_games(
        db,
        [
            make_game(id="w1", color="white", san_moves=["d4", "e5"]),
            make_game(id="w2", color="white", san_moves=["d4", "Nf6", "c4", "e5"]),
            make_game(id="w3", color="white", san_moves=["d4", "d5", "c4"]),
        ],
    )
    # Two games classify at an even, opponent's ply; one at an odd, the
    # player's own ply. 2 of 3 is a strict majority, so the row is faced.
    set_opening(db, "w1", Opening(eco="A40", name="Englund Gambit", ply=2))
    set_opening(db, "w2", Opening(eco="A40", name="Englund Gambit", ply=4))
    set_opening(db, "w3", Opening(eco="A40", name="Englund Gambit", ply=3))

    (stat,) = opening_stats(db, "testuser")
    assert stat.games == 3
    assert stat.faced is True


def test_opening_stats_faced_ties_are_chosen(db: Db) -> None:
    """An even split between opponent-named and player-named games is not
    a strict majority, so the row is chosen, never faced."""
    upsert_games(
        db,
        [
            make_game(id="w1", color="white", san_moves=["d4", "e5"]),
            make_game(id="w2", color="white", san_moves=["d4", "d5"]),
        ],
    )
    set_opening(db, "w1", Opening(eco="A40", name="Englund Gambit", ply=2))
    set_opening(db, "w2", Opening(eco="A40", name="Englund Gambit", ply=1))

    (stat,) = opening_stats(db, "testuser")
    assert stat.games == 2
    assert stat.faced is False


def test_opening_stats_system_and_first_moves_as_white(db: Db) -> None:
    game = make_game(
        id="w1", color="white", san_moves=["d4", "Nf6", "Nf3", "d5", "Bg5"]
    )
    upsert_games(db, [game])
    set_opening(db, "w1", RUY_LOPEZ)

    (stat,) = opening_stats(db, "testuser")
    assert stat.system == "1.d4 2.Nf3 3.Bg5"  # the player's own moves only
    assert stat.first_moves == "1.d4 Nf6 2.Nf3 d5 3.Bg5"  # both sides


def test_opening_stats_system_and_first_moves_as_black(db: Db) -> None:
    game = make_game(
        id="b1", color="black", san_moves=["e4", "d6", "d4", "Nf6", "Nc3", "g6"]
    )
    upsert_games(db, [game])
    set_opening(db, "b1", RUY_LOPEZ)

    (stat,) = opening_stats(db, "testuser")
    assert stat.system == "1...d6 2...Nf6 3...g6"  # the player's own moves only
    assert stat.first_moves == "1.e4 d6 2.d4 Nf6 3.Nc3 g6"  # both sides


def test_opening_stats_system_uses_most_played_line(db: Db) -> None:
    """The majority line wins even when it was not inserted first."""
    minority = make_game(id="g-b1", color="white", san_moves=["d4", "d5", "c4"])
    majority_1 = make_game(id="g-a1", color="white", san_moves=["d4", "Nf6", "c4"])
    majority_2 = make_game(id="g-a2", color="white", san_moves=["d4", "Nf6", "c4"])
    upsert_games(db, [minority, majority_1, majority_2])
    for game_id in ("g-b1", "g-a1", "g-a2"):
        set_opening(db, game_id, RUY_LOPEZ)

    (stat,) = opening_stats(db, "testuser")
    assert stat.system == "1.d4 2.c4"
    assert stat.first_moves == "1.d4 Nf6 2.c4"


def test_opening_stats_line_tie_break_uses_lowest_game_id(db: Db) -> None:
    """A 1-1 tie in game count is broken deterministically, not by
    insertion order."""
    tied_high_id = make_game(id="g-9", color="white", san_moves=["e4", "e5"])
    tied_low_id = make_game(id="g-1", color="white", san_moves=["d4", "Nf6"])
    upsert_games(db, [tied_high_id, tied_low_id])  # inserted high-id first
    for game_id in ("g-9", "g-1"):
        set_opening(db, game_id, RUY_LOPEZ)

    (stat,) = opening_stats(db, "testuser")
    assert stat.system == "1.d4"
    assert stat.first_moves == "1.d4 Nf6"


def test_opening_stats_system_follows_the_player_line_not_the_full_line(
    db: Db,
) -> None:
    """The most-played *player* sequence wins even when some other,
    less-played player sequence happens to have the single most-played
    full (both-sides) line.

    Six games share the player sequence "d4, Nf3, Bf4" (system A), but
    its games split 3/2/1 across three different Black setups, so no
    single full line backs more than 3 of them. Four games share a
    different player sequence "d4, Bf4, e3" (system B), all against the
    same Black setup, so system B's full line is backed by 4 games —
    more than any single system-A full line. `system` must still come
    from A (6 games beats 4), and `first_moves` from A's own
    most-played full line (the 3-game bucket), never from B's.
    """
    bucket1 = [
        make_game(
            id=f"a1-{i}",
            color="white",
            san_moves=["d4", "d5", "Nf3", "Nf6", "Bf4", "e6"],
        )
        for i in range(3)
    ]
    bucket2 = [
        make_game(
            id=f"a2-{i}",
            color="white",
            san_moves=["d4", "d5", "Nf3", "Nf6", "Bf4", "c5"],
        )
        for i in range(2)
    ]
    bucket3 = [
        make_game(
            id="a3-0", color="white", san_moves=["d4", "g6", "Nf3", "Bg7", "Bf4", "c6"]
        )
    ]
    system_b = [
        make_game(
            id=f"b-{i}",
            color="white",
            san_moves=["d4", "e6", "Bf4", "Nf6", "e3", "Be7"],
        )
        for i in range(4)
    ]
    games = bucket1 + bucket2 + bucket3 + system_b
    upsert_games(db, games)
    for game in games:
        set_opening(db, game.id, RUY_LOPEZ)

    (stat,) = opening_stats(db, "testuser")
    assert stat.games == 10
    assert stat.system == "1.d4 2.Nf3 3.Bf4"  # system A (6 games), not B (4)
    assert stat.first_moves == "1.d4 d5 2.Nf3 Nf6 3.Bf4 e6"  # A's own top bucket


def test_opening_stats_acpl_is_move_weighted_not_a_mean_of_means(db: Db) -> None:
    """A 1-move game and a 5-move game must not weigh equally.

    A naive mean of per-game means would average the short game's 100
    cp loss with the long game's 0 and report 50.0. Move-weighted, the
    100 is spread over all 6 player moves across both games: 16.7.
    """
    short_game = make_game(id="short", color="white", end_time=1)
    long_game = make_game(id="long", color="white", end_time=2)
    upsert_games(db, [short_game, long_game])
    set_opening(db, "short", RUY_LOPEZ)
    set_opening(db, "long", RUY_LOPEZ)

    save_analysis(
        db,
        GameAnalysis(
            game_id="short",
            depth=16,
            evals=[_move_eval(1, 100)],  # one player move (ply 1, White)
            overall_acpl=100.0,
            acpl_by_phase={"opening": 100.0, "middlegame": 0.0, "endgame": 0.0},
            judgment_counts={
                "best": 0,
                "good": 0,
                "inaccuracy": 0,
                "mistake": 1,
                "blunder": 0,
            },
        ),
        version=1,
    )
    save_analysis(
        db,
        GameAnalysis(
            game_id="long",
            depth=16,
            # plies 1,3,5,7,9 are the player's (White); all lose nothing.
            evals=[_move_eval(ply, 0) for ply in range(1, 10)],
            overall_acpl=0.0,
            acpl_by_phase={"opening": 0.0, "middlegame": 0.0, "endgame": 0.0},
            judgment_counts={
                "best": 9,
                "good": 0,
                "inaccuracy": 0,
                "mistake": 0,
                "blunder": 0,
            },
        ),
        version=1,
    )

    (stat,) = opening_stats(db, "testuser")
    assert stat.avg_cp_loss == 16.7  # 100 / (1 + 5), not (100 + 0) / 2 == 50.0
    assert stat.analyzed_games == 2
    # The denominator is the real move count across both games (6),
    # never the game count (2) — that is what keeps a rollup that
    # re-weights by these move-weighted.
    assert stat.player_moves == 6
    assert stat.opening_moves == 6  # all plies here are <= OPENING_PLIES


def test_opening_stats_opening_acpl_excludes_later_phases(db: Db) -> None:
    """opening_acpl restricts to opening-phase player moves; avg_cp_loss
    does not."""
    game = make_game(id="g1", color="white", end_time=1)
    upsert_games(db, [game])
    set_opening(db, "g1", RUY_LOPEZ)

    save_analysis(
        db,
        GameAnalysis(
            game_id="g1",
            depth=16,
            evals=[
                _move_eval(1, 20),  # ply 1 <= OPENING_PLIES: opening
                _move_eval(21, 300),  # ply 21 > OPENING_PLIES: middlegame
            ],
            overall_acpl=160.0,
            acpl_by_phase={"opening": 20.0, "middlegame": 300.0, "endgame": 0.0},
            judgment_counts={
                "best": 1,
                "good": 0,
                "inaccuracy": 0,
                "mistake": 0,
                "blunder": 1,
            },
        ),
        version=1,
    )

    (stat,) = opening_stats(db, "testuser")
    assert stat.opening_acpl == 20.0
    assert stat.avg_cp_loss == 160.0  # (20 + 300) / 2, whole game
    # The two ACPL columns have different denominators: opening_moves
    # counts only the opening-phase player move, player_moves counts
    # both player moves in the game.
    assert stat.opening_moves == 1
    assert stat.player_moves == 2


def test_list_analyzed_games_time_window(db: Db) -> None:
    upsert_games(
        db,
        [make_game(id="old", end_time=100), make_game(id="recent", end_time=200)],
    )
    save_analysis(db, make_analysis(game_id="old"), version=1)
    save_analysis(db, make_analysis(game_id="recent"), version=1)

    def ids(*, since: int | None = None, until: int | None = None) -> list[str]:
        return [
            g.id for g in list_analyzed_games(db, "testuser", since=since, until=until)
        ]

    assert ids() == ["recent", "old"]  # newest first
    assert ids(since=150) == ["recent"]
    assert ids(until=150) == ["old"]
    assert ids(since=200) == ["recent"]  # since is inclusive
    assert ids(until=200) == ["old"]  # until is exclusive


def test_time_class_filter(db: Db) -> None:
    upsert_games(
        db,
        [
            make_game(id="rapid", time_class="rapid"),
            make_game(id="blitz", time_class="blitz"),
        ],
    )
    save_analysis(db, make_analysis(game_id="rapid"), version=1)
    save_analysis(db, make_analysis(game_id="blitz"), version=1)
    for game_id in ("rapid", "blitz"):
        set_opening(db, game_id, RUY_LOPEZ)

    assert [g.id for g in list_analyzed_games(db, "testuser", time_class="rapid")] == [
        "rapid"
    ]
    (stat,) = opening_stats(db, "testuser", time_class="blitz")
    assert stat.games == 1
    assert stat.analyzed_games == 1


def test_count_games_time_window_edges(db: Db) -> None:
    """Mirrors `list_analyzed_games`'s window semantics exactly: `since`
    inclusive, `until` exclusive — the numerator and denominator behind
    the report's coverage statement must describe the same scope."""
    upsert_games(
        db,
        [make_game(id="old", end_time=100), make_game(id="recent", end_time=200)],
    )

    assert count_games(db, "testuser") == 2
    assert count_games(db, "testuser", since=150) == 1  # only "recent"
    assert count_games(db, "testuser", until=150) == 1  # only "old"
    assert count_games(db, "testuser", since=200) == 1  # since is inclusive
    assert count_games(db, "testuser", until=200) == 1  # until is exclusive


def test_count_games_time_class_filter(db: Db) -> None:
    upsert_games(
        db,
        [
            make_game(id="rapid", time_class="rapid"),
            make_game(id="blitz", time_class="blitz"),
        ],
    )

    assert count_games(db, "testuser", time_class="rapid") == 1
    assert count_games(db, "testuser", time_class="blitz") == 1
    assert count_games(db, "testuser") == 2


def test_count_games_includes_unanalyzed_games(db: Db) -> None:
    """The denominator counts every stored game, analyzed or not — it
    must never collapse onto `list_analyzed_games`'s numerator."""
    upsert_games(
        db,
        [
            make_game(id="analyzed-1", end_time=1),
            make_game(id="analyzed-2", end_time=2),
            make_game(id="unanalyzed-1", end_time=3),
            make_game(id="unanalyzed-2", end_time=4),
        ],
    )
    save_analysis(db, make_analysis(game_id="analyzed-1"), version=1)
    save_analysis(db, make_analysis(game_id="analyzed-2"), version=1)

    assert count_games(db, "testuser") == 4
    assert len(list_analyzed_games(db, "testuser")) == 2
    assert count_games(db, "testuser") > len(list_analyzed_games(db, "testuser"))


def test_count_games_scopes_to_user_with_no_filters(db: Db) -> None:
    upsert_games(
        db,
        [
            make_game(id="a1", username="alice", end_time=1),
            make_game(id="a2", username="alice", end_time=2),
            make_game(id="b1", username="bob", end_time=1),
        ],
    )

    assert count_games(db, "alice") == 2
    assert count_games(db, "bob") == 1
    assert count_games(db, "someone-else") == 0


def test_list_repertoire_games_includes_unanalyzed_with_none_evals(db: Db) -> None:
    """All stored games come back, analyzed or not — the LEFT JOIN
    leaves `evals` as None rather than dropping the row."""
    upsert_games(
        db,
        [
            make_game(id="analyzed", end_time=1),
            make_game(id="unanalyzed", end_time=2),
        ],
    )
    analysis = make_analysis(game_id="analyzed")
    save_analysis(db, analysis, version=1)

    games = {g.id: g for g in list_repertoire_games(db, "testuser", max_plies=30)}
    assert games.keys() == {"analyzed", "unanalyzed"}
    assert games["analyzed"].evals == analysis.evals
    assert games["unanalyzed"].evals is None
    assert games["unanalyzed"].san_moves == ["e4", "e5"]


def test_list_repertoire_games_slices_san_moves_and_evals(db: Db) -> None:
    """Both `san_moves` and `evals` are capped to `max_plies` inside
    storage — the documented exception to `pgn`/full `san_moves` never
    crossing the boundary (docs/03-storage.md)."""
    moves = ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4", "Nf6"]
    upsert_games(db, [make_game(san_moves=moves)])
    analysis = GameAnalysis(
        game_id="game-1",
        depth=16,
        evals=[_move_eval(ply, 0) for ply in range(1, len(moves) + 1)],
        overall_acpl=0.0,
        acpl_by_phase={"opening": 0.0, "middlegame": 0.0, "endgame": 0.0},
        judgment_counts={
            "best": len(moves),
            "good": 0,
            "inaccuracy": 0,
            "mistake": 0,
            "blunder": 0,
        },
    )
    save_analysis(db, analysis, version=1)

    (game,) = list_repertoire_games(db, "testuser", max_plies=4)
    assert game.san_moves == moves[:4]
    assert game.evals is not None
    assert [e.ply for e in game.evals] == [1, 2, 3, 4]


def test_list_repertoire_games_time_window(db: Db) -> None:
    """Mirrors `list_analyzed_games`'s window semantics exactly: `since`
    inclusive, `until` exclusive."""
    upsert_games(
        db,
        [make_game(id="old", end_time=100), make_game(id="recent", end_time=200)],
    )

    def ids(*, since: int | None = None, until: int | None = None) -> list[str]:
        return [
            g.id
            for g in list_repertoire_games(
                db, "testuser", max_plies=30, since=since, until=until
            )
        ]

    assert ids() == ["recent", "old"]  # newest first
    assert ids(since=150) == ["recent"]
    assert ids(until=150) == ["old"]
    assert ids(since=200) == ["recent"]  # since is inclusive
    assert ids(until=200) == ["old"]  # until is exclusive


def test_list_repertoire_games_time_class_filter(db: Db) -> None:
    upsert_games(
        db,
        [
            make_game(id="rapid", time_class="rapid"),
            make_game(id="blitz", time_class="blitz"),
        ],
    )

    assert [
        g.id
        for g in list_repertoire_games(db, "testuser", max_plies=30, time_class="rapid")
    ] == ["rapid"]


def test_list_repertoire_games_unknown_username_returns_empty(db: Db) -> None:
    upsert_games(db, [make_game()])
    assert list_repertoire_games(db, "someone-else", max_plies=30) == []


def test_games_missing_opening(db: Db) -> None:
    upsert_games(db, [make_game(id="a", end_time=1), make_game(id="b", end_time=2)])
    set_opening(db, "a", RUY_LOPEZ)
    assert [g.id for g in games_missing_opening(db, "testuser")] == ["b"]


def test_termination_round_trip(db: Db) -> None:
    upsert_games(db, [make_game(termination="resigned")])

    detail = get_game(db, "game-1")
    assert detail is not None
    assert detail.termination == "resigned"
    assert list_games(db, "testuser", GameFilters())[0].termination == "resigned"


def test_termination_is_none_until_resync_backfills_it(db: Db) -> None:
    """Existing rows predate the column and stay NULL until re-synced.

    A re-sync (a fresh upsert_games call for the same id) must backfill
    termination — the entire migration story for pre-existing games.
    """
    upsert_games(db, [make_game(termination=None)])
    detail = get_game(db, "game-1")
    assert detail is not None
    assert detail.termination is None

    upsert_games(db, [make_game(termination="timeout")])
    detail = get_game(db, "game-1")
    assert detail is not None
    assert detail.termination == "timeout"


def test_reopen_persists_data_and_migrations_are_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "persist.sqlite3"
    first = open_db(path)
    upsert_games(first, [make_game()])
    first.close()

    second = open_db(path)  # migrations must be a no-op here
    assert [g.id for g in list_games(second, "testuser", GameFilters())] == ["game-1"]
    second.close()


def test_get_explanation_misses_when_absent(db: Db) -> None:
    upsert_games(db, [make_game()])
    assert get_explanation(db, "game-1", 1, "coach-a") is None


def test_save_and_get_explanation_round_trip(db: Db) -> None:
    upsert_games(db, [make_game()])
    save_explanation(db, "game-1", 1, "coach-a", "1. e4 is a strong opening move.")

    assert (
        get_explanation(db, "game-1", 1, "coach-a") == "1. e4 is a strong opening move."
    )
    # A different ply, agent, or game is a distinct cache entry.
    assert get_explanation(db, "game-1", 2, "coach-a") is None
    assert get_explanation(db, "game-1", 1, "coach-b") is None


def test_save_explanation_overwrites_existing_text(db: Db) -> None:
    upsert_games(db, [make_game()])
    save_explanation(db, "game-1", 1, "coach-a", "first draft")
    save_explanation(db, "game-1", 1, "coach-a", "revised explanation")

    assert get_explanation(db, "game-1", 1, "coach-a") == "revised explanation"


def test_explanation_survives_close_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "explanations.sqlite3"
    first = open_db(path)
    upsert_games(first, [make_game()])
    save_explanation(first, "game-1", 3, "coach-a", "castle early for safety")
    first.close()

    second = open_db(path)  # migration 003 must be a no-op here
    assert get_explanation(second, "game-1", 3, "coach-a") == "castle early for safety"
    second.close()


def test_get_report_misses_when_absent(db: Db) -> None:
    key = ReportKey(username="testuser", agent_id="coach-a", prompt_version="v1")
    assert get_report(db, key) is None


def test_report_cache_round_trips_for_the_all_time_window(db: Db) -> None:
    """since=0, until=0, time_class='' (the dataclass defaults) is the
    all-time, all-controls report — the sentinel key that must round-
    trip cleanly rather than colliding with NULL semantics.
    """
    key = ReportKey(username="testuser", agent_id="coach-a", prompt_version="v1")
    save_report(db, key, prompt="the prompt", advice="the advice", games_analyzed=42)

    cached = get_report(db, key)
    assert cached is not None
    assert cached == CachedReport(
        prompt="the prompt",
        advice="the advice",
        games_analyzed=42,
        created_at=cached.created_at,
    )
    assert cached.created_at > 0


def test_save_report_returns_the_created_at_it_persisted(db: Db) -> None:
    """The caller (the API layer) must use this return value rather than
    reading its own clock a second time — two independent reads of
    `time.time()` can straddle a second boundary and disagree, which is
    exactly the flake this return value exists to remove."""
    key = ReportKey(username="testuser", agent_id="coach-a", prompt_version="v1")

    created_at = save_report(
        db, key, prompt="the prompt", advice="the advice", games_analyzed=42
    )

    assert created_at > 0
    cached = get_report(db, key)
    assert cached is not None
    assert cached.created_at == created_at


def test_report_cache_upsert_overwrites_same_key(db: Db) -> None:
    key = ReportKey(username="testuser", agent_id="coach-a", prompt_version="v1")
    save_report(db, key, prompt="first draft", advice="first advice", games_analyzed=10)
    save_report(db, key, prompt="revised", advice="revised advice", games_analyzed=20)

    cached = get_report(db, key)
    assert cached is not None
    assert cached.prompt == "revised"
    assert cached.advice == "revised advice"
    assert cached.games_analyzed == 20


def test_report_cache_keys_include_the_window_and_agent(db: Db) -> None:
    """A different window, time class, agent, or prompt version is a
    distinct cache entry — never a collision with the all-time row."""
    all_time = ReportKey(username="testuser", agent_id="coach-a", prompt_version="v1")
    windowed = all_time.model_copy(
        update={"since": 100, "until": 200, "time_class": "blitz"}
    )
    other_agent = all_time.model_copy(update={"agent_id": "coach-b"})
    other_prompt_version = all_time.model_copy(update={"prompt_version": "v2"})

    save_report(db, all_time, prompt="all", advice="all advice", games_analyzed=5)

    assert get_report(db, all_time) is not None
    assert get_report(db, windowed) is None
    assert get_report(db, other_agent) is None
    assert get_report(db, other_prompt_version) is None

    save_report(
        db, windowed, prompt="windowed", advice="windowed advice", games_analyzed=2
    )
    cached_all_time = get_report(db, all_time)
    cached_windowed = get_report(db, windowed)
    assert cached_all_time is not None
    assert cached_windowed is not None
    assert cached_all_time.prompt == "all"
    assert cached_windowed.prompt == "windowed"


def test_report_survives_close_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "reports.sqlite3"
    key = ReportKey(username="testuser", agent_id="coach-a", prompt_version="v1")

    first = open_db(path)
    save_report(first, key, prompt="prompt", advice="advice", games_analyzed=7)
    first.close()

    second = open_db(path)  # migration 005 must be a no-op here
    cached = get_report(second, key)
    assert cached is not None
    assert cached.advice == "advice"
    second.close()


def test_concurrent_statements_serialize(db: Db) -> None:
    """Threads sharing the one connection must not corrupt each other.

    Serialized sqlite3 protects single C calls, not usage patterns:
    concurrent `with db:` transactions interleave BEGIN/COMMIT and
    abort with InterfaceError, and reads racing another thread's
    statements corrupt pysqlite's statement cache — observed as
    interpreter segfaults at connection close. This mixes writer and
    reader threads, the exact shape of an analysis run's threadpooled
    saves racing a polling client's list reads; without Db's
    statement-level lock it fails (or crashes the process outright).
    """
    upsert_games(db, [make_game(id=f"g-{n}") for n in range(50)])
    errors: list[Exception] = []

    def save_all() -> None:
        try:
            for n in range(50):
                save_analysis(db, make_analysis(game_id=f"g-{n}"), version=1)
        except Exception as exc:  # any sqlite error fails the test
            errors.append(exc)

    def read_all() -> None:
        try:
            for _ in range(50):
                list_analyzed_games(db, "testuser")
        except Exception as exc:  # any sqlite error fails the test
            errors.append(exc)

    threads = [threading.Thread(target=save_all) for _ in range(2)] + [
        threading.Thread(target=read_all) for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(list_analyzed_games(db, "testuser")) == 50


# --- perspective ids (migration 006, scan finding 1) -----------------------


def test_two_perspectives_of_one_game_coexist(db: Db) -> None:
    """A game between two tracked players stores once per side: distinct
    perspective ids, so the second player's sync can no longer collide
    with the first's row and silently drop their copy."""
    upsert_games(
        db,
        [
            make_game(
                id="uuid-9:alice", username="alice", color="white", opponent="bob"
            ),
            make_game(id="uuid-9:bob", username="bob", color="black", opponent="alice"),
        ],
    )
    save_analysis(db, make_analysis(game_id="uuid-9:alice"), version=1)

    assert [g.id for g in list_games(db, "alice", GameFilters())] == ["uuid-9:alice"]
    assert [g.id for g in list_games(db, "bob", GameFilters())] == ["uuid-9:bob"]
    # Analyses (and everything keyed on game id) hang off the
    # perspective, not the shared game.
    alice = get_game(db, "uuid-9:alice")
    bob = get_game(db, "uuid-9:bob")
    assert alice is not None and alice.analysis is not None
    assert bob is not None and bob.analysis is None


def test_upsert_keeps_the_uuid_behind_the_perspective_id(db: Db) -> None:
    upsert_games(db, [make_game(id="uuid-9:alice", username="alice")])
    (row,) = db.execute("SELECT chesscom_uuid FROM games").fetchall()
    assert row["chesscom_uuid"] == "uuid-9"


# The shapes migrations 001-005 leave behind, so migration 006's rewrite
# can run against genuinely legacy rows (uuid-keyed, no chesscom_uuid).
_LEGACY_V5_SCHEMA = """
CREATE TABLE games (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    color TEXT NOT NULL,
    pgn TEXT NOT NULL,
    san_moves TEXT NOT NULL,
    time_control TEXT NOT NULL,
    time_class TEXT NOT NULL,
    result TEXT NOT NULL,
    end_time INTEGER NOT NULL,
    opponent TEXT NOT NULL,
    player_rating INTEGER NOT NULL,
    opponent_rating INTEGER NOT NULL,
    accuracy REAL,
    opening_eco TEXT,
    opening_name TEXT,
    opening_ply INTEGER,
    termination TEXT
);
CREATE INDEX idx_games_username_end_time ON games (username, end_time DESC);
CREATE TABLE analyses (
    game_id TEXT PRIMARY KEY REFERENCES games (id) ON DELETE CASCADE,
    depth INTEGER NOT NULL,
    evals TEXT NOT NULL,
    acpl_by_phase TEXT NOT NULL,
    judgment_counts TEXT NOT NULL,
    overall_acpl REAL NOT NULL DEFAULT 0
);
CREATE TABLE explanations (
    game_id TEXT NOT NULL REFERENCES games (id) ON DELETE CASCADE,
    ply INTEGER NOT NULL,
    agent_id TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (game_id, ply, agent_id)
);
CREATE TABLE reports (
    username TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    since INTEGER NOT NULL,
    until INTEGER NOT NULL,
    time_class TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    prompt TEXT NOT NULL,
    advice TEXT NOT NULL,
    games_analyzed INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (username, agent_id, since, until, time_class, prompt_version)
);
PRAGMA user_version = 5;
"""


def test_migration_006_rewrites_legacy_rows_to_perspective_ids(
    tmp_path: Path,
) -> None:
    """A database written before perspective ids: uuid-keyed rows (and
    their analyses/explanations) must come out keyed "{uuid}:{username}"
    with the raw uuid preserved in chesscom_uuid."""
    path = tmp_path / "legacy.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.executescript(_LEGACY_V5_SCHEMA)
    legacy.execute(
        "INSERT INTO games (id, username, color, pgn, san_moves,"
        " time_control, time_class, result, end_time, opponent,"
        " player_rating, opponent_rating) VALUES ('uuid-1', 'alice',"
        " 'white', '1. e4 *', '[\"e4\"]', '600', 'rapid', 'win', 100,"
        " 'bob', 1500, 1490)"
    )
    legacy.execute(
        "INSERT INTO analyses (game_id, depth, evals, acpl_by_phase,"
        " judgment_counts) VALUES ('uuid-1', 16, '[]', '{}', '{}')"
    )
    legacy.execute(
        "INSERT INTO explanations (game_id, ply, agent_id, text,"
        " created_at) VALUES ('uuid-1', 1, 'coach-a', 'push the pawn', 1)"
    )
    legacy.commit()
    legacy.close()

    migrated = open_db(path)  # applies only migration 006
    assert [g.id for g in list_games(migrated, "alice", GameFilters())] == [
        "uuid-1:alice"
    ]
    detail = get_game(migrated, "uuid-1:alice")
    assert detail is not None
    assert detail.analysis is not None  # the analyses row followed the id
    assert get_explanation(migrated, "uuid-1:alice", 1, "coach-a") == "push the pawn"
    (row,) = migrated.execute("SELECT chesscom_uuid FROM games").fetchall()
    assert row["chesscom_uuid"] == "uuid-1"
    migrated.close()


def test_migration_007_backfills_aggregates_from_stored_evals(
    tmp_path: Path,
) -> None:
    """Analyses saved before the aggregate columns existed: migration
    007 must derive player/opening move counts and losses from the
    stored evals (player plies by color parity, opening as ply <= 20),
    so opening_stats over legacy rows matches what save_analysis would
    have computed."""
    evals = json.dumps(
        [
            # White player: plies 1 and 21 are theirs, ply 2 is not;
            # only ply 1 is opening phase.
            {
                "ply": 1,
                "san": "e4",
                "eval_cp": 0,
                "eval_mate": None,
                "best_move": "e2e4",
                "cp_loss": 10,
                "judgment": "best",
            },
            {
                "ply": 2,
                "san": "e5",
                "eval_cp": 0,
                "eval_mate": None,
                "best_move": "e7e5",
                "cp_loss": 99,
                "judgment": "best",
            },
            {
                "ply": 21,
                "san": "h4",
                "eval_cp": 0,
                "eval_mate": None,
                "best_move": "g1f3",
                "cp_loss": 30,
                "judgment": "best",
            },
        ]
    )
    path = tmp_path / "legacy-aggregates.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.executescript(_LEGACY_V5_SCHEMA)
    legacy.execute(
        "INSERT INTO games (id, username, color, pgn, san_moves,"
        " time_control, time_class, result, end_time, opponent,"
        " player_rating, opponent_rating, opening_eco, opening_name,"
        " opening_ply) VALUES ('uuid-1', 'alice', 'white', '1. e4 *',"
        " '[\"e4\", \"e5\"]', '600', 'rapid', 'win', 100, 'bob', 1500,"
        " 1490, 'C60', 'Ruy Lopez', 5)"
    )
    legacy.execute(
        "INSERT INTO analyses (game_id, depth, evals, acpl_by_phase,"
        " judgment_counts) VALUES ('uuid-1', 16, ?, '{}', '{}')",
        (evals,),
    )
    legacy.commit()
    legacy.close()

    migrated = open_db(path)  # applies migrations 006, 007, and 008
    (stat,) = opening_stats(migrated, "alice")
    assert stat.analyzed_games == 1
    assert stat.player_moves == 2  # plies 1 and 21; ply 2 is black's
    assert stat.opening_moves == 1  # only ply 1 is <= OPENING_PLIES
    assert stat.avg_cp_loss == 20.0  # (10 + 30) / 2
    assert stat.opening_acpl == 10.0
    migrated.close()


def test_migration_008_grandfathers_existing_analyses_as_version_1(
    tmp_path: Path,
) -> None:
    """Analyses saved before `analysis_version` existed must come out
    reading as version 1 -- the carried-state semantic migration 008's
    DEFAULT deliberately assigns -- so a later engine version bump can
    mark every pre-existing row stale at once, without a data migration
    re-deriving anything (docs/archive/engine-search-hangs.md,
    "Re-analysing the existing rows")."""
    path = tmp_path / "legacy-version.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.executescript(_LEGACY_V5_SCHEMA)
    legacy.execute(
        "INSERT INTO games (id, username, color, pgn, san_moves,"
        " time_control, time_class, result, end_time, opponent,"
        " player_rating, opponent_rating) VALUES ('uuid-1', 'alice',"
        " 'white', '1. e4 *', '[\"e4\"]', '600', 'rapid', 'win', 100,"
        " 'bob', 1500, 1490)"
    )
    legacy.execute(
        "INSERT INTO analyses (game_id, depth, evals, acpl_by_phase,"
        " judgment_counts) VALUES ('uuid-1', 16, '[]', '{}', '{}')"
    )
    legacy.commit()
    legacy.close()

    migrated = open_db(path)  # applies migrations 006, 007, and 008
    (row,) = migrated.execute("SELECT analysis_version FROM analyses").fetchall()
    assert row["analysis_version"] == 1
    migrated.close()


def test_game_filters_reject_negative_paging() -> None:
    """Mirrors the API edge guard: SQLite reads a negative LIMIT as
    "unlimited" (and a negative OFFSET as 0), so the storage parameter
    type refuses both outright."""
    with pytest.raises(ValidationError):
        GameFilters(limit=-1)
    with pytest.raises(ValidationError):
        GameFilters(offset=-1)
