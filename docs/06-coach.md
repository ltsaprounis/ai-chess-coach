# Component 6 — Coach (report, prompt, LLM provider)

Turns analyzed games into a `PlayerReport`, renders the report into a
structured coaching prompt, and sends it to an LLM through a provider
interface. This is the only component that talks to an LLM API.

## Interface

```ts
// Pure aggregation — input is assembled by the server from storage.
function buildReport(
  games: Array<Game & { analysis: GameAnalysis; opening?: Opening }>
): PlayerReport;

// Deterministic markdown template, also shown/copyable in the UI.
function renderPrompt(report: PlayerReport): string;

// The provider seam — everything LLM-specific hides behind this.
interface CoachProvider {
  complete(prompt: string): Promise<string>;
}
function createProvider(cfg: AppConfig['llm'],
                        apiKey: string): CoachProvider;
```

## Report and prompt

`buildReport` computes overall/per-phase ACPL, judgment counts,
per-opening records (games, W/L/D, average cpLoss, worst first), and
the top-N critical positions (largest cpLoss) as FEN + played/best
moves. `renderPrompt` wraps that in a fixed template: a coach role
instruction, the stats as compact markdown tables, the critical
positions, and a closing instruction asking for prioritized, concrete
training advice.

## Providers

- **v1 — `AnthropicProvider`**: `@anthropic-ai/sdk`, model from
  config (default `claude-opus-4-8`), streaming with
  `finalMessage()`; API key injected, never read from `process.env`
  here (see [config](01-config.md)).
- **Later — `azure-foundry`**: the planned Azure AI Foundry demo.
  The Anthropic SDK's `AnthropicFoundry` client covers Claude-on-
  Foundry; other Foundry-hosted models would be one new class
  implementing `CoachProvider`. Nothing outside `createProvider`
  changes either way.

## Dependencies

- `shared/types.ts`; `@anthropic-ai/sdk` for the v1 provider.
- Consumed by the [server](07-server.md), which assembles the input
  from [storage](03-storage.md) and injects `cfg.llm` + the API key.
  No imports of storage, engine, or openings.

## Build plan

1. `buildReport` with unit tests on fixture analyses.
2. `renderPrompt` with a snapshot test (template stability matters —
   the prompt is a user-visible artifact).
3. `CoachProvider` + `AnthropicProvider` + factory; provider test
   mocks the SDK client.
