"""LLMModule — генерация AI-рекомендаций по обновлению зависимостей.

Модуль строит ``LLMInput`` (отчёт + структура проекта + файлы зависимостей),
формирует промпт и вызывает ``litellm.completion`` для получения markdown
с рекомендациями.

Контракт см. в ``docs/blocks/05-llm-module.md`` и в PLAN.md, секция «LLMModule».

Замечания по безопасности:

* API-ключи никогда не логируются. Строки ``api_key`` не передаются в логгер
  ни прямо, ни в составе сообщений.
* Реальных сетевых вызовов нет — только через ``litellm``; в тестах функцию
  ``litellm.completion`` мокают.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from tech_update_recommender.models import DependencyReport, FullReport, LLMInput

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

#: Каталоги, которые исключаются и в дереве проекта, и при поиске
#: dependency-файлов. Сегмент пути совпадает по точному имени.
EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        "node_modules",
        ".git",
        "venv",
        ".venv",
        "__pycache__",
        "dist",
        "build",
    }
)

#: Глубина обхода дерева проекта (включительно).
_TREE_MAX_DEPTH: int = 4

#: Глубина поиска dependency-файлов.
_DEPS_MAX_DEPTH: int = 3

#: Максимальный размер dependency-файла, который читается в LLM-контекст.
#: Lock-файлы npm/yarn могут быть в десятки мегабайт — туда не хочется.
_MAX_DEPENDENCY_FILE_BYTES: int = 200 * 1024  # 200 KB

#: Точные имена файлов, которые мы считаем дескрипторами зависимостей.
_DEP_FILE_EXACT: frozenset[str] = frozenset(
    {
        "requirements.txt",
        "pyproject.toml",
        "Pipfile",
        "Pipfile.lock",
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Cargo.toml",
        "Cargo.lock",
        "go.mod",
        "go.sum",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "Gemfile",
        "Gemfile.lock",
        "packages.config",
    }
)

#: Glob-паттерны для имён файлов (применяются к ``Path.name``).
_DEP_FILE_GLOBS: tuple[str, ...] = (
    "requirements-*.txt",
    "*.csproj",
)

#: Топ-N пакетов в полной версии LLMInput.
_TOP_N_FULL: int = 50

#: Топ-N пакетов после агрессивного усечения.
_TOP_N_TRUNCATED: int = 20

#: Порядок приоритета semver_diff (большее число = выше приоритет).
_SEMVER_PRIORITY: dict[str | None, int] = {
    "major": 3,
    "minor": 2,
    "patch": 1,
    None: 0,
}

#: Точный текст system prompt — не редактируем без обновления PLAN.md.
SYSTEM_PROMPT: str = (
    "Ты — эксперт по управлению зависимостями в software-проектах.\n"
    "Тебе предоставлен отчёт об устаревших зависимостях проекта,\n"
    "структура проекта и файлы зависимостей.\n"
    "\n"
    "Твоя задача:\n"
    "1. Проанализировать какие обновления безопасны (patch/minor) и какие "
    "рискованны (major)\n"
    "2. Определить связанные пакеты, которые нужно обновлять вместе\n"
    "3. Предложить порядок обновления (что сначала, что потом)\n"
    "4. Выделить критичные обновления (с CVE)\n"
    "5. Предупредить о потенциальных breaking changes в major-обновлениях\n"
    "\n"
    "Формат ответа: структурированный markdown с секциями:\n"
    "- 🔴 Критичные обновления (CVE)\n"
    "- 🟡 Рекомендуемые обновления (major с breaking changes)\n"
    "- 🟢 Безопасные обновления (minor/patch)\n"
    "- 📋 Порядок обновления (пошаговый план)"
)


# ---------------------------------------------------------------------------
# Иерархия исключений
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Базовое исключение модуля LLM."""


class LLMNotAvailableError(LLMError):
    """``litellm`` не установлен или недоступен."""


class LLMAuthError(LLMError):
    """Невалидный API-ключ или отсутствует доступ к модели."""


class LLMRateLimitError(LLMError):
    """Провайдер вернул rate-limit и retry не помог."""


class LLMNetworkError(LLMError):
    """Сетевая ошибка / таймаут при общении с провайдером."""


class LLMContextOverflowError(LLMError):
    """Промпт не помещается в ``max_context_tokens`` даже после усечения."""


# ---------------------------------------------------------------------------
# 1. Сбор контекста — дерево проекта
# ---------------------------------------------------------------------------


def _is_excluded(rel_parts: tuple[str, ...]) -> bool:
    """Проверить, попадает ли любой сегмент относительного пути в EXCLUDED_DIRS."""

    return any(part in EXCLUDED_DIRS for part in rel_parts)


def collect_project_tree(path: str, max_lines: int = 200) -> str:
    """Собрать дерево файлов проекта в виде строки.

    Эквивалент ``find <path> -type f`` с отбрасыванием служебных каталогов
    (``node_modules``, ``.git``, ``venv``, ``.venv``, ``__pycache__``,
    ``dist``, ``build``). Реализовано через ``pathlib`` без сторонних утилит.

    Параметры:
        path: путь к корню проекта.
        max_lines: верхняя граница строк в выводе. При превышении к строке
            добавляется метка ``... (truncated, N more files)`` (где ``N`` —
            сколько ещё файлов было не показано).

    Возвращает:
        Многострочную строку с относительными путями, по одному на строку.
        Если корня не существует — пустая строка.
    """

    base = Path(path)
    if not base.exists() or not base.is_dir():
        return ""

    collected: list[str] = []
    truncated_extra = 0

    # rglob("*") даёт все файлы и каталоги. Мы фильтруем по is_file() и
    # по сегментам пути (чтобы исключить вложенные node_modules/.git).
    for p in base.rglob("*"):
        try:
            rel = p.relative_to(base)
        except ValueError:
            continue
        rel_parts = rel.parts
        if not rel_parts:
            continue
        # Глубина = количество сегментов в относительном пути.
        if len(rel_parts) > _TREE_MAX_DEPTH:
            continue
        # Проверяем все сегменты пути целиком (включая родительские dirs).
        if _is_excluded(rel_parts):
            continue
        if not p.is_file():
            continue

        if len(collected) < max_lines:
            collected.append(str(rel))
        else:
            truncated_extra += 1

    # Стабильная сортировка — для воспроизводимости тестов.
    collected.sort()

    if truncated_extra > 0:
        collected.append(f"... (truncated, {truncated_extra} more files)")

    return "\n".join(collected)


# ---------------------------------------------------------------------------
# 2. Сбор контекста — файлы зависимостей
# ---------------------------------------------------------------------------


def _matches_dep_file(name: str) -> bool:
    """Истина, если имя файла относится к стандартным dependency-файлам."""

    if name in _DEP_FILE_EXACT:
        return True
    for glob in _DEP_FILE_GLOBS:
        if Path(name).match(glob):
            return True
    return False


def collect_dependency_files(path: str) -> dict[str, str]:
    """Найти и прочитать стандартные файлы зависимостей в проекте.

    Поиск ведётся в корне и до 3 уровней вложенности. Каталоги из
    ``EXCLUDED_DIRS`` пропускаются (включая вложенные).
    Файлы тяжелее ``_MAX_DEPENDENCY_FILE_BYTES`` (200 KB) пропускаются —
    обычно это огромные lock-файлы, которые забьют контекст.

    Параметры:
        path: путь к корню проекта.

    Возвращает:
        ``{relative_path: content}``. Содержимое читается в UTF-8 с
        ``errors="replace"`` — чтобы не падать на бинарных вкраплениях.
    """

    base = Path(path)
    if not base.exists() or not base.is_dir():
        return {}

    result: dict[str, str] = {}

    for p in base.rglob("*"):
        try:
            rel = p.relative_to(base)
        except ValueError:
            continue
        rel_parts = rel.parts
        if not rel_parts:
            continue
        if len(rel_parts) > _DEPS_MAX_DEPTH:
            continue
        if _is_excluded(rel_parts):
            continue
        if not p.is_file():
            continue
        if not _matches_dep_file(p.name):
            continue

        try:
            size = p.stat().st_size
        except OSError as err:
            logger.debug("cannot stat %s: %s", rel, err)
            continue
        if size > _MAX_DEPENDENCY_FILE_BYTES:
            logger.debug(
                "skip large file %s (%d bytes > %d)",
                rel,
                size,
                _MAX_DEPENDENCY_FILE_BYTES,
            )
            continue

        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError as err:
            logger.debug("cannot read %s: %s", rel, err)
            continue
        result[str(rel)] = content

    return result


# ---------------------------------------------------------------------------
# 3. Сборка LLMInput
# ---------------------------------------------------------------------------


def _is_relevant(dep: DependencyReport) -> bool:
    """Релевантны для LLM пакеты, которые устарели или имеют CVE."""

    return dep.is_outdated or len(dep.advisories) > 0


def _priority_key(dep: DependencyReport) -> tuple[int, int]:
    """Ключ сортировки: больше advisories и крупнее semver_diff — выше.

    Возвращаем кортеж отрицательных значений, чтобы обычный
    ``sorted(... )`` без ``reverse`` давал нужный порядок.
    """

    return (
        -len(dep.advisories),
        -_SEMVER_PRIORITY.get(dep.semver_diff, 0),
    )


def _select_top_n(deps: list[DependencyReport], top_n: int) -> list[DependencyReport]:
    """Отфильтровать релевантные и взять top-N по приоритету."""

    relevant = [d for d in deps if _is_relevant(d)]
    relevant.sort(key=_priority_key)
    return relevant[:top_n]


def _build_partial_report(report: FullReport, top_n: int) -> FullReport:
    """Создать копию ``FullReport`` с урезанным ``supported`` и пустым ``unsupported``.

    Счётчики (``total_packages``, ``outdated_count``, ``vulnerable_count``)
    сохраняются от оригинала — они полезны LLM как общий контекст ("в проекте
    300 пакетов, я показал тебе только top-50").
    """

    top = _select_top_n(report.supported, top_n)
    return FullReport(
        supported=top,
        unsupported=[],
        scan_timestamp=report.scan_timestamp,
        project_path=report.project_path,
        total_packages=report.total_packages,
        outdated_count=report.outdated_count,
        vulnerable_count=report.vulnerable_count,
    )


def build_llm_input(report: FullReport, project_path: str) -> LLMInput:
    """Собрать ``LLMInput`` из отчёта и контекста проекта.

    * В ``report.supported`` остаются только outdated/vulnerable пакеты.
    * При >200 таких пакетов сортируем по числу advisories и приоритету
      semver_diff (major > minor > patch > None) и берём top-50.
    * ``project_tree`` и ``dependency_files`` собираются с диска.
    """

    partial_report = _build_partial_report(report, _TOP_N_FULL)
    project_tree = collect_project_tree(project_path)
    dependency_files = collect_dependency_files(project_path)
    return LLMInput(
        report=partial_report,
        project_tree=project_tree,
        dependency_files=dependency_files,
    )


# ---------------------------------------------------------------------------
# 4. Промпт
# ---------------------------------------------------------------------------


def _report_for_prompt(report: FullReport) -> str:
    """Сериализовать частичный отчёт в JSON для user prompt'а.

    Используется ``model_dump(mode="json")``, чтобы datetime ушёл в ISO 8601.
    """

    data = report.model_dump(mode="json")
    return json.dumps(data, indent=2, ensure_ascii=False)


def build_user_prompt(llm_input: LLMInput) -> str:
    """Построить user prompt по контракту из ``05-llm-module.md``."""

    parts: list[str] = []
    parts.append("Отчёт об устаревших и уязвимых зависимостях:")
    parts.append(_report_for_prompt(llm_input.report))
    parts.append("")
    parts.append("Структура проекта:")
    parts.append(llm_input.project_tree or "(пусто)")
    parts.append("")
    parts.append("Файлы зависимостей:")
    if llm_input.dependency_files:
        for rel_path, content in llm_input.dependency_files.items():
            parts.append(f"=== {rel_path} ===")
            parts.append(content)
            parts.append("")
    else:
        parts.append("(не найдено)")
        parts.append("")
    parts.append("Сформируй рекомендации в указанном формате.")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 5. Лимит контекста
# ---------------------------------------------------------------------------


def _import_litellm() -> Any | None:
    """Ленивый импорт ``litellm``. Возвращает модуль или ``None``.

    Поддерживает тестовый патч ``sys.modules["litellm"] = None``,
    при котором ``import litellm`` отдаст None, и мы должны это
    интерпретировать как "не установлен".
    """

    if "litellm" in sys.modules and sys.modules["litellm"] is None:
        return None
    try:
        import litellm  # type: ignore[import-not-found]
    except ImportError:
        return None
    return litellm


def count_tokens(model: str, text: str) -> int:
    """Оценить число токенов в ``text`` для ``model``.

    Использует ``litellm.token_counter`` если ``litellm`` установлен и
    модель ему знакома. Иначе (или при любой ошибке внутри) возвращает
    грубую оценку ``len(text) // 4`` — этого достаточно для решения
    "усекать или нет".
    """

    litellm = _import_litellm()
    if litellm is not None:
        try:
            return int(litellm.token_counter(model=model, text=text))
        except Exception as err:  # noqa: BLE001 — token_counter может бросать что угодно
            logger.debug("token_counter fallback for %s: %s", model, err)
    return max(1, len(text) // 4)


def _truncate_dep_files(
    files: dict[str, str], max_bytes: int = 10 * 1024, max_lines: int = 200
) -> dict[str, str]:
    """Усечь большие dependency-файлы.

    Файлы > 10 KB обрезаются до первых ``max_lines`` строк с маркером.
    Маленькие файлы остаются как есть.
    """

    out: dict[str, str] = {}
    for name, content in files.items():
        if len(content) > max_bytes:
            lines = content.splitlines()
            if len(lines) > max_lines:
                head = lines[:max_lines]
                head.append(f"... (truncated, {len(lines) - max_lines} more lines)")
                out[name] = "\n".join(head)
            else:
                out[name] = content
        else:
            out[name] = content
    return out


def _truncate_tree(tree: str, max_lines: int) -> str:
    """Урезать готовое представление дерева до ``max_lines`` строк."""

    if not tree:
        return tree
    lines = tree.splitlines()
    # Если уже стоит маркер truncated — учитываем его как одну строку,
    # пересчитываем "сколько ещё".
    if len(lines) <= max_lines:
        return tree
    head = lines[:max_lines]
    head.append(f"... (truncated, {len(lines) - max_lines} more files)")
    return "\n".join(head)


def _fits(model: str, system_prompt: str, user_prompt: str, limit: int) -> bool:
    """Промпт укладывается в ``limit`` токенов?"""

    total = count_tokens(model, system_prompt) + count_tokens(model, user_prompt)
    return total <= limit


def truncate_input(
    llm_input: LLMInput,
    model: str,
    max_context_tokens: int = 8000,
    system_prompt: str = SYSTEM_PROMPT,
) -> LLMInput:
    """Постепенно усекать ``LLMInput`` пока промпт не уложится в лимит.

    Шаги:
        1. Если уже укладывается — вернуть как есть.
        2. Урезать дерево до 100 строк.
        3. Урезать большие dependency-файлы (>10 KB) до 200 строк.
        4. Уменьшить top-N пакетов с 50 до 20.

    Если ничего не помогло — ``LLMContextOverflowError``.
    """

    user_prompt = build_user_prompt(llm_input)
    if _fits(model, system_prompt, user_prompt, max_context_tokens):
        return llm_input

    # Шаг 2: дерево → 100 строк.
    new_tree = _truncate_tree(llm_input.project_tree, max_lines=100)
    candidate = LLMInput(
        report=llm_input.report,
        project_tree=new_tree,
        dependency_files=llm_input.dependency_files,
    )
    user_prompt = build_user_prompt(candidate)
    if _fits(model, system_prompt, user_prompt, max_context_tokens):
        return candidate

    # Шаг 3: большие dependency-файлы → 200 строк.
    new_files = _truncate_dep_files(candidate.dependency_files)
    candidate = LLMInput(
        report=candidate.report,
        project_tree=candidate.project_tree,
        dependency_files=new_files,
    )
    user_prompt = build_user_prompt(candidate)
    if _fits(model, system_prompt, user_prompt, max_context_tokens):
        return candidate

    # Шаг 4: top-N → 20.
    smaller_report = _build_partial_report(candidate.report, _TOP_N_TRUNCATED)
    candidate = LLMInput(
        report=smaller_report,
        project_tree=candidate.project_tree,
        dependency_files=candidate.dependency_files,
    )
    user_prompt = build_user_prompt(candidate)
    if _fits(model, system_prompt, user_prompt, max_context_tokens):
        return candidate

    raise LLMContextOverflowError(
        f"Промпт не уложился в {max_context_tokens} токенов даже после усечения."
    )


# ---------------------------------------------------------------------------
# 6. Вызов LiteLLM
# ---------------------------------------------------------------------------


def _is_local_model(model: str) -> bool:
    """Локальные модели (Ollama и т.п.) не требуют API-ключа."""

    if not model:
        return False
    return model.startswith("ollama/") or "ollama" in model.lower()


def _map_litellm_error(litellm: Any, err: Exception) -> LLMError:
    """Перевод специфичных litellm-ошибок в наш домен."""

    auth_cls = getattr(litellm, "AuthenticationError", None)
    if auth_cls is not None and isinstance(err, auth_cls):
        return LLMAuthError("Невалидный API-ключ. Проверьте --llm-api-key или env vars.")

    rate_cls = getattr(litellm, "RateLimitError", None)
    if rate_cls is not None and isinstance(err, rate_cls):
        return LLMRateLimitError("Превышен rate limit провайдера LLM.")

    network_classes: list[type] = []
    for name in ("APIConnectionError", "Timeout"):
        cls = getattr(litellm, name, None)
        if isinstance(cls, type):
            network_classes.append(cls)
    if network_classes and isinstance(err, tuple(network_classes)):
        return LLMNetworkError(f"Сетевая ошибка при вызове LLM: {err}")

    bad_req_cls = getattr(litellm, "BadRequestError", None)
    ctx_cls = getattr(litellm, "ContextWindowExceededError", None)
    if ctx_cls is not None and isinstance(err, ctx_cls):
        return LLMContextOverflowError("Контекст превышает лимит модели даже после усечения.")
    if bad_req_cls is not None and isinstance(err, bad_req_cls):
        # Часто BadRequestError — это переполнение контекста на стороне модели.
        return LLMError(f"Невалидный запрос к LLM: {err}")

    return LLMError(f"Ошибка LiteLLM: {err}")


def generate_advice(
    llm_input: LLMInput,
    model: str,
    api_key: str | None = None,
    max_tokens: int = 4000,
    temperature: float = 0.3,
    max_context_tokens: int = 8000,
) -> str:
    """Сгенерировать markdown-рекомендации по обновлению зависимостей.

    Параметры:
        llm_input: вход модуля (отчёт + контекст проекта).
        model: имя модели в формате LiteLLM (``"gemini/gemini-2.0-flash"``,
            ``"claude-sonnet-4-20250514"``, ``"ollama/llama3"``…).
        api_key: API-ключ. Для локальных моделей (``ollama/...``) не нужен.
        max_tokens: верхний предел токенов в ответе.
        temperature: температура сэмплинга (0–1).
        max_context_tokens: бюджет токенов на промпт; при превышении
            ``llm_input`` усекается через :func:`truncate_input`.

    Возвращает:
        Markdown-строку из ``response.choices[0].message.content``.

    Бросает:
        :class:`LLMNotAvailableError` — если ``litellm`` не установлен.
        :class:`LLMAuthError` / :class:`LLMRateLimitError` /
        :class:`LLMNetworkError` / :class:`LLMContextOverflowError` —
        в соответствующих кейсах. ``LLMError`` для всех прочих ошибок
        провайдера.
    """

    litellm = _import_litellm()
    if litellm is None:
        raise LLMNotAvailableError(
            "litellm не установлен. Установите: pip install tech-update-recommender[llm]"
        )

    # Усекаем под бюджет контекста (может бросить LLMContextOverflowError).
    truncated = truncate_input(
        llm_input,
        model=model,
        max_context_tokens=max_context_tokens,
        system_prompt=SYSTEM_PROMPT,
    )

    user_prompt = build_user_prompt(truncated)
    prompt_tokens_estimate = count_tokens(model, SYSTEM_PROMPT) + count_tokens(model, user_prompt)

    # API-ключ НИКОГДА не логируется — пишем только модель и оценку токенов.
    logger.info(
        "calling LLM model=%s prompt_tokens~%d max_tokens=%d temperature=%s",
        model,
        prompt_tokens_estimate,
        max_tokens,
        temperature,
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    rate_cls = getattr(litellm, "RateLimitError", None)

    def _do_call() -> Any:
        return litellm.completion(
            model=model,
            messages=messages,
            api_key=api_key,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    started = time.monotonic()
    try:
        try:
            response = _do_call()
        except Exception as err:  # noqa: BLE001 — мапим в свои исключения ниже
            # Один retry на rate-limit через 5 сек.
            if rate_cls is not None and isinstance(err, rate_cls):
                logger.warning("rate limit hit on %s, retrying in 5s", model)
                time.sleep(5)
                try:
                    response = _do_call()
                except Exception as retry_err:  # noqa: BLE001
                    raise _map_litellm_error(litellm, retry_err) from retry_err
            else:
                raise _map_litellm_error(litellm, err) from err
    except LLMError:
        raise
    elapsed = time.monotonic() - started

    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, KeyError) as err:
        raise LLMError(f"Не удалось извлечь content из ответа LLM: {err}") from err

    if content is None:
        raise LLMError("LLM вернул пустой content.")

    logger.info(
        "LLM response received model=%s elapsed=%.2fs response_chars=%d",
        model,
        elapsed,
        len(content),
    )

    # Для локальных моделей не падаем на отсутствии ключа: litellm обычно
    # сам справляется. _is_local_model сейчас не меняет логику вызова —
    # просто фиксируем ожидание для будущих ассертов и для ясности.
    _ = _is_local_model(model)

    return content
