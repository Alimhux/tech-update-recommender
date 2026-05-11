# Модели данных, которые гоняются между модулями.
# Это вся "схема" проекта: Syft складывает PackageInfo, DepsDevModule
# превращает их в DependencyReport и заворачивает в FullReport, а LLM
# добавляет дерево проекта и файлы зависимостей (LLMInput).
# Меняешь поле — пройдись по всему коду.

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class PackageInfo(BaseModel):
    # то, что Syft нашёл в проекте. purl храним как строку, чтобы не
    # таскать packageurl в публичный API — распарсить всегда можно обратно
    name: str
    version: str
    purl: str
    ecosystem: str


class Advisory(BaseModel):
    # уязвимость из deps.dev. severity — float (0..10, CVSS), не enum,
    # потому что deps.dev иногда возвращает дробные значения
    id: str
    severity: float
    summary: str


class DependencyReport(BaseModel):
    # всё, что знаем про один пакет после deps.dev.
    # latest_version и semver_diff могут быть None — если пакет
    # не известен api или версия не парсится в semver
    name: str
    ecosystem: str
    current_version: str
    latest_version: str | None = None
    is_outdated: bool
    semver_diff: str | None = None
    advisories: list[Advisory] = Field(default_factory=list)
    all_versions: list[str] | None = None


class FullReport(BaseModel):
    # главный объект, который потом отдают рендеру и LLM.
    # unsupported — пакеты экосистем, которых deps.dev не знает
    # (deb, apk, rpm и т.п.). Для них пишем "не проверяли"
    supported: list[DependencyReport] = Field(default_factory=list)
    unsupported: list[PackageInfo] = Field(default_factory=list)
    scan_timestamp: datetime
    project_path: str
    total_packages: int
    outdated_count: int
    vulnerable_count: int


class LLMInput(BaseModel):
    # контекст для модели. Кроме отчёта — текстовое дерево проекта
    # и содержимое манифестов (package.json, pyproject.toml и т.п.),
    # чтобы модель видела, где реально лежат зависимости
    report: FullReport
    project_tree: str
    dependency_files: dict[str, str] = Field(default_factory=dict)
