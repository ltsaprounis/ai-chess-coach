# Component 8 — Frontend (Vite + React)

The browser UI — the only TypeScript in the project. Talks
exclusively to the HTTP/SSE API defined in [07-api.md](07-api.md);
it shares no code with the backend. API request/response types are
generated from the backend's OpenAPI schema (`pnpm gen:api`), never
hand-written.

## Pages

1. **Home** — username input; triggers `POST /sync`, shows counts,
   links to the games list.
2. **Games** — table from `GET /players/{u}/games` with opening,
   result, time-class, and analyzed-state filters; "Analyze all"
   button posting to `/analyze`, progress bar fed by the SSE
   endpoint.
3. **Game** — `GET /games/{id}`: interactive board
   (`react-chessboard` + `chess.js` for replay), eval graph (custom
   SVG over `evals`), move list with judgment badges; clicking a
   move syncs board + graph.
4. **Dashboard** — repertoire record table from
   `GET /players/{u}/openings`, sorted worst-first; ACPL by phase
   and judgment totals join from `GET /players/{u}/report` once
   analysis ships.
5. **Coach** — `POST /coach`; renders the advice (markdown) and the
   generated prompt with a copy button (the manual-use fallback).

## Stack and structure

- Vite + React + TypeScript; TanStack Query for data fetching and
  cache invalidation (sync/analyze invalidate games + report).
- React Router for the five pages; a small typed API client module
  (`web/src/api.ts`) wraps the generated types and is the only place
  URLs appear.
- `chess.js` stays a frontend-only dependency for board replay; the
  backend uses python-chess independently.
- Dev: Vite proxies `/api` to the FastAPI port; prod: FastAPI serves
  `web/dist` statically.

## Dependencies

- The backend HTTP API only ([07-api.md](07-api.md)).
- Libraries: `react-chessboard`, `chess.js`, TanStack Query.

## Build plan

1. Scaffold, router, generated API types + client, query setup.
2. Home + Games pages (sync flow end to end).
3. Game page: board, move list, eval graph.
4. Analysis progress UI (SSE hook).
5. Dashboard + Coach pages.
