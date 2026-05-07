"""Тесты загрузки конфигурации DepScope."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from depscope.config import Config, load_config

# Изоляция от пользовательских env vars LLM
LLM_ENV_VARS = (
    "DEPSCOPE_LLM_MODEL",
    "DEPSCOPE_LLM_API_KEY",
    "DEPSCOPE_SYFT_PATH",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in LLM_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_defaults_no_yaml(tmp_path: Path) -> None:
    """Если YAML отсутствует и переменных окружения нет — берём дефолты."""
    cfg = load_config(cli_overrides={}, config_path=tmp_path / "missing.yaml")
    assert isinstance(cfg, Config)
    assert cfg.llm.model is None
    assert cfg.llm.api_key is None
    assert cfg.llm.max_context_tokens == 8000
    assert cfg.cache.enabled is True
    assert cfg.cache.ttl_seconds == 3600
    assert cfg.syft.path is None


def test_yaml_overrides_defaults(tmp_path: Path) -> None:
    yaml_path = tmp_path / "depscope.yaml"
    yaml_path.write_text(
        """
llm:
  model: "gemini/gemini-2.0-flash"
  api_key: "yaml-secret"
  max_context_tokens: 4096
cache:
  enabled: false
  ttl_seconds: 60
syft:
  path: "/usr/local/bin/syft"
""",
        encoding="utf-8",
    )
    cfg = load_config(cli_overrides={}, config_path=yaml_path)
    assert cfg.llm.model == "gemini/gemini-2.0-flash"
    assert isinstance(cfg.llm.api_key, SecretStr)
    assert cfg.llm.api_key.get_secret_value() == "yaml-secret"
    assert cfg.llm.max_context_tokens == 4096
    assert cfg.cache.enabled is False
    assert cfg.cache.ttl_seconds == 60
    assert cfg.syft.path == "/usr/local/bin/syft"


def test_env_overrides_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    yaml_path = tmp_path / "depscope.yaml"
    yaml_path.write_text(
        """
llm:
  model: "from-yaml"
  api_key: "yaml-key"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("DEPSCOPE_LLM_MODEL", "from-env")
    monkeypatch.setenv("DEPSCOPE_LLM_API_KEY", "env-key")
    cfg = load_config(cli_overrides={}, config_path=yaml_path)
    assert cfg.llm.model == "from-env"
    assert cfg.llm.api_key.get_secret_value() == "env-key"


def test_cli_overrides_env_and_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    yaml_path = tmp_path / "depscope.yaml"
    yaml_path.write_text(
        """
llm:
  model: "from-yaml"
syft:
  path: "/yaml/syft"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("DEPSCOPE_LLM_MODEL", "from-env")
    monkeypatch.setenv("DEPSCOPE_SYFT_PATH", "/env/syft")

    cli_overrides = {
        "llm_model": "from-cli",
        "llm_api_key": "cli-key",
        "syft_path": "/cli/syft",
    }
    cfg = load_config(cli_overrides=cli_overrides, config_path=yaml_path)
    assert cfg.llm.model == "from-cli"
    assert cfg.llm.api_key.get_secret_value() == "cli-key"
    assert cfg.syft.path == "/cli/syft"


def test_standard_provider_env_vars_picked_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    cfg = load_config(cli_overrides={}, config_path=tmp_path / "missing.yaml")
    assert cfg.llm.api_key is not None
    assert cfg.llm.api_key.get_secret_value() == "openai-secret"


def test_api_key_is_masked_in_repr_and_dump(tmp_path: Path) -> None:
    """API-ключ нигде не должен выводиться открыто."""
    cfg = load_config(
        cli_overrides={"llm_api_key": "super-secret-key"},
        config_path=tmp_path / "missing.yaml",
    )
    # repr / str
    assert "super-secret-key" not in repr(cfg)
    assert "super-secret-key" not in repr(cfg.llm)
    assert "super-secret-key" not in str(cfg)
    # model_dump (по умолчанию SecretStr сериализуется как '**********')
    dumped = cfg.model_dump()
    assert "super-secret-key" not in str(dumped)
    # JSON-сериализация тоже маскирует
    dumped_json = cfg.model_dump_json()
    assert "super-secret-key" not in dumped_json


def test_malformed_yaml_falls_back_to_defaults(tmp_path: Path) -> None:
    yaml_path = tmp_path / "broken.yaml"
    yaml_path.write_text("llm: [this is not: valid", encoding="utf-8")
    cfg = load_config(cli_overrides={}, config_path=yaml_path)
    # Должны получить дефолты, без падения.
    assert cfg.llm.model is None


def test_cli_none_does_not_override_yaml(tmp_path: Path) -> None:
    """Явное CLI значение None не должно сбрасывать значение из YAML."""
    yaml_path = tmp_path / "depscope.yaml"
    yaml_path.write_text(
        'llm:\n  model: "gemini/gemini-2.0-flash"\n',
        encoding="utf-8",
    )
    cfg = load_config(
        cli_overrides={"llm_model": None, "llm_api_key": None, "syft_path": None},
        config_path=yaml_path,
    )
    assert cfg.llm.model == "gemini/gemini-2.0-flash"
