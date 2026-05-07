# Блок 4 — ReportModule

## Цель блока

Превратить `FullReport` в человеко- или машиночитаемый вывод в одном из
трёх форматов: `table`, `json`, `markdown`. Поддержать фильтр
`--only-outdated` и опциональное сохранение в файл.

## Пререквизиты

- Завершён блок 3 (есть `FullReport`).

## Задачи

### 4.1 Публичный API

```python
def render_report(
    report: FullReport,
    fmt: Literal["table", "json", "markdown"],
    only_outdated: bool = False,
    llm_advice: str | None = None,
    llm_model_name: str | None = None,
) -> str:
    ...
```

Возвращает строку для печати/сохранения. Не делает `print` сам —
печать в CLI.

### 4.2 Формат `table` (default)

- Используем `rich.table.Table`.
- Колонки: `name | ecosystem | current | latest | diff | advisories`.
- Цвета:
  - CVE present → красный
  - `semver_diff == "major"` → жёлтый
  - `semver_diff == "patch"` или `"minor"` → зелёный
  - актуальные → серый
- Перед таблицей — summary-строка:
  `"Total: 142, outdated: 27, with CVE: 4"`.
- После таблицы — секция unsupported:
  `"⚠ Не проверено через deps.dev: 12 системных пакетов (deb/apk/rpm)"`.
- Возвращаем строку через `Console(record=True).export_text()` или
  аналогично — чтобы вывод можно было сохранить в файл.

### 4.3 Формат `json`

- Используем `report.model_dump_json(indent=2)`.
- Если `only_outdated` — фильтруем `supported` перед сериализацией.
- LLM-advice добавляем как отдельное поле `"llm_advice"` в корне (если есть).
  Для этого либо собираем dict вручную, либо делаем `model_dump()` и
  добавляем ключ.

### 4.4 Формат `markdown`

- Заголовок `# DepScope Report`.
- Метаданные: путь, timestamp.
- Summary-блок (`**Total:** ...`).
- Таблица в markdown-формате (вручную или через `tabulate`):
  ```
  | Name | Ecosystem | Current | Latest | Diff | Advisories |
  |------|-----------|---------|--------|------|------------|
  ```
- Секция unsupported.
- Если есть `llm_advice` — отдельная секция «## AI-рекомендации»
  с дисклеймером (см. 4.6) и содержимым.

### 4.5 Фильтр `--only-outdated`

- Применяется до рендера: пакеты с `is_outdated == False` исключаются
  из таблицы / json (у unsupported не применяется — они и так отдельной секцией).
- Summary-блок считаем по полному отчёту (чтобы пользователь видел общее число).

### 4.6 Дисклеймер LLM-секции (обязателен)

Текст дисклеймера — точно как в PLAN.md:

```
⚠️ Рекомендации ниже сгенерированы AI-моделью ({model_name}) и носят
рекомендательный характер. Качество рекомендаций зависит от выбранной
модели. Всегда проверяйте совместимость обновлений в вашем проекте
перед применением.
```

`{model_name}` подставляется из аргумента `llm_model_name`.

### 4.7 Сохранение в файл (`--save`)

- В CLI: после `render_report` — `Path(save).write_text(output)`.
- Кодировка: UTF-8.
- Расширение не валидируем (пользователь сам выбирает).

## Критерии приёмки

- [ ] `render_report(..., fmt="table")` печатает корректную таблицу с цветами.
- [ ] `render_report(..., fmt="json")` валидируется как JSON
      (json.loads без ошибок).
- [ ] Markdown-вывод корректно отображается на GitHub (визуальная проверка).
- [ ] `only_outdated=True` исключает актуальные пакеты.
- [ ] LLM-секция содержит дисклеймер с подставленным именем модели.

## Тесты блока

### Тест-кейсы (`tests/test_report.py`)

- `test_json_format_valid` — вывод парсится через `json.loads`.
- `test_json_includes_llm_advice` — поле `llm_advice` в корне.
- `test_only_outdated_filter` — актуальные пакеты исключены.
- `test_markdown_has_disclaimer` — дисклеймер с именем модели.
- `test_unsupported_section_present` — при наличии unsupported есть
  соответствующая секция.
- `test_summary_counts` — summary считается по полному отчёту, не по фильтру.
- `test_table_no_crash_empty` — пустой отчёт не падает, печатает summary.

Для table-формата проверяем подстроки в выводе (через `Console(record=True)`),
не визуальное соответствие.

## Подводные камни

- Кодировка терминала: на Windows эмодзи в дисклеймере могут не отрендериться.
  Опционально: флаг `--no-emoji` или fallback. Решаем по факту.
- Большие отчёты (1000+ пакетов): table в rich может тормозить — делаем
  `pagination` опционально, но в первой версии не критично.
- При `fmt="json"` поля `datetime` сериализуем в ISO 8601 (Pydantic 2 это
  делает по умолчанию).

## Что НЕ делаем в этом блоке

- Не дёргаем LLM — мы только принимаем готовую markdown-строку.
- Не делаем CLI-склейку (это блок 6).
- Не делаем экспорт в HTML/CSV.
