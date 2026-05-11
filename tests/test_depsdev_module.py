"""Тесты DepsDevModule.

Все HTTP моки через ``aioresponses``, никаких реальных запросов.
Кеш создаётся в ``tmp_path``, чтобы не трогать ``~/.cache``.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

import aiohttp
import pytest
from aioresponses import aioresponses

from tech_update_recommender import depsdev_module
from tech_update_recommender.cache import _LATEST_KEY, Cache
from tech_update_recommender.depsdev_module import (
    DEPSDEV_BATCH_URL,
    DEPSDEV_PACKAGE_URL_TPL,
    DepsDevError,
    _canonical_name,
    _parse_advisories,
    _pick_latest_version,
    build_report,
    fetch_current_versions,
    fetch_latest_versions,
)
from tech_update_recommender.models import PackageInfo
from tech_update_recommender.utils import (
    compute_semver_diff,
    normalize_pypi_name,
    url_encode_package_name,
)

# помогалки


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict:
    with open(FIXTURES_DIR / name, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def cache(tmp_path: Path) -> Cache:
    return Cache(tmp_path / "cache.db", ttl_seconds=3600)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Меняем _sleep на no-op, чтобы retry-тесты летали мгновенно."""

    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(depsdev_module, "_sleep", _instant)


# compute_semver_diff


@pytest.mark.parametrize(
    "current, latest, expected",
    [
        ("1.2.3", "2.0.0", "major"),
        ("1.2.3", "1.3.0", "minor"),
        ("1.2.3", "1.2.4", "patch"),
        ("1.2.3", "1.2.3", None),
        # невалидная версия (latest=None или мусор) — None
        ("not-a-version", "1.0.0", None),
        ("1.0.0", "garbage", None),
        # maven-стиль (4-сегментные релизы)
        ("5.3.10", "6.0.0", "major"),
        ("5.3.10", "5.3.20", "patch"),
        # pre-release ловится packaging.Version
        ("1.0.0", "1.0.1rc1", "patch"),
    ],
)
def test_compute_semver_diff(current: str, latest: str, expected: str | None) -> None:
    assert compute_semver_diff(current, latest) == expected


# нормализация имён


def test_normalize_pypi_name() -> None:
    assert normalize_pypi_name("Flask-Babel") == "flask-babel"
    assert normalize_pypi_name("Flask_Babel") == "flask-babel"
    assert normalize_pypi_name("requests") == "requests"


def test_url_encode_maven() -> None:
    encoded = url_encode_package_name("MAVEN", "org.springframework:spring-core")
    # ``:`` кодируется, ``.`` — нет (он в unreserved)
    assert "%3A" in encoded
    assert encoded == "org.springframework%3Aspring-core"


def test_url_encode_pypi_normalizes() -> None:
    assert url_encode_package_name("PYPI", "Flask-Babel") == "flask-babel"


def test_canonical_name_golang_uses_namespace() -> None:
    pkg = PackageInfo(
        name="bar",
        version="1.0.0",
        purl="pkg:golang/github.com%2Ffoo/bar@1.0.0",
        ecosystem="golang",
    )
    # PackageURL декодирует namespace, ожидаем "github.com/foo/bar"
    canonical = _canonical_name(pkg, "GO")
    assert canonical == "github.com/foo/bar"


# fetch_current_versions: batch payload и парсинг ответа


async def test_fetch_current_versions_batch() -> None:
    packages = [
        PackageInfo(
            name="express",
            version="4.18.2",
            purl="pkg:npm/express@4.18.2",
            ecosystem="npm",
        ),
        PackageInfo(
            name="Flask-Babel",
            version="2.0.0",
            purl="pkg:pypi/flask-babel@2.0.0",
            ecosystem="pypi",
        ),
    ]

    fixture = _load_fixture("depsdev_batch_response.json")

    with aioresponses() as mocked:
        mocked.post(DEPSDEV_BATCH_URL, payload=fixture)

        async with aiohttp.ClientSession() as session:
            result = await fetch_current_versions(session, packages)

    # в payload должны быть правильные системы и нормализованное имя PyPI
    request_calls = mocked.requests[("POST", _yarl(DEPSDEV_BATCH_URL))]
    assert len(request_calls) == 1
    sent_payload = request_calls[0].kwargs["json"]
    sent_systems = [r["versionKey"]["system"] for r in sent_payload["requests"]]
    sent_names = [r["versionKey"]["name"] for r in sent_payload["requests"]]
    assert sent_systems == ["NPM", "PYPI"]
    # PyPI name должно быть нормализовано до lowercase + dash
    assert "flask-babel" in sent_names
    assert "Flask-Babel" not in sent_names

    # результат содержит ключ для express и flask-babel
    assert ("NPM", "express", "4.18.2") in result
    assert ("PYPI", "flask-babel", "2.0.0") in result


def _yarl(url: str):
    """aioresponses нормализует URL через yarl.URL — повторяем то же."""

    from yarl import URL

    return URL(url)


# fetch_latest_versions: дедупликация, 404, retry


async def test_fetch_latest_versions_dedup() -> None:
    """Два пакета express разных версий: один HTTP-запрос."""

    fixture = _load_fixture("depsdev_getpackage_express.json")

    pkgs = [
        PackageInfo(
            name="express",
            version="4.18.2",
            purl="pkg:npm/express@4.18.2",
            ecosystem="npm",
        ),
        PackageInfo(
            name="express",
            version="4.17.1",
            purl="pkg:npm/express@4.17.1",
            ecosystem="npm",
        ),
    ]

    url = DEPSDEV_PACKAGE_URL_TPL.format(system="NPM", name="express")

    with aioresponses() as mocked:
        mocked.get(url, payload=fixture)

        async with aiohttp.ClientSession() as session:
            result = await fetch_latest_versions(session, pkgs)

    # один запрос на пару (NPM, express)
    calls = mocked.requests[("GET", _yarl(url))]
    assert len(calls) == 1

    # latest берётся по isDefault: true, это 4.21.0
    assert result == {("NPM", "express"): "4.21.0"}


async def test_404_handling() -> None:
    pkgs = [
        PackageInfo(
            name="nonexistent-pkg",
            version="1.0.0",
            purl="pkg:npm/nonexistent-pkg@1.0.0",
            ecosystem="npm",
        )
    ]
    url = DEPSDEV_PACKAGE_URL_TPL.format(system="NPM", name="nonexistent-pkg")

    with aioresponses() as mocked:
        mocked.get(url, status=404, payload=_load_fixture("depsdev_404.json"))

        async with aiohttp.ClientSession() as session:
            result = await fetch_latest_versions(session, pkgs)

    assert result == {("NPM", "nonexistent-pkg"): None}


async def test_retry_on_5xx() -> None:
    """503, 503, 200 — должно отработать успешно."""

    fixture = _load_fixture("depsdev_getpackage_express.json")
    pkgs = [
        PackageInfo(
            name="express",
            version="4.18.2",
            purl="pkg:npm/express@4.18.2",
            ecosystem="npm",
        )
    ]
    url = DEPSDEV_PACKAGE_URL_TPL.format(system="NPM", name="express")

    with aioresponses() as mocked:
        mocked.get(url, status=503)
        mocked.get(url, status=503)
        mocked.get(url, payload=fixture)

        async with aiohttp.ClientSession() as session:
            result = await fetch_latest_versions(session, pkgs)

    assert result == {("NPM", "express"): "4.21.0"}


async def test_retry_exhausted_raises() -> None:
    """Три 503 подряд, ждём DepsDevError."""

    pkgs = [
        PackageInfo(
            name="express",
            version="4.18.2",
            purl="pkg:npm/express@4.18.2",
            ecosystem="npm",
        )
    ]
    url = DEPSDEV_PACKAGE_URL_TPL.format(system="NPM", name="express")

    with aioresponses() as mocked:
        for _ in range(3):
            mocked.get(url, status=503)

        async with aiohttp.ClientSession() as session:
            with pytest.raises(DepsDevError):
                await fetch_latest_versions(session, pkgs)


# кеш


def test_cache_get_set_clear(cache: Cache) -> None:
    assert cache.get("NPM", "express", "4.18.2") is None
    cache.set("NPM", "express", "4.18.2", {"x": 1})
    assert cache.get("NPM", "express", "4.18.2") == {"x": 1}
    cache.clear()
    assert cache.get("NPM", "express", "4.18.2") is None


def test_cache_ttl_expiry(tmp_path: Path) -> None:
    """Если запись старше ttl — get() возвращает None."""

    c = Cache(tmp_path / "ttl.db", ttl_seconds=0)
    c.set("NPM", "express", "4.18.2", {"x": 1})
    # ttl=0, любая запись протухает сразу
    assert c.get("NPM", "express", "4.18.2") is None


async def test_cache_hit_skips_http(cache: Cache, tmp_path: Path) -> None:
    """Если кеш заполнен — build_report не делает HTTP-запросов."""

    pkg = PackageInfo(
        name="express",
        version="4.18.2",
        purl="pkg:npm/express@4.18.2",
        ecosystem="npm",
    )

    # кладём версию и latest в кеш
    cache.set("NPM", "express", "4.18.2", {"versionKey": {"system": "NPM"}})
    cache.set("NPM", "express", _LATEST_KEY, {"latest": "4.21.0"})

    with aioresponses() as mocked:
        # никаких mocked.get/post — если build_report попытается достучаться,
        # aioresponses бросит ConnectionError
        report = await build_report(
            supported=[pkg],
            unsupported=[],
            project_path=str(tmp_path),
            cache=cache,
        )
        # ни одного запроса
        assert mocked.requests == {}

    assert len(report.supported) == 1
    dep = report.supported[0]
    assert dep.latest_version == "4.21.0"
    assert dep.is_outdated is True
    assert dep.semver_diff == "minor"


# парсинг advisories


def test_advisory_parsing_advisorykeys() -> None:
    payload = {
        "advisoryKeys": [
            {"id": "GHSA-rv95-896h-c2vc"},
            "CVE-2024-1234",
        ]
    }
    advisories = _parse_advisories(payload)
    assert len(advisories) == 2
    assert advisories[0].id == "GHSA-rv95-896h-c2vc"
    assert advisories[0].severity == 0.0
    assert advisories[0].summary == ""
    assert advisories[1].id == "CVE-2024-1234"


def test_advisory_parsing_full() -> None:
    payload = {
        "advisories": [
            {
                "id": "GHSA-aaaa-bbbb-cccc",
                "summary": "Bad bug",
                "severity": {"score": 7.5},
            },
            {
                "id": "GHSA-xxxx-yyyy-zzzz",
                "title": "Other bug",
                "severity": 9.1,
            },
        ]
    }
    advisories = _parse_advisories(payload)
    assert len(advisories) == 2
    assert advisories[0].severity == 7.5
    assert advisories[0].summary == "Bad bug"
    assert advisories[1].severity == 9.1
    assert advisories[1].summary == "Other bug"


def test_advisory_parsing_empty() -> None:
    assert _parse_advisories(None) == []
    assert _parse_advisories({}) == []
    assert _parse_advisories({"advisoryKeys": []}) == []


# _pick_latest_version


def test_pick_latest_version_isdefault() -> None:
    payload = _load_fixture("depsdev_getpackage_express.json")
    assert _pick_latest_version(payload) == "4.21.0"


def test_pick_latest_version_publishedat_fallback() -> None:
    payload = {
        "versions": [
            {
                "versionKey": {"version": "1.0.0"},
                "publishedAt": "2020-01-01T00:00:00Z",
            },
            {
                "versionKey": {"version": "2.0.0"},
                "publishedAt": "2022-01-01T00:00:00Z",
            },
        ]
    }
    assert _pick_latest_version(payload) == "2.0.0"


def test_pick_latest_version_empty() -> None:
    assert _pick_latest_version({"versions": []}) is None
    assert _pick_latest_version({}) is None


# build_report — интеграция: batch + GetPackage + summary


async def test_build_report_summary(cache: Cache, tmp_path: Path) -> None:
    """Полный путь build_report со смешанными outdated/vulnerable пакетами."""

    pkgs = [
        # outdated + vulnerable
        PackageInfo(
            name="express",
            version="4.18.2",
            purl="pkg:npm/express@4.18.2",
            ecosystem="npm",
        ),
        # outdated, без CVE
        PackageInfo(
            name="Flask-Babel",
            version="2.0.0",
            purl="pkg:pypi/flask-babel@2.0.0",
            ecosystem="pypi",
        ),
        # up-to-date (latest == current)
        PackageInfo(
            name="org.springframework:spring-core",
            version="6.0.0",
            purl="pkg:maven/org.springframework/spring-core@6.0.0",
            ecosystem="maven",
        ),
    ]
    unsupported = [
        PackageInfo(
            name="libssl",
            version="1.1",
            purl="pkg:deb/debian/libssl@1.1",
            ecosystem="deb",
        )
    ]

    batch_payload = _load_fixture("depsdev_batch_response.json")
    express_pkg = _load_fixture("depsdev_getpackage_express.json")

    flask_pkg = {
        "packageKey": {"system": "PYPI", "name": "flask-babel"},
        "versions": [
            {
                "versionKey": {"system": "PYPI", "name": "flask-babel", "version": "2.0.0"},
                "isDefault": False,
            },
            {
                "versionKey": {"system": "PYPI", "name": "flask-babel", "version": "3.1.0"},
                "isDefault": True,
            },
        ],
    }
    spring_pkg = {
        "packageKey": {"system": "MAVEN", "name": "org.springframework:spring-core"},
        "versions": [
            {
                "versionKey": {
                    "system": "MAVEN",
                    "name": "org.springframework:spring-core",
                    "version": "6.0.0",
                },
                "isDefault": True,
            }
        ],
    }

    with aioresponses() as mocked:
        mocked.post(DEPSDEV_BATCH_URL, payload=batch_payload)
        mocked.get(
            DEPSDEV_PACKAGE_URL_TPL.format(system="NPM", name="express"),
            payload=express_pkg,
        )
        mocked.get(
            DEPSDEV_PACKAGE_URL_TPL.format(system="PYPI", name="flask-babel"),
            payload=flask_pkg,
        )
        encoded_maven = quote("org.springframework:spring-core", safe="")
        mocked.get(
            DEPSDEV_PACKAGE_URL_TPL.format(system="MAVEN", name=encoded_maven),
            payload=spring_pkg,
        )

        report = await build_report(
            supported=pkgs,
            unsupported=unsupported,
            project_path=str(tmp_path),
            cache=cache,
        )

    # сводка
    assert report.total_packages == 4  # 3 supported + 1 unsupported
    assert report.outdated_count == 2  # express, flask-babel
    assert report.vulnerable_count == 1  # у express есть advisoryKeys

    # конкретика
    by_name = {r.name: r for r in report.supported}
    assert by_name["express"].latest_version == "4.21.0"
    assert by_name["express"].is_outdated is True
    assert by_name["express"].semver_diff == "minor"
    assert len(by_name["express"].advisories) == 1
    assert by_name["express"].advisories[0].id == "GHSA-rv95-896h-c2vc"

    assert by_name["Flask-Babel"].latest_version == "3.1.0"
    assert by_name["Flask-Babel"].is_outdated is True
    assert by_name["Flask-Babel"].semver_diff == "major"

    assert by_name["org.springframework:spring-core"].latest_version == "6.0.0"
    assert by_name["org.springframework:spring-core"].is_outdated is False

    assert report.unsupported == unsupported


async def test_build_report_chunks_batch_above_5000(
    cache: Cache, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Если пакетов > BATCH_CHUNK_SIZE — делаем несколько POST-запросов."""

    # понижаем лимит, чтобы тест летал
    monkeypatch.setattr(depsdev_module, "BATCH_CHUNK_SIZE", 2)

    pkgs = [
        PackageInfo(
            name=f"pkg{i}",
            version="1.0.0",
            purl=f"pkg:npm/pkg{i}@1.0.0",
            ecosystem="npm",
        )
        for i in range(5)
    ]

    with aioresponses() as mocked:
        # repeat=True — один mocked response ловит все три запроса
        mocked.post(DEPSDEV_BATCH_URL, payload={"responses": []}, repeat=True)
        # GetPackage: для каждого уникального имени — отдельный URL,
        # все возвращают один и тот же ответ
        for i in range(5):
            mocked.get(
                DEPSDEV_PACKAGE_URL_TPL.format(system="NPM", name=f"pkg{i}"),
                payload={"versions": []},
            )

        async with aiohttp.ClientSession() as session:
            await fetch_current_versions(session, pkgs)

        # 5 пакетов при chunk=2: ceil(5/2) = 3 POST-запроса
        calls = mocked.requests[("POST", _yarl(DEPSDEV_BATCH_URL))]
        assert len(calls) == 3


async def test_build_report_unknown_package_no_latest(cache: Cache, tmp_path: Path) -> None:
    """Пакет, по которому 404: latest_version=None, is_outdated=False."""

    pkg = PackageInfo(
        name="nonexistent",
        version="1.0.0",
        purl="pkg:npm/nonexistent@1.0.0",
        ecosystem="npm",
    )

    with aioresponses() as mocked:
        mocked.post(DEPSDEV_BATCH_URL, payload={"responses": []})
        mocked.get(
            DEPSDEV_PACKAGE_URL_TPL.format(system="NPM", name="nonexistent"),
            status=404,
            payload=_load_fixture("depsdev_404.json"),
        )

        report = await build_report(
            supported=[pkg],
            unsupported=[],
            project_path=str(tmp_path),
            cache=cache,
        )

    assert len(report.supported) == 1
    dep = report.supported[0]
    assert dep.latest_version is None
    assert dep.is_outdated is False
    assert dep.semver_diff is None
    assert dep.advisories == []


async def test_build_report_invalid_version_string(cache: Cache, tmp_path: Path) -> None:
    """Невалидная (не-SemVer) текущая версия: semver_diff=None,
    is_outdated=True (потому что строки не равны)."""

    pkg = PackageInfo(
        name="weird-pkg",
        version="not-a-version",
        purl="pkg:npm/weird-pkg@not-a-version",
        ecosystem="npm",
    )

    latest_payload = {
        "versions": [
            {
                "versionKey": {
                    "system": "NPM",
                    "name": "weird-pkg",
                    "version": "1.2.3",
                },
                "isDefault": True,
            }
        ]
    }

    with aioresponses() as mocked:
        mocked.post(DEPSDEV_BATCH_URL, payload={"responses": []})
        mocked.get(
            DEPSDEV_PACKAGE_URL_TPL.format(system="NPM", name="weird-pkg"),
            payload=latest_payload,
        )

        report = await build_report(
            supported=[pkg],
            unsupported=[],
            project_path=str(tmp_path),
            cache=cache,
        )

    dep = report.supported[0]
    assert dep.latest_version == "1.2.3"
    assert dep.is_outdated is True
    assert dep.semver_diff is None
