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

```ts
// Parse the TSVs once at startup into a position -> opening map.
function loadOpeningBook(dir: string): OpeningBook;

// Deepest book match wins. Returns null for book-less games.
OpeningBook.classify(sanMoves: string[]): Opening | null;
```

## Logic

1. At load time, replay each TSV line's `pgn` with `chess.js` and map
   the final position's EPD (FEN minus clocks) to `{eco, name}`.
   ~3,500 lines; loads in well under a second, kept in memory.
2. `classify` replays the game's moves (capped at 30 plies), computes
   each position's EPD, and returns the deepest position present in
   the map, with `ply` = where the game left book.

## Dependencies

- `shared/types.ts` (`Opening`) and `chess.js`. Nothing else.
- The submodule directory path is injected by the
  [server](07-server.md), which calls `classify` after
  [ingestion](02-ingestion.md) and persists the result via
  [storage](03-storage.md).

## Build plan

1. TSV parser (no dependency; the format is trivial).
2. Book loader building the EPD map.
3. `classify` with the deepest-match rule.
4. Tests: known lines (Ruy Lopez), a transposition case, and a game
   that leaves book immediately (1. h4).
