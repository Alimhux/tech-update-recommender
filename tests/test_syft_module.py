"""Тесты SyftModule (блок 2).

Все тест-кейсы используют заранее подготовленные CycloneDX-фикстуры
в ``tests/fixtures/``. Реальный бинарник syft не запускается:
``find_syft_binary`` и ``run_syft`` тестируются через моки.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from depscope import syft_module
from depscope.models import PackageInfo
from depscope.syft_module import (
    SUPPORTED_ECOSYSTEMS,
    SyftError,
    SyftExecutionError,
    SyftNotFoundError,
    SyftParseError,
    find_syft_binary,
    parse_cyclonedx,
    run_syft,
    scan_project,
    split_supported,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Smoke
# ---------------------------------------------------------------------------


def test_syft_module_importable() -> None:
    """Модуль импортируется (smoke-тест из блока 1, оставлен для совместимости)."""

    assert syft_module is not None


def test_supported_ecosystems_keys() -> None:
    """Константа должна содержать ровно 7 экосистем deps.dev."""

    assert set(SUPPORTED_ECOSYSTEMS) == {
        "npm",
        "pypi",
        "maven",
        "golang",
        "cargo",
        "gem",
        "nuget",
    }


def test_exception_hierarchy() -> None:
    """Все специальные исключения наследуются от ``SyftError``."""

    for exc_cls in (SyftNotFoundError, SyftExecutionError, SyftParseError):
        assert issubclass(exc_cls, SyftError)


# ---------------------------------------------------------------------------
# parse_cyclonedx
# ---------------------------------------------------------------------------


def test_parse_simple() -> None:
    """3 npm-пакета из cyclonedx_simple.json."""

    packages = parse_cyclonedx(FIXTURES / "cyclonedx_simple.json")
    assert len(packages) == 3

    by_name = {p.name: p for p in packages}
    assert set(by_name) == {"express", "lodash", "react"}

    express = by_name["express"]
    assert express.version == "4.18.2"
    assert express.ecosystem == "npm"
    assert express.purl == "pkg:npm/express@4.18.2"


def test_maven_namespace() -> None:
    """Для maven namespace склеивается в имя через двоеточие."""

    packages = parse_cyclonedx(FIXTURES / "cyclonedx_maven.json")
    assert len(packages) == 3

    names = {p.name for p in packages}
    assert names == {
        "org.springframework:spring-core",
        "org.springframework:spring-context",
        "com.fasterxml.jackson.core:jackson-databind",
    }

    spring_core = next(p for p in packages if p.name == "org.springframework:spring-core")
    assert spring_core.ecosystem == "maven"
    assert spring_core.version == "5.3.0"
    assert spring_core.purl == "pkg:maven/org.springframework/spring-core@5.3.0"


def test_empty_components() -> None:
    """Пустой ``components`` парсится в пустой список без ошибок."""

    assert parse_cyclonedx(FIXTURES / "cyclonedx_empty.json") == []


def test_broken_json() -> None:
    """Невалидный JSON → ``SyftParseError``."""

    with pytest.raises(SyftParseError):
        parse_cyclonedx(FIXTURES / "cyclonedx_broken.json")


def test_parse_skips_components_without_purl(tmp_path: Path) -> None:
    """Компоненты без поля ``purl`` пропускаются, остальные обрабатываются."""

    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "components": [
            {"name": "no-purl-here", "version": "1.0.0"},
            {
                "name": "express",
                "version": "4.18.2",
                "purl": "pkg:npm/express@4.18.2",
            },
        ],
    }
    sbom_path = tmp_path / "sbom.json"
    sbom_path.write_text(json.dumps(sbom), encoding="utf-8")

    packages = parse_cyclonedx(sbom_path)
    assert len(packages) == 1
    assert packages[0].name == "express"


# ---------------------------------------------------------------------------
# split_supported
# ---------------------------------------------------------------------------


def test_split_supported_unsupported() -> None:
    """npm/pypi → supported, deb → unsupported."""

    packages = parse_cyclonedx(FIXTURES / "cyclonedx_mixed.json")
    supported, unsupported = split_supported(packages)

    supported_ecos = {p.ecosystem for p in supported}
    unsupported_ecos = {p.ecosystem for p in unsupported}

    assert supported_ecos == {"npm", "pypi"}
    assert unsupported_ecos == {"deb"}

    assert len(supported) == 4  # 2 npm + 2 pypi
    assert len(unsupported) == 2  # 2 deb


def test_dedup() -> None:
    """Повторяющиеся ``(ecosystem, name, version)`` схлопываются."""

    packages = [
        PackageInfo(
            name="express",
            version="4.18.2",
            purl="pkg:npm/express@4.18.2",
            ecosystem="npm",
        ),
        PackageInfo(
            name="express",
            version="4.18.2",
            purl="pkg:npm/express@4.18.2",
            ecosystem="npm",
        ),
        PackageInfo(
            name="express",
            version="4.19.0",
            purl="pkg:npm/express@4.19.0",
            ecosystem="npm",
        ),
        PackageInfo(
            name="libc6",
            version="2.36-9",
            purl="pkg:deb/debian/libc6@2.36-9",
            ecosystem="deb",
        ),
        PackageInfo(
            name="libc6",
            version="2.36-9",
            purl="pkg:deb/debian/libc6@2.36-9",
            ecosystem="deb",
        ),
    ]

    supported, unsupported = split_supported(packages)

    assert len(supported) == 2  # express@4.18.2 и express@4.19.0
    assert {(p.name, p.version) for p in supported} == {
        ("express", "4.18.2"),
        ("express", "4.19.0"),
    }

    assert len(unsupported) == 1
    assert unsupported[0].name == "libc6"


def test_split_preserves_order() -> None:
    """При дедупликации сохраняется порядок первых вхождений."""

    packages = [
        PackageInfo(
            name="b",
            version="1.0.0",
            purl="pkg:npm/b@1.0.0",
            ecosystem="npm",
        ),
        PackageInfo(
            name="a",
            version="1.0.0",
            purl="pkg:npm/a@1.0.0",
            ecosystem="npm",
        ),
        PackageInfo(
            name="b",
            version="1.0.0",
            purl="pkg:npm/b@1.0.0",
            ecosystem="npm",
        ),
    ]
    supported, _ = split_supported(packages)
    assert [p.name for p in supported] == ["b", "a"]


# ---------------------------------------------------------------------------
# find_syft_binary
# ---------------------------------------------------------------------------


def test_syft_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """``shutil.which`` → None и нет ``custom_path`` → ``SyftNotFoundError``."""

    monkeypatch.setattr(syft_module.shutil, "which", lambda _: None)

    with pytest.raises(SyftNotFoundError) as excinfo:
        find_syft_binary(None)

    msg = str(excinfo.value)
    assert "syft не найден" in msg
    assert "brew install syft" in msg
    assert "github.com/anchore/syft/releases" in msg


def test_find_syft_binary_uses_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если syft есть в PATH, ``find_syft_binary(None)`` его возвращает."""

    monkeypatch.setattr(syft_module.shutil, "which", lambda _: "/usr/local/bin/syft")

    assert find_syft_binary(None) == "/usr/local/bin/syft"


def test_find_syft_binary_uses_custom_path(tmp_path: Path) -> None:
    """Если ``custom_path`` валиден — возвращается он, без обращения к PATH."""

    fake_syft = tmp_path / "syft"
    fake_syft.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_syft.chmod(0o755)

    assert find_syft_binary(str(fake_syft)) == str(fake_syft)


def test_find_syft_binary_custom_path_missing(tmp_path: Path) -> None:
    """Несуществующий ``custom_path`` → ``SyftNotFoundError``."""

    missing = tmp_path / "does-not-exist-syft"

    with pytest.raises(SyftNotFoundError):
        find_syft_binary(str(missing))


# ---------------------------------------------------------------------------
# run_syft
# ---------------------------------------------------------------------------


def test_syft_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ненулевой код возврата → ``SyftExecutionError`` с stderr в сообщении."""

    def fake_run(args, **kwargs):  # noqa: ANN001, ANN003
        return subprocess.CompletedProcess(
            args=args,
            returncode=1,
            stdout=None,
            stderr=b"could not access path: permission denied",
        )

    monkeypatch.setattr(syft_module.subprocess, "run", fake_run)

    with pytest.raises(SyftExecutionError) as excinfo:
        run_syft("/tmp/some-project", "/usr/bin/syft")

    msg = str(excinfo.value)
    assert "permission denied" in msg
    assert "1" in msg  # код возврата фигурирует


def test_run_syft_success_writes_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """При успешном запуске возвращается путь к временному файлу с SBOM."""

    captured: dict[str, object] = {}

    def fake_run(args, stdout=None, stderr=None, check=False):  # noqa: ANN001, ANN003
        captured["args"] = args
        # имитируем успешную запись syft в stdout-файл
        if stdout is not None and hasattr(stdout, "write"):
            stdout.write(b'{"bomFormat":"CycloneDX","components":[]}')
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=None, stderr=b"")

    monkeypatch.setattr(syft_module.subprocess, "run", fake_run)

    out_path = run_syft("/tmp/proj", "/usr/bin/syft")
    try:
        assert out_path.exists()
        assert out_path.suffix == ".json"
        content = out_path.read_text(encoding="utf-8")
        assert "CycloneDX" in content
    finally:
        out_path.unlink(missing_ok=True)

    assert captured["args"] == [
        "/usr/bin/syft",
        "dir:/tmp/proj",
        "-o",
        "cyclonedx-json",
    ]


# ---------------------------------------------------------------------------
# scan_project — интеграция всех шагов с мокированием subprocess+which
# ---------------------------------------------------------------------------


def test_scan_project_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Полный pipeline: find → run → parse → split (с моками)."""

    sbom_text = (FIXTURES / "cyclonedx_mixed.json").read_text(encoding="utf-8")

    monkeypatch.setattr(syft_module.shutil, "which", lambda _: "/usr/bin/syft")

    def fake_run(args, stdout=None, stderr=None, check=False):  # noqa: ANN001, ANN003
        if stdout is not None and hasattr(stdout, "write"):
            stdout.write(sbom_text.encode("utf-8"))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=None, stderr=b"")

    monkeypatch.setattr(syft_module.subprocess, "run", fake_run)

    supported, unsupported = scan_project("/tmp/sample-mixed-project")

    assert {p.ecosystem for p in supported} == {"npm", "pypi"}
    assert {p.ecosystem for p in unsupported} == {"deb"}
    assert len(supported) == 4
    assert len(unsupported) == 2


def test_scan_project_propagates_not_found() -> None:
    """Если syft не найден — ``scan_project`` пробрасывает ``SyftNotFoundError``."""

    with patch.object(syft_module.shutil, "which", return_value=None):
        with pytest.raises(SyftNotFoundError):
            scan_project("/tmp/proj")
