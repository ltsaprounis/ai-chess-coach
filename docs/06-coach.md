# Component 6 — Coach (report, prompt, LLM provider)

Turns analyzed games into a `PlayerReport`, renders the report into a
structured coaching prompt, and sends it to an LLM through a provider
interface. This is the only component that talks to an LLM API.

## Interface

```python
# Pure aggregation — input assembled by the API layer from storage.
def build_report(username: str, games: list[AnalyzedGame]) -> PlayerReport
# AnalyzedGame: domain composite = Game + GameAnalysis + Opening|None

# Deterministic markdown template, also shown/copyable in the UI.
def render_prompt(report: PlayerReport) -> str

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

`build_report` computes overall/per-phase ACPL, judgment counts,
per-opening records (games, W/L/D, average cp_loss, worst first), and
the top-N critical positions (largest cp_loss) as FEN + played/best
moves. `render_prompt` wraps that in a fixed template: a coach role
instruction, the stats as compact markdown tables, the critical
positions, and a closing instruction asking for prioritized, concrete
training advice.

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
- **Planned — `anthropic`** (the API SDK; needs `ANTHROPIC_API_KEY`)
  and **`azure-foundry`** (the Azure AI Foundry demo, via the
  Anthropic SDK's `AnthropicFoundry` client). Each is one new class
  behind `create_provider` and owns its own tool loop for `explain`;
  selecting one before it ships raises a clear `CoachProviderError`.

## Dependencies

- `chess_coach.domain`; `claude-agent-sdk` for the v1 provider;
  python-chess (replaying moves to FEN for critical positions).
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
