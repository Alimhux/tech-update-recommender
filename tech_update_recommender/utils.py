"""Вспомогательные функции Tech Update Recommender.

Здесь живёт нормализация имён пакетов под формат deps.dev и сравнение
версий (`compute_semver_diff`). Все функции — pure, без I/O.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from packaging.version import InvalidVersion, Version

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Сравнение версий
# ---------------------------------------------------------------------------


def compute_semver_diff(current: str, latest: str) -> str | None:
    """Вернуть тип разницы между ``current`` и ``latest``.

    Возвращает:
    - ``"major"`` если major-компонент увеличился;
    - ``"minor"`` если поменялся только minor;
    - ``"patch"`` если поменялся только patch;
    - ``None`` если версии равны (после нормализации) или если хотя бы одна
      из строк не парсится как ``packaging.version.Version``.

    Поддерживает нестрогий SemVer (Maven, PyPI, pre-release): мы опираемся
    на ``packaging.version.Version``, который умеет в эпохи, pre/post/dev и
    т.п. Если парсинг падает — возвращаем ``None``; вызывающая сторона
    использует строгое сравнение строк (``current != latest``) для
    определения ``is_outdated``.
    """

    try:
        cur = Version(current)
        new = Version(latest)
    except InvalidVersion:
        return None

    if cur == new:
        return None

    # Сравниваем именно release-tuple (без pre/post/dev), чтобы semver_diff
    # отражал реальную «крупность» изменения, а не наличие суффикса.
    cur_rel = cur.release
    new_rel = new.release

    # release может иметь произвольную длину; нормализуем до трёх компонент.
    def _pad(release: tuple[int, ...]) -> tuple[int, int, int]:
        padded = list(release[:3])
        while len(padded) < 3:
            padded.append(0)
        return padded[0], padded[1], padded[2]

    cur_major, cur_minor, cur_patch = _pad(cur_rel)
    new_major, new_minor, new_patch = _pad(new_rel)

    if new_major != cur_major:
        return "major"
    if new_minor != cur_minor:
        return "minor"
    if new_patch != cur_patch:
        return "patch"

    # Дошли сюда — major/minor/patch одинаковые, отличается только
    # pre/post/dev/local. Считаем как patch — это ближе всего по семантике.
    return "patch"


# ---------------------------------------------------------------------------
# Нормализация имён пакетов под формат deps.dev
# ---------------------------------------------------------------------------


def normalize_pypi_name(name: str) -> str:
    """Привести имя пакета PyPI к каноничному виду.

    Согласно PEP 503 (на котором основан deps.dev) имена нечувствительны к
    регистру и трактуют ``-`` и ``_`` как одно и то же. Для запросов мы
    приводим к ``lowercase`` и заменяем ``_`` на ``-`` (точки оставляем).
    """

    return name.lower().replace("_", "-")


def url_encode_package_name(system: str, name: str) -> str:
    """URL-encode имя пакета для подстановки в путь GetPackage.

    deps.dev принимает имена с произвольными символами (``:`` для maven,
    ``/`` для go, и т.п.) — но в URL все эти символы должны быть
    закодированы целиком. ``urllib.parse.quote`` с ``safe=""`` делает
    именно это.

    Для PyPI дополнительно нормализуем имя.
    """

    if system == "PYPI":
        name = normalize_pypi_name(name)
    return quote(name, safe="")
