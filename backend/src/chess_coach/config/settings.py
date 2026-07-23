"""Load and validate application configuration (docs/01-config.md)."""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Self, cast

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

from chess_coach.domain import CoachAgent, Thresholds

DEFAULT_CONFIG_PATH = Path("coach.config.yaml")


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


class ServerConfig(BaseModel):
    port: int = 8000


class StorageConfig(BaseModel):
    db_path: Path = Path("data/coach.sqlite3")  # created on demand


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


def load_config(
    path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> AppConfig:
    """Read the YAML config, apply defaults, and merge env secrets.

    A missing file means all defaults. Fails fast with `ConfigError`
    on invalid YAML/values, on secrets placed in the file, and on a
    missing API key for a provider some coach agent requires.
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

    config.anthropic_api_key = env.get("ANTHROPIC_API_KEY")
    needs_key = any(a.provider == "anthropic" for a in config.coach.agents)
    if needs_key and not config.anthropic_api_key:
        raise ConfigError(
            "a coach agent uses provider 'anthropic' but the "
            "ANTHROPIC_API_KEY environment variable is not set"
        )
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
