"""ReportModule — рендер ``FullReport`` в table / json / markdown.

Модуль не делает сетевых вызовов и ничего не печатает сам — только
возвращает строку, готовую для вывода в stdout или сохранения в файл.

Контракт см. в ``docs/blocks/04-report-module.md``.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from rich.console import Console
from rich.table import Table

from tech_update_recommender.models import DependencyReport, FullReport

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

#: Точный текст дисклеймера LLM-секции (см. PLAN.md).
#: ``{model_name}`` подставляется через ``str.format``.
LLM_DISCLAIMER_TEMPLATE = (
    "⚠️ Рекомендации ниже сгенерированы AI-моделью ({model_name}) и носят "
    "рекомендательный характер. Качество рекомендаций зависит от выбранной "
    "модели. Всегда проверяйте совместимость обновлений в вашем проекте "
    "перед применением."
)

#: Заголовки колонок таблицы зависимостей. Используются и в ``rich``,
#: и в markdown-выводе, чтобы тексты совпадали.
TABLE_COLUMNS = (
    "Name",
    "Ecosystem",
    "Current",
    "Latest",
    "Diff",
    "Advisories",
)


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def _filter_supported(deps: list[DependencyReport], only_outdated: bool) -> list[DependencyReport]:
    """Применить фильтр ``--only-outdated`` к списку supported-пакетов."""

    if not only_outdated:
        return list(deps)
    return [d for d in deps if d.is_outdated]


def _row_color(dep: DependencyReport) -> str:
    """Возвращает имя цвета (``rich`` style) для строки таблицы.

    Приоритет: advisories > major > minor/patch > актуальные.
    """

    if dep.advisories:
        return "red"
    if dep.semver_diff == "major":
        return "yellow"
    if dep.semver_diff in ("minor", "patch"):
        return "green"
    # Невалидная версия (is_outdated=True, semver_diff=None) и
    # актуальные пакеты попадают в серый.
    return "grey50"


def _diff_label(dep: DependencyReport) -> str:
    """Текст для колонки ``Diff``.

    Особый кейс: ``is_outdated=True`` без ``semver_diff`` — показываем
    ``?``, чтобы было видно, что версия не парсится.
    """

    if dep.semver_diff is not None:
        return dep.semver_diff
    if dep.is_outdated:
        return "?"
    return "—"


def _advisories_label(dep: DependencyReport) -> str:
    """Список advisory-id через запятую или ``—`` если пусто."""

    if not dep.advisories:
        return "—"
    return ", ".join(a.id for a in dep.advisories)


def _summary_line(report: FullReport) -> str:
    """Однострочное summary поверх отчёта.

    Считается ВСЕГДА по полному ``FullReport`` (не зависит от
    ``only_outdated``).
    """

    return (
        f"Total: {report.total_packages}, "
        f"outdated: {report.outdated_count}, "
        f"with CVE: {report.vulnerable_count}"
    )


def _unsupported_summary(report: FullReport) -> str | None:
    """Текст секции unsupported или ``None`` если их нет."""

    if not report.unsupported:
        return None
    return (
        f"⚠ Не проверено через deps.dev: {len(report.unsupported)} системных пакетов (deb/apk/rpm)"
    )


# ---------------------------------------------------------------------------
# Формат table (rich)
# ---------------------------------------------------------------------------


def _render_table(
    report: FullReport,
    only_outdated: bool,
    llm_advice: str | None,
    llm_model_name: str | None,
) -> str:
    """Сформировать table-вывод через ``rich.console.Console(record=True)``."""

    # ``no_color=True`` плюс ``styles=False`` в export_text — детерминированный
    # текстовый вывод без ANSI, удобный для тестов. Стили остаются в Table
    # для случая, когда вывод печатают напрямую через console.print().
    # ``soft_wrap=True`` отключает жёсткий перенос длинных строк (важно для
    # дисклеймера и LLM-текста, чтобы тесты находили их подстрокой).
    console = Console(
        record=True,
        force_terminal=False,
        no_color=True,
        width=120,
        soft_wrap=True,
    )

    console.print(_summary_line(report))
    console.print("")

    table = Table(show_header=True, header_style="bold")
    for col in TABLE_COLUMNS:
        table.add_column(col)

    deps = _filter_supported(report.supported, only_outdated)
    for dep in deps:
        style = _row_color(dep)
        latest = dep.latest_version if dep.latest_version is not None else "—"
        table.add_row(
            dep.name,
            dep.ecosystem,
            dep.current_version,
            latest,
            _diff_label(dep),
            _advisories_label(dep),
            style=style,
        )

    console.print(table)

    unsupported = _unsupported_summary(report)
    if unsupported is not None:
        console.print("")
        console.print(unsupported)

    if llm_advice is not None:
        console.print("")
        console.print("## AI-рекомендации")
        console.print("")
        model_name = llm_model_name or "unknown"
        console.print(LLM_DISCLAIMER_TEMPLATE.format(model_name=model_name))
        console.print("")
        console.print(llm_advice)

    return console.export_text(styles=False)


# ---------------------------------------------------------------------------
# Формат json
# ---------------------------------------------------------------------------


def _json_default(obj: Any) -> str:
    """Сериализация типов, которые ``json`` сам не умеет (datetime → ISO 8601)."""

    # Pydantic 2 уже сериализует datetime через model_dump(mode="json"),
    # но на всякий случай оставляем fallback.
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj)!r} is not JSON serializable")


def _render_json(
    report: FullReport,
    only_outdated: bool,
    llm_advice: str | None,
) -> str:
    """JSON-вывод. Не мутирует исходный ``FullReport``."""

    # Если есть фильтр — клонируем модель, чтобы не трогать оригинал.
    if only_outdated:
        working = report.model_copy(deep=True)
        working.supported = _filter_supported(working.supported, only_outdated=True)
    else:
        working = report

    # ``model_dump(mode="json")`` сериализует datetime в ISO 8601 строку.
    data = working.model_dump(mode="json")

    if llm_advice is not None:
        data["llm_advice"] = llm_advice

    return json.dumps(data, indent=2, ensure_ascii=False, default=_json_default)


# ---------------------------------------------------------------------------
# Формат markdown
# ---------------------------------------------------------------------------


def _md_escape_pipe(text: str) -> str:
    """Экранировать ``|`` внутри markdown-таблицы."""

    return text.replace("|", "\\|")


def _render_markdown(
    report: FullReport,
    only_outdated: bool,
    llm_advice: str | None,
    llm_model_name: str | None,
) -> str:
    """Markdown-отчёт со всеми секциями."""

    lines: list[str] = []
    lines.append("# Tech Update Recommender Report")
    lines.append("")
    lines.append(f"**Project:** {report.project_path}")
    lines.append(f"**Scanned:** {report.scan_timestamp.isoformat()}")
    lines.append("")
    lines.append(
        f"**Total:** {report.total_packages}, "
        f"**outdated:** {report.outdated_count}, "
        f"**with CVE:** {report.vulnerable_count}"
    )
    lines.append("")

    # Таблица зависимостей
    lines.append("| " + " | ".join(TABLE_COLUMNS) + " |")
    lines.append("|" + "|".join(["------"] * len(TABLE_COLUMNS)) + "|")

    deps = _filter_supported(report.supported, only_outdated)
    for dep in deps:
        latest = dep.latest_version if dep.latest_version is not None else "—"
        cells = [
            _md_escape_pipe(dep.name),
            _md_escape_pipe(dep.ecosystem),
            _md_escape_pipe(dep.current_version),
            _md_escape_pipe(latest),
            _md_escape_pipe(_diff_label(dep)),
            _md_escape_pipe(_advisories_label(dep)),
        ]
        lines.append("| " + " | ".join(cells) + " |")

    # Секция unsupported
    if report.unsupported:
        lines.append("")
        lines.append("## Unsupported packages")
        lines.append("")
        lines.append(
            f"Не проверено через deps.dev: {len(report.unsupported)} "
            f"системных пакетов (deb/apk/rpm)."
        )
        lines.append("")
        for pkg in report.unsupported:
            lines.append(f"- {pkg.name}@{pkg.version} ({pkg.ecosystem})")

    # Секция LLM-рекомендаций
    if llm_advice is not None:
        lines.append("")
        lines.append("## AI-рекомендации")
        lines.append("")
        model_name = llm_model_name or "unknown"
        lines.append(LLM_DISCLAIMER_TEMPLATE.format(model_name=model_name))
        lines.append("")
        lines.append(llm_advice)

    # Финальный перевод строки для аккуратного файла.
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------


def render_report(
    report: FullReport,
    fmt: Literal["table", "json", "markdown"],
    only_outdated: bool = False,
    llm_advice: str | None = None,
    llm_model_name: str | None = None,
) -> str:
    """Отрисовать ``FullReport`` в выбранном формате.

    Параметры:
        report: исходный отчёт DepsDevModule.
        fmt: один из ``"table"``, ``"json"``, ``"markdown"``.
        only_outdated: если ``True`` — supported-пакеты, не помеченные
            ``is_outdated``, исключаются из вывода. Summary всегда
            считается по полному отчёту, unsupported остаются как есть.
        llm_advice: готовая markdown-строка с LLM-рекомендациями.
            Если задано — добавляется отдельной секцией (table/markdown)
            или полем верхнего уровня (json).
        llm_model_name: имя модели для подстановки в дисклеймер.

    Возвращает строку — функция ничего не печатает.
    """

    if fmt == "table":
        return _render_table(report, only_outdated, llm_advice, llm_model_name)
    if fmt == "json":
        return _render_json(report, only_outdated, llm_advice)
    if fmt == "markdown":
        return _render_markdown(report, only_outdated, llm_advice, llm_model_name)
    raise ValueError(f"Unknown report format: {fmt!r}. Expected one of: table, json, markdown.")
