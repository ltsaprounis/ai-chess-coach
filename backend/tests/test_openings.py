"""Openings component tests (docs/05-openings.md)."""

from pathlib import Path

import pytest

from chess_coach.openings import OpeningBook, load_opening_book

TESTDATA = Path(__file__).parent / "testdata"
REAL_BOOK = Path(__file__).resolve().parents[2] / "vendor" / "chess-openings"


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
