# Component 6 — Coach (report, prompt, LLM provider)

Turns analyzed games into a `PlayerReport`, renders the report into a
structured coaching prompt, and sends it to an LLM through a provider
interface. This is the only component that talks to an LLM API.

## Interface

```python
# Pure aggregation — input assembled by the API layer from storage.
# `time_class` is the filter the caller applied, recorded so the
# prompt can state the scope of its own numbers; the *covered* window
# bounds are derived from the games themselves. `requested_since`/
# `requested_until` are the bounds the caller asked for, and
# `games_in_scope` is storage's count of every stored game matching
# the same filters, analyzed or not — copied onto the report so the
# prompt can state coverage rather than presenting the analyzed span
# as the requested one. All default to None: with no scope info the
# report (and prompt) render exactly as before.
def build_report(username: str, games: list[AnalyzedGame], *,
                 time_class: TimeClass | None = None,
                 requested_since: int | None = None,
                 requested_until: int | None = None,
                 games_in_scope: int | None = None) -> PlayerReport
# AnalyzedGame: domain composite = Game + GameAnalysis + Opening|None

# Deterministic markdown template, also shown/copyable in the UI.
def render_prompt(report: PlayerReport) -> str

# Post-processes the model's advice so its handle citations resolve
# to real links: normalizes inline `[text](gN)` to reference style,
# degrades citations with handles the prompt never offered to their
# plain text, and appends one `[gN]: /games/{id}?ply={n}` reference
# definition per offered handle (definitions the advice never cites
# render as nothing). URLs are minted here from the report, never
# written by the model. The API layer calls this on the provider's
# advice before caching, so cached advice is self-contained.
def append_game_links(advice: str, report: PlayerReport) -> str

# --- Highlights (Dashboard-only; deliberately outside PlayerReport
# --- so the coaching prompt doesn't grow thousands of rows) ---

class HighlightMove(BaseModel):    # one linkable move: identity the
    game_id: str; end_time: int    # student can find (date, opponent,
    time_class: TimeClass          # move number) plus the numbers the
    color: Color; result: Result   # row renders
    opponent: str
    opening_name: str | None
    ply: int                       # 1-based, matches MoveEval.ply
    move_number: int               # the "26" in "26...Nb6"
    san: str
    cp_loss: int
    eval_after_cp: int | None      # after the move, white POV like
    eval_after_mate: int | None    # MoveEval — the UI folds by color

class PlayerHighlights(BaseModel):
    blunders: list[HighlightMove]      # newest game first, then ply
    brilliancies: list[HighlightMove]  # same order

# Pure static analysis over stored analyses — SEE + stored evals; no
# engine calls, no LLM. `thresholds` comes from config's `brilliant`
# section (a domain type, like judgment `Thresholds`).
def build_highlights(games: list[AnalyzedGame], *,
                     thresholds: BrilliantThresholds) -> PlayerHighlights

# Bumped whenever the template changes materially. The API layer keys
# the report cache on it, so a reworded prompt invalidates old advice
# instead of serving it against a template that no longer exists.
PROMPT_VERSION: str

# --- Player profile (see "Player profile" below; the design record
# --- is docs/future-improvements/player-profile.md) ---

# Pure distillation of an already-built report into the compact facts
# layer — the aggregation runs once, in build_report; this projects
# it. `narrative` stays None here; the API layer attaches the stored
# narrative when one exists.
def build_profile(report: PlayerReport) -> PlayerProfile

# The narrative-generation prompt (snapshot-tested): the facts, plus
# instructions asking for 3-5 sentences of tendencies and a short
# weakness list with every claim tied to a figure the facts state.
def render_profile_prompt(profile: PlayerProfile) -> str

# The ~250-token block other prompts embed at the top: the facts one
# line each, then the narrative as the coach's read when present.
# Total over narrative=None (renders the facts alone).
def render_profile_context(profile: PlayerProfile) -> str

# The narrative's own version constant, independent of PROMPT_VERSION
# (the report template and the profile prompt evolve separately).
# Row metadata, never a cache key: a bump flags staleness in the UI;
# it must never silently re-bill (docs/03-storage.md).
PROFILE_PROMPT_VERSION: str

# --- Move explanation (runs only on explicit user request — LLM
# --- calls cost money; the API layer caches results in storage) ---

class MoveContext(BaseModel):      # everything the prompt needs
    username: str; color: Color; opening_name: str | None
    ply: int; san: str             # the played move (1-based ply,
    fen_before: str; fen_after: str  # matching MoveEval.ply)
    best_move: str; cp_loss: int; judgment: Judgment

# Pure aggregation: replays the game to the ply's positions and pulls
# that ply's MoveEval. Raises ValueError when the ply is out of range.
def build_move_context(game: Game, analysis: GameAnalysis,
                       opening: Opening | None, ply: int) -> MoveContext

# Deterministic template: the position, played vs best move, the
# seeded candidate lines (a MultiPV snapshot of fen_before), and an
# instruction to consult the engine tool for follow-ups — e.g. eval
# fen_after to name the opponent's refutation. The style contract:
# the audience is a club player, so the instructions demand pawn
# units ("about 4 pawns"), never raw centipawns; the idea (threats,
# plans, what the refutation wins) before any number; and no
# redundant annotation ("?" glyphs AND the word blunder). Eval
# numbers handed to the model in this template are pre-rendered in
# pawns for the same reason. Given a `profile`, the student-profile
# context block opens the prompt (see "Player profile"); with None
# the prompt renders exactly as before.
def render_explain_prompt(ctx: MoveContext, lines: list[EvalLine], *,
                          profile: PlayerProfile | None = None) -> str

# The engine seam. The API layer injects a callable wrapping the
# engine pool (components never import each other); depth and
# multipv are the injector's choice. FEN in, final lines out.
PositionAnalystFn = Callable[[str], Awaitable[list[EvalLine]]]

class ExplainEvent(BaseModel):     # one streamed explain increment
    type: Literal["text", "tool"]
    text: str                      # text chunk | tool-call summary

# --- Chat (see "Chat" below; every LLM call is a user pressing
# --- send, so the house cost rule is satisfied by construction) ---

class ChatEvent(BaseModel):        # one streamed chat increment
    type: Literal["text", "tool", "done"]
    text: str = ""                 # chunk | tool summary | full reply
    provider_state: str | None = None   # done events only

# The toolkit seam — PositionAnalystFn generalized. The API layer
# implements it over storage and the engine pool (components never
# import each other), pre-scoped to the thread's player: the model
# passes filters, never a username. All read-only; there is no raw
# SQL tool. Coach owns the tool names, schemas, descriptions, and
# result rendering, so the prompt style stays owned in one place.
class ChatToolkit(Protocol):
    analyst: PositionAnalystFn | None   # None = engine pool down
    async def find_games(self, *, opponent: str | None = None,
                         opening: str | None = None,
                         result: Result | None = None,
                         time_class: TimeClass | None = None,
                         since: int | None = None,
                         until: int | None = None,
                         limit: int = 10) -> list[GameSummary]
    async def get_game(self, game_id: str) -> GameDetail | None
    async def opening_stats(self) -> list[OpeningStats]

# Scope seeds — deterministic templates, snapshot-tested like every
# other prompt. `render_game_chat_context` raises ValueError when
# `ply` is set but out of range or the game is unanalyzed (mirroring
# build_move_context); `lines` is the MultiPV snapshot of the
# anchored position, seeded by the API layer exactly as for explain.
# `profile` embeds the student-profile block exactly as in
# render_explain_prompt; the report seed never takes one — the report
# is the profile's own source (see "Player profile").
def render_game_chat_context(detail: GameDetail, *,
                             ply: int | None = None,
                             lines: list[EvalLine] | None = None,
                             engine_available: bool,
                             profile: PlayerProfile | None = None,
                             ) -> str
def render_report_chat_context(report: PlayerReport, *,
                               engine_available: bool) -> str

# The shared replay formatter (see "Replay" below): prior turns as
# Student:/Coach: blocks, oldest first, then the new message. Every
# provider that cannot resume replays through this one function.
def render_chat_prompt(history: list[ChatMessage],
                       message: str) -> str

# The provider seam — everything LLM-specific hides behind this.
# `complete` takes an optional analyst: with one, the report run is
# agentic — the analyst exposed as the same `analyze_position(fen)`
# tool `explain` uses, under a small turn budget — so the model can
# verify concrete lines before asserting them. With `None` it is
# today's single turn, the fallback when no engine pool exists.
# `chat` is stateless with an opaque resume token: each call carries
# everything needed to answer from scratch (seed, stored transcript,
# new message), and a provider MAY shortcut the replay by resuming a
# warm session named by `provider_state` — a token it returned on a
# previous done event, persisted by the API layer on the thread. A
# provider that cannot resume (no token, expired session, API-backed
# provider) MUST fall back to replaying the transcript; resume
# failure is a cost event, never an error. Yields exactly one final
# `done` event carrying the full reply and the new token (None when
# the provider has nothing to resume).
class CoachProvider(Protocol):
    async def complete(self, prompt: str,
                       analyst: PositionAnalystFn | None = None) -> str
    def explain(self, prompt: str, analyst: PositionAnalystFn,
                ) -> AsyncGenerator[ExplainEvent]
    def chat(self, *, system_context: str,
             history: list[ChatMessage], message: str,
             toolkit: ChatToolkit,
             provider_state: str | None = None,
             ) -> AsyncGenerator[ChatEvent]

# LlmConfig is a domain type, populated by config. The factory
# raises if the selected provider needs a key that is None. The
# API layer calls it once per configured agent (`CoachAgent` is an
# LlmConfig subclass) to build the selectable-provider map.
def create_provider(cfg: LlmConfig,
                    api_key: str | None = None) -> CoachProvider

# The concrete classes behind the factory — exported so tests can
# construct one directly and isinstance-check the factory's choice.
# See "Providers" below for what each wraps.
class ClaudeAgentSdkProvider: ...  # claude-agent-sdk (default)
class CopilotSdkProvider: ...      # github-copilot-sdk

# The typed error both providers raise when a run fails (CLI
# missing, run failure, empty output). The API layer maps it to
# 502, or to an SSE error event once a stream has started.
class CoachProviderError(Exception): ...
```

## Report and prompt

`build_report` is pure aggregation over analyzed games. The rules
below are the component's contract, not implementation detail: the
Dashboard reads the same `PlayerReport` and the same `OpeningStats`,
so a rule stated loosely here becomes two implementations that
disagree. See
[coach-report-improvements.md](archive/coach-report-improvements.md)
for why each exists.

### Aggregation is move-weighted, never a mean of means

Every ACPL figure is **total centipawn loss ÷ total player moves**
over the games in scope, computed from the raw `evals` on each
`AnalyzedGame`. A mean of per-game means makes a 15-move loss weigh as
much as a 90-move grind; per-phase, it is worse than that, because
`GameAnalysis.acpl_by_phase` carries `0.0` for phases a game never
reached, and averaging those in drags every endgame figure toward zero
in exact proportion to how rarely the player reaches one.

`PhaseStats.acpl` is therefore `None` — not `0.0` — when `moves` is
zero, and every phase carries its `moves` denominator so a thin sample
is visible rather than inferred.

Phases are re-derived here from the raw evals, replaying the board and
applying the same rule as the engine (`OPENING_PLIES`,
`ENDGAME_MATERIAL`, `PIECE_POINTS` in `domain`, so the constants are
shared even though the two components cannot import each other).
`tests/test_engine_analysis.py` asserts the two implementations agree.

### Repertoire: keyed by the side the player had

An opening is a property of the *game*, not of the player's choice, so
grouping by (eco, name) alone merges "openings I play" with "openings
played against me" and invites advice to stop playing a gambit the
player has only ever faced.

- Rows are keyed by **(color, eco, name)** and rendered under separate
  "As White" / "As Black" headings — never one table.
- `OpeningStats.system` is the player's **own** first three moves,
  written with the move numbers they were played at (`1.d4 2.Nf3
  3.Bg5` as White, `1...d6 2...Nf6 3...g6` as Black). This is the
  repertoire: it is chosen by the player in every game in the group,
  whatever the opponent did.
- `OpeningStats.first_moves` is the same opening with both sides
  answering, six plies (`1.d4 e5 2.dxe5 Nc6 3.Nf3 Qe7`). It states who
  chose what in a form no opening name can obscure, and LLMs read move
  sequences far more reliably than ECO names.
- Both strings describe the group's **representative line**, chosen in
  two tiers so the opponent never decides which system represents the
  player: first the most-played *player-only* move sequence (that is
  what `system` is), then, among only the games sharing it, the
  most-played full line (that is `first_moves`). Ties break on the
  lowest game id at each tier, so the value is deterministic. Picking
  by full line in one pass instead would let a system backed by more
  games lose to one whose opponents happened to reply uniformly.
- They describe that representative line, not an invariant of the
  group: transpositions inside one (color, eco, name) can reach the
  same name by a different move order, so a row's `system` is the
  commonest way the player got there, not the only one.
- `opening_moves` and `player_moves` carry the denominators behind the
  two ACPL columns, so a consumer rolling rows up can stay
  move-weighted (see below).
- `OpeningStats.faced` marks the rows whose name describes the
  **opponent's** choice. Per game, the classification's `Opening.ply`
  is the 1-based ply of the book move that fixed the name — White
  moves are odd plies, Black moves even — so the name is
  opponent-named iff that parity belongs to the opponent: an even
  `ply` when the player is White, an odd one when Black. The Englund
  is named by 1...e5 (ply 2), so a White player's Englund rows are
  faced; the Pirc is named by 1...d6 (ply 2), so a Black player's
  Pirc rows are chosen. Transpositions can reach one name at
  different plies, so per row the flag is a **strict majority** over
  the group's games: `faced` iff opponent-named games × 2 > `games`;
  ties are chosen. Like the rest of these semantics the rule is
  implemented twice — by storage over classified rows and by this
  component over analyzed games — against this one statement, and
  `test_repertoire_agreement.py` keeps the two honest.

**Family rollup.** Consumers (this component's prompt, and the
Dashboard) partition rows by `faced` **before** rolling up. The
chosen partition collapses by **(color, system)**, labelling the
family with its most-played member's name root — the name up to the
first colon. Keying on the player's own moves is what makes the
rollup correct where a name-based key is not: it keeps the London and
the Torre apart though both are named "Queen's Pawn Game", and it
gathers every Pirc under one heading though the opponent's setup
splits the lichess names a dozen ways. The faced partition collapses
by **(color, name root)** instead: for faced lines the name *is* the
opponent's choice, while the player's own system varies with their
replies, so keying it on `system` would split one opposing gambit
across as many families as the player has tried answers to it.

Records sum. **Both ACPL columns re-weight by moves**, not by games
and not by analyzed games: a family's `opening_acpl` is
`Σ(opening_acpl × opening_moves) ÷ Σ opening_moves`, and `avg_cp_loss`
likewise over `player_moves`. Weighting a rollup by game count would
rebuild the mean-of-per-game-means at family level, one step above the
row where it was just removed, and it is why the rows carry their
denominators. Rows whose ACPL is `None` contribute nothing to either
sum or denominator. These summing rules are identical in both
partitions — only the rollup key differs.

Note `analyzed_games` means slightly different things either side:
a genuine sub-count of `games` in storage's SQL, and necessarily equal
to `games` in the coach's, which is only ever handed analyzed games.
It is a coverage figure for the Dashboard, never an ACPL denominator.

**Sample floor and sort.** The main tables require **5+ games**,
applied per partition; below-floor families from *both* partitions
fold into the color section's single long-tail line stating how many
lines and games it covers. Rows sort by **impact** — games ×
win-rate deficit — not by raw win rate, which floats every 0-1-0
singleton above every genuine problem. Each row shows a score
percentage, not only W-L-D.

**Rendering the split.** Each color section renders two sub-tables:
"Systems you chose" (the chosen partition, keyed and labelled as
above) and "What you face as White" / "as Black" (the faced
partition, keyed by name root and rendered with `first_moves` so the
player's reply is visible alongside the opponent's line). The chosen
table is the player's repertoire; the faced table is the coaching
target for "learn a response", never "stop playing this".

### Coverage is stated, not implied

The report aggregates analyzed games only, and the first live run
showed why that must be said out loud: a "last 6 months" request over
1,010 games silently became a report on the 450 recent ones that had
analysis, presented under a window line that made the shrunken span
look like the request. When the caller supplies them, the prompt's
student section states the requested window alongside the covered
span, and renders coverage as "N of M games in scope"; when analysis
covers less than the scope it adds an explicit caveat that the
remaining games are unanalyzed and the figures describe only the
analyzed span — which is what lets the instruction block's honesty
rule actually bite. With no scope information (`None` throughout) the
section renders as it always did.

### Judgment counts carry their denominator

`PlayerReport.player_moves` is the denominator for `judgment_counts`,
and the prompt renders rates alongside counts, per phase as well as
overall. "1,823 blunders" cannot be calibrated; "9.6% of moves, 3.5
per game" can.

### Turning points, not the five biggest numbers

`critical_positions` selects moves where the evaluation crossed a
decision boundary **while the game was still contestable** — a
before-eval within roughly ±3 pawns — capped at one per game and
spread across phases, openings and time classes, weighted toward
recent games. Sorting by raw `cp_loss` instead hands all five slots to
walk-into-mate moves permanently, most of them played in already-lost
positions, crowding out every instructive error.

Each entry carries identity (opponent, date, time class, color,
opening, move number with side) so the prompt can cite "in your game
against marko77 on June 14, 26...Nb6" — opponent and date because
that is how players remember games; findable by the student,
deep-linkable by the UI — and never a list index. It also carries
the plies leading in and the eval either side of the move, so a
blunder that threw away a won game is distinguishable from a coup
de grâce.

The engine's principal variation would be the natural companion to
`best`, but only a live engine call produces a trustworthy one, so
entries still state the move, not a stored refutation line. The
refutation comes from the run instead: each rendered entry includes
the position's FEN, `complete` carries the engine tool (see
Providers), and the instruction block directs the model to verify the
turning points it cites. The FEN is load-bearing — the first live run
proved a tool that takes a FEN is unusable from a prompt that
contains none, and the model correctly asserted nothing new.

### Error patterns are counted, not narrated

`error_patterns` tags blunders by static analysis with python-chess —
never by a model. Anecdotes do not generalize; counts do, and they
cost no tokens. The starting tag set:

| Pattern | Meaning |
|---------|---------|
| `hangs_piece` | after the move, a piece is capturable at a net material loss |
| `hangs_piece_to_check` | as above, where the refutation opens with check |
| `back_rank` | the refutation mates or wins material on the back rank |
| `missed_win` | the position was winning (+3 or better) and is no longer |
| `walks_into_mate` | the move concedes a forced mate |

Each tag must be deterministic and unit-tested. The refutation is the
opponent's `best_move` at the following ply, which the stored evals
already carry.

### Highlights: blunders and brilliancies

`build_highlights` produces the Dashboard's two highlight lists from
already-stored analyses — no re-analysis, no engine calls, no LLM —
and stays **out of `PlayerReport`**: the report feeds the coaching
prompt, and thousands of per-move rows are display data, not coaching
signal.

**Blunders** are simply the player's moves with
`MoveEval.judgment == "blunder"` — the stored judgment is the source
of truth, never re-derived here. An illegal SAN mid-replay ends that
game's walk (the same tolerance `classify` applies in 05), keeping
whatever the walk had collected up to that point.

**Brilliancies** follow chess.com's post-2021 definition — a *sound
piece sacrifice* — approximated over single-PV analyses
(github issue #1). A player move is brilliant iff all four hold,
cutoffs from `BrilliantThresholds`:

1. **Engine-best**: `cp_loss <= best_tolerance_cp` (default 0 — the
   move is the engine's choice or exactly as good).
2. **Real sacrifice**: after the move, the opponent's best static
   exchange anywhere on the board nets at least `sac_points` (default
   2) more than the move itself just captured. SEE is computed with
   python-chess over *legal* captures (pins respected), least-valuable
   attacker first, `PIECE_POINTS` values with a king attacker counting
   as value 0 (it may capture, but a legal king capture implies no
   recapture chain exists); "minus what the move
   captured" is what makes an even trade or a queen-grab-leaving-a-
   knight not a sacrifice, and the 2-point floor is what excludes
   pure pawn sacs — a minor piece or the exchange is the entry bar.
3. **Not already winning**: player-POV eval before the move
   `<= winning_cap_cp` (default +200) — a flashy sac in a decided
   game doesn't count. The before-eval is the previous ply's stored
   eval, folded to the player's POV with mate at `±MATE_SCORE`; the
   game's first move counts as equal.
4. **Still sound after**: player-POV eval after the move
   `>= sound_floor_cp` (default 0).

Known divergence, accepted for v1: single-PV storage cannot tell
whether a *safe* alternative existed, so moves chess.com would call
"Great" (the sac was the only good move) are also awarded here.
Closing that gap needs MultiPV at analysis time and a re-analyzed
archive — recorded in github issue #1, not built.

Both lists carry the citation identity this doc already requires
(date, time class, color, opponent, opening, move number with side)
plus `game_id` and `ply`, so the UI deep-links straight to the move
on the Game page. Evals stay white-POV as stored; the consumer folds
by `color`. Sorting is deterministic: newest game first
(`end_time` desc, ties by `game_id`), then ascending `ply`.

### Prompt

`render_prompt` renders the report into a fixed markdown template —
the student and window, the phase table with denominators, the trend,
how games end, the repertoire split by color (each color as the two
sub-tables above: systems chosen, then lines faced), error patterns,
then the turning points — and closes with the instruction block. The template
is a user-visible artifact (the UI shows it with a copy button), so it
is snapshot-tested; `PROMPT_VERSION` moves with it.

The instruction block states the student (rating band, time controls),
demands **one** biggest lever rather than a flat list of co-equal
weaknesses, and carries the rules the data alone cannot enforce:

- **Attribution.** An opening is the student's only where the
  repertoire lists it under their color in "Systems you chose". Never
  advise dropping a line from the "What you face" table — recommend a
  response to it.
- **Citation.** Game first, move second: name the game by opponent
  and date at its first citation, then give the move in notation as
  the link — "In your game against marko77 on June 14,
  [26...Nb6][g3] …" — with the reference link written through the
  entry's link handle. Never a raw URL, never an invented handle,
  never a list position. Later references to an already-cited game
  may shorten ("that marko77 game"). The opening name appears only
  as coaching content, never as the identifier; time class only when
  the report mixes time controls.
- **Register.** The explain prompt's style contract applies here too:
  a club player, pawns never centipawns, the idea before the number.
- **Honesty.** Say when the data does not support a conclusion instead
  of filling the section anyway.
- **Verification.** When the `analyze_position` tool is available:
  for each turning point the brief features, run the tool on that
  entry's FEN and state the refutation — what the played move loses
  to, not just the better move's name — and check any other concrete
  line before asserting it. Never present an unverified variation as
  fact. The rule is affirmative but scoped to the *cited* positions:
  the run budget affords a handful of engine calls, not one per
  rendered entry. It stays conditional on tool availability because
  the same template serves analyst-less runs — the cache is keyed on
  `PROMPT_VERSION`, not tool availability.

### Game links

Citations must survive the trip through the model without the model
ever writing a URL — game ids are UUID-plus-username strings, and one
mistyped character is a broken link. So the prompt and a
post-processing step split the job:

- Every turning-point entry and error-pattern example carries a short
  **link handle** — `[g1]`, `[g2]`, … — assigned in render order over
  distinct `(game_id, ply)` targets, turning points first, then error
  examples. A move cited by both sections shares one handle.
- The citation rule (above) has the model write each citation as a
  markdown *reference* link through the handle: the game stays named
  in prose by opponent and date, the linked text is the move itself
  ("In your game against marko77 on June 14, [26...Nb6][g3] …"), and
  the handle is all the model must copy.
- `append_game_links` then makes the handles resolve: it normalizes
  inline `[text](gN)` slips to reference style, degrades citations
  with unknown handles to their plain text, and appends one
  `[gN]: /games/{game_id}?ply={ply}` definition per offered handle.
  Definitions the advice never cites are invisible when rendered, so
  over-appending is free.

Link targets deliberately couple to the frontend's Game-page route
and `?ply=` deep link (docs/08-frontend.md) — the exact link the
Dashboard's highlight lists already use. The failure modes all
degrade soft: a model that ignores the rule yields today's plain-text
citation, an invented handle renders as its text (in inline or
reference form alike), and a correct handle can only resolve to a
URL minted from the report itself — model-authored `[gN]:`
definition lines are stripped before the minted block is appended,
so a handle cannot be redefined from inside the advice.

## Player profile

The durable who-is-this-student artifact
(docs/future-improvements/player-profile.md is the design record;
this section is the contract). Every feature that talks to the
student used to re-derive who the student is from scratch; the
profile computes it once, in two layers, and
`render_profile_context` is the payoff — one compact block every
other prompt embeds at the top, one place to improve it.

**Facts.** `build_profile(report)` distills an already-built
`PlayerReport` into `domain.PlayerProfile`: rating and record per
time class, the most recent months of trend, overall and per-phase
quality, the repertoire as `ProfileOpening` rows, and the tagged
error patterns. Pure and free, recomputed on demand; the aggregation
itself runs once, in `build_report` — the profile projects it and
adds no second implementation of any semantic. Distillation rules:

- Repertoire rows reuse the family rollup defined under "Repertoire"
  above — partition by `faced`, chosen rolled up by (color, system),
  faced by (color, name root), move-weighted throughout, the 5+ game
  sample floor applied per partition — then keep the top few
  families per color: chosen rows by games played (what the player
  actually plays), faced rows by impact (what actually hurts them,
  the same games × win-rate-deficit sort the report tables use).
- Every list is capped so the rendered block stays around 250
  tokens; the exact caps are implementation detail, pinned by the
  snapshot tests rather than stated here.

**Narrative.** Three to five sentences of tendencies plus a short
weakness list, every claim tied to a figure the facts state.
`render_profile_prompt(profile)` renders the facts with those
instructions; generation goes through `CoachProvider.complete` with
`analyst=None` — the narrative summarizes aggregates and asserts no
concrete variations, so there is nothing for an engine to verify and
one turn is the whole cost. The explain register applies (club
player, pawns never centipawns). No link handles are offered and the
instructions forbid game citations: the narrative is durable text
embedded into *other* prompts, where a game reference could neither
be resolved into a link nor verified by tools.

Expensive, therefore stored: the API layer persists the narrative —
beside the facts snapshot it described, the agent that wrote it, and
`PROFILE_PROMPT_VERSION` — in storage's `player_profiles` table
(docs/03-storage.md), one row per username, regenerated only on
explicit user action. A regeneration under any agent replaces the
row: the profile is singular, not per-agent. `PROFILE_PROMPT_VERSION`
is row metadata, not a cache key — a bump makes the UI flag the
narrative as stale, and must never trigger a silent re-bill.

**Embedding.** `render_explain_prompt` and `render_game_chat_context`
take `profile: PlayerProfile | None = None`; given one they open
with `render_profile_context(profile)`, and with None they render
exactly as before. The API layer passes the **stored** profile
(facts snapshot with narrative attached — one row read), never a
fresh aggregation: the stored pair is coherent where fresh facts
under an older narrative could contradict each other, and rebuilding
facts would put a full-archive aggregation on every explain call and
chat message. The report prompt and the report-scope chat seed never
embed the block — the report is the profile's own source. Chat seeds
are rebuilt per message, so a new profile reaches every thread's
next message; cached explanations generated before a profile existed
keep serving until `refresh` regenerates them.

## Chat

One backbone, two scopes (docs/future-improvements/coach-chat.md is
the design record; this section is the contract). The API layer owns
threads and transcripts; this component owns the seeds, the tools,
and the provider mechanics.

**Seeds.** `system_context` is rendered per scope. Game scope: the
game's identity (opponent, date, result, time class, opening) and,
when a ply anchor is set, the same `MoveContext` fields and seeded
eval lines the explain prompt uses. Report scope: the report's data
sections — not the coaching-brief instruction block, which is
replaced by the chat instructions below. Both seeds state whether
engine analysis is available (`engine_available`), so the model
never promises a verification it cannot run. Cached explanations and
cached advice are *history*, not seed: the API layer prepends them
as the first assistant turn, so the chat genuinely continues from
what the student just read.

**Tools.** Each `ChatToolkit` capability is exposed to the model as
one tool: `analyze_position` (only when `analyst` is not None),
`find_games`, `get_game`, `get_opening_stats`. The roster is
deliberately small — every tool costs schema tokens on every message
— and read-only by construction. Coach renders every result to text
itself: `find_games` as compact rows (date, color, opponent with
ratings, result, time class, opening, game id), `get_game` as
identity plus a compact move sheet (SAN with judgments; evals in
pawns at the moves that matter), `get_opening_stats` as the
repertoire rows. The explain style contract applies to all of it:
pawns, never centipawns.

**Instructions.** The chat system prompt carries the explain
register rules (club player, the idea before the number, no
redundant annotation) plus two chat-specific rules: claims about the
student's games must come from tool results in this conversation,
never from memory of the seed or of earlier turns; and game
references are written as app-relative markdown links
(`/games/{id}?ply={n}`) using ids returned by tools — never an
invented id (see "Link discipline" in the design record for why
there is no `append_game_links` pass here).

**Replay.** A provider that cannot resume renders the transcript
into its prompt: the shared module helper `render_chat_prompt(
history, message)` formats prior turns (Student:/Coach: blocks,
oldest first) followed by the new message, so every provider replays
identically. Replay is text-level, not block-level — an earlier
turn's tool trace is not reconstructed; its stored final text is the
conversational content.

**Budgets.** `_CHAT_MAX_TURNS` (8) bounds each message's agentic
loop, enforced per provider exactly as the explain and report
budgets are: an SDK `max_turns` hard stop on the Claude provider,
the counted grace-round pattern on Copilot. The stall timeout is
shared with the other flows.

## Providers

- **v1 — `ClaudeAgentSdkProvider`** (default): `complete` runs
  `claude_agent_sdk.query(...)` with a coach system prompt that
  replaces the Claude Code coding persona. Given an analyst it is
  agentic with the same MCP-server mechanics as `explain` below,
  bounded by `_REPORT_MAX_TURNS`; with `analyst=None` it is a
  one-shot `max_turns=1` completion.
  `explain` is an agentic `query(...)`: the injected
  `PositionAnalystFn` is exposed as an `analyze_position(fen)` tool
  on an **in-process SDK MCP server** (`create_sdk_mcp_server` +
  `@tool` — no separate process, no other tools allowed), with a
  small `max_turns` bound; it yields a `text` event per assistant
  text block and a `tool` event per engine call. `chat` runs the
  same agentic mechanics with the full toolkit registered on the
  in-process MCP server, under `_CHAT_MAX_TURNS`. Resumption: the
  SDK reports a session id on each run; the provider returns it as
  `provider_state` on the `done` event, and passes a stored token
  back through the SDK's `resume` option, sending only the new
  message (options — system prompt, MCP server, max_turns — are
  re-supplied on every call, so tools re-register on resume). A
  resume failure before any event has reached the caller falls back
  silently to `render_chat_prompt` replay and hands back whatever
  session id the fresh run reports; one that strikes after partial
  output has already streamed surfaces as `CoachProviderError`
  instead — a silent restart there would duplicate or contradict
  content the student already saw.
  Authentication and billing ride the local Claude Code login —
  **no API key anywhere**; requires the `claude` CLI installed and
  logged in. Errors (CLI missing, run failure, empty output)
  surface as `CoachProviderError`.
- **`CopilotSdkProvider`** (`github-copilot`): the official
  `github-copilot-sdk` package, which bundles and drives the Copilot
  CLI runtime. Authentication and billing ride the local GitHub
  Copilot CLI login (premium requests against the user's Copilot
  seat) — like the Claude provider, **no API key anywhere**. The
  session replaces the Copilot coding persona with the coach system
  prompt. `complete` is one session + one prompt, collecting
  assistant text until the session idles; given an analyst it
  registers the same `analyze_position` custom tool `explain` does
  and enforces the same self-imposed budget, sized by
  `_REPORT_MAX_TURNS` (one grace wrap-up round past the budget, then
  the run is cut off); with `analyst=None` the session gets no tools
  at all. `explain` registers the
  injected `PositionAnalystFn` as a custom `analyze_position` tool
  on the session — built-in Copilot tools (shell, file edits, web)
  are not permitted; only the engine tool may run — and yields a
  `text` event per assistant message and a `tool` event per engine
  call. The SDK has no built-in turn limit, so the provider enforces
  its own budget: engine calls beyond `_EXPLAIN_MAX_TURNS` get one
  wrap-up round (a tool result telling the model to finish with what
  it has), and any engine call after that grace round cuts the run
  off — the generator ends and the session is torn down, rather than
  letting a looping model run indefinitely. `chat` registers the
  full toolkit as custom tools (the only tools the session admits)
  and applies the same counted budget against `_CHAT_MAX_TURNS`.
  Resumption: the provider returns the runtime's session id as
  `provider_state` and resumes it when a stored token names a
  session the runtime still has, sending only the new message;
  otherwise it creates a fresh session and replays via
  `render_chat_prompt`. Errors (runtime missing,
  not logged in, run failure, empty output) surface as
  `CoachProviderError`.
- **Planned — `anthropic`** (the API SDK; needs `ANTHROPIC_API_KEY`)
  and **`azure-foundry`** (the Azure AI Foundry demo, via the
  Anthropic SDK's `AnthropicFoundry` client). Each is one new class
  behind `create_provider` and owns its own tool loop for `explain`
  and `chat`. For `chat` these are the always-replay providers the
  stateless contract exists for: `provider_state` stays None and
  every turn replays the transcript — which is their natural mode,
  and server-side prompt caching keeps the stable prefix cheap.
  Neither is selectable yet: `domain.LlmProvider` lists implemented
  providers only, so config rejects them at load. Shipping one means
  adding its Literal value together with its `create_provider`
  branch — `assert_never` in the factory keeps the two in step.

## Dependencies

- `chess_coach.domain`; `claude-agent-sdk` for the v1 provider;
  `github-copilot-sdk` for the github-copilot provider; python-chess,
  which replays each game to derive turning-point FENs, re-derive
  phases for the move-weighted aggregates, tag error patterns, and
  run the highlights' static exchange evaluation.
- Consumed by the [API layer](07-api.md), which assembles the
  input from [storage](03-storage.md) and injects each configured
  `CoachAgent` from `cfg.coach.agents` (+ the API key once the
  anthropic provider lands). No imports of storage, engine, or
  openings.

## Build plan

1. `build_report` with unit tests on fixture analyses.
2. `render_prompt` with a snapshot test (template stability matters —
   the prompt is a user-visible artifact).
3. `CoachProvider` protocol + `AnthropicProvider` + factory; provider
   test stubs the SDK client.
4. Explain flow: `build_move_context` + `render_explain_prompt`
   (snapshot test) + provider `explain` with a stubbed SDK and a
   stub `PositionAnalystFn` — no real engine or LLM in tests.
