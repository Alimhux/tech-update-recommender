"""Тесты Pydantic-моделей контрактов."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from tech_update_recommender.models import (
    Advisory,
    DependencyReport,
    FullReport,
    LLMInput,
    PackageInfo,
)

# --- PackageInfo --------------------------------------------------------------


def test_package_info_valid() -> None:
    pkg = PackageInfo(
        name="express",
        version="4.18.2",
        purl="pkg:npm/express@4.18.2",
        ecosystem="npm",
    )
    assert pkg.name == "express"
    assert pkg.ecosystem == "npm"


def test_package_info_missing_required_field() -> None:
    with pytest.raises(ValidationError):
        PackageInfo(name="express", version="4.18.2", purl="pkg:npm/express@4.18.2")  # type: ignore[call-arg]


def test_package_info_wrong_type() -> None:
    with pytest.raises(ValidationError):
        PackageInfo(
            name=123,  # type: ignore[arg-type]
            version="4.18.2",
            purl="pkg:npm/express@4.18.2",
            ecosystem="npm",
        )


# --- Advisory -----------------------------------------------------------------


def test_advisory_valid() -> None:
    adv = Advisory(id="CVE-2023-0001", severity=8.1, summary="Critical bug")
    assert adv.id == "CVE-2023-0001"
    assert adv.severity == 8.1


def test_advisory_severity_must_be_number() -> None:
    with pytest.raises(ValidationError):
        Advisory(id="CVE-2023-0001", severity="high", summary="x")  # type: ignore[arg-type]


# --- DependencyReport ---------------------------------------------------------


def test_dependency_report_minimal() -> None:
    dep = DependencyReport(
        name="lodash",
        ecosystem="npm",
        current_version="4.17.20",
        is_outdated=True,
    )
    assert dep.latest_version is None
    assert dep.semver_diff is None
    assert dep.advisories == []
    assert dep.all_versions is None


def test_dependency_report_full() -> None:
    dep = DependencyReport(
        name="lodash",
        ecosystem="npm",
        current_version="4.17.20",
        latest_version="4.17.21",
        is_outdated=True,
        semver_diff="patch",
        advisories=[Advisory(id="CVE-x", severity=5.5, summary="s")],
        all_versions=["4.17.20", "4.17.21"],
    )
    assert dep.semver_diff == "patch"
    assert len(dep.advisories) == 1


def test_dependency_report_missing_required() -> None:
    with pytest.raises(ValidationError):
        DependencyReport(
            name="lodash",
            ecosystem="npm",
            current_version="4.17.20",
        )  # type: ignore[call-arg]


# --- FullReport ---------------------------------------------------------------


def test_full_report_valid() -> None:
    report = FullReport(
        supported=[],
        unsupported=[],
        scan_timestamp=datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc),
        project_path="/tmp/x",
        total_packages=0,
        outdated_count=0,
        vulnerable_count=0,
    )
    assert report.total_packages == 0
    assert report.supported == []


def test_full_report_defaults_for_lists() -> None:
    report = FullReport(
        scan_timestamp=datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc),
        project_path="/tmp/x",
        total_packages=0,
        outdated_count=0,
        vulnerable_count=0,
    )
    assert report.supported == []
    assert report.unsupported == []


def test_full_report_invalid_timestamp() -> None:
    with pytest.raises(ValidationError):
        FullReport(
            scan_timestamp="not-a-datetime-at-all",
            project_path="/tmp/x",
            total_packages=0,
            outdated_count=0,
            vulnerable_count=0,
        )


# --- LLMInput -----------------------------------------------------------------


def test_llm_input_valid(sample_full_report: FullReport) -> None:
    llm_in = LLMInput(
        report=sample_full_report,
        project_tree="root\n  pkg.json",
        dependency_files={"package.json": '{"name":"x"}'},
    )
    assert "pkg.json" in llm_in.project_tree
    assert llm_in.dependency_files["package.json"].startswith("{")


def test_llm_input_default_dependency_files(sample_full_report: FullReport) -> None:
    llm_in = LLMInput(report=sample_full_report, project_tree="")
    assert llm_in.dependency_files == {}


def test_llm_input_missing_report() -> None:
    with pytest.raises(ValidationError):
        LLMInput(project_tree="")  # type: ignore[call-arg]
