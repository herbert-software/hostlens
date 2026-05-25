"""``hostlens inspectors`` Typer subcommand group.

Spec: ``openspec/changes/add-inspector-plugin-system/specs/inspector-plugin-system/spec.md``
§需求:CLI ``hostlens inspectors list`` 必须支持过滤与 JSON 输出 /
§需求:CLI ``hostlens inspectors show <name>`` 必须脱敏 secrets.

Both commands are **read-only**, so they tolerate root execution (no EUID==0
refusal — contrast ``hostlens target add/remove`` which must refuse root per
CLAUDE.md §4.5).

Output convention (data on stdout, diagnostics on stderr) follows the rest of
the CLI: ``--json`` writes machine-readable payloads to stdout; manifest load
errors, "inspector not found", and unexpected exceptions go to stderr via
``typer.echo(..., err=True)``.

``inspectors show`` deliberately renders ``secrets`` and ``parameters.default``
values **as declared in the manifest** — the loader stores only the env-var
names, not their resolved values, so the CLI never has the chance to leak
``os.environ`` content. The Pydantic round-trip in ``--json`` mode preserves
the same invariant.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from hostlens.core.config import Settings
from hostlens.core.exceptions import InspectorError
from hostlens.inspectors.registry import (
    RegistryBuildResult,
    build_registry_from_search_paths,
)

__all__ = ["app"]


app = typer.Typer(
    name="inspectors",
    help="Inspector management commands.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _root() -> None:
    """Force Typer into multi-command mode.

    Without an explicit callback, a Typer app with exactly one registered
    ``@app.command`` collapses into single-command mode, which would break
    the spec contract that ``hostlens inspectors --help`` lists ``list``
    and ``show`` as discoverable subcommands.
    """


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _build_registry_or_fatal(verb: str) -> RegistryBuildResult:
    """Assemble the registry and surface fatal errors to stderr+exit 1.

    Per-file user-path failures land in ``result.errors`` (the caller emits
    them); duplicate_inspector (and any other non-collectable kind) is fatal
    and is re-raised by ``build_registry_from_search_paths`` — we catch it
    here so the CLI surfaces a structured one-liner instead of a Python
    traceback. The registry-builder itself never reads secrets, so nothing
    sensitive can show up in ``str(err)``.
    """

    settings = Settings()
    try:
        return build_registry_from_search_paths(
            settings.inspectors_search_paths,
            settings=settings,
        )
    except InspectorError as exc:
        typer.echo(f"hostlens inspectors {verb}: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _emit_load_errors(verb: str, result: RegistryBuildResult) -> bool:
    """Print each per-file user-path failure to stderr.

    Returns ``True`` when at least one error was emitted so the caller can
    flip the exit code to 1. Each error line carries the path, the error
    kind, and a short detail string — enough for an operator (or Agent
    consumer) to locate the bad manifest.
    """

    if not result.errors:
        return False
    for err in result.errors:
        typer.echo(
            f"hostlens inspectors {verb}: {err.path}: {err.kind}: {err.detail}",
            err=True,
        )
    return True


# ---------------------------------------------------------------------------
# `hostlens inspectors list`
# ---------------------------------------------------------------------------


@app.command("list")
def list_cmd(
    tag: str | None = typer.Option(
        None,
        "--tag",
        help="Filter inspectors whose ``tags`` list contains the given tag.",
    ),
    target_kind: str | None = typer.Option(
        None,
        "--target-kind",
        help="Filter inspectors whose ``targets`` list contains the given kind.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON to stdout instead of a Rich table.",
    ),
) -> None:
    """List registered inspectors (builtin + user search paths).

    Filters are AND-combined; missing filters leave the result untouched.
    Output is sorted by inspector name (``InspectorRegistry.list_summaries``
    already returns dictionary order, so the CLI just preserves it).
    """

    result = _build_registry_or_fatal("list")
    has_errors = _emit_load_errors("list", result)

    summaries = result.registry.list_summaries()
    if tag is not None:
        summaries = [s for s in summaries if tag in s.tags]
    if target_kind is not None:
        summaries = [s for s in summaries if target_kind in s.compatible_target_kinds]

    if json_output:
        sys.stdout.write(
            json.dumps([s.model_dump() for s in summaries], indent=2)
        )
        sys.stdout.write("\n")
        sys.stdout.flush()
    else:
        table = Table(title="hostlens inspectors")
        table.add_column("name", no_wrap=True)
        table.add_column("version", no_wrap=True)
        table.add_column("description")
        table.add_column("tags")
        table.add_column("compatible_target_kinds")
        for s in summaries:
            table.add_row(
                s.name,
                s.version,
                s.description,
                ", ".join(s.tags),
                ", ".join(s.compatible_target_kinds),
            )
        Console(highlight=False, soft_wrap=True).print(table)

    if has_errors:
        # Spec §场景:加载错误 exit 1 且 stderr 显示每个失败文件 — silent
        # skip is forbidden so attackers can't plant a same-named manifest
        # in the user path and have the operator miss the load failure.
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# `hostlens inspectors show`
# ---------------------------------------------------------------------------


def _render_manifest_human(payload: dict[str, Any], console: Console) -> None:
    """Render a manifest dict as a two-column Rich key/value table.

    The payload is whatever ``manifest.model_dump(mode='json')`` returns —
    that mode coerces ``Path`` / ``datetime`` / Enum values to plain JSON
    primitives, so a single ``json.dumps`` call renders nested dicts /
    lists cleanly. ``secrets`` carries only env-var **names** (the schema
    stores no values), so this path cannot leak ``os.environ`` content.
    """

    table = Table(title=f"inspector: {payload.get('name', '<unknown>')}")
    table.add_column("field", no_wrap=True)
    table.add_column("value")
    for key in payload:
        value = payload[key]
        if isinstance(value, (dict, list)):
            rendered = json.dumps(value, indent=2, sort_keys=False)
        elif value is None:
            rendered = ""
        else:
            rendered = str(value)
        table.add_row(key, rendered)
    console.print(table)


@app.command("show")
def show_cmd(
    name: str = typer.Argument(..., help="Fully qualified inspector name (e.g. hello.echo)."),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON to stdout instead of a Rich rendering.",
    ),
) -> None:
    """Show a single inspector manifest (read-only; secrets are redacted).

    Looks up ``name`` against the assembled registry; missing names raise
    ``InspectorError(kind="inspector_not_found")`` which the handler maps
    to a stderr message + exit 1. ``secrets`` and ``parameters.default``
    fields render exactly as the manifest declares them — the loader has
    never substituted real values, so this command is structurally unable
    to leak ``os.environ`` content.
    """

    result = _build_registry_or_fatal("show")
    # Per-file user-path failures still emit to stderr so the operator
    # notices them even when running ``show``; they do not by themselves
    # flip the exit code — only a missing inspector does.
    _emit_load_errors("show", result)

    try:
        manifest = result.registry.get(name)
    except InspectorError as exc:
        typer.echo(f"hostlens inspectors show: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    payload = manifest.model_dump(mode="json")

    if json_output:
        sys.stdout.write(json.dumps(payload, indent=2))
        sys.stdout.write("\n")
        sys.stdout.flush()
        return

    _render_manifest_human(payload, Console(highlight=False, soft_wrap=True))
