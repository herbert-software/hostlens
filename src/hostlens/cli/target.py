"""``hostlens target`` Typer subcommand group.

Spec: ``openspec/changes/add-execution-target-abstraction/specs/execution-target/spec.md``
§需求:`hostlens target` CLI 命令集且写命令拒绝 root.

The four subcommands ``add`` / ``list`` / ``remove`` / ``test`` are wired
into ``hostlens.cli.app`` so ``hostlens target --help`` displays them.

Write commands (``add`` / ``remove``) refuse to run when ``os.geteuid() ==
0`` (CLAUDE.md §4.5 + global "writes must reject root" rule). Read
commands (``list`` / ``test``) tolerate root.

Errors go to stderr (via ``typer.echo(..., err=True)``); structured data
(yaml writes, JSON output) goes to stdout. The CLI never writes a
``TargetError``'s ``original`` attribute or any password / passphrase
value through any output stream — only ``TargetError.kind`` and the
target name reach the user.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import typer

# ``PyYAML`` ships no PEP 561 marker; ``types-PyYAML`` is a separate dist
# the project does not depend on. Suppress at the import site instead of
# polluting the global mypy config.
import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from hostlens.core.config import load_settings
from hostlens.core.exceptions import ConfigError, TargetError
from hostlens.targets.config import (
    LocalEntry,
    SSHEntry,
    load_targets_config,
)
from hostlens.targets.registry import build_registry_from_config

__all__ = ["target_app"]


target_app = typer.Typer(
    name="target",
    help="Manage Hostlens execution targets (local / ssh).",
    no_args_is_help=True,
    add_completion=False,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _refuse_root_for_write(verb: str) -> None:
    """Exit 1 immediately when running as root for a write-class command.

    CLAUDE.md §4.5: writes must reject ``EUID==0`` to avoid creating
    root-owned config files that the daemon user cannot later read or
    rewrite. The check fires BEFORE any argument parsing side-effect or
    yaml mutation; the spec scenario `target add EUID==0 直接 exit 1`
    asserts that ``targets.yaml`` is never created in this branch.
    """

    if os.geteuid() == 0:
        typer.echo(
            f"hostlens target {verb}: refusing to run as root (EUID=0).",
            err=True,
        )
        typer.echo(
            "Run as a regular user; if you must deploy a daemon as root, "
            "create the config file under a regular user first and chown it.",
            err=True,
        )
        raise typer.Exit(code=1)


# Mirrors ``hostlens.targets.config._PLACEHOLDER_PATTERN``: the loader
# only expands ``${VAR}`` when ``VAR`` matches ``^[A-Z_][A-Z0-9_]*$``.
# Accepting anything else here writes a placeholder that will never be
# expanded, silently surfacing the literal ``${var}`` as the credential.
_ENV_VAR_NAME_PATTERN: re.Pattern[str] = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def _validate_env_var_name(verb: str, flag: str, value: str) -> None:
    if _ENV_VAR_NAME_PATTERN.fullmatch(value) is None:
        typer.echo(
            f"hostlens target {verb}: {flag} value {value!r} is not a valid "
            f"env var name (must match ^[A-Z_][A-Z0-9_]*$)",
            err=True,
        )
        raise typer.Exit(code=2)


def _emit_target_error(verb: str, exc: TargetError) -> None:
    """Render a ``TargetError`` to stderr with only its ``kind`` + target.

    ``original`` is intentionally NOT serialised: SSHTarget's three-layer
    scrubber runs at the throw site, so the only safe surface is the
    structured ``kind`` (already documented in the spec) plus ``target``.
    We also never echo ``extra`` values because callers may put hosts /
    paths in there that should not appear in CLI stderr for an Agent
    consumer.
    """

    parts = [f"hostlens target {verb}: error {exc.kind}"]
    if exc.target is not None:
        parts.append(f"target={exc.target}")
    typer.echo(" ".join(parts), err=True)


def _load_raw_targets_dict(cfg_path: Path, *, fallback_version: str = "1") -> dict[str, Any]:
    """Return the raw ``yaml.safe_load`` dict for the targets config.

    Critical to credential safety: this path does NOT run
    ``${VAR}`` placeholder expansion (that lives in
    ``load_targets_config``). When ``hostlens target add`` / ``remove``
    round-trips the file, we MUST keep the placeholder strings intact
    on entries other than the one being added — otherwise the loader's
    eager expansion would surface real secret values, and writing the
    config back would persist them in plaintext to disk.

    Missing or empty files default to a minimal skeleton
    ``{"version": fallback_version, "targets": []}`` so callers can
    treat the result as always-mutable.
    """

    if not cfg_path.exists():
        return {"version": fallback_version, "targets": []}
    text = cfg_path.read_text() or ""
    parsed = yaml.safe_load(text)
    if not isinstance(parsed, dict):
        return {"version": fallback_version, "targets": []}
    parsed.setdefault("version", fallback_version)
    parsed.setdefault("targets", [])
    return parsed


def _entry_to_dict(
    entry: LocalEntry | SSHEntry, *, password_env: str | None, passphrase_env: str | None
) -> dict[str, Any]:
    """Serialise an entry back into the yaml representation.

    Secret fields (``password`` / ``passphrase``) are written as
    ``${VAR}`` placeholders when the CLI was invoked with the
    corresponding ``--password-env`` / ``--passphrase-env`` flag —
    matches the spec scenario `target add 凭据参数命名一致` and avoids
    ever writing literal passwords to disk via the CLI.
    """

    common: dict[str, Any] = {
        "name": entry.name,
        "type": entry.type,
    }
    # ``enabled`` defaults to True; we still write it explicitly so the
    # yaml stays self-describing for operators reading the file.
    if entry.enabled is False:
        common["enabled"] = False
    if entry.display_name is not None:
        common["display_name"] = entry.display_name
    if entry.description is not None:
        common["description"] = entry.description
    if entry.tags:
        common["tags"] = list(entry.tags)

    if isinstance(entry, SSHEntry):
        common["host"] = entry.host
        common["user"] = entry.user
        if entry.port != 22:
            common["port"] = entry.port
        if entry.key_path is not None:
            common["key_path"] = entry.key_path
        if password_env is not None:
            common["password"] = "${" + password_env + "}"
        if passphrase_env is not None:
            common["passphrase"] = "${" + passphrase_env + "}"
        if entry.connect_timeout is not None:
            common["connect_timeout"] = entry.connect_timeout
    return common


# ---------------------------------------------------------------------------
# Typer wiring
# ---------------------------------------------------------------------------


@target_app.callback()
def _target_root() -> None:
    """Force Typer into multi-command mode for the ``target`` group.

    Mirrors the same trick used by the root ``hostlens.cli.app`` callback:
    without an explicit callback a single-subcommand Typer app collapses
    into single-command mode and the subcommand name disappears from
    ``--help``.
    """


@target_app.command("add")
def add_cmd(
    name: str = typer.Argument(..., help="Target name; must match ^[a-z][a-z0-9_-]{0,63}$."),
    target_type: str = typer.Option(
        ...,
        "--type",
        help="Target backend type: 'local' or 'ssh'.",
    ),
    host: str | None = typer.Option(None, "--host", help="SSH host (required for --type ssh)."),
    user: str | None = typer.Option(None, "--user", help="SSH username."),
    port: int = typer.Option(22, "--port", help="SSH port."),
    key_path: str | None = typer.Option(None, "--key-path", help="Path to SSH private key file."),
    password_env: str | None = typer.Option(
        None,
        "--password-env",
        help="Name of the env var holding the SSH password (yaml will store ${VAR}).",
    ),
    passphrase_env: str | None = typer.Option(
        None,
        "--passphrase-env",
        help="Name of the env var holding the SSH key passphrase (yaml will store ${VAR}).",
    ),
) -> None:
    """Add a target entry to ``~/.config/hostlens/targets.yaml``."""

    # Root check fires before any other side effect (spec
    # §场景:target add EUID==0 直接 exit 1 asserts targets.yaml is not
    # created in this branch).
    _refuse_root_for_write("add")

    if password_env is not None:
        _validate_env_var_name("add", "--password-env", password_env)
    if passphrase_env is not None:
        _validate_env_var_name("add", "--passphrase-env", passphrase_env)

    try:
        settings = load_settings()
    except ConfigError as exc:
        typer.echo(f"hostlens target add: configuration error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    cfg_path = settings.targets_config_path

    try:
        # Write commands use ``expand_env=False`` so they don't fail
        # when existing entries reference an env var that the operator
        # hasn't currently exported. The raw yaml round-trip below
        # preserves those ``${VAR}`` strings verbatim regardless.
        config = load_targets_config(cfg_path, expand_env=False)
    except (ConfigError, ValidationError) as exc:
        typer.echo(f"hostlens target add: failed to load targets config: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    # Name conflict check — exit 2 per spec scenario `target add 名称冲突 exit 2`.
    existing = {entry.name for entry in config.targets}
    if name in existing:
        typer.echo(f"target {name!r} already exists", err=True)
        raise typer.Exit(code=2)

    # Build a new entry. Pydantic re-validates name regex / required
    # fields so we surface the same ValidationError shape used by the
    # yaml loader path.
    new_entry: LocalEntry | SSHEntry
    try:
        if target_type == "local":
            new_entry = LocalEntry(name=name, type="local")
        elif target_type == "ssh":
            if host is None or user is None:
                typer.echo(
                    "hostlens target add: --host and --user are required for --type ssh",
                    err=True,
                )
                raise typer.Exit(code=2)
            new_entry = SSHEntry(
                name=name,
                type="ssh",
                host=host,
                user=user,
                port=port,
                key_path=key_path,
                # Yaml will hold the ``${VAR}`` placeholder string; the
                # in-memory entry temporarily holds the same string
                # because no env-var expansion runs here (CLI is the
                # writer; loader does the expansion on read).
                password=("${" + password_env + "}") if password_env is not None else None,
                passphrase=("${" + passphrase_env + "}") if passphrase_env is not None else None,
            )
        else:
            typer.echo(
                f"hostlens target add: unknown --type {target_type!r}; must be 'local' or 'ssh'",
                err=True,
            )
            raise typer.Exit(code=2)
    except Exception as exc:  # ValidationError from Pydantic
        if isinstance(exc, typer.Exit):
            raise
        typer.echo(f"hostlens target add: invalid entry: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    # Append + write back. To avoid round-tripping ``${VAR}`` placeholders
    # through the loader (which expands them, leaving us writing the
    # **literal expanded** secret back to disk), we re-read the file as
    # a raw dict via ``yaml.safe_load`` — preserving any existing
    # placeholder strings — and append only the new entry's serialised
    # form. The earlier ``load_targets_config(cfg_path)`` call still
    # served its purpose: it validated the existing file is parseable
    # before we touch it.
    raw = _load_raw_targets_dict(cfg_path, fallback_version=config.version)
    new_entry_dict = _entry_to_dict(
        new_entry,
        password_env=password_env,
        passphrase_env=passphrase_env,
    )
    raw.setdefault("targets", [])
    raw["targets"].append(new_entry_dict)

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    typer.echo(f"added target {name!r} ({target_type}) to {cfg_path}")


@target_app.command("list")
def list_cmd(
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON to stdout instead of a Rich table.",
    ),
) -> None:
    """List configured targets, their capabilities, and enabled state."""

    try:
        settings = load_settings()
    except ConfigError as exc:
        typer.echo(f"hostlens target list: configuration error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    try:
        config = load_targets_config(settings.targets_config_path)
        registry = build_registry_from_config(config, settings)
    except (ConfigError, TargetError, ValidationError) as exc:
        typer.echo(f"hostlens target list: failed to load targets: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    rows: list[dict[str, Any]] = []
    for entry in registry.list_entries():
        target = registry.get(entry.name)
        # ``capabilities`` is the live set the target maintains. Sort
        # to keep snapshot-style JSON output deterministic for tests.
        caps = sorted(c.value for c in target.capabilities)
        rows.append(
            {
                "name": entry.name,
                "type": entry.type,
                "enabled": entry.enabled,
                "capabilities": caps,
            }
        )

    if json_output:
        sys.stdout.write(json.dumps({"targets": rows}, indent=2, sort_keys=False))
        sys.stdout.write("\n")
        sys.stdout.flush()
        return

    table = Table(title="hostlens targets")
    table.add_column("name", no_wrap=True)
    table.add_column("type", no_wrap=True)
    table.add_column("enabled", no_wrap=True)
    table.add_column("capabilities")
    for row in rows:
        table.add_row(
            row["name"],
            row["type"],
            str(row["enabled"]),
            ", ".join(row["capabilities"]),
        )
    Console(highlight=False, soft_wrap=True).print(table)
    if not rows:
        typer.echo("no targets configured; run `hostlens target add` to start.")


@target_app.command("remove")
def remove_cmd(
    name: str = typer.Argument(..., help="Target name to remove."),
    yes: bool = typer.Option(False, "--yes", help="Skip the interactive y/N confirmation."),
) -> None:
    """Remove a target entry from ``~/.config/hostlens/targets.yaml``."""

    _refuse_root_for_write("remove")

    try:
        settings = load_settings()
    except ConfigError as exc:
        typer.echo(f"hostlens target remove: configuration error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    cfg_path = settings.targets_config_path

    try:
        # Same expand_env=False rationale as ``target add`` — operators
        # must be able to remove a stale entry even when other entries
        # reference env vars that aren't currently set.
        config = load_targets_config(cfg_path, expand_env=False)
    except (ConfigError, ValidationError) as exc:
        typer.echo(f"hostlens target remove: failed to load targets config: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    matching = [entry for entry in config.targets if entry.name == name]
    if not matching:
        typer.echo(f"target {name!r} not found", err=True)
        raise typer.Exit(code=2)

    # Non-interactive guard (spec §场景:target remove 无 TTY 无 --yes
    # exit 1). When stdin is not a TTY and ``--yes`` is absent we refuse
    # to delete silently — otherwise a CI pipe could nuke a target
    # without intent.
    if not yes:
        if not sys.stdin.isatty():
            typer.echo(
                "hostlens target remove: --yes required in non-interactive mode",
                err=True,
            )
            raise typer.Exit(code=1)
        confirmed = typer.confirm(f"Remove target {name!r}?", default=False)
        if not confirmed:
            typer.echo("aborted; no changes written")
            raise typer.Exit(code=1)

    # Round-trip via the raw yaml dict (not the expanded TargetsConfig)
    # so existing ``${VAR}`` placeholders on other entries survive
    # untouched. ``load_targets_config(cfg_path)`` above already
    # validated the file is parseable before we mutate it.
    raw = _load_raw_targets_dict(cfg_path, fallback_version=config.version)
    raw["targets"] = [item for item in raw.get("targets", []) if item.get("name") != name]
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    typer.echo(f"removed target {name!r} from {cfg_path}")


@target_app.command("test")
def test_cmd(
    name: str = typer.Argument(..., help="Target name to test."),
) -> None:
    """Probe a target's connectivity and runtime capabilities."""

    try:
        settings = load_settings()
    except ConfigError as exc:
        typer.echo(f"hostlens target test: configuration error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    try:
        config = load_targets_config(settings.targets_config_path)
        registry = build_registry_from_config(config, settings)
    except (ConfigError, TargetError, ValidationError) as exc:
        typer.echo(f"hostlens target test: failed to load targets: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    try:
        entry = registry.get_entry(name)
    except KeyError:
        typer.echo(f"target {name!r} not found", err=True)
        raise typer.Exit(code=2) from None

    if entry.enabled is False:
        # Spec §需求:`hostlens target` CLI 命令集 — disabled target test
        # must exit 1 and never trigger a connection.
        typer.echo(
            f"target {name!r} is disabled in targets.yaml",
            err=True,
        )
        raise typer.Exit(code=1)

    target = registry.get(name)
    probe_cmd = "echo hostlens-probe-$$"

    try:
        result = asyncio.run(target.exec(probe_cmd, timeout=5))
    except TargetError as exc:
        _emit_target_error("test", exc)
        raise typer.Exit(code=1) from exc

    console = Console(highlight=False, soft_wrap=True)
    console.print(f"target {name!r} ({entry.type}):")
    console.print(f"  exit_code: {result.exit_code}")
    console.print(f"  timed_out: {result.timed_out}")
    console.print(f"  duration_seconds: {result.duration_seconds:.3f}")
    if result.stdout:
        console.print(f"  stdout: {result.stdout.rstrip()}")
    if result.stderr:
        console.print(f"  stderr: {result.stderr.rstrip()}")
    caps = sorted(c.value for c in target.capabilities)
    console.print(f"  capabilities: {', '.join(caps)}")

    if result.timed_out or (result.exit_code is not None and result.exit_code != 0):
        typer.echo(
            f"hostlens target test: probe command failed for {name!r}",
            err=True,
        )
        raise typer.Exit(code=1)
