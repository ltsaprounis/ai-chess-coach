# 07 — Analysis coverage: state it, then let the user fix it

**Status: wave 5 (`c2f9f2c`) shipped the report/prompt slice with
doc 04's follow-up. Wave 6 in progress: analyze-endpoint filters +
the backfill CLI (slices 2-3). The Coach-page warning (slice 4)
remains.**

## Symptom

The first live report run (2026-07-27): the user selected "last 6
months, rapid" — 1,010 stored games — and the Coach page itself said
"Covers 1025 games · rapid". The generated report covered 450 games
spanning 2026-05-27 to 2026-07-23, because analysis is enqueued
newest-first and had only ever reached the recent end of the archive
(January–April: 527 rapid games, zero analyzed; May: 57 of 90).

Every layer behaved as documented — unanalyzed games are excluded
from the report (07-api.md), the window bounds derive from the games
themselves (06-coach.md) — and the composition still misled:

- The UI stated the in-window total, then generated over less than
  half of it, warning nowhere before the LLM spend.
- The prompt presented the analyzed span as "Window:", so the model
  could not know 560 games were missing and its honesty rule could
  not fire. May's trend row silently described the last third of May.
- Nothing lets the user aim analysis at a window: `POST /analyze`
  takes only `limit` (newest-first) or explicit `game_ids`.

## Decision: state coverage everywhere, then make backfill aimable

Backfill has two consumers of one endpoint (decision revised
2026-07-27; the first cut said "no committed script"). The Coach
page's button covers small gaps — the run continues server-side if
the tab closes. But archive-scale jobs (4,300 unanalyzed blitz
games) need what no browser owns: chaining batches past
`engine.analyze_limit` for hours, and keeping the machine awake. So
a committed CLI exists under conditions that keep it from becoming
a second path into the system: it is an **HTTP client only** — no
sqlite reads, no `chess_coach` imports, every decision delegated to
the filtered analyze endpoint below — living in
`backend/scripts/backfill.py` with a `make backfill` target that
wraps `caffeinate` on macOS. The original objection was to a
DB-poking orchestrator; this is a loop over the public API.

Four slices:

1. **Report states its coverage** (wave 5, with doc 04's follow-up).
   `PlayerReport` gains `requested_since` / `requested_until` /
   `games_in_scope` (all `None` = no scope info, renders as before);
   storage gains `count_games` with `list_analyzed_games`'s exact
   window semantics; `/report` and `/coach` pass both through. The
   prompt's student section states the requested window alongside
   the covered span and renders "N of M games in scope", with an
   explicit caveat when N < M. Contracts: 03-storage.md,
   06-coach.md ("Coverage is stated, not implied"), 07-api.md.
2. **`POST /analyze` gains `since`/`until`/`time_class`** so
   "analyze this window" is expressible. Extends the body model;
   `limit` and `game_ids` behave as today; same window semantics as
   everything else (since inclusive, until exclusive). The filters
   scope both the enqueue and `remaining`. A request that resolves
   to zero games starts **no run** and still answers 202 — which
   makes `limit=0` a free probe ("how much is left in this scope?")
   and `queued=0, remaining=0` the backfill's termination signal.
   Storage's `games_needing_analysis` / `count_games_needing_analysis`
   gain the same kwargs. storage-dev, then api-dev.
3. **The backfill CLI** (`backend/scripts/backfill.py`, main
   session — it is a client, owned by no component): stdlib-only,
   POSTs slice 2's endpoint in a loop — 202 starts a batch, 409
   means one is still running (poll again), `queued=0, remaining=0`
   means done. `--dry-run` uses the `limit=0` probe. `make backfill`
   wraps it in `caffeinate -dims` on Darwin.
4. **The Coach page warns before generating** when coverage is
   partial ("450 of 1,025 games in this window are analyzed") with
   an "Analyze the rest" action driving slice 2, progress via the
   existing SSE stream. The warning reads the `PlayerReport` fields
   from slice 1 — no client-side recount. frontend-dev.

## Slices (wave 5)

### storage-dev

- `count_games(db, username, *, since=None, until=None,
  time_class=None) -> int` in `storage/games.py`: every stored game
  matching the filters, analyzed or not. The WHERE clause must
  mirror `list_analyzed_games` exactly (since inclusive, until
  exclusive) so numerator and denominator describe the same scope.
- Tests: window edges (inclusive/exclusive), time-class filter,
  and a mixed analyzed/unanalyzed fixture asserting the count
  exceeds `len(list_analyzed_games(...))` over the same filters.

### coach-dev

- `build_report` gains the three keyword args and copies them onto
  the report (domain fields already exist).
- Prompt: student section renders requested window, covered span,
  "N of M" coverage and the partial-coverage caveat; turning points
  gain the FEN line; Verification rule reworded (doc 04 follow-up).
  One `PROMPT_VERSION` bump: `"2026-07-fen-coverage"`. Snapshot
  regenerated and the diff read: only these changes may appear.

### api-dev (after the above)

- `player_report` and `coach_player` compute
  `count_games(db, user, since=..., until=..., time_class=...)` and
  pass it plus the requested bounds to `build_report`.
- `PlayerReport`'s new optional fields change the OpenAPI schema:
  regenerate `web/` types (`pnpm gen:api`); no UI consumption until
  slice 4.
- The routes always pass `games_in_scope` — a filter-less request
  still states "N of M" over the full history; `requested_since`/
  `requested_until` are passed only when the request carried them
  (the `None` render path exists for build_report's other callers,
  not for these routes).
- Tests: the coach prompt sent to the provider states coverage when
  a scoped request finds fewer analyzed games than stored ones; a
  filter-less request still passes the full-history count.

## Slices (wave 6)

### storage-dev

- `games_needing_analysis` and `count_games_needing_analysis` gain
  `*, since=None, until=None, time_class=None` with the same window
  semantics as everything else (since inclusive, until exclusive);
  the newest-first order and depth semantics are unchanged.
- Tests: window edges, time-class filter, and that the two
  functions agree on a mixed fixture (len of one = the other).

### api-dev (after storage-dev)

- `AnalyzeRequest` gains `since`/`until`/`time_class`; the bulk
  path passes them to both storage calls so `queued` and
  `remaining` describe the same scope. `game_ids` ignores them.
- Zero resolved games → no run started, still 202 (the `limit=0`
  probe and the termination signal).
- Tests: scoped enqueue picks only in-scope games; `remaining`
  is in-scope; `limit=0` starts no run and reports `remaining`;
  zero-game result leaves no 409-blocking run behind.

### main session — backfill CLI (after api-dev)

- `backend/scripts/backfill.py` + `make backfill`, per slice 3
  above. Stdlib-only; every count comes from the API.

## Acceptance

Wave 5: both gates green; snapshot diff shows only the student
section coverage lines, the turning-point FEN lines, and the
Verification reword; report cache behavior unchanged (the version
bump invalidates old entries by design).

Wave 6: both gates green; a scoped analyze request enqueues only
in-scope games and reports in-scope `remaining`; `--dry-run`
touches nothing; the CLI drains a multi-batch backfill to
`queued=0, remaining=0` unattended.

Slice 4 (later): a partial window shows the warning, the action
analyzes exactly the window's remainder, and the warning disappears
at full coverage.
