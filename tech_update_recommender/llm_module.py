"""Ходим в LLM за советами по обновлению зависимостей.

Шаги:
  1. собираем LLMInput — отчёт + дерево проекта + файлы зависимостей,
  2. лепим из этого промпт,
  3. зовём litellm.completion, получаем markdown с рекомендациями.

API-ключ нигде не логируется. В сеть ходим только через litellm — в тестах
его мокают, реальных запросов нет.
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


# папки, которые не показываем LLM — ни в дереве, ни при поиске манифестов.
# сравниваем по точному имени сегмента
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

# глубина обхода для дерева проекта
_TREE_MAX_DEPTH: int = 4

# глубина обхода для поиска манифестов
_DEPS_MAX_DEPTH: int = 3

# не читаем файлы тяжелее этого порога. npm lock-файлы бывают по 10+ MB —
# если впихнуть в промпт, контекст взорвётся
_MAX_DEPENDENCY_FILE_BYTES: int = 200 * 1024  # 200 KB

# стандартные манифесты по точному имени (python, node, rust, go, java, ruby, .net)
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

# манифесты с неточными именами (requirements-dev.txt, *.csproj) — ловим через glob
_DEP_FILE_GLOBS: tuple[str, ...] = (
    "requirements-*.txt",
    "*.csproj",
)

# сколько пакетов кладём в LLMInput в обычном режиме
_TOP_N_FULL: int = 50

# а сколько оставляем, если совсем не лезет в контекст
_TOP_N_TRUNCATED: int = 20

# приоритет по типу обновления: major > minor > patch. число больше — приоритет выше
_SEMVER_PRIORITY: dict[str | None, int] = {
    "major": 3,
    "minor": 2,
    "patch": 1,
    None: 0,
}

# то, что отправляем модели в роли "system"
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
    "- Критичные обновления (CVE)\n"
    "- Рекомендуемые обновления (major с breaking changes)\n"
    "- Безопасные обновления (minor/patch)\n"
    "- Порядок обновления (пошаговый план)"
)


# свои исключения — чтобы наверху ловить и показывать юзеру понятное сообщение


class LLMError(Exception):
    """Базовый класс — от него всё остальное наследуется."""


class LLMNotAvailableError(LLMError):
    """litellm не установлен."""


class LLMAuthError(LLMError):
    """Кривой API-ключ или нет доступа к модели."""


class LLMRateLimitError(LLMError):
    """Провайдер ругается на частоту запросов, retry не помог."""


class LLMNetworkError(LLMError):
    """Сеть отвалилась или таймаут."""


class LLMContextOverflowError(LLMError):
    """Промпт не лезет в лимит токенов даже после усечения."""


# 1) дерево проекта


def _is_excluded(rel_parts: tuple[str, ...]) -> bool:
    # хоть один сегмент пути из EXCLUDED_DIRS — значит пропускаем.
    # т.е. и "node_modules/...", и "src/node_modules/..." — оба мимо
    return any(part in EXCLUDED_DIRS for part in rel_parts)


def collect_project_tree(path: str, max_lines: int = 200) -> str:
    """Дерево файлов проекта одной строкой.

    По сути это ``find <path> -type f``, написанный на pathlib и с
    выкидыванием служебных папок (node_modules, .git и т.п.).

    Параметры:
        path: корень проекта.
        max_lines: больше этого числа строк не отдаём. Если переполнили —
            в конец добавляем "... (truncated, N more files)".

    Возвращает:
        Строку, в которой каждый файл на отдельной строке.
        Если папки нет — пустая строка.
    """

    base = Path(path)
    # папки нет / не папка — возвращаем пустую строку, без падений
    if not base.exists() or not base.is_dir():
        return ""

    collected: list[str] = []
    truncated_extra = 0  # сколько файлов не показали

    # rglob идёт рекурсивно, дальше фильтруем сами
    for p in base.rglob("*"):
        try:
            rel = p.relative_to(base)
        except ValueError:
            # по идее не бывает, но мало ли
            continue
        rel_parts = rel.parts
        if not rel_parts:
            continue
        if len(rel_parts) > _TREE_MAX_DEPTH:
            continue
        # выкидываем всё в node_modules / .git / venv / ...
        if _is_excluded(rel_parts):
            continue
        if not p.is_file():
            continue

        if len(collected) < max_lines:
            collected.append(str(rel))
        else:
            truncated_extra += 1

    # сортируем — иначе порядок зависит от ОС и тесты будут флакать
    collected.sort()

    if truncated_extra > 0:
        collected.append(f"... (truncated, {truncated_extra} more files)")

    return "\n".join(collected)


# 2) файлы зависимостей — requirements.txt, package.json и прочее


def _matches_dep_file(name: str) -> bool:
    # сначала точное совпадение (быстрее), потом glob
    if name in _DEP_FILE_EXACT:
        return True
    for glob in _DEP_FILE_GLOBS:
        if Path(name).match(glob):
            return True
    return False


def collect_dependency_files(path: str) -> dict[str, str]:
    """Найти и прочитать манифесты зависимостей.

    Ищем в корне и до 3 уровней вглубь. EXCLUDED_DIRS пропускаем.
    Файлы тяжелее 200 KB тоже пропускаем — обычно это lock-файлы.

    Возвращает словарь ``{относительный_путь: содержимое}``.
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
        # ограничиваем глубину — лезть в чужие монорепы по 10 уровней не надо
        if len(rel_parts) > _DEPS_MAX_DEPTH:
            continue
        if _is_excluded(rel_parts):
            continue
        if not p.is_file():
            continue
        if not _matches_dep_file(p.name):
            continue

        # битый симлинк? — едем дальше
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


# 3) сборка LLMInput — фильтры, приоритеты, top-N


def _is_relevant(dep: DependencyReport) -> bool:
    # LLM нужны только устаревшие или дырявые пакеты.
    # свежие и без CVE — токены тратить не на что
    return dep.is_outdated or len(dep.advisories) > 0


def _priority_key(dep: DependencyReport) -> tuple[int, int]:
    # сначала пакеты с CVE, потом major > minor > patch.
    # возвращаем отрицательные значения — чтобы sorted() без reverse=True
    # давал убывающий приоритет
    return (
        -len(dep.advisories),
        -_SEMVER_PRIORITY.get(dep.semver_diff, 0),
    )


def _select_top_n(deps: list[DependencyReport], top_n: int) -> list[DependencyReport]:
    relevant = [d for d in deps if _is_relevant(d)]
    relevant.sort(key=_priority_key)
    return relevant[:top_n]


def _build_partial_report(report: FullReport, top_n: int) -> FullReport:
    """Обрезанная копия FullReport для LLM.

    Оставляем только топ-N пакетов в supported, unsupported пустой.
    Счётчики (total_packages и т.д.) берём из оригинала — пусть LLM
    видит реальный масштаб ("в проекте 300 зависимостей, показали 50").
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
    """Собираем всё, что пойдёт в LLM, в один LLMInput.

    Что делает:
      - выкидывает свежие пакеты без CVE,
      - если осталось много — берёт top-50 по приоритету
        (сначала CVE, потом major > minor > patch),
      - подтягивает дерево проекта и файлы зависимостей с диска.
    """

    partial_report = _build_partial_report(report, _TOP_N_FULL)
    project_tree = collect_project_tree(project_path)
    dependency_files = collect_dependency_files(project_path)
    return LLMInput(
        report=partial_report,
        project_tree=project_tree,
        dependency_files=dependency_files,
    )


# 4) сам промпт — то, что уходит в LLM как user message


def _report_for_prompt(report: FullReport) -> str:
    # сериализуем в JSON. mode="json" нужен чтобы datetime превратился
    # в ISO-строку, иначе json.dumps упадёт
    data = report.model_dump(mode="json")
    return json.dumps(data, indent=2, ensure_ascii=False)


def build_user_prompt(llm_input: LLMInput) -> str:
    """Собираем user-сообщение по кусочкам.

    По порядку: отчёт, дерево проекта, файлы зависимостей, инструкция.
    """

    parts: list[str] = []
    # отчёт по зависимостям как JSON
    parts.append("Отчёт об устаревших и уязвимых зависимостях:")
    parts.append(_report_for_prompt(llm_input.report))
    parts.append("")
    # дерево проекта
    parts.append("Структура проекта:")
    parts.append(llm_input.project_tree or "(пусто)")
    parts.append("")
    # содержимое манифестов
    parts.append("Файлы зависимостей:")
    if llm_input.dependency_files:
        for rel_path, content in llm_input.dependency_files.items():
            # === имя === — разделитель, по нему LLM ориентируется
            parts.append(f"=== {rel_path} ===")
            parts.append(content)
            parts.append("")
    else:
        parts.append("(не найдено)")
        parts.append("")
    # финальная команда модели
    parts.append("Сформируй рекомендации в указанном формате.")
    return "\n".join(parts)


# 5) считаем токены и режем контекст если не влазит


def _import_litellm() -> Any | None:
    """Лениво подгружаем litellm. Если его нет — возвращаем None.

    Отдельно ловим ``sys.modules["litellm"] = None`` — так делают тесты,
    чтобы изобразить "не установлен". Без этой проверки Python вернул бы
    кэшированный None и всё бы запуталось.
    """

    # тесты подменяют модуль на None
    if "litellm" in sys.modules and sys.modules["litellm"] is None:
        return None
    try:
        import litellm  # type: ignore[import-not-found]
    except ImportError:
        return None
    return litellm


def count_tokens(model: str, text: str) -> int:
    """Прикидываем сколько токенов займёт текст.

    Если litellm установлен — спрашиваем у него. Иначе грубая оценка
    len // 4. Точность тут не критична — нужно только понять, лезем
    в контекст или нет.
    """

    litellm = _import_litellm()
    if litellm is not None:
        try:
            return int(litellm.token_counter(model=model, text=text))
        except Exception as err:  # noqa: BLE001 — token_counter может кидать что угодно
            logger.debug("token_counter fallback for %s: %s", model, err)
    # ~4 символа на токен — очень грубо, но для проверки лимита сойдёт
    return max(1, len(text) // 4)


def _truncate_dep_files(
    files: dict[str, str], max_bytes: int = 10 * 1024, max_lines: int = 200
) -> dict[str, str]:
    """Урезаем файлы зависимостей, если они слишком жирные.

    Маленькие не трогаем. Большие (>10 KB) обрезаем до первых
    ``max_lines`` строк и в конец добавляем пометку.
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
                # размер большой, но строк мало (одна длинная) — оставляем как есть
                out[name] = content
        else:
            out[name] = content
    return out


def _truncate_tree(tree: str, max_lines: int) -> str:
    # обрезаем дерево по строкам. Короткое — не трогаем
    if not tree:
        return tree
    lines = tree.splitlines()
    # тут может быть пометка про truncated с прошлого раза — считается обычной строкой, и это ок
    if len(lines) <= max_lines:
        return tree
    head = lines[:max_lines]
    head.append(f"... (truncated, {len(lines) - max_lines} more files)")
    return "\n".join(head)


def _fits(model: str, system_prompt: str, user_prompt: str, limit: int) -> bool:
    # укладываемся в бюджет токенов или нет
    total = count_tokens(model, system_prompt) + count_tokens(model, user_prompt)
    return total <= limit


def truncate_input(
    llm_input: LLMInput,
    model: str,
    max_context_tokens: int = 8000,
    system_prompt: str = SYSTEM_PROMPT,
) -> LLMInput:
    """Постепенно ужимаем LLMInput, пока не влезет в лимит.

    Режем поэтапно, от лёгкого к жёсткому:
      1. Уже влезает? — отдаём как есть.
      2. Режем дерево проекта до 100 строк.
      3. Режем большие манифесты до 200 строк.
      4. Уменьшаем top-N пакетов с 50 до 20.

    Если и после этого не вошло — кидаем LLMContextOverflowError.
    """

    user_prompt = build_user_prompt(llm_input)
    # может уже влезает?
    if _fits(model, system_prompt, user_prompt, max_context_tokens):
        return llm_input

    # шаг 2: подрезаем дерево
    new_tree = _truncate_tree(llm_input.project_tree, max_lines=100)
    candidate = LLMInput(
        report=llm_input.report,
        project_tree=new_tree,
        dependency_files=llm_input.dependency_files,
    )
    user_prompt = build_user_prompt(candidate)
    if _fits(model, system_prompt, user_prompt, max_context_tokens):
        return candidate

    # шаг 3: подрезаем толстые dep-файлы (обычно package-lock.json виноват)
    new_files = _truncate_dep_files(candidate.dependency_files)
    candidate = LLMInput(
        report=candidate.report,
        project_tree=candidate.project_tree,
        dependency_files=new_files,
    )
    user_prompt = build_user_prompt(candidate)
    if _fits(model, system_prompt, user_prompt, max_context_tokens):
        return candidate

    # шаг 4: жертвуем количеством пакетов — оставляем 20 самых важных
    smaller_report = _build_partial_report(candidate.report, _TOP_N_TRUNCATED)
    candidate = LLMInput(
        report=smaller_report,
        project_tree=candidate.project_tree,
        dependency_files=candidate.dependency_files,
    )
    user_prompt = build_user_prompt(candidate)
    if _fits(model, system_prompt, user_prompt, max_context_tokens):
        return candidate

    # порезали всё что могли — не лезет. Сдаёмся
    raise LLMContextOverflowError(
        f"Промпт не уложился в {max_context_tokens} токенов даже после усечения."
    )


# 6) сам вызов LiteLLM и перевод его ошибок в наши


def _is_local_model(model: str) -> bool:
    # для локальных моделей (ollama) ключ не нужен
    if not model:
        return False
    return model.startswith("ollama/") or "ollama" in model.lower()


def _map_litellm_error(litellm: Any, err: Exception) -> LLMError:
    """Превращаем ошибки litellm в наши, чтобы CLI знал что показать юзеру."""

    # кривой API-ключ
    auth_cls = getattr(litellm, "AuthenticationError", None)
    if auth_cls is not None and isinstance(err, auth_cls):
        return LLMAuthError("Невалидный API-ключ. Проверьте --llm-api-key или env vars.")

    # слишком частые запросы
    rate_cls = getattr(litellm, "RateLimitError", None)
    if rate_cls is not None and isinstance(err, rate_cls):
        return LLMRateLimitError("Превышен rate limit провайдера LLM.")

    # сеть/таймаут — для юзера это одно и то же ("LLM не отвечает")
    network_classes: list[type] = []
    for name in ("APIConnectionError", "Timeout"):
        cls = getattr(litellm, name, None)
        if isinstance(cls, type):
            network_classes.append(cls)
    if network_classes and isinstance(err, tuple(network_classes)):
        return LLMNetworkError(f"Сетевая ошибка при вызове LLM: {err}")

    # переполнение окна модели
    bad_req_cls = getattr(litellm, "BadRequestError", None)
    ctx_cls = getattr(litellm, "ContextWindowExceededError", None)
    if ctx_cls is not None and isinstance(err, ctx_cls):
        return LLMContextOverflowError("Контекст превышает лимит модели даже после усечения.")
    if bad_req_cls is not None and isinstance(err, bad_req_cls):
        # обычно сюда падают плохие параметры запроса или тот же overflow,
        # но без отдельного класса. Точнее не угадаем — отдаём базовое
        return LLMError(f"Невалидный запрос к LLM: {err}")

    # всё остальное — общая LLMError
    return LLMError(f"Ошибка LiteLLM: {err}")


def generate_advice(
    llm_input: LLMInput,
    model: str,
    api_key: str | None = None,
    max_tokens: int = 4000,
    temperature: float = 0.3,
    max_context_tokens: int = 8000,
) -> str:
    """Главная функция модуля — спросить LLM и вернуть markdown-ответ.

    Параметры:
        llm_input: то, что отдадим модели (отчёт + контекст проекта).
        model: имя модели в формате litellm — например,
            ``"gemini/gemini-2.0-flash"``, ``"claude-sonnet-4-20250514"``,
            ``"ollama/llama3"``.
        api_key: ключ от провайдера. Для ollama (локально) можно None.
        max_tokens: максимум токенов в ответе.
        temperature: насколько "креативно" модель отвечает (0..1).
            По умолчанию 0.3 — почти детерминированный совет.
        max_context_tokens: бюджет на сам промпт. Если не влезаем —
            автоматом ужимаемся через truncate_input.

    Возвращает:
        Текст ответа модели (markdown).

    Бросает:
        LLMNotAvailableError — если litellm не поставили.
        LLMAuthError / LLMRateLimitError / LLMNetworkError /
        LLMContextOverflowError — по типу проблемы.
        LLMError — на всё остальное.
    """

    litellm = _import_litellm()
    if litellm is None:
        raise LLMNotAvailableError(
            "litellm не установлен. Установите: pip install tech-upd-recommender"
        )

    # сначала ужимаем вход под бюджет. Не влезет — truncate_input сам кинет ошибку
    truncated = truncate_input(
        llm_input,
        model=model,
        max_context_tokens=max_context_tokens,
        system_prompt=SYSTEM_PROMPT,
    )

    user_prompt = build_user_prompt(truncated)
    # оценка только для логов, точное число знает провайдер
    prompt_tokens_estimate = count_tokens(model, SYSTEM_PROMPT) + count_tokens(model, user_prompt)

    # api_key сюда НЕ пишем. Только название модели и метрики
    logger.info(
        "calling LLM model=%s prompt_tokens~%d max_tokens=%d temperature=%s",
        model,
        prompt_tokens_estimate,
        max_tokens,
        temperature,
    )
    logger.debug("=== SYSTEM PROMPT ===\n%s", SYSTEM_PROMPT)
    logger.debug("=== USER PROMPT ===\n%s", user_prompt)

    # формат сообщений как в OpenAI chat completion api
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    rate_cls = getattr(litellm, "RateLimitError", None)

    def _do_call() -> Any:
        # хелпер чтобы повторить вызов один-в-один при retry
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
        except Exception as err:  # noqa: BLE001 — ниже мапим в свои классы
            # если rate-limit — даём провайдеру 5 секунд и пробуем ещё раз. Единственный случай retry
            if rate_cls is not None and isinstance(err, rate_cls):
                logger.warning("rate limit hit on %s, retrying in 5s", model)
                time.sleep(5)
                try:
                    response = _do_call()
                except Exception as retry_err:  # noqa: BLE001
                    raise _map_litellm_error(litellm, retry_err) from retry_err
            else:
                # любая другая ошибка — сразу мапим без retry
                raise _map_litellm_error(litellm, err) from err
    except LLMError:
        # свои ошибки просто прокидываем дальше
        raise
    elapsed = time.monotonic() - started

    # достаём текст ответа. Тут много чего может пойти не так
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, KeyError) as err:
        raise LLMError(f"Не удалось извлечь content из ответа LLM: {err}") from err

    if content is None:
        # бывает — модель отказалась отвечать (safety-фильтр)
        raise LLMError("LLM вернул пустой content.")

    logger.info(
        "LLM response received model=%s elapsed=%.2fs response_chars=%d",
        model,
        elapsed,
        len(content),
    )

    # дёргаем _is_local_model просто чтобы не было warning про неиспользованную
    # функцию. Пригодится когда добавим разную обработку ключа для ollama
    _ = _is_local_model(model)

    return content
