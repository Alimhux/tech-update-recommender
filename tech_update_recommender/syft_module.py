"""SyftModule — запуск Syft и парсинг CycloneDX SBOM.

Этот модуль:
1. Находит бинарник `syft` (через PATH либо явный путь).
2. Запускает `syft dir:<path> -o cyclonedx-json`, stdout пишет во временный файл.
3. Парсит JSON, извлекает purl каждого компонента.
4. Возвращает список `PackageInfo`, разделённый на supported/unsupported
   экосистемы deps.dev.

Это единственный модуль, общающийся с syft. Дальше pipeline идёт уже
через `PackageInfo` (см. `tech_update_recommender.models`).
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from packageurl import PackageURL

from tech_update_recommender.models import PackageInfo

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Иерархия исключений
# ---------------------------------------------------------------------------


class SyftError(Exception):
    """Базовое исключение SyftModule."""


class SyftNotFoundError(SyftError):
    """Бинарник syft не найден ни в PATH, ни по custom_path."""


class SyftExecutionError(SyftError):
    """Syft отработал с ненулевым кодом возврата."""


class SyftParseError(SyftError):
    """CycloneDX JSON, полученный от syft, не парсится."""


# ---------------------------------------------------------------------------
# Поддерживаемые экосистемы (purl type → deps.dev system)
# ---------------------------------------------------------------------------


SUPPORTED_ECOSYSTEMS: dict[str, str] = {
    "npm": "NPM",
    "pypi": "PYPI",
    "maven": "MAVEN",
    "golang": "GO",
    "cargo": "CARGO",
    "gem": "RUBYGEMS",
    "nuget": "NUGET",
}


# ---------------------------------------------------------------------------
# Поиск бинарника
# ---------------------------------------------------------------------------


_SYFT_INSTALL_HINT = (
    "syft не найден. Установите: brew install syft "
    "или скачайте с https://github.com/anchore/syft/releases"
)


def find_syft_binary(custom_path: str | None) -> str:
    """Вернуть путь к бинарнику syft.

    Если ``custom_path`` задан — проверяем, что он существует и исполнимый.
    Иначе ищем через ``shutil.which``.
    """

    if custom_path:
        path = Path(custom_path)
        if path.is_file():
            return str(path)
        raise SyftNotFoundError(_SYFT_INSTALL_HINT)

    found = shutil.which("syft")
    if found:
        return found

    raise SyftNotFoundError(_SYFT_INSTALL_HINT)


# ---------------------------------------------------------------------------
# Запуск syft
# ---------------------------------------------------------------------------


def run_syft(project_path: str, syft_binary: str) -> Path:
    """Запустить syft на ``project_path``, вернуть путь к JSON-файлу с SBOM.

    stdout записывается во временный файл (delete=False, suffix=".json"),
    путь возвращается. Удаление временного файла остаётся на совести вызывающего.
    """

    logger.info("running syft on %s...", project_path)

    tmp = tempfile.NamedTemporaryFile(
        mode="wb",
        delete=False,
        suffix=".json",
        prefix="tur-syft-",
    )
    tmp_path = Path(tmp.name)

    try:
        result = subprocess.run(  # noqa: S603 — syft binary controlled by user
            [syft_binary, f"dir:{project_path}", "-o", "cyclonedx-json"],
            stdout=tmp,
            stderr=subprocess.PIPE,
            check=False,
        )
    finally:
        tmp.close()

    if result.returncode != 0:
        stderr_text = (result.stderr or b"").decode("utf-8", errors="replace")
        raise SyftExecutionError(
            f"syft failed with exit code {result.returncode}: {stderr_text.strip()}"
        )

    return tmp_path


# ---------------------------------------------------------------------------
# Парсинг CycloneDX JSON
# ---------------------------------------------------------------------------


def _build_name(purl: PackageURL) -> str:
    """Собрать корректное имя пакета из purl.

    Для maven: ``namespace:name`` (groupId:artifactId).
    Для остальных: используем ``purl.name`` как есть.

    Замечание про golang: deps.dev ожидает полный путь модуля
    (``github.com/foo/bar``). Эту нюансировку трогаем уже в DepsDevModule
    (Блок 3) — здесь оставляем стандартное поведение, чтобы не вводить
    специальную логику без явных требований.
    """

    if purl.type == "maven" and purl.namespace:
        return f"{purl.namespace}:{purl.name}"
    return purl.name


def parse_cyclonedx(json_path: Path) -> list[PackageInfo]:
    """Прочитать CycloneDX JSON и вернуть список ``PackageInfo``.

    - Компоненты без ``purl`` пропускаются (DEBUG-лог).
    - Невалидный JSON → ``SyftParseError``.
    - Невалидный purl у одного компонента → пропуск с DEBUG, остальные
      компоненты обрабатываются нормально.
    """

    try:
        with open(json_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise SyftParseError(f"failed to parse CycloneDX JSON at {json_path}: {exc}") from exc
    except OSError as exc:
        raise SyftParseError(f"failed to read CycloneDX JSON at {json_path}: {exc}") from exc

    components = data.get("components") or []
    packages: list[PackageInfo] = []

    for component in components:
        purl_str = component.get("purl")
        if not purl_str:
            logger.debug(
                "skipping component without purl: %s",
                component.get("name", "<unnamed>"),
            )
            continue

        try:
            purl = PackageURL.from_string(purl_str)
        except ValueError as exc:
            logger.debug("skipping component with invalid purl %r: %s", purl_str, exc)
            continue

        if not purl.name or not purl.version:
            logger.debug(
                "skipping component with incomplete purl (no name/version): %s",
                purl_str,
            )
            continue

        packages.append(
            PackageInfo(
                name=_build_name(purl),
                version=purl.version,
                purl=purl_str,
                ecosystem=(purl.type or "").lower(),
            )
        )

    return packages


# ---------------------------------------------------------------------------
# Разделение supported / unsupported + дедупликация
# ---------------------------------------------------------------------------


def _dedup(packages: list[PackageInfo]) -> list[PackageInfo]:
    """Убрать дубликаты по ключу ``(ecosystem, name, version)``.

    Сохраняется порядок первого вхождения.
    """

    seen: set[tuple[str, str, str]] = set()
    result: list[PackageInfo] = []
    for pkg in packages:
        key = (pkg.ecosystem, pkg.name, pkg.version)
        if key in seen:
            continue
        seen.add(key)
        result.append(pkg)
    return result


def split_supported(
    packages: list[PackageInfo],
) -> tuple[list[PackageInfo], list[PackageInfo]]:
    """Разделить пакеты на supported и unsupported по ``SUPPORTED_ECOSYSTEMS``.

    Дедупликация происходит независимо внутри каждой группы.
    """

    supported: list[PackageInfo] = []
    unsupported: list[PackageInfo] = []
    for pkg in packages:
        if pkg.ecosystem in SUPPORTED_ECOSYSTEMS:
            supported.append(pkg)
        else:
            unsupported.append(pkg)

    return _dedup(supported), _dedup(unsupported)


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------


def scan_project(
    project_path: str,
    syft_path: str | None = None,
) -> tuple[list[PackageInfo], list[PackageInfo]]:
    """Полный цикл сканирования проекта через syft.

    Возвращает кортеж ``(supported, unsupported)`` пакетов, готовый
    для передачи в DepsDevModule.
    """

    binary = find_syft_binary(syft_path)
    sbom_path = run_syft(project_path, binary)
    try:
        packages = parse_cyclonedx(sbom_path)
    finally:
        try:
            sbom_path.unlink()
        except OSError:
            logger.debug("could not unlink temp SBOM file %s", sbom_path)

    return split_supported(packages)
