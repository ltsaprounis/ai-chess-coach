"""Load and validate application configuration (docs/01-config.md)."""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Self, cast

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

from chess_coach.domain import CoachAgent, Thresholds

# Source-checkout root (four parents above this file). Relative
# config paths anchor here, never at the cwd, so the server, scripts,
# and tests all open the same files no matter where they run from.
REPO_ROOT = Path(__file__).resolve().parents[4]

DEFAULT_CONFIG_PATH = REPO_ROOT / "coach.config.yaml"


class ConfigError(Exception):
    """The config file or environment is invalid."""


class EngineConfig(BaseModel):
    depth: int = 16
    workers: int = 2
    bin_path: Path | None = None  # None -> engines/stockfish/src/stockfish
    analyze_limit: int = 100  # newest games per "analyze all" run
    # candidate lines for live eval and the coach's engine tool;
    # batch analysis stays single-PV and ignores this.
    multipv: int = Field(default=5, ge=1, le=10)
    # seconds capping each position search and the gap between
    # streamed infos; tripping it means a wedged engine worker that
    # the pool kills and retires (see docs/04-engine.md). The default
    # sits well above the slowest honest search measured (~104s cold
    # at depth 16) so it only ever fires on a genuine wedge.
    eval_timeout: float = Field(default=300.0, gt=0)


class ServerConfig(BaseModel):
    port: int = 8000


class StorageConfig(BaseModel):
    db_path: Path = Path("data/coach.sqlite3")  # repo-root relative; created on demand


class OpeningsConfig(BaseModel):
    book_dir: Path | None = None  # None -> <repo root>/vendor/chess-openings


def _default_agents() -> list[CoachAgent]:
    return [CoachAgent(id="claude", label="Claude")]  # LlmConfig defaults


class CoachConfig(BaseModel):
    agents: list[CoachAgent] = Field(default_factory=_default_agents)
    default_agent: str = ""  # resolved: "" -> first agent's id

    @model_validator(mode="after")
    def _check_roster(self) -> Self:
        if not self.agents:
            raise ValueError("coach.agents must list at least one agent")
        ids = [agent.id for agent in self.agents]
        if not all(ids):
            raise ValueError("every coach.agents entry needs a non-empty id")
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(
                f"coach.agents ids must be unique; duplicated: {', '.join(duplicates)}"
            )
        if not self.default_agent:
            self.default_agent = ids[0]
        elif self.default_agent not in ids:
            raise ValueError(
                f"coach.default_agent {self.default_agent!r} is not a "
                f"configured agent id (configured: {', '.join(ids)})"
            )
        return self


class AppConfig(BaseModel):
    engine: EngineConfig = Field(default_factory=EngineConfig)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    coach: CoachConfig = Field(default_factory=CoachConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    openings: OpeningsConfig = Field(default_factory=OpeningsConfig)
    anthropic_api_key: str | None = None

    @model_validator(mode="after")
    def _anchor_paths(self) -> Self:
        self.storage.db_path = _anchored(self.storage.db_path)
        if self.engine.bin_path is not None:
            self.engine.bin_path = _anchored(self.engine.bin_path)
        if self.openings.book_dir is not None:
            self.openings.book_dir = _anchored(self.openings.book_dir)
        return self


def _anchored(path: Path) -> Path:
    return path if path.is_absolute() else REPO_ROOT / path


def load_config(
    path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> AppConfig:
    """Read the YAML config, apply defaults, and merge env secrets.

    A missing file means all defaults. Fails fast with `ConfigError`
    on invalid YAML/values (including a coach `provider` that is not
    an implemented `LlmProvider`) and on secrets placed in the file.
    """
    path = DEFAULT_CONFIG_PATH if path is None else path
    env = os.environ if env is None else env

    raw = _read_yaml_mapping(path)
    if "anthropic_api_key" in raw:
        raise ConfigError(
            f"{path}: secrets never go in the config file; "
            "set the ANTHROPIC_API_KEY environment variable instead"
        )
    if "llm" in raw:
        raise ConfigError(
            f"{path}: the top-level 'llm' section was replaced by the "
            "coach agent roster; move it to 'coach.agents' "
            "(see coach.config.example.yaml)"
        )

    try:
        config = AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{path} is invalid:\n{exc}") from exc

    # Reserved for the planned API-backed providers; no shipped
    # provider reads it (both ride local CLI logins).
    config.anthropic_api_key = env.get("ANTHROPIC_API_KEY")
    return config


def _read_yaml_mapping(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    return cast(dict[str, object], raw)
