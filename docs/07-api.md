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
| GET    | `/api/players/{u}/openings`            | Per-opening record (games, W/L/D; avg cp loss once analyzed) |
| GET    | `/api/games/{id}`                      | Game + analysis + opening |
| POST   | `/api/players/{u}/analyze`             | Enqueue newest unanalyzed games up to body `limit` (capped by `engine.analyze_limit`), or explicit body `game_ids`; 202 with queued+remaining |
| GET    | `/api/players/{u}/analyze/progress`    | SSE stream of pool progress events |
| GET    | `/api/players/{u}/report`              | `build_report` over stored analyses |
| GET    | `/api/coach/agents`                    | Selectable coach agents: `{agents: [{id, label, provider, model}], default}` from config |
| POST   | `/api/players/{u}/coach`               | Build report → `render_prompt` → chosen provider; optional body `{agent_id}` (default agent otherwise, 400 on unknown id); returns `{prompt, advice, agent_id}` |

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
