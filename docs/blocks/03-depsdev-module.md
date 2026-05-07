# Блок 3 — DepsDevModule

## Цель блока

По списку `PackageInfo` (только supported) собрать данные через
deps.dev API: latest-версии, advisories, semver-разница. Результат —
объект `FullReport`, готовый для отчёта и LLM.

## Пререквизиты

- Завершён блок 2 (есть `scan_project`).
- Доступ к интернету для интеграционных проверок (опциональный).

## Задачи

### 3.1 Async HTTP-клиент

- Один `aiohttp.ClientSession` на запуск, передаётся в функции.
- Таймаут запроса: 30 секунд.
- Retry с экспоненциальным бэкоффом (3 попытки) на 5xx и сетевые ошибки.
- 4xx (особенно 404 — пакет не найден) — НЕ retry, обрабатываем как «нет данных».

### 3.2 Batch-запрос текущих версий

- Endpoint: `POST https://api.deps.dev/v3alpha/versionbatch`.
- До 5000 пакетов на запрос. Если больше — разбиваем на чанки.
- Реализация:
  ```python
  async def fetch_current_versions(
      session: aiohttp.ClientSession,
      packages: list[PackageInfo],
  ) -> dict[tuple[str, str, str], dict]:
      """Возвращает dict (system, name, version) -> ответ deps.dev."""
  ```
- Из ответа извлекаем advisories для каждой версии.

### 3.3 GetPackage для latest

- Endpoint: `GET https://api.deps.dev/v3/systems/{system}/packages/{name}`
  (имя URL-encoded, для maven — формат `groupId:artifactId`).
- Параллельно через семафор `asyncio.Semaphore(20)`.
- Дедупликация: если несколько `PackageInfo` с одним `(system, name)` —
  один запрос на пакет.
- Ответ содержит список версий; latest — это версия с `isDefault: true`.
  Если такой нет — берём последнюю по дате публикации.
- Реализация:
  ```python
  async def fetch_latest_versions(
      session: aiohttp.ClientSession,
      packages: list[PackageInfo],
  ) -> dict[tuple[str, str], str | None]:
      """Возвращает dict (system, name) -> latest_version."""
  ```

### 3.4 Сравнение версий и semver_diff

- В `depscope/utils.py` функция `compute_semver_diff(current, latest) -> str | None`:
  - `"major"` если major увеличился
  - `"minor"` если minor
  - `"patch"` если patch
  - `None` если версии равны
- Поддерживать pre-release и нестрогие SemVer-схемы (Maven, PyPI).
  Использовать `packaging.version.Version` для PyPI и обёртку для остальных.
  Если парсинг падает — возвращаем `None` и `is_outdated = current != latest`.

### 3.5 Кеш

- Класс `Cache` в отдельном файле `depscope/cache.py`.
- Бэкенд: SQLite (`stdlib sqlite3`) с таблицей
  `(system, name, version, payload, fetched_at)`.
- TTL — из конфига (`cache.ttl_seconds`, default 3600).
- Путь — из конфига (`~/.cache/depscope/cache.db`).
- API:
  - `get(system, name, version) -> dict | None`
  - `set(system, name, version, payload: dict) -> None`
  - `clear() -> None`
- Кешируем как ответы batch (по конкретным версиям), так и GetPackage
  (по `(system, name, "__latest__")`).

### 3.6 Сборка `FullReport`

```python
async def build_report(
    supported: list[PackageInfo],
    unsupported: list[PackageInfo],
    project_path: str,
    cache: Cache,
) -> FullReport:
    ...
```

Шаги:
1. Дедуплицируем входные пакеты.
2. Для каждого — пробуем достать из кеша.
3. Что не в кеше — собираем в batch и параллельные GetPackage.
4. Складываем результаты в кеш.
5. Маппим в `DependencyReport`, заполняя `latest_version`, `is_outdated`,
   `semver_diff`, `advisories`.
6. Считаем сводку: `total_packages`, `outdated_count`, `vulnerable_count`.
7. Возвращаем `FullReport`.

### 3.7 Обработка ошибок

- Пакет не найден в deps.dev → `latest_version=None`, `is_outdated=False`,
  `advisories=[]`. Лог DEBUG.
- API недоступен 3 раза подряд → `DepsDevError` с понятным сообщением.
- Невалидная версия → `semver_diff=None`, всё остальное заполняем.

## Критерии приёмки

- [ ] `build_report(...)` возвращает корректный `FullReport` для тестового набора.
- [ ] Повторный запуск использует кеш (видно по логам или счётчику запросов).
- [ ] Пакеты с CVE имеют непустой `advisories`.
- [ ] При сетевой ошибке — внятное сообщение, не traceback.

## Тесты блока

### Фикстуры

- `tests/fixtures/depsdev_batch_response.json` — пример ответа `versionbatch`.
- `tests/fixtures/depsdev_getpackage_express.json` — пример `GetPackage`.
- `tests/fixtures/depsdev_404.json` — пакет не найден.

### Тест-кейсы (`tests/test_depsdev_module.py`)

- `test_compute_semver_diff` — таблица параметров: major/minor/patch/equal.
- `test_fetch_current_versions_batch` — мок `aioresponses`, проверка payload.
- `test_fetch_latest_versions_dedup` — два пакета одного имени → один запрос.
- `test_404_handling` — пакет не найден → `latest_version=None`.
- `test_retry_on_5xx` — два 503 затем 200 → успех после retry.
- `test_cache_hit` — повторный вызов не делает HTTP-запрос.
- `test_advisory_parsing` — ответ с CVE → `Advisory` с правильным `severity`.
- `test_build_report_summary` — корректные `outdated_count`, `vulnerable_count`.

Использовать `aioresponses` для мока aiohttp.

## Подводные камни

- Нормализация имён пакетов: PyPI lowercase + `-`/`_` нормализация
  (`Flask-Babel` → `flask-babel`). При формировании URL — нормализуем
  на нашей стороне до отправки.
- Maven: имя — это `groupId:artifactId`, в URL нужно экранировать `:` как `%3A`.
- Golang: имена с `/` (например `github.com/foo/bar`) — URL-encode
  всё имя целиком.
- Параллельные запросы могут перегрузить deps.dev — держим concurrency 20.
- v3 vs v3alpha: GetPackage — v3, batch — v3alpha. Не путать.

## Что НЕ делаем в этом блоке

- Не вызываем LLM.
- Не форматируем вывод (table/json/markdown — это блок 4).
- Не пишем CLI-склейку.
