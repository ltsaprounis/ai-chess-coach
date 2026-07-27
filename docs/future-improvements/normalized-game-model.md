# Normalized game model — considered, deferred

Status: deferred (2026-07). The shipped fix for scan finding 1 is the
cheaper perspective-id scheme; this doc records the deeper remodel we
evaluated ("Option B"), what it would take, and when it would be worth
revisiting.

## The problem it solves

A `games` row was never really a game — it is *one player's view* of a
game (`username`, `color`, `player_rating`, `result`, `termination`,
`accuracy` are all per-side), yet its identity was the chess.com uuid,
which names the game. When two tracked players played each other, the
second player's sync hit the `ON CONFLICT (id)` upsert and their
perspective was silently dropped
([CODEBASE-SCAN-2026-07.md](../CODEBASE-SCAN-2026-07.md), finding 1).

Two shapes fix the identity mismatch:

- **Perspective ids (shipped).** The stored id becomes
  `{uuid}:{username}` — one row per (game, perspective). One migration,
  one line in ingestion, no HTTP or frontend changes; the raw uuid is
  kept in `games.chesscom_uuid`.
- **Normalization (this doc).** One neutral row per game, with the
  perspective derived per requesting player.

## The design, done properly

The sane version keeps today's perspective-shaped types (`Game`,
`GameSummary`, `AnalyzedGame`) at every component boundary and pushes
neutrality down into storage:

- `games` becomes neutral: one row per uuid with both sides
  denormalized (`white_username`, `white_rating`, `white_result_code`,
  `white_accuracy`, `black_…`) plus the shared fields (pgn, san_moves,
  time control/class, end_time, opening).
- A new `players` table records *tracked* usernames. Not optional:
  today "tracked" is encoded implicitly by `games.username`; a neutral
  table loses it, and `list_players` would otherwise return every
  opponent ever faced.
- `analyses` becomes neutral — `(game_uuid, depth, evals)` — since the
  eval list already covers both sides. Each game is analyzed once.
- Storage's read functions keep their signatures but match `username`
  against either side and *project* each neutral row into today's
  perspective types. Coach never notices; the API mostly doesn't.

## Why we deferred it — the cost inventory

Roughly a week of careful work, ~80% of it in storage, ingestion, and
their tests (the two largest test files in the suite), with five traps
that make it bigger than it first looks:

1. **The stored analysis aggregates have to move.**
   `GameAnalysis.overall_acpl` / `acpl_by_phase` / `judgment_counts`
   are player-POV, so a neutral analyses row cannot hold them.
   Deriving them at read time needs a full board replay (the endgame
   phase rule depends on material), and storage's dependency contract
   is domain + stdlib only — no python-chess. The derivation would
   have to become a new public engine function called by the API when
   projecting `GameDetail`. (Verified: the only reader of the stored
   aggregates is the Game page's ACPL strip; the coach report already
   recomputes everything from raw evals.)
2. **Result classification moves out of ingestion.** win/draw/loss is
   a property of a perspective, so the `_WIN`/`_DRAW`/`_LOSS` code
   tables migrate to projection time — a contract relocation, plus a
   decision about what "unknown result code → skip" means when only
   one side's code is unknown.
3. **The HTTP surface breaks.** `GET /games/{uuid}` and its `/explain`
   are ambiguous once one row serves two perspectives; both need a
   username (param or path move) → OpenAPI regen and edits to every
   frontend link builder. The explanations cache key gains a username
   column. This is the one axis where normalization is strictly worse
   than perspective ids, which keep every URL opaque and unchanged.
4. **The data migration must synthesize what was never stored.**
   Merging perspective rows into neutral rows needs the *other* side's
   raw result code, which is only sometimes derivable (a stored loss
   implies the opponent's "win"; a stored win says nothing about how
   the opponent lost; `timevsinsufficient` draws are asymmetric).
   Migrated rows carry NULLs until a full re-sync heals them — the
   same recovery step the cheap fix needs, on top of a much larger
   transform.
5. **The test bill is the real cost.** Nearly all of `test_storage`
   exercises exactly the queries being rewritten; `test_ingestion`'s
   fixtures all assert perspective output. This is where the calendar
   time goes and where regressions in well-documented semantics
   (repertoire agreement, window semantics) would creep in.

## What it buys, and why that rounds to zero today

- **One engine analysis per shared game.** Real, but proportional to
  games between tracked players — currently ~none. The shipped scheme
  can capture this later with ~20 lines: `chesscom_uuid` allows
  copying evals from any existing analysis of the same game before
  analyzing (per-perspective aggregate columns stay per-row).
- **One stored copy of each PGN.** ~2 KB per shared game.
- **The "right" model for hypothetical features** — head-to-head
  views, opponent scouting, a cross-player browser. None are on the
  roadmap; GUIDELINES.md's no-speculative-abstraction rule applies.
- **Fixes the explanation-cache perspective leak** — which perspective
  ids fix identically, for free.

## When to revisit

Normalization earns its cost the day a feature actually needs
game-neutral queries — head-to-head records, opponent analysis, or
heavy analysis of games between tracked players. The shipped scheme
deliberately preserves the migration path: `chesscom_uuid` groups
perspective rows by true game, and the perspective id itself parses
back into (uuid, username) with `rsplit(":", 1)` — chess.com usernames
cannot contain `:`. Migrating from perspective ids then is no harder
than migrating from the original schema was.
