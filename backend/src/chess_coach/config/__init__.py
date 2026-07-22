"""Config component — see docs/01-config.md."""

from chess_coach.config.settings import (
    DEFAULT_CONFIG_PATH,
    AppConfig,
    ConfigError,
    EngineConfig,
    ServerConfig,
    StorageConfig,
    load_config,
)

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "AppConfig",
    "ConfigError",
    "EngineConfig",
    "ServerConfig",
    "StorageConfig",
    "load_config",
]
