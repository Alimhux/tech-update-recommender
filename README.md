# DepScope

DepScope — open-source CLI-утилита для локального анализа зависимостей проекта.
Она находит application-level зависимости через [Syft](https://github.com/anchore/syft),
проверяет их версии и известные уязвимости через [deps.dev](https://deps.dev),
и опционально генерирует AI-рекомендации по обновлению через
[LiteLLM](https://github.com/BerriAI/litellm).

Ключевые возможности:

- Локально, без отправки исходного кода на сервер.
- Поддержка ecosystem'ов npm, PyPI, Maven, Go, Cargo, RubyGems, NuGet.
- Кеш ответов deps.dev (SQLite, TTL 1 час) — повторные запуски быстрые.
- Несколько форматов вывода: `table` (rich), `json`, `markdown`.
- Выбор LLM-провайдера через LiteLLM (Anthropic / OpenAI / Gemini / Ollama).

## Установка

DepScope требует Python 3.11+.

```bash
# Из исходников (для разработки):
pip install -e ".[llm]"

# Когда пакет будет опубликован на PyPI:
pip install depscope[llm]
# или через pipx:
pipx install "depscope[llm]"
```

Группа `[llm]` ставит `litellm`. Без неё DepScope работает в режимах
`report` (без LLM) и просто возвращает понятную ошибку, если запросить
`advice`/`full`.

### Установка Syft

DepScope не пытается тянуть Syft в зависимостях — этот инструмент удобнее
ставить системно.

```bash
# macOS:
brew install syft

# Linux / любой UNIX (официальный установщик):
curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh | sh -s -- -b /usr/local/bin
```

Если syft установлен в нестандартное место — передайте путь через
`--syft-path` или поле `syft.path` в `~/.depscope.yaml`.

## Quickstart

```bash
# 1. Простой отчёт по фактам (без LLM):
depscope scan ./my-project

# 2. Полный отчёт с AI-рекомендациями:
depscope scan ./my-project --mode full \
    --llm-model gemini/gemini-2.0-flash

# 3. JSON в файл:
depscope scan ./my-project --output json --save out.json
```

## Режимы работы

| Режим    | Что делает                                                           |
|----------|----------------------------------------------------------------------|
| `report` | (default) только факты: версии, дельты semver, advisories.           |
| `advice` | только AI-рекомендации (без таблицы фактов в LLM-секции — но summary остаётся). |
| `full`   | факты + AI-рекомендации.                                             |

Режимы `advice` и `full` требуют указать LLM-модель (через CLI-аргумент,
env var или конфиг). Без модели DepScope сообщит ошибку конфигурации
(`exit code 5`) и подскажет, что делать.

Флаг `--no-llm` принудительно понижает режим до `report` — удобно,
если конфиг по умолчанию содержит модель, но прямо сейчас не хочется
дёргать API.

## Конфигурационный файл

Путь по умолчанию: `~/.depscope.yaml`. Файл опционален — при отсутствии
используются значения по умолчанию.

Пример полной структуры см. в `docs/depscope.yaml.example`. Минимальный
вариант:

```yaml
llm:
  model: "gemini/gemini-2.0-flash"
  max_context_tokens: 8000

cache:
  enabled: true
  ttl_seconds: 3600
  path: "~/.cache/depscope/"
```

API-ключи лучше держать в env vars, а не в файле. Если всё-таки храните
в файле — `chmod 600 ~/.depscope.yaml`.

## Переменные окружения

| Переменная              | Что задаёт                                              |
|-------------------------|---------------------------------------------------------|
| `DEPSCOPE_LLM_MODEL`    | Имя LLM-модели (как `--llm-model`).                     |
| `DEPSCOPE_LLM_API_KEY`  | Универсальный API-ключ DepScope.                        |
| `ANTHROPIC_API_KEY`     | Используется, если `DEPSCOPE_LLM_API_KEY` не задан.     |
| `OPENAI_API_KEY`        | То же самое.                                            |
| `GEMINI_API_KEY`        | То же самое.                                            |
| `DEPSCOPE_SYFT_PATH`    | Путь к бинарнику syft (как `--syft-path`).              |

Каскад приоритетов значений (от высшего к низшему):
CLI > env vars > `~/.depscope.yaml` > дефолты.

## Поддерживаемые экосистемы

Экосистемы, для которых DepScope умеет проверять версии и advisories
через deps.dev:

- `npm`
- `pypi`
- `maven`
- `golang`
- `cargo`
- `gem` (RubyGems)
- `nuget`

Системные пакеты (`deb`, `apk`, `rpm` и т.п.), найденные Syft, не
проверяются — они показываются отдельной секцией «Не проверено через
deps.dev» (это ограничение API deps.dev, а не DepScope).

## Известные ограничения

- **Syft требует lock-файлы.** Если в проекте только `requirements.txt`
  без зафиксированных версий или без lock-файла — Syft найдёт меньше
  зависимостей, чем хотелось бы.
- **deps.dev не знает системные пакеты.** Контейнерные пакеты (deb / apk
  / rpm) выводятся отдельной секцией без проверки на устаревание/CVE.
- **deps.dev v3alpha может меняться.** Batch endpoint используется для
  получения advisories текущих версий; в случае поломки API — DepScope
  выдаст понятную ошибку и не упадёт с traceback.
- **LLM не гарантирует корректность рекомендаций.** Это всегда advisory:
  проверяйте совместимость и тестируйте обновления.
- **Нормализация имён пакетов.** PyPI нормализует имена
  (`Flask-Babel` → `flask-babel`). DepScope делает это на своей стороне
  для запросов и кеша.

## Коды возврата

| Код | Значение                                                  |
|-----|-----------------------------------------------------------|
| 0   | Успех.                                                    |
| 1   | Любая прочая ошибка (с подсказкой использовать `--verbose`). |
| 2   | Ошибка Syft (`SyftError`).                                |
| 3   | Ошибка deps.dev (`DepsDevError`).                         |
| 4   | Ошибка LLM (`LLMError` и его подклассы).                  |
| 5   | Ошибка конфигурации (`ConfigError`).                      |
| 130 | Отменено пользователем (`Ctrl+C`).                        |

## Тесты и линтер

```bash
pytest -q
ruff check .
ruff format --check .
```

## Лицензия

MIT — см. файл [`LICENSE`](LICENSE).
