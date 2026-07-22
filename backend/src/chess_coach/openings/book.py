"""Opening book: position-based ECO classification (docs/05-openings.md).

Positions are keyed by EPD (FEN minus move counters), so games that
transpose into a book line are classified correctly regardless of
move order. Deepest book match wins.
"""

import csv
import io
import logging
from pathlib import Path

import chess
import chess.pgn

from chess_coach.domain import Opening

logger = logging.getLogger(__name__)

_MAX_BOOK_PLIES = 30
_TSV_FILES = ("a.tsv", "b.tsv", "c.tsv", "d.tsv", "e.tsv")


class OpeningBook:
    def __init__(self, by_epd: dict[str, tuple[str, str]]) -> None:
        self._by_epd = by_epd

    def __len__(self) -> int:
        return len(self._by_epd)

    def classify(self, san_moves: list[str]) -> Opening | None:
        """Deepest book position the game passes through, or None."""
        board = chess.Board()
        best: Opening | None = None
        for ply, san in enumerate(san_moves[:_MAX_BOOK_PLIES], start=1):
            try:
                board.push_san(san)
            except ValueError:
                break  # malformed/illegal continuation: keep what we have
            entry = self._by_epd.get(board.epd())
            if entry is not None:
                best = Opening(eco=entry[0], name=entry[1], ply=ply)
        return best


def load_opening_book(book_dir: Path) -> OpeningBook:
    """Parse the lichess chess-openings TSVs into an OpeningBook."""
    by_epd: dict[str, tuple[str, str]] = {}
    for tsv_name in _TSV_FILES:
        path = book_dir / tsv_name
        if not path.exists():
            raise FileNotFoundError(
                f"opening book file missing: {path} "
                "(run `git submodule update --init vendor/chess-openings`?)"
            )
        with path.open(newline="") as handle:
            for row in csv.DictReader(handle, delimiter="\t"):
                epd = _final_epd(row["pgn"])
                if epd is None:
                    logger.warning("unparseable book line in %s: %r", tsv_name, row)
                    continue
                by_epd[epd] = (row["eco"], row["name"])
    return OpeningBook(by_epd)


def _final_epd(pgn: str) -> str | None:
    game = chess.pgn.read_game(io.StringIO(pgn))
    if game is None or game.errors:
        return None
    board = game.board()
    for move in game.mainline_moves():
        board.push(move)
    return board.epd()
