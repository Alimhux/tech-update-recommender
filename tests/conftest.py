"""Общие фикстуры для тестов DepScope."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from depscope.models import (
    Advisory,
    DependencyReport,
    FullReport,
    PackageInfo,
)


@pytest.fixture
def sample_package_info() -> PackageInfo:
    return PackageInfo(
        name="express",
        version="4.18.2",
        purl="pkg:npm/express@4.18.2",
        ecosystem="npm",
    )


@pytest.fixture
def sample_advisory() -> Advisory:
    return Advisory(
        id="GHSA-xxxx-yyyy-zzzz",
        severity=7.5,
        summary="Sample advisory for testing",
    )


@pytest.fixture
def sample_dependency_report(sample_advisory: Advisory) -> DependencyReport:
    return DependencyReport(
        name="express",
        ecosystem="npm",
        current_version="4.18.2",
        latest_version="4.19.2",
        is_outdated=True,
        semver_diff="minor",
        advisories=[sample_advisory],
    )


@pytest.fixture
def sample_full_report(
    sample_dependency_report: DependencyReport,
) -> FullReport:
    return FullReport(
        supported=[sample_dependency_report],
        unsupported=[],
        scan_timestamp=datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc),
        project_path="/tmp/sample",
        total_packages=1,
        outdated_count=1,
        vulnerable_count=1,
    )
