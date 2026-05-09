# Tech Update Recommender — Чеклист реализации

Заполняется по мере работы. Подробности по каждому блоку — в `docs/blocks/`.

Легенда: `[ ]` — не начато, `[~]` — в работе, `[x]` — готово.

---

## Блок 1 — Скелет проекта
> Подробности: [docs/blocks/01-skeleton.md](docs/blocks/01-skeleton.md)

- [x] `pyproject.toml` создан и валиден
- [x] Структура директорий создана (`tech_update_recommender/`, `tests/`, `docs/`, `tests/fixtures/`)
- [x] `tech_update_recommender/__init__.py` с `__version__`
- [x] `models.py`: `PackageInfo`, `Advisory`, `DependencyReport`, `FullReport`, `LLMInput`
- [x] `config.py`: `Config` + `load_config()` с каскадом дефолтов
- [x] `cli.py`: `click`-команда `scan` со всеми опциями (заглушки)
- [x] `--version` работает
- [x] Логирование настроено (`--verbose` → DEBUG)
- [x] `pip install -e ".[llm,dev]"` проходит
- [x] `tests/test_models.py` — минимальные тесты моделей
- [x] `tests/test_config.py` — каскад конфигурации
- [x] `pytest -q` зелёный

---

## Блок 2 — SyftModule
> Подробности: [docs/blocks/02-syft-module.md](docs/blocks/02-syft-module.md)

- [x] `find_syft_binary()` + `SyftNotFoundError`
- [x] `run_syft()` через `subprocess`, stdout во временный файл
- [x] `SyftExecutionError` при ненулевом exit
- [x] `parse_cyclonedx()` — извлечение purl, парсинг через `packageurl-python`
- [x] Maven namespace склеивается корректно (`groupId:artifactId`)
- [x] `SUPPORTED_ECOSYSTEMS` константа
- [x] `split_supported()` — разделение на supported/unsupported
- [x] Дедупликация пакетов
- [x] Публичная функция `scan_project()`
- [x] Иерархия исключений (`SyftError`, `SyftNotFoundError`, `SyftExecutionError`, `SyftParseError`)
- [x] Фикстуры: `cyclonedx_simple.json`, `_mixed.json`, `_maven.json`, `_empty.json`, `_broken.json`
- [x] `tests/test_syft_module.py` — все тест-кейсы из блока 2

---

## Блок 3 — DepsDevModule
> Подробности: [docs/blocks/03-depsdev-module.md](docs/blocks/03-depsdev-module.md)

- [x] Async HTTP-клиент с таймаутом и retry (3 попытки, exp backoff)
- [x] `fetch_current_versions()` — batch POST `/v3alpha/versionbatch`
- [x] Чанкирование при > 5000 пакетов
- [x] `fetch_latest_versions()` — GET `/v3/systems/{system}/packages/{name}` параллельно
- [x] Семафор concurrency = 20
- [x] Дедупликация запросов GetPackage по `(system, name)`
- [x] `compute_semver_diff()` в `utils.py` — major/minor/patch/None
- [x] Поддержка нестрогого SemVer (Maven, PyPI)
- [x] `cache.py`: SQLite-кеш с TTL
- [x] `Cache.get/set/clear` API
- [x] `build_report()` — основная функция модуля
- [x] Корректные `outdated_count`, `vulnerable_count`
- [x] Обработка 404 (latest_version=None)
- [x] `DepsDevError` при недоступности API
- [x] Нормализация имён (PyPI lowercase, Maven groupId:artifactId)
- [x] Фикстуры: `depsdev_batch_response.json`, `depsdev_getpackage_express.json`, `depsdev_404.json`
- [x] `tests/test_depsdev_module.py` — все тест-кейсы из блока 3

---

## Блок 4 — ReportModule
> Подробности: [docs/blocks/04-report-module.md](docs/blocks/04-report-module.md)

- [x] `render_report()` — публичный API
- [x] Формат `table` через `rich.table.Table`
- [x] Цвета: красный/жёлтый/зелёный/серый по статусу
- [x] Summary-строка перед таблицей
- [x] Секция unsupported с кратким упоминанием
- [x] Формат `json` через `model_dump_json(indent=2)`
- [x] Поле `llm_advice` в JSON-выводе при наличии
- [x] Формат `markdown` с заголовками и таблицей
- [x] Дисклеймер LLM-секции с подстановкой `{model_name}`
- [x] Фильтр `only_outdated` применяется до рендера
- [x] Summary считается по полному отчёту (не по фильтру)
- [x] `tests/test_report.py` — все тест-кейсы из блока 4

---

## Блок 5 — LLMModule
> Подробности: [docs/blocks/05-llm-module.md](docs/blocks/05-llm-module.md)

- [x] `collect_project_tree()` — обход с исключениями (`node_modules`, `.git`, `venv`, …)
- [x] Лимит строк дерева (default 200)
- [x] `collect_dependency_files()` — поиск стандартных файлов
- [x] Пропуск lock-файлов > 200 KB
- [x] `build_llm_input()` — top-50 outdated/vulnerable
- [x] Приоритизация: CVE → major → minor → patch
- [x] System prompt из PLAN.md
- [x] User prompt с отчётом, деревом, файлами
- [x] Подсчёт токенов через `litellm.token_counter` или fallback
- [x] Алгоритм усечения при превышении `max_context_tokens`
- [x] `LLMContextOverflowError` если усечение не помогло
- [x] `generate_advice()` через `litellm.completion`
- [x] Ленивый импорт `litellm`
- [x] `LLMNotAvailableError` при отсутствии зависимости
- [x] Маппинг ошибок: `LLMAuthError`, `LLMRateLimitError`, `LLMNetworkError`
- [x] Retry на rate limit (1 попытка через 5 сек)
- [x] API-ключи не попадают в логи
- [x] Поддержка локальных моделей (Ollama) без API-ключа
- [x] `tests/test_llm_module.py` — все тест-кейсы из блока 5

---

## Блок 6 — Интеграция и финализация
> Подробности: [docs/blocks/06-integration.md](docs/blocks/06-integration.md)

- [x] CLI pipeline `scan` склеен (Syft → DepsDev → LLM → Report)
- [x] Прогресс-бары через `rich.progress` (в stderr)
- [x] Обработка ошибок верхнего уровня в `main()`, корректные exit codes
- [x] Ошибка при `--mode advice/full` без указанной модели
- [x] Конфиг `~/.tech-update-recommender.yaml` загружается, если есть
- [x] `docs/tech-update-recommender.yaml.example` создан
- [x] `README.md` написан (установка, quickstart, режимы, конфиг, env vars, ограничения)
- [x] `CLAUDE.md` создан (карта модулей, контракты, тесты)
- [x] `LICENSE` (MIT) в корне
- [x] `tests/test_integration.py` — full pipeline с моками
- [x] `ruff check .` + `ruff format --check .` зелёные
- [ ] Ручной прогон на 2–3 реальных проектах разных экосистем
- [x] Версия 0.1.0 в `pyproject.toml`
- [x] `python -m build` создаёт wheel + sdist
- [x] `twine check dist/*` без ошибок

---

## Сквозные требования (проверять в течение всех блоков)

- [x] Все публичные функции имеют type hints
- [x] Никаких `print()` для статусной информации (только `logging`)
- [x] API-ключи никогда не логируются
- [x] Все внешние вызовы (subprocess, HTTP) обёрнуты в try/except
- [x] Тесты не требуют установленного syft и доступа к интернету
- [x] `pytest -q` зелёный после каждого блока
