# 01 — Slim the games list row and remove the 2000-game cap

## Symptom

`web/src/api.ts` `allGames()` stops at 2,000 games. The Dashboard's
tiles, charts, window filter and default time control are computed
client-side from that slice, while `/report` and `/openings`
aggregate the full history server-side — so a repertoire row can
show more games than the page's own total, and the default time
control is wrong: the capped slice makes rapid look most-played
(792) when the player's real main control is blitz (4,324 vs 1,897
lifetime, verified against `backend/data/coach.sqlite3`, 8,148
games).

## Decision: remove the cap; slim the row to pay for it

A per-time-class cap was considered and rejected: blitz alone
exceeds 2,000 games in the real archive, so the main class would
still be truncated and the Dashboard would still disagree with the
server aggregates — the exact bug being fixed — while multiplying
the fetch loops. Removal is the only option that makes the client
and server agree, and it is cheaper than the status quo once the
row is slimmed:

- Today (capped, fat rows): ~5.9 MB. Every row ships `pgn`
  (~2.3 KB) and full `san_moves`, which no list view renders.
- Uncapped, slim rows: ~2.4 MB for 8,148 games (~0.3 KB/row).

One wrinkle the original sizing analysis predates: the repertoire
drill-through (`web/src/pages/Games.tsx`) now derives the player's
system from `san_moves` via `playerSystem()`. That function reads
at most the first 6 plies, so the slim row carries exactly those.

## Contract change (main session, before delegating)

`GameSummary` (`backend/src/chess_coach/domain.py`) stops extending
`Game` and becomes a standalone list-row model:

```python
class GameSummary(BaseModel):
    """One row of the games list — everything the list views render,
    nothing they don't. `first_plies` is the first 6 SAN plies, the
    exact prefix `playerSystem` needs for the repertoire
    drill-through; it is not the game record."""

    id: str
    color: Color
    time_class: TimeClass
    result: Result
    end_time: int
    opponent: str
    player_rating: int
    opponent_rating: int
    accuracy: float | None = None
    termination: str | None = None
    first_plies: list[str]  # first 6 SAN plies of the game
    opening: Opening | None = None
    analyzed: bool = False
```

Dropped relative to today: `username`, `pgn`, `san_moves`,
`time_control` — none is read by any list consumer (`pgn` is unused
in all of `web/src`; full `san_moves` is used only by the Game
detail page, which fetches `GameDetail`).

Docs to update in the same commit: `docs/03-storage.md` (list row
shape), `docs/07-api.md` (games endpoint payload), `docs/README.md`
if it names `GameSummary`'s shape.

## Slices

### storage-dev

- `list_games` (`storage/games.py`) builds the new model: stop
  selecting `pgn`; slice the stored `san_moves` JSON to its first
  6 entries in Python for `first_plies`.
- Update `tests/test_storage.py` fixtures/assertions to the new
  shape.

Acceptance: `make check` green; `list_games` rows carry no pgn and
at most 6 plies.

### api-dev

- `player_games` (`api/routes.py`) is unchanged in signature; verify
  the OpenAPI schema reflects the new `GameSummary` and update
  `tests/test_api.py` fixtures (`tests/factories.py` likely builds
  `GameSummary` from `Game` fields — adjust).
- Regenerate the frontend schema: `cd web && pnpm gen:api` (or
  `make gen-api`), so frontend-dev starts from real types.

### frontend-dev (after api-dev's `gen:api`)

- `web/src/api.ts`: delete `ALL_GAMES_CAP`; keep the page loop that
  stops on a short page. Raise `ALL_GAMES_PAGE` to 1000. Add a
  pathological-loop guard at 50,000 games (log a console warning if
  hit) so the loop is bounded without reintroducing a cap anyone
  will meet.
- `web/src/pages/Games.tsx`: drill-through switches to
  `playerSystem(game.first_plies, game.color)`.
- Fix type fallout from the regenerated schema (fields dropped from
  `GameSummary`); `web/src/api.test.ts` has cap-behavior tests to
  update, and `stats.test.ts` fixtures may need reshaping.
- Verify in the browser against the real database: the Dashboard's
  default time control must become blitz, and the game count on the
  Games page must match the archive (8,148, not 2,000).

Acceptance: `pnpm biome check && pnpm typecheck && pnpm test &&
pnpm build` green; drill-through from a repertoire row still
narrows to the clicked (color, system).

## Out of scope

Server-side aggregate endpoints to replace the client-side tiles.
The bulk fetch at ~2.4 MB is fine for a localhost single-user app;
revisit only if archives grow an order of magnitude.
