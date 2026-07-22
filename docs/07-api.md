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
5. `create_provider(cfg.llm, cfg.anthropic_api_key)` —
   [coach](06-coach.md)
6. Register routers; serve the built frontend statically in prod.

Shutdown closes the pool and the DB. Instances live on `app.state`,
injected into routes via FastAPI dependencies.

## HTTP API (the contract for [08-frontend.md](08-frontend.md))

| Method | Path                                   | Behavior            |
|--------|----------------------------------------|---------------------|
| POST   | `/api/players/{u}/sync`                | Run ingestion from `latest_game_time`; upsert + classify openings; return counts |
| GET    | `/api/players/{u}/games`               | List games (query: opening, result, time_class, analyzed, paging) |
| GET    | `/api/games/{id}`                      | Game + analysis + opening |
| POST   | `/api/players/{u}/analyze`             | Enqueue unanalyzed games (or body `game_ids`); 202 |
| GET    | `/api/players/{u}/analyze/progress`    | SSE stream of pool progress events |
| GET    | `/api/players/{u}/report`              | `build_report` over stored analyses |
| POST   | `/api/players/{u}/coach`               | Build report → `render_prompt` → provider; returns `{prompt, advice}` |

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
  events fan out to open SSE connections (sse-starlette).
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
