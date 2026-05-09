# Блок 1 — Скелет проекта

## Цель блока

Подготовить базовую инфраструктуру проекта: упаковку, структуру директорий,
контракты данных (Pydantic-модели) и CLI-каркас с заглушками. После этого
блока проект должен устанавливаться (`pip install -e .`), команда `tech-update-recommender`
должна запускаться, а импорт всех модулей должен проходить без ошибок.

## Пререквизиты

- Установлен Python 3.11+
- Установлен `pip` или `uv`
- Опционально: `pre-commit` для будущих хуков

## Задачи

### 1.1 pyproject.toml

- Создать `pyproject.toml` со сборкой через `setuptools` или `hatchling`.
- Заполнить метаданные: `name = "tech-update-recommender"`, `version = "0.1.0"`, license MIT,
  python `>=3.11`.
- Зависимости (runtime):
  - `click`
  - `packageurl-python`
  - `aiohttp`
  - `pydantic >= 2`
  - `rich`
  - `pyyaml`
- `optional-dependencies`:
  - `llm = ["litellm"]`
- `dev`-зависимости:
  - `pytest`
  - `pytest-asyncio`
  - `aioresponses`
- Зарегистрировать entry-point:
  ```
  [project.scripts]
  depscope = "tech_update_recommender.cli:main"
  ```

### 1.2 Структура директорий

Создать следующее дерево (пустые `.py` файлы или с минимальными заглушками):

```
tech_update_recommender/
├── __init__.py        # Версия пакета: __version__ = "0.1.0"
├── cli.py
├── config.py
├── syft_module.py
├── depsdev_module.py
├── llm_module.py
├── report.py
├── models.py
└── utils.py
tests/
├── __init__.py
├── conftest.py
├── test_syft_module.py
├── test_depsdev_module.py
├── test_llm_module.py
├── test_report.py
└── fixtures/
docs/
```

### 1.3 Pydantic-модели (tech_update_recommender/models.py)

Реализовать ровно те модели, что указаны в PLAN.md, без отсебятины:

- `PackageInfo` — выход SyftModule.
- `Advisory` — CVE/GHSA с CVSS.
- `DependencyReport` — выход DepsDevModule по одному пакету.
- `FullReport` — итоговый отчёт (supported + unsupported + summary).
- `LLMInput` — вход LLMModule.

Требования:

- Все модели наследуются от `pydantic.BaseModel`.
- Опциональные поля помечены как `Optional[...] = None` (или `| None = None`).
- Списки имеют дефолт `Field(default_factory=list)`.
- Модели должны быть импортируемы: `from tech_update_recommender.models import PackageInfo, ...`.

### 1.4 Конфигурация (tech_update_recommender/config.py)

- Класс `Config` (Pydantic-модель) с дефолтами.
- Функция `load_config(cli_overrides: dict) -> Config` со слиянием:
  CLI args → env vars → `~/.tech-update-recommender.yaml` → defaults.
- На этом этапе достаточно реализовать загрузку YAML и слияние с defaults.
  Реальное использование появится позже.
- API-ключи никогда не логируются (заложить в `__repr__`/`model_dump` маску).

### 1.5 CLI-каркас (tech_update_recommender/cli.py)

- Использовать `click`.
- Команда `tech-update-recommender scan <path>` со всеми опциями из PLAN.md
  (`--output`, `--mode`, `--only-outdated`, `--save`, `--llm-model`,
  `--llm-api-key`, `--no-llm`, `--syft-path`, `--verbose`).
- На этом этапе обработчик команды печатает «not implemented yet»
  и выходит с кодом 0. Реальная склейка — в блоке 6.
- `def main()` — entry point.
- `--version` — печатает версию из `tech_update_recommender.__version__`.

### 1.6 Логирование

- В `cli.py` настроить `logging.basicConfig` исходя из `--verbose`.
- Уровни: default `WARNING`, `--verbose` → `DEBUG`.
- Никаких `print()` для статусной информации.

## Критерии приёмки

- [ ] `pip install -e ".[llm,dev]"` проходит без ошибок.
- [ ] `tech-update-recommender --version` печатает корректную версию.
- [ ] `tech-update-recommender scan .` запускается и выводит заглушку.
- [ ] `python -c "from tech_update_recommender.models import FullReport"` работает.
- [ ] `pytest -q` проходит (даже если тестов пока 0).

## Тесты блока

- `tests/test_models.py` (минимальный): инстанцирование каждой модели
  с валидными и невалидными данными.
- `tests/test_config.py`: каскад дефолтов и YAML.

## Подводные камни

- Не тащить в скелет логику syft/deps.dev/LLM — только контракты и заглушки.
- Версии Pydantic 1 vs 2 несовместимы — фиксируем `pydantic >= 2`.
- `click` группы и команды: `scan` сделать командой, чтобы потом легко
  добавить, например, `tech-update-recommender cache clear`.

## Что НЕ делаем в этом блоке

- Не пишем реальную логику работы с syft.
- Не делаем HTTP-запросы.
- Не интегрируем LiteLLM.
- Не делаем форматирование отчёта.
