---
title: AI Chess Coach Codebase Assessment
description: >-
  Architecture, code quality, reliability, security, UI, UX, testing, and
  operational assessment
author: GitHub Copilot
ms.date: 2026-07-30
ms.topic: overview
keywords:
  - architecture
  - code quality
  - security
  - ui
  - ux
  - testing
estimated_reading_time: 22
---

## Status (checked 2026-08-03)

Open. This review is not archived and should not be read as history:
its P0 and most of its P1 are still unaddressed in the current tree.
The findings below are left exactly as written on 2026-07-30 so they
stay usable as evidence; what has changed since is recorded only
here.

Closed since the review:

* **F12, recurring-mistake links.** The Dashboard's example links now
  carry `?ply={example_ply}` and land on the move
  (`Dashboard.tsx`, commit `536dcc0`).
* **F12, the header player switcher.** This was documentation drift,
  not a code regression: the header never had a switcher, and
  [08-frontend.md](08-frontend.md) claimed one. The doc now describes
  what `Layout.tsx` renders. The product gap the finding names is
  unchanged, and stays on the roadmap as P2.1: switching players
  still means a detour through Settings.

Partly addressed:

* **F8**, failed single-game analysis. The Game page's analyze call
  is now a mutation that starts polling only when the server confirms
  `queued > 0`, so a rejected enqueue (409 run active, 503 no engine)
  surfaces as an error instead of polling forever. The finding's own
  case is untouched: a run that fails *after* enqueue still leaves
  the page polling once a second, because the page does not consume
  the SSE stream.
* **F9**, silent truncation. `allGames`'s 50,000-row bound is now
  documented and warned about as a pathological-loop guard rather
  than a cap, which is the honest reading of it. It still returns the
  accumulated rows as though they were complete, which is what the
  finding asks to change.

Everything else is unchanged: F1 through F7, F10, F11, the F12
icon-only board controls, and the CI and testing recommendations. F1
in particular still stands exactly as described: `_apply_migrations`
runs `executescript`, then sets `user_version`, then commits, with no
enclosing transaction.

The Validation Snapshot below describes revision `60f245f` and is not
re-measured here; test counts in particular have moved on.

## Executive Summary

AI Chess Coach has a strong engineering foundation. Its component boundaries
are explicit and mechanically enforced, its domain semantics are unusually
careful, and its automated test surface is broad for a project of this size.
The codebase does not need an architectural rewrite.

The main risks sit at operational boundaries rather than in the core design:
database migration recovery, derived-data provenance, partial startup cleanup,
external-process error translation, and long-running UI state recovery. The
highest-priority issue is migration atomicity because a failed migration can
leave the real SQLite database partly changed while its version remains old.

The desktop interface is coherent, information-rich, and task-oriented. It
preserves context well through exact game links, persistent filters, explicit
LLM actions, and useful empty and stale states. The narrow-screen experience
is functional but not purposefully adapted. Navigation consumes a large part
of the first viewport, analytical tables require horizontal scrolling, and
the dashboard remains one very long document.

The application is appropriately designed as a local, single-user tool. It is
not hardened for untrusted network exposure: there is no authentication,
authorization, request throttling, or global limit on expensive LLM requests.
That is a deployment constraint, not a vulnerability in the documented
loopback-only workflow.

The overall assessment is:

* Architecture: strong
* Code quality and maintainability: strong
* Domain correctness: strong
* Reliability and recovery: good, with focused high-value gaps
* Data integrity: one high-priority migration issue
* Desktop UI and UX: good
* Mobile and narrow-screen UX: needs improvement
* Accessibility: good foundation, incomplete verification
* Security: appropriate for local use, not ready for public exposure
* Performance: good at current personal-archive scale, with clear ceilings
* Testing and CI: strong, with browser, failure-path, and drift gaps
* Documentation: excellent depth, with several current contract mismatches

## Review Metadata

Review date: 2026-07-30.

Reviewed revision: `60f245f7b138f2dd9f93bfe7a02d19193413c9c2`.

Author: GitHub Copilot.

> [!NOTE]
> I am GitHub Copilot. No lower-level model identifier was exposed to this
> review session, so this assessment does not claim one.

The review covered 194 tracked entries, approximately 6,750 lines of
first-party Python source and tests, 5,420 lines of frontend TypeScript and
CSS, and 3,090 lines of current documentation. Vendored Stockfish and lichess
opening-book internals were excluded except at their integration boundaries.
Files under [archive](archive/README.md) were treated as historical evidence,
not as current requirements.

The review combined:

* Current architecture and component contracts in
  [README.md](README.md), [GUIDELINES.md](GUIDELINES.md), and component
  documents 01 through 08
* First-party backend, frontend, scripts, migrations, tests, manifests, and CI
* Static diagnostics and the repository's declared quality gates
* Focused call-path analysis for persistence, subprocesses, SSE, caching,
  external I/O, and LLM tooling
* A visual and accessibility-tree inspection using a SQLite backup in a
  temporary directory
* Desktop and narrow-screen checks against 8,156 copied game records

No source, configuration, dependency, or environment file was changed during
the review. The real SQLite database was not opened by the preview server. The
assessment file is the only repository change.

## Validation Snapshot

The following checks passed on the reviewed revision:

* `uv run ruff check --no-cache .`
* `uv run ruff format --check .`
* `uv run pyright`: 0 errors and 0 warnings
* `uv run lint-imports`: 2 contracts kept, 0 broken
* Backend tests: 343 passed and 5 engine tests deselected
* Opt-in Stockfish tests: 5 passed
* `pnpm lint`: 65 files checked with no fixes required
* `pnpm typecheck`
* Frontend tests: 178 passed across 14 files
* VS Code diagnostics for backend source, backend tests, and frontend source
  reported no errors

Backend tests emitted one dependency deprecation warning: FastAPI's current
`TestClient` path reports that Starlette's `httpx` integration is deprecated
in favor of `httpx2`. It does not affect current test results, but it should be
handled before the dependency transition becomes mandatory.

The visual review observed:

* No document-level horizontal overflow at a 1,152 by 720 desktop viewport
* A readable 480-pixel game board and coherent two-column game workspace
* A 6,706-pixel desktop dashboard for the default 998-game rapid window
* A 7,855-pixel dashboard at the integrated browser's 312 by 675 narrow
  viewport
* A 178-pixel-tall wrapped sticky header at that narrow viewport
* Horizontal overflow, contained by scroll wrappers, in all eight rendered
  dashboard tables at that narrow viewport
* Accessible names on all six rendered dashboard charts
* No rendered link or button with a completely empty accessible name

No load test, dependency advisory scan, automated WCAG audit, or production
deployment test was performed. Those remain explicit confidence limits.

## Architecture Assessment

### What Works Well

The dependency model is clear and real. The architecture described in
[README.md](README.md#architecture) is encoded as import-linter contracts in
[backend/pyproject.toml](../backend/pyproject.toml#L62). Analysis covered 33
Python files and 69 dependencies without finding a broken component contract.
Only the API layer composes config, ingestion, storage, engine, openings, and
coach.

The shared domain module is a useful contract boundary rather than a dumping
ground. Models such as `Game`, `GameAnalysis`, `OpeningStats`, and
`PlayerReport` encode cross-component meaning in
[domain.py](../backend/src/chess_coach/domain.py). Important analytical rules,
including phase boundaries and mate-score normalization, live beside the
types that depend on them.

The API is a genuine composition root. Dependency injection in
[routes.py](../backend/src/chess_coach/api/routes.py#L59) keeps components
independent while still making orchestration visible. This is preferable to a
service-locator layer or cross-component imports hidden behind helpers.

The frontend/backend boundary is similarly disciplined. Request and response
types are primarily derived from FastAPI's generated OpenAPI schema in
[api.ts](../web/src/api.ts#L5). Handwritten SSE payloads are isolated and
document which backend model they mirror.

The architecture is proportionate to the product. SQLite, plain functions,
Pydantic models, and a small React client are appropriate choices for a local
single-user analysis tool. Introducing distributed queues, an ORM, or a
separate analytics service would add operational cost without solving the
current risks.

### Maintainability Hotspots

Several modules are now large because the product surface has grown:

* API orchestration in
  [routes.py](../backend/src/chess_coach/api/routes.py)
* LLM lifecycle logic in
  [providers.py](../backend/src/chess_coach/coach/providers.py)
* Report aggregation in
  [report.py](../backend/src/chess_coach/coach/report.py)
* Dashboard composition in
  [Dashboard.tsx](../web/src/pages/Dashboard.tsx)

These files remain cohesive enough to understand, so a broad refactor is not
currently justified. Further features should split them by owned workflow,
not by arbitrary file length. Good candidates are analysis orchestration,
coaching cache orchestration, dashboard highlights, and dashboard repertoire.

## Priority Findings

No critical-severity issue was found. One high-severity data-integrity issue
and ten medium-severity issues warrant planned remediation.

### F1: Migrations Are Not Failure-Atomic

Severity: High.

Dimension: Data integrity and recoverability.

The migration runner calls `executescript()`, then updates `user_version`, then
commits in
[db.py](../backend/src/chess_coach/storage/db.py#L111). Migration 007 contains
four `ALTER TABLE` statements followed by a data backfill in
[007_analysis_player_aggregates.sql](../backend/src/chess_coach/storage/migrations/007_analysis_player_aggregates.sql#L12).
The script does not contain an explicit transaction.

If a later statement fails, earlier schema changes can remain while
`user_version` still names the old migration. The next startup retries the
same `ALTER TABLE` statements against a partially migrated database and can no
longer self-recover.

Recommended action: execute each migration and its `user_version` update in
one explicit rollback-capable transaction. Add a test migration that fails
after an earlier DDL statement and assert that both schema and version return
to their original state. Keep a documented SQLite backup step before applying
future production-data migrations.

### F2: Analysis and LLM Cache Identities Omit Material Inputs

Severity: Medium.

Dimension: Correctness, provenance, and cost control.

Stored analysis staleness is based on depth and the integer
`ANALYSIS_VERSION` in
[games.py](../backend/src/chess_coach/storage/games.py#L218). Move judgments
also depend on configurable thresholds, but threshold values are not part of
the stored identity. Changing them can leave old and new classifications
mixed under the same analysis version.

Whole-report cache keys contain player, agent id, window, time class, and
prompt version in
[reports.py](../backend/src/chess_coach/storage/reports.py#L15). They do not
contain the selected provider, model, analysis fingerprint, engine-tool
configuration, or source-data revision. Cache lookup happens before the
report is rebuilt in
[routes.py](../backend/src/chess_coach/api/routes.py#L774). Move-explanation
keys similarly omit prompt and model versions.

The UI catches one stale case by comparing analyzed-game counts, but it cannot
detect changed analysis semantics, a reanalyzed fixed-size dataset, or a model
change under the same agent id.

Recommended action: persist deterministic fingerprints. The analysis
fingerprint should cover the engine semantic version, depth, thresholds, and
binary identity where practical. Coaching and explanation keys should cover
prompt version, agent provider/model, tool configuration, and analysis/data
revision. The design direction in [prompt-version-fingerprint.md][fingerprint]
is a suitable starting point but should include these wider inputs.

[fingerprint]: future-improvements/prompt-version-fingerprint.md

### F3: Some Live UCI Failures Can Recycle a Broken Worker

Severity: Medium.

Dimension: Subprocess reliability.

`Engine.stream_infos()` translates errors while creating the analysis object,
but the `async for` iteration itself is outside that handler in
[uci.py](../backend/src/chess_coach/engine/uci.py#L103). A raw
`chess.engine.EngineError` raised during iteration can therefore bypass the
component's typed `EngineError` contract.

The pool retires a worker only when its own `EngineError` is raised in
[pool.py](../backend/src/chess_coach/engine/pool.py#L263). An untranslated
iteration failure can return a dead or desynchronized worker to the idle
queue.

Recommended action: wrap creation, context entry, iteration, and context exit
as one translation boundary. Add a test double that raises the raw
python-chess exception after yielding at least one info event, then assert the
worker is retired and the next checkout respawns successfully.

### F4: Partial Startup Can Leak Earlier Resources

Severity: Medium.

Dimension: Lifecycle management.

Application startup opens the database, loads the opening book, creates the
engine pool, and creates LLM providers before reaching `yield` in
[app.py](../backend/src/chess_coach/api/app.py#L33). Cleanup exists only after
that yield. If a later provider or opening-book step fails, resources opened
earlier are not unwound by the lifespan function.

Pool construction also opens workers through a list comprehension in
[pool.py](../backend/src/chess_coach/engine/pool.py#L284). If worker N fails,
workers 1 through N-1 have no local rollback path.

Recommended action: use `AsyncExitStack` or explicit staged rollback for app
startup. Make `create_pool()` close all previously opened workers if any
subsequent open fails. Cover both paths with focused startup-failure tests.

### F5: The Typed Error Contract Is Not End to End

Severity: Medium.

Dimension: API reliability and supportability.

The ingestion contract says transport failures surface as `IngestionError`,
but [client.py](../backend/src/chess_coach/ingestion/client.py#L34) allows raw
httpx transport, HTTP, and JSON errors to escape. The service translates only
the unknown-user 404 path in
[service.py](../backend/src/chess_coach/ingestion/service.py#L34).

The app registers handlers for `UnknownUserError` and Starlette HTTP errors in
[app.py](../backend/src/chess_coach/api/app.py#L74), but it has no logged
catch-all that produces the documented `{"error": ...}` envelope. Unexpected
failures therefore do not consistently satisfy the API contract in
[07-api.md](07-api.md).

Recommended action: translate all ingestion boundary failures into typed
component exceptions with stable, non-sensitive messages. Add handlers for
request validation, component failures, and logged unexpected errors. Test
the JSON envelope for malformed upstream JSON, timeout, connection failure,
and internal exceptions.

### F6: Configuration Permits Runtime-Invalid Values

Severity: Medium.

Dimension: Configuration safety.

`depth`, `workers`, and `analyze_limit` are unconstrained integers in
[settings.py](../backend/src/chess_coach/config/settings.py#L24).
`Thresholds` accepts negative or unordered cutoffs in
[domain.py](../backend/src/chess_coach/domain.py#L34). Extra YAML keys are also
accepted by default.

Concrete failure modes include:

* `workers: 0` creates an empty pool whose checkout waits forever.
* A negative configured analysis limit becomes SQLite's unlimited limit.
* Unordered thresholds silently assign misleading judgments.
* Invalid ports and extreme depths fail later than configuration loading.

Recommended action: add field bounds, cross-field threshold ordering, and
`extra="forbid"` on configuration models. Validate that coach labels and model
names are non-empty. Add table-driven tests for every rejected boundary.

### F7: Analyze Request Setup Performs Synchronous DB Work on the Event Loop

Severity: Medium.

Dimension: Concurrency and responsiveness.

The async analyze endpoint directly calls `get_game()`,
`games_needing_analysis()`, and count queries in
[routes.py](../backend/src/chess_coach/api/routes.py#L297). Other expensive
routes correctly use `run_in_threadpool()`.

Most calls will be quick, but they share the serialized SQLite connection.
Waiting for that lock or deserializing a batch of game rows can pause unrelated
HTTP and SSE work on the event loop.

Recommended action: move game resolution and remaining-count calculation into
one synchronous function and run it in FastAPI's thread pool. Preserve the
one-run-per-player check on the event loop before registering the new run.

### F8: Failed Single-Game Analysis Polls Indefinitely

Severity: Medium.

Dimension: UI state recovery and unnecessary load.

The Game page enables a one-second refetch interval while local `analyzing`
state is true in
[Game.tsx](../web/src/pages/Game.tsx#L78). It sets that state after enqueue and
clears it only when an analysis appears in
[Game.tsx](../web/src/pages/Game.tsx#L142).

If the backend emits `run_failed`, no analysis appears. The button stays
disabled and the page polls once per second until navigation or reload. The
Games and Coach pages already use the terminal-aware
[useAnalysisProgress.ts](../web/src/useAnalysisProgress.ts) hook.

Recommended action: use the same SSE hook for single-game analysis. Stop on
success, `run_failed`, or stream loss; show a retryable error and invalidate
the game query once at termination. Add a component-level failure test.

### F9: Full-Archive Client Fetching Has a Silent Truncation Boundary

Severity: Medium.

Dimension: Scalability and data trust.

`api.allGames()` fetches 1,000-row pages into browser memory and stops at a
50,000-row guard in [api.ts](../web/src/api.ts#L70). If the guard is reached,
it logs a console warning and returns the accumulated rows as though they were
complete.

Games, dashboard statistics, rating charts, and parts of filtering depend on
this helper. A sufficiently large archive can therefore produce valid-looking
but incomplete analytics. Well below 50,000, repeated full-array filtering,
sorting, and chart derivation will eventually become the dominant frontend
cost.

Recommended action: return an explicit truncation result or throw instead of
silently accepting partial data. Longer term, expose server aggregates and
cursor-based game navigation, leaving client-side calculation for the current
page or a bounded requested window.

### F10: Expensive Endpoints Assume a Trusted Loopback User

Severity: Medium when network-exposed; low in the documented local workflow.

Dimension: Security, abuse resistance, and spend control.

The API has no authentication, authorization, request throttling, CSRF token,
or per-user ownership boundary. Analysis is limited to one run per username,
but many usernames can register runs, and coach `refresh` requests can trigger
concurrent paid or premium LLM usage.

The default `uvicorn` workflow listens on loopback, no permissive CORS policy
is configured, and the engine pool bounds Stockfish concurrency. Those are
appropriate local safeguards. They are insufficient if the app is bound to a
LAN or public interface.

Recommended action: document loopback-only support explicitly. Before any
network deployment, add identity, ownership, origin protection, global and
per-identity rate limits, request-size limits, LLM concurrency limits, and
spend telemetry. Add trusted-host and proxy configuration appropriate to the
deployment.

### F11: Narrow-Screen Layout Is Contained but Not Adapted

Severity: Medium.

Dimension: UI, UX, and accessibility.

The stylesheet has a dark-mode media query but no layout breakpoint in
[index.css](../web/src/index.css#L55). Flex wrapping and table scroll wrappers
prevent document overflow, which is a sound baseline, but the visual review
showed a 178-pixel sticky header at a 312 by 675 viewport. Player tabs wrapped
across two rows and Settings occupied a third. Only the dashboard title fit in
the remaining first viewport.

All eight rendered dashboard tables required horizontal scrolling at that
width. The dashboard remained one 7,855-pixel page with no local section
navigation or progressive disclosure.

Recommended action: introduce a compact mobile navigation pattern, keep the
current player visible, and reduce the sticky header to one row. Convert the
most important tables to responsive summary rows or column-priority views.
Add dashboard section navigation or collapsible secondary analysis sections.
Retain full data access rather than hiding columns without an alternative.

### F12: Several Frontend Contracts Have Drifted

Severity: Low.

Dimension: Documentation and interaction consistency.

The frontend contract promises a saved-player switcher in the shared header,
but [Layout.tsx](../web/src/components/Layout.tsx#L42) renders section links
and Settings only. Switching players requires a detour through Settings.

The recurring-mistake examples are documented as exact-ply deep links, but
[Dashboard.tsx](../web/src/pages/Dashboard.tsx#L708) links only to the game.
The student must find the example move manually.

The game navigation buttons in
[Game.tsx](../web/src/pages/Game.tsx#L240) use glyphs as their only accessible
names and have no explanatory tooltip. Screen readers may announce the glyph
rather than "first move", "previous move", "next move", and "last move".

Recommended action: implement the documented header switcher, include
`?ply={example_ply}` on recurring-mistake links, and add explicit `aria-label`
and `title` values to icon-only board controls.

## Code Quality and Domain Correctness

The codebase consistently uses narrow types, early returns, plain functions,
and lifecycle-owning classes only where state warrants them. Strict Pyright,
Ruff, Biome, and TypeScript checks all pass. Suppressions are rare and include
specific rationale.

Several implementation details are particularly strong:

* Ingestion mints perspective-specific identities so the same chess.com game
  can be stored for both tracked players without collision.
* Custom-start-position games are rejected because downstream replay assumes
  the standard initial board.
* ACPL is move-weighted rather than calculated as a mean of game means.
* Phase constants are shared so engine and coach aggregation cannot silently
  diverge.
* Opening statistics distinguish systems the player chose from openings the
  opponent chose, including color and transposition-aware semantics.
* Storage and coach implementations are checked for repertoire agreement in
  [test_repertoire_agreement.py](../backend/tests/test_repertoire_agreement.py).
* Critical positions are selected for instructional value rather than raw
  centipawn magnitude alone.
* Highlight and error-pattern generation is deterministic rather than left to
  an LLM.

The main correctness concern is provenance. The system calculates nuanced
derived data, but the persisted records do not yet identify every input that
made those calculations true. F2 should be addressed before adding more
configurable analysis semantics.

## Persistence and Data Lifecycle

The storage implementation shows careful understanding of SQLite concurrency.
The `Db` wrapper materializes rows under a re-entrant lock and holds that lock
through transaction commit in
[db.py](../backend/src/chess_coach/storage/db.py#L34). This is a deliberate
response to sharing one connection across FastAPI worker threads, not an
accidental global lock.

WAL mode, foreign keys, parameterized queries, compact list projections, and
precomputed analysis aggregates are appropriate for the workload. The
perspective-id migration and aggregate backfills have meaningful tests.

Data lifecycle is less developed than persistence correctness:

* There is no in-app player deletion or data-retention workflow.
* Full PGNs, analyses, prompts, and LLM advice remain in local SQLite until
  the user manually removes the database.
* Backup and restore are not first-class user operations.
* Migrations do not record an application or schema compatibility range.

For a local developer tool, these are acceptable early-stage omissions. A
broader audience would benefit from delete-player, export, backup, and restore
workflows before additional data surfaces are added.

## Engine and Long-Running Work

The engine subsystem is one of the strongest parts of the implementation.
It uses python-chess instead of a handwritten UCI parser, creates fresh search
state for reproducibility, bounds both per-position searches and streamed-info
gaps, kills wedged processes, retires failed workers, and drains cancellation
paths. The focused pool tests cover crashes, failed respawn, timeouts, caller
cancellation, stream cancellation, and shutdown.

The API tracks analysis tasks and cancels and awaits them before pool and DB
shutdown in [app.py](../backend/src/chess_coach/api/app.py#L56). Analysis can
continue when one game fails and exposes a terminal failed state over SSE.

The remaining reliability work is concentrated in F3, F4, and F7. One
additional operational risk is that subscriber queues in
[runs.py](../backend/src/chess_coach/api/runs.py#L47) are unbounded. A stalled
SSE client can accumulate progress events while analysis continues. A bounded
latest-state queue or coalesced progress event would make memory behavior
explicit without losing useful information.

## LLM Safety and Coaching Quality

LLM calls are explicit user actions and results are cached. This is a strong
product and cost-control decision. The prompt gives the model structured,
coverage-aware evidence and instructs it to state uncertainty rather than fill
unsupported sections.

Provider isolation is also strong:

* Claude built-in file, shell, and web tools are disabled.
* Copilot's available tool set contains only the injected position analyst.
* Engine tool turns are bounded, with timeout and runaway handling.
* Model-authored game-link definitions are stripped before trusted links are
  minted in
  [prompt.py](../backend/src/chess_coach/coach/prompt.py#L660).
* The frontend uses `react-markdown` without enabling raw HTML.

The product should state more directly that a requested coaching report sends
the rendered chess report, positions, and game context to the selected remote
provider. "Calls only fire when you ask" is useful but is not a complete data
disclosure. Provider-specific privacy and retention links would make consent
more informed.

## Frontend UI and UX

### Strengths

The desktop visual language is restrained and appropriate for an operational
chess tool. It uses a small tokenized palette, clear result and judgment
semantics, stable controls, compact KPI tiles, and OS-driven light and dark
themes. The desktop dashboard had no document overflow in the reviewed state.

The information architecture maps well to user goals:

* Games supports sync, filter, sort, selection, paging, and analysis.
* Game combines replay, stored evaluation, live engine lines, and on-demand
  explanation without hiding the board.
* Dashboard separates record, trend, terminations, opponent strength,
  repertoire, highlights, and recurring mistakes.
* Openings provides a useful move-tree drill-down rather than another flat
  opening table.
* Coach scopes advice to the same persisted time and time-control filters as
  the dashboard.

Context preservation is excellent. List links open in a new tab, exact-ply
links restore the relevant position, filter choices persist, stale advice is
identified, and the Coach page can analyze missing coverage without requiring
the user to leave the workflow.

Loading, error, empty, filtered-empty, partial-coverage, stale-cache, failed
run, and lost-stream states are handled in most major workflows. The single
game analysis path in F8 is the important exception.

### Accessibility

The code includes visible focus rings, native buttons and form controls,
semantic tables, `aria-sort`, labeled pagination, alert roles, keyboard
operation for the evaluation graph, and accessible chart names. Result and
evaluation meaning is not conveyed by color alone.

Accessibility is not yet proven end to end:

* Custom SVG charts expose a general label but not their underlying values as
  a screen-reader-accessible table or summary.
* The chessboard library's keyboard and screen-reader behavior was not
  independently verified.
* Icon-only navigation controls need explicit names, as noted in F12.
* No automated axe or browser-level keyboard test is present.
* No reduced-motion override exists for board and evaluation transitions.

A focused WCAG 2.2 AA pass should follow the mobile navigation work because
both touch the shared shell and core game controls.

## Security and Privacy

The source review found no committed application secret, dynamic SQL built
from user-controlled fragments, shell command injection path, unsafe YAML
loading, raw HTML rendering, or static-file path traversal.

Positive controls include:

* Local config and database files are ignored by Git.
* Config explicitly rejects secrets in YAML and reads reserved secrets from
  the environment.
* `yaml.safe_load()` is used.
* SQLite values are parameterized; dynamic clauses come from controlled code.
* Stockfish is launched as a fixed executable path without a shell.
* SPA file serving resolves candidates and verifies they remain below the
  distribution directory in
  [app.py](../backend/src/chess_coach/api/app.py#L84).
* External chess.com URLs come from the fixed public API flow rather than a
  caller-supplied arbitrary fetch endpoint.
* LLM tools are allow-listed and bounded.

Residual risks are primarily operational:

* The trust model depends on loopback-only access, as described in F10.
* Local SQLite content is not encrypted at rest.
* There is no user-facing deletion or retention control.
* Dependency advisories are not checked in CI.
* Logs are human-readable rather than structured and lack request correlation.
* No security headers are set for a network deployment.

## Performance and Scalability

The implementation contains several good scale-conscious choices:

* Opening data is parsed once into in-memory position indexes.
* Repertoire construction uses a two-pass trie rather than replaying every
  game at every node.
* Game list rows omit PGN and full move arrays.
* Analysis aggregates are persisted so opening statistics avoid reparsing
  every evaluation blob.
* Long CPU and storage operations are usually moved off the event loop.
* The Stockfish pool provides a natural concurrency bound.
* GZip is enabled for large JSON payloads.

The temporary 8,156-game archive rendered successfully, including a 998-game
default dashboard window. That is useful evidence for personal-archive scale,
not a load benchmark.

The next scale limits are predictable:

* Browser-wide `allGames()` fetching and repeated full-array derivation
* Report aggregation that replays analyzed games several times per request
* Large repertoire-tree payloads and client-side drill state
* Unbounded progress subscriber queues
* Synchronous analyze-request storage setup
* An eagerly loaded frontend bundle reported at roughly 564 KB minified by a
  review build

Route-level lazy loading is a low-risk first bundle improvement. Server-side
summary endpoints and cursor pagination are more important than micro-
optimizing current React calculations.

## Testing and CI

The test suite is a major strength. Backend tests cover component behavior,
API composition, migration upgrades, thread sharing, engine lifecycle,
deterministic coaching analytics, and cross-implementation repertoire
agreement. Frontend tests cover the highest-risk pure transformations,
including filters, pagination, score folding, family rollups, deep-link paths,
coverage chaining, and streamed explanation state.

The highest-value missing tests mirror the findings:

* Failed-migration rollback
* Raw UCI failure during streamed iteration
* Partial app and partial pool startup cleanup
* Ingestion timeout, connection, malformed JSON, and typed API envelopes
* Invalid worker, depth, limit, port, and threshold combinations
* Cache invalidation after threshold, model, tool, or analysis changes
* Failed single-game analysis termination
* Mobile navigation and exact-ply recurring-mistake links
* SSE backpressure and event-loop responsiveness under concurrent analysis

CI runs lint, format, strict typing, import contracts, tests, and frontend
builds in separate jobs in
[ci.yml](../.github/workflows/ci.yml). Lockfiles are committed and frontend
installation is frozen.

Recommended CI improvements:

* Generate OpenAPI and fail if
  [schema.d.ts](../web/src/api/schema.d.ts) changes. Current drift prevention
  depends on a developer remembering `make gen-api`.
* Add a browser smoke test for onboarding, Games, one analyzed game,
  Dashboard, Openings, and Coach error states.
* Add axe checks at desktop and mobile viewports.
* Add dependency advisory and secret scanning appropriate to the repository.
* Publish test and coverage summaries without making a numeric coverage target
  the goal by itself.
* Decide whether CI should initialize `vendor/chess-openings`. The current
  real-book test skips when the submodule is absent, and checkout does not
  request submodules. The five real Stockfish tests are also opt-in by design.
* Resolve the Starlette `TestClient` deprecation before the next major test
  client transition.

Some tests import component internals despite the public-surface rule in
[GUIDELINES.md](GUIDELINES.md#testing). This is understandable for lifecycle
and failure injection, but the guideline should either name narrow white-box
exceptions or tests should gain explicit public injection seams.

## Documentation and Developer Experience

Documentation quality is substantially above average. The architecture,
interfaces, data semantics, failure behavior, and build rationale are written
down in enough detail to support independent component work. The docs explain
why decisions exist, especially around perspective identity, ACPL weighting,
opening attribution, analysis coverage, and engine reproducibility.

The toolchain is small and coherent. `make check`, `make serve`,
`make gen-api`, and `make backfill` cover the important developer workflows.
Python and Node versions are pinned, local hooks cover the fast Python path,
and the frontend lockfile includes a registry-agnostic guard.

The cost of this depth is contract drift. Confirmed mismatches include:

* The missing header player switcher
* Recurring-mistake links that omit the exact ply
* Ingestion errors that do not always become `IngestionError`
* Unexpected API errors that do not always use the documented envelope
* The test public-surface rule and current white-box tests

Treat component documents as tested contracts where feasible. Small contract
tests are more durable than relying on prose review alone.

## Prioritized Roadmap

### P0: Protect Real Data

1. Make migrations and `user_version` updates atomic.
2. Add induced-failure rollback tests and document backup/restore.

### P1: Make Derived Results Trustworthy

1. Introduce analysis, report, and explanation fingerprints.
2. Validate all configuration bounds and cross-field invariants.
3. Translate all ingestion and engine failures at component boundaries.
4. Add partial-startup cleanup for the app and engine pool.
5. Move analyze request storage resolution into the thread pool.
6. End single-game analysis UI state on failed or lost SSE runs.

### P2: Improve Product Ergonomics

1. Implement a compact header with the documented player switcher.
2. Add responsive dashboard section navigation and table alternatives.
3. Add exact-ply recurring-mistake links and named icon controls.
4. Add an explicit provider data-disclosure note before LLM actions.
5. Add delete-player, export, backup, and restore workflows.

### P3: Raise the Scale and Delivery Ceiling

1. Replace silent archive truncation with an explicit result immediately.
2. Move archive-wide aggregates and pagination to the server when scale
   requires it.
3. Add OpenAPI drift, browser smoke, accessibility, and dependency checks to
   CI.
4. Add structured logs, health information, and workload metrics before any
   non-local deployment.
5. Add authentication, ownership, throttling, and spend controls before
   exposing the API to an untrusted network.

## Final Assessment

AI Chess Coach is a well-designed local application with strong boundaries,
excellent domain thinking, and credible automated verification. Its code is
generally readable, typed, and consistent with the repository's stated
engineering values. The core architecture should be preserved.

The next engineering phase should focus on trust under change and failure:
atomic migrations, explicit provenance, complete cleanup, stable error
contracts, and terminal UI states. After that, the largest product gain is a
purpose-built narrow-screen shell and a less linear dashboard. Those changes
would raise the application from a strong developer-grade tool to a more
resilient and broadly usable product without adding unnecessary architectural
complexity to its core.
