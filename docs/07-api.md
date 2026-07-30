# Component 7 — API layer (FastAPI)

The composition root. The only module that imports components 1-6; it
wires them together behind an HTTP API and owns all orchestration
(what gets fetched, analyzed, classified, stored, and when).

## Startup sequence (FastAPI lifespan)

1. `load_config()` — [config](01-config.md)
2. `open_db(cfg.storage.db_path)` — [storage](03-storage.md)
3. `load_opening_book(cfg.openings.book_dir or _DEFAULT_BOOK_DIR)`
   — [openings](05-openings.md); the default is the submodule under
   config's `REPO_ROOT`
4. `await create_pool(bin_path, cfg.engine.workers,
   cfg.engine.eval_timeout)` — [engine](04-engine.md)
5. `create_provider(agent, cfg.anthropic_api_key)` for each agent in
   `cfg.coach.agents` → `dict[agent_id, CoachProvider]` on
   `app.state.providers` — [coach](06-coach.md). A misconfigured
   agent fails startup (fail fast, consistent with config).
6. Register routers; serve the built frontend statically in prod
   (SPA fallback: unknown non-API paths serve index.html so
   client-side routes survive refreshes and deep links). Starlette's
   `GZipMiddleware` (`minimum_size=1000`) is app-wide — the openings
   tree (low hundreds of KB worst case) motivates it, but any JSON
   response over the threshold benefits.

Shutdown cancels any in-flight analysis-run tasks and awaits them
before closing the pool and the DB. Instances live on `app.state`,
injected into routes via FastAPI dependencies. Analysis runs are
tracked in-process, one per username: a new run replaces that user's
previous one, and once finished runs exceed a cap
(`MAX_FINISHED_RUNS`) the oldest are swept when the next run
registers — `analyze/progress` for a swept run 404s like any unknown
run. Active runs are never swept.

## HTTP API (the contract for [08-frontend.md](08-frontend.md))

| Method | Path                                   | Behavior            |
|--------|----------------------------------------|---------------------|
| POST   | `/api/players/{u}/sync`                | Run ingestion from `latest_game_time`; upsert + classify openings; return counts. Query `full` (default false) re-fetches the entire archive instead — the upsert makes it idempotent — to backfill columns added after games were stored (currently `termination`; a normal sync never re-fetches a stored game) |
| GET    | `/api/players`                         | Stored players (`{username, games, last_played}`), most games first — the saved-players picker |
| GET    | `/api/players/{u}/games`               | List games (query: opening, result, time_class, analyzed, paging). Rows are slim `GameSummary` (no pgn/full moves — see docs/03-storage.md), so the frontend can page through the whole archive |
| GET    | `/api/players/{u}/openings`            | Per-opening record (games, W/L/D; avg cp loss once analyzed); optional `since`/`until` epoch-second window and `time_class` |
| GET    | `/api/players/{u}/openings/tree`       | Repertoire move tree for one color (docs/future-improvements/openings-explorer.md). Query: `color` (required), `since`/`until`, `time_class` (same window semantics as `/openings`), `min_games` (default 2, clamped 1-10), `max_plies` (default 30, clamped 4-40) |
| GET    | `/api/games/{id}`                      | Game + analysis + opening |
| POST   | `/api/players/{u}/analyze`             | Enqueue newest unanalyzed games up to body `limit` (capped by `engine.analyze_limit`), or explicit body `game_ids`; 202 with queued+remaining. "Unanalyzed" includes games whose stored analysis predates `engine.ANALYSIS_VERSION` (enqueue and `remaining` alike), so an engine version bump re-queues stored games automatically. Optional body `since`/`until`/`time_class` scope the bulk path — both the enqueue and `remaining` (`game_ids` ignores them). Zero resolved games starts no run and still answers 202, so `limit: 0` is a pure "how much is left?" probe and `queued=0, remaining=0` is a backfill's termination signal |
| GET    | `/api/players/{u}/analyze/progress`    | SSE stream of pool progress events |
| GET    | `/api/players/{u}/report`              | `build_report` over stored analyses; optional `since`/`until` epoch-second window and `time_class` |
| GET    | `/api/players/{u}/highlights`          | `build_highlights` over stored analyses — the Dashboard's blunders + brilliancies lists (`PlayerHighlights`), each entry deep-linkable to `/games/{id}` at its ply; same optional `since`/`until`/`time_class` as `/report`. Kept out of `PlayerReport` so the coach prompt isn't bloated; brilliant cutoffs injected from config's `brilliant` section |
| GET    | `/api/coach/agents`                    | Selectable coach agents: `{agents: [{id, label, provider, model}], default}` from config |
| POST   | `/api/players/{u}/coach`               | Build report → `render_prompt` → chosen provider (agentic with the engine tool when the pool is up) → `append_game_links` (the advice's `[gN]` citations become `/games/{id}?ply=` links; see 06-coach.md "Game links"), cached post-processed. Optional body `{agent_id, since, until, time_class, refresh}` — the same window and time-control filters `/report` takes, so the coach reasons over the period the student is looking at (default agent otherwise, 400 on unknown id); returns `{prompt, advice, agent_id, cached, generated_at, games_analyzed}` |
| GET    | `/api/players/{u}/profile`             | The student profile (docs/06-coach.md, "Player profile"): facts always fresh (`build_report` → `build_profile` over the full stored history), narrative and its metadata (`agent_id`, `prompt_version`, `generated_at`, `games_covered` at generation) from storage — never an LLM call. Unknown player: empty facts and no narrative, 200 like `/report` |
| POST   | `/api/players/{u}/profile`             | Regenerate the narrative (user-triggered — LLM calls cost money; GET never generates): body `{agent_id?}` (default agent otherwise, 400 on unknown id); fresh facts → `render_profile_prompt` → `complete(analyst=None)` → `save_player_profile`; returns the same shape as GET. 409 with no analyzed games to describe |
| GET    | `/api/eval`                            | SSE live eval of one position: query `fen` (required), `depth` (optional, default `engine.depth`, clamped 1-40), `multipv` (optional, default `engine.multipv`, clamped 1-10); `eval` event per `LiveEval` snapshot, then `done` |
| GET    | `/api/games/{id}/explain`              | SSE coach explanation of one move: query `ply` (required, 1-based), `agent_id` (optional, default agent), `refresh` (optional, default false — skip the cache read and regenerate; the result overwrites the cached row). Cached hit: one `done` event with the full text. Miss or refresh: `text`/`tool` events (mirroring coach `ExplainEvent`) while the agent works, then `done` with the full text (now cached) |
| POST   | `/api/players/{u}/chat/threads`        | Create a chat thread: body `{scope, agent_id?, game_id?, ply?, since?, until?, time_class?}`. `scope="game"` requires `game_id` (400 without; 404 unknown game; a `ply` anchor additionally requires analysis — 409 unanalyzed, 400 out-of-range, mirroring explain). `scope="report"` rejects `game_id`/`ply` (400). Unknown `agent_id` 400; omitted = default agent. Returns the thread |
| GET    | `/api/players/{u}/chat/threads`        | List the player's threads, most recently updated first (id, scope, anchor fields, agent, title = first user line, message count, updated_at) |
| GET    | `/api/chat/threads/{id}`               | Thread + full transcript (`ChatMessage` rows, oldest first); 404 unknown |
| POST   | `/api/chat/threads/{id}/messages`      | Body `{text}` (400 blank). SSE reply: `text`/`tool` events while the agent works, then `done` with the full reply — persisted, with the new `provider_state`, before `done` is emitted. `error` event on `CoachProviderError`, nothing persisted, `provider_state` cleared. 404 unknown thread; 409 while a reply is already streaming for this thread, or when the thread is at the message cap ("start a new chat") |
| DELETE | `/api/chat/threads/{id}`               | Delete thread + transcript; 404 unknown, 204 on success |

Request/response models are pydantic, so FastAPI's OpenAPI schema is
complete — the frontend generates its TS types from it
(`pnpm gen:api`, see [GUIDELINES.md](GUIDELINES.md)).

Errors are JSON `{"error": {"code", "message"}}` via exception
handlers; ingestion's `UnknownUserError` maps to 404, everything
unexpected to 500.

## Orchestration notes

- Sync pipeline per batch: `upsert_games` → `classify` each →
  `set_opening`. Openings classification is cheap; do it at ingest.
- Analysis flow: pool task resolves → `save_analysis`, stamped with
  `engine.ANALYSIS_VERSION` — the API layer is the one place that
  imports the constant and threads it into storage (save and the
  needing-analysis queries alike); components stay decoupled.
  Progress events fan out to open SSE connections (sse-starlette). A
  missing engine binary is not fatal at startup — analyze returns 503
  with a `make engine` hint; one run per player at a time (409
  otherwise). A position that exceeds `engine.eval_timeout` surfaces
  as `EngineError`: that one game fails, the run continues, and the
  pool respawns the killed worker — a hang costs seconds now, not a
  manual kill (docs/archive/engine-search-hangs.md).
  Archive-scale backfills ride this endpoint too:
  `backend/scripts/backfill.py` (`make backfill`) loops scoped
  requests until `queued=0, remaining=0`, treating 409 as "batch
  still running", and follows `analyze/progress` for the per-game
  x/X line — a second consumer of that stream besides the UI, and a
  purely optional one: if the stream drops it falls back to the 409
  poll, which is the authoritative liveness signal. It is an HTTP
  client only — it never touches the DB or imports components
  (docs/archive/fixes-2026-07/07-analysis-coverage.md).
- Highlights (`/players/{u}/highlights`) is `list_analyzed_games` →
  `build_highlights` with `cfg.brilliant`, mirroring `/report`'s shape:
  same window/time-class query params, same threadpool execution (it
  replays games with python-chess), and an unknown player returns
  empty lists rather than 404, consistent with `/openings` and
  `/report`.
- The openings tree (`/players/{u}/openings/tree`) is
  `list_repertoire_games` → `build_repertoire`, one fetch per color —
  the frontend drills client-side with no further requests. `color`
  is required (a bad value 422s, per FastAPI's own Literal
  validation); `min_games`/`max_plies` clamp silently (max/min, no
  422), like `/api/eval`'s depth/multipv, and both are passed through
  to `list_repertoire_games` and `build_repertoire` identically so the
  two components agree on the ply cap. An unknown player has no
  stored games, so the route returns an empty tree (`games=0`, a
  childless root) rather than 404ing, consistent with `/openings` and
  `/report`. The route reads the built root's own `record.games`/
  `analyzed` for the response's scope totals rather than re-counting.
- The coach route reads everything from storage — a game with no
  analysis is simply excluded from the report. That exclusion must
  never be silent: both `/report` and `/coach` pass `build_report`
  the requested `since`/`until` and storage's `count_games(...)`
  over the same window/time-class filters, so the report states its
  own coverage (see 06-coach.md, "Coverage is stated, not implied").
- Coaching (`POST /players/{u}/coach`) is the most expensive call the
  app makes, so it follows the explain rule: user-triggered **and**
  cached. The window/time-class filters are part of the cache key
  along with the agent and `coach.PROMPT_VERSION` — two runs over
  different periods are different reports, not a cache hit — with a
  `refresh` escape hatch mirroring `GET /games/{id}/explain`. The
  response reports `games_analyzed` at generation time so the UI can
  say "generated over 515 games; you have 540 now" rather than serving
  stale advice silently. When the engine pool is up, the route passes
  `complete` the same analyst wrapper explain builds around
  `pool.eval_lines` (config depth/multipv), so the report run can
  verify concrete lines with `analyze_position`; with no pool it
  passes `None` and the provider degrades to a single turn — the
  report still generates without an engine.
- Player profile (docs/06-coach.md, "Player profile"): GET assembles
  fresh facts — `list_analyzed_games` → `build_report` →
  `build_profile`, in the threadpool like `/report` — and attaches
  the stored narrative row when one exists; POST regenerates through
  the chosen provider and saves. The **stored** row, never a fresh
  aggregation, feeds the embed paths: the explain miss path and the
  game-scope chat seed read `get_player_profile` and pass the
  profile into their render calls — one row read per call; no row →
  `None` → the prompts render exactly as before. Report scope never
  embeds it (the report is the profile's own source).
- Chat (docs/future-improvements/coach-chat.md is the design
  record). The stored transcript is the source of truth; the API
  layer never interprets `provider_state`, only persists and returns
  it. Per message: load thread + transcript, build the per-thread
  `ChatToolkit` — `find_games`/`get_game`/`opening_stats` wrap the
  sync storage repos in starlette's `run_in_threadpool` (the same
  offload every sync route here rides), pre-scoped to the
  thread's username (`get_game` returns None for another player's
  game id — the model can never read across players);
  `opening_stats` applies the thread's window; `analyst` is the same
  `pool.eval_lines` wrapper explain uses, or None when the pool is
  down — then stream `provider.chat(...)` as SSE. Seeds: game scope
  renders `render_game_chat_context` (with `eval_lines` seeding at
  the anchored ply and the stored profile, both as explain does);
  report scope builds the report over the thread window and renders
  `render_report_chat_context` (no profile block — the report is the
  profile's own source).
  A cached explanation (game scope, anchored ply) or cached advice
  (report scope) for the thread's agent is prepended to history as
  the first assistant turn. The user message and reply are persisted
  atomically via `append_chat_exchange` before `done` is emitted;
  client disconnect or a mid-stream `CoachProviderError` persists
  nothing and clears `provider_state` (the discarded turn may have
  reached the provider's warm session — the next turn must replay
  from the stored transcript, docs/06-coach.md "Chat"). One reply
  in flight per thread (in-process per-thread locks, 409 otherwise);
  threads cap at `CHAT_MESSAGE_CAP` (40) messages — 409 directs
  the student to a new thread. Thread ids are uuid4, minted here.
- Live eval (`/api/eval`): engine's `stream_eval` does the work; the
  route maps its `ValueError` (bad FEN) to 400 and a missing pool to
  503, and closes the iterator when the client disconnects so the
  worker frees immediately. One stream per request; the frontend
  drops the old stream when the shown position changes.
- Explain (`/api/games/{id}/explain`) runs only on explicit user
  request (LLM calls cost money) and caches per
  (game, ply, agent) via storage's explanation repo. Miss path:
  `get_game` (404 unknown, 409 unanalyzed) → coach
  `build_move_context` (`ValueError` → 400, like unknown `agent_id`)
  → `eval_lines(fen_before)` with config depth/multipv (missing pool
  → 503, `EngineError` → 502) → `render_explain_prompt` (with the
  stored profile block when a `player_profiles` row exists) →
  `provider.explain(prompt,
  analyst)` streamed as SSE, then `save_explanation`. The `analyst`
  is the API layer's wrapper around `pool.eval_lines` with config
  depth/multipv — this is where coach meets engine; they never
  import each other. Client disconnects abort generation and cache
  nothing. A `CoachProviderError` raised mid-stream is too late for
  an HTTPException, so it becomes an `error` SSE event instead and
  nothing is cached.

## Dependencies

All of components 1-6 (this is the point). FastAPI + uvicorn +
sse-starlette for HTTP.

## Build plan

1. App factory with the lifespan above + dependency wiring.
2. Sync + games routes; wire ingestion/openings/storage.
3. Analyze route + SSE progress.
4. Report + coach routes.
5. Integration tests with `TestClient`/`httpx.ASGITransport`, a temp
   DB, a stub engine pool, and a stub provider (no real Stockfish or
   LLM calls in CI).
