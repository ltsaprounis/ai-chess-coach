# Openings explorer — hierarchical repertoire page

Status: built (2026-07-29). Each surface below has migrated into its
owning component doc (03, 05, 07, 08, plus `domain` in
docs/README.md), which are now the living contracts; this doc remains
as the design record. The "Deferred" section is still open backlog.

## What it is and why

The Dashboard's repertoire table answers "which openings do I play
and how do they score" — one flat row per (color, eco, name). It
cannot answer the questions a player actually studies with: *where*
in a line do my games go wrong, where do I leave book, which book
continuations have I never tried, and what my habitual moves cost.

The explorer is a dedicated page showing the player's games as a
per-color **move tree** — drill from 1.e4 into 2.Nf3 into any line —
with, at every node: games through it, score, average engine eval,
the cost of the move that got there, whether the position is still
in book, and the named book continuations (played and unplayed).

Everything is deterministic aggregation over stored data: no new
engine runs, no LLM calls. The only engine touch is the existing
user-triggered live-eval stream, reused from the Game page.

## Semantics (the contract)

### Nodes are move paths; names are positions

A node is a **move path** from the start position: root at ply 0,
then one node per SAN move. Two games share a node iff they began
with the same moves. Stats are path-scoped — transpositions reaching
one position by different orders are different nodes — because the
page answers "how do my games go", and merging paths would make the
breadcrumb ambiguous.

Book knowledge stays **position-keyed (EPD)**, exactly like
`classify`: a node's opening name is the deepest entry on its path,
a position is in book iff its EPD lies on any book line, and
continuations are looked up by EPD. A game that transposes *into*
book is therefore still named and still shows book moves, even
though its path is its own.

### Parity: levels alternate between the player and the opponent

The tree is per color, so whose move each level is follows from ply
parity — the same rule behind `OpeningStats.faced` (06-coach.md): as
White the odd plies are the player's, as Black the even ones.
Children at a player level are the player's repertoire choices;
children at an opponent level are what they face. As Black the first
level is the opponent's move — a Black repertoire is a set of
answers, and the tree shows it that way. The UI labels the levels;
the data model does not duplicate the information.

### Node fields

```python
class RepertoireNode(BaseModel):
    san: str                  # move that reached this node; "" root
    ply: int                  # 0 for the root
    record: Record            # every game through this node (domain)
    analyzed: int             # of those, games with a stored analysis
    eco: str | None           # deepest book entry on the path so far
    name: str | None
    in_book: bool             # EPD lies on a book line
    avg_eval_cp: float | None # after this move; player POV; analyzed
    avg_cp_loss: float | None # cost of the arriving move (mover POV)
    exits: int                # games whose next move left book here
    book_moves: list[BookMove]  # continuations from this position
    children: list[RepertoireNode]
```

Rules behind the fields:

- `record` counts every game through the node, analyzed or not.
  `analyzed` counts those with a stored analysis; the two eval
  fields aggregate over analyzed games only, and the UI states the
  coverage rather than implying it.
- `avg_eval_cp` averages the stored eval after the node's arriving
  move (`evals[ply-1]`, white-POV per 04-engine.md), sign-flipped
  for a Black player so the number always reads from the player's
  side. Each sample folds mate to `±MATE_SCORE` and is then clamped
  to ±1000 before averaging: +9 and mate-in-3 both mean "winning",
  and one unclamped mate would otherwise dominate the mean. `None`
  when no analyzed game reaches the node.
- `avg_cp_loss` is the mean stored `cp_loss` of the arriving move —
  mover's perspective by construction (04-engine.md), so at player
  levels it is the player's cost and at opponent levels the
  opponent's (which is itself useful: "they go wrong here often").
  `None` at the root and when no analyzed game reaches the node.
- `in_book` is the position test, not the entry test: interior
  positions of a long line count even when they carry no
  (eco, name) entry of their own.
- `book_moves` are the book edges out of this EPD. Each carries the
  (eco, name) of the position it reaches when that position is
  itself a named entry, else `None` — display inherits the node's
  current name. `played` marks edges that appear in the games
  (tested before pruning). Empty when the position is not in book.
- `exits` counts games through the node whose next move produced an
  out-of-book position; the side that left is implied by parity.
  Games that *end* at the node exit nowhere. Exits are counted over
  all raw edges, pruned or not, so the number survives pruning.
- `children`: pruned below `min_games`; pruning hides rows, it
  never changes the parent's counts. Sorted most games first, ties
  by SAN, so the payload is deterministic.

### Pruning and caps

`min_games` defaults to 2 (clamped 1–10): one-off deviations vanish,
repeated habits stay — the page is about patterns, and the impact
ranking below wants repetition anyway. `max_plies` defaults to 30,
the same cap as `classify` (`_MAX_BOOK_PLIES`), clamped 4–40.

## Component surfaces

### domain

`RepertoireGame` — produced by storage, consumed by openings, which
is exactly the rule for a domain type:

```python
class RepertoireGame(BaseModel):
    id: str; color: Color; result: Result
    san_moves: list[str]            # sliced to the requested cap
    evals: list[MoveEval] | None    # same slice; None if unanalyzed
```

`RepertoireNode`, `BookMove`, and `RepertoireTree` stay on the
surfaces of their single producers (openings, openings, api).

### storage

```python
def list_repertoire_games(
    db: Db, username: str, *, max_plies: int,
    since: int | None = None, until: int | None = None,
    time_class: TimeClass | None = None) -> list[RepertoireGame]
```

All stored games in scope, analyzed or not (LEFT JOIN on analyses;
`evals` sliced to the same cap). Window semantics identical to
`list_analyzed_games` (since inclusive, until exclusive). Slicing
happens inside storage and `pgn` is never selected — the documented
exception to "`san_moves` never crosses the boundary": rows are
bounded and the consumer is server-side, so the reason behind that
rule (uncapped archive fetches to the browser) is not violated.

### openings

Load-time: the TSV replay already visits every interior position of
every line; record them. `by_epd` stays as is; add the set of all
on-line EPDs and an edge map `epd -> {san: entry | None}` (the entry
of the resulting position, when it has one). ~3,500 lines yield
roughly 20–30k positions — trivial memory, no extra load cost.

New public surface:

```python
class BookMove(BaseModel):
    san: str; eco: str | None; name: str | None
    played: bool = False

class OpeningBook:
    def is_book(self, epd: str) -> bool
    def continuations(self, epd: str) -> list[BookMove]  # SAN-sorted

class RepertoireNode(BaseModel): ...   # as specified above

def build_repertoire(
    book: OpeningBook, games: list[RepertoireGame], *,
    color: Color, min_games: int = 2, max_plies: int = 30,
) -> RepertoireNode
```

`build_repertoire` filters to `color` internally and is pure — data
in, tree out; imports stay `chess_coach.domain` + python-chess, per
the component's dependency contract. An illegal SAN ends that game's
walk (the same tolerance `classify` applies).

**Required implementation shape.** Replaying every game through
python-chess per request costs seconds on a 5k-game archive; string
trie operations do not. Build in two passes:

1. Counting pass — walk each game's `san_moves` as plain strings
   into a trie, accumulating record, analyzed, and eval/loss sums
   per node. No board objects: ~150k dict operations for 5k games
   at 30 plies.
2. Annotation pass — prune to `min_games`, then DFS the surviving
   trie with one `chess.Board` (push/pop per node) computing EPDs:
   name, `in_book`, `book_moves` (matched against raw edges for
   `played`), and `exits` (tested over raw edges, pruned or not).
   Surviving nodes number in the low thousands; the chess work
   happens only there.

### api

New route, joining the table in 07-api.md:

| Method | Path                             | Behavior              |
|--------|----------------------------------|-----------------------|
| GET    | `/api/players/{u}/openings/tree` | Repertoire move tree for one color. Query: `color` (required), `since`/`until`, `time_class`, `min_games` (default 2, clamp 1–10), `max_plies` (default 30, clamp 4–40) |

```python
class RepertoireTree(BaseModel):
    username: str; color: Color
    games: int; analyzed: int      # scope totals for this color
    root: RepertoireNode
```

The route is `list_repertoire_games` → `build_repertoire`. One fetch
per color; the frontend drills client-side with no further requests.
An unknown player returns an empty tree, consistent with `/openings`
and `/report`. Clamping mirrors `/api/eval`'s style. The models are
pydantic, so `pnpm gen:api` covers the frontend types — recursive
schemas are ordinary `$ref`s in OpenAPI.

Payload: low hundreds of KB worst case at `min_games=2` on a large
archive (thousands of nodes × ~200 bytes). Acceptable for one cached
fetch; `min_games` is the lever if an archive proves bigger.

### frontend

New page `/openings`, a nav tab beside Games / Dashboard / Coach
(`Layout.tsx`):

- Color toggle (White / Black). The Dashboard's time-window and
  time-control filters scope the fetch (`since`/`until`/
  `time_class`) — same components, same defaults.
- Drill-down: a breadcrumb of the current path plus a children
  table — move, name, games, score, avg eval, avg loss, book badge.
  Unplayed `book_moves` render greyed in the same list with their
  names: the "still to learn" rows. Levels are labelled "your move"
  / "their move" from parity.
- Board panel showing the current node (the path replayed
  client-side with chess.js — the tree carries no FENs), with the
  Game page's live-eval toggle (`GET /api/eval`) for a deeper,
  user-triggered look at any position.
- A "worst lines" strip above the tree: top player-level nodes by
  impact = `record.games × avg_cp_loss`, computed client-side from
  the fetched tree — it answers "which line do I fix first".
- A coverage line ("N of M games in this window are analyzed"): the
  eval columns only speak for analyzed games. House rule — coverage
  is stated, not implied.

## Synergy

Building the tree computes the true book-exit position per game —
the exact input NEW-FEATURE-PROPOSAL.md #5 (opening phase refined by
actual book exit) needs. #5 stays its own change (it touches engine
phase attribution), but the book surface added here (`is_book`,
`continuations`) is the machinery it would reuse.

## Deferred (recorded, not built)

- **Punish metric** — when the opponent leaves book first, the eval
  swing over the following few plies ("you get +0.8 out of book and
  still score 40%" points at the middlegame, not theory). Needs a
  defined window; design when the base page exists.
- **Transposition annotation** — "also reached via …" by merging
  node stats on EPD. Path identity stays; this only adds a label.
- **Per-node drill to the Games page** — needs a by-position game
  filter or sample game ids on nodes; both have costs.
- **LLM "explain this line"** — user-triggered + cached, reusing
  the explain stack, per house policy on LLM spend.

## Build order

1. Main session: land the contract — `RepertoireGame` in `domain`
   plus the 03/05/07/08 doc updates, each in the same commit as its
   code change.
2. openings-dev and storage-dev in parallel (disjoint directories).
3. api-dev: route, wiring, clamps, `TestClient` tests.
4. `pnpm gen:api`, then frontend-dev.
5. boundary-reviewer over the whole diff before committing.
