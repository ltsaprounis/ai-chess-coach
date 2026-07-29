# 02 — Full re-sync so `Game.termination` backfills

**Status: shipped 2026-07-25 in `f90f97e` (wave 1). The backfill
itself ran on 2026-07-25: all 8,149 stored games verified non-NULL.**

## Symptom

`Game.termination` (migration 004) is NULL on every game stored
before the column existed, so the "How games end" feature reads
`unknown` across the whole real archive.

The rework's handover assumed one normal sync would backfill it.
That is wrong: `sync_player` (`api/routes.py`) passes
`since = latest_game_time(db, user)` and the ingestion service
skips months that ended before `since` and drops any game with
`end_time <= since` (`ingestion/service.py`). A normal sync never
re-fetches a stored game, so the backfill never happens.

## Decision

Add a full re-sync escape hatch instead of changing normal sync.
The pieces already exist:

- `sync_games(user, since=None)` fetches every archive month.
- `upsert_games` is idempotent and its ON CONFLICT clause already
  updates `pgn`, `san_moves`, `accuracy` and `termination` on
  existing rows.

So a full re-sync is one query parameter and one button — no
ingestion or storage change at all.

## Slices

### api-dev

- `POST /players/{username}/sync` gains `full: bool = False`. When
  true, pass `since=None` to `sync_games` instead of
  `latest_game_time`. Docstring: full re-fetches the entire archive
  to backfill columns added since the games were stored (currently
  `termination`); the upsert makes it safe to repeat.
- `games_synced` will equal the whole archive on a full run; that is
  accurate (they were all upserted), so leave `SyncResult` alone.
- Test: a full sync passes `since=None` through; a normal sync still
  passes the latest stored time. Update `docs/07-api.md` (the sync
  endpoint's parameter list) and add one line to `docs/02-ingestion.md`
  noting the backfill role of `since=None`.

### frontend-dev (after api-dev; sequence after doc 01's slice —
both touch `Games.tsx`)

- `web/src/api.ts`: `sync` takes an optional `{ full?: boolean }`
  and forwards it as the query param.
- `web/src/pages/Games.tsx`: beside the existing Sync button, a
  low-emphasis "Full re-sync" action with a title/hint: re-fetches
  the whole archive from chess.com to backfill how games ended for
  games stored before that was recorded. Same invalidations as
  Sync. Expect it to be slow on a large archive — disable both
  buttons while pending, as Sync does today.
- Update `docs/08-frontend.md`'s Games-page description.

Optional, only if cheap: when the Dashboard's termination rows are
dominated by `unknown`, render a one-line hint linking to the Games
page ("run a full re-sync to backfill how games ended"). Skip if it
drags in new plumbing.

Acceptance: both gates green; on the real database, one full
re-sync leaves zero NULL terminations (spot-check with
`sqlite3 backend/data/coach.sqlite3 "SELECT COUNT(*) FROM games
WHERE termination IS NULL"`).

## Out of scope

Rate limiting or resumable full syncs. The chess.com archive API is
month-granular and the loop is sequential; ~8k games is a handful
of month fetches per year of play, which is acceptable for a
user-triggered action.
