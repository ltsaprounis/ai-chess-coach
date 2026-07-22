"""Load and validate application configuration (docs/01-config.md)."""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import yaml
from pydantic import BaseModel, Field, ValidationError

from chess_coach.domain import LlmConfig, Thresholds

DEFAULT_CONFIG_PATH = Path("coach.config.yaml")


class ConfigError(Exception):
    """The config file or environment is invalid."""


class EngineConfig(BaseModel):
    depth: int = 16
    workers: int = 2


class ServerConfig(BaseModel):
    port: int = 8000


class StorageConfig(BaseModel):
    db_path: Path = Path("coach.sqlite3")


class AppConfig(BaseModel):
    engine: EngineConfig = Field(default_factory=EngineConfig)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    anthropic_api_key: str | None = None


def load_config(
    path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> AppConfig:
    """Read the YAML config, apply defaults, and merge env secrets.

    A missing file means all defaults. Fails fast with `ConfigError`
    on invalid YAML/values, on secrets placed in the file, and on a
    missing API key for the selected LLM provider.
    """
    path = DEFAULT_CONFIG_PATH if path is None else path
    env = os.environ if env is None else env

    raw = _read_yaml_mapping(path)
    if "anthropic_api_key" in raw:
        raise ConfigError(
            f"{path}: secrets never go in the config file; "
            "set the ANTHROPIC_API_KEY environment variable instead"
        )

    try:
        config = AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{path} is invalid:\n{exc}") from exc

    config.anthropic_api_key = env.get("ANTHROPIC_API_KEY")
    if config.llm.provider == "anthropic" and not config.anthropic_api_key:
        raise ConfigError(
            "llm.provider is 'anthropic' but the ANTHROPIC_API_KEY "
            "environment variable is not set"
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
