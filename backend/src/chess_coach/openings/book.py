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
from pydantic import BaseModel

from chess_coach.domain import Opening

logger = logging.getLogger(__name__)

_MAX_BOOK_PLIES = 30
_TSV_FILES = ("a.tsv", "b.tsv", "c.tsv", "d.tsv", "e.tsv")

_Entry = tuple[str, str]  # (eco, name)


class BookMove(BaseModel):
    """One book continuation out of a position (docs/05-openings.md).

    `eco`/`name` describe the position this move reaches, when that
    position is itself a named entry — `None` for an interior book
    position, in which case display inherits the caller's current
    name. `played` is filled in by the repertoire builder; a bare
    `OpeningBook.continuations()` call always returns `played=False`,
    since the book alone has no notion of which games were played.
    """

    san: str
    eco: str | None
    name: str | None
    played: bool = False


class OpeningBook:
    def __init__(
        self,
        by_epd: dict[str, _Entry],
        on_book: set[str],
        edges: dict[str, dict[str, _Entry | None]],
    ) -> None:
        self._by_epd = by_epd
        self._on_book = on_book
        self._edges = edges

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

    def is_book(self, epd: str) -> bool:
        """Whether this exact position lies on any book line."""
        return epd in self._on_book

    def continuations(self, epd: str) -> list[BookMove]:
        """Book edges out of this position, SAN-sorted. `[]` off-book."""
        edges = self._edges.get(epd)
        if not edges:
            return []
        return [
            BookMove(
                san=san,
                eco=entry[0] if entry is not None else None,
                name=entry[1] if entry is not None else None,
            )
            for san, entry in sorted(edges.items())
        ]

    def _entry_at(self, epd: str) -> _Entry | None:
        """Named entry exactly at this EPD, or None.

        Package-private: the repertoire builder (`repertoire.py`) needs
        a position -> entry test, alongside `is_book`/`continuations`,
        to derive the deepest name on a path one node at a time with a
        single `chess.Board` — the same per-position rule `classify`
        applies, without `classify`'s per-call board replay.
        """
        return self._by_epd.get(epd)


def load_opening_book(book_dir: Path) -> OpeningBook:
    """Parse the lichess chess-openings TSVs into an OpeningBook."""
    by_epd: dict[str, _Entry] = {}
    on_book: set[str] = set()
    raw_edges: dict[str, dict[str, str]] = {}
    for tsv_name in _TSV_FILES:
        path = book_dir / tsv_name
        if not path.exists():
            raise FileNotFoundError(
                f"opening book file missing: {path} "
                "(run `git submodule update --init vendor/chess-openings`?)"
            )
        with path.open(newline="") as handle:
            for row in csv.DictReader(handle, delimiter="\t"):
                final_epd = _replay_line(row["pgn"], on_book, raw_edges)
                if final_epd is None:
                    logger.warning("unparseable book line in %s: %r", tsv_name, row)
                    continue
                by_epd[final_epd] = (row["eco"], row["name"])
    edges = {
        epd: {san: by_epd.get(next_epd) for san, next_epd in sans.items()}
        for epd, sans in raw_edges.items()
    }
    return OpeningBook(by_epd, on_book, edges)


def _replay_line(
    pgn: str, on_book: set[str], raw_edges: dict[str, dict[str, str]]
) -> str | None:
    """Replay one TSV line, recording every interior EPD and edge.

    Returns the final position's EPD, or None if the PGN is
    unparseable. Mutates `on_book` and `raw_edges` in place: every line
    the loader reads contributes its interior positions and edges,
    regardless of whether its own final position later turns out to
    also be some other line's prefix.
    """
    game = chess.pgn.read_game(io.StringIO(pgn))
    if game is None or game.errors:
        return None
    board = game.board()
    on_book.add(board.epd())
    for move in game.mainline_moves():
        prev_epd = board.epd()
        san = board.san(move)
        board.push(move)
        next_epd = board.epd()
        on_book.add(next_epd)
        raw_edges.setdefault(prev_epd, {})[san] = next_epd
    return board.epd()
