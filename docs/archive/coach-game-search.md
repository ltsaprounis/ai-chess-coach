# Coach game search: predicate scans and honest coverage

Status: designed and built 2026-08-03, same day. The contracts are
authoritative in docs 03/06/07 and `domain`; this record keeps the
reasoning and the measurements. Event definitions were validated
against the production archive before the build
([spike report](../spike-reports/coach-game-search-events.md), which
stays in spike-reports as live evidence for the deferred cache
decision), and the built toolkit was smoke-tested end to end against
a copy of the production DB: the target dialogue's queen sacrifice
surfaces by pinpoint, sweep and chain scans; its two near-misses
stay silent. What the build left open moved to
[NEW-FEATURE-PROPOSAL.md](../NEW-FEATURE-PROPOSAL.md) housekeeping
before archiving (see "Decisions", updated with resolutions).

Written against a target dialogue ("find games where I sacrificed my
rook or queen and won decisively", then "what about the game with
ousaama78 on the 7th of March 2026?") that the chat agent could not
answer well. Three candidate designs were drafted and adversarially
reviewed; this doc records the winning hybrid, extended with event
composition after the spike showed the definitions hold on real
data. No migrations, no HTTP surface change, no LLM cost beyond
modest tool-result tokens.

## The ask

The target dialogue decomposes into four capabilities, two of which
the chat agent lacks entirely:

1. **Tactical discovery.** "Games where I sacrifice my rook or queen"
   is a question about move content, not metadata. No current tool
   filters on anything below the game row: the agent's only path is
   paging `find_games(result="win")` and reading full games one at a
   time, which samples a dozen of 489 wins at real token cost.
2. **Honest coverage.** The coach in the dialogue says "I only read a
   dozen of your 489 wins". `find_games` returns rows but no total,
   so the agent cannot know the denominator it is being honest about.
3. **Pinpoint lookup.** Opponent plus date already works through
   `find_games` filters, but opponent matching is exact and the model
   must guess how a remembered "7th of March" maps to UTC epochs.
4. **Engine-grounded verification.** "The engine confirms it was
   mate-in-5 from the moment you played Qxg7" needs per-ply evals at
   a named moment and, for narrating the forced line, a position to
   hand `analyze_position`. Both exist in principle; neither is
   reachable in practice (see the rendering gap below).

## What the chat agent can and cannot do today

The roster ([06-coach.md](../06-coach.md), "Chat"): `find_games`,
`get_game`, `get_opening_stats`, `compare_groups`, and
`analyze_position`, all read-only, pre-scoped to the thread's player,
under `_CHAT_MAX_TURNS = 8` tool calls per message
([providers.py:86](../../backend/src/chess_coach/coach/providers.py)).

Verified gaps, each with the line that causes it:

- **A brilliant move is invisible in the move sheet.**
  `_render_move_sheet` annotates only `judgment != "best"`
  ([providers.py:1273](../../backend/src/chess_coach/coach/providers.py)).
  A sound queen sacrifice is by definition an engine-best move, so
  the one move the student is asking about renders as bare SAN with
  no eval and no mate score. The agent cannot even *find* the
  sacrifice ply in a game it has already fetched.
- **No total counts.** `find_games` returns at most 25 rows
  (`_FIND_GAMES_LIMIT_CAP`,
  [chat.py:61](../../backend/src/chess_coach/api/chat.py)) and
  nothing else. The 489 in "12 of your 489 wins" is unknowable.
- **Opponent match is exact.**
  [games.py:142-144](../../backend/src/chess_coach/storage/games.py)
  compares `LOWER(g.opponent) = LOWER(?)`. A half-remembered
  "ousama78" finds nothing.
- **No position at an arbitrary ply.** `analyze_position` takes a
  FEN, but no tool ever returns one, so fresh engine lines are only
  reachable from a game-scoped thread's own seeded anchor.
- **The `analyzed` flag is dropped.** `GameSummary.analyzed` exists
  ([domain.py:803](../../backend/src/chess_coach/domain.py)) but the
  row renderer omits it, so the agent cannot tell which games it
  could verify with evals versus which it would be guessing about.

## What the repo already knows

The Dashboard's brilliancy detector is most of the hard part, built
and tested:
[highlights.py](../../backend/src/chess_coach/coach/highlights.py)
replays each analyzed game with python-chess and flags sound
sacrifices by four criteria: engine-best move, a real sacrifice per a
recursive static-exchange evaluation (SEE) with legal-move generation
(pins for free), **not already winning beforehand**, and still sound
afterwards. That third criterion is exactly the distinction the
target dialogue turns on: 27...Qxe1+ while already up a rook is
cashing in, not sacrificing.

But it is Dashboard-only: reachable through
`GET /players/{u}/highlights`, not from chat, computed with fixed
brilliancy thresholds, and it does not record *which piece* was
sacrificed, so "rook or queen" cannot be asked of it. The design
below reuses its SEE machinery in-component rather than rebuilding
detection from scratch.

## Recommended design

Three parts. Part 1 is the new capability; parts 2 and 3 are small
fixes that the pinpoint-lookup turn needs and every other search
benefits from.

### 1. `scan_games`: an on-demand server-side predicate scan

One new chat tool. The model composes one to three **named events
from a fixed server-side library** into an ordered match sequence,
plus the same metadata filters `find_games` takes; the server
fetches matching games, replays each with python-chess against its
stored evals when it has them, and returns capped match rows.
Recall-first extends to analysis coverage: events computable from
the moves alone (`sacrifice`, `delivered_mate`, `castled`) scan
**every** stored game, with eval-backed annotations marked
unverified on unanalyzed games; only the events that read stored
evals (`comeback`, `eval_swing`) are restricted to analyzed games.
An unanalyzed remembered gem is therefore *found* either way; only
its soundness check degrades, into an offer to analyze.
No LLM tokens are spent scanning, no arbitrary code runs against the
archive, and the vocabulary is closed by construction.

Schema exposed to the model. A scan takes an ordered sequence of one
or more event conditions; a single event is the common case, and a
sequence expresses "A, then B within N plies, in the same game":

- `match`: array of 1-3 event conditions, each `{event, ...params,
  within_plies?}`, where `within_plies` bounds the gap to the
  previous step's match ply (default: rest of the game). Events:
  - `sacrifice`, with `piece`: enum `queen | rook | minor`, default
    `minor` (the piece given up, resolved from the SEE target;
    `rook` means rook or queen, `minor` means any piece) and
    `sound_only`: bool, default false (keep matches where the
    player's eval after the move is >= 0, mate folded)
  - `eval_swing`, with `min_swing_pawns`: number >= 1.0, default
    3.0, and `direction`: `gained | lost`, default `gained`
  - `comeback` (win after standing >= 3 pawns worse; match ply is
    the worst moment)
  - `delivered_mate` (win with the final replayed board in
    checkmate)
  - `castled`, with `side`: `short | long | any`, default `any`
- shared filters, identical semantics to `find_games`: `opponent`,
  `opening`, `result`, `time_class`, `since`, `until`
- `limit`: matches returned, default 10, server cap 25

Every event is defined by exact computation over the replayed board
and stored evals; there is no fuzzy event. A king-hunt event was
considered and excluded: no definition tried so far survives
contact with real games, and a bad event is worse than no event
(see the signal-algebra rejection below). The one event that needed
real design is `sacrifice`; it is highlights' `_is_real_sacrifice`
parameterized. This is a *retrieval* event, so its stance is
recall-first: gates exist only to kill spam classes, and everything
judgment-shaped is an annotation the model reads, never a filter
that hides games. Both measurement passes are in the
[spike report](../spike-reports/coach-game-search-events.md).
The gates:

- **The move must escalate the offer.** The opponent's best SEE
  gain must rise by >= 2 points across the move (before-the-move
  gain via a null move; a mover in check counts as gain 0). This
  dedups by construction: while a queen hangs untaken, later moves
  cannot re-fire, so one loose-piece middlegame cannot flood the
  match cap. Unlike a binary "nothing already hanging" gate, it
  still admits a real sacrifice played while a pawn or minor was
  en prise. Measured cost vs the binary form: readmits 4
  blunder-shaped events (correctly annotated unsound), drops 7
  marginal net-2 ones.
- **Piece tier, not net points.** "Rook sacrifice" means the piece,
  not the arithmetic: an exchange sac (rook for knight) nets 2
  points and a queen-for-rook sac nets 4, and a net-points threshold
  silently excludes both from "rook or queen". The `piece` parameter
  resolves from what stood en prise; the detail line still reports
  net points so queen-for-rook reads "gave the queen for a rook",
  never "gave up 9".
- **Promotions are not sacrifices.** To SEE, `a8=Q` is a queen en
  prise for net 4+; on the real archive roughly half the "sound
  queen sacrifices" were endgame promotions. Excluded outright.

The annotations, carried on every match: `realizes` (the ply count
within which the player's material actually dropped >= 2 below its
pre-move level or mate was delivered, or `declined` when it never
did; moves-only, so always present), `sound` (eval after >= 0),
`balanced_before` (eval before <= +2.0), and the eval-before/after
pair; the eval-backed three render as `unverified (unanalyzed)` on
games without stored analysis. Realization was a 6-ply gate
in the first design pass; the second measurement pass showed that
filter hides 14 of 64 candidate games on the real archive,
including a sound declined exchange sac from an equal position and
two winning swindle sacrifices that realized late, which is
exactly the good stuff a coach conversation wants. Retrieval
surfaces them; the annotations tell the model which is which.
`sound_only` remains available as an opt-in filter parameter,
default off.

Each match returns: `GameSummary` fields, ply, SAN, `fen_before`
(feeds `analyze_position` directly), and a detail line carrying net
points, **eval before** and eval after in player POV (pawns or
mate-in-N). Eval-before is what lets the model itself separate a real
sacrifice from cashing in while winning, instead of hoping it infers
that from a digit stream.

Every result opens with structural denominators, computed by the
server from `game_record` over the same filters:

    Scanned all 489 games matching the filters (177 without
    analysis: soundness unverified; scan truncated: no).

For eval-reading events the unanalyzed games are skipped and the
preamble says so instead. Coverage honesty stops being something
the model estimates and becomes something it reads. This is
strictly better than the target dialogue: not "a dozen of your 489
wins" but every one, with the unverifiable remainder named.

Composition is what makes a small exact vocabulary expressive. The
flagship question is a chain, stated structurally instead of hoped
from prompt discipline: `sacrifice(piece=rook)` then
`delivered_mate within 12`. "Swindled a win" is `eval_swing(lost)`
then `comeback`. "Castled long and threw the queen in" is
`castled(long)` then `sacrifice(queen)`, and on the target game that
chain matches the real plies (19.O-O-O at ply 37, 20.Qxg7+ at ply
39). The detectors stay server-side code; the model only sequences
them, so a wrong composition can return an empty result but never a
wrong event. Replay from the standard start is guaranteed because
ingestion drops `SetUp`/`FEN` games
([normalize.py:125](../../backend/src/chess_coach/ingestion/normalize.py)).

**Validated against real data.** The three games the target dialogue
names are all in the production archive, analyzed, and a prototype
of these exact definitions (reusing highlights' SEE helpers
verbatim) behaves correctly on each: the ousaama78 game fires
`sacrifice` exactly once, at ply 39 `Qxg7+` (queen, net 6, eval
+9.2 before, mate-in-5 after, sound, realizes in 1); the
abd_ennouer game fires **no** sacrifice for 27...Qxe1+, because SEE
sees that answering the check cannot win the queen, while
`comeback` fires at its true story (worst stored eval −14.4 in a
won game); the rinuf combination fires nothing at rook-or-queen
tier. The archive sweep: 605 analyzed wins in ~4 s; 116 games carry
a raw rook+ offer, the escalation gate keeps 60, and the
annotations split those into the sound, balanced and realized
subsets the model triages. (The spike ran over analyzed wins; the
later all-games decision widens coverage, not definitions.)
Numbers, conditions, and what was not measured live in the
[spike report](../spike-reports/coach-game-search-events.md).

Caps and cost: match cap 25; candidate cap 800 games per call,
newest first, with a `truncated` flag telling the model to narrow the
window rather than page. The candidate cap is what makes all-games
scanning affordable: an unfiltered archive (8,200 games) never
costs more than 800 replays. The sacrifice event costs roughly
3-10 ms per game of replay plus SEE in a threadpool (the
`/highlights` endpoint already set the precedent for this latency
class); a capped scan runs a few seconds while the SSE tool event
shows progress. Tool-result size: ~450-600 tokens for a
10-match scan, ~120 for a pinpoint one. No cache in v1: at ~2k games
a repeat scan costs seconds, and determinism makes it identical; log
scan wall time so a later LRU decision is data-driven.

### 2. Retrieval fixes

- **Opponent becomes substring match.** Flip
  [games.py:142-144](../../backend/src/chess_coach/storage/games.py)
  to the `LIKE`-with-escape pattern `opening_name_like` already
  uses. Verified safe: the chat toolkit is the only consumer of
  `GameFilters.opponent`; the HTTP surface never sets it. Typo
  tolerance directly serves the "ousaama78" turn, where the
  remembered username is itself suspect.
- **`find_games` returns a page, not a bare list.** New domain type
  `GameSearchPage`: `games`, `total`, `offset`. The total comes from
  the existing `game_record` over the same filters (it shares
  `_game_filter_clauses`, so counts cannot drift from rows).
  Deliberately the total only, never the W/D/L split: a self-served
  win-rate would reopen the back door `compare_groups` exists to
  close. The renderer gains a header ("Matched 489 games; showing
  1-25, newest first") and an `unanalyzed` marker per row from the
  `GameSummary.analyzed` field the renderer currently drops. An
  `offset` parameter allows deliberate paging under the total.
- **`get_game` annotation widening.** The move-sheet rule becomes:
  annotate when `judgment != "best"` OR the move is a capture OR the
  stored eval is a mate score. Best-move captures and mate-scored
  positions get a bare eval annotation with no judgment word. This
  is the fix that makes a brilliant Qxg7 findable at all.
- **`get_game` grows an optional `ply` parameter.** When passed, a
  position block is appended: FEN before and after (replayed from
  `san_moves`), the played move, judgment, loss in pawns, engine
  best move, eval before and after. This gives every moment in every
  stored game a road to `analyze_position`, which is what "narrate
  the forced mate" needs when the stored single-PV analysis holds
  only the eval, not the line.

### 3. Prompt additions

Three bullets in `_CHAT_INSTRUCTIONS`
([prompt.py:1825](../../backend/src/chess_coach/coach/prompt.py)):

- **Coverage honesty, phrased around tool output.** When answering
  from search or scan results, state the result's own denominators
  (scanned, matched, unanalyzed skipped) and offer to widen. Matches
  are examples, never tendencies; `compare_groups` remains the only
  tendency tool.
- **Dates.** Game times are UTC epoch seconds; when the student
  names a calendar date, widen the window by a day on each side. A
  late-evening game in the student's timezone lands on the next UTC
  day, and "no such game" for a game the student vividly remembers
  is the worst answer available.
- **Event fit.** When no event or chain matches the question ("games
  where I slowly strangled a knight"), say so and fall back to
  metadata search plus reading, rather than stretching the nearest
  event.

## Rejected alternatives

- **A precomputed tactical feature index** (new `game_features`
  table written at analysis-save time, backfilled over the ~1,200
  existing analyses, exposed as `find_games` filters). Best query
  latency and per-turn token cost, and its detection vocabulary
  (realization tracking, balanced-before flags) sharpened the event
  design above. Rejected as v1 for its bill and its failure modes:
  migration + backfill route + script + a domain package conversion
  (~4.5 days); every threshold frozen into stored rows where
  `scan_games` gets parameterization free at query time; and a new
  staleness class where a version bump or re-analysis (a real event:
  the whole archive was re-analyzed 2026-07-28) empties tactical
  search until a manual backfill heals it. At ~2k games, on-demand
  scanning wins; at ~50k the trade flips, and the tool contract
  survives that switch (the predicate library becomes the backfill's
  extractor, results become a cache table).
- **A generic signal algebra** (exposing raw per-ply signals such as
  material balance, eval, checks and king position, with thresholds
  and boolean combinators, so the model can compose arbitrary
  conditions). More generic than event composition, and rejected for
  where the wrongness would live: the precision is in the detectors,
  not the combinators. The sacrifice event needed three gates and
  four annotations (escalation, piece tier, promotion exclusion;
  realizes/sound/balanced/evals), most of them discovered or
  corrected only by running against the real archive twice; a model
  composing "material drops and stays down while eval holds" from
  primitives rebuilds the ungated version per query, badly. The
  failure modes also differ in kind: a missing named event yields an
  honest refusal, a miscomposed algebra yields a confident wrong
  answer. And a recursive expression-tree schema costs several
  hundred tokens on every chat message of every thread. Event
  composition (a sequence of named, gated events) keeps most of the
  expressiveness with none of the open semantics.
- **LLM-side detection over rendered digests** (a batch tool
  rendering per-ply eval traces and material-event lines for the
  model to read). Rejected on three structural grounds: base rates
  (sound queen sacs are rare, so reading 16-48 recent wins of 489
  usually finds nothing and the flagship turn ends in a shrug);
  reliability (white-POV traces mean sign-flipping every Black game
  in-head, and declined sacrifices produce no material event at
  all); and cost (~2.3k tokens per 8-game digest call, the worst
  economics of the three designs). The winning design keeps the
  model's judgment where it adds value, reading a handful of
  *pre-screened* candidates, not raw archives.

## Contract changes (main session, before delegating)

- `domain.py`: `ScanSpec` (the ordered `match` list of event
  conditions), `ScanMatch`, `ScanOutcome`, `ScanCandidate` (a lean
  storage row: summary + `san_moves` + `evals`, evals `None` on
  unanalyzed games exactly as `RepertoireGame` does, no pgn), and
  `GameSearchPage`. Component docs updated in the same commit per
  the hard rule.
- `ChatToolkit` protocol
  ([providers.py:275](../../backend/src/chess_coach/coach/providers.py)):
  `scan_games(spec, *, filters...) -> ScanOutcome`; `find_games`
  return type becomes `GameSearchPage` and gains `offset`;
  `get_game` gains `ply`.
- `GameFilters.opponent` semantics: exact to substring
  ([03-storage.md](../03-storage.md) in the same commit).
- [06-coach.md](../06-coach.md) "Chat": tool table gains
  `scan_games` with the predicate definitions and renderer contract.
  While that section is open, fix the known drift where "Chat >
  Tools" omits `compare_groups`.
- [07-api.md](../07-api.md) "Chat": `ApiChatToolkit` additions. No
  HTTP endpoint changes, so no OpenAPI regen and no frontend work;
  `/games/{id}?ply=N` links already render clickable in chat.

Statistical hygiene, stated as a contract: scan predicates are
outcome-adjacent, so they must never become `ComparisonGroup`
dimensions. Conditioning a bucket on "made a sacrifice" and then
measuring how those games ended manufactures a tendency; scans show
games, `compare_groups` proves differences.

## Build plan

1. **main session**: contract commit above (domain types, protocol,
   doc updates).
2. **storage-dev**: opponent substring flip + `scan_candidates(db,
   username, filters) -> list[ScanCandidate]` (LEFT JOIN analyses,
   `evals` None on unanalyzed rows, honoring `filters.analyzed`,
   shared clause builder, newest first, no pgn hauled) + tests +
   [03-storage.md](../03-storage.md). ~0.5 day.
3. **coach-dev**: `coach/scan.py`, pure functions over
   `ScanCandidate`, reusing highlights' SEE helpers (same component;
   promote from underscore-private as needed) with the sacrifice
   gates (escalation, piece tier, promotion exclusion), the
   realization/soundness/balance annotations, and the sequence join
   for `match` chains; event unit tests over crafted games,
   including the favourable-capture, the hung-queen, the
   already-winning cash-in, the declined-sac, and the promotion
   cases; providers.py wiring (schema, description,
   both provider registrations, renderer, tool summary), the
   `get_game` renderer widening and ply block, the `find_games` page
   renderer, prompt bullets. ~2 days. Runs in parallel with 2 after
   the contract commit.
4. **api-dev**: `ApiChatToolkit.scan_games` (filters from args,
   candidate cap, `game_record` denominators, `run_scan` in the
   threadpool so the event loop stays free for SSE), `find_games`
   total + offset, `get_game` ply pass-through; caps as module
   constants beside `_FIND_GAMES_LIMIT_CAP`; scan wall-time log
   line; tests + [07-api.md](../07-api.md). ~0.5-1 day.
5. **boundary-reviewer** before committing, as usual for
   multi-agent work.

Slices, each independently valuable: slice 1 is steps 1-4 with the
**sacrifice event only**, single-event `match` (both target turns
work end to end); slice 2 adds `comeback`, `eval_swing`,
`delivered_mate`, `castled` and the multi-event sequence join
(registry entries, enum values, the chain matcher, tests; no storage
or api change, and `ScanSpec.match` is a list from slice 1 so the
schema does not break); slice 3, only if the logged wall times
demand it, an in-process scan LRU keyed on (username, spec, filters,
analysis freshness).

Estimate: 3-4 days for slice 1, most of it in coach-dev's predicate
tests.

## Tests

Hermetic as always: no network, no real Stockfish, no LLM.

- storage-dev: substring matching with `%`/`_` escaping; scan
  candidates exclude unanalyzed games and never carry pgn; count
  parity between `scan_candidates` denominators and `game_record`.
- coach-dev: the event suite is the heart of the feature. Fixture
  games modeled on the three real dialogue games (the spike is the
  reference for expected behaviour) asserting: a queen sac followed
  by mate matches with `piece="queen"`, a mate-in-N detail and
  `realizes` set; an exchange sac matches `piece="rook"` despite
  netting 2; a favourable capture does not match; a check the
  opponent must answer is not an offer (the Qxe1+ shape); a
  promotion is never a sacrifice; a declined offer matches and is
  annotated `declined`; a queen hanging for three plies yields one
  match at the offer, never a re-fire (the escalation gate); a sac
  played while a pawn hangs still matches (escalation, not a binary
  gate); an in-check ply counts prior gain as zero; eval-before
  lands in the detail so an already-winning +8 cash-in is visibly
  not "balanced before". Composition: a two-step chain matches in
  order, respects `within_plies`, and never matches out of order.
  Renderer goldens for the scan preamble, the widened move sheet,
  and the ply position block.
- api-dev: cross-player guard holds for scan and ply paths; caps
  clamp; denominators correct when filters exclude everything.

## Risks and trade-offs

- **SEE is static.** A "sacrifice" whose recapture is refuted by a
  zwischenzug can be flagged when it is really a combination winning
  material; players often call exactly that a sacrifice, so this
  cuts both ways. A hung queen the opponent captured passes the same
  test as an immortal sac: `sound_only` filters the ones that lost,
  eval-before plus the `get_game` judgment cross-check catches the
  rest, and the prompt orders a read before praise.
- **The residue class is marked, not removed.** Recall-first means
  what survives the gates besides genuine attacking sacs includes
  material conceded or offered in dead-won positions (a king move
  abandoning a rook at +10) and queen-drop blunders. All of it
  carries the annotations that give it away (`balanced_before`
  false, `sound` false, `declined`) and a SAN that reads nothing
  like a sacrifice; the model triages by flags and detail line,
  which is the division of labour the design intends. The volume is
  bounded: 60 flagged games in 605 analyzed wins on the real
  archive. See the spike report's decomposition table.
- **False negatives are the honest class.** Long-term positional
  sacs where the material is never takeable at one ply, pure pawn
  sacs, and sacs living only in unanalyzed games will not surface.
  The denominators keep those silences visible instead of
  invisible.
- **Latency.** Seconds of CPU inside an SSE turn, re-paid on
  repeats. The tool event keeps the UI honest; the candidate cap
  bounds the worst case; the wall-time log gates the cache decision.
- **Schema-token tax.** One more tool costs ~200-250 tokens on every
  chat message of every thread, used or not (the design record's
  standing warning). The description must stay tight, and this is
  the argument for exactly one new tool rather than three.
- **Reply-side honesty is still prompt discipline.** The
  denominators are structural but the model repeating them is not,
  the same accepted-risk class as chat link integrity. If it slips
  in practice, a lightweight render-side check is the follow-up.
- **Turn budget.** The flagship turn spends 3-4 of 8 tool calls;
  a correction round fits, but thinly. See decision 3.

## Decisions, with how they resolved (2026-08-03)

1. **Verifying an unanalyzed find.** ~7,000 of ~8,200 stored games
   have no analysis. The all-games scan finds the sacrifice in them
   anyway, but cannot verify soundness, and full analysis takes
   minutes, far beyond a chat turn. Resolved as recommended: the
   prompt has the coach say so plainly and link the game. The
   bounded "analyze this one game from chat" affordance (it breaks
   the toolkit's read-only rule and needs async progress) moved to
   the backlog's housekeeping list as its own decision.
2. **v1 event set.** Resolved: built in two gated phases within one
   build day: the sacrifice event with its full fixture battery
   first, then the remaining events and the sequence join.
   `ScanSpec.match` was a list from day one, so phase 2 changed no
   contract.
3. **`_CHAT_MAX_TURNS`.** Resolved: left at 8. A search turn spends
   3-4 calls; if real turns hit the ceiling, the bump is a one-line
   change, noted on the backlog watch list.
4. **Opponent substring flip.** Resolved: shipped. Verified
   chat-only (no HTTP route sets the filter), and docs/03 records
   the loosening and its reason.
