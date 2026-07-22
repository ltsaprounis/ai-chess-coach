# Component 8 — Frontend (Vite + React)

The browser UI. Talks exclusively to the HTTP/SSE API defined in
[07-server.md](07-server.md) — it never imports server code, and the
API JSON shapes mirror `shared/types.ts`.

## Pages

1. **Home** — username input; triggers `POST /sync`, shows counts,
   links to the games list.
2. **Games** — table from `GET /players/:u/games` with opening,
   result, and analyzed-state filters; "Analyze all" button posting
   to `/analyze`, progress bar fed by the SSE endpoint.
3. **Game** — `GET /games/:id`: interactive board
   (`react-chessboard` + `chess.js` for replay), eval graph (custom
   SVG over `evals`), move list with judgment badges; clicking a
   move syncs board + graph.
4. **Dashboard** — `GET /players/:u/report`: ACPL by phase, judgment
   totals, per-opening record table sorted worst-first.
5. **Coach** — `POST /coach`; renders the advice (markdown) and the
   generated prompt with a copy button (the manual-use fallback).

## Stack and structure

- Vite + React + TypeScript; TanStack Query for data fetching and
  cache invalidation (sync/analyze invalidate games + report).
- React Router for the five pages; a small typed API client module
  (`web/src/api.ts`) is the only place URLs appear.
- Dev: Vite proxies `/api` to the server port; prod: the server
  serves `web/dist` statically.

## Dependencies

- The server HTTP API only ([07-server.md](07-server.md)).
- Libraries: `react-chessboard`, `chess.js`, TanStack Query.

## Build plan

1. Scaffold, router, API client, query setup.
2. Home + Games pages (sync flow end to end).
3. Game page: board, move list, eval graph.
4. Analysis progress UI (SSE hook).
5. Dashboard + Coach pages.
