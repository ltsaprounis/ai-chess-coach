# New Feature Proposal

The live backlog: what to build next, first written after the MultiPV
+ explain-move work and last refreshed 2026-08-03. Each candidate is
grounded in code that exists today, mapped to the components it
touches, and sorted by value against two standing constraints:

- **Engine time is free, LLM calls are not.** Features that run on
  Stockfish or stored data alone can be generous; anything LLM-backed
  must be explicitly user-triggered and cached (the explain-move
  rule, now house policy).
- **The architecture is the product.** Components 1-8 stay decoupled;
  cross-component features get their contracts decided first, doc'd,
  then built per component. A genuinely new concern takes docs/09+.

## Where the app is today

Sync from chess.com → SQLite; Stockfish analysis with per-move
judgments, ACPL by phase, progress SSE; ECO opening classification;
stats dashboard (record, ratings, activity, monthly ACPL and blunder
rate, repertoire split chosen-vs-faced, blunder and brilliancy
highlights that deep-link to the move); an openings explorer that
drills the repertoire as a per-color move tree; persisted window and
time-control filters shared by Dashboard, Openings and Coach; live
MultiPV candidate-lines panel on the game replay; on-demand, cached,
regenerable coach explanations with the engine as an agentic tool;
whole-report coaching advice that states its own analysis coverage; a
per-time-control player profile whose context block the explain and
game-chat prompts embed; and tool-grounded follow-up chat on both a
game and a report. All gates green at 561 backend tests (5 of them
opt-in against a real Stockfish) and 291 frontend.

Two review cycles closed in July 2026 (the coach report rework and
its fix iteration, and the whole-codebase scan), followed by three
designs that shipped: the openings explorer, the player profile and
the coach chat. All of them are in [archive/](archive/README.md);
what they left unbuilt is in
[future-improvements/](future-improvements/) or below, not there.

One review is *not* closed: the outside
[codebase assessment](codebase-assessment-2026-07-30.md) of
2026-07-30. Its P0 (atomic migrations) and most of its P1 are
untouched engineering debt rather than features, so they are not
re-listed here; read it directly before planning a hardening pass.

## Tier 1 — quick wins, zero LLM cost

### 1. Click a candidate line to walk it on the board

The Engine panel shows five lines; the obvious next question is
"show me". Clicking a line should preview its PV on the board,
step-through included, with an easy way back to the game.
Deferred twice already; the panel's row structure was designed
for it.

- Touches: frontend only (`Game.tsx`, `LiveEvalPanel`, board state
  gains a "preview variation" mode distinct from game plies).
- Risk: none to the backend; pure UI state machine.

### 2. Blunder puzzles from your own games

Half-built since this was written: the Dashboard's blunder highlights
(`GET /players/{u}/highlights`, shipped `523d85b`) already select and
rank the positions and deep-link each to its ply. What is missing is
the trainer — hide the answer, take a guess, reveal.

Storage already holds every blunder: position (replayable from
`san_moves` to the ply), the move played, the engine's best move,
and the cost in pawns. That is a puzzle database of the player's
own mistakes — "you played Qc7 here; find the move you missed."
Far more personal than lichess puzzles, and it costs nothing.

- Touches: api (endpoint listing blunder positions across analyzed
  games, e.g. `GET /players/{u}/puzzles`), frontend (a Puzzles page:
  board, guess input via piece drag, reveal + jump-to-game link).
  Optional: accept alternate solutions by checking the guess with
  `pool.eval_lines` — the engine seam already exists.
- Contract: probably one new response model; no domain change if it
  reuses `CriticalPosition` (extend with `ply`/`san_moves` prefix or
  derive FEN server-side).

### 3. Progress-over-time on the dashboard — **shipped**

Delivered by the coach report rework rather than as its own feature:
`PlayerReport.months` carries games, rating, ACPL and blunder rate
per month, and the Dashboard charts the last two. Kept here as the
record of why it was worth doing — "am I getting better?" is the
question the app exists to answer.

## Tier 2 — medium efforts, still engine/data only

### 4. Time-management analysis

chess.com PGNs embed per-move clocks (`%clk`), and `Game.pgn` is
stored verbatim — the data is already in the database, unparsed.
Extract clock series, then: time per move alongside the eval graph,
average time in lost vs won positions, and the flag that matters —
blunders committed under low clock. "Your blunders cluster under
30 seconds" is coaching gold no engine eval alone can give.

- Touches: domain (`MoveEval.clock_seconds: float | None` or a
  parallel structure — contract decision first), ingestion (parse
  clocks during PGN parse), engine (thread through analysis),
  storage (persisted inside the existing JSON columns), frontend.
- Note: reanalysis not required if clocks live on `Game` rather than
  `MoveEval`; deciding where they live is the main design call.

### 5. Opening phase refined by actual book exit

docs/04 explicitly defers this: opening phase = first 10 full moves,
"refined later by the actual book-exit ply". `Opening.ply` (where
the game left book) is classified and stored per game today but
never reaches the engine's phase logic. Wiring it in makes
opening-phase ACPL mean what it says.

- Touches: engine (`EngineOptions` or `analyze_game` gains the
  book-exit ply; phase boundary uses it when present), api (pass it
  from the stored opening at analyze time), docs/04.
- Caveat: changes phase attribution for future analyses; old ones
  keep their stored numbers unless reanalyzed. Acceptable drift, and
  cheaper than it was: `analysis_version` already exists to re-queue
  affected rows.
- The openings explorer
  ([openings-explorer.md](archive/openings-explorer.md))
  wants the same book-exit ply, and is designed against it.

### 6. Lichess as a second game source

Ingestion is stateless fetch-and-parse behind `sync_games`; a
lichess client is the same shape (their API is public, NDJSON).
Doubles the audience for everything downstream.

- Touches: ingestion (a second client + source tag), domain
  (`Game.source` or id namespacing — contract decision), storage
  (migration for the new column), api/frontend (source picker).
- The biggest cost is product, not code: two rating scales, two
  usernames per player.

### 10. Second pass on the openings explorer

The page shipped 2026-07-29 with four extensions recorded and left
unbuilt; they came off the design record when it was archived
([openings-explorer.md](archive/openings-explorer.md), "Deferred"),
and the reasoning for each is still there. In rough value order:

- **Punish metric**: when the *opponent* leaves book first, the eval
  swing over the next few plies. "You get +0.8 out of book and still
  score 40%" points at the middlegame, not at theory. Needs a defined
  window before it can be built.
- **Per-node drill to the Games page**: needs either a by-position
  game filter or sample game ids on nodes; both have costs.
- **Transposition annotation**: "also reached via …", by merging
  node stats on EPD. Path identity stays; this only adds a label.
- **LLM "explain this line"**: the one LLM item here, so
  user-triggered and cached like every other, reusing the explain
  stack.

Touches openings + api + frontend, in that order; none of them is a
domain change.

## Tier 3 — LLM features (user-triggered + cached, always)

### 7. One-click game review

Explain-move works per ply; the natural aggregate is "review this
game": one click, one LLM call, producing a short narrative that
names the 2-3 turning points (largest cp swings — already computed)
with links that jump the board to each. Reuses the entire explain
stack: seeded context, engine tool, SSE streaming, cache keyed
(game, agent), regenerable.

- Touches: coach (a `render_review_prompt` over the game's worst
  moments), api (endpoint + cache table or reuse explanations with
  a sentinel ply), storage (either way), frontend (a Review button
  on the Game page).
- This is the highest-value LLM feature: it turns "analyze" output
  into a story a club player actually reads.

### 8. Ask a follow-up question — **shipped**

Built 2026-07-30 as the coach chat:
[coach-chat.md](archive/coach-chat.md) is the design
record, and the contracts live in the component docs. One chat
backbone, two scopes (game-anchored on the Game page, report-scoped
on the Coach page); `CoachProvider.chat` is stateless with an
opaque resume token, the agent queries the student's games and
Stockfish through a read-only injected toolkit, and transcripts
persist so reopening a thread bills nothing. Kept here as the
record of the original sizing call ("bigger than it looks" — it
was) and of what settled it: the stored transcript as the single
source of truth, warm provider sessions demoted to a cache.

### 9. Finish the planned providers

docs/06 already commits to `anthropic` (API key) and
`azure-foundry`. Each is one class behind `create_provider`, plus
an explain tool loop. Unblocks users without a Claude Code login.

A third candidate now has a design record: a **local / OpenAI-
compatible provider**
([local-llm-provider.md](future-improvements/local-llm-provider.md),
researched 2026-07-31), which takes the app fully offline and needs
no subscription at all. One class speaking the OpenAI wire protocol
covers Ollama, llama.cpp, LM Studio and vLLM, plus hosted
aggregators, on a config change. It is the largest accessibility win
of the three and the most work: unlike the other two it borrows no
agentic loop from a vendor SDK and must own one outright, against
models measurably worse at tool calling. The doc also settles the
question of chess-specialised models — there are many on Hugging
Face and none can serve the seam — and finds two things worth
taking regardless: Maia-2 for human-findability, and a python-chess
faithfulness verifier over generated text.

## Housekeeping worth scheduling

- **Prompt-version the explanation cache**: cached explanations keep
  the style of the prompt that made them (bit us this week). A
  `prompt_version` column lets the UI badge stale ones instead of
  relying on the user to regenerate. Designed, not scheduled —
  [prompt-version-fingerprint.md](future-improvements/prompt-version-fingerprint.md)
  bundles it with making both prompt versions content fingerprints.
- **Eval cache keyed (fen, depth, multipv)**: stepping back and forth
  through a game re-searches identical positions; an LRU (memory or
  table) makes replay instant and cuts explain seeding cost.
- **Reserved live-eval worker**: one long analysis run currently
  starves the live panel (workers=2). Reserve one worker for
  interactive streams, or make it a config choice.
- **Coach chat follow-ups**: the small items its design record left
  open ([coach-chat.md](archive/coach-chat.md), "Open questions and
  risks"). Cheapest first: memo the report-scope seed per thread (it
  currently rebuilds the whole `PlayerReport` on every message, which
  is correct but real latency on a 1,200-game archive); abort the old
  stream when the Game page's ply anchor changes mid-reply; close the
  in-flight slot a vanished client can leak between the in-flight
  mark and the SSE generator's first iteration. Two questions are
  still unmeasured rather than unbuilt: how often a live provider
  actually resumes rather than replaying, and whether the
  ~40-message cap is anywhere near where context stops helping.
- **Docker packaging**: the install is five prerequisites deep and
  the C++ engine build is the step most likely to fail on someone
  else's machine. One `docker compose up` fixes that, and settles how
  a container authenticates when both providers ride a local CLI
  login rather than a key. Designed, not scheduled —
  [docker-packaging.md](future-improvements/docker-packaging.md) also
  closes scan finding 14, whose revisit was gated on the licence
  decision that has since been made.

## Recommended order

1. **Clickable candidate lines** (1) — days, pure frontend, completes
   the panel users already stare at.
2. **Blunder puzzles** (2) — the best value-per-effort in the list;
   makes the app a trainer, not just a reporter, and the highlights
   work already did its selection half.
3. **One-click game review** (7) — the flagship coaching moment;
   every piece of infrastructure it needs already exists.

Then 4 (time management) as the next substantial analysis feature.
5, 6, 9, 10 as demand pulls; 10 in particular is a page that already
has users, so its items are cheap to justify one at a time.

What remains in [future-improvements/](future-improvements/) sits
outside this ordering, waiting on a trigger rather than on priority.
The **prompt-version fingerprint** waits on the next material change
to the explain prompt, so the key change and its cache invalidation
land together. The **local LLM provider** (2026-07-31) is research
rather than a scheduled build: it is sized as the largest item in
item 9, and its own doc lists four cheap in-repo measurements that
should settle before anyone starts. One of them,
[the llama-cpp-python spike](spike-reports/local-llm-provider.md),
has since run. The **normalized game model** is there as a rejected
alternative, kept so the rejection does not have to be re-argued.

**Docker packaging** (2026-07-31) sits outside the ordering for a
different reason: its trigger is publishing the repo, not a feature
anyone is waiting on. It touches no component contract as designed,
and one assumption — that the CLI bundled inside the Claude SDK
wheel can complete an interactive login — should be tested before
the first slice.
