"""CLI для Tech Update Recommender.

Это финальный пайплайн: сначала сканируем проект через Syft,
потом подтягиваем данные из deps.dev, при необходимости просим LLM
дать рекомендации и в конце собираем отчёт.

Точка входа — main(), она вызывается через console_scripts.

Прогресс показываем через rich в stderr, чтобы не смешивать его
с основным выводом в stdout, особенно если пользователь выбрал JSON.
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

from tech_update_recommender import __version__
from tech_update_recommender.cache import Cache
from tech_update_recommender.config import ConfigError, load_config
from tech_update_recommender.depsdev_module import DepsDevError, build_report
from tech_update_recommender.llm_module import LLMError, build_llm_input, generate_advice
from tech_update_recommender.report import render_report
from tech_update_recommender.syft_module import SyftError, scan_project

logger = logging.getLogger("tech_update_recommender")


# Тут храним значение --verbose.
# Нужно это для main(): если случится неожиданная ошибка, надо понять,
# показывать полный traceback или просто короткое сообщение.
_VERBOSE_FLAG = {"value": False}


def _configure_logging(verbose: bool) -> None:
    """Настраиваем логирование для приложения."""

    # В verbose-режиме показываем больше деталей, иначе только предупреждения и ошибки.
    level = logging.DEBUG if verbose else logging.WARNING

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )


def _make_progress() -> Progress:
    """Создаём прогресс-бар для долгих шагов."""

    # Пишем именно в stderr, чтобы stdout оставался чистым для отчёта или JSON.
    return Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        console=Console(stderr=True),
        transient=True,
    )


def _build_cli_overrides(
    llm_model: str | None,
    llm_api_key: str | None,
    max_context_tokens: int | None,
    syft_path: str | None,
    no_llm: bool,
) -> dict[str, Any]:
    """Собираем настройки, которые пользователь передал через CLI."""

    overrides: dict[str, Any] = {}

    # Добавляем только те параметры, которые реально были переданы.
    # Остальное load_config возьмёт из env или конфиг-файла.
    if llm_model is not None:
        overrides["llm_model"] = llm_model
    if llm_api_key is not None:
        overrides["llm_api_key"] = llm_api_key
    if max_context_tokens is not None:
        overrides["max_context_tokens"] = max_context_tokens
    if syft_path is not None:
        overrides["syft_path"] = syft_path

    return overrides


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(version=__version__, prog_name="tech-update-recommender")
def cli() -> None:
    """Tech Update Recommender — локальный сканер зависимостей с AI-рекомендациями."""


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
    "--max-context-tokens",
    type=int,
    default=None,
    help="Лимит токенов для LLM-промпта (по умолчанию 8000).",
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
    max_context_tokens: int | None,
    no_llm: bool,
    syft_path: str | None,
    verbose: bool,
) -> None:
    """Сканируем проект по PATH и выводим отчёт по зависимостям."""

    _VERBOSE_FLAG["value"] = verbose
    _configure_logging(verbose)

    output = output.lower()
    mode = mode.lower()

    # Если пользователь явно отключил LLM, то оставляем только обычный отчёт.
    if no_llm and mode != "report":
        logger.info("--no-llm передан вместе с --mode=%s, режим понижен до report", mode)
        mode = "report"

    cli_overrides = _build_cli_overrides(
        llm_model,
        llm_api_key,
        max_context_tokens,
        syft_path,
        no_llm,
    )

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

    # Для режимов с LLM обязательно должна быть указана модель.
    if mode in ("advice", "full") and not config.llm.model:
        raise ConfigError(
            "Для режима --mode=" + mode + " нужно указать LLM-модель: "
            "через --llm-model, env var TUR_LLM_MODEL или ~/.tech-update-recommender.yaml."
        )

    # Шаг 1: сканируем проект через Syft и получаем список зависимостей.
    with _make_progress() as progress:
        task_id = progress.add_task("Scanning project with syft...", total=None)
        supported, unsupported = scan_project(path, syft_path=config.syft.path)
        progress.update(task_id, completed=1)

    # Шаг 2: идём в deps.dev и собираем фактический отчёт.
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
        # Кеш открывает SQLite-соединение, поэтому его лучше закрыть явно.
        cache.close()

    # Шаг 3: если нужен advice/full, просим LLM написать рекомендации.
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

    # Шаг 4: собираем финальный отчёт и либо печатаем его, либо сохраняем.
    if mode in ("advice", "full"):
        # В LLM-режимах отчёт сохраняем в файл.
        # Так удобнее, потому что рекомендации могут быть длинными.
        save_path = save or "tech-upd-report.md"

        # Если пользователь не указал --save, по умолчанию сохраняем Markdown.
        save_fmt = output if save else "markdown"

        text = render_report(
            report,
            fmt=save_fmt,
            only_outdated=only_outdated,
            llm_advice=advice,
            llm_model_name=config.llm.model if advice else None,
        )

        Path(save_path).write_text(text, encoding="utf-8")
        click.echo(f"Все рекомендации записаны в {save_path}", err=True)
    else:
        # В обычном режиме просто печатаем отчёт в консоль.
        text = render_report(
            report,
            fmt=output,
            only_outdated=only_outdated,
        )

        click.echo(text)

        # Но если пользователь дал --save, дополнительно сохраняем в файл.
        if save:
            Path(save).write_text(text, encoding="utf-8")
            click.echo(f"Saved to {save}", err=True)


def main() -> None:
    """Точка входа для console_scripts и общая обработка ошибок."""

    try:
        cli(standalone_mode=False)

    except click.exceptions.UsageError as exc:
        # Ошибки в аргументах Click умеет красиво показывать сам.
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
        # Не мешаем Click нормально обрабатывать --help, --version и похожие случаи.
        raise

    except Exception as exc:
        # Для неожиданных ошибок без --verbose показываем короткое сообщение.
        # А с --verbose даём обычный traceback, чтобы было проще дебажить.
        if _VERBOSE_FLAG["value"]:
            raise

        click.echo(
            f"Ошибка: {exc}\nЗапустите с --verbose для подробного traceback.",
            err=True,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()