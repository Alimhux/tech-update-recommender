# Блок 6 — Интеграция и финализация

## Цель блока

Склеить все модули в работающий pipeline через `cli.py`, дописать конфиг,
README, финальные интеграционные тесты и подготовить релиз 0.1.0.

## Пререквизиты

- Завершены блоки 1–5.
- Все модули покрыты модульными тестами.

## Задачи

### 6.1 Финальный CLI pipeline (depscope/cli.py)

Реализовать команду `scan` целиком:

```python
@click.command()
@click.argument("path")
# ... все опции ...
def scan(path, output, mode, only_outdated, save, llm_model, ...):
    # 1. Загрузить конфиг (CLI > env > ~/.depscope.yaml > defaults)
    config = load_config(cli_overrides)

    # 2. Включить логирование
    setup_logging(verbose=config.verbose)

    # 3. SyftModule
    with progress_bar("Scanning project..."):
        supported, unsupported = scan_project(path, syft_path=config.syft.path)

    # 4. DepsDevModule
    with progress_bar("Querying deps.dev..."):
        report = asyncio.run(build_report(supported, unsupported, path, cache))

    # 5. LLMModule (если нужен)
    advice = None
    if mode in ("advice", "full"):
        with progress_bar("Generating AI advice..."):
            llm_input = build_llm_input(report, path)
            advice = generate_advice(llm_input, model=config.llm.model, ...)

    # 6. ReportModule
    output_text = render_report(
        report, fmt=output, only_outdated=only_outdated,
        llm_advice=advice if mode != "report" else None,
        llm_model_name=config.llm.model if advice else None,
    )

    # 7. Печать или сохранение
    if save:
        Path(save).write_text(output_text, encoding="utf-8")
    else:
        click.echo(output_text)
```

Прогресс-бары через `rich.progress.Progress`.

### 6.2 Обработка ошибок верхнего уровня

- В `main()` обернуть `scan` в try/except:
  - `SyftError` → красное сообщение в stderr, exit 2.
  - `DepsDevError` → exit 3.
  - `LLMError` → exit 4.
  - `ConfigError` → exit 5.
  - `KeyboardInterrupt` → «Отменено пользователем», exit 130.
  - Прочие — traceback только при `--verbose`, иначе короткое сообщение.

### 6.3 Конфигурационный файл

- Создать пример `docs/depscope.yaml.example` (структура из PLAN.md).
- Реализовать в `depscope/config.py` загрузку из `~/.depscope.yaml`,
  если файл существует.
- Документировать в README, как создать конфиг.

### 6.4 README.md

Минимальный, но полноценный:

- Что делает DepScope (1 абзац).
- Установка: pip / pipx, опциональные группы (`[llm]`).
- Установка syft (ссылка на anchore/syft, brew, curl-скрипт).
- Quickstart:
  ```
  depscope scan ./my-project
  depscope scan ./my-project --mode full --llm-model gemini/gemini-2.0-flash
  ```
- Описание режимов (`report`, `advice`, `full`).
- Конфиг-файл — пример и где лежит.
- Env vars: `DEPSCOPE_LLM_API_KEY`, `ANTHROPIC_API_KEY`, …
- Поддерживаемые экосистемы — список.
- Известные ограничения (из PLAN.md, секция «Нюансы»).
- Лицензия: MIT.

### 6.5 CLAUDE.md

Краткий файл-«ориентир» для будущих агентов:

- Где что лежит (карта модулей).
- Контракты между модулями (краткая выжимка).
- Как запускать тесты, линтер.
- Что **не** делать (не печатать API-ключи, не превращать pipeline в agentic).

### 6.6 Интеграционные тесты

`tests/test_integration.py`:

- `test_full_pipeline_with_mocks` — мокаем syft + deps.dev + LLM,
  гоняем `scan` целиком, проверяем выходной markdown.
- `test_mode_report_no_llm_call` — в режиме `report` LLM не дёргается.
- `test_only_outdated_end_to_end` — флаг доходит до отчёта.
- `test_save_to_file` — `--save` создаёт файл с корректным содержимым.

### 6.7 Линтер и форматтер

- `ruff` (lint + format): добавить в dev-деps, конфиг в `pyproject.toml`.
- Целевые проверки: `E`, `F`, `I` (imports), `B` (bugbear).
- Запуск: `ruff check . && ruff format --check .`.

### 6.8 Pre-release-чеклист

- [ ] `pytest -q` — все тесты зелёные.
- [ ] `ruff check . && ruff format --check .` — без ошибок.
- [ ] Ручной прогон на 2–3 реальных проектах разных экосистем.
- [ ] README актуален.
- [ ] `pyproject.toml`: версия 0.1.0, classifiers, license, авторы.
- [ ] `LICENSE` файл (MIT) в корне.

### 6.9 Сборка пакета

- `python -m build` → `dist/depscope-0.1.0-py3-none-any.whl` + sdist.
- `twine check dist/*` — без ошибок.
- Публикацию на PyPI оставляем за пользователем (не автоматизируем
  в рамках курсовой).

## Критерии приёмки

- [ ] `depscope scan <реальный_проект>` выводит корректный отчёт
      в режимах `report`, `advice`, `full`.
- [ ] `depscope scan ... --output json --save out.json` создаёт
      валидный JSON-файл.
- [ ] При ошибке (нет syft, нет API-ключа) — понятное сообщение, не traceback.
- [ ] Все интеграционные тесты зелёные.
- [ ] README достаточен для нового пользователя без обращения к коду.

## Подводные камни

- В `asyncio.run` нельзя вкладывать вызовы — следим, чтобы не вызывался
  из уже работающего event loop (в Jupyter, например).
- При сохранении в файл с `fmt=table` — rich-цвета должны быть отключены
  или сохранены как ANSI. Решение: `Console(force_terminal=False)` при сохранении.
- Прогресс-бары не должны портить вывод JSON в stdout — печатать прогресс
  в stderr, результат в stdout.
- Кеш SQLite: при первом запуске файл может не существовать — создаём
  директорию `~/.cache/depscope/` через `mkdir parents=True`.

## Что НЕ делаем в этом блоке

- Не публикуем на PyPI автоматически.
- Не делаем GitHub Actions CI (можно отдельной задачей после релиза).
- Не делаем GUI/web-интерфейс.
