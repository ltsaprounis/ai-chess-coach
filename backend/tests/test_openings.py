"""Openings component tests (docs/05-openings.md)."""

from pathlib import Path

import chess
import pytest

from chess_coach.openings import OpeningBook, load_opening_book

TESTDATA = Path(__file__).parent / "testdata"
REAL_BOOK = Path(__file__).resolve().parents[2] / "vendor" / "chess-openings"


def _epd(*san_moves: str) -> str:
    board = chess.Board()
    for san in san_moves:
        board.push_san(san)
    return board.epd()


@pytest.fixture(scope="module")
def book() -> OpeningBook:
    return load_opening_book(TESTDATA / "minibook")


def test_known_line_deepest_match_wins(book: OpeningBook) -> None:
    opening = book.classify(["e4", "e5", "Nf3", "Nc6", "Bb5"])
    assert opening is not None
    assert (opening.eco, opening.name, opening.ply) == ("C60", "Ruy Lopez", 5)


def test_moves_after_book_exit_keep_deepest_match(book: OpeningBook) -> None:
    opening = book.classify(["e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4"])
    assert opening is not None
    assert (opening.eco, opening.ply) == ("C60", 5)


def test_transposition_is_found_by_position(book: OpeningBook) -> None:
    # 1. Nf3 d5 2. d4 transposes into the 1. d4 d5 2. Nf3 book line;
    # the intermediate positions are not in the mini book.
    opening = book.classify(["Nf3", "d5", "d4"])
    assert opening is not None
    assert (opening.eco, opening.ply) == ("D02", 3)


def test_out_of_book_immediately_returns_none(book: OpeningBook) -> None:
    assert book.classify(["h4", "h5"]) is None


def test_empty_and_illegal_move_lists(book: OpeningBook) -> None:
    assert book.classify([]) is None
    # An illegal continuation stops the walk but keeps what matched.
    opening = book.classify(["e4", "e5", "Qxh7"])
    assert opening is not None
    assert opening.eco == "C20"


def test_missing_book_dir_raises() -> None:
    with pytest.raises(FileNotFoundError, match="submodule"):
        load_opening_book(TESTDATA / "no-such-book")


@pytest.mark.skipif(
    not (REAL_BOOK / "a.tsv").exists(), reason="submodule not checked out"
)
def test_real_lichess_book_loads_and_classifies() -> None:
    book = load_opening_book(REAL_BOOK)
    assert len(book) > 3000
    opening = book.classify(["e4", "e5", "Nf3", "Nc6", "Bb5", "a6"])
    assert opening is not None
    assert opening.eco == "C70"  # Ruy Lopez: Morphy Defense


# --- is_book / continuations (docs/05-openings.md, repertoire tree) -----


def test_is_book_true_for_every_interior_position_on_a_line(book: OpeningBook) -> None:
    # Every position from the start to C60 (Ruy Lopez) is on-line, not
    # just the ones that carry their own (eco, name) entry.
    assert book.is_book(_epd())  # the start position itself
    assert book.is_book(_epd("e4"))
    assert book.is_book(_epd("e4", "e5"))
    assert book.is_book(_epd("e4", "e5", "Nf3"))
    assert book.is_book(_epd("e4", "e5", "Nf3", "Nc6"))
    assert book.is_book(_epd("e4", "e5", "Nf3", "Nc6", "Bb5"))


def test_is_book_false_off_line(book: OpeningBook) -> None:
    assert not book.is_book(_epd("h4"))
    assert not book.is_book(_epd("e4", "e5", "Bc4"))


def test_continuations_sorted_by_san_and_named_when_reached_position_is_an_entry(
    book: OpeningBook,
) -> None:
    moves = book.continuations(_epd("e4", "e5"))
    # C20 has two recorded continuations in the mini book: 2. Nf3 (C40)
    # and 2. d4 (C21). "Nf3" < "d4" in plain string order (SAN-sorted,
    # not alphabetical-ignoring-case).
    assert [m.san for m in moves] == ["Nf3", "d4"]
    nf3 = moves[0]
    assert (nf3.eco, nf3.name, nf3.played) == ("C40", "King's Knight Opening", False)
    d4 = moves[1]
    assert (d4.eco, d4.name, d4.played) == ("C21", "Center Game", False)


def test_continuations_none_entry_for_unnamed_interior_position(
    book: OpeningBook,
) -> None:
    # 5. ...Nf6 (after Bb5) is on-book (it leads into C65, Berlin
    # Defense) but is not itself a named entry, so its own continuation
    # from Bb5 carries no (eco, name).
    moves = book.continuations(_epd("e4", "e5", "Nf3", "Nc6", "Bb5"))
    assert [m.san for m in moves] == ["Nf6"]
    assert (moves[0].eco, moves[0].name) == (None, None)


def test_continuations_empty_when_position_not_in_book(book: OpeningBook) -> None:
    assert book.continuations(_epd("h4")) == []
