# Component 7 — Server (Fastify API)

The composition root. The only module that imports components 1-6; it
wires them together behind an HTTP API and owns all orchestration
(what gets fetched, analyzed, classified, stored, and when).

## Startup sequence

1. `loadConfig()` — [config](01-config.md)
2. `openDb(cfg.storage.dbPath)` — [storage](03-storage.md)
3. `loadOpeningBook('vendor/chess-openings')` —
   [openings](05-openings.md)
4. `createAnalysisPool(binPath, cfg.engine.workers)` —
   [engine](04-engine.md)
5. `createProvider(cfg.llm, env.ANTHROPIC_API_KEY)` —
   [coach](06-coach.md)
6. Register routes; serve the built frontend statically in prod.

## HTTP API (the contract for [08-frontend.md](08-frontend.md))

| Method | Path                                   | Behavior            |
|--------|----------------------------------------|---------------------|
| POST   | `/api/players/:u/sync`                 | Run ingestion from `latestGameTime`; upsert + classify openings; return counts |
| GET    | `/api/players/:u/games`                | List games (query: opening, result, analyzed, paging) |
| GET    | `/api/games/:id`                       | Game + analysis + opening |
| POST   | `/api/players/:u/analyze`              | Enqueue unanalyzed games (or body `gameIds`); 202 |
| GET    | `/api/players/:u/analyze/progress`     | SSE stream of pool progress events |
| GET    | `/api/players/:u/report`               | `buildReport` over stored analyses |
| POST   | `/api/players/:u/coach`                | Build report → `renderPrompt` → provider; returns `{ prompt, advice }` |

Errors are JSON `{ error: { code, message } }`; ingestion's
`UnknownUserError` maps to 404, everything unexpected to 500.

## Orchestration notes

- Sync pipeline per batch: `upsertGames` → `classify` each →
  `setOpening`. Openings classification is cheap; do it at ingest.
- Analysis results flow: pool promise resolves → `saveAnalysis`.
  Progress events fan out to any open SSE connections.
- The coach route reads everything from storage — a game with no
  analysis is simply excluded from the report.

## Dependencies

All of components 1-6 (this is the point). Fastify for HTTP.

## Build plan

1. App factory (startup sequence above) + graceful shutdown.
2. Sync + games routes; wire ingestion/openings/storage.
3. Analyze route + SSE progress.
4. Report + coach routes.
5. Integration tests with a temp DB, a stub engine pool, and a stub
   provider (no real Stockfish or LLM calls in CI).
