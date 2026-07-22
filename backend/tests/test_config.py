"""Config component tests (docs/01-config.md)."""

from pathlib import Path

import pytest

from chess_coach.config import AppConfig, ConfigError, load_config

KEY_ENV = {"ANTHROPIC_API_KEY": "sk-test"}


def write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "coach.config.yaml"
    path.write_text(content)
    return path


def test_missing_file_yields_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path / "nope.yaml", env=KEY_ENV)
    assert config == AppConfig(anthropic_api_key="sk-test")
    assert config.engine.depth == 16
    assert config.thresholds.blunder == 200


def test_empty_file_yields_defaults(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, ""), env=KEY_ENV)
    assert config.llm.model == "claude-opus-4-8"


def test_partial_override_keeps_other_defaults(tmp_path: Path) -> None:
    path = write(tmp_path, "engine:\n  depth: 20\n")
    config = load_config(path, env=KEY_ENV)
    assert config.engine.depth == 20
    assert config.engine.workers == 2


def test_invalid_value_raises_config_error(tmp_path: Path) -> None:
    path = write(tmp_path, "engine:\n  depth: deep\n")
    with pytest.raises(ConfigError, match="invalid"):
        load_config(path, env=KEY_ENV)


def test_invalid_yaml_raises_config_error(tmp_path: Path) -> None:
    path = write(tmp_path, "engine: [unclosed\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(path, env=KEY_ENV)


def test_secret_in_file_is_rejected(tmp_path: Path) -> None:
    path = write(tmp_path, "anthropic_api_key: sk-oops\n")
    with pytest.raises(ConfigError, match="never go in the config file"):
        load_config(path, env=KEY_ENV)


def test_missing_api_key_for_anthropic_provider(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        load_config(tmp_path / "nope.yaml", env={})


def test_example_config_in_repo_root_is_valid() -> None:
    example = Path(__file__).resolve().parents[2] / "coach.config.example.yaml"
    config = load_config(example, env=KEY_ENV)
    assert config == AppConfig(anthropic_api_key="sk-test")
