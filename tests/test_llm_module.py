"""Тесты LLMModule (Блок 5).

Все вызовы ``litellm.completion`` мокаются — реальных сетевых вызовов
ни один тест не делает. Конструируем фейковые провайдерские ошибки
и проверяем маппинг в наши классы.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from depscope import llm_module
from depscope.llm_module import (
    SYSTEM_PROMPT,
    LLMAuthError,
    LLMContextOverflowError,
    LLMNetworkError,
    LLMNotAvailableError,
    LLMRateLimitError,
    build_llm_input,
    build_user_prompt,
    collect_dependency_files,
    collect_project_tree,
    count_tokens,
    generate_advice,
    truncate_input,
)
from depscope.models import (
    Advisory,
    DependencyReport,
    FullReport,
    LLMInput,
)

# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------


def _make_dep(
    name: str,
    *,
    is_outdated: bool = True,
    semver_diff: str | None = "patch",
    advisories: int = 0,
) -> DependencyReport:
    return DependencyReport(
        name=name,
        ecosystem="npm",
        current_version="1.0.0",
        latest_version="1.0.1",
        is_outdated=is_outdated,
        semver_diff=semver_diff,
        advisories=[
            Advisory(id=f"GHSA-{name}-{i}", severity=7.5, summary="x") for i in range(advisories)
        ],
    )


def _make_report(deps: list[DependencyReport]) -> FullReport:
    return FullReport(
        supported=deps,
        unsupported=[],
        scan_timestamp=datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc),
        project_path="/tmp/x",
        total_packages=len(deps),
        outdated_count=sum(1 for d in deps if d.is_outdated),
        vulnerable_count=sum(1 for d in deps if d.advisories),
    )


def _make_completion_response(content: str = "ok") -> SimpleNamespace:
    """Имитация ответа litellm: ``response.choices[0].message.content``."""

    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


# ---------------------------------------------------------------------------
# 1. collect_project_tree
# ---------------------------------------------------------------------------


def test_collect_project_tree_excludes_node_modules(tmp_path):
    # Структура проекта: один валидный файл + мусор в исключаемых каталогах.
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lodash.js").write_text("//")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]")
    (tmp_path / "venv").mkdir()
    (tmp_path / "venv" / "pyvenv.cfg").write_text("home = /usr/bin")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "out.js").write_text("//")
    # Вложенный node_modules — должен тоже отфильтроваться.
    (tmp_path / "src" / "node_modules").mkdir()
    (tmp_path / "src" / "node_modules" / "left-pad.js").write_text("//")

    out = collect_project_tree(str(tmp_path))

    assert "app.py" in out
    assert "node_modules" not in out
    assert ".git" not in out
    assert "venv" not in out
    assert "build/out.js" not in out
    assert "left-pad.js" not in out


def test_collect_project_tree_max_lines(tmp_path):
    # 50 файлов, max_lines=10 → должны увидеть пометку про усечение.
    for i in range(50):
        (tmp_path / f"f_{i:03d}.txt").write_text("x")

    out = collect_project_tree(str(tmp_path), max_lines=10)
    lines = out.splitlines()

    # 10 строк с файлами + 1 строка-маркер усечения.
    assert len(lines) == 11
    assert lines[-1].startswith("... (truncated,")
    assert "40 more files" in lines[-1]


def test_collect_project_tree_empty_for_missing_dir(tmp_path):
    missing = tmp_path / "does_not_exist"
    assert collect_project_tree(str(missing)) == ""


# ---------------------------------------------------------------------------
# 2. collect_dependency_files
# ---------------------------------------------------------------------------


def test_collect_dependency_files_known_set(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask==2.0.0\n")
    (tmp_path / "package.json").write_text('{"name":"x"}')
    (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\n')
    # Под-каталог в пределах глубины 3.
    sub = tmp_path / "service"
    sub.mkdir()
    (sub / "go.mod").write_text("module x\n")
    # Glob-паттерны.
    (tmp_path / "requirements-dev.txt").write_text("pytest\n")
    (tmp_path / "App.csproj").write_text("<Project/>")
    # Мусор не должен попасть.
    (tmp_path / "README.md").write_text("# hi")
    # node_modules с package.json внутри — не попадает.
    nm = tmp_path / "node_modules" / "express"
    nm.mkdir(parents=True)
    (nm / "package.json").write_text('{"name":"express"}')

    files = collect_dependency_files(str(tmp_path))

    assert "requirements.txt" in files
    assert files["requirements.txt"].startswith("flask==")
    assert "package.json" in files
    assert "pyproject.toml" in files
    assert "service/go.mod" in files
    assert "requirements-dev.txt" in files
    assert "App.csproj" in files
    assert "README.md" not in files
    # Вложенный package.json в node_modules — должен быть пропущен.
    assert all("node_modules" not in path for path in files.keys())


def test_collect_dependency_files_skips_large(tmp_path, caplog):
    # Файл ~250 KB должен быть пропущен (>200 KB лимит).
    big = tmp_path / "package-lock.json"
    big.write_text("a" * (250 * 1024))
    small = tmp_path / "package.json"
    small.write_text('{"name":"x"}')

    with caplog.at_level(logging.DEBUG, logger="depscope.llm_module"):
        files = collect_dependency_files(str(tmp_path))

    assert "package.json" in files
    assert "package-lock.json" not in files
    # Лог DEBUG должен содержать сообщение о пропуске.
    assert any("skip large file" in rec.getMessage() for rec in caplog.records)


def test_collect_dependency_files_empty_for_missing_dir(tmp_path):
    missing = tmp_path / "does_not_exist"
    assert collect_dependency_files(str(missing)) == {}


# ---------------------------------------------------------------------------
# 3. build_llm_input + приоритизация
# ---------------------------------------------------------------------------


def test_build_llm_input_top_n(tmp_path):
    # 300 outdated пакетов: должны увидеть top-50.
    deps = [_make_dep(f"pkg_{i:03d}", is_outdated=True, semver_diff="patch") for i in range(300)]
    report = _make_report(deps)
    llm_input = build_llm_input(report, str(tmp_path))

    assert len(llm_input.report.supported) == 50
    # Счётчики оригинала сохраняются, чтобы LLM понимал масштаб.
    assert llm_input.report.total_packages == 300
    assert llm_input.report.outdated_count == 300


def test_build_llm_input_filters_only_outdated_or_vulnerable(tmp_path):
    deps = [
        _make_dep("up_to_date", is_outdated=False, semver_diff=None),
        _make_dep("outdated", is_outdated=True, semver_diff="patch"),
        _make_dep(
            "secure_but_old",
            is_outdated=False,
            semver_diff=None,
            advisories=1,
        ),
    ]
    report = _make_report(deps)

    llm_input = build_llm_input(report, str(tmp_path))
    names = {d.name for d in llm_input.report.supported}
    assert names == {"outdated", "secure_but_old"}


def test_priority_ordering(tmp_path):
    # Пакеты с advisory должны идти раньше пакетов без advisory.
    deps = [
        _make_dep("plain_minor", is_outdated=True, semver_diff="minor"),
        _make_dep("plain_major", is_outdated=True, semver_diff="major"),
        _make_dep("with_cve", is_outdated=True, semver_diff="patch", advisories=2),
        _make_dep("plain_patch", is_outdated=True, semver_diff="patch"),
    ]
    report = _make_report(deps)
    llm_input = build_llm_input(report, str(tmp_path))
    ordered = [d.name for d in llm_input.report.supported]
    assert ordered[0] == "with_cve"


def test_top_n_priority_cve_before_major(tmp_path):
    """CVE без semver_diff важнее любого major без advisory."""

    deps = [
        _make_dep("major_no_cve", is_outdated=True, semver_diff="major"),
        _make_dep("cve_only", is_outdated=False, semver_diff=None, advisories=1),
    ]
    report = _make_report(deps)
    llm_input = build_llm_input(report, str(tmp_path))
    names = [d.name for d in llm_input.report.supported]
    assert names[0] == "cve_only"
    assert names[1] == "major_no_cve"


# ---------------------------------------------------------------------------
# 4. truncate_input — усечение по бюджету токенов
# ---------------------------------------------------------------------------


def _make_long_input() -> LLMInput:
    """LLMInput с заведомо большим деревом и dependency-файлами."""

    deps = [_make_dep(f"pkg_{i:04d}", advisories=0) for i in range(300)]
    report = _make_report(deps)
    partial = llm_module._build_partial_report(report, llm_module._TOP_N_FULL)

    big_tree = "\n".join(f"src/file_{i:04d}.py" for i in range(500))
    big_files = {
        "package-lock.json": "lock-line\n" * 5000,
        "requirements.txt": "flask==2.0.0\n",
    }
    return LLMInput(
        report=partial,
        project_tree=big_tree,
        dependency_files=big_files,
    )


def test_context_truncation_returns_smaller_input():
    big = _make_long_input()
    # Подбираем лимит так, чтобы исходный промпт не влез, но после усечения
    # дерева/dep-файлов/top-N — точно вошёл.
    truncated = truncate_input(big, model="gemini/gemini-2.0-flash", max_context_tokens=4000)

    # truncate_input должен уменьшить хотя бы что-то.
    smaller = (
        len(truncated.project_tree) < len(big.project_tree)
        or len(truncated.dependency_files.get("package-lock.json", ""))
        < len(big.dependency_files["package-lock.json"])
        or len(truncated.report.supported) < len(big.report.supported)
    )
    assert smaller, "truncate_input должен уменьшить input"


def test_context_truncation_overflow_raises():
    big = _make_long_input()
    # Лимит = 1 токен — ни одна стратегия не поможет.
    with pytest.raises(LLMContextOverflowError):
        truncate_input(big, model="gemini/gemini-2.0-flash", max_context_tokens=1)


def test_truncate_input_passthrough_when_fits():
    deps = [_make_dep("pkg_a")]
    report = _make_report(deps)
    partial = llm_module._build_partial_report(report, llm_module._TOP_N_FULL)
    small = LLMInput(report=partial, project_tree="src/a.py", dependency_files={})

    out = truncate_input(small, model="gemini/gemini-2.0-flash", max_context_tokens=8000)
    # Промпт маленький — usecase не должен ничего переписать.
    assert out.project_tree == small.project_tree
    assert out.report.supported == small.report.supported


# ---------------------------------------------------------------------------
# 5. count_tokens — fallback
# ---------------------------------------------------------------------------


def test_count_tokens_fallback_when_no_litellm(monkeypatch):
    monkeypatch.setitem(sys.modules, "litellm", None)
    # 8 символов → ровно 2 токена в fallback'е.
    assert count_tokens("any-model", "abcdefgh") == 2


def test_count_tokens_uses_litellm_when_available(monkeypatch):
    fake = MagicMock()
    fake.token_counter.return_value = 42
    monkeypatch.setitem(sys.modules, "litellm", fake)

    assert count_tokens("gpt-x", "hello") == 42
    fake.token_counter.assert_called_once_with(model="gpt-x", text="hello")


def test_count_tokens_handles_litellm_exception(monkeypatch):
    fake = MagicMock()
    fake.token_counter.side_effect = RuntimeError("unknown model")
    monkeypatch.setitem(sys.modules, "litellm", fake)

    # Падение token_counter → fallback len // 4.
    assert count_tokens("weird-model", "abcdefgh") == 2


# ---------------------------------------------------------------------------
# 6. generate_advice — happy path и ошибки
# ---------------------------------------------------------------------------


def _input_for_call() -> LLMInput:
    deps = [_make_dep("flask", advisories=1)]
    report = _make_report(deps)
    partial = llm_module._build_partial_report(report, llm_module._TOP_N_FULL)
    return LLMInput(
        report=partial,
        project_tree="src/app.py",
        dependency_files={"requirements.txt": "flask==2.0.0\n"},
    )


def test_generate_advice_happy_path():
    fake_litellm = MagicMock()
    fake_litellm.completion.return_value = _make_completion_response(
        "## 🔴 Критичные обновления\n- flask"
    )
    # Атрибут token_counter поведём через fallback (его нет на mock с auth-классами).
    fake_litellm.token_counter.side_effect = RuntimeError("nope")
    fake_litellm.AuthenticationError = type("AuthenticationError", (Exception,), {})
    fake_litellm.RateLimitError = type("RateLimitError", (Exception,), {})
    fake_litellm.APIConnectionError = type("APIConnectionError", (Exception,), {})
    fake_litellm.Timeout = type("Timeout", (Exception,), {})
    fake_litellm.BadRequestError = type("BadRequestError", (Exception,), {})

    with patch.dict(sys.modules, {"litellm": fake_litellm}):
        result = generate_advice(
            _input_for_call(),
            model="gemini/gemini-2.0-flash",
            api_key="secret-key",
        )

    assert "flask" in result
    fake_litellm.completion.assert_called_once()
    kwargs = fake_litellm.completion.call_args.kwargs
    assert kwargs["model"] == "gemini/gemini-2.0-flash"
    assert kwargs["api_key"] == "secret-key"
    # Системный промпт должен совпадать с эталоном.
    assert kwargs["messages"][0]["content"] == SYSTEM_PROMPT
    assert kwargs["messages"][0]["role"] == "system"
    assert kwargs["messages"][1]["role"] == "user"


def test_litellm_not_installed(monkeypatch):
    # sys.modules["litellm"] = None имитирует "не установлен".
    monkeypatch.setitem(sys.modules, "litellm", None)
    with pytest.raises(LLMNotAvailableError) as exc:
        generate_advice(
            _input_for_call(),
            model="gemini/gemini-2.0-flash",
            api_key=None,
        )
    assert "pip install depscope[llm]" in str(exc.value)


def test_auth_error_mapped(monkeypatch):
    auth_cls = type("AuthenticationError", (Exception,), {})
    fake_litellm = MagicMock()
    fake_litellm.AuthenticationError = auth_cls
    fake_litellm.RateLimitError = type("RateLimitError", (Exception,), {})
    fake_litellm.APIConnectionError = type("APIConnectionError", (Exception,), {})
    fake_litellm.Timeout = type("Timeout", (Exception,), {})
    fake_litellm.BadRequestError = type("BadRequestError", (Exception,), {})
    fake_litellm.token_counter.side_effect = RuntimeError("nope")
    fake_litellm.completion.side_effect = auth_cls("bad key")

    with patch.dict(sys.modules, {"litellm": fake_litellm}):
        with pytest.raises(LLMAuthError) as exc:
            generate_advice(
                _input_for_call(),
                model="gemini/gemini-2.0-flash",
                api_key="bad",
            )
    assert "API-ключ" in str(exc.value)


def test_rate_limit_retries_then_maps(monkeypatch):
    rate_cls = type("RateLimitError", (Exception,), {})
    fake_litellm = MagicMock()
    fake_litellm.AuthenticationError = type("AuthenticationError", (Exception,), {})
    fake_litellm.RateLimitError = rate_cls
    fake_litellm.APIConnectionError = type("APIConnectionError", (Exception,), {})
    fake_litellm.Timeout = type("Timeout", (Exception,), {})
    fake_litellm.BadRequestError = type("BadRequestError", (Exception,), {})
    fake_litellm.token_counter.side_effect = RuntimeError("nope")
    fake_litellm.completion.side_effect = [rate_cls("slow down"), rate_cls("still")]

    sleep_mock = MagicMock()
    monkeypatch.setattr("depscope.llm_module.time.sleep", sleep_mock)

    with patch.dict(sys.modules, {"litellm": fake_litellm}):
        with pytest.raises(LLMRateLimitError):
            generate_advice(
                _input_for_call(),
                model="gemini/gemini-2.0-flash",
                api_key="k",
            )

    # Один retry → одно ожидание в 5 секунд.
    sleep_mock.assert_called_once_with(5)
    assert fake_litellm.completion.call_count == 2


def test_rate_limit_retry_succeeds(monkeypatch):
    rate_cls = type("RateLimitError", (Exception,), {})
    fake_litellm = MagicMock()
    fake_litellm.AuthenticationError = type("AuthenticationError", (Exception,), {})
    fake_litellm.RateLimitError = rate_cls
    fake_litellm.APIConnectionError = type("APIConnectionError", (Exception,), {})
    fake_litellm.Timeout = type("Timeout", (Exception,), {})
    fake_litellm.BadRequestError = type("BadRequestError", (Exception,), {})
    fake_litellm.token_counter.side_effect = RuntimeError("nope")
    fake_litellm.completion.side_effect = [
        rate_cls("slow down"),
        _make_completion_response("recovered"),
    ]

    monkeypatch.setattr("depscope.llm_module.time.sleep", MagicMock())

    with patch.dict(sys.modules, {"litellm": fake_litellm}):
        result = generate_advice(
            _input_for_call(),
            model="gemini/gemini-2.0-flash",
            api_key="k",
        )
    assert result == "recovered"


def test_network_error_mapped(monkeypatch):
    timeout_cls = type("Timeout", (Exception,), {})
    fake_litellm = MagicMock()
    fake_litellm.AuthenticationError = type("AuthenticationError", (Exception,), {})
    fake_litellm.RateLimitError = type("RateLimitError", (Exception,), {})
    fake_litellm.APIConnectionError = type("APIConnectionError", (Exception,), {})
    fake_litellm.Timeout = timeout_cls
    fake_litellm.BadRequestError = type("BadRequestError", (Exception,), {})
    fake_litellm.token_counter.side_effect = RuntimeError("nope")
    fake_litellm.completion.side_effect = timeout_cls("timeout")

    with patch.dict(sys.modules, {"litellm": fake_litellm}):
        with pytest.raises(LLMNetworkError):
            generate_advice(
                _input_for_call(),
                model="gemini/gemini-2.0-flash",
                api_key="k",
            )


def test_local_model_no_api_key_required():
    # Для ollama-моделей мы не должны падать на отсутствии ключа.
    fake_litellm = MagicMock()
    fake_litellm.completion.return_value = _make_completion_response("local advice")
    fake_litellm.token_counter.side_effect = RuntimeError("nope")
    fake_litellm.AuthenticationError = type("AuthenticationError", (Exception,), {})
    fake_litellm.RateLimitError = type("RateLimitError", (Exception,), {})
    fake_litellm.APIConnectionError = type("APIConnectionError", (Exception,), {})
    fake_litellm.Timeout = type("Timeout", (Exception,), {})
    fake_litellm.BadRequestError = type("BadRequestError", (Exception,), {})

    with patch.dict(sys.modules, {"litellm": fake_litellm}):
        result = generate_advice(
            _input_for_call(),
            model="ollama/llama3",
            api_key=None,
        )
    assert result == "local advice"
    # api_key=None прокидывается прямо — litellm.completion сам разберётся.
    kwargs = fake_litellm.completion.call_args.kwargs
    assert kwargs["api_key"] is None


def test_api_key_not_logged(caplog):
    fake_litellm = MagicMock()
    fake_litellm.completion.return_value = _make_completion_response("advice text")
    fake_litellm.token_counter.side_effect = RuntimeError("nope")
    fake_litellm.AuthenticationError = type("AuthenticationError", (Exception,), {})
    fake_litellm.RateLimitError = type("RateLimitError", (Exception,), {})
    fake_litellm.APIConnectionError = type("APIConnectionError", (Exception,), {})
    fake_litellm.Timeout = type("Timeout", (Exception,), {})
    fake_litellm.BadRequestError = type("BadRequestError", (Exception,), {})

    secret = "sk-super-secret-1234567890"

    caplog.set_level(logging.DEBUG, logger="depscope.llm_module")
    with patch.dict(sys.modules, {"litellm": fake_litellm}):
        generate_advice(
            _input_for_call(),
            model="gemini/gemini-2.0-flash",
            api_key=secret,
        )

    # Ключ не должен утечь ни в одно сообщение лога.
    for rec in caplog.records:
        assert secret not in rec.getMessage()
        # И в args тоже (если кто-то когда-нибудь начнёт писать %r).
        for a in rec.args or ():
            assert secret not in str(a)


# ---------------------------------------------------------------------------
# 7. user prompt
# ---------------------------------------------------------------------------


def test_build_user_prompt_contains_sections():
    llm_input = _input_for_call()
    prompt = build_user_prompt(llm_input)
    assert "Отчёт об устаревших и уязвимых зависимостях:" in prompt
    assert "Структура проекта:" in prompt
    assert "Файлы зависимостей:" in prompt
    assert "=== requirements.txt ===" in prompt
    assert "Сформируй рекомендации в указанном формате." in prompt
