"""
depsdev module - это модуль для взаимодействия системы с API от deps.dev

Модуль запрашивает у deps.dev информацию о последних версиях пакетов,
найдённых в анализируемом проекте, и о наличии у них известных уязвимостей.
Эти данные нужны для построения рекомендаций по обновлению зависимостей.

Модуль использует SQLite-кеш, чтобы не обращаться к API при повторных
запусках, а также сводит результаты в FullReport из models.py

Все HTTP-запросы асинхронные, работают через aiohttp
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

import aiohttp
from packageurl import PackageURL

from tech_update_recommender.cache import _LATEST_KEY, Cache
from tech_update_recommender.models import (
    Advisory,
    DependencyReport,
    FullReport,
    PackageInfo,
)
from tech_update_recommender.syft_module import SUPPORTED_ECOSYSTEMS
from tech_update_recommender.utils import (
    compute_semver_diff,
    normalize_pypi_name,
    url_encode_package_name,
)

logger = logging.getLogger(__name__)


DEPSDEV_BATCH_URL = "https://api.deps.dev/v3alpha/versionbatch"
DEPSDEV_PACKAGE_URL_TPL = "https://api.deps.dev/v3/systems/{system}/packages/{name}"

REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RETRIES = 3
BATCH_CHUNK_SIZE = 5000
GETPACKAGE_CONCURRENCY = 20


# Исключения
class DepsDevError(Exception):
    """Базовое исключение DepsDevModule."""


# ---------------------------------------------------------------------------
# Внутренние утилиты: retry + sleep (sleep — отдельной функцией, чтобы
# тесты могли мокать без реальной задержки)
# ---------------------------------------------------------------------------


async def _sleep(seconds: float) -> None:
    """Тонкая обёртка над asyncio.sleep — точка для патчинга в тестах."""

    await asyncio.sleep(seconds)


async def _with_retry(
    op: Callable[[], Awaitable[aiohttp.ClientResponse]],
    *,
    retries: int = MAX_RETRIES,
    label: str = "request",
) -> aiohttp.ClientResponse:
    """Выполнить async-операцию с retry на 5xx и сетевые ошибки, либо прокидываем
    DepsDevError если все попытки упали с сетевой ошибкой.
    """

    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            response = await op()
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
        ) as exc:
            last_exc = exc
            logger.debug(
                "%s: network error on attempt %d/%d: %s",
                label,
                attempt + 1,
                retries,
                exc,
            )
            if attempt + 1 >= retries:
                break
            await _sleep(2**attempt)
            continue

        if 500 <= response.status < 600:
            logger.debug(
                "%s: server %d on attempt %d/%d",
                label,
                response.status,
                attempt + 1,
                retries,
            )
            response.release()
            if attempt + 1 >= retries:
                raise DepsDevError(
                    f"deps.dev API недоступен после {retries} попыток "
                    f"(последний статус {response.status})"
                )
            await _sleep(2**attempt)
            continue

        return response

    raise DepsDevError(f"deps.dev API недоступен после {retries} попыток: {last_exc}")


# 3.2 Batch-запрос текущих версий
def _batch_request_payload(packages: list[PackageInfo]) -> dict:
    """Собирает payload для POST /v3alpha/versionbatch.

    Имя пакета в payload передаётся в каноничной для deps.dev форме:
    модули от pypi — нормализуются, остальные — используются как есть.
    """

    requests = []
    for p in packages:
        system = SUPPORTED_ECOSYSTEMS[p.ecosystem]
        name = _canonical_name(p, system)
        requests.append(
            {
                "versionKey": {
                    "system": system,
                    "name": name,
                    "version": p.version,
                }
            }
        )
    return {"requests": requests}


def _canonical_name(package: PackageInfo, system: str) -> str:
    """Преобразует в каноничное имя пакета для deps.dev (для payload и ключа кеша)."""

    if system == "PYPI":
        return normalize_pypi_name(package.name)

    if system == "GO":
        # SyftModule сохраняет в .name только purl.name без namespace.
        # Для go нам нужен полный путь модуля, так что читаем его из purl.
        try:
            purl = PackageURL.from_string(package.purl)
        except ValueError:
            return package.name

        if purl.namespace:
            return f"{purl.namespace}/{purl.name}"
        return purl.name or package.name

    return package.name


async def fetch_current_versions(
    session: aiohttp.ClientSession,
    packages: list[PackageInfo],
) -> dict[tuple[str, str, str], dict | None]:
    """Запросить через batch endpoint данные по текущим версиям пакетов.

    Возвращает словарь (system, name, version) : response_payload.
    Ключ name в результате - каноничное имя
    """

    if not packages:
        return {}

    result: dict[tuple[str, str, str], dict | None] = {}

    # делаем чанки (хотя 5000 пакетов на запуск маловероятно — но требование).
    for start in range(0, len(packages), BATCH_CHUNK_SIZE):
        chunk = packages[start : start + BATCH_CHUNK_SIZE]
        payload = _batch_request_payload(chunk)

        async def _do(_payload: dict = payload) -> aiohttp.ClientResponse:
            return await session.post(DEPSDEV_BATCH_URL, json=_payload)

        response = await _with_retry(_do, label="versionbatch")
        try:
            data = await response.json(content_type=None)
        finally:
            response.release()

        responses = data.get("responses") or []
        for entry in responses:
            key = _extract_batch_key(entry)
            if key is None:
                continue
            payload_for_key = entry.get("version")
            result[key] = payload_for_key

        # Для пакетов из chunk, по которым нет ответа, явно ставим None.
        for p in chunk:
            system = SUPPORTED_ECOSYSTEMS[p.ecosystem]
            name = _canonical_name(p, system)
            key = (system, name, p.version)
            result.setdefault(key, None)

    return result


def _extract_batch_key(entry: dict) -> tuple[str, str, str] | None:

    vk = entry.get("versionKey")
    if vk is None:
        request = entry.get("request") or {}
        vk = request.get("versionKey")
    if not vk:
        return None
    system = vk.get("system")
    name = vk.get("name")
    version = vk.get("version")
    if not (system and name and version):
        return None
    return system, name, version


# 3.3 GetPackage для последних версий (latest)


async def fetch_latest_versions(
    session: aiohttp.ClientSession,
    packages: list[PackageInfo],
) -> dict[tuple[str, str], str | None]:
    """Запросить последнюю версию для каждого уникального (system, name).

    Параллельность ограничена семафором (GETPACKAGE_CONCURRENCY).
    Дедупликация идёт по каноническому имени (PyPI lowercase, Golang
    namespace+name).
    """

    if not packages:
        return {}

    # Дедупликация по ключу (system, canonical_name).
    pairs: dict[tuple[str, str], None] = {}
    for p in packages:
        system = SUPPORTED_ECOSYSTEMS[p.ecosystem]
        name = _canonical_name(p, system)
        pairs[(system, name)] = None

    semaphore = asyncio.Semaphore(GETPACKAGE_CONCURRENCY)

    async def _fetch_one(system: str, name: str) -> tuple[tuple[str, str], str | None]:
        async with semaphore:
            latest = await _fetch_latest_for(session, system, name)
        return (system, name), latest

    results = await asyncio.gather(
        *(_fetch_one(system, name) for (system, name) in pairs),
        return_exceptions=False,
    )

    return dict(results)


async def _fetch_latest_for(
    session: aiohttp.ClientSession,
    system: str,
    name: str,
) -> str | None:
    """Один GetPackage-запрос с retry и обработкой 404."""

    encoded_name = url_encode_package_name(system, name)
    url = DEPSDEV_PACKAGE_URL_TPL.format(system=system, name=encoded_name)

    async def _do() -> aiohttp.ClientResponse:
        return await session.get(url)

    response = await _with_retry(_do, label=f"GetPackage {system}/{name}")
    try:
        if response.status == 404:
            logger.debug("deps.dev: package not found: %s/%s", system, name)
            return None
        if 400 <= response.status < 500:
            logger.debug(
                "deps.dev: client error %d for %s/%s",
                response.status,
                system,
                name,
            )
            return None
        data = await response.json(content_type=None)
    finally:
        response.release()

    return _pick_latest_version(data)


def _pick_latest_version(package_payload: dict) -> str | None:
    """Из ответа GetPackage выбрать строку latest-версии.

    Алгоритм:
    1. Если есть запись с isDefault=True — берём её versionKey.version.
    2. Иначе сортируем по publishedAt (если есть) и берём максимум.
    3. Иначе — последняя в массиве.
    4. Если массив пуст — None.
    """

    versions = package_payload.get("versions") or []
    if not versions:
        return None

    # 1. isDefault
    for v in versions:
        if v.get("isDefault") is True:
            vk = v.get("versionKey") or {}
            ver = vk.get("version")
            if ver:
                return ver

    # 2. publishedAt
    dated = [v for v in versions if v.get("publishedAt")]
    if dated:
        dated.sort(key=lambda v: v["publishedAt"])
        vk = dated[-1].get("versionKey") or {}
        ver = vk.get("version")
        if ver:
            return ver

    # 3. fallback: последняя
    last = versions[-1]
    vk = last.get("versionKey") or {}
    return vk.get("version")


# 3.6 Построение полного отчёта


def _parse_advisories(version_payload: dict | None) -> list[Advisory]:
    """Достать advisories из ответа версии.

    deps.dev возвращает поле advisoryKeys со ссылками вида
    [{"id": "GHSA-xxxx"}]. Иногда встречается ``advisories`` со
    структурой ``[{"id": ..., "summary": ..., "severity": ...}]``. Парсим
    оба варианта.

    TODO: severity без второго запроса в /v3/advisories/{id} мы не знаем,
    поэтому при отсутствии — оставляем 0.0 и summary="".
    """

    if not version_payload:
        return []

    out: list[Advisory] = []

    advisory_keys = version_payload.get("advisoryKeys") or []
    for entry in advisory_keys:
        if isinstance(entry, str):
            adv_id = entry
        elif isinstance(entry, dict):
            adv_id = entry.get("id") or entry.get("name") or ""
        else:
            continue
        if not adv_id:
            continue
        out.append(Advisory(id=adv_id, severity=0.0, summary=""))

    advisories = version_payload.get("advisories") or []
    for entry in advisories:
        if not isinstance(entry, dict):
            continue
        adv_id = entry.get("id") or entry.get("name") or ""
        if not adv_id:
            continue
        severity = entry.get("severity")
        if isinstance(severity, dict):
            score = severity.get("score") or severity.get("cvss") or 0.0
        else:
            score = severity or 0.0
        try:
            severity_num = float(score)
        except (TypeError, ValueError):
            severity_num = 0.0
        summary = entry.get("summary") or entry.get("title") or ""
        out.append(Advisory(id=adv_id, severity=severity_num, summary=str(summary)))

    return out


def _make_session() -> aiohttp.ClientSession:
    """Создать ClientSession с дефолтным таймаутом 30 сек."""

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    return aiohttp.ClientSession(timeout=timeout)


async def build_report(
    supported: list[PackageInfo],
    unsupported: list[PackageInfo],
    project_path: str,
    cache: Cache,
) -> FullReport:
    """Построить ``FullReport`` для supported-пакетов через deps.dev.

    Использует ``cache`` для ответа batch (по конкретной версии) и для
    GetPackage (по ключу ``(system, name, "__latest__")``). Сетевые
    ошибки → ``DepsDevError``.
    """

    deduped: dict[tuple[str, str, str], PackageInfo] = {}
    for p in supported:
        system = SUPPORTED_ECOSYSTEMS.get(p.ecosystem)
        if system is None:
            # supported по контракту, так что это не должно случаться.
            continue
        name = _canonical_name(p, system)
        deduped[(system, name, p.version)] = p
    unique_packages = list(deduped.values())

    # что брать из кеша / по сети.
    cached_current: dict[tuple[str, str, str], dict | None] = {}
    need_current: list[PackageInfo] = []

    for p in unique_packages:
        system = SUPPORTED_ECOSYSTEMS[p.ecosystem]
        name = _canonical_name(p, system)
        cached = cache.get(system, name, p.version)
        if cached is not None:
            cached_current[(system, name, p.version)] = cached
        else:
            need_current.append(p)

    # latest: ключ (system, name) — мы дедуплицируем сами.
    cached_latest: dict[tuple[str, str], str | None] = {}
    need_latest_pairs: dict[tuple[str, str], None] = {}

    for p in unique_packages:
        system = SUPPORTED_ECOSYSTEMS[p.ecosystem]
        name = _canonical_name(p, system)
        if (system, name) in cached_latest or (system, name) in need_latest_pairs:
            continue
        cached = cache.get(system, name, _LATEST_KEY)
        if cached is not None:
            cached_latest[(system, name)] = cached.get("latest")
        else:
            need_latest_pairs[(system, name)] = None

    fetched_current: dict[tuple[str, str, str], dict | None] = {}
    fetched_latest: dict[tuple[str, str], str | None] = {}

    if need_current or need_latest_pairs:
        async with _make_session() as session:
            if need_current:
                fetched_current = await fetch_current_versions(session, need_current)
            if need_latest_pairs:
                # для fetch_latest_versions достаточно списка PackageInfo
                # с правильным ecosystem/name; для go fallback намёкa
                # хватает уже сохранённого purl
                stubs: list[PackageInfo] = []
                # Нам нужен ровно один PackageInfo на пару (system, name);
                # берём первый матч из unique_packages.
                seen: set[tuple[str, str]] = set()
                for p in unique_packages:
                    system = SUPPORTED_ECOSYSTEMS[p.ecosystem]
                    name = _canonical_name(p, system)
                    if (system, name) in need_latest_pairs and (system, name) not in seen:
                        stubs.append(p)
                        seen.add((system, name))
                fetched_latest = await fetch_latest_versions(session, stubs)

    # Сохраняем в кеш.
    for key, payload in fetched_current.items():
        if payload is not None:
            cache.set(key[0], key[1], key[2], payload)

    for (system, name), latest in fetched_latest.items():
        cache.set(system, name, _LATEST_KEY, {"latest": latest})

    #  Оборачиваем в DependencyReport.
    all_current: dict[tuple[str, str, str], dict | None] = {
        **cached_current,
        **fetched_current,
    }
    all_latest: dict[tuple[str, str], str | None] = {
        **cached_latest,
        **fetched_latest,
    }

    reports: list[DependencyReport] = []
    for p in supported:
        system = SUPPORTED_ECOSYSTEMS.get(p.ecosystem)
        if system is None:
            continue
        name = _canonical_name(p, system)

        version_payload = all_current.get((system, name, p.version))
        latest = all_latest.get((system, name))

        advisories = _parse_advisories(version_payload)

        if latest is None:
            is_outdated = False
            semver_diff = None
        else:
            if p.version == latest:
                is_outdated = False
                semver_diff = None
            else:
                semver_diff = compute_semver_diff(p.version, latest)
                is_outdated = True

        reports.append(
            DependencyReport(
                name=p.name,  # отчёт показываем как было в исходных данных
                ecosystem=p.ecosystem,
                current_version=p.version,
                latest_version=latest,
                is_outdated=is_outdated,
                semver_diff=semver_diff,
                advisories=advisories,
            )
        )

    # 6. Сводка.
    outdated_count = sum(1 for r in reports if r.is_outdated)
    vulnerable_count = sum(1 for r in reports if r.advisories)
    total_packages = len(supported) + len(unsupported)

    return FullReport(
        supported=reports,
        unsupported=list(unsupported),
        scan_timestamp=datetime.now(timezone.utc),
        project_path=project_path,
        total_packages=total_packages,
        outdated_count=outdated_count,
        vulnerable_count=vulnerable_count,
    )


__all__ = [
    "DepsDevError",
    "DEPSDEV_BATCH_URL",
    "DEPSDEV_PACKAGE_URL_TPL",
    "build_report",
    "fetch_current_versions",
    "fetch_latest_versions",
]
