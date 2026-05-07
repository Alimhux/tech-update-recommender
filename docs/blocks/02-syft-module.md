# Блок 2 — SyftModule

## Цель блока

Реализовать модуль, который запускает `syft`, парсит CycloneDX JSON,
извлекает purl каждого компонента и возвращает `list[PackageInfo]`
с разделением на поддерживаемые и неподдерживаемые экосистемы.

## Пререквизиты

- Завершён блок 1 (есть Pydantic-модели и каркас CLI).
- `syft` установлен на машине разработчика
  (`brew install syft` или [официальная инструкция](https://github.com/anchore/syft)).

## Задачи

### 2.1 Проверка наличия syft

- Функция `find_syft_binary(custom_path: str | None) -> str`.
- Логика: если `custom_path` задан — используем его, иначе `shutil.which("syft")`.
- Если не найдено — кидаем `SyftNotFoundError` (своё исключение)
  с понятным сообщением: «syft не найден. Установите: `brew install syft`
  или скачайте с https://github.com/anchore/syft/releases».

### 2.2 Запуск syft

- Функция `run_syft(project_path: str, syft_binary: str) -> Path`.
- Вызов: `syft dir:<project_path> -o cyclonedx-json` через `subprocess.run`.
- stdout пишем во временный файл (`tempfile.NamedTemporaryFile`),
  возвращаем путь. Не держим JSON целиком в памяти.
- При ненулевом коде возврата — кидаем `SyftExecutionError`
  с stderr в сообщении.
- Logging уровня INFO: «running syft on <path>...».

### 2.3 Парсинг CycloneDX

- Функция `parse_cyclonedx(json_path: Path) -> list[PackageInfo]`.
- Используем стандартный `json.load`, читаем поле `components`.
- Для каждого компонента берём `purl` (если поля нет — пропускаем компонент,
  логируем DEBUG).
- Парсим purl через `packageurl.PackageURL.from_string(purl)`.
- Извлекаем:
  - `name` — `purl.name` (для maven — `f"{purl.namespace}:{purl.name}"`).
  - `version` — `purl.version`.
  - `ecosystem` — `purl.type` (lowercase).
  - `purl` — оригинальная строка.

### 2.4 Фильтрация экосистем

- Константа в модуле:
  ```python
  SUPPORTED_ECOSYSTEMS = {
      "npm":    "NPM",
      "pypi":   "PYPI",
      "maven":  "MAVEN",
      "golang": "GO",
      "cargo":  "CARGO",
      "gem":    "RUBYGEMS",
      "nuget":  "NUGET",
  }
  ```
- Функция `split_supported(packages: list[PackageInfo]) -> tuple[list, list]`
  разделяет на supported / unsupported.
- Дедупликация: если один и тот же `(name, version, ecosystem)` встречается
  несколько раз — оставляем один.

### 2.5 Публичный API модуля

```python
def scan_project(
    project_path: str,
    syft_path: str | None = None,
) -> tuple[list[PackageInfo], list[PackageInfo]]:
    """Возвращает (supported, unsupported)."""
```

Это единственная функция, которую дёргает CLI.

### 2.6 Исключения

- `SyftNotFoundError`
- `SyftExecutionError`
- `SyftParseError` (на случай битого JSON)

Все наследуются от общего `SyftError(Exception)`.

## Критерии приёмки

- [ ] `scan_project("./testproject")` возвращает корректные tuple.
- [ ] При отсутствии syft — понятная ошибка, не traceback.
- [ ] Системные пакеты (deb/apk/rpm) попадают в `unsupported`.
- [ ] Дубликаты схлопываются.

## Тесты блока

Покрытие через моки и фикстуры — без реального запуска syft.

### Фикстуры (`tests/fixtures/`)

- `cyclonedx_simple.json` — 3 npm пакета.
- `cyclonedx_mixed.json` — npm + pypi + deb (системные).
- `cyclonedx_maven.json` — maven с namespace.
- `cyclonedx_empty.json` — пустой `components`.
- `cyclonedx_broken.json` — невалидный JSON для теста ошибки парсинга.

### Тест-кейсы (`tests/test_syft_module.py`)

- `test_parse_simple` — фикстура `cyclonedx_simple.json` → 3 пакета npm.
- `test_split_supported_unsupported` — `cyclonedx_mixed.json`:
  npm/pypi → supported, deb → unsupported.
- `test_maven_namespace` — namespace корректно склеивается.
- `test_dedup` — повторяющиеся пакеты схлопнуты.
- `test_empty_components` — возвращается пустой список без ошибок.
- `test_broken_json` — `SyftParseError`.
- `test_syft_not_found` — мок `shutil.which` → `SyftNotFoundError`.
- `test_syft_nonzero_exit` — мок `subprocess.run` → `SyftExecutionError`.

Использовать `pytest.monkeypatch` или `unittest.mock.patch` для subprocess.

## Подводные камни

- Покетные purl-строки: maven использует `namespace:name`, у gem нет namespace,
  у golang `purl.namespace` может содержать domain (`github.com/foo`) —
  тестировать на реальных фикстурах syft.
- Большие проекты: stdout syft может быть в десятки MB → пишем в файл, читаем
  потоково при необходимости.
- syft на macOS иногда требует `XDG_DATA_HOME` — пробрасываем env как есть.
- Если в проекте нет lock-файлов, syft найдёт мало пакетов. Это нормально —
  предупреждение в README, не падаем.

## Что НЕ делаем в этом блоке

- Не ходим в deps.dev — это блок 3.
- Не форматируем отчёт.
- Не пишем интеграционные тесты с реальным syft (только модульные с моками).
