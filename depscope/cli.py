"""CLI DepScope.

Финальный pipeline (Блок 6): Syft → deps.dev → (опционально) LLM → отчёт.

Точка входа — функция :func:`main`, вызываемая через ``console_scripts``.
Прогресс-бары пишутся в stderr через ``rich.progress.Progress``, чтобы
не портить ``stdout`` при выводе JSON.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from depscope import __version__
from depscope.cache import Cache
from depscope.config import ConfigError, load_config
from depscope.depsdev_module import DepsDevError, build_report
from depscope.llm_module import LLMError, build_llm_input, generate_advice
from depscope.report import render_report
from depscope.syft_module import SyftError, scan_project

logger = logging.getLogger("depscope")


# ---------------------------------------------------------------------------
# Глобальный флаг --verbose для main(): нужен, чтобы решить — печатать
# короткое сообщение или полный traceback при неожиданной ошибке.
# Click устанавливает его внутри scan(), main() читает.
# ---------------------------------------------------------------------------
_VERBOSE_FLAG = {"value": False}


def _configure_logging(verbose: bool) -> None:
    """Настроить корневой логгер DepScope.

    --verbose => DEBUG, иначе WARNING. Никаких print() для статуса.
    """
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )


def _make_progress() -> Progress:
    """Прогресс-бар, пишущий в stderr (чтобы не портить stdout с JSON)."""

    return Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        console=Console(stderr=True),
        transient=True,
    )


def _build_cli_overrides(
    llm_model: str | None,
    llm_api_key: str | None,
    syft_path: str | None,
    no_llm: bool,
) -> dict[str, Any]:
    """Собрать словарь cli_overrides для load_config."""

    overrides: dict[str, Any] = {}
    if llm_model is not None:
        overrides["llm_model"] = llm_model
    if llm_api_key is not None:
        overrides["llm_api_key"] = llm_api_key
    if syft_path is not None:
        overrides["syft_path"] = syft_path
    # --no-llm обрабатывается в самом scan() (override mode).
    return overrides


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(version=__version__, prog_name="depscope")
def cli() -> None:
    """DepScope — локальный сканер зависимостей с AI-рекомендациями."""


@cli.command("scan")
@click.argument(
    "path",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, resolve_path=True),
)
@click.option(
    "--output",
    "-o",
    type=click.Choice(["table", "json", "markdown"], case_sensitive=False),
    default="table",
    show_default=True,
    help="Формат вывода отчёта.",
)
@click.option(
    "--mode",
    "-m",
    type=click.Choice(["report", "advice", "full"], case_sensitive=False),
    default="report",
    show_default=True,
    help="Режим работы: report (только факты), advice (только LLM), full (всё).",
)
@click.option(
    "--only-outdated",
    is_flag=True,
    default=False,
    help="Показывать только устаревшие пакеты.",
)
@click.option(
    "--save",
    type=click.Path(dir_okay=False, writable=True, resolve_path=True),
    default=None,
    help="Сохранить отчёт в файл.",
)
@click.option(
    "--llm-model",
    type=str,
    default=None,
    help="Имя модели LiteLLM (например, gemini/gemini-2.0-flash).",
)
@click.option(
    "--llm-api-key",
    type=str,
    default=None,
    help="API-ключ для LLM-провайдера (или через env vars).",
)
@click.option(
    "--no-llm",
    is_flag=True,
    default=False,
    help="Явно отключить LLM-секцию.",
)
@click.option(
    "--syft-path",
    type=click.Path(dir_okay=False, resolve_path=True),
    default=None,
    help="Путь к бинарнику syft (если не в PATH).",
)
@click.option(
    "--verbose",
    is_flag=True,
    default=False,
    help="Подробное логирование (DEBUG).",
)
def scan(
    path: str,
    output: str,
    mode: str,
    only_outdated: bool,
    save: str | None,
    llm_model: str | None,
    llm_api_key: str | None,
    no_llm: bool,
    syft_path: str | None,
    verbose: bool,
) -> None:
    """Просканировать проект по PATH и вывести отчёт о зависимостях."""

    _VERBOSE_FLAG["value"] = verbose
    _configure_logging(verbose)

    output = output.lower()
    mode = mode.lower()

    # --no-llm форсирует режим report (даже если пользователь указал full/advice).
    if no_llm and mode != "report":
        logger.info("--no-llm передан вместе с --mode=%s, режим понижен до report", mode)
        mode = "report"

    cli_overrides = _build_cli_overrides(llm_model, llm_api_key, syft_path, no_llm)
    config = load_config(cli_overrides)

    logger.debug(
        "scan invoked: path=%s output=%s mode=%s only_outdated=%s save=%s "
        "llm_model=%s no_llm=%s syft_path=%s",
        path,
        output,
        mode,
        only_outdated,
        save,
        config.llm.model,
        no_llm,
        config.syft.path,
    )

    if mode in ("advice", "full") and not config.llm.model:
        raise ConfigError(
            "Для режима --mode=" + mode + " нужно указать LLM-модель: "
            "через --llm-model, env var DEPSCOPE_LLM_MODEL или ~/.depscope.yaml."
        )

    # 1. SyftModule
    with _make_progress() as progress:
        task_id = progress.add_task("Scanning project with syft...", total=None)
        supported, unsupported = scan_project(path, syft_path=config.syft.path)
        progress.update(task_id, completed=1)

    # 2. DepsDevModule
    cache = Cache(
        path=Path(config.cache.path).expanduser(),
        ttl_seconds=config.cache.ttl_seconds,
    )
    try:
        with _make_progress() as progress:
            task_id = progress.add_task("Querying deps.dev...", total=None)
            report = asyncio.run(build_report(supported, unsupported, path, cache))
            progress.update(task_id, completed=1)
    finally:
        cache.close()

    # 3. LLMModule (опционально)
    advice: str | None = None
    if mode in ("advice", "full"):
        with _make_progress() as progress:
            task_id = progress.add_task("Generating AI advice...", total=None)
            llm_input = build_llm_input(report, path)
            api_key_value: str | None = None
            if config.llm.api_key is not None:
                api_key_value = config.llm.api_key.get_secret_value()
            advice = generate_advice(
                llm_input,
                model=config.llm.model,
                api_key=api_key_value,
                max_context_tokens=config.llm.max_context_tokens,
            )
            progress.update(task_id, completed=1)

    # 4. ReportModule
    text = render_report(
        report,
        fmt=output,
        only_outdated=only_outdated,
        llm_advice=advice if mode != "report" else None,
        llm_model_name=config.llm.model if advice else None,
    )

    # 5. Печать или сохранение.
    if save:
        Path(save).write_text(text, encoding="utf-8")
        click.echo(f"Saved to {save}", err=True)
    else:
        click.echo(text)


def main() -> None:
    """Entry point для console_scripts с обработкой ошибок верхнего уровня."""

    try:
        cli(standalone_mode=False)
    except click.exceptions.UsageError as exc:
        # Click сам форматирует UsageError; печатаем и выходим с его кодом.
        exc.show()
        sys.exit(exc.exit_code)
    except click.exceptions.Abort:
        click.echo("Отменено пользователем", err=True)
        sys.exit(130)
    except SyftError as exc:
        click.echo(f"[syft] {exc}", err=True)
        sys.exit(2)
    except DepsDevError as exc:
        click.echo(f"[deps.dev] {exc}", err=True)
        sys.exit(3)
    except LLMError as exc:
        click.echo(f"[llm] {exc}", err=True)
        sys.exit(4)
    except ConfigError as exc:
        click.echo(f"[config] {exc}", err=True)
        sys.exit(5)
    except KeyboardInterrupt:
        click.echo("Отменено пользователем", err=True)
        sys.exit(130)
    except SystemExit:
        # Прокидываем дальше — Click ловит --version, --help, и т.п. через SystemExit.
        raise
    except Exception as exc:
        if _VERBOSE_FLAG["value"]:
            raise
        click.echo(
            f"Ошибка: {exc}\nЗапустите с --verbose для подробного traceback.",
            err=True,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
