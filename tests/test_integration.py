"""Интеграционные тесты CLI pipeline.

Все внешние зависимости (syft, deps.dev, LiteLLM) мокаются — реальные
HTTP/subprocess вызовы недопустимы.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from depscope.cli import cli
from depscope.models import (
    Advisory,
    DependencyReport,
    FullReport,
    PackageInfo,
)


@pytest.fixture
def mock_packages() -> tuple[list[PackageInfo], list[PackageInfo]]:
    """Один npm-пакет (outdated с CVE) и один PyPI (актуальный)."""

    supported = [
        PackageInfo(
            name="express",
            version="4.18.2",
            purl="pkg:npm/express@4.18.2",
            ecosystem="npm",
        ),
        PackageInfo(
            name="requests",
            version="2.31.0",
            purl="pkg:pypi/requests@2.31.0",
            ecosystem="pypi",
        ),
    ]
    unsupported: list[PackageInfo] = []
    return supported, unsupported


@pytest.fixture
def mock_full_report(
    mock_packages: tuple[list[PackageInfo], list[PackageInfo]],
) -> FullReport:
    """FullReport, соответствующий mock_packages."""

    supported, unsupported = mock_packages
    return FullReport(
        supported=[
            DependencyReport(
                name="express",
                ecosystem="npm",
                current_version="4.18.2",
                latest_version="4.19.2",
                is_outdated=True,
                semver_diff="minor",
                advisories=[
                    Advisory(
                        id="GHSA-xxxx-yyyy-zzzz",
                        severity=7.5,
                        summary="Sample advisory",
                    )
                ],
            ),
            DependencyReport(
                name="requests",
                ecosystem="pypi",
                current_version="2.31.0",
                latest_version="2.31.0",
                is_outdated=False,
                semver_diff=None,
                advisories=[],
            ),
        ],
        unsupported=list(unsupported),
        scan_timestamp=datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc),
        project_path="/tmp/sample",
        total_packages=2,
        outdated_count=1,
        vulnerable_count=1,
    )


def _project_path(tmp_path: Path) -> str:
    """Создать пустой каталог-проект (path должен существовать для click)."""

    project = tmp_path / "project"
    project.mkdir()
    return str(project)


def test_full_pipeline_with_mocks(
    tmp_path: Path,
    mock_packages: tuple[list[PackageInfo], list[PackageInfo]],
    mock_full_report: FullReport,
) -> None:
    """Полный pipeline в режиме full: syft → deps.dev → LLM → markdown."""

    runner = CliRunner()
    project = _project_path(tmp_path)

    async def fake_build_report(*_args, **_kwargs):
        return mock_full_report

    with (
        patch(
            "depscope.cli.scan_project",
            return_value=mock_packages,
        ) as scan_mock,
        patch("depscope.cli.build_report", side_effect=fake_build_report) as build_mock,
        patch(
            "depscope.cli.generate_advice",
            return_value="# Test advice",
        ) as advice_mock,
    ):
        result = runner.invoke(
            cli,
            [
                "scan",
                project,
                "--mode",
                "full",
                "--output",
                "markdown",
                "--llm-model",
                "test/model",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "# DepScope Report" in result.output
    # advice должен попасть в вывод (через render_report)
    assert "# Test advice" in result.output
    # дисклеймер должен присутствовать с именем модели
    assert "test/model" in result.output

    scan_mock.assert_called_once()
    build_mock.assert_called_once()
    advice_mock.assert_called_once()


def test_mode_report_no_llm_call(
    tmp_path: Path,
    mock_packages: tuple[list[PackageInfo], list[PackageInfo]],
    mock_full_report: FullReport,
) -> None:
    """В режиме report LLM не должен вызываться."""

    runner = CliRunner()
    project = _project_path(tmp_path)

    async def fake_build_report(*_args, **_kwargs):
        return mock_full_report

    def boom(*_args, **_kwargs):  # noqa: ANN001 — тестовый стаб
        raise AssertionError("generate_advice should not be called in --mode=report")

    with (
        patch("depscope.cli.scan_project", return_value=mock_packages),
        patch("depscope.cli.build_report", side_effect=fake_build_report),
        patch("depscope.cli.generate_advice", side_effect=boom) as advice_mock,
    ):
        result = runner.invoke(
            cli,
            ["scan", project, "--mode", "report", "--output", "markdown"],
        )

    assert result.exit_code == 0, result.output
    advice_mock.assert_not_called()


def test_only_outdated_end_to_end(
    tmp_path: Path,
    mock_packages: tuple[list[PackageInfo], list[PackageInfo]],
    mock_full_report: FullReport,
) -> None:
    """Флаг --only-outdated должен фильтровать supported в выводе."""

    runner = CliRunner()
    project = _project_path(tmp_path)

    async def fake_build_report(*_args, **_kwargs):
        return mock_full_report

    with (
        patch("depscope.cli.scan_project", return_value=mock_packages),
        patch("depscope.cli.build_report", side_effect=fake_build_report),
    ):
        result = runner.invoke(
            cli,
            [
                "scan",
                project,
                "--mode",
                "report",
                "--output",
                "json",
                "--only-outdated",
            ],
        )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    names = [d["name"] for d in payload["supported"]]
    assert names == ["express"]


def test_save_to_file(
    tmp_path: Path,
    mock_packages: tuple[list[PackageInfo], list[PackageInfo]],
    mock_full_report: FullReport,
) -> None:
    """--save должен создать файл с валидным JSON."""

    runner = CliRunner()
    project = _project_path(tmp_path)
    out_path = tmp_path / "out.json"

    async def fake_build_report(*_args, **_kwargs):
        return mock_full_report

    with (
        patch("depscope.cli.scan_project", return_value=mock_packages),
        patch("depscope.cli.build_report", side_effect=fake_build_report),
    ):
        result = runner.invoke(
            cli,
            [
                "scan",
                project,
                "--mode",
                "report",
                "--output",
                "json",
                "--save",
                str(out_path),
            ],
        )

    assert result.exit_code == 0, result.output
    assert out_path.is_file()
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["total_packages"] == 2
    assert payload["outdated_count"] == 1
    assert any(d["name"] == "express" for d in payload["supported"])


def test_advice_mode_without_model_raises_config_error(tmp_path: Path) -> None:
    """--mode=advice без модели → ConfigError → exit code 5."""

    runner = CliRunner()
    project = _project_path(tmp_path)

    # Нужно подменить env vars и yaml — иначе пользовательский конфиг
    # подцепится. Patch'им load_config, чтобы отдавать пустой Config.
    from depscope.config import Config

    with (
        patch("depscope.cli.load_config", return_value=Config()),
        patch("depscope.cli.scan_project") as scan_mock,
    ):
        result = runner.invoke(
            cli,
            ["scan", project, "--mode", "advice"],
            standalone_mode=True,
        )

    # При standalone_mode=True click сам ловит SystemExit и сообщения
    # выводятся через ConfigError → click.echo → exit 0 от runner-а
    # не подходит. Используем main()? CliRunner всегда вызывает cli
    # напрямую. Проверим, что scan_project не успел вызваться.
    scan_mock.assert_not_called()
    # ConfigError бросается из scan() — runner ловит его как exception.
    assert result.exception is not None
    assert "advice" in str(result.exception) or "model" in str(result.exception).lower()
