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


def test_empty_file_yields_default_coach_roster(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, ""), env=KEY_ENV)
    assert len(config.coach.agents) == 1
    agent = config.coach.agents[0]
    assert (agent.id, agent.label) == ("claude", "Claude")
    assert agent.provider == "claude-agent-sdk"
    assert agent.model == "claude-opus-4-8"
    assert config.coach.default_agent == "claude"


def test_partial_override_keeps_other_defaults(tmp_path: Path) -> None:
    path = write(tmp_path, "engine:\n  depth: 20\n")
    config = load_config(path, env=KEY_ENV)
    assert config.engine.depth == 20
    assert config.engine.workers == 2


def test_invalid_value_raises_config_error(tmp_path: Path) -> None:
    path = write(tmp_path, "engine:\n  depth: deep\n")
    with pytest.raises(ConfigError, match="invalid"):
        load_config(path, env=KEY_ENV)


def test_multipv_defaults_to_five(tmp_path: Path) -> None:
    config = load_config(tmp_path / "nope.yaml", env=KEY_ENV)
    assert config.engine.multipv == 5


def test_multipv_yaml_value_is_honored(tmp_path: Path) -> None:
    path = write(tmp_path, "engine:\n  multipv: 3\n")
    config = load_config(path, env=KEY_ENV)
    assert config.engine.multipv == 3


@pytest.mark.parametrize("value", [0, 11])
def test_multipv_out_of_range_raises_config_error(tmp_path: Path, value: int) -> None:
    path = write(tmp_path, f"engine:\n  multipv: {value}\n")
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


def test_default_provider_needs_no_api_key(tmp_path: Path) -> None:
    config = load_config(tmp_path / "nope.yaml", env={})
    assert config.coach.agents[0].provider == "claude-agent-sdk"
    assert config.anthropic_api_key is None


def test_custom_roster_with_explicit_default(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "coach:\n"
        "  agents:\n"
        "    - id: claude\n"
        "      label: Claude\n"
        "    - id: fast\n"
        "      label: Fast Claude\n"
        "      model: claude-haiku-4-5\n"
        "  default_agent: fast\n",
    )
    config = load_config(path, env=KEY_ENV)
    assert [a.id for a in config.coach.agents] == ["claude", "fast"]
    assert config.coach.agents[1].model == "claude-haiku-4-5"
    assert config.coach.default_agent == "fast"


def test_duplicate_agent_ids_are_rejected(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "coach:\n"
        "  agents:\n"
        "    - id: claude\n"
        "      label: One\n"
        "    - id: claude\n"
        "      label: Two\n",
    )
    with pytest.raises(ConfigError, match="unique"):
        load_config(path, env=KEY_ENV)


def test_unknown_default_agent_is_rejected(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "coach:\n"
        "  agents:\n"
        "    - id: claude\n"
        "      label: Claude\n"
        "  default_agent: nope\n",
    )
    with pytest.raises(ConfigError, match="not a configured agent id"):
        load_config(path, env=KEY_ENV)


def test_legacy_llm_key_gets_migration_hint(tmp_path: Path) -> None:
    path = write(tmp_path, "llm:\n  provider: anthropic\n")
    with pytest.raises(ConfigError, match=r"coach\.agents"):
        load_config(path, env=KEY_ENV)


def test_unimplemented_provider_rejected_at_load(tmp_path: Path) -> None:
    # `LlmProvider` lists implemented providers only, so a planned
    # one fails config validation instead of blowing up at startup.
    path = write(
        tmp_path,
        "coach:\n"
        "  agents:\n"
        "    - id: api\n"
        "      label: API Claude\n"
        "      provider: anthropic\n",
    )
    with pytest.raises(ConfigError, match="github-copilot"):
        load_config(path, env=KEY_ENV)


def test_example_config_in_repo_root_is_valid() -> None:
    example = Path(__file__).resolve().parents[2] / "coach.config.example.yaml"
    config = load_config(example, env=KEY_ENV)
    assert config == AppConfig(anthropic_api_key="sk-test")
