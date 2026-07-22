# Component 1 — Config

Loads and validates all tunable settings once at startup, producing a
single typed `AppConfig` object. Everything configurable lives here;
no other component reads files or environment variables.

## Interface

```ts
function loadConfig(path?: string): AppConfig; // default: coach.config.json

type AppConfig = {
  engine: { depth: number; workers: number };        // depth default 16
  thresholds: { inaccuracy: number; mistake: number; // centipawn loss
                blunder: number };                   // 50 / 100 / 200
  llm: { provider: 'anthropic' | 'azure-foundry';
         model: string; maxTokens: number };
  server: { port: number };
  storage: { dbPath: string };
};
```

Secrets are read from the environment, not the file: `ANTHROPIC_API_KEY`
is required when `llm.provider` is `anthropic`. `loadConfig` fails fast
with a readable error when the file is invalid or a required secret is
missing for the selected provider.

## Dependencies

- None on other components. Uses `zod` for schema validation.
- Consumed by the [server](07-server.md), which injects individual
  values into [engine](04-engine.md) and [coach](06-coach.md). Those
  components receive plain arguments and never import this module.

## Build plan

1. Define the zod schema with defaults so an empty `{}` file works.
2. Implement `loadConfig` (read file, parse, validate, merge env).
3. Ship a commented `coach.config.example.json` in the repo root.
4. Unit tests: defaults, invalid values, missing API key error.
