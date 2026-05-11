# Вспомогательные функции, которыми пользуются другие модули.
# Здесь живёт нормализация имён пакетов под формат deps.dev и сравнение
# Версий (`compute_semver_diff`). Все функции — pure, без I/O.

from __future__ import annotations

import logging
from urllib.parse import quote

from packaging.version import InvalidVersion, Version

logger = logging.getLogger(__name__)


# --- сравнение версий ------------------------------------------------------


def compute_semver_diff(current: str, latest: str) -> str | None:
    # Понимаем, насколько новая версия «крупнее» текущей: major / minor /
    # patch. Если версии равны или одна из них не парсится — возвращаем
    # None, и тогда вызывающий код решает сам (обычно — сравнивает
    # строки и считает пакет устаревшим, но без понятного diff).
    #
    # Под капотом packaging.version.Version, у него богатая семантика
    # (epoch, pre/post/dev), но важна только release-часть.

    try:
        cur = Version(current)
        new = Version(latest)
    except InvalidVersion:
        return None

    if cur == new:
        return None

    # Берём только release-tuple (без pre/post/dev) для нас
    # должны различаться лишь как patch, а не major.
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

    # Сюда попадаем, когда три числа совпали, а версии всё-таки разные:
    # значит, отличаются только pre/post/dev/local. Считаем это patch.
    return "patch"


# --- имена пакетов под deps.dev -------------------------------------------


def normalize_pypi_name(name: str) -> str:
    # PEP 503: pypi не различает регистр и `-`/`_`. deps.dev живёт по
    # тем же правилам, поэтому перед запросом причёсываем имя сами.
    # Точки не трогаем — они валидны.
    return name.lower().replace("_", "-")


def url_encode_package_name(system: str, name: str) -> str:
    # Имена пакетов содержат всё что угодно: двоеточия (maven),
    # слэши (go-модули), скобки в названиях scope (@scope/pkg).
    # В URL deps.dev их положено кодировать полностью, без safe-символов.
    # quote(safe="") как раз это и делает.
    if system == "PYPI":
        name = normalize_pypi_name(name)
    return quote(name, safe="")
