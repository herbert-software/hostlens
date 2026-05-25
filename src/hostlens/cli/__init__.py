"""Hostlens CLI entrypoint.

`pyproject.toml` registers `hostlens = "hostlens.cli:app"`, so the `app`
object below is what `pip install -e .` exposes as the `hostlens` shell
command. M0 ships a single subcommand, `doctor`.
"""

from __future__ import annotations

import sys

import typer

from hostlens.cli.doctor import run_doctor
from hostlens.cli.inspectors import app as inspectors_app
from hostlens.cli.target import target_app
from hostlens.core.exceptions import ConfigError

__all__ = ["app"]


app = typer.Typer(
    name="hostlens",
    help="Hostlens CLI — LLM-driven server inspection agent.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _root() -> None:
    """Force Typer into multi-command mode.

    Without an explicit callback, a Typer app with exactly one registered
    `@app.command` collapses into single-command mode and the subcommand
    name disappears from `--help`. This callback keeps `doctor` addressable
    as `hostlens doctor` (which the cli-foundation spec requires).
    """


@app.command("doctor")
def doctor_cmd(
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON to stdout instead of a Rich table.",
    ),
) -> None:
    """Check local environment health (Python version, env vars, config dir)."""

    try:
        exit_code = run_doctor(json_output=json_output)
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


def main() -> None:  # pragma: no cover - convenience for `python -m hostlens.cli`
    sys.exit(app())
