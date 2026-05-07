# CLAUDE.md — ориентир для агентов

Краткий справочник по структуре DepScope, контрактам между модулями и
правилам, которым нужно следовать при доработке.

## Карта модулей

| Файл | Что делает |
|------|------------|
| `depscope/__init__.py`        | Экспортирует `__version__`. |
| `depscope/__main__.py`        | `python -m depscope` → `cli.main`. |
| `depscope/cli.py`             | Click-команда `scan`, склейка pipeline, обработка ошибок верхнего уровня. |
| `depscope/config.py`          | Pydantic-конфиг + каскад «CLI > env > yaml > defaults», `ConfigError`. |
| `depscope/cache.py`           | SQLite-кеш `(system, name, version) → JSON` с TTL. |
| `depscope/models.py`          | Pydantic-модели контрактов: `PackageInfo`, `DependencyReport`, `FullReport`, `LLMInput`, `Advisory`. |
| `depscope/syft_module.py`     | Запуск syft, парсинг CycloneDX, фильтр по `SUPPORTED_ECOSYSTEMS`, `SyftError`. |
| `depscope/depsdev_module.py`  | Async HTTP к deps.dev (batch + GetPackage), `build_report`, `DepsDevError`. |
| `depscope/llm_module.py`      | Сбор контекста (tree + dep-файлы), промпт, `litellm.completion`, `LLMError`. |
| `depscope/report.py`          | Рендер `FullReport` в `table` / `json` / `markdown`. |
| `depscope/utils.py`           | semver-сравнение, нормализация PyPI-имён, URL-кодирование. |

## Контракты между модулями (одной строкой)

- `scan_project(path) -> (supported, unsupported)` — оба списка `PackageInfo`.
- `build_report(supported, unsupported, project_path, cache) -> FullReport` (async).
- `build_llm_input(report, project_path) -> LLMInput`.
- `generate_advice(llm_input, model, api_key=None, max_context_tokens=8000) -> str` (markdown).
- `render_report(report, fmt, only_outdated=False, llm_advice=None, llm_model_name=None) -> str`.

Pipeline: Syft → deps.dev (+ Cache) → LLM (опц.) → Report → stdout/файл.

## Команды разработчика

```bash
pytest -q                  # все тесты (юнит + интеграционные)
ruff check .               # линтер: E, F, I, B
ruff format --check .      # форматирование
python -m build            # сборка wheel + sdist
twine check dist/*         # метаданные
```

## Что НЕ делать

- Не печатать API-ключи в логах. `SecretStr` уже маскирует — не вытаскивайте
  значение в логгер. В `generate_advice` ключ не логируется (см. блок 5).
- Не превращать pipeline в agentic / tool-using. DepScope делает один
  LLM-вызов с собранным заранее контекстом — и всё. Без циклов «LLM зовёт
  инструменты».
- Не делать реальные HTTP/syft-вызовы в тестах. Используйте `aioresponses`
  для deps.dev и моки `subprocess.run` / `scan_project` для syft.
- Не лить `print()` для статуса — только `logging`. Прогресс — через
  `rich.progress.Progress` и `Console(stderr=True)`.
- Не трогать `cli` стайл вывода: stdout = только результат отчёта, stderr =
  прогресс / сообщения / ошибки. Иначе `--output json` в pipe сломается.
- Не публиковать на PyPI без явной просьбы. `python -m build` и
  `twine check` — да; `twine upload` — нет.

## Версионирование

Версия в `depscope/__init__.py` и `pyproject.toml` должна совпадать.
Текущая релизная — `0.1.0`.
