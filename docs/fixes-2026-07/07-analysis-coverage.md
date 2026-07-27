# 07 — Analysis coverage: state it, then let the user fix it

**Status: wave 5 ships the report/prompt slice (with doc 04's
follow-up); the analyze-endpoint and Coach-page slices follow.**

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

No committed backfill script — a script would be a second, unowned
path into the system (direct DB reads plus API orchestration outside
the components), redundant the day the slices below land. One-off
backfills run as throwaways against the live API; the durable path
is the endpoint filter plus the UI action.

Three slices:

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
   everything else (since inclusive, until exclusive). api-dev.
3. **The Coach page warns before generating** when coverage is
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
  slice 3.
- The routes always pass `games_in_scope` — a filter-less request
  still states "N of M" over the full history; `requested_since`/
  `requested_until` are passed only when the request carried them
  (the `None` render path exists for build_report's other callers,
  not for these routes).
- Tests: the coach prompt sent to the provider states coverage when
  a scoped request finds fewer analyzed games than stored ones; a
  filter-less request still passes the full-history count.

## Acceptance

Wave 5: both gates green; snapshot diff shows only the student
section coverage lines, the turning-point FEN lines, and the
Verification reword; report cache behavior unchanged (the version
bump invalidates old entries by design). Slices 2–3: a partial
window shows the warning, the action analyzes exactly the window's
remainder, and the warning disappears at full coverage.
