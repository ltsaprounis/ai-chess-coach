# Component 6 — Coach (report, prompt, LLM provider)

Turns analyzed games into a `PlayerReport`, renders the report into a
structured coaching prompt, and sends it to an LLM through a provider
interface. This is the only component that talks to an LLM API.

## Interface

```python
# Pure aggregation — input assembled by the API layer from storage.
def build_report(games: list[AnalyzedGame]) -> PlayerReport
# AnalyzedGame = Game + GameAnalysis + Opening | None

# Deterministic markdown template, also shown/copyable in the UI.
def render_prompt(report: PlayerReport) -> str

# The provider seam — everything LLM-specific hides behind this.
class CoachProvider(Protocol):
    async def complete(self, prompt: str) -> str

def create_provider(cfg: LlmConfig, api_key: str) -> CoachProvider
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

- **v1 — `AnthropicProvider`**: the `anthropic` package
  (`AsyncAnthropic`), model from config (default `claude-opus-4-8`),
  streaming via `client.messages.stream(...)` +
  `get_final_message()`; API key injected, never read from
  `os.environ` here (see [config](01-config.md)).
- **Later — `azure-foundry`**: the planned Azure AI Foundry demo.
  The Anthropic SDK's `AnthropicFoundry` client covers Claude-on-
  Foundry; other Foundry-hosted models would be one new class
  implementing `CoachProvider`. Nothing outside `create_provider`
  changes either way.

## Dependencies

- `chess_coach.domain`; the `anthropic` SDK for the v1 provider.
- Consumed by the [API layer](07-server.md), which assembles the
  input from [storage](03-storage.md) and injects `cfg.llm` + the
  API key. No imports of storage, engine, or openings.

## Build plan

1. `build_report` with unit tests on fixture analyses.
2. `render_prompt` with a snapshot test (template stability matters —
   the prompt is a user-visible artifact).
3. `CoachProvider` protocol + `AnthropicProvider` + factory; provider
   test stubs the SDK client.
