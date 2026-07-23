"""Config component — see docs/01-config.md."""

from chess_coach.config.settings import (
    DEFAULT_CONFIG_PATH,
    AppConfig,
    CoachConfig,
    ConfigError,
    EngineConfig,
    OpeningsConfig,
    ServerConfig,
    StorageConfig,
    load_config,
)

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "AppConfig",
    "CoachConfig",
    "ConfigError",
    "EngineConfig",
    "OpeningsConfig",
    "ServerConfig",
    "StorageConfig",
    "load_config",
]
