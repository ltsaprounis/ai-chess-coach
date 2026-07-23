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

# The provider seam — everything LLM-specific hides behind this.
class CoachProvider(Protocol):
    async def complete(self, prompt: str) -> str

# LlmConfig is a domain type, populated by config. The factory
# raises if the selected provider needs a key that is None. The
# API layer calls it once per configured agent (`CoachAgent` is an
# LlmConfig subclass) to build the selectable-provider map.
def create_provider(cfg: LlmConfig,
                    api_key: str | None) -> CoachProvider
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

- **v1 — `ClaudeAgentSdkProvider`** (default): a one-shot
  `claude_agent_sdk.query(...)` with `max_turns=1` and a coach
  system prompt that replaces the Claude Code coding persona.
  Authentication and billing ride the local Claude Code login —
  **no API key anywhere**; requires the `claude` CLI installed and
  logged in. Errors (CLI missing, run failure, empty output)
  surface as `CoachProviderError`.
- **Planned — `anthropic`** (the API SDK; needs `ANTHROPIC_API_KEY`)
  and **`azure-foundry`** (the Azure AI Foundry demo, via the
  Anthropic SDK's `AnthropicFoundry` client). Each is one new class
  behind `create_provider`; selecting one before it ships raises a
  clear `CoachProviderError`.

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
