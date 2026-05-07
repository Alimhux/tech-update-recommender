"""Конфигурация DepScope.

Каскад источников значений (от высшего приоритета к низшему):
    1. CLI аргументы (cli_overrides)
    2. Переменные окружения
    3. Файл ~/.depscope.yaml
    4. Дефолты, заданные в моделях ниже

API-ключи всегда хранятся в pydantic.SecretStr и маскируются в выводе.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, SecretStr

logger = logging.getLogger(__name__)


DEFAULT_CONFIG_PATH = Path("~/.depscope.yaml").expanduser()


class ConfigError(Exception):
    """Базовое исключение конфигурационных проблем DepScope."""


class LLMConfig(BaseModel):
    """Настройки LLM-провайдера."""

    model: str | None = None
    api_key: SecretStr | None = None
    max_context_tokens: int = 8000


class CacheConfig(BaseModel):
    """Настройки локального кеша deps.dev."""

    enabled: bool = True
    ttl_seconds: int = 3600
    path: str = "~/.cache/depscope/"


class SyftConfig(BaseModel):
    """Настройки запуска Syft."""

    path: str | None = None


class Config(BaseModel):
    """Конечная сложенная конфигурация DepScope."""

    llm: LLMConfig = Field(default_factory=LLMConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    syft: SyftConfig = Field(default_factory=SyftConfig)


def _read_yaml(path: Path) -> dict[str, Any]:
    """Прочитать YAML, вернуть пустой dict, если файла нет/пустой/битый."""
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Не удалось прочитать конфиг %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("Конфиг %s не является словарём, игнорируем", path)
        return {}
    return data


def _env_overrides() -> dict[str, Any]:
    """Собрать значения из переменных окружения.

    Поддерживаются:
      - DEPSCOPE_LLM_MODEL
      - DEPSCOPE_LLM_API_KEY (общий ключ)
      - GEMINI_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY (стандартные)
      - DEPSCOPE_SYFT_PATH
    """
    env: dict[str, Any] = {}
    llm: dict[str, Any] = {}

    if model := os.environ.get("DEPSCOPE_LLM_MODEL"):
        llm["model"] = model

    api_key = (
        os.environ.get("DEPSCOPE_LLM_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
    )
    if api_key:
        llm["api_key"] = api_key

    if llm:
        env["llm"] = llm

    if syft_path := os.environ.get("DEPSCOPE_SYFT_PATH"):
        env["syft"] = {"path": syft_path}

    return env


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Глубокое слияние двух словарей. `override` побеждает."""
    result = dict(base)
    for key, value in override.items():
        if value is None:
            continue
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _normalize_cli_overrides(cli_overrides: dict[str, Any]) -> dict[str, Any]:
    """Привести плоские CLI-аргументы к вложенной структуре Config.

    Принимаются ключи:
      llm_model, llm_api_key, syft_path, cache_*, ...
    Неизвестные ключи игнорируются.
    """
    nested: dict[str, Any] = {}
    llm: dict[str, Any] = {}
    syft: dict[str, Any] = {}

    if (model := cli_overrides.get("llm_model")) is not None:
        llm["model"] = model
    if (api_key := cli_overrides.get("llm_api_key")) is not None:
        llm["api_key"] = api_key
    if (syft_path := cli_overrides.get("syft_path")) is not None:
        syft["path"] = syft_path

    if llm:
        nested["llm"] = llm
    if syft:
        nested["syft"] = syft
    return nested


def load_config(
    cli_overrides: dict[str, Any] | None = None,
    config_path: Path | None = None,
) -> Config:
    """Загрузить конфигурацию с каскадом источников.

    Порядок (последний побеждает):
        defaults < ~/.depscope.yaml < env vars < cli_overrides
    """
    cli_overrides = cli_overrides or {}
    path = config_path if config_path is not None else DEFAULT_CONFIG_PATH

    yaml_data = _read_yaml(path)
    env_data = _env_overrides()
    cli_data = _normalize_cli_overrides(cli_overrides)

    merged: dict[str, Any] = {}
    merged = _deep_merge(merged, yaml_data)
    merged = _deep_merge(merged, env_data)
    merged = _deep_merge(merged, cli_data)

    return Config.model_validate(merged)
