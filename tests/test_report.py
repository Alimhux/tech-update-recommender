"""Тесты ReportModule (Блок 4)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from depscope.models import (
    Advisory,
    DependencyReport,
    FullReport,
    PackageInfo,
)
from depscope.report import LLM_DISCLAIMER_TEMPLATE, render_report

# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------


@pytest.fixture
def vulnerable_dep() -> DependencyReport:
    return DependencyReport(
        name="lodash",
        ecosystem="npm",
        current_version="4.17.20",
        latest_version="4.17.21",
        is_outdated=True,
        semver_diff="patch",
        advisories=[
            Advisory(
                id="GHSA-35jh-r3h4-6jhm",
                severity=7.4,
                summary="Command injection in lodash",
            )
        ],
    )


@pytest.fixture
def major_outdated_dep() -> DependencyReport:
    return DependencyReport(
        name="express",
        ecosystem="npm",
        current_version="3.21.0",
        latest_version="4.18.2",
        is_outdated=True,
        semver_diff="major",
        advisories=[],
    )


@pytest.fixture
def up_to_date_dep() -> DependencyReport:
    return DependencyReport(
        name="requests",
        ecosystem="pypi",
        current_version="2.31.0",
        latest_version="2.31.0",
        is_outdated=False,
        semver_diff=None,
        advisories=[],
    )


@pytest.fixture
def unsupported_pkg() -> PackageInfo:
    return PackageInfo(
        name="libssl1.1",
        version="1.1.1n-0+deb11u3",
        purl="pkg:deb/debian/libssl1.1@1.1.1n-0+deb11u3",
        ecosystem="deb",
    )


@pytest.fixture
def rich_full_report(
    vulnerable_dep: DependencyReport,
    major_outdated_dep: DependencyReport,
    up_to_date_dep: DependencyReport,
    unsupported_pkg: PackageInfo,
) -> FullReport:
    return FullReport(
        supported=[vulnerable_dep, major_outdated_dep, up_to_date_dep],
        unsupported=[unsupported_pkg],
        scan_timestamp=datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc),
        project_path="/tmp/sample-project",
        total_packages=4,
        outdated_count=2,
        vulnerable_count=1,
    )


@pytest.fixture
def empty_full_report() -> FullReport:
    return FullReport(
        supported=[],
        unsupported=[],
        scan_timestamp=datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc),
        project_path="/tmp/empty",
        total_packages=0,
        outdated_count=0,
        vulnerable_count=0,
    )


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def test_json_format_valid(rich_full_report: FullReport) -> None:
    """JSON-вывод парсится через json.loads без ошибок."""

    out = render_report(rich_full_report, fmt="json")
    parsed = json.loads(out)
    assert parsed["total_packages"] == 4
    assert parsed["outdated_count"] == 2
    assert parsed["vulnerable_count"] == 1
    assert len(parsed["supported"]) == 3
    assert len(parsed["unsupported"]) == 1
    # datetime сериализован в ISO-строку
    assert isinstance(parsed["scan_timestamp"], str)
    assert "2026-05-07" in parsed["scan_timestamp"]


def test_json_includes_llm_advice(rich_full_report: FullReport) -> None:
    """Поле llm_advice добавляется в корень JSON при наличии."""

    advice = "## Recommendations\n- Update lodash"
    out = render_report(
        rich_full_report,
        fmt="json",
        llm_advice=advice,
        llm_model_name="claude-sonnet-4-20250514",
    )
    parsed = json.loads(out)
    assert parsed["llm_advice"] == advice


def test_json_no_llm_advice_key_when_absent(rich_full_report: FullReport) -> None:
    """Без llm_advice ключа в JSON быть не должно."""

    out = render_report(rich_full_report, fmt="json")
    parsed = json.loads(out)
    assert "llm_advice" not in parsed


def test_only_outdated_filter(rich_full_report: FullReport) -> None:
    """only_outdated=True исключает актуальные supported-пакеты из JSON."""

    out = render_report(rich_full_report, fmt="json", only_outdated=True)
    parsed = json.loads(out)
    names = {p["name"] for p in parsed["supported"]}
    assert "requests" not in names  # актуальный — исключён
    assert "lodash" in names
    assert "express" in names
    assert len(parsed["supported"]) == 2


def test_only_outdated_does_not_mutate_report(
    rich_full_report: FullReport,
) -> None:
    """Фильтр не должен изменять переданный FullReport."""

    before = len(rich_full_report.supported)
    render_report(rich_full_report, fmt="json", only_outdated=True)
    assert len(rich_full_report.supported) == before


def test_summary_counts(rich_full_report: FullReport) -> None:
    """Summary считается по полному отчёту, не по фильтру."""

    out = render_report(rich_full_report, fmt="json", only_outdated=True)
    parsed = json.loads(out)
    # Несмотря на фильтр (1 пакет был отброшен), агрегаты сохраняются.
    assert parsed["total_packages"] == 4
    assert parsed["outdated_count"] == 2
    assert parsed["vulnerable_count"] == 1

    # Та же проверка для table-формата.
    out_table = render_report(rich_full_report, fmt="table", only_outdated=True)
    assert "Total: 4, outdated: 2, with CVE: 1" in out_table

    # И для markdown.
    out_md = render_report(rich_full_report, fmt="markdown", only_outdated=True)
    assert "**Total:** 4" in out_md
    assert "**outdated:** 2" in out_md
    assert "**with CVE:** 1" in out_md


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def test_markdown_has_disclaimer(rich_full_report: FullReport) -> None:
    """Дисклеймер с подставленным именем модели присутствует в markdown."""

    advice = "Update everything safely."
    model_name = "gemini/gemini-2.0-flash"
    out = render_report(
        rich_full_report,
        fmt="markdown",
        llm_advice=advice,
        llm_model_name=model_name,
    )
    expected = LLM_DISCLAIMER_TEMPLATE.format(model_name=model_name)
    assert expected in out
    # Проверяем, что само имя модели прокинулось.
    assert model_name in out
    # Заголовок секции и содержимое тоже на месте.
    assert "## AI-рекомендации" in out
    assert advice in out


def test_markdown_no_disclaimer_without_advice(
    rich_full_report: FullReport,
) -> None:
    """Без llm_advice секции AI-рекомендаций нет."""

    out = render_report(rich_full_report, fmt="markdown")
    assert "## AI-рекомендации" not in out
    assert "Рекомендации ниже сгенерированы" not in out


def test_markdown_table_pipe_format(rich_full_report: FullReport) -> None:
    """В markdown-выводе есть pipe-таблица с заголовком и сепаратором."""

    out = render_report(rich_full_report, fmt="markdown")
    header = "| Name | Ecosystem | Current | Latest | Diff | Advisories |"
    assert header in out
    # Сепаратор содержит правильное число колонок.
    assert "|------|------|------|------|------|------|" in out
    # Хотя бы одна строка пакета.
    assert "| lodash | npm |" in out
    # CVE-id виден в колонке Advisories.
    assert "GHSA-35jh-r3h4-6jhm" in out


def test_markdown_metadata_block(rich_full_report: FullReport) -> None:
    """Project / Scanned метаданные присутствуют."""

    out = render_report(rich_full_report, fmt="markdown")
    assert "# DepScope Report" in out
    assert "**Project:** /tmp/sample-project" in out
    assert "**Scanned:**" in out
    assert "2026-05-07" in out


# ---------------------------------------------------------------------------
# Unsupported
# ---------------------------------------------------------------------------


def test_unsupported_section_present(rich_full_report: FullReport) -> None:
    """При наличии unsupported в markdown и table есть секция."""

    out_md = render_report(rich_full_report, fmt="markdown")
    assert "## Unsupported packages" in out_md
    assert "libssl1.1@1.1.1n-0+deb11u3 (deb)" in out_md

    out_table = render_report(rich_full_report, fmt="table")
    assert "Не проверено через deps.dev" in out_table
    assert "системных пакетов" in out_table


def test_unsupported_section_absent_when_empty(
    sample_full_report: FullReport,
) -> None:
    """Без unsupported соответствующие секции не появляются."""

    out_md = render_report(sample_full_report, fmt="markdown")
    assert "## Unsupported packages" not in out_md

    out_table = render_report(sample_full_report, fmt="table")
    assert "Не проверено через deps.dev" not in out_table


# ---------------------------------------------------------------------------
# Table
# ---------------------------------------------------------------------------


def test_table_format_contains_columns(rich_full_report: FullReport) -> None:
    """Заголовки колонок присутствуют в выводе."""

    out = render_report(rich_full_report, fmt="table")
    for col in ("Name", "Ecosystem", "Current", "Latest", "Diff", "Advisories"):
        assert col in out


def test_table_summary_line(rich_full_report: FullReport) -> None:
    """Строка summary печатается."""

    out = render_report(rich_full_report, fmt="table")
    assert "Total: 4, outdated: 2, with CVE: 1" in out


def test_table_no_crash_empty(empty_full_report: FullReport) -> None:
    """Пустой FullReport не падает, summary печатается."""

    out = render_report(empty_full_report, fmt="table")
    assert "Total: 0, outdated: 0, with CVE: 0" in out
    # Заголовки таблицы тоже присутствуют (пустая таблица — норма).
    assert "Name" in out


def test_table_llm_disclaimer(rich_full_report: FullReport) -> None:
    """Если задан llm_advice — table-вывод содержит дисклеймер."""

    advice = "Some markdown advice"
    out = render_report(
        rich_full_report,
        fmt="table",
        llm_advice=advice,
        llm_model_name="gpt-4o",
    )
    expected = LLM_DISCLAIMER_TEMPLATE.format(model_name="gpt-4o")
    assert expected in out
    assert advice in out


def test_table_only_outdated_drops_rows(rich_full_report: FullReport) -> None:
    """only_outdated скрывает актуальный пакет в table-выводе."""

    out = render_report(rich_full_report, fmt="table", only_outdated=True)
    # Актуальный пакет (requests) не должен попасть в строки таблицы.
    assert "requests" not in out
    assert "lodash" in out
    assert "express" in out


# ---------------------------------------------------------------------------
# Прочее
# ---------------------------------------------------------------------------


def test_unknown_format_raises(rich_full_report: FullReport) -> None:
    """Неизвестный формат — ValueError."""

    with pytest.raises(ValueError):
        render_report(rich_full_report, fmt="xml")  # type: ignore[arg-type]


def test_invalid_version_marker(empty_full_report: FullReport) -> None:
    """is_outdated=True && semver_diff=None → diff помечен как '?'."""

    weird = DependencyReport(
        name="weird-pkg",
        ecosystem="pypi",
        current_version="not-a-version",
        latest_version="1.0.0",
        is_outdated=True,
        semver_diff=None,
        advisories=[],
    )
    report = empty_full_report.model_copy(deep=True)
    report.supported = [weird]
    report.total_packages = 1
    report.outdated_count = 1

    out_table = render_report(report, fmt="table")
    assert "weird-pkg" in out_table
    assert "?" in out_table

    out_md = render_report(report, fmt="markdown")
    assert "| weird-pkg | pypi | not-a-version | 1.0.0 | ? |" in out_md
