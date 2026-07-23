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
    engine: EngineConfig        # depth=16, workers=2, analyze_limit
                                # =100 (cap per analyze run);
                                # bin_path None -> repo submodule build
    thresholds: Thresholds      # centipawn loss: inaccuracy=50,
                                # mistake=100, blunder=200
    coach: CoachConfig          # selectable coach agents (below)
    server: ServerConfig        # port
    storage: StorageConfig      # db_path
    openings: OpeningsConfig    # book_dir; None -> repo submodule
    anthropic_api_key: str | None   # from env, never from the file

class CoachConfig(BaseModel):
    # Roster of selectable coach agents shown in the UI. Each entry
    # is a domain `CoachAgent` (id + label + the LlmConfig fields).
    # Defaults to a single claude-agent-sdk agent (id "claude").
    agents: list[CoachAgent]
    default_agent: str          # resolved: omitted -> first agent id
```

`Thresholds`, `LlmConfig`, and `CoachAgent` (an `LlmConfig` subclass
adding `id` + `label`) are domain types — [engine](04-engine.md),
[coach](06-coach.md), and the [API layer](07-api.md) consume them
too. The other sub-models (`EngineConfig`, `CoachConfig`,
`ServerConfig`, `StorageConfig`) are config-local.

Validation beyond field types: agent ids must be non-empty and
unique, `agents` must not be empty, and `default_agent` must match a
configured id (when omitted it resolves to the first agent's id).
The retired top-level `llm:` key fails fast with a migration hint
pointing at `coach.agents`.

Secrets come from the environment, not the file: `ANTHROPIC_API_KEY`
is required only when some agent's `provider` is `anthropic`. The
default provider (`claude-agent-sdk`) authenticates via the local
Claude Code login, so the default setup needs no environment at all.
`load_config` fails fast with a readable error when the file is
invalid or a required secret is missing for a selected provider.

## Dependencies

- `chess_coach.domain` (`Thresholds`, `LlmConfig`, `CoachAgent`).
  Libraries: pydantic v2 and PyYAML (`yaml.safe_load` → validation).
- Consumed by the [API layer](07-api.md), which injects individual
  values into [engine](04-engine.md) and [coach](06-coach.md). Those
  components receive plain arguments and never import this module.

## Build plan

1. Pydantic models with defaults so an empty `{}` file works.
2. `load_config` (read YAML, validate, merge env secrets).
3. Ship a commented `coach.config.example.yaml` in the repo root.
4. Unit tests: defaults, invalid values, missing API key error.
