# Блок 5 — LLMModule

## Цель блока

По `FullReport` + структуре проекта + файлам зависимостей сгенерировать
markdown-рекомендации через LiteLLM. Поддержать любую модель (Gemini,
Claude, GPT, Ollama и т.д.) единым интерфейсом.

## Пререквизиты

- Завершён блок 3 (есть `FullReport`).
- Установлена опциональная зависимость `litellm`
  (`pip install -e ".[llm]"`).

## Задачи

### 5.1 Сбор контекста (depscope/llm_module.py)

#### 5.1.1 Дерево проекта

- Функция `collect_project_tree(path: str, max_lines: int = 200) -> str`.
- Эквивалент:
  ```
  find <path> -type f \
    -not -path '*/node_modules/*' \
    -not -path '*/.git/*' \
    -not -path '*/venv/*' \
    -not -path '*/__pycache__/*' \
    -not -path '*/.venv/*' \
    -not -path '*/dist/*' \
    -not -path '*/build/*'
  ```
- Реализуем на чистом Python через `pathlib`.
- Глубина — до 4 уровней.
- Ограничение по строкам: `max_lines` (по умолчанию 200), при превышении —
  усечение с пометкой `... (truncated, N more files)`.

#### 5.1.2 Файлы зависимостей

- Функция `collect_dependency_files(path: str) -> dict[str, str]`.
- Ищем стандартные файлы в корне и подкаталогах:
  - `requirements.txt`, `requirements-*.txt`
  - `pyproject.toml`
  - `Pipfile`, `Pipfile.lock`
  - `package.json`, `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`
  - `Cargo.toml`, `Cargo.lock`
  - `go.mod`, `go.sum`
  - `pom.xml`, `build.gradle`, `build.gradle.kts`
  - `Gemfile`, `Gemfile.lock`
  - `*.csproj`, `packages.config`
- Возвращаем `{relative_path: content}`.
- Для очень больших lock-файлов (>200 KB) — пропускаем целиком,
  логируем DEBUG.

### 5.2 Сборка `LLMInput`

```python
def build_llm_input(
    report: FullReport,
    project_path: str,
) -> LLMInput:
    ...
```

- Берём только `outdated` или `vulnerable` пакеты из `report.supported`.
- Если их больше 200 — отсортировать по приоритету:
  - сначала по числу advisories
  - потом по `semver_diff`: major > minor > patch
- Берём top-50.
- Дерево + dependency-файлы — через функции выше.

### 5.3 Промпт

#### System prompt

Брать дословно из PLAN.md, секция «System prompt для LLM».
Хранить в коде как многострочный литерал (или `prompts/system.md`).

#### User prompt

Структура:

```
Отчёт об устаревших и уязвимых зависимостях:
<JSON или markdown с топ-50 пакетами>

Структура проекта:
<вывод collect_project_tree>

Файлы зависимостей:
=== requirements.txt ===
<содержимое>

=== package.json ===
<содержимое>
...

Сформируй рекомендации в указанном формате.
```

### 5.4 Лимит контекста

- В конфиге: `llm.max_context_tokens` (default 8000).
- Для оценки токенов используем `litellm.token_counter(model=..., text=...)`
  если доступен, иначе грубая оценка `len(text) // 4`.
- Алгоритм усечения при превышении лимита:
  1. Сначала уменьшаем `max_lines` дерева до 100.
  2. Затем усекаем большие dependency-файлы (>10 KB) до первых 200 строк.
  3. Затем уменьшаем top-N пакетов с 50 до 20.
- Если всё равно не влезает — кидаем `LLMContextOverflowError`.

### 5.5 Вызов LiteLLM

```python
def generate_advice(
    llm_input: LLMInput,
    model: str,
    api_key: str | None = None,
    max_tokens: int = 4000,
    temperature: float = 0.3,
) -> str:
    ...
```

- Импорт `litellm` ленивый (внутри функции), чтобы при отсутствии
  зависимости не падало при `import depscope.llm_module`.
- Если `litellm` не установлен — `LLMNotAvailableError`
  с подсказкой `pip install depscope[llm]`.
- Передача `api_key`: через `litellm.completion(api_key=...)`.
- Возвращаем `response.choices[0].message.content`.
- Логирование: модель, кол-во токенов промпта, время ответа.
  API-ключ НИКОГДА не логируется.

### 5.6 Обработка ошибок LiteLLM

- Auth errors → `LLMAuthError` с подсказкой про env vars.
- Rate limit → один retry через 5 секунд, дальше `LLMRateLimitError`.
- Сетевые ошибки → `LLMNetworkError`.
- Все наследуют `LLMError`.

### 5.7 Конфигурация

- Если `--mode advice` или `--mode full`, но модель не указана —
  CLI кидает `ConfigError` с инструкцией: укажите `--llm-model`,
  выставьте env var или `~/.depscope.yaml`.
- API-ключ: `--llm-api-key` > `DEPSCOPE_LLM_API_KEY` >
  стандартные (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`)
  > `~/.depscope.yaml`.

## Критерии приёмки

- [ ] `generate_advice(...)` возвращает markdown с секциями
      🔴 / 🟡 / 🟢 / 📋 (визуальная проверка с реальной моделью).
- [ ] При отсутствии `litellm` — понятная ошибка.
- [ ] При невалидном API-ключе — понятная ошибка, не stacktrace.
- [ ] Большие проекты не превышают лимит токенов (срабатывает усечение).
- [ ] API-ключи не появляются в логах.

## Тесты блока

### Тест-кейсы (`tests/test_llm_module.py`)

Все вызовы `litellm.completion` мокаются.

- `test_collect_project_tree_excludes_node_modules` — фикстура с проектом,
  в дереве нет `node_modules`.
- `test_collect_dependency_files_known_set` — корректно находит
  `requirements.txt`, `package.json`.
- `test_build_llm_input_top_n` — при 300 outdated пакетах берём 50.
- `test_priority_ordering` — пакеты с CVE идут первыми.
- `test_context_truncation` — при искусственно большом промпте срабатывает
  усечение, не падает.
- `test_litellm_not_installed` — мок ImportError → `LLMNotAvailableError`.
- `test_auth_error_mapped` — мок `litellm.AuthenticationError` →
  `LLMAuthError`.
- `test_api_key_not_logged` — capsys/caplog проверка отсутствия ключа в логах.

## Подводные камни

- LiteLLM имеет разные исключения в разных версиях — закладываем
  совместимость через `getattr(litellm, "AuthenticationError", Exception)`.
- Token counter LiteLLM требует совместимую модель в реестре. Если модели
  нет в реестре — fallback на грубую оценку.
- Пользователь может указать локальную модель (Ollama), API-ключ не нужен —
  не падаем при отсутствии ключа.
- Большие lock-файлы (`package-lock.json` на 5 MB) — главная причина
  переполнения контекста. Поэтому фильтр >200 KB.

## Что НЕ делаем в этом блоке

- Не делаем встроенный fine-tuning или собственный prompt-tuning UI.
- Не валидируем ответ модели на схему — просто возвращаем как markdown.
- Не делаем многошаговые вызовы (no agentic loop) — один вызов.
