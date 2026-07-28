# Component 1 — Config

Loads and validates all tunable settings once at startup, producing a
single typed `AppConfig` object. Everything configurable lives here;
no other component reads configuration or environment variables —
components touch data files (TSVs, the DB, the engine binary) only
at paths injected from this config.

## Interface

```python
REPO_ROOT: Path  # source-checkout root, from the package location

DEFAULT_CONFIG_PATH = REPO_ROOT / "coach.config.yaml"

class ConfigError(Exception): ...  # invalid config file or environment

def load_config(
    path: Path | None = None,              # None -> DEFAULT_CONFIG_PATH
    env: Mapping[str, str] | None = None,  # None -> os.environ
) -> AppConfig

class AppConfig(BaseModel):
    engine: EngineConfig        # depth=16, workers=2, analyze_limit
                                # =100 (cap per analyze run);
                                # multipv=5 (1-10; candidate lines for
                                # live eval and the coach's engine
                                # tool — batch analysis stays 1);
                                # eval_timeout=300.0 (seconds, > 0;
                                # cap per position search and on the
                                # gap between streamed infos —
                                # tripping it means a wedged worker,
                                # which the pool kills and retires;
                                # the default clears the slowest
                                # honest search measured, ~104s cold —
                                # see docs/04-engine.md);
                                # bin_path None -> repo submodule build
    thresholds: Thresholds      # centipawn loss: inaccuracy=50,
                                # mistake=100, blunder=200
    brilliant: BrilliantThresholds  # sound-sacrifice cutoffs for the
                                # coach highlights surface: sac_points
                                # =2, best_tolerance_cp=0,
                                # winning_cap_cp=200, sound_floor_cp=0
                                # (docs/06-coach.md, "Highlights")
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

`Thresholds`, `BrilliantThresholds`, `LlmConfig`, and `CoachAgent`
(an `LlmConfig` subclass adding `id` + `label`) are domain types —
[engine](04-engine.md), [coach](06-coach.md), and the
[API layer](07-api.md) consume them too. The other sub-models
(`EngineConfig`, `CoachConfig`, `ServerConfig`, `StorageConfig`,
`OpeningsConfig`) are config-local.

Validation beyond field types: agent ids must be non-empty and
unique, `agents` must not be empty, and `default_agent` must match a
configured id (when omitted it resolves to the first agent's id).
The retired top-level `llm:` key fails fast with a migration hint
pointing at `coach.agents`.

Paths are anchored, never cwd-relative: a relative `storage.db_path`,
`engine.bin_path`, or `openings.book_dir` is resolved against
`REPO_ROOT` during validation, so every path the returned
`AppConfig` carries is absolute (`bin_path` and `book_dir` may also
stay `None`, resolved to their submodule defaults by the API
layer) and every entry point — server, scripts,
tests — opens the same files regardless of working directory.
Absolute paths pass through untouched. The default `db_path`
therefore resolves to `<repo root>/data/coach.sqlite3`, and the
config file itself is looked up at the repo root.

Secrets come from the environment, not the file: `ANTHROPIC_API_KEY`
is read into `anthropic_api_key` but no shipped provider uses it —
it is reserved for the planned API-backed `anthropic` provider. Both
shipped providers (`claude-agent-sdk`, `github-copilot`) authenticate
via their local CLI logins, so the default setup needs no environment
at all. `load_config` fails fast with `ConfigError` when the file is
invalid — including a `provider` naming an unimplemented provider,
since `domain.LlmProvider` lists implemented ones only; a missing
file at the default path just means all defaults.

## Dependencies

- `chess_coach.domain` (`Thresholds`, `BrilliantThresholds`,
  `LlmConfig`, `CoachAgent`). Libraries: pydantic v2 and PyYAML
  (`yaml.safe_load` → validation).
- Consumed by the [API layer](07-api.md), which injects individual
  values into [engine](04-engine.md) and [coach](06-coach.md). Those
  components receive plain arguments and never import this module.

## Build plan

1. Pydantic models with defaults so an empty `{}` file works.
2. `load_config` (read YAML, validate, merge env secrets).
3. Ship a commented `coach.config.example.yaml` in the repo root.
4. Unit tests: defaults, invalid values, unimplemented-provider
   rejection.
