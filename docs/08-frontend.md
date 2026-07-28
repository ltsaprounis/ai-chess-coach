# Component 8 — Frontend (Vite + React)

The browser UI — the only TypeScript in the project. Talks
exclusively to the HTTP/SSE API defined in [07-api.md](07-api.md);
it shares no code with the backend. API request/response types are
generated from the backend's OpenAPI schema (`pnpm gen:api`), never
hand-written — except SSE payload types, which the schema cannot
express and are hand-declared with a comment naming the backend
model each mirrors (see [GUIDELINES.md](GUIDELINES.md)).

## Pages

1. **Home** (`/`) — a redirect to the last-viewed player's dashboard
   (the player is remembered in localStorage; falls back to the
   most-played stored player from `GET /api/players`). With no stored
   players it shows onboarding: an add-a-player form (`POST /sync`).
2. **Games** — a sortable, paged table over the whole archive
   (`api.allGames`, the paged fetch shared with the Dashboard cache):
   result / time-class / analyzed-state filters and an opponent search
   applied client-side, click-to-sort column headers, and prev/next
   paging. A "Sync new games" button pulls fresh games for the player
   (`POST /sync`, incremental) and refreshes the derived caches. Beside
   it, a low-emphasis "Full re-sync" action (`POST /sync?full=true`)
   re-fetches the whole archive to backfill columns — currently
   `termination` — added after older games were stored; both buttons
   disable while either sync is pending, and it can be slow on a large
   archive. A separate analyze bar (kept out of the filter row) posts
   to `/analyze` — "Analyze latest N" or "Analyze selected" via row
   checkboxes — with the progress bar fed by the SSE endpoint.
3. **Game** — `GET /games/{id}`: interactive board
   (`react-chessboard` + `chess.js` for replay), eval graph (custom
   SVG over `evals`), move list with judgment badges; clicking a
   move syncs board + graph. The page supports ply deep-linking: a
   `?ply=N` query param opens the board positioned after move N
   (clamped to the game's range once moves load; absent or invalid
   means the start position), so the Dashboard's highlight rows and
   any shared link land on the exact move. Navigating moves afterwards
   is local state; it does not rewrite the URL. On an analyzed game
   the board overlays
   the analysis: the last move's squares are shaded by its judgment,
   and a green best-move arrow (from the stored `best_move`) appears at
   positions where the move actually played was an inaccuracy, mistake,
   or blunder. An unanalyzed game shows an "Analyze
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
   judgment charts plus the termination breakdown and the monthly
   ACPL/blunder-rate trend from `GET /players/{u}/report`, and the
   sortable repertoire table from `GET /players/{u}/openings`
   (collapsed client-side into opening families with a min-games
   threshold, showing analyzed coverage — see `openings.ts`); a family
   links through to the Games page filtered to it. Both the Games and
   repertoire tables use the shared `useTableSort` hook + `SortableTh`
   header for click-to-sort columns.

   The repertoire is **split by the color the player had**, and
   within each color **split again into chosen vs faced** — the
   systems the player picked first, then a "What you face" table of
   the lines opponents picked against them — mirroring the coach
   prompt. The system (their own first moves) and the line as played
   are shown as columns: without them the tables would list openings
   the opponent chose as if they were the player's own. The two ACPL
   columns are labelled for what they measure — opening-phase and
   whole-game — since only the first is opening advice.
   `groupByFamily` partitions rows by `faced` *before* rolling up,
   per the rule in [06-coach.md](06-coach.md): the chosen partition
   by `(color, system)`, the faced partition by `(color, name root)`;
   two colors of one family never merge.

   A family row drills through to the Games page carrying its member
   openings — one `opening=ECO|name` URL param per rolled-up row —
   plus `color` (faced rows add a display-only `faced=true`, which
   the filter chip renders as "faced as white"), and the Games
   filter matches games by their classified opening against that
   list. Matching instead by
   re-deriving the player's system from each game's moves only ever
   matched the family's *representative* line, so transposed games
   silently dropped out and a family reporting 8 games drilled
   through to 4. The member list is frozen at click time, so the
   drill-through shows exactly the games the row counted. Legacy
   links still work: a `system` param falls back to the exact
   system match, a bare `family` to the name-root match — which
   respects `color` when present. The ACPL-by-phase chart shows the
   move count behind each bar and renders "no endgame moves" where the
   phase has none, rather than a zero bar that reads as flawless play.
   Two highlight sections — **Brilliant moves** and **Blunders** —
   render `GET /players/{u}/highlights` (fetched with the same
   `since`/`time_class` as report and openings). Each row carries the
   citation identity (date, opponent, color, the move as
   `26...Nb6`-style SAN, result) plus the number that matters: the
   player-POV eval after the move for brilliancies (folded from the
   white-POV fields by `color`, mates rendered as `#N`), centipawn
   loss for blunders. Every row links to `/games/{id}?ply={ply}` —
   the Game page's ply deep link — so the student lands on the exact
   position. Brilliancies are rare and render in full; the blunders
   table caps its initial render (top rows by recency) behind a
   "show all N" toggle so an all-time window cannot flood the page.

   Time-window (all-time / 30d / 90d / 6mo / 1yr) and time-control
   (per class; never mixed across controls unless "All classes" is
   picked) filters scope the whole page: games-derived stats are
   filtered client-side, while report, openings, and highlights are
   re-fetched with the `since` window and `time_class`. A first
   visit defaults to the last 6 months of rapid; the selection then
   persists in localStorage (`statsFilterStorage.ts`) as one
   selection shared with the Coach page, so navigating away — into
   a game, say — and back keeps the chosen scope. A picked time
   control absent from the current window (default rapid included,
   for a player with no rapid games) falls back to the most-played.
   Charts are custom SVG components — no chart library.
5. **Coach** — `POST /coach` with the agent chosen in Settings;
   renders the advice (markdown) and the generated prompt with a copy
   button (the manual-use fallback). The same time-window and
   time-control controls the Dashboard uses scope the request, so the
   advice covers the period the student is looking at rather than
   every game they have ever played. The page reads `games_analyzed`
   and `games_in_scope` off `GET /report` for the same filters —
   server-truth counts, never recomputed client-side (`coverageGap` in
   `coachCoverage.ts`) — and shows one line: a plain "N games
   analyzed" once coverage is full (or `games_in_scope` is `null`,
   meaning no scope info), or, while `games_analyzed < games_in_scope`,
   a warning ("N of M games in this window are analyzed — advice will
   only cover the analyzed games") with an "Analyze the rest" action.
   That action posts the page's current `since`/`time_class` to
   `POST /analyze` and tracks progress with the same SSE hook
   (`useAnalysisProgress`) the Games page's analyze bar uses; a 409
   (a run already active for this player, e.g. started from Games or
   a backfill CLI run) attaches to that progress instead of showing an
   error. Because each run caps at `engine.analyze_limit`, when a run
   finishes **cleanly** the page re-reads the report and, if a gap
   remains, fires another run automatically while the user stays on
   the page; a failed run or a lost progress stream stops the chain
   instead (`shouldChainAfterRun` in `coachCoverage.ts`) — a
   persistently-failing game would otherwise re-fire forever with no
   backoff — and the user resumes manually with "Analyze the rest".
   Leaving the page also stops the chain (the server-side run
   continues regardless). Coverage reaching full clears the warning
   and the plain generate flow stands. Advice is cached server-side per
   (player, agent, window, prompt version): a cached result renders
   immediately, labelled with when it was generated and over how many
   games, with a "Regenerate" action (`refresh: true`) — the same
   user-triggered rule as the in-game Explain button, since both spend
   LLM calls.
6. **Settings** (`/settings`) — manages the two things a player
   configures: the saved players (list from `GET /api/players` + an
   add-a-player form) and the coach LLM (`AgentSelect`, persisted in
   localStorage and read by Coach + the in-game Explain button).

## Stack and structure

- Vite + React + TypeScript; TanStack Query for data fetching and
  cache invalidation (sync/analyze invalidate games + report).
- React Router for the six pages; a small typed API client module
  (`web/src/api.ts`) wraps the generated types and is the only place
  URLs appear.
- A shared `Layout` (`web/src/components/Layout.tsx`) wraps every page
  with the app header — brand, a saved-players switcher (from
  `GET /api/players`), the always-available section tabs (Games /
  Dashboard / Coach, pointing at the current player, remembered in
  localStorage via `currentPlayer.ts`), and a Settings link — so
  navigation lives in one place instead of per-page links.
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
