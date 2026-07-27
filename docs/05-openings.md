# Component 5 — Openings (ECO classification)

Classifies the opening of a game by looking up the positions it passes
through in the lichess ECO database. Position-based lookup handles
transpositions correctly, unlike move-sequence matching.

## Data source

`lichess-org/chess-openings` as a git submodule at
`vendor/chess-openings`: five TSV files (`a.tsv` … `e.tsv`) with
columns `eco`, `name`, `pgn`. It is the de-facto standard ECO
database (used by lichess itself).

## Interface

```python
# Parse the TSVs once at startup into a position -> opening map.
def load_opening_book(book_dir: Path) -> OpeningBook

class OpeningBook:
    # Deepest book match wins. None for book-less games.
    def classify(self, san_moves: list[str]) -> Opening | None
```

## Logic

1. At load time, replay each TSV line's `pgn` on a `chess.Board` and
   map the final position's EPD (`board.epd()` — FEN minus clocks) to
   `(eco, name)`. ~3,500 lines; loads in well under a second, kept in
   memory as a plain dict.
2. `classify` replays the game's moves (capped at 30 plies), computes
   each position's EPD, and returns the deepest position present in
   the map, with `ply` = the 1-based ply of the deepest matched book
   move — the move that fixed the name, *not* the first out-of-book
   ply. The distinction matters: `OpeningStats.faced` hangs on this
   ply's parity (docs/06-coach.md), and an off-by-one would flip it.

## Dependencies

- `chess_coach.domain` (`Opening`) and python-chess. Nothing else.
- The submodule directory path is injected by the
  [API layer](07-api.md), which calls `classify` after
  [ingestion](02-ingestion.md) and persists the result via
  [storage](03-storage.md).

## Build plan

1. TSV parser (stdlib `csv`; the format is trivial).
2. Book loader building the EPD dict.
3. `classify` with the deepest-match rule.
4. Tests: known lines (Ruy Lopez), a transposition case, and a game
   that leaves book immediately (1. h4).
