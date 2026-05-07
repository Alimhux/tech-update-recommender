"""Pydantic-модели контрактов между модулями DepScope.

Эти модели — единственный канал обмена данными между Syft, deps.dev и LLM
модулями. Имена полей и типы должны строго соответствовать PLAN.md.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class PackageInfo(BaseModel):
    """Один пакет, извлечённый Syft из CycloneDX SBOM."""

    name: str
    version: str
    purl: str
    ecosystem: str


class Advisory(BaseModel):
    """Запись об уязвимости пакета (CVE/GHSA)."""

    id: str
    severity: float
    summary: str


class DependencyReport(BaseModel):
    """Отчёт по одному пакету после проверки через deps.dev."""

    name: str
    ecosystem: str
    current_version: str
    latest_version: str | None = None
    is_outdated: bool
    semver_diff: str | None = None
    advisories: list[Advisory] = Field(default_factory=list)
    all_versions: list[str] | None = None


class FullReport(BaseModel):
    """Итоговый отчёт DepsDevModule, передаваемый в Report и LLM модули."""

    supported: list[DependencyReport] = Field(default_factory=list)
    unsupported: list[PackageInfo] = Field(default_factory=list)
    scan_timestamp: datetime
    project_path: str
    total_packages: int
    outdated_count: int
    vulnerable_count: int


class LLMInput(BaseModel):
    """Вход LLMModule: отчёт + контекст проекта."""

    report: FullReport
    project_tree: str
    dependency_files: dict[str, str] = Field(default_factory=dict)
