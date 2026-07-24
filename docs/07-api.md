# Component 7 — API layer (FastAPI)

The composition root. The only module that imports components 1-6; it
wires them together behind an HTTP API and owns all orchestration
(what gets fetched, analyzed, classified, stored, and when).

## Startup sequence (FastAPI lifespan)

1. `load_config()` — [config](01-config.md)
2. `open_db(cfg.storage.db_path)` — [storage](03-storage.md)
3. `load_opening_book(Path("vendor/chess-openings"))` —
   [openings](05-openings.md)
4. `await create_pool(bin_path, cfg.engine.workers)` —
   [engine](04-engine.md)
5. `create_provider(agent, cfg.anthropic_api_key)` for each agent in
   `cfg.coach.agents` → `dict[agent_id, CoachProvider]` on
   `app.state.providers` — [coach](06-coach.md). A misconfigured
   agent fails startup (fail fast, consistent with config).
6. Register routers; serve the built frontend statically in prod
   (SPA fallback: unknown non-API paths serve index.html so
   client-side routes survive refreshes and deep links).

Shutdown closes the pool and the DB. Instances live on `app.state`,
injected into routes via FastAPI dependencies.

## HTTP API (the contract for [08-frontend.md](08-frontend.md))

| Method | Path                                   | Behavior            |
|--------|----------------------------------------|---------------------|
| POST   | `/api/players/{u}/sync`                | Run ingestion from `latest_game_time`; upsert + classify openings; return counts |
| GET    | `/api/players/{u}/games`               | List games (query: opening, result, time_class, analyzed, paging) |
| GET    | `/api/players/{u}/openings`            | Per-opening record (games, W/L/D; avg cp loss once analyzed); optional `since`/`until` epoch-second window and `time_class` |
| GET    | `/api/games/{id}`                      | Game + analysis + opening |
| POST   | `/api/players/{u}/analyze`             | Enqueue newest unanalyzed games up to body `limit` (capped by `engine.analyze_limit`), or explicit body `game_ids`; 202 with queued+remaining |
| GET    | `/api/players/{u}/analyze/progress`    | SSE stream of pool progress events |
| GET    | `/api/players/{u}/report`              | `build_report` over stored analyses; optional `since`/`until` epoch-second window and `time_class` |
| GET    | `/api/coach/agents`                    | Selectable coach agents: `{agents: [{id, label, provider, model}], default}` from config |
| POST   | `/api/players/{u}/coach`               | Build report → `render_prompt` → chosen provider; optional body `{agent_id}` (default agent otherwise, 400 on unknown id); returns `{prompt, advice, agent_id}` |
| GET    | `/api/eval`                            | SSE live eval of one position: query `fen` (required), `depth` (optional, default `engine.depth`, clamped 1-40), `multipv` (optional, default `engine.multipv`, clamped 1-10); `eval` event per `LiveEval` snapshot, then `done` |
| GET    | `/api/games/{id}/explain`              | SSE coach explanation of one move: query `ply` (required, 1-based), `agent_id` (optional, default agent), `refresh` (optional, default false — skip the cache read and regenerate; the result overwrites the cached row). Cached hit: one `done` event with the full text. Miss or refresh: `text`/`tool` events (mirroring coach `ExplainEvent`) while the agent works, then `done` with the full text (now cached) |

Request/response models are pydantic, so FastAPI's OpenAPI schema is
complete — the frontend generates its TS types from it
(`pnpm gen:api`, see [GUIDELINES.md](GUIDELINES.md)).

Errors are JSON `{"error": {"code", "message"}}` via exception
handlers; ingestion's `UnknownUserError` maps to 404, everything
unexpected to 500.

## Orchestration notes

- Sync pipeline per batch: `upsert_games` → `classify` each →
  `set_opening`. Openings classification is cheap; do it at ingest.
- Analysis flow: pool task resolves → `save_analysis`. Progress
  events fan out to open SSE connections (sse-starlette). A missing
  engine binary is not fatal at startup — analyze returns 503 with a
  `make engine` hint; one run per player at a time (409 otherwise).
- The coach route reads everything from storage — a game with no
  analysis is simply excluded from the report.
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
  → 503, `EngineError` → 502) → `render_explain_prompt` →
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
