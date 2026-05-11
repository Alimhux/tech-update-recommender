# Модели данных, которые гоняются между модулями.
#
# По сути это вся «схема» проекта: Syft складывает PackageInfo,
# DepsDevModule превращает их в DependencyReport и заворачивает в
# FullReport, а LLM добавляет вокруг отчёта дерево проекта и файлы
# зависимостей (LLMInput). Если меняешь поле — будь готов пройтись
# по всему коду.

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class PackageInfo(BaseModel):
    # То, что Syft нашёл в проекте. purl храним как строку, чтобы не
    # таскать packageurl в публичный API — распарсить всегда можно
    # обратно.
    name: str
    version: str
    purl: str
    ecosystem: str


class Advisory(BaseModel):
    # Уязвимость из deps.dev. severity у нас float (0..10, CVSS),
    # а не enum, потому что deps.dev иногда возвращает дробные значения.
    id: str
    severity: float
    summary: str


class DependencyReport(BaseModel):
    # Всё, что мы знаем про один пакет после похода в deps.dev.
    # latest_version и semver_diff могут быть None — если пакет
    # либо не известен api, либо версия не парсится в semver.
    name: str
    ecosystem: str
    current_version: str
    latest_version: str | None = None
    is_outdated: bool
    semver_diff: str | None = None
    advisories: list[Advisory] = Field(default_factory=list)
    all_versions: list[str] | None = None


class FullReport(BaseModel):
    # Главный объект, который потом отдают рендеру и LLM.
    # unsupported — это пакеты экосистем, которых deps.dev не знает
    # (deb, apk, rpm и прочее). Для них мы просто пишем «не проверяли».
    supported: list[DependencyReport] = Field(default_factory=list)
    unsupported: list[PackageInfo] = Field(default_factory=list)
    scan_timestamp: datetime
    project_path: str
    total_packages: int
    outdated_count: int
    vulnerable_count: int


class LLMInput(BaseModel):
    # Контекст, который скармливаем модели. Кроме самого отчёта —
    # текстовое дерево проекта и содержимое манифестов
    # (package.json, pyproject.toml и т.п.), чтобы модель видела,
    # где у пользователя реально лежат зависимости.
    report: FullReport
    project_tree: str
    dependency_files: dict[str, str] = Field(default_factory=dict)
