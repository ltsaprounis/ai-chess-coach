# Component 6 — Coach (report, prompt, LLM provider)

Turns analyzed games into a `PlayerReport`, renders the report into a
structured coaching prompt, and sends it to an LLM through a provider
interface. This is the only component that talks to an LLM API.

## Interface

```python
# Pure aggregation — input assembled by the API layer from storage.
# `time_class` is the filter the caller applied, recorded so the
# prompt can state the scope of its own numbers; the window bounds are
# derived from the games themselves.
def build_report(username: str, games: list[AnalyzedGame], *,
                 time_class: TimeClass | None = None) -> PlayerReport
# AnalyzedGame: domain composite = Game + GameAnalysis + Opening|None

# Deterministic markdown template, also shown/copyable in the UI.
def render_prompt(report: PlayerReport) -> str

# Bumped whenever the template changes materially. The API layer keys
# the report cache on it, so a reworded prompt invalidates old advice
# instead of serving it against a template that no longer exists.
PROMPT_VERSION: str

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
# pawns for the same reason.
def render_explain_prompt(ctx: MoveContext,
                          lines: list[EvalLine]) -> str

# The engine seam. The API layer injects a callable wrapping the
# engine pool (components never import each other); depth and
# multipv are the injector's choice. FEN in, final lines out.
PositionAnalystFn = Callable[[str], Awaitable[list[EvalLine]]]

class ExplainEvent(BaseModel):     # one streamed explain increment
    type: Literal["text", "tool"]
    text: str                      # text chunk | tool-call summary

# The provider seam — everything LLM-specific hides behind this.
class CoachProvider(Protocol):
    async def complete(self, prompt: str) -> str
    def explain(self, prompt: str, analyst: PositionAnalystFn,
                ) -> AsyncGenerator[ExplainEvent]

# LlmConfig is a domain type, populated by config. The factory
# raises if the selected provider needs a key that is None. The
# API layer calls it once per configured agent (`CoachAgent` is an
# LlmConfig subclass) to build the selectable-provider map.
def create_provider(cfg: LlmConfig,
                    api_key: str | None = None) -> CoachProvider
```

## Report and prompt

`build_report` is pure aggregation over analyzed games. The rules
below are the component's contract, not implementation detail: the
Dashboard reads the same `PlayerReport` and the same `OpeningStats`,
so a rule stated loosely here becomes two implementations that
disagree. See [COACH-REPORT-IMPROVEMENTS.md](COACH-REPORT-IMPROVEMENTS.md)
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

**Family rollup.** Consumers (this component's prompt, and the
Dashboard) collapse rows by **(color, system)**, labelling the family
with its most-played member's name root — the name up to the first
colon. Keying on the player's own moves is what makes the rollup
correct where a name-based key is not: it keeps the London and the
Torre apart though both are named "Queen's Pawn Game", and it gathers
every Pirc under one heading though the opponent's setup splits the
lichess names a dozen ways.

Records sum. **Both ACPL columns re-weight by moves**, not by games
and not by analyzed games: a family's `opening_acpl` is
`Σ(opening_acpl × opening_moves) ÷ Σ opening_moves`, and `avg_cp_loss`
likewise over `player_moves`. Weighting a rollup by game count would
rebuild the mean-of-per-game-means at family level, one step above the
row where it was just removed, and it is why the rows carry their
denominators. Rows whose ACPL is `None` contribute nothing to either
sum or denominator.

Note `analyzed_games` means slightly different things either side:
a genuine sub-count of `games` in storage's SQL, and necessarily equal
to `games` in the coach's, which is only ever handed analyzed games.
It is a coverage figure for the Dashboard, never an ACPL denominator.

**Sample floor and sort.** The main table requires **5+ games**;
everything below collapses into a single long-tail line stating how
many lines and games it covers. Rows sort by **impact** — games ×
win-rate deficit — not by raw win rate, which floats every 0-1-0
singleton above every genuine problem. Each row shows a score
percentage, not only W-L-D.

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

Each entry carries identity (date, time class, color, opening, move
number with side) so the prompt can cite "your 26...Nb6 in the June 14
blitz Pirc" — findable by the student, deep-linkable by the UI — and
never a list index. It also carries the plies leading in and the eval
either side of the move, so a blunder that threw away a won game is
distinguishable from a coup de grâce.

The engine's principal variation would be the natural companion to
`best`, but the report path has no engine tool (finding 9, not yet
built), so entries state the move, not the refutation line.

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

### Prompt

`render_prompt` renders the report into a fixed markdown template —
the student and window, the phase table with denominators, the trend,
how games end, the repertoire split by color, error patterns, then the
turning points — and closes with the instruction block. The template
is a user-visible artifact (the UI shows it with a copy button), so it
is snapshot-tested; `PROMPT_VERSION` moves with it.

The instruction block states the student (rating band, time controls),
demands **one** biggest lever rather than a flat list of co-equal
weaknesses, and carries the rules the data alone cannot enforce:

- **Attribution.** An opening is the student's only where the
  repertoire lists it under their color as a system they chose. Never
  advise dropping an opening they only face — recommend a response.
- **Citation.** Refer to positions by date and move number, never by
  list position.
- **Register.** The explain prompt's style contract applies here too:
  a club player, pawns never centipawns, the idea before the number.
- **Honesty.** Say when the data does not support a conclusion instead
  of filling the section anyway.

## Providers

- **v1 — `ClaudeAgentSdkProvider`** (default): `complete` is a
  one-shot `claude_agent_sdk.query(...)` with `max_turns=1` and a
  coach system prompt that replaces the Claude Code coding persona.
  `explain` is an agentic `query(...)`: the injected
  `PositionAnalystFn` is exposed as an `analyze_position(fen)` tool
  on an **in-process SDK MCP server** (`create_sdk_mcp_server` +
  `@tool` — no separate process, no other tools allowed), with a
  small `max_turns` bound; it yields a `text` event per assistant
  text block and a `tool` event per engine call. Authentication and
  billing ride the local Claude Code login — **no API key
  anywhere**; requires the `claude` CLI installed and logged in.
  Errors (CLI missing, run failure, empty output) surface as
  `CoachProviderError`.
- **`CopilotSdkProvider`** (`github-copilot`): the official
  `github-copilot-sdk` package, which bundles and drives the Copilot
  CLI runtime. Authentication and billing ride the local GitHub
  Copilot CLI login (premium requests against the user's Copilot
  seat) — like the Claude provider, **no API key anywhere**. The
  session replaces the Copilot coding persona with the coach system
  prompt. `complete` is one session + one prompt, collecting
  assistant text until the session idles. `explain` registers the
  injected `PositionAnalystFn` as a custom `analyze_position` tool
  on the session — built-in Copilot tools (shell, file edits, web)
  are not permitted; only the engine tool may run — and yields a
  `text` event per assistant message and a `tool` event per engine
  call. The SDK has no built-in turn limit, so the provider enforces
  its own budget: engine calls beyond `_EXPLAIN_MAX_TURNS` get one
  wrap-up round (a tool result telling the model to finish with what
  it has), and any engine call after that grace round cuts the run
  off — the generator ends and the session is torn down, rather than
  letting a looping model run indefinitely. Errors (runtime missing,
  not logged in, run failure, empty output) surface as
  `CoachProviderError`.
- **Planned — `anthropic`** (the API SDK; needs `ANTHROPIC_API_KEY`)
  and **`azure-foundry`** (the Azure AI Foundry demo, via the
  Anthropic SDK's `AnthropicFoundry` client). Each is one new class
  behind `create_provider` and owns its own tool loop for `explain`;
  selecting one before it ships raises a clear `CoachProviderError`.

## Dependencies

- `chess_coach.domain`; `claude-agent-sdk` for the v1 provider;
  `github-copilot-sdk` for the github-copilot provider; python-chess,
  which replays each game to derive turning-point FENs, re-derive
  phases for the move-weighted aggregates, and tag error patterns.
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
