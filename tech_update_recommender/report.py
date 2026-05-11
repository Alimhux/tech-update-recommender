# Превращаем FullReport в человекочитаемый вывод.
# Поддерживаем три формата: table (rich), json и markdown.
# Сам модуль ничего не печатает и не пишет в файлы — только
# возвращает строку. Решать, куда её девать, — задача CLI.

from __future__ import annotations

import io
import json
from typing import Any, Literal

from rich.console import Console
from rich.table import Table

from tech_update_recommender.models import DependencyReport, FullReport

# --- константы ------------------------------------------------------------

# Дисклеймер обязан выводиться рядом с любым LLM-блоком. Точный текст
# зафиксирован в PLAN.md — менять формулировки нельзя, иначе тесты
# отвалятся, да и пользователь не обязан догадываться, что советы
# написала нейронка.
LLM_DISCLAIMER_TEMPLATE = (
    "Рекомендации ниже сгенерированы AI-моделью ({model_name}) и носят "
    "рекомендательный характер. Качество рекомендаций зависит от выбранной "
    "модели. Всегда проверяйте совместимость обновлений в вашем проекте "
    "перед применением."
)

# Один и тот же набор колонок и для rich-таблицы, и для markdown.
# Если поменять порядок — поправится сразу в обоих местах.
TABLE_COLUMNS = (
    "Name",
    "Ecosystem",
    "Current",
    "Latest",
    "Diff",
    "Advisories",
)


# --- хелперы --------------------------------------------------------------


def _filter_supported(deps: list[DependencyReport], only_outdated: bool) -> list[DependencyReport]:
    # Флаг --only-outdated скрывает актуальные пакеты из таблицы.
    # В json он же — но реализован отдельно, на копии модели.
    if not only_outdated:
        return list(deps)
    return [d for d in deps if d.is_outdated]


def _row_color(dep: DependencyReport) -> str:
    # Цвет строки таблицы. Приоритет такой:
    # уязвимости — красный, major — жёлтый, minor/patch — зелёный,
    # всё остальное (актуальные и битые версии) — серый.
    if dep.advisories:
        return "red"
    if dep.semver_diff == "major":
        return "yellow"
    if dep.semver_diff in ("minor", "patch"):
        return "green"
    return "grey50"


def _diff_label(dep: DependencyReport) -> str:
    # В колонке Diff пишем «major/minor/patch», либо «?» если знаем,
    # что версия устарела, но diff не посчитался (версия не SemVer),
    # либо тире — когда версия актуальна.
    if dep.semver_diff is not None:
        return dep.semver_diff
    if dep.is_outdated:
        return "?"
    return "—"


def _advisories_label(dep: DependencyReport) -> str:
    # Просто id-шники CVE/GHSA через запятую. Полные описания
    # не выводим — в табличку они не влезут.
    if not dep.advisories:
        return "—"
    return ", ".join(a.id for a in dep.advisories)


def _summary_line(report: FullReport) -> str:
    # Однострочная сводка над таблицей. Считаем по полному отчёту,
    # а не по отфильтрованному — пользователю важно видеть общую
    # картину, даже если он включил --only-outdated.
    return (
        f"Total: {report.total_packages}, "
        f"outdated: {report.outdated_count}, "
        f"with CVE: {report.vulnerable_count}"
    )


def _unsupported_summary(report: FullReport) -> str | None:
    # Короткая фраза «вот эти пакеты deps.dev не знает». Если их нет —
    # возвращаем None, чтобы вызывающий код не печатал пустую секцию.
    if not report.unsupported:
        return None
    return (
        f"Не проверено через deps.dev: {len(report.unsupported)} системных пакетов (deb/apk/rpm)"
    )


# --- формат table ---------------------------------------------------------


def _render_table(
    report: FullReport,
    only_outdated: bool,
    llm_advice: str | None,
    llm_model_name: str | None,
) -> str:
    # Внутри прячем rich-консоль с record=True, чтобы потом получить
    # чистую строку. force_terminal=False и no_color=True убивают
    # ANSI-эскейпы — это удобно и для тестов, и при перенаправлении в
    # файл. soft_wrap отключает жёсткий перенос: длинные строки с
    # дисклеймером и LLM-текстом тесты ищут как одну подстроку.
    console = Console(
        record=True,
        file=io.StringIO(),
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


# --- формат json ----------------------------------------------------------


def _json_default(obj: Any) -> str:
    # Подстраховка на случай, если в дамп просочится что-то, что
    # стандартный json не умеет сериализовать. Сейчас pydantic с
    # mode="json" сам приводит datetime к ISO 8601, но мало ли.
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj)!r} is not JSON serializable")


def _render_json(
    report: FullReport,
    only_outdated: bool,
    llm_advice: str | None,
) -> str:
    # Если стоит --only-outdated, делаем глубокую копию и режем
    # supported уже на ней. Оригинальный FullReport остаётся целым —
    # это важно, потому что один и тот же объект может потом
    # отдаваться в LLM или повторно рендериться.
    if only_outdated:
        working = report.model_copy(deep=True)
        working.supported = _filter_supported(working.supported, only_outdated=True)
    else:
        working = report

    # mode="json" — это то, что разворачивает datetime, SecretStr и
    # прочую pydantic-магию в обычные JSON-типы.
    data = working.model_dump(mode="json")

    if llm_advice is not None:
        data["llm_advice"] = llm_advice

    return json.dumps(data, indent=2, ensure_ascii=False, default=_json_default)


# --- формат markdown ------------------------------------------------------


def _md_escape_pipe(text: str) -> str:
    # Внутри ячеек markdown-таблицы вертикальная черта обязана быть
    # экранирована, иначе таблица «поедет».
    return text.replace("|", "\\|")


def _render_markdown(
    report: FullReport,
    only_outdated: bool,
    llm_advice: str | None,
    llm_model_name: str | None,
) -> str:
    # Собираем markdown построчно — выходит проще, чем городить шаблоны.

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

    # Шапка и разделитель таблицы. Колонки берём из TABLE_COLUMNS,
    # чтобы было синхронно с rich-вариантом.
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

    # Системные пакеты — отдельной секцией снизу, просто списком.
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

    # И отдельным блоком — рекомендации от модели, если были.
    if llm_advice is not None:
        lines.append("")
        lines.append("## AI-рекомендации")
        lines.append("")
        model_name = llm_model_name or "unknown"
        lines.append(LLM_DISCLAIMER_TEMPLATE.format(model_name=model_name))
        lines.append("")
        lines.append(llm_advice)

    # Финальный \n, чтобы файл заканчивался переводом строки.
    lines.append("")
    return "\n".join(lines)


# --- единственная публичная функция --------------------------------------


def render_report(
    report: FullReport,
    fmt: Literal["table", "json", "markdown"],
    only_outdated: bool = False,
    llm_advice: str | None = None,
    llm_model_name: str | None = None,
) -> str:
    # Простой диспетчер. Сам ничего не делает, только зовёт нужный
    # рендерер. only_outdated прячет актуальные пакеты, но summary
    # всё равно считается по полному отчёту. llm_advice — уже готовая
    # markdown-строка от LLM-модуля; если она есть, рядом обязательно
    # добавляется дисклеймер с именем модели.

    if fmt == "table":
        return _render_table(report, only_outdated, llm_advice, llm_model_name)
    if fmt == "json":
        return _render_json(report, only_outdated, llm_advice)
    if fmt == "markdown":
        return _render_markdown(report, only_outdated, llm_advice, llm_model_name)
    raise ValueError(f"Unknown report format: {fmt!r}. Expected one of: table, json, markdown.")
