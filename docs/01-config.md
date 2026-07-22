# Component 1 — Config

Loads and validates all tunable settings once at startup, producing a
single typed `AppConfig` object. Everything configurable lives here;
no other component reads configuration or environment variables —
components touch data files (TSVs, the DB, the engine binary) only
at paths injected from this config.

## Interface

```python
def load_config(path: Path = Path("coach.config.yaml")) -> AppConfig

class AppConfig(BaseModel):
    engine: EngineConfig        # depth=16, workers=2;
                                # bin_path None -> repo submodule build
    thresholds: Thresholds      # centipawn loss: inaccuracy=50,
                                # mistake=100, blunder=200
    llm: LlmConfig              # provider: "anthropic" |
                                # "azure-foundry"; model; max_tokens
    server: ServerConfig        # port
    storage: StorageConfig      # db_path
    openings: OpeningsConfig    # book_dir; None -> repo submodule
    anthropic_api_key: str | None   # from env, never from the file
```

`Thresholds` and `LlmConfig` are domain types — [engine](04-engine.md)
and [coach](06-coach.md) consume them too. The other sub-models
(`EngineConfig`, `ServerConfig`, `StorageConfig`) are config-local.

Secrets come from the environment, not the file: `ANTHROPIC_API_KEY`
is required when `llm.provider` is `anthropic`. `load_config` fails
fast with a readable error when the file is invalid or a required
secret is missing for the selected provider.

## Dependencies

- `chess_coach.domain` (`Thresholds`, `LlmConfig`). Libraries:
  pydantic v2 and PyYAML (`yaml.safe_load` → model validation).
- Consumed by the [API layer](07-api.md), which injects individual
  values into [engine](04-engine.md) and [coach](06-coach.md). Those
  components receive plain arguments and never import this module.

## Build plan

1. Pydantic models with defaults so an empty `{}` file works.
2. `load_config` (read YAML, validate, merge env secrets).
3. Ship a commented `coach.config.example.yaml` in the repo root.
4. Unit tests: defaults, invalid values, missing API key error.
