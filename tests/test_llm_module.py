"""Тесты для LLM-модуля.

Всё мокается, в сеть не ходим (иначе тесты падают без интернета).
Берём фейковые исключения от litellm и смотрим, что наш код их ловит.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tech_update_recommender import llm_module
from tech_update_recommender.llm_module import (
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
from tech_update_recommender.models import (
    Advisory,
    DependencyReport,
    FullReport,
    LLMInput,
)

# маленькие фабрики, чтобы не плодить копипасту в каждом тесте


def _make_dep(
    name: str,
    *,
    is_outdated: bool = True,
    semver_diff: str | None = "patch",
    advisories: int = 0,
) -> DependencyReport:
    # один пакет для теста (по умолчанию устаревший)
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
    # обёртка над списком зависимостей, чтобы получить FullReport
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
    # litellm возвращает объект с choices[0].message.content — повторяем его
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


# 1) collect_project_tree


def test_collect_project_tree_excludes_node_modules(tmp_path):
    # нормальный файл — должен остаться
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')")
    # папки, которые надо игнорить
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
    # отдельный кейс: node_modules внутри src — тоже мимо
    (tmp_path / "src" / "node_modules").mkdir()
    (tmp_path / "src" / "node_modules" / "left-pad.js").write_text("//")

    out = collect_project_tree(str(tmp_path))

    # хороший файл на месте
    assert "app.py" in out
    # всё лишнее отвалилось
    assert "node_modules" not in out
    assert ".git" not in out
    assert "venv" not in out
    assert "build/out.js" not in out
    assert "left-pad.js" not in out


def test_collect_project_tree_max_lines(tmp_path):
    # 50 файлов, лимит 10 — должна быть пометка про обрезание
    for i in range(50):
        (tmp_path / f"f_{i:03d}.txt").write_text("x")

    out = collect_project_tree(str(tmp_path), max_lines=10)
    lines = out.splitlines()

    # 10 файлов + 1 строка "... обрезано" = 11
    assert len(lines) == 11
    assert lines[-1].startswith("... (truncated,")
    assert "40 more files" in lines[-1]


def test_collect_project_tree_empty_for_missing_dir(tmp_path):
    # папки нет — пустая строка, без падений
    missing = tmp_path / "does_not_exist"
    assert collect_project_tree(str(missing)) == ""


# 2) collect_dependency_files


def test_collect_dependency_files_known_set(tmp_path):
    # раскидываем разные манифесты
    (tmp_path / "requirements.txt").write_text("flask==2.0.0\n")
    (tmp_path / "package.json").write_text('{"name":"x"}')
    (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\n')
    # положу глубже одну штуку — глубина 3 разрешена, должен найти
    sub = tmp_path / "service"
    sub.mkdir()
    (sub / "go.mod").write_text("module x\n")
    # это уже через glob-паттерны должно ловиться
    (tmp_path / "requirements-dev.txt").write_text("pytest\n")
    (tmp_path / "App.csproj").write_text("<Project/>")
    # README не нужен
    (tmp_path / "README.md").write_text("# hi")
    # ловушка: package.json внутри node_modules — игнорим
    nm = tmp_path / "node_modules" / "express"
    nm.mkdir(parents=True)
    (nm / "package.json").write_text('{"name":"express"}')

    files = collect_dependency_files(str(tmp_path))

    # всё, что должно быть — есть
    assert "requirements.txt" in files
    assert files["requirements.txt"].startswith("flask==")
    assert "package.json" in files
    assert "pyproject.toml" in files
    assert "service/go.mod" in files
    assert "requirements-dev.txt" in files
    assert "App.csproj" in files
    # README не попал
    assert "README.md" not in files
    # из node_modules ничего не пролезло
    assert all("node_modules" not in path for path in files.keys())


def test_collect_dependency_files_skips_large(tmp_path, caplog):
    # большой lock-файл (250 KB) — пропускаем, лимит 200 KB
    big = tmp_path / "package-lock.json"
    big.write_text("a" * (250 * 1024))
    # маленький package.json пусть остаётся
    small = tmp_path / "package.json"
    small.write_text('{"name":"x"}')

    with caplog.at_level(logging.DEBUG, logger="tech_update_recommender.llm_module"):
        files = collect_dependency_files(str(tmp_path))

    assert "package.json" in files
    assert "package-lock.json" not in files
    # в DEBUG-логи должно попасть сообщение про пропуск
    assert any("skip large file" in rec.getMessage() for rec in caplog.records)


def test_collect_dependency_files_empty_for_missing_dir(tmp_path):
    # папки нет, возвращается пустой dict, никаких ошибок
    missing = tmp_path / "does_not_exist"
    assert collect_dependency_files(str(missing)) == {}


# 3) build_llm_input — фильтры и сортировка


def test_build_llm_input_top_n(tmp_path):
    # 300 устаревших пакетов, в LLM должны пойти только топ-50
    deps = [_make_dep(f"pkg_{i:03d}", is_outdated=True, semver_diff="patch") for i in range(300)]
    report = _make_report(deps)
    llm_input = build_llm_input(report, str(tmp_path))

    assert len(llm_input.report.supported) == 50
    # счётчики должны остаться как было — LLM видит реальный масштаб
    assert llm_input.report.total_packages == 300
    assert llm_input.report.outdated_count == 300


def test_build_llm_input_filters_only_outdated_or_vulnerable(tmp_path):
    # три случая: свежий, устаревший, "новый, но с дырой"
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
    # up_to_date — без CVE и не устаревший, пропускаем
    assert names == {"outdated", "secure_but_old"}


def test_priority_ordering(tmp_path):
    # если у пакета есть CVE — он идёт первым
    deps = [
        _make_dep("plain_minor", is_outdated=True, semver_diff="minor"),
        _make_dep("plain_major", is_outdated=True, semver_diff="major"),
        _make_dep("with_cve", is_outdated=True, semver_diff="patch", advisories=2),
        _make_dep("plain_patch", is_outdated=True, semver_diff="patch"),
    ]
    report = _make_report(deps)
    llm_input = build_llm_input(report, str(tmp_path))
    ordered = [d.name for d in llm_input.report.supported]
    # самый "опасный" пакет — первый
    assert ordered[0] == "with_cve"


def test_top_n_priority_cve_before_major(tmp_path):
    # CVE без обновления > major без CVE
    deps = [
        _make_dep("major_no_cve", is_outdated=True, semver_diff="major"),
        _make_dep("cve_only", is_outdated=False, semver_diff=None, advisories=1),
    ]
    report = _make_report(deps)
    llm_input = build_llm_input(report, str(tmp_path))
    names = [d.name for d in llm_input.report.supported]
    # сначала тот, у кого CVE
    assert names[0] == "cve_only"
    assert names[1] == "major_no_cve"


# 4) truncate_input — урезание по токенам


def _make_long_input() -> LLMInput:
    # большой инпут: куча файлов в дереве + жирный lock-файл
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
    # 4000 токенов — целое дерево не влезет, но после обрезки должно поместиться
    truncated = truncate_input(big, model="gemini/gemini-2.0-flash", max_context_tokens=4000)

    # хоть что-то должно стать меньше — дерево, lock-файл или список пакетов
    smaller = (
        len(truncated.project_tree) < len(big.project_tree)
        or len(truncated.dependency_files.get("package-lock.json", ""))
        < len(big.dependency_files["package-lock.json"])
        or len(truncated.report.supported) < len(big.report.supported)
    )
    assert smaller, "truncate_input должен уменьшить input"


def test_context_truncation_overflow_raises():
    big = _make_long_input()
    # 1 токен — сколько ни режь, не влезет
    with pytest.raises(LLMContextOverflowError):
        truncate_input(big, model="gemini/gemini-2.0-flash", max_context_tokens=1)


def test_truncate_input_passthrough_when_fits():
    # маленький инпут — и так помещается, ничего не трогаем
    deps = [_make_dep("pkg_a")]
    report = _make_report(deps)
    partial = llm_module._build_partial_report(report, llm_module._TOP_N_FULL)
    small = LLMInput(report=partial, project_tree="src/a.py", dependency_files={})

    out = truncate_input(small, model="gemini/gemini-2.0-flash", max_context_tokens=8000)
    # вышло один в один то, что положили
    assert out.project_tree == small.project_tree
    assert out.report.supported == small.report.supported


# 5) count_tokens — что будет если litellm нет / упал


def test_count_tokens_fallback_when_no_litellm(monkeypatch):
    # эмулируем отсутствие litellm
    monkeypatch.setitem(sys.modules, "litellm", None)
    # fallback грубый: длина / 4. У нас 8 символов, ждём 2 токена
    assert count_tokens("any-model", "abcdefgh") == 2


def test_count_tokens_uses_litellm_when_available(monkeypatch):
    # подсовываем фейковый litellm со своим token_counter
    fake = MagicMock()
    fake.token_counter.return_value = 42
    monkeypatch.setitem(sys.modules, "litellm", fake)

    assert count_tokens("gpt-x", "hello") == 42
    # заодно проверим, что дёрнули правильно
    fake.token_counter.assert_called_once_with(model="gpt-x", text="hello")


def test_count_tokens_handles_litellm_exception(monkeypatch):
    # если litellm.token_counter упал — мягко уходим на fallback
    fake = MagicMock()
    fake.token_counter.side_effect = RuntimeError("unknown model")
    monkeypatch.setitem(sys.modules, "litellm", fake)

    # снова len // 4 = 2
    assert count_tokens("weird-model", "abcdefgh") == 2


# 6) generate_advice. Тут самое интересное: happy path + ошибки


def _input_for_call() -> LLMInput:
    # маленький готовый LLMInput
    deps = [_make_dep("flask", advisories=1)]
    report = _make_report(deps)
    partial = llm_module._build_partial_report(report, llm_module._TOP_N_FULL)
    return LLMInput(
        report=partial,
        project_tree="src/app.py",
        dependency_files={"requirements.txt": "flask==2.0.0\n"},
    )


def test_generate_advice_happy_path():
    # базовый сценарий — litellm работает, отдаёт ответ
    fake_litellm = MagicMock()
    fake_litellm.completion.return_value = _make_completion_response(
        "## 🔴 Критичные обновления\n- flask"
    )
    # token_counter роняем — пусть идёт fallback
    fake_litellm.token_counter.side_effect = RuntimeError("nope")
    # фейковые классы ошибок — код сравнивает по isinstance
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

    # ответ модели пробрасывается как есть
    assert "flask" in result
    fake_litellm.completion.assert_called_once()
    kwargs = fake_litellm.completion.call_args.kwargs
    # параметры тоже на месте
    assert kwargs["model"] == "gemini/gemini-2.0-flash"
    assert kwargs["api_key"] == "secret-key"
    # system-промпт тот самый, который экспортируем
    assert kwargs["messages"][0]["content"] == SYSTEM_PROMPT
    assert kwargs["messages"][0]["role"] == "system"
    assert kwargs["messages"][1]["role"] == "user"


def test_litellm_not_installed(monkeypatch):
    # litellm нет вообще — понятная ошибка с инструкцией
    monkeypatch.setitem(sys.modules, "litellm", None)
    with pytest.raises(LLMNotAvailableError) as exc:
        generate_advice(
            _input_for_call(),
            model="gemini/gemini-2.0-flash",
            api_key=None,
        )
    # в сообщении подсказка как поставить
    assert "pip install tech-upd-recommender" in str(exc.value)


def test_auth_error_mapped(monkeypatch):
    # кривой ключ: litellm кидает AuthenticationError, а у нас должен быть LLMAuthError
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
    # в сообщении должно быть про API-ключ
    assert "API-ключ" in str(exc.value)


def test_rate_limit_retries_then_maps(monkeypatch):
    # rate limit два раза подряд: должен попробовать ещё раз и сдаться
    rate_cls = type("RateLimitError", (Exception,), {})
    fake_litellm = MagicMock()
    fake_litellm.AuthenticationError = type("AuthenticationError", (Exception,), {})
    fake_litellm.RateLimitError = rate_cls
    fake_litellm.APIConnectionError = type("APIConnectionError", (Exception,), {})
    fake_litellm.Timeout = type("Timeout", (Exception,), {})
    fake_litellm.BadRequestError = type("BadRequestError", (Exception,), {})
    fake_litellm.token_counter.side_effect = RuntimeError("nope")
    # два rate-limit подряд: первый и retry
    fake_litellm.completion.side_effect = [rate_cls("slow down"), rate_cls("still")]

    # мокаем sleep — иначе тест будет тормозить
    sleep_mock = MagicMock()
    monkeypatch.setattr("tech_update_recommender.llm_module.time.sleep", sleep_mock)

    with patch.dict(sys.modules, {"litellm": fake_litellm}):
        with pytest.raises(LLMRateLimitError):
            generate_advice(
                _input_for_call(),
                model="gemini/gemini-2.0-flash",
                api_key="k",
            )

    # ждали 5 секунд один раз (между двумя попытками)
    sleep_mock.assert_called_once_with(5)
    # completion дёрнули ровно 2 раза
    assert fake_litellm.completion.call_count == 2


def test_rate_limit_retry_succeeds(monkeypatch):
    # первый вызов — 429, второй — нормальный ответ
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

    # sleep тоже заглушим
    monkeypatch.setattr("tech_update_recommender.llm_module.time.sleep", MagicMock())

    with patch.dict(sys.modules, {"litellm": fake_litellm}):
        result = generate_advice(
            _input_for_call(),
            model="gemini/gemini-2.0-flash",
            api_key="k",
        )
    assert result == "recovered"


def test_network_error_mapped(monkeypatch):
    # таймаут от litellm должен превратиться в LLMNetworkError
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
    # для ollama (локально) ключ не нужен, и падать без него мы не должны
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
    # api_key=None так и уйдёт в litellm — он сам решит что делать
    kwargs = fake_litellm.completion.call_args.kwargs
    assert kwargs["api_key"] is None


def test_api_key_not_logged(caplog):
    # ключ нигде не должен светиться в логах
    fake_litellm = MagicMock()
    fake_litellm.completion.return_value = _make_completion_response("advice text")
    fake_litellm.token_counter.side_effect = RuntimeError("nope")
    fake_litellm.AuthenticationError = type("AuthenticationError", (Exception,), {})
    fake_litellm.RateLimitError = type("RateLimitError", (Exception,), {})
    fake_litellm.APIConnectionError = type("APIConnectionError", (Exception,), {})
    fake_litellm.Timeout = type("Timeout", (Exception,), {})
    fake_litellm.BadRequestError = type("BadRequestError", (Exception,), {})

    # узнаваемый ключ — чтобы потом легко поискать
    secret = "sk-super-secret-1234567890"

    caplog.set_level(logging.DEBUG, logger="tech_update_recommender.llm_module")
    with patch.dict(sys.modules, {"litellm": fake_litellm}):
        generate_advice(
            _input_for_call(),
            model="gemini/gemini-2.0-flash",
            api_key=secret,
        )

    # пробегаем по всем записям — ключа ни в сообщении, ни в args быть не должно
    for rec in caplog.records:
        assert secret not in rec.getMessage()
        # доп. проверка — на случай если кто-то начнёт логать через %r
        for a in rec.args or ():
            assert secret not in str(a)


# 7) user-промпт — проверяем что все секции на месте


def test_build_user_prompt_contains_sections():
    # в промпте должны быть все заголовки
    llm_input = _input_for_call()
    prompt = build_user_prompt(llm_input)
    assert "Отчёт об устаревших и уязвимых зависимостях:" in prompt
    assert "Структура проекта:" in prompt
    assert "Файлы зависимостей:" in prompt
    # имя файла зависимостей тоже на месте
    assert "=== requirements.txt ===" in prompt
    # и финальная инструкция для модели
    assert "Сформируй рекомендации в указанном формате." in prompt
