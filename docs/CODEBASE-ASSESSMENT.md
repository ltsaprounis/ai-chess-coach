# Codebase Assessment — AI Chess Coach

_Review date: 2026-07-24 · Reviewer: Claude (full-repo read, no
sub-agents) · Scope: all 132 tracked files (backend, web, docs, CI)._

> **Status (2026-07-27, `assessment-fixes` branch):** every finding
> is resolved or deliberately closed. 1, 3, 6, 7, 8, 10 fixed on
> that branch; 2 and 5 had already been fixed by the coach-report
> rework; 4 was deleted in the repo-root cleanup; 9 remains the
> documented accepted trade-off. Details below are the original
> 2026-07-24 snapshot, kept as-is.

## Executive summary

This is an unusually well-built codebase. The architecture the docs
promise is the architecture the code delivers: six decoupled
components that never import each other, a single composition root
(`chess_coach.api`), a shared `domain` contract, and dependency
injection with no module-level singletons. The boundaries are not
aspirational — they are mechanically enforced by import-linter, and
both contracts pass. Typing is strict end to end (pyright strict +
`tsc --strict` with `noUncheckedIndexedAccess`), errors are typed per
component and mapped to HTTP at the edge, and the async/streaming code
(engine pool, SSE, LLM provider seam) handles cancellation and client
disconnects with real care.

I ran every CI gate locally; all are green:

| Surface  | Gates run                                            | Result |
|----------|------------------------------------------------------|--------|
| Backend  | ruff · ruff format · pyright (strict) · import-linter · pytest | **pass** — 142 passed, 5 engine tests deselected |
| Frontend | Biome · tsc (strict) · Vitest · build                | **pass** — 64 passed, tsc clean |

Nothing here is on fire. The findings below are **polish, robustness,
and doc-drift**, not defects that break the product. The single most
valuable theme is **documentation drift**: a few places where
`GUIDELINES.md` / component docs describe things the code does not yet
do — which matters precisely because this project's "definition of
done" says docs and code move together.

**Overall grade: A−.** Coherent design, disciplined execution, strong
tests. The gap between this and an A is a handful of small robustness
gaps and keeping the docs honest.

## What is genuinely good (keep doing this)

- **Boundary enforcement is real, not a convention.** The
  `independence` + `layers` import-linter contracts encode the rules
  and fail CI on violation. Verified: 2 kept, 0 broken.
- **The LLM provider seam** (`coach/providers.py`) is the cleanest
  part of the design: `CoachProvider` Protocol, two concrete SDK
  providers, and the engine injected as a `PositionAnalystFn` so coach
  and engine never import each other. The API layer is where they meet
  (`routes.py:375`), exactly as the docs claim.
- **Streaming discipline.** Both ends treat SSE as adversarial:
  `aclosing()` on every generator so a client disconnect stops engine
  work and caches nothing; `useExplain` deliberately uses `fetch` over
  `EventSource` so pre-stream JSON errors (404/409/400/503) surface
  instead of vanishing into a bodyless `error` event.
- **Comments state constraints, not narration** ("mate maps to ±10000
  cp for loss arithmetic", the sqlite `threadsafety==3` guard, the
  SPA path-traversal check). This is the style the guidelines ask for.
- **Tests favor fixtures over mocks**, target public `__init__`
  surfaces, and keep the real engine behind an opt-in `-m engine`
  marker so CI needs no Stockfish, network, or live LLM.

## Ranked findings

Ordered by value-to-fix (impact × how cheap the fix is). "Owner" is
the sub-agent whose component owns the change.

| # | Sev | Finding | Owner |
|---|-----|---------|-------|
| 1 | Med | Shutdown cancels analysis tasks but never awaits them, then closes the DB and engine pool immediately after — unclean shutdown, and a stated async rule is not met | api-dev |
| 2 | Med | Doc drift: docs/08 says the Games page has **opening + analyzed-state** filters; only result/time-class exist | frontend-dev |
| 3 | Med | Doc drift: GUIDELINES claims Node is pinned via `.nvmrc` + `engines`; neither exists | frontend-dev |
| 4 | Low–Med | `web/.oxlintrc.json` is unwired (no script/CI/hook runs it) and contradicts the "Biome, one tool" rule | frontend-dev |
| 5 | Low–Med | `POST /analyze` `limit` is unvalidated; a negative value becomes SQLite `LIMIT -1` = unlimited, bypassing `analyze_limit` | api-dev |
| 6 | Low | `runs` dict is never pruned; finished runs linger for the app's lifetime | api-dev |
| 7 | Low | Board replay assumes the standard start position (`chess.Board()`); a custom-FEN "chess" game fails its own analysis silently | ingestion-dev |
| 8 | Low | `list_analyses` is effectively dead code (no production call site; exported + one test) | storage-dev |
| 9 | Low | Vite dev proxy hardcodes `localhost:8000` while `server.port` is configurable | frontend-dev |
| 10 | Nit | `domain.LlmProvider` advertises unimplemented `anthropic`/`azure-foundry`; config lets `azure-foundry` validate, then startup fails | config-dev / coach-dev |

## Findings in detail

### 1 — Analysis tasks are cancelled but not awaited on shutdown (Med)

`api/app.py:55-60` (lifespan teardown):

```python
for run in runs.values():
    if run.task is not None:
        run.task.cancel()
if app.state.pool is not None:
    await app.state.pool.close()   # quits engines
app.state.db.close()
```

The tasks are cancelled but never awaited, so teardown proceeds while
cancellation is still in flight. `GUIDELINES.md` is explicit: "every
`asyncio.Task` is awaited or tracked and cancelled on shutdown."
There's no torn-write path (`save_analysis` is synchronous and sits
between awaits), but `pool.close()` can call `engine.quit()` on an
engine a still-unwinding task is mid-`analyse` on. Low real-world blast
radius (shutdown only), but it's the one place production code diverges
from a rule the project holds itself to.

**Fix:** collect the tasks and
`await asyncio.gather(*tasks, return_exceptions=True)` after cancelling
and before closing the pool/DB.

### 2 — Games page is missing filters the doc promises (Med)

docs/08-frontend.md:20 describes the Games table "with opening,
result, time-class, and analyzed-state filters." `web/src/pages/Games.tsx`
implements only `result` and `time_class` selects (plus the analyze
limit). The backend and `GameFilters` already support `opening_eco`
and `analyzed`, so this is a UI gap, not an API one. Either add the two
controls or trim the doc sentence.

### 3 — Node version is not actually pinned (Med)

GUIDELINES.md:33 lists Node as "pinned: `.nvmrc` + engines," but there
is no `.nvmrc` anywhere and `web/package.json` has no `engines` field.
CI pins Node 22 in the workflow, but a local contributor gets whatever
Node they happen to have. Add `web/.nvmrc` (`22`) and an
`"engines": { "node": ">=22" }` block, or soften the doc.

### 4 — Unwired oxlint config contradicts "one tool" (Low–Med)

`web/.oxlintrc.json` exists but nothing references oxlint —
`pnpm lint` is `biome check`, and neither CI nor pre-commit invokes it.
GUIDELINES.md:33 and docs/08 both state Biome is the single lint/format
tool. Either wire oxlint in deliberately and document why (Biome's
`react/rules-of-hooks` coverage differs), or delete the file. As-is
it's a silent second linter that never runs.

### 5 — `analyze` limit is unvalidated (Low–Med)

`api/routes.py:151` — `AnalyzeRequest.limit: int | None`. The bulk path
computes `min(body.limit, cfg.engine.analyze_limit)` (routes.py:192)
and passes it straight to `games_needing_analysis`, which binds it to
SQL `LIMIT ?`. A negative `limit` survives the `min()` and SQLite reads
`LIMIT -1` as *unlimited*, so `{"limit": -1}` analyzes every unanalyzed
game and defeats the `analyze_limit` cap. The frontend clamps to `>=1`,
so this is only reachable by a direct API call. **Fix:** annotate
`limit: int | None = Field(default=None, ge=1)`.

### 6 — `runs` registry grows unbounded (Low)

`api/app.py:49` / `routes.py:199` keep one `AnalysisRun` per username
for the process lifetime; finished runs are never evicted. Bounded by
distinct usernames (a new run for the same user replaces the old
entry), so for the intended single-user local app this is negligible —
worth a note, not urgent. A sweep of finished runs older than N, or
eviction when a fresh run replaces a finished one, would close it.

### 7 — Standard-start-position assumption is undocumented (Low)

`engine/analysis.py:52`, `coach/context.py:47`, and
`coach/report.py:85` all rebuild the game from `chess.Board()` and
replay SAN. Ingestion keeps only `rules == "chess"` games
(normalize.py:36) — but a "chess" game can still carry a `SetUp`/`FEN`
header (custom-position daily challenges). `_san_moves` honors that
header, but the analysis/replay code always starts from the standard
position, so such a game would mis-parse and fail its own analysis
(caught per-game by the `gather(return_exceptions=True)`, so no crash —
just a silently missing analysis). Either drop games with a `SetUp`
header at ingestion, or document "standard start only" as an explicit
invariant in docs/02 and docs/04.

### 8 — `list_analyses` is dead (Low)

`storage/analyses.py:35` is exported from `storage/__init__.py` and
exercised once in `test_storage.py`, but has no production caller
(`list_analyzed_games` is what the report path uses). GUIDELINES.md:87
asks for three call sites before a helper exists. Remove it, or leave a
note that it's a kept public surface.

### 9 — Dev proxy hardcodes the API port (Low)

`web/vite.config.ts:8` proxies `/api` to `http://localhost:8000`, but
`server.port` is configurable and `make dev-api` reads it from config.
Change the port and the dev proxy silently breaks. The example config
already flags a related 8000 coupling with `.claude/launch.json`, so
this is a known, accepted trade-off — lowest priority, listed for
completeness.

### 10 — Domain advertises providers that don't exist (Nit)

`domain.py:18` lists `anthropic` and `azure-foundry` in `LlmProvider`,
but `create_provider` (providers.py:445) raises for both. Config only
enforces `ANTHROPIC_API_KEY` for `anthropic` (settings.py:114), so an
agent configured with `azure-foundry` passes config validation and
then fails at app startup. That's fail-fast and acceptable, but the
type surface over-promises. Consider narrowing the runtime-selectable
set, or a config-time check that the provider is actually implemented.

## Anti-over-engineering note

Several things that might look like gaps are correct choices for this
project and should **not** be "fixed": the single shared SQLite
connection (guarded by the `threadsafety==3` check, WAL, and SQLite's
own locking), plain-sync storage vs. async elsewhere, hand-written SSE
payload types (the one documented exemption from generated types), and
custom SVG charts instead of a chart library. Leave them.

## Suggested sequencing

1. **api-dev** — #1 (await tasks on shutdown), #5 (`ge=1` on limit),
   #6 (prune runs). Small, same file neighborhood, real robustness.
2. **frontend-dev** — #2, #3, #4, #9 are all doc/tooling honesty in
   `web/`; batch them.
3. **ingestion-dev** — #7 (drop `SetUp`-header games or document the
   invariant).
4. **storage-dev** — #8 (remove or annotate `list_analyses`).
5. **config-dev / coach-dev** — #10, only if you want the type surface
   to match reality.

Run **boundary-reviewer** on the resulting diff before committing —
several of these touch component public surfaces and docs, which is
exactly what it checks for.
