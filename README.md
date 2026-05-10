# Tech Update Recommender

Tech Update Recommender — open-source CLI-утилита для локального анализа зависимостей проекта.
Она находит application-level зависимости через [Syft](https://github.com/anchore/syft),
проверяет их версии и известные уязвимости через [deps.dev](https://deps.dev),
и опционально генерирует AI-рекомендации по обновлению через
[LiteLLM](https://github.com/BerriAI/litellm).

Ключевые возможности:

- Локально, без отправки исходного кода на сервер.
- Поддержка ecosystem'ов npm, PyPI, Maven, Go, Cargo, RubyGems, NuGet.
- Кеш ответов deps.dev (SQLite, TTL 1 час) — повторные запуски быстрые.
- Несколько форматов вывода: `table` (rich), `json`, `markdown`.
- Выбор LLM-провайдера через LiteLLM (OpenAI / Anthropic / Gemini / Yandex Cloud / Ollama и любые OpenAI-совместимые API).

## Установка

Требует Python 3.11+.

```bash
# Из PyPI:
pip install tech-upd-recommender

# или через pipx:
pipx install tech-upd-recommender

# Из исходников (для разработки):
pip install -e ".[dev]"
```

Все зависимости (включая LiteLLM для AI-рекомендаций) ставятся автоматически.

### Установка Syft

Syft нужно ставить отдельно — это системный инструмент для сканирования зависимостей.

```bash
# macOS:
brew install syft

# Linux / любой UNIX (официальный установщик):
curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh | sh -s -- -b /usr/local/bin
```

Если syft установлен в нестандартное место — передайте путь через
`--syft-path` или поле `syft.path` в `~/.tech-update-recommender.yaml`.

## Quickstart

```bash
# 1. Простой отчёт по фактам (без LLM):
tech-upd-recommender scan ./my-project

# 2. Полный отчёт с AI-рекомендациями (Gemini):
tech-upd-recommender scan ./my-project --mode full \
    --llm-model gemini/gemini-2.0-flash

# 3. С OpenAI-совместимым провайдером (Yandex Cloud, DeepSeek и т.п.):
export OPENAI_API_BASE=https://ai.api.cloud.yandex.net/v1
export OPENAI_API_KEY=ваш_ключ
tech-upd-recommender scan ./my-project --mode full \
    --llm-model "openai/gpt://folder_id/model_name"

# 4. JSON в файл:
tech-upd-recommender scan ./my-project --output json --save out.json
```

## Режимы работы

| Режим    | Что делает                                                           |
|----------|----------------------------------------------------------------------|
| `report` | (default) только факты: версии, дельты semver, advisories.           |
| `advice` | только AI-рекомендации (без таблицы фактов в LLM-секции — но summary остаётся). |
| `full`   | факты + AI-рекомендации.                                             |

Режимы `advice` и `full` требуют указать LLM-модель (через CLI-аргумент,
env var или конфиг). Без модели будет ошибка конфигурации (`exit code 5`)
с подсказкой что делать.

Флаг `--no-llm` принудительно понижает режим до `report` — удобно,
если конфиг по умолчанию содержит модель, но прямо сейчас не хочется
дёргать API.

## Конфигурационный файл

Путь по умолчанию: `~/.tech-update-recommender.yaml`. Файл опционален — при отсутствии
используются значения по умолчанию.

```yaml
llm:
  model: "gemini/gemini-2.0-flash"
  max_context_tokens: 8000

cache:
  enabled: true
  ttl_seconds: 3600
  path: "~/.cache/tech-update-recommender/"
```

API-ключи лучше держать в env vars, а не в файле. Если всё-таки храните
в файле — `chmod 600 ~/.tech-update-recommender.yaml`.

## Переменные окружения

| Переменная              | Что задаёт                                              |
|-------------------------|---------------------------------------------------------|
| `TUR_LLM_MODEL`        | Имя LLM-модели (как `--llm-model`).                     |
| `TUR_LLM_API_KEY`      | Универсальный API-ключ.                                 |
| `OPENAI_API_KEY`        | API-ключ для OpenAI-совместимых провайдеров.            |
| `OPENAI_API_BASE`       | URL эндпоинта для OpenAI-совместимых провайдеров (Yandex Cloud, DeepSeek и др.). |
| `ANTHROPIC_API_KEY`     | API-ключ Anthropic.                                     |
| `GEMINI_API_KEY`        | API-ключ Google Gemini.                                 |
| `TUR_SYFT_PATH`         | Путь к бинарнику syft (как `--syft-path`).              |

Каскад приоритетов значений (от высшего к низшему):
CLI > env vars > `~/.tech-update-recommender.yaml` > дефолты.

## Поддерживаемые экосистемы

| Экосистема | Примеры файлов зависимостей |
|---|---|
| `npm` | package.json, package-lock.json |
| `pypi` | requirements.txt, pyproject.toml, Pipfile |
| `maven` | pom.xml |
| `golang` | go.mod, go.sum |
| `cargo` | Cargo.toml, Cargo.lock |
| `gem` (RubyGems) | Gemfile, Gemfile.lock |
| `nuget` | *.csproj, packages.config |

Системные пакеты (`deb`, `apk`, `rpm` и т.п.), найденные Syft, не
проверяются — они показываются отдельной секцией «Не проверено через
deps.dev» (это ограничение API deps.dev).

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

## Разработка

```bash
# Установить с dev-зависимостями:
pip install -e ".[dev]"

# Тесты:
pytest -q

# Линтер:
ruff check .
ruff format --check .
```

## Лицензия

MIT — см. файл [`LICENSE`](LICENSE).
