# Component 8 — Frontend (Vite + React)

The browser UI — the only TypeScript in the project. Talks
exclusively to the HTTP/SSE API defined in [07-api.md](07-api.md);
it shares no code with the backend. API request/response types are
generated from the backend's OpenAPI schema (`pnpm gen:api`), never
hand-written — except SSE payload types, which the schema cannot
express and are hand-declared with a comment naming the backend
model each mirrors (see [GUIDELINES.md](GUIDELINES.md)).

## Pages

1. **Home** — username input; triggers `POST /sync`, shows counts,
   links to the games list. Coach-agent selector fed by
   `GET /coach/agents`; the choice persists in localStorage and the
   Coach page sends it as `agent_id`.
2. **Games** — a sortable, paged table over the whole archive
   (`api.allGames`, the paged fetch shared with the Dashboard cache):
   result / time-class / analyzed-state filters and an opponent search
   applied client-side, click-to-sort column headers, and prev/next
   paging. A separate analyze bar (kept out of the filter row) posts to
   `/analyze` — "Analyze latest N" or "Analyze selected" via row
   checkboxes — with the progress bar fed by the SSE endpoint.
3. **Game** — `GET /games/{id}`: interactive board
   (`react-chessboard` + `chess.js` for replay), eval graph (custom
   SVG over `evals`), move list with judgment badges; clicking a
   move syncs board + graph. An unanalyzed game shows an "Analyze
   this game" button posting `/analyze` for that single game and
   polls `GET /games/{id}` until the results land; an analyzed game
   shows a summary strip (overall ACPL, blunder and mistake counts,
   engine depth). Live-engine toggle: while on, each shown position
   streams `GET /eval` (SSE) into a candidate-lines panel (the
   server decides how many lines; the UI renders whatever the
   snapshot carries); the stream is dropped and reopened when the
   ply changes. The panel is self-explanatory to a non-engine user:
   a header names the side to move and what the list is, each row
   leads with the candidate move (bold SAN) and a sign-colored eval
   chip, the continuation is secondary text, raw engine detail
   (depth) lives in a tooltip, and a one-line legend states the sign
   convention (+ White / − Black). On an analyzed game, the selected
   move offers an "Explain" button (explicitly user-triggered — it
   spends LLM calls) that streams `GET /games/{id}/explain` into a
   titled coach panel ("Coach on 14...f3"), with tool events shown
   as progress lines while it streams (cleared once the explanation
   completes); cached explanations render instantly, with a
   "Regenerate" action (`refresh=true`, same user-triggered rule)
   for stale ones.
4. **Dashboard** — the player's stats hub: summary tiles (record,
   win rate by color, current rating per time class, ACPL, blunder
   rate), rating-over-time and monthly-activity charts from
   `GET /players/{u}/games` (paged fetch), ACPL-by-phase and
   judgment charts from `GET /players/{u}/report`, and the
   worst-first repertoire table from `GET /players/{u}/openings`.
   A time-window control (all-time / 30d / 90d / 6mo / 1yr) scopes the
   whole page: games-derived stats are filtered client-side, while
   report and openings are re-fetched with the `since` epoch-second
   window. Charts are custom SVG components — no chart library.
5. **Coach** — `POST /coach` with the selected `agent_id`; renders
   the advice (markdown) and the generated prompt with a copy
   button (the manual-use fallback); shows/lets you switch the
   active agent.

## Stack and structure

- Vite + React + TypeScript; TanStack Query for data fetching and
  cache invalidation (sync/analyze invalidate games + report).
- React Router for the five pages; a small typed API client module
  (`web/src/api.ts`) wraps the generated types and is the only place
  URLs appear.
- A shared `Layout` (`web/src/components/Layout.tsx`) wraps every page
  with the app header — brand, player-scoped section tabs (Games /
  Dashboard / Coach), and a player switcher — so navigation lives in
  one place instead of per-page links.
- Colors are CSS custom properties defined once in `index.css`
  (light + dark via `prefers-color-scheme`); the SVG charts read the
  same tokens through `components/chartTheme.ts`.
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
