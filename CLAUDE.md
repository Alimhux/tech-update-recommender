# CLAUDE.md — DepScope

## Обзор проекта

DepScope — open-source CLI-утилита для локального анализа зависимостей проекта.
Утилита находит все application-level зависимости, проверяет наличие обновлений через deps.dev API,
и опционально генерирует AI-рекомендации по обновлению через LLM (LiteLLM).

Язык: Python 3.11+
Лицензия: MIT

## Архитектура

Три модуля с чёткими границами:

```
CLI (точка входа)
 │
 ├─► SyftModule ──► CycloneDX JSON (SBOM)
 │                        │
 │                        ▼
 ├─► DepsDevModule ──► Отчёт: [{name, ecosystem, current_version, latest_version, is_outdated, semver_diff, advisories}]
 │                        │
 │                        ▼
 └─► LLMModule (опционально) ──► Рекомендации по обновлению (markdown)
```

Данные текут строго в одном направлении: Syft → deps.dev → LLM → вывод.
Каждый модуль можно запустить и протестировать изолированно.

## Структура проекта

```
depscope/
├── CLAUDE.md
├── README.md
├── pyproject.toml
├── depscope/
│   ├── __init__.py
│   ├── cli.py              # Точка входа, argparse/click
│   ├── config.py            # Загрузка конфигурации (~/.depscope.yaml + env vars)
│   ├── syft_module.py       # Запуск syft, парсинг CycloneDX JSON
│   ├── depsdev_module.py    # Batch API deps.dev, сравнение версий
│   ├── llm_module.py        # LiteLLM интеграция, генерация рекомендаций
│   ├── report.py            # Форматирование вывода (JSON, markdown, table)
│   ├── models.py            # Pydantic-модели для контрактов между модулями
│   └── utils.py             # Вспомогательные функции (semver-сравнение, purl-парсинг)
├── tests/
│   ├── test_syft_module.py
│   ├── test_depsdev_module.py
│   ├── test_llm_module.py
│   ├── test_report.py
│   └── fixtures/            # Примеры CycloneDX JSON, ответов deps.dev
└── docs/
```

## Контракты между модулями (КРИТИЧЕСКИ ВАЖНО)

### SyftModule → DepsDevModule

SyftModule возвращает список `PackageInfo`:

```python
class PackageInfo(BaseModel):
    name: str              # "express"
    version: str           # "4.18.2"
    purl: str              # "pkg:npm/express@4.18.2"
    ecosystem: str         # "npm" (извлекается из purl type)
    # ecosystem может быть: npm, pypi, maven, golang, cargo, gem, nuget
    # Если ecosystem не поддерживается deps.dev (deb, apk, rpm и т.д.),
    # пакет помечается как unsupported и НЕ отправляется в deps.dev
```

### DepsDevModule → LLMModule / ReportModule

DepsDevModule возвращает список `DependencyReport`:

```python
class DependencyReport(BaseModel):
    name: str
    ecosystem: str
    current_version: str
    latest_version: str | None       # None если deps.dev не нашёл пакет
    is_outdated: bool
    semver_diff: str | None          # "major", "minor", "patch", None
    advisories: list[Advisory]       # CVE, severity, описание
    all_versions: list[str] | None   # Полный список версий (опционально, для LLM)

class Advisory(BaseModel):
    id: str            # "GHSA-xxxx" или "CVE-xxxx"
    severity: float    # CVSS score 0-10
    summary: str

class FullReport(BaseModel):
    supported: list[DependencyReport]      # Пакеты, проверенные через deps.dev
    unsupported: list[PackageInfo]          # Системные пакеты (deb/apk/rpm), не проверялись
    scan_timestamp: datetime
    project_path: str
    total_packages: int
    outdated_count: int
    vulnerable_count: int
```

### LLMModule принимает:

```python
class LLMInput(BaseModel):
    report: FullReport                  # Отчёт из DepsDevModule
    project_tree: str                   # Вывод tree/find (структура директорий)
    dependency_files: dict[str, str]    # Содержимое файлов зависимостей
    # Ключ — путь к файлу ("requirements.txt", "pyproject.toml", "package.json")
    # Значение — содержимое файла
```

LLMModule НЕ получает исходный код проекта — только структуру и файлы зависимостей.

## Детали реализации по модулям

### 1. CLI (cli.py)

Фреймворк: click (предпочтительно) или argparse.

```
depscope scan <path> [опции]

Обязательные:
  <path>                    Путь к проекту

Опции вывода:
  --output, -o              Формат: table (default), json, markdown
  --mode, -m                Режим: report (default), advice, full
                            report  — только факты (версии, CVE, дельты)
                            advice  — только LLM-рекомендации
                            full    — отчёт + рекомендации
  --only-outdated           Показать только устаревшие пакеты
  --save <file>             Сохранить отчёт в файл

Опции LLM:
  --llm-model               Модель LiteLLM (например "gemini/gemini-2.0-flash", "claude-sonnet-4-20250514")
  --llm-api-key             API-ключ (или через конфиг/env var)
  --no-llm                  Явно отключить LLM (по умолчанию LLM отключен)

Опции Syft:
  --syft-path               Путь к бинарнику syft (если не в PATH)
```

Конфигурация загружается каскадно: CLI аргументы > env vars > ~/.depscope.yaml > defaults.

### 2. SyftModule (syft_module.py)

**Что делает:**
1. Проверяет, что syft установлен (`shutil.which("syft")` или `--syft-path`)
2. Запускает `syft dir:<path> -o cyclonedx-json` через `subprocess.run()`
3. Парсит JSON, извлекает components
4. Для каждого component парсит purl через библиотеку `packageurl-python`
5. Фильтрует: разделяет пакеты на supported (npm, pypi, maven, golang, cargo, gem, nuget) и unsupported (deb, apk, rpm и всё остальное)
6. Возвращает `list[PackageInfo]`

**Важно:**
- Syft хорошо работает с application-level зависимостями (pip, npm, maven, cargo, go, gem, nuget) при наличии lock-файлов
- Системные пакеты (deb/apk/rpm) Syft тоже найдёт, но deps.dev их не поддерживает — поэтому мы их отделяем и показываем отдельно в отчёте
- Если syft не установлен — выводим понятную ошибку с инструкцией по установке
- stdout syft пишем во временный файл, не держим весь JSON в памяти для больших проектов

**Поддерживаемые экосистемы (purl type → deps.dev system):**

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

### 3. DepsDevModule (depsdev_module.py)

**Что делает:**
1. Принимает `list[PackageInfo]` (только supported)
2. Для получения latest-версий: запрашивает `GetPackage` для каждого уникального пакета
   - Endpoint: `GET https://api.deps.dev/v3/systems/{system}/packages/{name}`
   - Ответ содержит список всех версий с пометкой `isDefault: true` для latest
3. Для получения advisories текущей версии: использует batch API
   - Endpoint: `POST https://api.deps.dev/v3alpha/versionbatch`
   - До 5000 пакетов за один запрос
   - Ответ содержит advisories для каждой версии
4. Сравнивает текущую версию с latest, определяет semver_diff
5. Возвращает `FullReport`

**Оптимизация запросов:**
- Batch API для получения информации о текущих версиях (advisories, лицензии): один POST запрос на все пакеты (до 5000)
- GetPackage запросы (для получения latest): выполняем параллельно через asyncio + aiohttp, с ограничением concurrency (семафор, ~20 одновременных запросов)
- Локальный кеш: SQLite или JSON-файл с TTL (~1 час), чтобы повторные запуски не дёргали API заново
- Дедупликация: если несколько версий одного пакета — запрашиваем GetPackage один раз

**Обработка ошибок:**
- Если deps.dev не знает пакет — ставим latest_version=None, is_outdated=False
- Если API временно недоступен — retry с exponential backoff (3 попытки)
- Если пакет есть, но нет default-версии — берём последнюю по дате

**Пример batch-запроса:**

```python
async def fetch_versions_batch(packages: list[PackageInfo]) -> dict:
    payload = {
        "requests": [
            {
                "versionKey": {
                    "system": SUPPORTED_ECOSYSTEMS[p.ecosystem],
                    "name": p.name,
                    "version": p.version,
                }
            }
            for p in packages
        ]
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.deps.dev/v3alpha/versionbatch",
            json=payload,
        ) as resp:
            return await resp.json()
```

### 4. ReportModule (report.py)

**Форматы вывода:**

- **table** (default): человекочитаемая таблица в терминал через `rich` или `tabulate`
- **json**: машиночитаемый JSON (FullReport.model_dump_json())
- **markdown**: markdown-отчёт с таблицами и секциями

**Структура отчёта:**
1. Summary: всего пакетов / устаревших / с уязвимостями
2. Таблица: name | ecosystem | current | latest | diff | advisories
3. Секция unsupported (если есть системные пакеты): краткое упоминание, что X пакетов не проверялось
4. Если mode=full или mode=advice: LLM-рекомендации в отдельной секции с дисклеймером

**Дисклеймер для LLM-секции (обязателен):**
> ⚠️ Рекомендации ниже сгенерированы AI-моделью ({model_name}) и носят рекомендательный характер.
> Качество рекомендаций зависит от выбранной модели. Всегда проверяйте совместимость обновлений
> в вашем проекте перед применением.

### 5. LLMModule (llm_module.py)

**Фреймворк:** LiteLLM — единый интерфейс ко всем LLM API.

**Что делает:**
1. Принимает LLMInput (отчёт + дерево проекта + файлы зависимостей)
2. Формирует промпт (system + user)
3. Вызывает litellm.completion()
4. Возвращает markdown-строку с рекомендациями

**System prompt для LLM (примерный):**

```
Ты — эксперт по управлению зависимостями в software-проектах.
Тебе предоставлен отчёт об устаревших зависимостях проекта,
структура проекта и файлы зависимостей.

Твоя задача:
1. Проанализировать какие обновления безопасны (patch/minor) и какие рискованны (major)
2. Определить связанные пакеты, которые нужно обновлять вместе
3. Предложить порядок обновления (что сначала, что потом)
4. Выделить критичные обновления (с CVE)
5. Предупредить о потенциальных breaking changes в major-обновлениях

Формат ответа: структурированный markdown с секциями:
- 🔴 Критичные обновления (CVE)
- 🟡 Рекомендуемые обновления (major с breaking changes)
- 🟢 Безопасные обновления (minor/patch)
- 📋 Порядок обновления (пошаговый план)
```

**Контекст для LLM (что передаём в user message):**
- Отчёт deps.dev: только outdated и vulnerable пакеты (не все)
- Дерево проекта: `find <path> -type f -not -path '*/node_modules/*' -not -path '*/.git/*' -not -path '*/venv/*' -not -path '*/__pycache__/*'` (ограничить глубину, max ~200 строк)
- Файлы зависимостей: целиком (requirements.txt, pyproject.toml, package.json, Cargo.toml, go.mod, pom.xml, Gemfile, и т.д.)
- НЕ передаём исходный код (не нужен для задачи, забивает контекст)

**Ограничение контекста:**
- Если пакетов слишком много (>200 outdated), передаём только top-50 по severity/semver_diff
- Итоговый промпт не должен превышать ~8000 токенов (чтобы влезть даже в дешёвые модели)
- Для дорогих моделей (Claude, GPT-4) можно передавать больше контекста

**Конфигурация LLM:**
- API-ключ: CLI-аргумент > env var (DEPSCOPE_LLM_API_KEY, или стандартные ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY) > ~/.depscope.yaml
- Модель: CLI-аргумент > конфиг > default (нет default — если модель не указана и mode=advice, выводим ошибку)
- Бесплатный вариант: пользователь может указать `--llm-model gemini/gemini-2.0-flash` с бесплатным Gemini API key

## Конфигурационный файл

Путь: `~/.depscope.yaml`

```yaml
# Настройки LLM
llm:
  model: "gemini/gemini-2.0-flash"    # Модель по умолчанию
  api_key: "sk-..."                     # Или через env var
  max_context_tokens: 8000              # Лимит контекста для промпта

# Настройки кеширования
cache:
  enabled: true
  ttl_seconds: 3600                     # 1 час
  path: "~/.cache/depscope/"

# Настройки Syft
syft:
  path: null                            # null = ищем в PATH
```

## Зависимости проекта (pyproject.toml)

```
dependencies:
  click            # CLI
  packageurl-python # Парсинг purl
  aiohttp          # Async HTTP для deps.dev API
  pydantic         # Модели данных и валидация
  litellm          # Универсальный LLM клиент (опциональная зависимость)
  rich             # Красивый вывод в терминал (таблицы, progress bar)
  pyyaml           # Парсинг конфига

optional-dependencies:
  llm = ["litellm"]

dev-dependencies:
  pytest
  pytest-asyncio
  aioresponses     # Мок aiohttp для тестов
```

## Правила разработки

### Общие
- Весь код типизирован (type hints). Используем Pydantic для валидации данных между модулями
- Async: depsdev_module использует asyncio. Остальные модули синхронные
- Логирование через `logging` (не print). CLI управляет уровнем: --verbose → DEBUG, default → WARNING
- Все внешние вызовы (syft subprocess, HTTP requests) обёрнуты в try/except с понятными сообщениями об ошибках

### Тестирование
- Тесты для каждого модуля отдельно
- Для SyftModule: фикстуры с примерами CycloneDX JSON (tests/fixtures/)
- Для DepsDevModule: мок HTTP через aioresponses
- Для LLMModule: мок litellm.completion
- Интеграционные тесты не требуют установленного syft или доступа к API

### CLI UX
- Progress bar через rich при сканировании и запросах к API
- Понятные ошибки: "syft не найден. Установите: brew install syft" вместо трейсбека
- Если --mode=advice или --mode=full, но LLM не настроен — вывести ошибку и подсказку как настроить
- Цветной вывод: красный для CVE, жёлтый для major updates, зелёный для patch

### Безопасность
- API-ключи никогда не логируются
- API-ключи можно передавать через env vars (не только через конфиг/CLI)
- Конфиг-файл с ключами: рекомендуем chmod 600

## Порядок реализации

1. **Скелет:** pyproject.toml, структура директорий, models.py с Pydantic-моделями, cli.py с заглушками команд
2. **SyftModule:** запуск syft, парсинг JSON, фильтрация по экосистемам, тесты с фикстурами
3. **DepsDevModule:** batch API, GetPackage для latest, сравнение версий, кеш, тесты с моками
4. **ReportModule:** форматирование table/json/markdown, фильтрация --only-outdated
5. **LLMModule:** интеграция LiteLLM, формирование промпта, обработка ответа, дисклеймер
6. **Интеграция:** склейка pipeline в cli.py, конфиг-файл, README, финальные тесты

## Нюансы и известные ограничения

- **Syft требует lock-файлы:** если в проекте только requirements.txt без версий или без lock-файла — Syft найдёт меньше. Это ограничение Syft, мы его документируем в README
- **deps.dev не знает системные пакеты:** deb, apk, rpm пакеты из контейнеров не проверяются. Мы их показываем отдельной секцией "не проверено", а не ошибкой
- **deps.dev API стабильность:** v3 — стабильный с гарантией обратной совместимости. v3alpha — для batch-запросов и purl lookup. Используем v3 где возможно, v3alpha для batch
- **Нормализация имён пакетов:** PyPI нормализует имена (Flask-Babel → flask-babel). deps.dev делает это на своей стороне, но нужно учитывать при кешировании
- **LLM не гарантирует корректность:** рекомендации — advisory. Всегда требуется ручная проверка и тестирование после обновления
