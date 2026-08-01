# Component 6 — Coach (report, prompt, LLM provider)

Turns analyzed games into a `PlayerReport`, renders the report into a
structured coaching prompt, and sends it to an LLM through a provider
interface. This is the only component that talks to an LLM API.

## Interface

```python
# Pure aggregation — input assembled by the API layer from storage.
# `games` are the analyzed games and carry the quality layer;
# `all_games` is every stored game in the same scope and carries the
# volume layer (see "Volume and quality" below). `all_games=None`
# aggregates volume over the analyzed games alone — the behaviour
# before the split, kept so callers that genuinely have only analyzed
# games read unchanged; callers with the full list must pass it.
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
                 all_games: list[GameSummary] | None = None,
                 time_class: TimeClass | None = None,
                 requested_since: int | None = None,
                 requested_until: int | None = None,
                 games_in_scope: int | None = None) -> PlayerReport
# AnalyzedGame: domain composite = Game + GameAnalysis + Opening|None
# GameSummary: the light row storage returns for lists — carries
#   result, ratings, opening and the 6-ply opening prefix, which is
#   everything the volume layer reads and no PGN.

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
# it, carrying through the report's `time_class` (the profile's own
# scope and storage key) and both denominators. `narrative` stays
# None here; the API layer attaches the stored narrative when one
# exists. `trajectory` covers the full archive and is therefore
# supplied by the caller, which is the only party holding the
# unwindowed months (see "Window").
def build_profile(report: PlayerReport, *,
                  trajectory: RatingTrajectory | None = None,
                  spans_level_change: bool = False) -> PlayerProfile

# The profile window (see "Window"): the epoch second the outcome
# layer should start at, given every month in the archive, oldest
# first. None = no cut, the whole archive is one level. The API layer
# calls this on the *unwindowed* month list, then re-queries with the
# bound — the rule is a coach semantic, so it lives here rather than
# in whichever caller happens to need it.
def profile_window(months: list[MonthStats]) -> int | None

# Full-archive direction, over the same unwindowed months plus the
# volume rows the deltas and the drawdown are measured on. Pure.
def build_trajectory(games: list[GameSummary]) -> RatingTrajectory | None

# The profile's comparison family (see "Reading a comparison"):
# matched buckets in, gaps and BH-adjusted verdicts out. Pure, and
# takes only `Record`s — a bucket's W/D/L carries both its mean and
# its exact variance, so no new aggregation is needed.
def build_comparisons(pairs: list[ComparisonInput]) -> list[Comparison]

WINDOW_DRIFT_POINTS: int   # 200
WINDOW_MAX_MONTHS: int     # 12
WINDOW_MIN_GAMES: int      # the sample floor that overrides drift
COMPARISON_FDR: float      # 0.05, the Benjamini-Hochberg level

# The narrative-generation prompt (snapshot-tested): the facts, plus
# instructions asking for 3-5 sentences of tendencies and a short
# weakness list with every claim tied to a figure the facts state —
# written in the third person, to a coach, naming the time control
# and both denominators (see "Narrative").
def render_profile_prompt(profile: PlayerProfile) -> str

# The compact block other prompts embed at the top -- ~420 tokens of
# facts, ~750 with a narrative attached, measured on a real profile
# (see "Embedding"): a header
# naming the student and the profile's time control, coverage when
# partial, the facts one line each, then the narrative as the coach's
# read when present, block-quoted. Quality figures spell their unit
# ("1.07 pawns lost per move"), as everywhere else (see "Units").
# Total over narrative=None (renders the facts alone).
def render_profile_context(profile: PlayerProfile) -> str

# The narrative's own version constant, independent of PROMPT_VERSION
# (the report template and the profile prompt evolve separately).
# Row metadata, never a cache key: a bump flags staleness in the UI;
# it must never silently re-bill (docs/03-storage.md).
PROFILE_PROMPT_VERSION: str

# --- Milestones (see "Milestones" below) ---
#
# Volume-layer figures on PlayerReport, copied onto PlayerProfile:
#
#   TimeClassStats.rating_max_at / rating_min_at  # dated extremes
#   PlayerReport.color_records: dict[Color, Record]
#   PlayerReport.best_win: BestWin | None
#   PlayerReport.streaks: StreakStats | None
#
# class BestWin(BaseModel):       # the strongest opponent beaten
#     game_id: str; end_time: int; time_class: TimeClass; color: Color
#     opponent: str; opponent_rating: int; player_rating: int
#
# class StreakStats(BaseModel):   # runs, and the rebound after a loss
#     current_result: Result      # the run the newest game belongs to
#     current_length: int
#     longest_win: int; longest_loss: int
#     after_loss: Record          # the next game of the same sitting

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
# context block opens the prompt and the instructions gain one clause
# telling the model to use it (see "Player profile"); with None the
# prompt renders exactly as before.
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
    # The comparison guard, exposed so a run cannot obtain an unjudged
    # percentage (see "Reading a comparison"). Returns the group's
    # record and the rest of `within`, computed by subtraction -- the
    # caller never supplies the other side. `prior_comparisons` seeds
    # the BH family with whatever the profile already judged.
    prior_comparisons: list[Comparison]
    async def compare_games(self, group: ComparisonGroup,
                            within: ComparisonGroup | None = None,
                            ) -> tuple[Record, Record]

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
# verify concrete lines before asserting them. `toolkit` widens that
# to chat's whole read-only roster and is what the profile narrative
# passes (see "Narrative"); it subsumes `analyst`, carrying one of
# its own. With neither it is a single turn, the fallback when no
# engine pool exists.
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
                       analyst: PositionAnalystFn | None = None,
                       *, toolkit: ChatToolkit | None = None) -> str
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
"Systems the student chose" (the chosen partition, keyed and
labelled as above) and "What they face as White" / "as Black" (the faced
partition, keyed by name root and rendered with `first_moves` so the
player's reply is visible alongside the opponent's line). The chosen
table is the player's repertoire; the faced table is the coaching
target for "learn a response", never "stop playing this".

### Coverage is stated, not implied

The report's quality layer covers analyzed games only, and the first
live run showed why that must be said out loud: a "last 6 months"
request over 1,010 games silently became a report on the 450 recent
ones that had analysis, presented under a window line that made the
shrunken span look like the request. When the caller supplies them,
the prompt's student section states the requested window alongside the
covered span, and renders coverage as "N of M games in scope"; when
analysis covers less than the scope it adds an explicit caveat naming
the remaining games — which is what lets the instruction block's
honesty rule actually bite. With no scope information (`None`
throughout) the section renders as it always did.

The caveat **names which figures the shortfall touches**, rather than
saying every figure below describes the analyzed span. That shorter
wording was true when it was written and the next section made it
false: ratings, records, milestones, terminations and the
repertoire's game counts all cover every stored game. Left as it was,
it told the model to discount the half of the brief that is complete.

### Volume and quality

Stating coverage fixed the *narration*; it left the numbers wrong.
Every aggregate was computed over the analyzed subset, including the
ones no engine contributes to — so an archive with 1,209 of 8,156
games analyzed reported a "current rating" from whichever game the
engine happened to reach last, and a win rate over a biased sample.

`build_report` therefore takes two lists and keeps two denominators:

- **Volume**, over `all_games` — every stored game in scope: `record`,
  `time_classes` and their ratings, `months` game counts and closing
  ratings, `terminations`, `opponents`, and the repertoire's
  `games`/`wins`/`losses`/`draws`. None of these need analysis, so
  none may be restricted to it.
- **Quality**, over `games` — the analyzed subset: `overall_acpl`,
  `judgment_counts`, `phases`, `error_patterns`,
  `critical_positions`, the ACPL columns, and `months`
  ACPL/blunder rate. Nothing else can produce these.

`OpeningStats.analyzed_games` has always been declared as "how many of
`games` have engine analysis"; only now can the two differ. A window
with games but no analysis reads `acpl`/`blunder_rate` as `None` —
absent, which is what it is, never `0.0`, which is indistinguishable
from flawless play.

Both lists describe the same scope by construction: the API layer
builds them from `list_analyzed_games` and `list_game_summaries` with
identical filters, whose window semantics storage pins to each other.

### Recent form

The months table answers "what happened in March"; `periods` answers
"how is this student playing *now*", which is the question a profile
exists to answer. `PeriodStats` rows are **trailing windows anchored
to the most recent game in scope** — last 30 days, last 90 days, and
the whole span — each carrying its own volume and quality
denominators.

Three choices worth stating:

- **Nested, not disjoint.** A single month with four analyzed games
  produces an ACPL that swings on one bad game, and a narrative
  reading that wobble as a trend is worse than one reading nothing.
  Nesting means a thin recent window always sits inside a wider one
  that is still more recent than the whole span.
- **Anchored to the data, not the clock.** A student who stopped
  playing three months ago would otherwise get three empty windows
  and a profile that says nothing.
- **Windows that would restate the span are dropped.** With two
  months of history, "last 90 days" and "whole span" are the same
  row, and showing both invites a narrative to read a difference that
  cannot exist.

All three data prompts render the table through one shared function
(`_periods_section`, as `_trend_section` is shared for `months` — the
table is register-free), and the report brief and profile prompt both
instruct the model to lead with the most recent window carrying a
real sample, in preference to a single month's row. The compact embed
block renders one line instead — the narrowest window with analysis,
against the whole-span figure — because the question other prompts
need settled is just "better or worse than usual right now".

The brief went a release without this: `periods` shipped with the
profile and reached only the profile prompt, so every piece of
`/coach` advice averaged the whole span flat while the profile
narrative beside it led with the last 30 days.

### Milestones

Averages describe a student; milestones are what the student
describes themselves by. "Peaked at 1723 last March and has been
below it since", "beat a 1900 in May", "lost four in a row", "scores
39% in the game right after a loss against 48% overall", "loses 38%
of their losses on the clock" — none of these survives being averaged
into an ACPL, and every one of them names a coaching problem or a
piece of evidence a coach can hand back.

All of it is **volume layer**, so all of it covers every stored game
in scope, analyzed or not: beating a 1900 is a fact about the game,
not about whether an engine has looked at it. Computing any of it over
the analyzed subset would reproduce, one level down, the bug the
volume/quality split exists to stamp out.

Four rules worth stating, since each has a wrong-looking alternative:

- **A peak is dated at the first game that reached it.** `max()` over
  a chronological list returns the earliest extreme, which is when the
  student got there — "peaked in March and has not passed it since" is
  only true of the first date. Both extremes are extremes *of the
  games in scope*, never chess.com's own all-time best, which the
  archive does not carry.
- **A run of one is not a run.** The current run is the run the most
  recent game belongs to, where a run is consecutive games with the
  same result (a draw is a run of draws, not a break in one). Both
  renderers word a run of length one as the last game's result rather
  than as "a 1-game winning run", which invites a reader to narrate
  momentum that does not exist.
- **The after-a-loss score is a comparison or it is nothing.** 39% is
  bad only next to a better overall score, so `PlayerProfile.record`
  rides beside it and every renderer states both. `after_loss` counts
  each game whose immediately preceding game in scope was a loss
  ended within two hours — the same sitting, which is where tilt shows
  up. Chained losses each seed the next game, so a six-game slide
  contributes five. On an archive with no back-to-back games it is
  legitimately empty, and an empty record must read as "no sample",
  never as a 0% score.
- **Terminations reach the profile uncapped**, unlike every other list
  it distills: the vocabulary is chess.com's own handful of result
  codes, and a capped list would make the totals its renderers state
  ("62 losses: …") disagree with the record above them.

All three prompts render them: the report brief and the report-scope
chat seed as a "Milestones" section, the profile prompt as
"Milestones and tendencies", both above the shared "How games end"
table.

**Every milestone line is subject-free** — "Best win: beat marko77
(1750) on…" — like every other line of the student section it sits
beside. A subject would have to be a person, and the system prompt
opens "You are a … coach", so "you beat marko77" briefly has two
candidate referents where a label:value line has none. Nothing is
gained by spending that ambiguity.

**One register per document.** Where a subject *is* unavoidable, the
report's data uses the third person — "Systems the student chose",
"Played **7.Qd2**" — matching its own instruction block and the
profile prompt alike. The brief once mixed the two, describing the
student in the third person in its milestones and addressing them in
the second in its repertoire headings and turning points; both
registers are defensible, having both in one document is not. The
data is the side that moved because the instructions could not: a
second person inside an instruction is the model being instructed,
not the student. What the model *writes* is unaffected and stays
second person, stated by the register rule above rather than implied
by a heading.

Subject-free leaves the after-a-loss and color-split lines identical
in both prompts, so there is one implementation of each, taking the
fields rather than either container. Only two lines genuinely differ.

The two that do: the profile's best-win line names **no opponent**,
the report's does. The profile narrative is stored and pasted into
other prompts where a game reference resolves to nothing, so its
citation ban covers opponent handles; the brief's whole citation rule
is "name the game by opponent and date", and a milestone the student
cannot go and find is not a milestone. And the profile's streak line
carries "their", where the report's needs no subject at all.

The compact embed block takes **one** of them — how the student
loses — because that is the only one that changes advice about a
single move: "you were winning and lost on time" is a different
lesson from "you were winning and blundered".

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

### Units

**Every loss figure is in pawns, and nothing is called "ACPL".** One
scale with one name, from the domain types out to the browser tab.

The acronym was doing double duty and a glossary line was papering
over it: both data prompts used to open `*(ACPL = average centipawn
loss per move, shown in pawns…)*`, which expands the acronym to one
scale and then redefines the figure as the other. Prompts followed
the redefinition; the frontend followed the literal meaning; the
same Englund row read `5.16` in the brief and `516` in the
dashboard's repertoire table, and a move that cost `−310` in the
move list was `about 3.1 pawns` in the explanation beside it.

Pawns won for three reasons. A single move's cost cannot be anything
else — the eval bar is pawns everywhere in chess — so unifying on
centipawns was never actually available, only "two scales" under a
different name. The audience arrives from chess.com, where the eval
bar is pawns and ACPL is not house vocabulary. And the coach's own
prose is pawns by style contract, rendered on the same screens as
these numbers.

Where the conversion happens:

- **Nowhere but the render edge.** `MoveEval.cp_loss`, `overall_acpl`,
  `PhaseStats`/`MonthStats`/`PeriodStats.acpl`, `OpeningStats.
  opening_acpl` and `avg_cp_loss` are centipawns, as their field
  comments now say — the engine's own unit, and integers. Aggregation,
  rollups, sorting and chart geometry all stay on those raw numbers,
  where a factor of a hundred cancels; only the step that produces a
  *string* divides.
- **Backend:** `_pawns_or_na` (aggregates, two decimals),
  `format_cp_loss` (one move, one decimal, "about 3.1 pawns"),
  `format_eval` (signed evals), and `providers.py::_pawns` for chat
  tool results. Tool results name the unit beside the number: they
  land mid-conversation with no header above them.
- **Frontend:** `web/src/units.ts` — `formatPawns` and `formatPawnLoss`
  mirror the first two backend helpers, decimals included, so a table
  cell and the advice paragraph under it agree. The conversion itself
  is deliberately not exported: everything leaving that module is a
  string, so no pawn-scale number is ever in flight for something
  downstream to divide a second time.
- **Charts** keep centipawn values and convert only in the axis labels
  and tooltip, which is why `BarChart`/`MonthlyMetricChart` take a
  `formatValue` and apply it to *both*. An axis reading 50 under a
  tooltip reading 0.50 is the same defect at chart scale.

"Centipawns" still appears in the instruction blocks, always as the
thing not to do ("pawns, never centipawns"), and in code comments and
field names where the reader is a developer and precision is the
point. What no template does any more is put the acronym next to a
figure — `test_no_template_says_acpl_anywhere` splits each document at
its instruction block and asserts the data half is clean.

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

- **Audience and register.** The brief is written **to the student,
  in the second person** — the register the UI renders and the one
  thing about person that is not negotiable. The data above it
  describes the student in the third, and the instruction block must
  (its own second person is the model), so the rule states the
  output register outright rather than leaving the data's headings
  to imply it.
- **Attribution.** An opening is the student's only where the
  repertoire lists it under their color in "Systems the student
  chose". Never advise dropping a line from the "What they face"
  table — recommend a response to it.
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

**Scope.** A profile covers **one time control** (`time_class`;
`None` = all controls mixed). A 2100 bullet player and their 1500
rapid self are different students, and one profile averaging both
describes neither — measurably: on a real archive, rapid loses 1.66
pawns a move at 8.9% blunders against bullet's 2.01 and 12.2%. The
scope is carried on the profile, stated by both renderers, and is
storage's key for the narrative. It is deliberately the *only* scope
the narrative carries — see "Narrative" below for why the window is
not.

**Window.** A profile's outcome rates cover the student's **current
level**, not their whole archive. On a developing player the two are
not close: the reference archive runs 185 → 1479 over 1,925 rapid
games, so "54% overall" averages a beginner beating beginners with a
1500 at equilibrium and describes neither.

The window is selected on **time, never on rating**. Filtering games
by a ±rating band is selection on the outcome — games played below
the current rating are disproportionately the ones that were lost —
and on the reference archive a ±100 band keeps 699 games at 52.9%
while deleting 173 at 41.9%, erasing a real 245-point drawdown. The
rating curve therefore picks one **cut point** and every game after
it is kept.

`profile_window(months)` walks back in whole months from the most
recent, extending while `|median(month) − median(newest)| ≤ 200`, and
stops at the first month that exceeds it. Guard rails, each closing a
way the bare rule misreads:

- **Minimum sample.** Below `WINDOW_MIN_GAMES` analyzed games the
  window keeps extending regardless of drift, and the profile carries
  `window_spans_level_change`, which both renderers state — a student
  mid-climb gets an honest caveat instead of a 40-game window.
- **Maximum span** of 12 months, so a settled player does not get a
  five-year window across a changed opponent pool.
- **Thin months** below 30 games cannot set the boundary; the median
  is taken over a trailing 30-game window instead. Otherwise a
  12-game month decides the scope of the whole profile.

Only the outcome layer is windowed. **Trajectory covers the full
archive** (below), because a window is a stationarity assumption and
the thing trajectory exists to report is the opposite.

**Trajectory.** Averages describe a student; direction is what a coach
asks first. `RatingTrajectory` carries the rating now, the change over
the last 30/90/180/365 days with the games behind each, both dated
extremes, and the largest `Drawdown` — peak, trough, the record
through the fall, and whether it has been recovered. Three rules:

- **A drawdown is a field, not a window artifact.** A collapse from
  1574 to 1329 in 23 days is exactly what a stationary window either
  swallows or hides depending on where its edge lands. Reporting it
  explicitly is the only way it survives either outcome.
- **The peak gap is not a headline unless the trend agrees.** Renderers
  suppress it as a lead unless the 90-day *and* 365-day deltas are both
  ≤ 0. "95 below peak" on a student up 443 on the year is a misread,
  and it is the one the first live narrative made.
- **`best_win` is the biggest upset, not the highest-rated opponent.**
  Because chess.com pairs by rating, "highest-rated opponent beaten"
  is structurally the student's own peak — 1559 against a 1574 peak in
  rapid, 1172 against 1162 in blitz — so it restates the ratings table
  two rows above and, on a tight archive, names a *weaker* opponent.
  The largest positive rating gap in a win is a different and genuinely
  earned milestone (+117 in blitz, +169 in bullet). It is `None` when
  no win in scope beat a higher-rated opponent, which on a
  rating-matched archive is common and correct.

**Reading a comparison.** Several profile figures are differences
between two disjoint buckets of the same games — after-a-loss against
not, White against Black, one opening family against the rest. These
are noisy, and a prompt that hands a model the difference and calls it
a coaching problem will get a coin flip narrated as a tendency; the
first live narrative duly hedged one in as "worth watching".

`Comparison` carries both buckets, the gap in percentage points, the
resolution, and a verdict. The rules:

- **Matched baselines.** After-a-loss is compared against games *not*
  after a loss, never against the overall record, which counts the
  after-loss games on both sides of its own comparison.
- **The score's own variance.** A game scores 1 / ½ / 0, so the
  per-game variance is computed from the bucket's W/D/L rather than
  assumed Bernoulli — `Record` carries everything needed, which is why
  no new aggregation is required to produce any of this.
- **Zero is not always the neutral point.** `ComparisonInput.baseline`
  is the gap the null already expects, and the test is on how far the
  observed gap runs past it. It is zero for everything except colour,
  where White scores better than Black for every player alive —
  roughly 4–6 points at amateur online level. Testing a colour gap
  against zero therefore asks whether the student is a chess player,
  and answers yes as soon as the sample is large enough:
  `WHITE_ADVANTAGE_POINTS = 4.0` is a round documented choice like
  `_OPPONENT_BAND`, at the conservative end of that range so a
  genuinely odd split still fires. On the reference archive the gap is
  4.8 points over ~580 games a side and reads as noise either way —
  but at ~1,400 a side a zero null would have called the base rate
  this student's personal weakness. Both renderers say the edge is
  allowed for, since "within noise" against a baseline means "no more
  than everyone has", not "no difference".
- **Benjamini–Hochberg across the profile's whole family.** A profile
  makes up to fourteen of these comparisons; at an unadjusted 2σ that
  is 0.6 expected false positives per profile, so the guard would
  manufacture roughly one spurious tendency every other student. BH
  controls the false-discovery rate, which is the right error to
  control here — a missed tendency costs a bullet, a fabricated one is
  pasted into every later prompt.
- **The verdict is rendered, the arithmetic is not.** Both renderers
  state "within noise" or the plain difference; neither prints sigmas
  or p-values, which are not this audience's vocabulary.

**The guard is a tool, not only a rule.** A run with `find_games` can
slice the archive itself, and a percentage it derives that way arrives
outside the BH family with no verdict attached — the multiple
comparisons problem, reintroduced through the back door by the same
tools that make the narrative worth generating. So the comparison
itself is a tool: `compare_groups` is the only way to obtain a
difference, and everything it returns is already judged.

Three properties make it a guard rather than a convenience:

- **The other side is computed, never supplied.** The model names one
  group and, optionally, the scope to read it against; the tool
  subtracts to get the rest. Two free-form filters would let a run
  compare a group against a set containing it — which is the
  double-counting that made "48% after a loss against 52% overall"
  understate its own gap.
- **A group cannot be named by its outcome.** `ComparisonGroup` takes
  colour, opening, time control and a date window — properties fixed
  before the game was played. It deliberately has no `result` and no
  rating band, because selecting games on the thing being measured is
  what makes a ±100 rating window delete drawdowns and a
  win-rate-conditioned bucket manufacture tilt. `find_games` keeps its
  `result` filter; it answers "show me games", not "is this a
  tendency".
- **The family grows with the asking.** Each call is judged over every
  comparison the profile already made *plus* every one this run has
  requested, so a run that fishes raises its own bar. The result states
  the family size, and it is the honest answer to "can I just ask
  fourteen more ways": yes, and each answer gets harder to earn.

A verdict can therefore change between calls, and that is correct: BH
is a property of the family, so the fourteenth question genuinely does
change what the first one supports.

Tilt stays precomputed. It conditions on the *previous* game's result,
which is not a property of the game being counted and so cannot be
expressed as a group at all.

The SE is closed-form Welch. A session-level block bootstrap over the
reference archive (4,000 resamples, 239 sittings) puts the dependence
inflation at 1.02×, so the closed form is used in production and stays
deterministic; that measurement is the justification, recorded here
rather than re-run.

**Facts.** `build_profile(report)` distills an already-built
`PlayerReport` into `domain.PlayerProfile`: rating and record per
time class with the extremes dated, the most recent months of trend,
the recent-form `periods`, overall and per-phase quality, the
milestones (see "Milestones" above — `record`, `color_records`,
`best_win`, `streaks`, `opponents`, `terminations`, all copied
verbatim), the repertoire as
`ProfileOpening` rows, and the tagged error patterns. Pure and free,
recomputed on demand; the aggregation itself runs once, in
`build_report` — the profile projects it and adds no second
implementation of any semantic, which is why the volume/quality split
lives there and reaches the profile for free. Both denominators come
with it: `games_covered` is the analyzed sample behind the quality
figures, `games_in_scope` every stored game behind the volume ones.
Distillation rules:

- Repertoire rows reuse the family rollup defined under "Repertoire"
  above — partition by `faced`, chosen rolled up by (color, system),
  faced by (color, name root), move-weighted throughout, the 5+ game
  sample floor applied per partition — then keep the top few
  families per color: chosen rows by games played (what the player
  actually plays), faced rows by impact (what actually hurts them,
  the same games × win-rate-deficit sort the report tables use).
- Every list is capped to hold the rendered block down,
  `terminations` excepted for the reason given under "Milestones";
  the exact caps are implementation detail, pinned by the snapshot
  tests rather than stated here. The block was ~250 tokens when the
  profile shipped and is ~420 now, the trajectory line being most of
  the growth — worth knowing before adding another, since this is
  paid on every explain call and every chat message, not once.
- Fields added to `PlayerProfile` after the first release carry
  empty-ish defaults, so a snapshot stored under an older shape still
  parses. The embed paths read stored rows (see "Embedding"), and a
  required new field would turn every one of them into a validation
  error until the student paid to regenerate.

**Narrative.** Three to five sentences of tendencies plus a short
weakness list, every claim tied to a figure the facts state.
`render_profile_prompt(profile)` renders the facts with those
instructions; generation goes through `CoachProvider.complete` with
`analyst=None` — the narrative summarizes aggregates and asserts no
concrete variations, so there is nothing for an engine to verify and
one turn is the whole cost. No link handles are offered and the
instructions forbid game citations: the narrative is durable text
embedded into *other* prompts, where a game reference could neither
be resolved into a link nor verified by tools.

It is written **in the third person, to a coach** — never to the
student. This is the same fact as the citation rule, applied to
person: the text is stored and pasted into other prompts, where the
reader is another coach, so a v1 narrative opening "You are a rapid
player who hangs pieces" told that coach *they* were the rapid
player. The instructions forbid the second person and the rendered
facts follow suit, since a model copies the register it is given.
What differs from the brief is the *output*: the narrative is third
person because a coach reads it, the brief second person because the
student does. Their data reads the same way in both. The register
otherwise matches explain: club player, pawns never centipawns.

**The instructions say what the text is for, and then get out of the
way** (`profile-v5`). Until then they ran to twelve bullets
prescribing shape, and never once said what the narrative was *for* —
so the model optimized the only thing it had been given, and the
repertoire, which no bullet named, lost the sentence budget in every
run. Twelve rules could not make it mention the openings; one
sentence of purpose does, because a text written to be *useful in
another session* has to say what the student plays.

So the block now opens with the job — write the context that will be
pasted in when another coach explains a move or answers a question,
so write what would change the advice — and keeps only the rules
nothing else can supply:

- **Third person, to a coach.** The register rule above.
- **No game citations, dates, opponents, links or handles.** They
  resolve to nothing where this text lands.
- **No markdown headings**, since it lands *inside* another prompt's
  sections where a heading of its own reads as starting a new one.
  Belt and braces beside the block quote the embed applies: quoting
  bounds whatever arrives, the rule stops it arriving.
- **Spell every unit out** — "1.30 pawns a move", never "1.30 ACPL",
  per "Units" below.
- **An observation from reading games is an example, never a
  tendency**, and says how many games it rests on. `compare_groups`
  guards differences *between buckets*; it says nothing about
  inference drawn from reading individual games, which is the second
  path the tools opened. The first live narrative walked down it,
  reading a few collapsed endgames as "a composure/reset issue" — an
  unguarded psychological claim, two paragraphs above its own correct
  statement that there is no measurable tilt effect.
- **Dense, not polished, around 200 words.** The first live narrative
  ran to 619 words — about 950 tokens — which the embed then pastes
  into every explain prompt and every game-scope chat message, against
  a facts block that is itself ~420. Dropping "three to five
  sentences" removed the only bound on length, and the model spent the
  room on connective tissue: every figure acquired a sentence
  explaining its significance to a reader who is a coach and can see
  it. The rule is density rather than a shape, since a shape
  prescription is what the rework removed.
- **A comparison marked "within noise" is not a tendency.**

Everything cut was either a shape constraint the purpose statement
implies, or an instruction to say something the facts already say.
Recency is the clearest case: it was a bullet ("lead with the most
recent window"), and it is now the *window*, which is where it
belongs. A fact the data enforces needs no rule.

**Generation is designed to be agentic**, and the template is ready
for it: `render_profile_prompt(profile, has_tools=...)` swaps one
clause between "the facts are everything you have" and "use the tools
to check anything the summary rests on". A tool-less run told to use
tools either invents the lookups or spends its turn explaining that
it cannot, which is why the clause is conditional rather than
aspirational — the same shape, and the same reason, as
`render_explain_prompt`'s profile clause.

`complete` therefore takes an optional `toolkit: ChatToolkit`
alongside its analyst, and with one registers chat's whole read-only
roster under `_REPORT_MAX_TURNS` — the same in-process MCP mechanics
chat uses, and the same mechanics the report brief has had since it
gained the engine tool, which is why the brief is the best text this
system produces. A `toolkit` subsumes an `analyst`, since it carries
one of its own; passing neither is still today's single turn, which
is what every other `complete` caller does.

The facts block is then the starting point and not the limit: it
exists so the run does not spend turns re-deriving, badly, what
aggregation already computed correctly.

The toolkit the API hands it is scoped to **the same window as the
facts**. It was unwindowed at first, on the reasoning that the
narrative covers the control's whole history — which confused the
storage *key* (time control alone, see "Why time control keys it")
with the scope of what the text describes. The narrative describes the
windowed facts, so an unwindowed tool answers a different question
from the one the document is about.

That is not theoretical: live, `get_opening_stats` returned a 484-game
London over the whole 1,925-game archive into a narrative whose every
other figure covered 1,158, and `compare_groups` returned a 968-game
White split beside a facts block stating 576 — two colour splits in
one document. **One document, one denominator.** It is the same
volume/quality denominator defect the report layer already fixed,
reappearing one level up in the seam between the facts and the tools,
and worth checking for whenever a new tool is added: the guard covers
*comparisons*, and nothing else covers bare *counts*.

One risk comes with the tools and is worth stating where the rule
lives: **a run that slices the data itself produces comparisons with
no verdict attached**, which is the multiple-comparisons problem
outside the BH family that "Reading a comparison" corrects for. The
instruction covers it for now; the durable fix is to expose the
comparison itself as a tool, so every difference the model can obtain
already carries its verdict. Not built — recorded here so the next
person to widen the toolkit does it in the right direction.

A bump only flags stored narratives stale in the UI — it never
re-bills on its own — so the standing rule is to bank cheap
durable-text rules and spend one bump on the lot.

Expensive, therefore stored: the API layer persists the narrative —
beside the facts snapshot it described, the agent that wrote it, and
`PROFILE_PROMPT_VERSION` — in storage's `player_profiles` table
(docs/03-storage.md), one row per (username, time control),
regenerated only on explicit user action. A regeneration under any
agent replaces the row: the profile is keyed by scope, not by agent.
`PROFILE_PROMPT_VERSION` is row metadata, not a cache key — a bump
makes the UI flag the narrative as stale, and must never trigger a
silent re-bill.

**Why time control keys it and the window does not.** The facts
honour a window filter like every other view; the narrative does not,
and is always generated over the control's full history. `since` is
quantized to the current day, so keying the narrative on it would
mint a fresh key every midnight — stranding every stored narrative
and leaving the embed paths reading nothing, which is precisely the
durability this artifact exists for. Time control is stable, and is
the dimension along which a student actually differs from themselves.
The cost is that under a window the facts and the narrative describe
different spans; the API states the narrative's own live count so the
UI can label both honestly rather than flagging every windowed view
as stale.

**Embedding.** `render_explain_prompt` and `render_game_chat_context`
take `profile: PlayerProfile | None = None`; given one they open
with `render_profile_context(profile)`, and with None they render
exactly as before. The block names its own student and scope in the
header — without the scope, an embedded rapid profile reads as a
description of the whole player; without the name, the header's own
"their" refers to nobody, since it is the first line of the host
prompt and the block never names the student anywhere else.

Explain also **says what the block is for**: with a profile its
instructions gain one clause — pitch the explanation at the student
the profile describes, and name the move as an instance of a pattern
the profile already counts when it is one. Context a prompt never
refers to is context a model may ignore, and that clause is the
whole embed's payoff. It renders only alongside the block, so the
profile-less prompt is unchanged and the instruction never points at
a section that is not there. One clause and no more: these
instructions are the one part of the explain prompt on a strict
length budget, and the profile is context, not the subject. Chat
needs no equivalent — its seed rule now names the context as usable
directly.

The narrative is embedded **block-quoted**. It is model-written, so
an unquoted narrative opening `## Tendencies` would forge a section
boundary in the host and hand every section after it to the
narrative. The instructions do now forbid headings (see "Narrative"),
but that only binds text written under `profile-v4` and later:
narratives stored before it are still out there, and a rule is a
model obeying an instruction while quoting is arithmetic on a
string. Quoting bounds whatever actually arrives.

The API layer passes the **stored** profile (facts snapshot with
narrative attached — one row read), never a fresh aggregation: the
stored pair is coherent where fresh facts under an older narrative
could contradict each other, and rebuilding facts would put a
full-archive aggregation on every explain call and chat message.
Which row: the one for **the game's own time control**, falling back
to the all-controls row — a bullet game should be explained to a
coach who knows this student's bullet tendencies, and the fallback is
what a student who only ever generated the mixed profile has. Both
embed paths resolve it through one helper (`api/chat.py`'s
`profile_for_game`) so they cannot disagree.

The report prompt and the report-scope chat seed never
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
redundant annotation) plus two chat-specific rules: the facts the
seed states are established and may be used and quoted, while any
claim past them — another game, another result, a move not shown —
must come from a tool result in this conversation, never from
memory; and game references are written as app-relative markdown
links (`/games/{id}?ply={n}`) using ids returned by tools — never an
invented id (see "Link discipline" in the design record for why
there is no `append_game_links` pass here).

The first rule is *stated versus recalled*, not context versus
tools, and the distinction is load-bearing. Scoped to the whole
context it bans the seed: report scope ships ~1,650 tokens of
ratings, record, repertoire and turning points and then forbids all
of it, and on a thread's first message no tool has run, so the model
may assert nothing whatever about the student it was just briefed
on. Game scope additionally loses the anchored game's own result,
opponent and played move, which the seed states three lines above
the rule. The invented-game risk the rule exists for lives entirely
past the seed's edge, which is where it now sits. Game *links* stay
tool-only regardless: the seed's own game is already linked by the
UI, so an id has no legitimate source but a tool result.

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

**One persona, three artifacts.** `create_provider` builds a single
provider carrying a single system prompt, and that provider serves
the report brief, the profile narrative and move explanations alike.
So the persona names none of them: it establishes the coach, states
that the figures it is handed are already computed, and defers what
to write to the instruction block each template ends with. A persona
that named one artifact would misdirect the other two — a move
explanation is not a brief and re-averages nothing, and the
narrative is a briefing *about* the student for another coach, which
its own instructions say and a "write the coaching brief" persona
would contradict. Chat is the one genuine divergence and keeps its
own persona (`CHAT_SYSTEM_PROMPT`): its instructions arrive in the
seed, not in a block at the end, and the turns after the first are a
conversation rather than a request for a finished piece.

**The answer is what the model writes last.** Every agentic path —
the report brief, chat, and the explain stream the API layer caches —
collects assistant text as it arrives, and a model about to call a
tool narrates first ("I'll verify the turning points before
writing"). Left in, that narration was concatenated onto the front of
the finished piece, without even a separator, on every run that used
a tool. So **a tool call discards the text collected before it**:
what the model writes after its last tool call is the answer, and an
empty result falls through to the run's own final message exactly as
before. Streaming is unaffected — the narration still reaches the UI
as its own event, so the student watches the coach work; only the
accumulated string, the one that gets cached and replayed into later
prompts, drops it. Two details are not incidental. The Copilot
providers clear on every call *except* a budget `cutoff`, which is
salvaging a runaway run rather than returning a clean answer and
keeps whatever text exists. And Copilot's chat clears inside the tool
handler rather than where the caller drains the event queue, because
text is accumulated at enqueue time: by the time a consumer dequeued
the tool event, text belonging *after* it could already have arrived.

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
