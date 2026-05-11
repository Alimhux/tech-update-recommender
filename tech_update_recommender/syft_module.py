# Обёртка над утилитой syft (https://github.com/anchore/syft).
# Запускаем её через subprocess, забираем CycloneDX-JSON, превращаем
# компоненты в наши PackageInfo. Дальше с syft никто не общается —
# все остальные модули работают уже с моделями.

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


# --- ошибки ---------------------------------------------------------------
#
# Все исключения наследуются от SyftError, чтобы в CLI можно было
# ловить одним except.


class SyftError(Exception):
    """Базовое исключение SyftModule."""


class SyftNotFoundError(SyftError):
    """syft не нашли — ни в PATH, ни по пути из конфига."""


class SyftExecutionError(SyftError):
    """syft запустился, но завершился ненулевым кодом."""


class SyftParseError(SyftError):
    """syft вернул что-то, что мы не смогли распарсить."""


# --- какие экосистемы умеет deps.dev --------------------------------------
#
# Ключ — purl type, который кладёт в SBOM сам syft, значение — то,
# как этот же менеджер пакетов называется в API deps.dev. Всё, что
# не в этом словаре, помечаем как unsupported и просто пишем в отчёт.


SUPPORTED_ECOSYSTEMS: dict[str, str] = {
    "npm": "NPM",
    "pypi": "PYPI",
    "maven": "MAVEN",
    "golang": "GO",
    "cargo": "CARGO",
    "gem": "RUBYGEMS",
    "nuget": "NUGET",
}


# --- поиск бинарника ------------------------------------------------------


_SYFT_INSTALL_HINT = (
    "syft не найден. Установите: brew install syft "
    "или скачайте с https://github.com/anchore/syft/releases"
)


def find_syft_binary(custom_path: str | None) -> str:
    # Если пользователь явно указал путь (через --syft-path или конфиг) —
    # доверяем ему, только проверяем, что файл вообще существует. Иначе —
    # обычный поиск по PATH через shutil.which.

    if custom_path:
        path = Path(custom_path)
        if path.is_file():
            return str(path)
        raise SyftNotFoundError(_SYFT_INSTALL_HINT)

    found = shutil.which("syft")
    if found:
        return found

    raise SyftNotFoundError(_SYFT_INSTALL_HINT)


# --- запуск syft ----------------------------------------------------------


def run_syft(project_path: str, syft_binary: str) -> Path:
    # Гоняем syft на каталоге, формат — cyclonedx-json. stdout пишем
    # сразу в файл (через NamedTemporaryFile, чтобы не держать большой
    # вывод в памяти — для крупных проектов это десятки мегабайт).
    # Файл не удаляем — за это отвечает scan_project.

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


# --- разбор CycloneDX-JSON ------------------------------------------------


def _build_name(purl: PackageURL) -> str:
    # У maven «имя» пакета — это пара groupId:artifactId, namespace
    # без artifactId бессмысленен. У остальных менеджеров purl.name —
    # уже готовое имя.
    #
    # Про go: deps.dev хочет полный путь модуля (github.com/foo/bar),
    # но syft и так кладёт его в purl.name, ничего отдельно собирать
    # не нужно. Если когда-нибудь syft начнёт класть туда что-то
    # другое — этот код придётся править.

    if purl.type == "maven" and purl.namespace:
        return f"{purl.namespace}:{purl.name}"
    return purl.name


def parse_cyclonedx(json_path: Path) -> list[PackageInfo]:
    # Открываем CycloneDX и бежим по components. Битый JSON или
    # недоступный файл — фатально (SyftParseError). А вот отдельные
    # компоненты без purl или с кривым purl просто пропускаем

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
            # Так бывает, например, для компонентов типа "operating-system" —
            # они описывают саму ОС, нас не интересуют.
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
            # Без имени или версии пакет всё равно бесполезен — в deps.dev
            # с такими данными мы ничего не запросим.
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


# --- разделение supported / unsupported -----------------------------------


def _dedup(packages: list[PackageInfo]) -> list[PackageInfo]:
    # syft периодически выдаёт один и тот же пакет дважды: например,
    # если он встречается и в lock-файле, и в package.json. Считаем
    # одинаковыми те, у которых совпадает экосистема + имя + версия.
    # Порядок первого вхождения сохраняем — это удобно для тестов.

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
    # Раскидываем пакеты по двум корзинам. Дедупликация — отдельно
    # внутри каждой, чтобы случайный системный дубликат не вытеснил
    # реальный supported-пакет.

    supported: list[PackageInfo] = []
    unsupported: list[PackageInfo] = []
    for pkg in packages:
        if pkg.ecosystem in SUPPORTED_ECOSYSTEMS:
            supported.append(pkg)
        else:
            unsupported.append(pkg)

    return _dedup(supported), _dedup(unsupported)


# --- внешний API ---------------------------------------------------------


def scan_project(
    project_path: str,
    syft_path: str | None = None,
) -> tuple[list[PackageInfo], list[PackageInfo]]:
    # Главная функция модуля: найти syft, запустить, распарсить,
    # разложить по корзинам — и удалить за собой временный файл.
    # Результат сразу готов к передаче в depsdev_module.build_report.

    binary = find_syft_binary(syft_path)
    sbom_path = run_syft(project_path, binary)
    try:
        packages = parse_cyclonedx(sbom_path)
    finally:
        # Если удалить не получилось — не страшно, ОС всё равно
        # подметёт временную папку. Поэтому только debug-лог.
        try:
            sbom_path.unlink()
        except OSError:
            logger.debug("could not unlink temp SBOM file %s", sbom_path)

    return split_supported(packages)
