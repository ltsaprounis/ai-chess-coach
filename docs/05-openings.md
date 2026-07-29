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
    def is_book(self, epd: str) -> bool          # EPD on any book line
    def continuations(self, epd: str) -> list[BookMove]  # SAN-sorted

class BookMove(BaseModel):
    san: str; eco: str | None; name: str | None
    played: bool = False
```

## Logic

1. At load time, replay each TSV line's `pgn` on a `chess.Board` and
   map the final position's EPD (`board.epd()` — FEN minus clocks) to
   `(eco, name)`. ~3,500 lines; loads in well under a second, kept in
   memory as a plain dict (`by_epd`).
2. The same replay already visits every interior position of every
   line, so it also builds two more structures, kept alongside
   `by_epd`: the set of every on-line EPD (`is_book`'s backing store)
   and an edge map `epd -> {san: (eco, name) | None}` recording, for
   each position, the moves that continue a book line from it and the
   named entry each reaches (`None` for an interior position with no
   entry of its own). `board.san(move)` (computed before `push`) gives
   the edge's SAN. ~3,500 lines yield roughly 20-30k positions —
   trivial memory, no extra load cost. `continuations` looks up this
   map and returns `[]` off-book.
3. `classify` replays the game's moves (capped at 30 plies), computes
   each position's EPD, and returns the deepest position present in
   the map, with `ply` = the 1-based ply of the deepest matched book
   move — the move that fixed the name, *not* the first out-of-book
   ply. The distinction matters: `OpeningStats.faced` hangs on this
   ply's parity (docs/06-coach.md), and an off-by-one would flip it.

## Repertoire tree

Built for the [openings explorer](future-improvements/openings-explorer.md)
(design record there; this is the shipped contract). A per-color move
tree over a player's games: root at ply 0, one node per SAN move.
Nodes are move paths — two games share a node iff they played the
same moves so far, no transposition merging — while book knowledge
stays position-keyed (EPD), exactly like `classify`: a node's name is
the deepest book entry on its path, `in_book` is a position test
(interior positions count, not just named entries), and `book_moves`
are looked up by the node's own EPD. A game that transposes into book
is therefore still named even though its path is its own.

```python
class RepertoireNode(BaseModel):
    san: str                  # move that reached this node; "" root
    ply: int                  # 0 for the root
    record: Record             # every game through this node
    analyzed: int              # of those, games with a stored analysis
    eco: str | None            # deepest book entry on the path so far
    name: str | None
    in_book: bool              # EPD lies on a book line
    avg_eval_cp: float | None  # after this move; player POV; analyzed
    avg_cp_loss: float | None  # cost of the arriving move (mover POV)
    exits: int                 # games whose next move left book here
    book_moves: list[BookMove]  # continuations from this position
    children: list[RepertoireNode]

def build_repertoire(
    book: OpeningBook, games: list[RepertoireGame], *,
    color: Color, min_games: int = 2, max_plies: int = 30,
) -> RepertoireNode
```

Field rules:

- `record` counts every game through the node, analyzed or not;
  `analyzed` counts those with a stored analysis (`evals is not
  None`); `avg_eval_cp`/`avg_cp_loss` aggregate over analyzed games
  only.
- `avg_eval_cp` averages `evals[ply-1]` (white-POV, docs/04-engine.md)
  sign-flipped for a Black player. Per sample: mate folds to
  `±MATE_SCORE` (sign of `eval_mate`) when `eval_cp` is None, then
  clamps to ±1000 before averaging (clamp and sign-flip commute — the
  bound is symmetric). `None` at the root (ply 0 has no arriving move)
  and when no analyzed game reaches the node; guarded against `evals`
  shorter than the ply.
- `avg_cp_loss` is the mean stored `cp_loss` of the arriving move,
  mover's POV by construction — no sign flip, present at opponent
  levels too. Same `None` rules as `avg_eval_cp`.
- `exits` counts games through the node whose next move produced an
  out-of-book position, over *all* raw edges, pruned or not; 0 when
  the node's own position is not in book (a game can only leave book
  from within it); games that end at the node exit nowhere.
- `book_moves` are book edges out of this EPD, SAN-sorted, each
  carrying the (eco, name) of the position it reaches when that
  position is itself a named entry, `played=True` iff the SAN appears
  among the node's raw (pre-pruning) children in the games. `[]` when
  the position is not in book.
- `children` are pruned below `min_games` — pruning hides rows, never
  changes the parent's counts or `exits` — sorted most games first,
  ties by SAN.
- An illegal/unparseable SAN ends that game's walk (the same
  tolerance `classify` applies). Walk at most `max_plies` plies per
  game. At an in-book node, a child SAN that fails to parse counts as
  an exit — an unfollowable move is not a book continuation. Real
  stored games never hit this (their SANs come from parsed PGNs); the
  rule only pins the behavior down.

`build_repertoire` filters `games` to `color` internally and is pure:
data in, tree out. Imports stay `chess_coach.domain` + python-chess.

**Implementation shape (performance contract).** Two passes:

1. Counting — walk each game's `san_moves` as plain strings into a
   trie, accumulating `record`/`analyzed`/eval and loss sums and raw
   (pre-pruning) child edges per node. No `chess.Board`.
2. Annotation — prune to `min_games`, then DFS the surviving trie with
   *one* `chess.Board` (push/pop per node) computing the EPD-derived
   fields: `eco`/`name`, `in_book`, `book_moves` (`played` matched
   against the raw edges), and `exits` (a one-ply push/pop probe per
   raw child edge, surviving or not, from the current node's already-
   pushed board — no deeper descent into a pruned subtree). The chess
   work is bounded by the number of surviving nodes and their
   immediate raw fan-out, not by replaying every game in full.

## Dependencies

- `chess_coach.domain` (`Opening`, `Color`, `Record`, `RepertoireGame`,
  `MATE_SCORE`) and python-chess. Nothing else.
- The submodule directory path is injected by the
  [API layer](07-api.md), which calls `classify` after
  [ingestion](02-ingestion.md) and persists the result via
  [storage](03-storage.md). `build_repertoire` is called by the API
  layer with games from [storage's](03-storage.md)
  `list_repertoire_games`.

## Build plan

1. TSV parser (stdlib `csv`; the format is trivial).
2. Book loader building the EPD dict.
3. `classify` with the deepest-match rule.
4. Tests: known lines (Ruy Lopez), a transposition case, and a game
   that leaves book immediately (1. h4).
5. `is_book`/`continuations` plus the on-line-EPD set and edge map
   the loader builds alongside `by_epd`.
6. `build_repertoire`'s two-pass tree builder (counting trie, then
   single-board annotation DFS) and `RepertoireNode`/`BookMove`.
7. Tests: deepest-entry naming, transposition-into-book naming and
   book moves, pruning vs. `exits`, eval/loss averaging (sign flip,
   mate folding, clamping), deterministic ordering, and a malformed
   SAN that ends a walk without crashing.
