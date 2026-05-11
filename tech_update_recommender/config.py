"""Конфиг для Tech Update Recommender.

Настройки берём из нескольких источников, чем выше в списке —
тем сильнее приоритет:

    1. Аргументы CLI
    2. Переменные окружения
    3. Файл ~/.tech-update-recommender.yaml
    4. Значения по умолчанию в pydantic-моделях

API-ключи храним через SecretStr — чтобы случайно не светились в логах
или при печати объекта.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, SecretStr

logger = logging.getLogger(__name__)


DEFAULT_CONFIG_PATH = Path("~/.tech-update-recommender.yaml").expanduser()


class ConfigError(Exception):
    """Ошибка, связанная с настройками приложения."""


class LLMConfig(BaseModel):
    """Настройки для LLM."""

    model: str | None = None
    api_key: SecretStr | None = None
    max_context_tokens: int = 8000


class CacheConfig(BaseModel):
    """Настройки кеша для ответов deps.dev."""

    enabled: bool = True
    ttl_seconds: int = 3600
    path: str = "~/.cache/tech-update-recommender/"


class SyftConfig(BaseModel):
    """Настройки для запуска Syft."""

    path: str | None = None


class Config(BaseModel):
    """Итоговый конфиг, который уже использует приложение."""

    llm: LLMConfig = Field(default_factory=LLMConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    syft: SyftConfig = Field(default_factory=SyftConfig)


def _read_yaml(path: Path) -> dict[str, Any]:
    """Пытаемся прочитать YAML-конфиг."""

    # файла нет — это норм, работаем с дефолтами/env/CLI
    if not path.is_file():
        return {}

    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}

    except (OSError, yaml.YAMLError) as exc:
        # конфиг не должен ломать запуск, поэтому только предупреждаем
        logger.warning("Не удалось прочитать конфиг %s: %s", path, exc)
        return {}

    # ждём именно словарь, дальше будем мержить секции
    if not isinstance(data, dict):
        logger.warning("Конфиг %s не является словарём, игнорируем", path)
        return {}

    return data


def _env_overrides() -> dict[str, Any]:
    """Собираем настройки из переменных окружения."""

    env: dict[str, Any] = {}
    llm: dict[str, Any] = {}

    # модель можно задать отдельной переменной проекта
    if model := os.environ.get("TUR_LLM_MODEL"):
        llm["model"] = model

    # сначала общий ключ проекта, потом стандартные ключи провайдеров
    api_key = (
        os.environ.get("TUR_LLM_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
    )

    if api_key:
        llm["api_key"] = api_key

    if llm:
        env["llm"] = llm

    # syft тоже можно указать через env, если бинарник не в PATH
    if syft_path := os.environ.get("TUR_SYFT_PATH"):
        env["syft"] = {"path": syft_path}

    return env


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Склеиваем два словаря, override имеет больший приоритет."""

    result = dict(base)

    for key, value in override.items():
        # None не считаем значением, чтобы он не затирал старые настройки
        if value is None:
            continue

        # обе стороны — словари: мержим рекурсивно
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value

    return result


def _normalize_cli_overrides(cli_overrides: dict[str, Any]) -> dict[str, Any]:
    """Плоские CLI-аргументы в структуру Config."""

    nested: dict[str, Any] = {}
    llm: dict[str, Any] = {}
    syft: dict[str, Any] = {}

    # Click отдаёт параметры плоско, а Config ждёт вложенные секции
    if (model := cli_overrides.get("llm_model")) is not None:
        llm["model"] = model
    if (api_key := cli_overrides.get("llm_api_key")) is not None:
        llm["api_key"] = api_key
    if (mct := cli_overrides.get("max_context_tokens")) is not None:
        llm["max_context_tokens"] = mct
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
    """Загружаем конфиг из файла, env и CLI."""

    cli_overrides = cli_overrides or {}
    path = config_path if config_path is not None else DEFAULT_CONFIG_PATH

    # сначала читаем каждый источник отдельно
    yaml_data = _read_yaml(path)
    env_data = _env_overrides()
    cli_data = _normalize_cli_overrides(cli_overrides)

    # потом накладываем в порядке приоритета — позднее побеждает
    merged: dict[str, Any] = {}
    merged = _deep_merge(merged, yaml_data)
    merged = _deep_merge(merged, env_data)
    merged = _deep_merge(merged, cli_data)

    # Pydantic проверит типы и соберёт Config-объект
    return Config.model_validate(merged)
