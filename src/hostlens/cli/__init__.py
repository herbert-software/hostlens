"""Hostlens CLI entrypoint.

`pyproject.toml` registers `hostlens = "hostlens.cli:main"`, so the
`main()` function below is what `pip install -e .` invokes as the
`hostlens` shell command. ``main`` wraps the Typer ``app`` and rewrites
Click ``UsageError`` exits from ``2`` to ``3`` so the project-wide exit
code semantics (``2`` = runner / business failure; ``3`` = usage error)
hold uniformly across every subcommand.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click
import typer
from dotenv import dotenv_values

from hostlens.cli.demo import app as demo_app
from hostlens.cli.doctor import run_doctor
from hostlens.cli.fix import fix_cmd
from hostlens.cli.inspect import inspect_cmd
from hostlens.cli.inspectors import app as inspectors_app
from hostlens.cli.mcp import mcp_app
from hostlens.cli.notify import notify_app
from hostlens.cli.reports import reports_app
from hostlens.cli.schedule import schedule_app
from hostlens.cli.target import target_app
from hostlens.core.exceptions import ConfigError

__all__ = ["app", "main"]


app = typer.Typer(
    name="hostlens",
    help="Hostlens CLI — LLM-driven server inspection agent.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _root() -> None:
    """Force Typer into multi-command mode and load `.env` into `os.environ`.

    Without an explicit callback, a Typer app with exactly one registered
    `@app.command` collapses into single-command mode and the subcommand
    name disappears from `--help`. This callback keeps `doctor` addressable
    as `hostlens doctor` (which the cli-foundation spec requires).

    Loading `.env` here makes it the single source for every env-based config
    surface: pydantic `Settings` already read `.env`, but `${VAR}` placeholder
    expansion (notifiers.yaml / targets.yaml) and inspector secrets read bare
    `os.environ` and never saw it. We read via `dotenv_values` + `setdefault`,
    not `load_dotenv`: `dotenv_values` resolves `${VAR}` interpolation with the
    same file-wins precedence pydantic uses, so a `Settings` value never
    changes (only its hit layer moves to `os.environ`), and it is immune to the
    `PYTHON_DOTENV_DISABLED` env var that silently turns `load_dotenv` into a
    no-op. `setdefault` keeps an explicit `export` authoritative; the explicit
    cwd-relative path (no `find_dotenv`) matches `Settings(env_file=".env")`. A
    missing — or unreadable / is-a-directory — `.env` is skipped silently.
    """

    try:
        values = dotenv_values(dotenv_path=Path(".env"))
    except OSError:
        return
    for key, value in values.items():
        if value is not None:
            os.environ.setdefault(key, value)


@app.command("doctor")
def doctor_cmd(
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON to stdout instead of a Rich table.",
    ),
    check_channels: bool = typer.Option(
        False,
        "--check-channels",
        help="Probe each configured notifier channel (Telegram getMe / Lark "
        "config validation) and add the result under `checks.channels`.",
    ),
) -> None:
    """Check local environment health (Python version, env vars, config dir)."""

    try:
        exit_code = run_doctor(json_output=json_output, check_channels=check_channels)
    except ConfigError as exc:
        # `run_doctor()` calls `load_settings()` which raises ConfigError on
        # invalid user config (e.g. `HOSTLENS_LOG_MODE=invalid`). core/config
        # has already redacted sensitive field values to "***", so printing
        # str(exc) is safe. Show a friendly one-liner instead of a Python
        # traceback at the CLI boundary.
        typer.echo(f"hostlens: configuration error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


app.add_typer(target_app, name="target")
app.add_typer(inspectors_app, name="inspectors")
app.add_typer(demo_app, name="demo")
app.add_typer(reports_app, name="reports")
app.add_typer(schedule_app, name="schedule")
app.add_typer(notify_app, name="notify")
app.add_typer(mcp_app, name="mcp")
app.command("inspect")(inspect_cmd)
app.command("fix")(fix_cmd)


def main() -> None:
    """Run the Typer app with project-wide Click UsageError exit rewriting.

    Click maps usage errors (``Missing argument`` / ``Missing option`` /
    ``Invalid value for ...``) to ``SystemExit(2)``. The Hostlens CLI
    reserves exit code ``2`` for runner / business failures, so we
    intercept ``click.UsageError`` (and its subclasses ``BadParameter``
    / ``MissingParameter``) and translate to exit code ``3``. ``--help``
    / ``--version`` bypass ``UsageError`` (they go through Click's
    ``HelpOption`` / version flag which calls ``sys.exit(0)`` directly),
    so this wrapper never demotes their success exit.

    Using ``standalone_mode=False`` makes Click re-raise ``UsageError``
    to us instead of catching it internally and exiting 2 itself. In
    this mode Click also catches ``click.exceptions.Exit`` (which
    ``typer.Exit`` extends) and returns ``exit_code`` directly — we
    then ``sys.exit`` with that code so explicit ``typer.Exit(2)`` /
    ``typer.Exit(3)`` from the business layer reach the shell intact.
    """

    try:
        result = app(standalone_mode=False)
    except click.UsageError as exc:
        # Render the message the same way Click would have (single line
        # on stderr) so test scenarios that grep for ``Missing argument``
        # / ``Missing option`` / ``Invalid value for`` still pass.
        exc.show()
        sys.exit(3)
    except click.ClickException as exc:
        # Non-usage Click exceptions (e.g. ``Abort``) fall back to
        # Click's own exit code so existing semantics (target remove
        # abort = exit 1) keep working.
        exc.show()
        sys.exit(exc.exit_code)

    # Click returns the function's return value when standalone_mode is
    # False; for a ``typer.Exit(code=N)`` the click runtime converts the
    # exception into a returned int. ``None`` means the command body
    # ran to completion without raising — exit 0.
    if isinstance(result, int):
        sys.exit(result)
    sys.exit(0)
