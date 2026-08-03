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
   paging. A row's date opens its Game page in a new tab — like every
   game link off a list page — so filters, sort, page, and any row
   selection survive the detour. A "Sync new games" button pulls
   fresh games for the player
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
   shows a summary strip (average loss in pawns per move, blunder
   and mistake counts,
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
   for stale ones. Below the explanation, an "Ask a follow-up"
   affordance opens the shared `ChatPanel` (see Stack and
   structure) on a game-scoped thread anchored to the selected ply:
   the newest existing thread matching (game, ply, agent) reopens
   from its stored transcript at no LLM cost; otherwise the thread
   is created on first send. Replies render as markdown; their
   game links open in new tabs like every other advice anchor.
4. **Dashboard** — the player's stats hub: summary tiles (record,
   win rate by color, current rating per time class, average loss,
   blunder
   rate), rating-over-time and monthly-activity charts from
   `GET /players/{u}/games` (paged fetch), avg-loss-by-phase and
   judgment charts plus the termination breakdown and the monthly
   avg-loss/blunder-rate trend from `GET /players/{u}/report`, and the
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
   the opponent chose as if they were the player's own. The two loss
   columns are labelled for what they measure — opening-phase and
   whole-game — since only the first is opening advice.
   `groupByFamily` partitions rows by `faced` *before* rolling up,
   per the rule in [06-coach.md](06-coach.md): the chosen partition
   by `(color, system)`, the faced partition by `(color, name root)`;
   two colors of one family never merge.

   A family row drills through to the Games page (a new tab, like
   the highlight rows — the link is URL-param-complete, so the tab
   stands alone) carrying its member
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
   respects `color` when present. The avg-loss-by-phase chart shows the
   move count behind each bar and renders "no endgame moves" where the
   phase has none, rather than a zero bar that reads as flawless play.
   Two highlight sections — **Brilliant moves** and **Blunders** —
   render `GET /players/{u}/highlights` (fetched with the same
   `since`/`time_class` as report and openings). Each row carries the
   citation identity (date, opponent, color, the move as
   `26...Nb6`-style SAN, result) plus the number that matters: the
   player-POV eval after the move for brilliancies (folded from the
   white-POV fields by `color`, mates rendered as `#N`), the loss in
   pawns for blunders. Every row links to `/games/{id}?ply={ply}` —
   the Game page's ply deep link — in a new tab (`target="_blank"`,
   like the Coach page's advice anchors), so the student lands on
   the exact position and the dashboard keeps its scroll and pager
   state. The Recurring-mistakes example links open the same way.
   Both tables page at 20 rows through a classic numbered
   pager (`components/Pagination.tsx`; ‹ 1 … 4 5 6 … N ›) — rows are
   newest-first, so higher pages reach older moves. The pager hides
   itself when one page fits (the usual case for brilliancies), and
   a filter or player change snaps both tables back to page one.

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
5. **Openings explorer** (`/players/{username}/openings`) — the
   per-color repertoire move tree from `GET /players/{u}/openings/
   tree` (docs/archive/openings-explorer.md), one fetch
   per (color, filters); every drill is client-side. A color toggle
   (White/Black) plus the Dashboard's time-window/time-control
   filters (`useStatsFilters`, same shared localStorage selection)
   scope the fetch, alongside a "Show one-off lines" control that
   flips the request's `min_games` between 2 (default, omitted) and
   1 — reflected in the query key so the two results cache
   separately. A coverage line ("N of M games in this window are
   analyzed") sits above a "worst lines" strip: the top player-level
   nodes by impact (games x avg loss), each clickable to jump the
   drill state straight to that line. Below that, a breadcrumb (root
   labelled "Start") and a children table — move, opening name
   (inherited from the current node when a child or book move
   carries none of its own), games, score, avg eval (player POV,
   signed pawns), avg loss, a book badge, and exits when nonzero;
   unplayed book continuations render as greyed "still to learn"
   rows in the same table, never duplicating a played move. The
   table's games/score/avg-eval/avg-loss columns sort via the shared
   `useTableSort`/`SortableTh`, defaulting to the payload's
   games-desc order. Levels are labelled "Your move"/"Their move"
   from ply parity (`isPlayerPly`/`levelLabel`). A board panel
   (`react-chessboard`) replays the current path client-side with
   `chess.js` — the tree carries no FENs — oriented to the selected
   color, with the Game page's live-eval toggle (`useLiveEval` +
   `LiveEvalPanel`) for a user-triggered deeper look. The color and
   drill path are encoded in the URL (`?color=white&path=e4,c5,Nf3`,
   each SAN `encodeURIComponent`-escaped since SAN can contain `+`/
   `#`) via `replaceState`-style navigation (no history spam); on
   load the path is validated against the fetched tree and falls
   back to the root if it no longer resolves (a stale link, a color
   swap, or a line pruned by `min_games`). All the path/ranking/
   formatting logic is pure and unit-tested in `repertoireTree.ts`
   (kept apart from the Dashboard's flat-table `openings.ts`).
6. **Coach** — the time-window/time-control filters sit at the top,
   directly under the heading, and scope everything below them
   including the profile. Beneath them, a **player-profile card**
   (`components/ProfileCard.tsx`) reads
   `GET /players/{u}/profile` (docs/06-coach.md "Player profile")
   with the page's `since`/`time_class`.

   The card lives inside a **collapsed `<details>` disclosure**: the
   profile is reference material about who the student is, not the
   thing they came to the page to do — the advice flow is — and at
   full height it cost the page its whole first screen. The summary
   line carries the headline (scope, games, whether a narrative
   exists) so the disclosure is worth opening or skipping without
   guessing.

   It shows the free facts `build_profile` distills — rating and
   games per time class, each tile carrying the **dated peak**, and
   how far below it the current rating sits *only when the student
   is not improving* (`isImproving`, mirroring the backend property
   pydantic cannot serialize): "95 below peak" beside a trajectory
   reading "+443 over the year" is the misread the profile rework
   exists to remove, and the card must not be what reintroduces it.
   Then a **Trajectory** block — deltas over 30/90/180/365 days and
   the largest drawdown with its recovery, labelled as covering the
   whole archive rather than the level window every other figure
   here uses — overall average loss plus blunder share,
   the **recent-form windows** (last 30/90 days against the whole
   span, rendered only when there is more than one row to compare),
   a **Milestones** table, a **Splits** table (docs/06-coach.md,
   "Reading a comparison": each row's verdict and never its
   arithmetic, unmeasurable splits dropped rather than labelled, and
   after-a-loss and by-color live *here* rather than in Milestones so
   the raw gap never sits above the judgement of it), the top chosen
   systems and faced problem lines per color with each family's
   **opening loss**, and
   recurring error patterns with counts and a deep-linked example
   (`/games/{id}?ply=`). A coverage line states both denominators —
   "ratings, records and repertoire cover all N games; quality
   figures cover the M analyzed" — since the two genuinely differ on
   a partly-analyzed archive.

   The staleness hint has two independent bases, and the card takes
   `currentPromptVersion` for the second: the narrative's own
   `games_covered` against the live count for its scope, and its
   `prompt_version` against the one the backend would generate under
   now. The second matters most exactly when the count has *not*
   moved — a narrative written under an older template can contradict
   the facts rendered above it — and its wording differs, since "you
   now have N games" would be a lie in that case.

   The Milestones table is the volume layer's own findings
   (docs/06-coach.md, "Milestones"): the **biggest upset** deep-linked
   to its game and read gap-first (chess.com pairs by rating, so the
   highest-rated opponent beaten is structurally the student's own
   peak — absent entirely on an archive with no win over a
   meaningfully higher-rated player, which is common), the current and
   longest runs, the opposition split, and how losses end. After-a-loss
   and the White/Black split are deliberately *not* here — they are
   Splits rows, with verdicts. Every row is built individually and the empty ones
   dropped — a student with no win yet has no "Best win" row rather
   than a dash — and the whole section disappears when none has data.
   It says out loud that it covers every game in scope, analyzed or
   not, since the coverage line above it is about the quality figures.

   Once one exists, the stored narrative renders as markdown under a
   "The coach's read on {username} in {scope}" header labelled with
   its agent (resolved against the same `/coach/agents` roster the
   page fetches for its picker, falling back to the raw id) and
   generation date. It is written to the coach in the third person,
   and the card says so, along with the fact that it covers the time
   control across all time — because the window filter re-scopes the
   figures above it but deliberately not the narrative.

   A Generate/Regenerate button posts `/players/{u}/profile` with the
   page's selected agent **and its time control** — the same
   user-triggered-only rule as advice and the in-game Explain button
   — with a pending state and, on success, an in-place update
   (`queryClient.setQueryData`, no refetch). One narrative per time
   control means switching the filter can reveal an ungenerated
   scope, which shows the Generate call-to-action rather than another
   control's read.

   The staleness hint compares the stored narrative's `games_covered`
   against the response's `narrative_games_now` — the live count for
   the *narrative's* scope, not the card's, which under a window
   filter are different spans and would otherwise read stale always.
   Two empty states: no narrative yet shows the facts with the
   Generate call-to-action; zero analyzed games in scope shows a
   short note instead, pointing at both the games page and the
   filters, with no Generate action since the POST would 409.

   Pure formatting/partitioning helpers (`isProfileStale`,
   `scopeLabel`, `hasPartialCoverage`, `blunderShare`,
   `openingsFor`, `scorePercent`, `formatGameDate`, `peakLabel`,
   `peakGap`, `streakLabel`, `terminationShares`,
   `errorExampleLabel`/`errorExampleHref`) live in
   `playerProfile.ts`, unit-tested apart from the component,
   mirroring `highlights.ts` beside the Dashboard.

   `POST /coach` with the agent chosen in Settings;
   renders the advice (markdown) and the generated prompt with a copy
   button (the manual-use fallback). Advice anchors open in a new tab
   (`target="_blank"`): the advice carries app-relative game links
   (06-coach.md "Game links"), and the panel is mutation state — a
   same-tab navigation into a game would blank the advice on return
   until the next "Get advice". The same time-window and
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
   LLM calls. Below the advice, the shared `ChatPanel` mounts on a
   report-scoped thread carrying the page's current window and
   time-control filters: the newest matching thread reopens from its
   stored transcript, a "New chat" action starts over, and at the
   server's message cap (409) the panel directs the student to a new
   thread. Tool events render as the same transient progress lines
   the Explain panel shows.
7. **Settings** (`/settings`) — manages the two things a player
   configures: the saved players (list from `GET /api/players` + an
   add-a-player form) and the coach LLM (`AgentSelect`, persisted in
   localStorage and read by Coach + the in-game Explain button).

## Stack and structure

- Vite + React + TypeScript; TanStack Query for data fetching and
  cache invalidation (sync/analyze invalidate games + report).
- React Router for the seven pages; a small typed API client module
  (`web/src/api.ts`) wraps the generated types and is the only place
  URLs appear.
- A shared `Layout` (`web/src/components/Layout.tsx`) wraps every page
  with the app header — brand, the always-available section tabs
  (Games / Dashboard / Openings / Coach, pointing at the current
  player, remembered in localStorage via `currentPlayer.ts`), and a
  Settings link — so navigation lives in one place instead of
  per-page links. Switching players happens in Settings, which owns
  the `GET /api/players` roster; a switcher in the header itself is
  proposed, not built
  ([codebase-assessment-2026-07-30.md](codebase-assessment-2026-07-30.md),
  F12 and P2.1).
- The coach chat is one shared `ChatPanel` component
  (`components/ChatPanel.tsx`) with two mounts (Game page,
  game-scoped; Coach page, report-scoped) driven by a `useChat`
  hook. Chat streams `POST /chat/threads/{id}/messages` with the
  same fetch-based SSE consumption the explain hook uses — the SSE
  block parser is extracted from `useExplain.ts` into a shared
  module rather than duplicated — because native `EventSource`
  cannot POST a body or surface pre-stream JSON errors. Chat SSE
  payload types are hand-declared mirroring coach `ChatEvent`, per
  the standing SSE-types rule. The agent comes from the Settings
  `AgentSelect`; threads are pinned to their agent at creation, so
  changing the selection starts a new thread rather than resuming
  an old one under a different provider.
- Colors are CSS custom properties defined once in `index.css`
  (light + dark via `prefers-color-scheme`); the SVG charts read the
  same tokens through `components/chartTheme.ts`.
- **Units: every loss figure the user sees is in pawns**, never
  centipawns and never labelled "ACPL" — one scale with one name, the
  same rule the backend follows and for the same reasons
  (docs/06-coach.md, "Units": the audience is chess.com players, the
  eval bar is pawns, and the coach's prose sits on these same
  screens). The API speaks centipawns; `web/src/units.ts` is the only
  place that converts, and it converts to a *string* — the division
  itself is not exported, so no pawn-scale number is ever in flight
  for a second consumer to divide again. `formatPawns` (aggregates,
  two decimals) and `formatPawnLoss` (one move, one decimal) mirror
  the backend's `_pawns_or_na` and `format_cp_loss` exactly, decimals
  included, so one figure reads the same in a table cell as in the
  advice paragraph under it. Chart *values* stay in centipawns —
  heights are ratios, so the scale cancels — and both chart
  components take a `formatValue` applied to the axis ticks and the
  tooltip alike.
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
