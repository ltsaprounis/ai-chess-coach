"""Repertoire tree tests (docs/05-openings.md; contract in
docs/archive/openings-explorer.md).

Uses the same tiny fixture book as test_openings.py
(tests/testdata/minibook), extended with a few extra lines this file
needs: C21 (Center Game, a second continuation from the C20 position),
C65 (Ruy Lopez: Berlin Defense, extending the Ruy Lopez line two plies
deeper so it has an interior position with no entry of its own), and a
deeper D02 line (so the position reached by transposition still has a
book continuation to show).
"""

from pathlib import Path

import pytest

from chess_coach.domain import MATE_SCORE, MoveEval, RepertoireGame
from chess_coach.openings import (
    OpeningBook,
    RepertoireNode,
    build_repertoire,
    load_opening_book,
)

TESTDATA = Path(__file__).parent / "testdata"


@pytest.fixture(scope="module")
def book() -> OpeningBook:
    return load_opening_book(TESTDATA / "minibook")


def _child(node: RepertoireNode, san: str) -> RepertoireNode:
    return next(c for c in node.children if c.san == san)


def _descend(node: RepertoireNode, *sans: str) -> RepertoireNode:
    for san in sans:
        node = _child(node, san)
    return node


def _ev(
    ply: int,
    san: str,
    *,
    eval_cp: int | None = 0,
    eval_mate: int | None = None,
    cp_loss: int = 0,
) -> MoveEval:
    return MoveEval(
        ply=ply,
        san=san,
        eval_cp=eval_cp,
        eval_mate=eval_mate,
        best_move=san,
        cp_loss=cp_loss,
        judgment="best",
    )


def _game(
    game_id: str,
    san_moves: list[str],
    *,
    color: str = "white",
    result: str = "win",
    evals: list[MoveEval] | None = None,
) -> RepertoireGame:
    return RepertoireGame.model_validate(
        {
            "id": game_id,
            "color": color,
            "result": result,
            "san_moves": san_moves,
            "evals": evals,
        }
    )


# --- A shared structural fixture -----------------------------------------
#
# Root-level branches and their game counts, chosen to exercise ordering,
# pruning, exits, and book-move/played matching in one build:
#   c4  (5 games, 1 ply, off-book)          -> ties with e4 on count
#   e4  (5 games total: see below)          -> ties with c4 on count
#   Nf3 (2 games, transposes into D02)      -> ties with d4 on count
#   d4  (2 games, "d4 d5")                  -> ties with Nf3 on count
#
# Under e4 (5 games: 2 deep Ruy Lopez + 1 Bc4 branch + 2 that stop at
# "e4 e5"):
#   e5 (5 games)
#     Nf3 (2 games, survives min_games=2)   -> in book (C40)
#       Nc6 (2) -> Bb5 (2, C60 Ruy Lopez) -> Nf6 (2, interior, no entry)
#         -> O-O (2, C65 Berlin Defense)
#     Bc4 (1 game, pruned at min_games=2)   -> not a book continuation
#       Nf6 (1)
def _structural_games() -> list[RepertoireGame]:
    games: list[RepertoireGame] = []
    games += [_game(f"c4-{i}", ["c4"]) for i in range(5)]
    games += [
        _game(f"ruy-{i}", ["e4", "e5", "Nf3", "Nc6", "Bb5", "Nf6", "O-O"])
        for i in range(2)
    ]
    games += [_game("bc4-0", ["e4", "e5", "Bc4", "Nf6"])]
    games += [_game(f"stop-{i}", ["e4", "e5"]) for i in range(2)]
    games += [_game(f"transpose-{i}", ["Nf3", "d5", "d4"]) for i in range(2)]
    games += [_game(f"d4d5-{i}", ["d4", "d5"]) for i in range(2)]
    return games


def test_known_line_names_deepen_at_each_node(book: OpeningBook) -> None:
    root = build_repertoire(book, _structural_games(), color="white")
    e4 = _child(root, "e4")
    assert (e4.eco, e4.name, e4.in_book) == ("B00", "King's Pawn Game", True)
    e5 = _child(e4, "e5")
    assert (e5.eco, e5.name) == ("C20", "King's Pawn Game")
    nf3 = _child(e5, "Nf3")
    assert (nf3.eco, nf3.name) == ("C40", "King's Knight Opening")
    nc6 = _child(nf3, "Nc6")
    assert (nc6.eco, nc6.name) == ("C44", "King's Pawn Game")
    bb5 = _child(nc6, "Bb5")
    assert (bb5.eco, bb5.name) == ("C60", "Ruy Lopez")
    # Deepest-entry rule: an interior position with no entry of its own
    # keeps the last named entry on the path.
    nf6 = _child(bb5, "Nf6")
    assert (nf6.eco, nf6.name) == ("C60", "Ruy Lopez")
    assert nf6.in_book is True
    o_o = _child(nf6, "O-O")
    assert (o_o.eco, o_o.name) == ("C65", "Ruy Lopez: Berlin Defense")
    assert o_o.children == []


def test_in_book_true_on_interior_position_without_its_own_entry(
    book: OpeningBook,
) -> None:
    root = build_repertoire(book, _structural_games(), color="white")
    nf6 = _descend(root, "e4", "e5", "Nf3", "Nc6", "Bb5", "Nf6")
    assert (nf6.eco, nf6.name) == ("C60", "Ruy Lopez")  # no entry of its own
    assert nf6.in_book is True
    # Its own continuation (O-O -> C65) still shows, proving `in_book`
    # is a position test, not gated on this node having its own entry.
    assert [m.san for m in nf6.book_moves] == ["O-O"]
    assert nf6.book_moves[0].eco == "C65"


def test_transposition_into_book_still_named_and_shows_book_moves(
    book: OpeningBook,
) -> None:
    root = build_repertoire(book, _structural_games(), color="white")
    nf3 = _child(root, "Nf3")
    d5 = _child(nf3, "d5")
    d4 = _child(d5, "d4")
    # Reached via Nf3-d5-d4, a different path from the book's own
    # d4-d5-Nf3 line, but the same *position* -- still named, and its
    # book continuation (Nf6, unplayed here) still shows up.
    assert (d4.eco, d4.name) == (
        "D02",
        "Queen's Pawn Game: Zukertort Variation",
    )
    assert d4.in_book is True
    assert [m.san for m in d4.book_moves] == ["Nf6"]
    assert d4.book_moves[0].played is False
    assert (
        d4.book_moves[0].name == "Queen's Pawn Game: Zukertort Variation, Two Knights"
    )


def test_pruning_hides_child_but_keeps_parent_counts_and_exits(
    book: OpeningBook,
) -> None:
    root = build_repertoire(book, _structural_games(), color="white", min_games=2)
    e5 = _descend(root, "e4", "e5")
    assert e5.record.games == 5  # 2 Ruy Lopez + 1 Bc4 branch + 2 stopped
    assert [c.san for c in e5.children] == ["Nf3"]  # Bc4 (1 game) pruned
    # Pruning hides the row but does not change the parent's own counts
    # or its exits, which still reflects the pruned Bc4 branch leaving.
    assert e5.exits == 1


def test_exits_zero_at_out_of_book_node_and_ignores_games_that_end(
    book: OpeningBook,
) -> None:
    # min_games=1 so the Bc4 branch (only 1 game) survives and can be
    # inspected directly.
    root = build_repertoire(book, _structural_games(), color="white", min_games=1)
    e5 = _descend(root, "e4", "e5")
    assert e5.record.games == 5
    assert e5.exits == 1  # only the Bc4 game leaves; the 2 "stop" games
    # end at e5 and must not be counted as exits.
    bc4 = _child(e5, "Bc4")
    assert bc4.in_book is False
    # Bc4 is already out of book, so exits is 0 regardless of what
    # happens next (it continues with Nf6 in the fixture).
    assert bc4.exits == 0


def test_children_ordering_is_deterministic(book: OpeningBook) -> None:
    root = build_repertoire(book, _structural_games(), color="white")
    # c4 (5) and e4 (5) tie on games and break by SAN ("c4" < "e4");
    # Nf3 (2) and d4 (2) tie and break by SAN ("Nf3" < "d4", plain
    # string/codepoint order, not case-insensitive).
    assert [c.san for c in root.children] == ["c4", "e4", "Nf3", "d4"]
    assert [c.record.games for c in root.children] == [5, 5, 2, 2]


def test_book_moves_played_flag_matches_pre_pruning_edges(book: OpeningBook) -> None:
    root = build_repertoire(book, _structural_games(), color="white", min_games=1)
    e5 = _descend(root, "e4", "e5")
    moves = {m.san: m for m in e5.book_moves}
    assert set(moves) == {"Nf3", "d4"}
    assert moves["Nf3"].played is True  # played by the Ruy Lopez games
    assert moves["d4"].played is False  # a legal book move, never played


def test_illegal_san_ends_walk_without_crashing(book: OpeningBook) -> None:
    games = [*_structural_games(), _game("garbage-0", ["e4", "e5", "Zz9", "Qh5"])]
    root = build_repertoire(book, games, color="white", min_games=1)
    e5 = _descend(root, "e4", "e5")
    # The garbage continuation must not appear as a child (push_san on
    # it fails), and building the rest of the tree must not raise.
    assert "Zz9" not in [c.san for c in e5.children]
    assert {c.san for c in e5.children} == {"Nf3", "Bc4"}


# --- avg_eval_cp / avg_cp_loss --------------------------------------------


def test_avg_fields_none_at_root(book: OpeningBook) -> None:
    games = [
        _game(
            "g0",
            ["e4"],
            evals=[_ev(1, "e4", eval_cp=30)],
        )
    ]
    root = build_repertoire(book, games, color="white", min_games=1)
    assert root.avg_eval_cp is None
    assert root.avg_cp_loss is None


def test_avg_fields_none_when_no_analyzed_game_reaches_node(book: OpeningBook) -> None:
    games = [_game("g0", ["e4", "e5"], evals=None)]
    root = build_repertoire(book, games, color="white", min_games=1)
    e4 = _child(root, "e4")
    assert e4.record.games == 1
    assert e4.analyzed == 0
    assert e4.avg_eval_cp is None
    assert e4.avg_cp_loss is None


def test_avg_eval_cp_and_avg_cp_loss_for_white(book: OpeningBook) -> None:
    games = [
        _game(
            "g0",
            ["e4", "e5"],
            evals=[
                _ev(1, "e4", eval_cp=50, cp_loss=0),
                _ev(2, "e5", eval_cp=40, cp_loss=20),
            ],
        ),
        _game(
            "g1",
            ["e4", "e5"],
            evals=[
                _ev(1, "e4", eval_cp=150, cp_loss=10),
                _ev(2, "e5", eval_cp=60, cp_loss=5),
            ],
        ),
    ]
    root = build_repertoire(book, games, color="white", min_games=1)
    e4 = _child(root, "e4")
    assert e4.analyzed == 2
    assert e4.avg_eval_cp == pytest.approx(100.0)  # mean(50, 150), white POV
    assert e4.avg_cp_loss == pytest.approx(5.0)  # mean(0, 10)
    # avg_cp_loss at an opponent-level node (ply 2, Black's move) is
    # still present -- mover's (Black's) own cost, no sign flip.
    e5 = _child(e4, "e5")
    assert e5.avg_cp_loss == pytest.approx(12.5)  # mean(20, 5)
    assert e5.avg_eval_cp == pytest.approx(50.0)  # mean(40, 60), white POV


def test_avg_eval_cp_sign_flip_for_black(book: OpeningBook) -> None:
    games = [
        _game(
            "g0",
            ["e4", "e5"],
            color="black",
            evals=[_ev(1, "e4", eval_cp=50), _ev(2, "e5", eval_cp=-30)],
        )
    ]
    root = build_repertoire(book, games, color="black", min_games=1)
    e4 = _child(root, "e4")  # opponent's move, from Black's perspective
    assert e4.avg_eval_cp == pytest.approx(-50.0)  # flipped from white-POV 50
    e5 = _child(e4, "e5")  # Black's own move
    assert e5.avg_eval_cp == pytest.approx(30.0)  # flipped from white-POV -30


def test_avg_eval_cp_folds_mate_and_clamps(book: OpeningBook) -> None:
    games = [
        _game(
            "mate",
            ["e4"],
            evals=[_ev(1, "e4", eval_cp=None, eval_mate=3)],
        ),
        _game(
            "big",
            ["e4"],
            evals=[_ev(1, "e4", eval_cp=1500)],
        ),
        _game(
            "big-neg",
            ["e4"],
            evals=[_ev(1, "e4", eval_cp=-1500)],
        ),
    ]
    root = build_repertoire(book, games, color="white", min_games=1)
    e4 = _child(root, "e4")
    # (MATE_SCORE clamped to 1000) + 1000 + (-1000), averaged over 3.
    assert MATE_SCORE > 1000  # sanity: folding really needs the clamp
    assert e4.avg_eval_cp == pytest.approx((1000.0 + 1000.0 - 1000.0) / 3)
