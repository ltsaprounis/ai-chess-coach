# Codebase scan — 2026-07-27

Scope: full read of `backend/src/chess_coach/` (all components), the
migrations, `scripts/backfill.py`, and every non-test file in `web/src/`,
plus mechanical checks (import-linter, grep for env reads and
cross-component imports) and a run of the quality gates.

Status: findings 2, 3, 4 and 8 are fixed on the `scan-fixes` branch
this report landed with (one commit per fix); the rest are open.

## Summary

The architecture holds. Both import-linter contracts pass, no component
imports another, only config touches the environment, and both
`OpeningStats` producers implement the documented rules with a shared
agreement test. All gates are green: 241 backend tests, 118 frontend
tests, ruff, pyright config intact.

Findings, most severe first:

1. **Bug** — a game between two tracked players is stored for only the
   first player synced; the second player's copy is silently dropped.
2. **Bug** — the Game page's "Analyze this game" button swallows errors
   and can stick at "Analyzing…" forever, polling every second.
3. **Bug** — a `Retry-After` header in HTTP-date form crashes ingestion;
   the delay is also unbounded.
4. **Robustness** — a crashed Stockfish process is recycled into the
   engine pool and poisons its slot until restart.
5. **Robustness** — `/eval` SSE has no mid-stream error event (unlike
   `/explain`).
6. **Architecture** — `sync_player` does DB writes and opening
   classification on the event loop; large re-syncs stall every
   concurrent request and SSE stream.
7. **Consistency** — the no-analyst Claude provider path doesn't lock
   down built-in tools the way every other provider path does.
8. Plus seven smaller opportunities (perf of `opening_stats`, missing
   `ge=0` guards on `/games` paging, explanation cache lacking a prompt
   version, analyze-by-id ownership, packaging-fragile default paths,
   an explain stream that can end without `done`, no timeout on the
   Copilot idle wait).

---

## Bugs

### 1. Games between two tracked players are dropped for the second player

`games.id` is the chess.com `uuid` and the table's sole primary key
(`001_initial.sql`), but a row is a *perspective* — it carries
`username`, `color`, `player_rating`, `result` for one side. When two
tracked players played each other, both archives contain the same
`uuid`. The first sync inserts the game under player A; player B's sync
hits `ON CONFLICT (id)` in `upsert_games`
(`backend/src/chess_coach/storage/games.py:46`), which only refreshes
`pgn`/`san_moves`/`accuracy`/`termination` — so B's perspective is
never stored, B's games list/dashboard/report silently miss those
games, and `games_synced` still counts them as synced.

`analyses` has the same shape problem one level down: `game_id` is its
primary key, but `GameAnalysis` aggregates *the player's* moves
(`overall_acpl`, `judgment_counts`), so even if games were stored per
perspective, one analysis row could not serve both sides.

Fix direction: key `games` by `(id, username)` and `analyses` by the
same pair (migration + FK change), or explicitly document single-user
scope. The docs (02/03) currently don't acknowledge the case.

### 2. "Analyze this game" swallows errors and sticks forever

`web/src/pages/Game.tsx:237` fires `void api.analyze(...)` with no
error handling after setting `analyzing = true`. If the POST fails —
409 (run already active), 503 (no engine binary), network — the
rejection is unhandled, the button stays at "Analyzing…", and the
`refetchInterval: 1000` poll runs indefinitely. Games.tsx and Coach.tsx
both use mutations with error states; this call site predates that
pattern. Fix: same `useMutation` treatment, reset `analyzing` on error
(and arguably treat 409 as "attach", as Coach.tsx does).

### 3. `Retry-After` parsing can crash a sync; delay unbounded

`backend/src/chess_coach/ingestion/client.py:38` does
`float(response.headers.get("Retry-After", 2**attempt))`. RFC 9110
allows HTTP-date values, which raise `ValueError` and escape as an
unhandled 500 out of `/sync`. The parsed delay is also used uncapped —
a large value parks the request (and the sync) for that long. Fix:
fall back to exponential backoff on parse failure and clamp the sleep.

## Robustness

### 4. Crashed engines are recycled into the pool

Both checkout paths in `backend/src/chess_coach/engine/pool.py` return
the worker unconditionally (`finally: self._idle.put_nowait(engine)`,
lines 70 and 123). If Stockfish dies mid-analysis
(`EngineTerminatedError` is an `EngineError`), the dead process goes
back into rotation and every future checkout of that slot fails until
the server restarts. With `workers: 2`, one crash halves throughput;
two kill analysis entirely while `/analyze` keeps accepting runs. Fix:
on `EngineError`, close and respawn the worker (or drop it and log)
instead of requeueing it.

### 5. `/eval` has no mid-stream error event

`explain_move` catches `CoachProviderError` mid-stream and emits an
`error` SSE event (`routes.py:477`); `eval_position`'s `stream()`
(`routes.py:375`) has no equivalent for `EngineError`, so a mid-search
failure tears the connection down bare. The frontend's `useLiveEval`
happens to cope (it treats the drop as done/error), but the contract
asymmetry means the client can't distinguish "engine failed" from
"stream finished oddly". Low cost to mirror the explain pattern.

### 6. `useExplain` can stick at "Explaining…" on truncated streams

`web/src/useExplain.ts:118` — if the response body ends without a
`done`/`error` event (server restart mid-explanation), the reader
returns and the hook exits without dispatching, leaving the panel in
`streaming` state with a disabled button. Dispatch an error on
unexpected EOF.

### 7. No timeout on the Copilot idle wait

`CopilotSdkProvider.complete` awaits `idle.wait()`
(`backend/src/chess_coach/coach/providers.py:351`) with no timeout; a
session that never goes idle or errors hangs the `/coach` request
forever. The explain path's queue drain has the same property. A
generous `asyncio.timeout` around the wait would convert a wedged SDK
into a `CoachProviderError`.

## Architecture / consistency

### 8. `sync_player` blocks the event loop

`routes.py:105-129` is `async def` but calls sync sqlite
(`upsert_games`, `set_opening`) and the CPU-bound `book.classify` loop
inline. After a full re-sync of a few thousand games, classification
alone replays up to 30 plies per game on the loop — during which every
other request and all SSE progress/eval streams stall. The codebase
already has the right pattern in `coach_player`
(`run_in_threadpool(_load_and_build)`); the classification pass and
batch upserts belong there too. (Smaller instances of the same thing:
`get_game`/`get_explanation`/`save_explanation` in `explain_move`, and
`save_analysis` inside `_run_analysis`.)

### 9. No-analyst Claude completions don't lock down built-in tools

`ClaudeAgentSdkProvider.complete` with `analyst=None`
(`providers.py:107-111`) sets only `model`/`max_turns=1`/
`system_prompt` — unlike the analyst branch and `explain()`, which
both pass `tools=[]` + `allowed_tools` to keep the run away from
Claude Code's built-in tools. This path runs whenever the engine pool
is missing (degraded coach mode). `max_turns=1` bounds it, but a
completion that decides to call a tool burns its only turn and returns
the empty-text error. Passing the same `tools=[]` lockdown makes the
degraded path predictable and consistent.

### 10. `/games` paging lacks the negative-limit guard

`AnalyzeRequest.limit` documents why it needs `ge=0` (SQLite reads a
negative LIMIT as unlimited — `routes.py:186`), but
`GameFilters.limit`/`offset` (`storage/games.py:28`) and the
`player_games` query params have no bounds, so `limit=-1` returns the
entire table in one response. Same one-line `Field(ge=...)` fix the
analyze body already has.

## Opportunities

### 11. `opening_stats` re-parses every eval blob per request

`storage/games.py:449-461` JSON-parses the full `evals` list of every
analyzed game in scope on each `/openings` call, which the Dashboard
issues on every filter change. At thousands of analyzed games this is
tens of MB of JSON per request. The two ACPL columns only need four
per-game numbers (opening/total loss and move counts) — persisting
those as columns at `save_analysis` time would make the endpoint pure
SQL. (The docs call the Python finish deliberate, but the per-request
cost grows with the archive.)

### 12. Explanation cache ignores `PROMPT_VERSION`

The report cache keys on `coach.PROMPT_VERSION` so a prompt rework
invalidates stale advice; `explanations` keys on
`(game_id, ply, agent_id)` only (`storage/explanations.py`), so a
reworked explain prompt keeps serving old cached explanations until
the user hits Regenerate. Either include a version in the key or
document the asymmetry as intended.

### 13. `analyze` by game id skips ownership and dedupe

`routes.py:232-240`: the `game_ids` path resolves ids with no check
that the game belongs to `{username}`, and doesn't dedupe — a
duplicated id is analyzed twice, and ids belonging to another player
run under the wrong player's registry entry (and count against the
wrong `remaining`). Harmless in single-user use; two lines to tighten.

### 14. Default engine/book paths assume a source checkout

`app.py:21-24` derives `_REPO_ROOT` via `Path(__file__).parents[4]`,
which points into `site-packages`' ancestry if the package is ever
installed as a wheel. Config overrides exist, so this is only a
packaging landmine — worth a comment or a `importlib.resources`-style
guard if distribution ever happens (noting the GPL concern in
pyproject already gates that).

## What was checked and found clean

- Import boundaries: both import-linter contracts kept; grep confirms
  no cross-component imports and no `os.environ` reads outside config.
- The repertoire dual-producer rules (storage SQL vs coach Python):
  both implement the documented majority/parity/rollup rules;
  `test_repertoire_agreement.py` pins them together.
- Phase-rule sharing via domain constants (engine vs coach) is
  mirrored exactly, with a test asserting it.
- Move-weighted ACPL discipline (never mean-of-means) is respected in
  all three consumers, including the frontend family rollup.
- SSE lifecycle handling (aclosing on generators, client-disconnect
  cleanup, task cancellation on shutdown) follows the guidelines.
- SPA fallback path-traversal is guarded (`is_relative_to` after
  `resolve()`).
- Gates: 241 backend tests pass, 118 frontend tests pass, ruff and
  import-linter clean.
