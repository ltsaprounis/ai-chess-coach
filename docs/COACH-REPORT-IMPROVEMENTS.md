# Coach report and prompt — review and improvement plan

Why the whole-report coaching advice (`POST /players/{u}/coach`) is
weak, traced to the code that produces it, with a prioritized plan.
Written July 2026 against a real 515-game report whose advice was
wrong in three ways the student named immediately:

1. It told them to stop playing the Englund Gambit — an opening they
   have never played, only faced.
2. It cited positions as "position 1", "position 5".
3. It had no idea whether they are improving, because nothing about
   rating or time ever reaches the prompt.

Each is a data problem, not a model problem. The model is Opus; it
answered the prompt it was given. This doc is about the prompt and
the report behind it.

The Dashboard reads the same two endpoints and shows the same numbers
to the student directly, so most findings land on both surfaces — see
[The dashboard shares this data](#the-dashboard-shares-this-data).

Quantities below are computed from that report's own repertoire table
(77 rows summing to exactly 515 games — every analyzed game got an
opening) and its judgment counts (19,018 player moves).

## Status

Findings 1-8, 10 and 12 shipped in July 2026 (P0, P0b, P1 and the
report cache). Findings 9 and 11 are still open, and the line numbers
in the table below refer to the code as it stood before the rework.

The repertoire semantics that finding 1 turns on are now stated once
in [06-coach.md](06-coach.md) and implemented twice against that
statement; `tests/test_repertoire_agreement.py` asserts the two
producers agree field for field, which is the check that keeps the
duplication honest. The prompt itself is snapshot-tested
(`tests/testdata/coach_prompt.md`), so further changes to it are
reviewable as a diff of the artifact.

## Findings at a glance

| # | Finding | Where | Surfaces | Pri | Status |
|---|---------|-------|----------|-----|--------|
| 1 | Repertoire has no color; opponents' openings read as the student's | `coach/report.py:49`, `coach/prompt.py:73`, `storage/games.py:289` | coach + dashboard | P0 | done |
| 2 | 46 of 77 rows are ≤2 games, sorted to the top by raw win rate | `coach/report.py:103` | coach (dashboard already filters) | P0 | done |
| 3 | Repertoire `Avg cp loss` is whole-game ACPL, labelled as opening | `coach/report.py:64`, `storage/games.py:286` | coach + dashboard | P0 | done |
| 4 | Phase ACPL averages in 0.0 for phases a game never reached | `engine/analysis.py:97`, `coach/report.py:40` | coach + dashboard | P0 | done |
| 5 | Judgment counts have no denominator | `coach/prompt.py:66` | coach (dashboard has per-game tiles) | P0 | done |
| 6 | Critical positions cited by list index, bare FEN, no before-eval | `coach/prompt.py:89` | coach | P0 | done (PV still absent — needs 9) |
| 7 | Rating, trend, time class, termination never reach the prompt | `domain.py:131`, `ingestion/normalize.py:30` | coach + dashboard | P1 | done (termination backfills on re-sync) |
| 8 | Instructions ask for sections, not for rigor | `coach/prompt.py:32` | coach | P1 | done |
| 9 | Report path has no engine tool, so it cannot verify a line | `coach/providers.py`, `docs/06-coach.md:54` | coach | P2 | open |
| 10 | The report LLM call is never cached | `api/routes.py:486` | coach | P2 | done |
| 11 | No durable player profile to feed other features | — | coach + dashboard | P3 | open |
| 12 | The coach ignores the window/time-control filters the report takes | `api/routes.py:502` | coach | P1 | done |

Two notes for whoever picks up 9 and 11. The turning-point entries
carry no engine PV, because producing one needs the analyst callable
finding 9 adds — that is the one part of finding 6 still outstanding.
And the family rollup keys on the student's own first three moves
rather than on "first moves plus ECO, falling back to the name root"
as sketched below: the lichess names turned out too coarse for a
name-based fallback (82 variations sit under "Queen's Pawn Game",
spanning 36 distinct move orders), while the student's own moves are
a direct statement of the repertoire and separate chosen from faced
by construction.

## Non-goals

This doc covers the whole-report coaching path and the Dashboard
views that read the same data. The explain-move prompt is in good
shape and is cited here as the pattern to copy. One-click game review
(NEW-FEATURE-PROPOSAL #7) and clock analysis (#4) are out of scope,
though both benefit from findings 7 and 11.

## 1. The report never records which side the student had

**Symptom.** "Avoid speculative choices such as the Englund and Ryder
gambits" — advice about openings the student only ever faced as
White.

**Cause.** `coach/report.py:49` groups by `(eco, name)`; `OpeningStats`
(`domain.py:104`) has no `color`. `coach/prompt.py:73` renders ECO /
name / games / W-L-D / cp with no indication of side. The opening is
a property of the *game*, not of the student's choice, so the table
silently merges "openings I chose" with "openings I faced", and
nothing in the prompt distinguishes them.

The report's own numbers show how badly this misleads. Rolled up by
family: Pirc-named rows total 167 games (73-88-6), London-named rows
155 games (78-71-6), Englund-named rows 24 games. That is a student
who plays 1.d4 systems as White and the Pirc/Modern as Black — and
whose opponents answer 1.d4 with the Englund, the Horwitz Defense
(29 games), the Benoni, the Dutch, the Grünfeld and the King's
Indian. Roughly half the table is the *opponent's* repertoire,
presented as the student's.

Note the model's Pirc advice may well have been right, and its
Englund advice was certainly wrong, for the same reason: it could not
tell chosen from faced. Fixing this is what makes the rest of its
opening advice trustworthy.

**Fix.**

- Add `color: Color` to `OpeningStats`; group by `(color, eco, name)`.
- Render two sections, "As White" and "As Black", never one table.
- Add a **first-moves column**: the most common 3-4 SAN moves for the
  group. `1.d4 e5 2.dxe5` states who chose what in a form no opening
  name can obscure. LLMs read move sequences far more reliably than
  ECO names, and this kills the whole class of error, not one
  instance of it.
- Add a repertoire summary above the tables — "As White: 1.d4 in 96%
  of games. As Black vs 1.e4: 1...d6 in 88%" — which is the student's
  actual repertoire; the table is the detail.

Within each color section, split "systems you chose" from "replies
you face" if it can be derived from the move column. The column makes
the distinction legible even when the split is not computed.

## 2. The table is noise-dominated, and sorted so the noise floats up

**Symptom.** The model reasoned about the top of a table labelled
"worst-scoring first" and drew conclusions from lines played once.

**Cause.** Two compounding problems.

*Sample.* 46 of the 77 rows have ≤2 games; 30 have exactly one. Those
46 rows cover 62 games — 12% of the data. The 18 rows with ≥5 games
cover 410 games (80%). Because `_score` (`coach/report.py:103`) is
raw win rate, every 0-1-0 singleton sorts above every genuine problem
line. There is no minimum sample and no cap on rows.

*Granularity.* The lichess ECO names are far finer than the concept
"my opening". The Pirc is split across **12 rows** (82 + 30 + 20 +
12 + 9 + …). The model saw 82 and reasoned about a third of the
database as if it were a sixth of it. The frontend already solved
this — `web/src/openings.ts:31` `groupByFamily` rolls names up at the
first colon for the dashboard — but that logic lives only in
TypeScript, which is why the dashboard reads better than the prompt.

**Fix.**

- Roll up to families in the report. A first-colon split alone is not
  enough: it files "Queen's Pawn Game: Accelerated London System"
  (122 games) under "Queen's Pawn Game" beside the Torre Attack. Key
  the rollup on first moves plus ECO, falling back to the name root.
- Require a minimum sample (~5 games) for the main table; collapse
  the remainder into one line — "long tail: 46 lines, 62 games, none
  with more than 2".
- Sort by **impact** (games × win-rate deficit), not by rate, so the
  top of the table is where the losses actually are.
- Show a score percentage rather than only W-L-D, and mark small
  samples so the model can discount them.

Cutting 46 rows is not only about accuracy — it frees prompt budget
for the context in finding 7, which is worth far more per token.

## 3. `Avg cp loss` in the repertoire table means something else

**Cause.** `coach/report.py:64` fills the column from
`g.analysis.overall_acpl` — whole-game ACPL, for every game in which
that opening appeared.

**Symptom.** "QGD Marshall Defense — 375.5" reads as *you play this
opening terribly*. It means *the three games it appeared in went
badly later*. The model duly built opening advice on it.

**Fix.** Two labelled columns: opening-phase ACPL (from the phase
split) and whole-game ACPL. They answer different questions — "do I
get out of the opening safely" and "do these games go well" — and
only the first is opening advice.

## 4. The phase aggregates are wrong, in the direction that matters

**Cause.** `engine/analysis.py:97` writes `0.0` for phases a game
never reached (`_mean([])` is `0.0`). `coach/report.py:40` then
averages those per-game numbers across all games. Every game that
ended before an endgame contributes **a 0.0 to the endgame average**.

**Symptom.** "ACPL by phase: opening 30.2, middlegame 227.8, endgame
121.2", and a report whose headline finding was "openings fine,
middlegame collapses, endgame acceptable". The endgame figure is
diluted by the fraction of games that never reached an endgame: if
40% of games reach one, the reported number is about 0.4× the truth.
The relative picture the entire report rests on is an artifact of the
zeros.

Same family, same cause: `overall_acpl` (`coach/report.py:38`) and
the per-opening column are means of per-game means, so a 15-move loss
counts as much as a 90-move grind.

**Fix.** Aggregate from the raw `evals` — already on `AnalyzedGame` —
as total loss ÷ total moves, per phase and overall. Report the move
count behind each phase figure so a thin sample is visible. Land this
before any prompt rewrite; otherwise the new prompt reasons over the
same bad numbers.

## 5. Judgment counts have no denominator

**Symptom.** "Across 515 games you made 1,535 mistakes and 1,823
blunders, indicating insufficient comparison of candidate captures" —
a number dump with a non sequitur attached, because raw counts cannot
be calibrated.

**Fix.** The same counts as rates: 19,018 player moves, 36.9 per
game — best 37.9%, good 34.4%, inaccuracy 10.0%, mistake 8.1%,
blunder 9.6%, i.e. **3.5 blunders per game**. Give rates per phase,
and a reference band for the student's rating so the model can say
"high for your level" instead of guessing.

## 6. Critical positions: unidentifiable, unverifiable, badly chosen

**Symptom.** "In position 1, you played `...Nb6` instead of
`...Re1+`." The student cannot find position 1 in their history, and
neither the model nor the reader can check that `...Re1+` is legal.

**Cause.** `coach/prompt.py:93` numbers the entries `1.`–`5.` and
gives a bare FEN. A list index is the only handle the prompt offers,
so the model uses it. A FEN is also close to opaque to an LLM: it
asserted concrete tactics from static piece placement, and with
`max_turns=1` and no tools (finding 9) it could not have verified
them even if asked to.

**Fix — identity.** Date, time class, the student's color, opening,
and move number with side. Then the model writes "your 26...Nb6 in the
June 14 blitz Pirc", which the student can find and the UI can
deep-link to the game.

**Fix — content.** Give the line, not the position: the last ~4 plies
leading in, the played move, and the engine's PV in SAN for the
refutation. `MoveEval.best_move` alone is a move with no consequence
attached, so the model has nothing to say about *what it wins* and
pads with generic advice. Render eval swings in pawns, following
`format_cp_loss` (`coach/prompt.py:194`) — the report prompt is the
only one still emitting raw centipawns.

**Fix — selection.** All five entries in the observed report were
mate-scale, and `_MATE_SCALE` (`coach/prompt.py:30`) renders every one
with the identical phrase "a decisive, forced-mate-scale blunder".
The before-eval is discarded entirely, so the model cannot tell a
game-losing blunder from a coup de grâce in an already-lost position
— and once a player has any walk-into-mate moves, those five slots
are permanently theirs, crowding out every instructive error.

Select **turning points** instead: moves where the eval crossed a
decision boundary (winning→equal, equal→lost) while the game was
still contestable — before-eval within, say, ±3 pawns — capped at one
per game, spread across phases, openings and time classes, weighted
toward recent games. Raise `_TOP_CRITICAL` (`coach/report.py:25`,
currently 5) accordingly.

**Fix — patterns.** Anecdotes do not generalize; counts do. Tag
blunders with deterministic patterns using python-chess: was the
piece en prise after the move, did the refutation open with a check,
is it a back-rank motif, a mis-counted capture sequence. "You hung a
piece to a check 34 times in 515 games" is worth more than five
positions and costs no LLM tokens. The same tags feed the blunder
puzzles (NEW-FEATURE-PROPOSAL #2) and the profile in finding 11.

## 7. Context in the database that never reaches the prompt

`Game` carries `player_rating`, `opponent_rating`, `end_time`,
`time_class` and `accuracy`. `PlayerReport` (`domain.py:131`) uses
none of it.

- **Rating trajectory** — current per time class, delta over the
  window, min/max, monthly series. "Up 60 in blitz over three months
  with flat ACPL" and "down 100" are different conversations; the
  prompt supports neither.
- **Time-class mix** — 515 bullet games and 515 rapid games need
  opposite advice. The model prescribed "20 puzzles daily" without
  knowing whether the problem is calculation or the clock.
- **Trend in our own metrics** — ACPL and blunder rate by month. This
  is NEW-FEATURE-PROPOSAL #3; the coach is the consumer that
  justifies building it.
- **Opponent strength** — average rating difference, score against
  stronger and weaker opponents.
- **How games end** — `ingestion/normalize.py:30` collapses `timeout`,
  `resigned`, `checkmated` and `abandoned` into a single `"loss"`.
  That is discarded at ingestion and it is among the most actionable
  signals a coach can have. Keep the raw per-player code as
  `Game.termination`: one column, one migration, a re-sync, no
  reanalysis.

Per-move clocks (NEW-FEATURE-PROPOSAL #4) would be the strongest
addition of all — "your blunders cluster under 30 seconds" — but
everything above is available today at near-zero cost.

## 8. The instruction block

`_INSTRUCTIONS` (`coach/prompt.py:32`) asks for four fixed sections
and "avoid generic advice", which is a request, not a constraint.
What would change the output:

- **State the student.** Rating band, time controls, realistic study
  time. There is no rating anywhere in the prompt today, so every
  recommendation is pitched at an imagined player.
- **Anti-hallucination rules, explicitly.** "Only call an opening the
  student's if it appears under their color with their own move
  choice. Never advise dropping an opening the opponent chose —
  recommend a response to it instead."
- **Ban index citation.** "Refer to positions by date and move
  number, never by list position."
- **Demand prioritization.** One "biggest lever" section. Three
  co-equal weaknesses and three co-equal exercises is a list, not
  coaching.
- **Adopt the explain style contract** — pawns not centipawns, idea
  before number. `docs/06-coach.md` specifies it for explain; the
  report prompt never got it.
- **Require honest uncertainty.** Say when the data does not support
  a conclusion, rather than filling the section anyway.

`SYSTEM_PROMPT` (`coach/prompt.py:14`) is also thin, and
`LlmConfig.max_tokens` defaults to 4096 (`domain.py:31`) — tight for
a full report, with no thinking budget configured.

## 9. Give the report the engine tool

The single biggest quality lever, already built. `explain` runs
agentically against `analyze_position` through `PositionAnalystFn`
(`docs/06-coach.md:47`), and both shipped providers implement it —
including the Copilot provider's turn-budget guard, which is the hard
part. The report path is `complete()`: `max_turns=1`, no tools, which
is exactly why the model asserted tactics it could not check.

Engine time is free (house rule). Let the coach verify a line before
writing it, and let it choose which of ~20 candidate positions matter
rather than being handed five. Two shapes worth weighing: one agentic
run with a small turn budget, or two passes (select positions and
call the engine, then write).

This needs a contract change — `CoachProvider.complete` takes no
analyst — so it is a main-session decision, not a component task.

## 10. The report LLM call is never cached

`coach_player` (`api/routes.py:486`) builds the report, renders the
prompt and calls `provider.complete` on every POST. There is no cache
read, no cache write, and no reports table — storage has `games`,
`analyses` and `explanations` only.

House policy is "user-triggered **and** cached" (the explain-move
rule). The report path satisfies the first half only: every click of
the coach button re-bills a full Opus report over identical data.
That is tolerable today at `max_turns=1` and gets materially worse
with finding 9's agentic run.

**Fix.** A `reports` table mirroring `explanations`: keyed by
(username, agent_id), storing the prompt, the advice, `generated_at`,
the window covered, `games_analyzed` and a `prompt_version`, with a
`refresh=True` escape hatch exactly like
`GET /games/{id}/explain`. Storing `games_analyzed` lets the UI say
"generated over 515 games; you have 540 now" instead of silently
serving stale advice — the same staleness signal the profile needs.

The `prompt_version` column is the housekeeping item already noted in
NEW-FEATURE-PROPOSAL; a rework this large is the reason it exists.

## 11. The player profile

The durable artifact that feeds move analysis and later features.
Two layers:

**Deterministic facts** — `PlayerProfile` in `domain`, computed in
`coach` from stored games: repertoire by color with move sequences,
rating trajectory, phase and time-class error rates, tagged error
patterns with counts. Free, always fresh, recomputed on demand. This
is the same aggregation findings 1-7 produce, which is why the
profile comes last: build it earlier and you build it twice.

**An LLM narrative** — three to five sentences of tendencies plus a
short evidence-linked weakness list. Expensive, therefore stored and
regenerated only on explicit user action, in a `player_profiles`
table keyed by username with `generated_at`, window bounds,
`games_covered`, `agent_id`, `prompt_version`, the facts JSON and the
narrative text.

The payoff is `render_profile_context(profile) -> str`: a compact
(~250 token) block other prompts embed at the top. The explain prompt
stops being "explain this move" and becomes "explain this move *to a
player who hangs pieces to back-rank checks and plays the London*".
One block, every future feature, one place to improve it.

Placement: aggregation in `coach` (it is report-shaped), persistence
in `storage`, composition in `api`; `domain` gains `PlayerProfile`.
Keep it inside component 6 rather than opening docs/09 — it is the
report's own output, not a new concern.

## 12. The coach reasons over all history, always

`player_report` (`api/routes.py:466`) accepts `since`, `until` and
`time_class`. `coach_player` (`api/routes.py:486`) accepts neither —
it calls `list_analyzed_games(db, user)` with no filters at all, and
`CoachRequest` carries only `agent_id`.

So the Dashboard can show the last 30 days of blitz while the coach
reasons over every game ever played, weighting a bullet game from
last year the same as yesterday's rapid. Advice about *form* is
impossible from that input, and the prompt does not even state which
period it covers.

**Fix.** Take the same three filters on `CoachRequest`, pass them
through, and state the resolved window and time-control mix at the
top of the prompt (see the appendix skeleton). The window then also
becomes part of the cache key in finding 10 — two coach runs over
different windows are different reports, not a cache hit.

## The dashboard shares this data

The Dashboard (`web/src/pages/Dashboard.tsx`) reads both endpoints
this doc is about — `GET /players/{u}/openings` and
`GET /players/{u}/report` — and renders them straight to the student.
Everything wrong in the prompt is also wrong on screen, minus the
model's editorializing. The repertoire table there lists the Englund
Gambit under "Repertoire — worst first … the ones to work on".

### The repertoire aggregation exists twice

`OpeningStats` is produced by two independent implementations:

- `storage/games.py:252` `opening_stats` — SQL, over **classified**
  games, feeding the Dashboard table.
- `coach/report.py:49` `_opening_stats` — Python, over **analyzed**
  games, feeding `PlayerReport` and the prompt.

Both have findings 1 and 3, and both take an unweighted mean of
per-game means (finding 4's second half — `AVG(a.overall_acpl)` at
`storage/games.py:286`). Fixing one does not fix the other. The
component boundary makes the duplication structural — coach cannot
import storage — so the fix is not to merge them but to **define the
semantics once** (family key, color split, which ACPL each column
means) in docs/03 and docs/06 and implement both against that
definition.

While adding `color`, note that `analyzed_games` means different
things in the two paths: a real sub-count in SQL, and always equal to
`games` in the coach path (`coach/report.py:63`). Same field, two
meanings, one type.

### What changes on the Dashboard

| Finding | Dashboard surface | Change |
|---|---|---|
| 1 | Repertoire table has no color column | Split the table by color, or add a color column plus a White/Black toggle; add the first-moves column so chosen vs faced is visible |
| 3 | `Avg CP loss` column | Relabel as whole-game; add an opening-phase column beside it. The caption ("covers the analyzed games only") explains the denominator but not which phase |
| 4 | "ACPL by phase" bar chart | Same diluted endgame bar, from the same `build_report`. Fixed server-side; the chart should then show move counts and render "no endgame moves" rather than a 0 bar (`Dashboard.tsx:162` filters on `!== undefined`, which never fires because every phase key exists) |
| 7 | Tiles and charts | New: termination breakdown ("38% of losses on time"), ACPL and blunder-rate trend by month, opponent-strength split. The rating chart already exists |
| 11 | — | The profile's error-pattern tags make a natural "Recurring mistakes" section, sharing data with the blunder puzzles |

Finding 6's turning-point selection is worth surfacing too: the
Dashboard has no "here are your five worst moments, click to replay"
section, and once the selection logic exists it is one query and one
list away — with a better claim on the student's attention than the
judgment-distribution bar chart.

### Where the dashboard is already ahead of the prompt

Three of this doc's recommendations are already implemented on the
Dashboard and should be copied into the report rather than reinvented:

- **A minimum-sample filter** (`Dashboard.tsx:76`) defaulting to 5
  games, user-adjustable, with an explicit "no family has 5+ games"
  empty state. Exactly finding 2's recommendation, already shipped —
  which is why the Dashboard repertoire reads sensibly and the prompt
  does not.
- **Per-game rates** — "blunders per game", "mistakes per game" tiles
  (`Dashboard.tsx:299`) instead of raw counts. Finding 5 asks the
  prompt for the same thing, per move as well as per game.
- **Window and time-control filters** scope every number on the page
  (`Dashboard.tsx:26`, `Dashboard.tsx:121`). The coach endpoint takes
  neither — finding 12. The filter UI already exists; the coach panel
  should reuse the same controls and pass them through.

The pattern is consistent enough to be worth stating plainly: where
the Dashboard and the prompt disagree, the Dashboard is usually
right. It was built for a human who would notice nonsense
immediately; the prompt was not.

## Contract changes required

Each updates the affected component doc in the same commit.

| Change | Components | Docs |
|---|---|---|
| `OpeningStats.color` + opening-phase ACPL column | coach, storage, api, frontend | README, 03, 06, 08 |
| Shared repertoire semantics (family key, color, ACPL meaning) | coach, storage | 03, 06 |
| `PlayerReport` gains ratings/trend/time-class/pattern blocks | coach, api, frontend | README, 06, 07, 08 |
| `Game.termination` (raw chess.com result code) | ingestion, storage, frontend | README, 02, 03, 08 |
| `CoachRequest` gains `since`/`until`/`time_class` | api, frontend | 07, 08 |
| `CoachProvider.complete` accepts a `PositionAnalystFn` | coach, api | 06, 07 |
| `reports` cache table | storage, api | 03, 07 |
| `PlayerProfile` + `player_profiles` table | domain, coach, storage, api | README, 03, 06, 07 |

`OpeningStats` is shared with the Dashboard repertoire endpoint
(`storage/games.py:252`, `api/routes.py:123`) and the frontend
(`web/src/openings.ts`), so adding `color` ripples through
`pnpm gen:api`, `groupByFamily` and the Dashboard table. Sequence it
as one contract change followed by two component slices (storage,
then frontend), not as a side effect of the coach work.

## Sequencing

**P0 — coach-only, one task.** Findings 1-6 all live inside
`chess_coach.coach` and need exactly one domain field
(`OpeningStats.color`): color and first moves in the repertoire,
family rollup with a sample floor, split ACPL columns, correct phase
aggregation from raw evals, judgment rates, and critical positions
with identity, lines, eval swings and turning-point selection. This
alone would have prevented every complaint that triggered this doc.
Do finding 4 first — everything else reasons over those numbers.

**P0b — the shared repertoire, one contract change.** `OpeningStats`
gains `color` and an opening-phase ACPL column; both implementations
are updated against the definitions written into docs/03 and docs/06;
`pnpm gen:api` runs; the Dashboard table gains the color split, the
first-moves column and the relabelled ACPL columns. Storage and
frontend slices are separable once the contract is fixed, and the
frontend one is a natural `frontend-dev` task.

**P1 — context.** Finding 7: rating trajectory, time-class mix,
monthly trend, opponent strength (one storage query plus report
fields), then `Game.termination` (ingestion field, migration,
re-sync). Finding 12 (coach filters) lands here too — it is small and
it makes the window statable in the prompt. Finding 8's instruction
rewrite comes last of the three, since it depends on the student
description they supply. The Dashboard additions from finding 7
(termination breakdown, ACPL/blunder trend) follow the same data and
can ship in the same pass or trail it.

**P2 — the LLM call path.** Findings 9 and 10 together: make the
report agentic and cache it in the same change, so the more expensive
call is never paid twice.

**P3 — profile.** Finding 11, once the deterministic facts settle.

## Verification and test gaps

`docs/06-coach.md` calls for "`render_prompt` with a snapshot test
(template stability matters — the prompt is a user-visible
artifact)". That test does not exist. What ships is
`test_render_prompt_is_deterministic_and_complete`
(`tests/test_coach.py:156`): it asserts `render_prompt` is
deterministic and that five substrings appear. Determinism is not
stability — the prompt can change completely and still pass.

This is docs-contract drift, and it matters right now: a rework this
size should be reviewable as a prompt diff. **Add the promised
snapshot test before starting**, so every change below shows up as a
readable diff of the artifact rather than as green tests.

Then, alongside the rework:

- A fixture where the student is White against an opponent's gambit,
  asserting the rendered prompt attributes that opening to the
  opponent. That is the Englund regression test.
- A phase-aggregation test with games that never reach an endgame,
  asserting the endgame figure ignores them rather than averaging in
  zeros.
- A repertoire test asserting single-game lines land in the long-tail
  bucket, not at the top of the table.
- A caching test: two coach calls, one provider invocation.
- A coach-filter test: a windowed request reaches
  `list_analyzed_games` with the window, and the prompt states it.

On the frontend, `web/src/openings.test.ts` and `web/src/api.test.ts`
both build `OpeningStats` fixtures and will need the new fields;
`groupByFamily` needs a case asserting that two colors of the same
family stay separate rather than merging.

Expect the existing substring assertion
`"| C60 | Ruy Lopez | 1 | 1-0-0 | 2.5 |"` (`tests/test_coach.py:175`)
to change — the columns it names are the ones findings 1 and 3
replace.

## Appendix — target prompt shape

Angle brackets are placeholders, not example values. The point is the
section set and the fields each carries; wording is the implementer's
call.

```markdown
# Coaching brief — <username>

## The student
- Ratings: blitz <n> (<±n> over window), rapid <n> (<±n>)
- Plays: <n> blitz, <n> rapid, <n> bullet, <start> to <end>
- Analyzed: <n> of <n> games, Stockfish depth <n>

## How the play breaks down
| Phase      | ACPL | Moves | Blunder % | Typical at <rating> |
| opening    | <n>  | <n>   | <n>%      | <band>              |
| middlegame | …
Overall <n> ACPL over <n> moves — <n> blunders per game.

## Trend
| Month | Games | Rating | ACPL | Blunder % |

## How games end
Wins <n>: checkmate <n>, resignation <n>, timeout <n>
Losses <n>: checkmate <n>, resignation <n>, timeout <n>

## Repertoire — as White (<n> games)
You open 1.d4 in <n>% of games.
| System (first moves) | Games | Score | Opening ACPL | Game ACPL |
### What you face as White
| Their reply (first moves) | Games | Score | Opening ACPL |

## Repertoire — as Black (<n> games)
(same shape)
Long tail: <n> lines under 5 games, <n> games total.

## Recurring error patterns
| Pattern | Count | % of blunders | Example |
| Piece hung to a check | <n> | <n>% | <date> <class>, move <n> |

## Turning points
### 1. <date>, <time class>, as <color>, <opening> — move <n>
Position: <fen>
Leading up: <last 4 plies in SAN>
You played <san>: <x> → <y> pawns
Engine: <best san> <pv in SAN> (<z> pawns)
```

Closing instructions, in place of `_INSTRUCTIONS`:

- Audience and register: a club player, pawns not centipawns, the
  idea before the number.
- Attribution rule: an opening is the student's only where the
  repertoire section lists it under their color as a system they
  chose; never advise dropping an opening they only face.
- Citation rule: refer to positions by date and move number.
- Verification rule (once finding 9 lands): check any line with
  `analyze_position` before asserting it.
- Output: the biggest lever first, then weaknesses with evidence,
  then opening advice, then a two-week plan sized to the student's
  actual playing time. Say so when the data does not support a
  claim.
