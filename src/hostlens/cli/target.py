"""``hostlens target`` Typer subcommand group.

Spec: ``openspec/changes/add-execution-target-abstraction/specs/execution-target/spec.md``
§需求:`hostlens target` CLI 命令集且写命令拒绝 root.

The five subcommands ``add`` / ``list`` / ``remove`` / ``test`` / ``import``
are wired into ``hostlens.cli.app`` so ``hostlens target --help`` displays
them.

Write commands (``add`` / ``remove``, and ``import`` on the ``--yes`` write
path) refuse to run when ``os.geteuid() == 0`` (CLAUDE.md §4.5 + global "writes
must reject root" rule). Read commands (``list`` / ``test``, and ``import``'s
default dry-run preview) tolerate root.

Errors go to stderr (via ``typer.echo(..., err=True)``); structured data
(yaml writes, JSON output) goes to stdout. The CLI never writes a
``TargetError``'s ``original`` attribute or any password / passphrase
value through any output stream — only ``TargetError.kind`` and the
target name reach the user.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
from typing import Any

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from hostlens.core.config import load_settings
from hostlens.core.exceptions import ConfigError, TargetError
from hostlens.targets.config import (
    LocalEntry,
    SSHEntry,
    _atomic_write_yaml,
    _entry_to_dict,
    _load_raw_targets_dict,
    load_targets_config,
    save_targets_config,
)
from hostlens.targets.onboard import assemble_save_entries, build_import_plan
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

# Known ``--source`` values for ``target import``. The flag is a **bare
# ``str``** validated manually here (not a Typer ``Choice`` / ``Enum``): an
# Enum's unknown-value path raises Click ``UsageError``, which
# ``cli/__init__.py`` rewrites to exit 3 — but an unknown ``--source`` is a
# parameter error and the project contract is exit 2. Manual validation lets us
# raise ``typer.Exit(2)`` directly.
_KNOWN_IMPORT_SOURCES: frozenset[str] = frozenset({"ssh_config", "yaml"})


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

    # Atomic ``0o600`` write (creates / tightens the parent dir to
    # ``0o700``). Same byte output as the prior ``write_text`` but no longer
    # leaves the file world-readable or half-written, and not abradable by a
    # subsequent ``import`` that already wrote ``0o600``.
    try:
        _atomic_write_yaml(cfg_path, raw)
    except ConfigError as exc:
        typer.echo(f"hostlens target add: {exc}", err=True)
        raise typer.Exit(code=2) from exc
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
    try:
        _atomic_write_yaml(cfg_path, raw)
    except ConfigError as exc:
        typer.echo(f"hostlens target remove: {exc}", err=True)
        raise typer.Exit(code=2) from exc
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


@target_app.command("import")
def import_cmd(
    inventory: str = typer.Argument(
        ...,
        help="Path to the inventory source (ssh_config file or yaml inventory).",
    ),
    source: str | None = typer.Option(
        None,
        "--source",
        help="Inventory source: 'ssh_config' or 'yaml'. Omit to content-sniff.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview the import plan without writing (this is the DEFAULT behaviour).",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Write the resolved targets to targets.yaml (otherwise dry-run preview only).",
    ),
    skip_unreachable: bool = typer.Option(
        False,
        "--skip-unreachable",
        help="Only onboard reachable candidates (this is the DEFAULT behaviour).",
    ),
    include_unreachable: bool = typer.Option(
        False,
        "--include-unreachable",
        help="Also register probe-failed candidates with enabled=False (escape hatch).",
    ),
    concurrency: int = typer.Option(
        10,
        "--concurrency",
        help="Max simultaneous probe connections (semaphore bound).",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit the import plan as machine-readable JSON to stdout.",
    ),
) -> None:
    """Batch-onboard targets from an inventory source (dry-run by default).

    Pipeline: source → promote → probe → plan → save. ``--dry-run`` (the
    default) renders the plan and exits 0 without writing; ``--yes`` writes
    ``to_add`` (and, with ``--include-unreachable``, the failed candidates as
    ``enabled=False``). There is no per-row prompt — absence of ``--yes`` is the
    dry-run preview, which is why missing ``--yes`` exits 0 rather than 1.
    """

    # ``--dry-run`` and ``--yes`` are mutually exclusive: passing both is a
    # fail-safe parameter error (prevents muscle-memory ``--dry-run`` plus an
    # added ``--yes`` from silently writing). Exit 2 (parameter error).
    if dry_run and yes:
        typer.echo(
            "hostlens target import: --dry-run and --yes are mutually exclusive",
            err=True,
        )
        raise typer.Exit(code=2)

    # ``--skip-unreachable`` (default) and ``--include-unreachable`` are
    # opposites; passing both is a parameter error (exit 2).
    if skip_unreachable and include_unreachable:
        typer.echo(
            "hostlens target import: --skip-unreachable and --include-unreachable "
            "are mutually exclusive",
            err=True,
        )
        raise typer.Exit(code=2)

    # ``--source`` is a bare str validated here so an unknown value maps to
    # exit 2 (a Typer Choice/Enum would route through Click UsageError → exit
    # 3, breaking the "parameter error == exit 2" contract).
    if source is not None and source not in _KNOWN_IMPORT_SOURCES:
        known = ", ".join(sorted(_KNOWN_IMPORT_SOURCES))
        typer.echo(
            f"hostlens target import: unknown --source {source!r}; must be one of: {known}",
            err=True,
        )
        raise typer.Exit(code=2)

    will_write = yes
    # Root refusal fires before any other write side-effect, but only on the
    # write path: a dry-run preview is read-only (it still probes remote hosts,
    # but never touches the local targets.yaml), so it must not refuse root.
    if will_write:
        _refuse_root_for_write("import")

    try:
        settings = load_settings()
    except ConfigError as exc:
        typer.echo(f"hostlens target import: configuration error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    cfg_path = settings.targets_config_path

    # ``--json`` requires stdout to be a single valid JSON document. The config
    # loader emits a structlog DEBUG line to stdout when ``targets.yaml`` is
    # absent (``PrintLoggerFactory`` writes to stdout), which would corrupt the
    # JSON. So while building the plan (and later while writing) we redirect
    # stdout to stderr; only the explicit ``render_json`` below reaches the real
    # stdout. In human mode this is a no-op. ``redirect_stdout`` is not
    # reusable, so build a fresh manager per critical section.
    def _quiet_stdout() -> contextlib.AbstractContextManager[Any]:
        if json_output:
            return contextlib.redirect_stdout(sys.stderr)
        return contextlib.nullcontext()

    with _quiet_stdout():
        # Pre-validate the existing targets.yaml (mirrors ``target add``): a
        # corrupt / misplaced-placeholder file raises ConfigError → exit 2
        # rather than being silently round-tripped. ``expand_env=False`` so
        # unrelated entries referencing currently-unset env vars do not block.
        try:
            existing = load_targets_config(cfg_path, expand_env=False)
        except (ConfigError, ValidationError) as exc:
            typer.echo(
                f"hostlens target import: failed to load existing targets config: {exc}",
                err=True,
            )
            raise typer.Exit(code=2) from exc

        existing_names = {entry.name for entry in existing.targets}

        # Build the plan: parse + promote + probe + classify. Parse errors
        # (inventory syntax / unknown source / ambiguous sniff) raise
        # ConfigError → exit 2; the rest (unreachable hosts, invalid candidates)
        # are bucketed into the plan, never raised.
        try:
            plan = asyncio.run(
                build_import_plan(
                    inventory,
                    source=source,
                    settings=settings,
                    existing_names=existing_names,
                    concurrency=concurrency,
                )
            )
        except ConfigError as exc:
            typer.echo(f"hostlens target import: {exc}", err=True)
            raise typer.Exit(code=2) from exc

    if json_output:
        sys.stdout.write(plan.render_json())
        sys.stdout.write("\n")
        sys.stdout.flush()
    else:
        console = Console(highlight=False, soft_wrap=True)
        console.print(plan.render_diff(), markup=False)

    # Human-readable status trailers go to stdout normally, but in ``--json``
    # mode stdout must stay a single valid JSON document — so route any trailer
    # to stderr when ``--json`` is active (the plan JSON is already on stdout).
    def _status(message: str) -> None:
        typer.echo(message, err=json_output)

    if not will_write:
        # Dry-run (default): render the plan, mark it clearly, exit 0. Zero
        # local side-effect (the remote read-only probe already ran above).
        _status(f"DRY-RUN: nothing written. Pass --yes to write these targets to {cfg_path}.")
        raise typer.Exit(code=0)

    save_entries = assemble_save_entries(plan, include_unreachable=include_unreachable)

    if not save_entries:
        # ``--yes`` but nothing to write. Two distinct cases land here:
        #
        # 1. **All candidates failed probe** (and no ``--include-unreachable``)
        #    → business failure: the operator wanted to onboard but no host was
        #    reachable. Exit 1 (spec §场景:--yes 全探活失败且非 include 退 1).
        # 2. **Empty / already-managed inventory** (no probe failures) →
        #    nothing to onboard is not a failure; exit 0 (spec §场景:空
        #    inventory → 空 plan → exit 0, and idempotent re-runs where every
        #    candidate is ``skipped``).
        if plan.failed_probe and not include_unreachable:
            typer.echo(
                "hostlens target import: no reachable targets to write "
                "(use --include-unreachable to register unreachable candidates).",
                err=True,
            )
            raise typer.Exit(code=1)
        _status("nothing to import; targets.yaml unchanged.")
        raise typer.Exit(code=0)

    try:
        # ``save_targets_config`` re-runs ``load_targets_config`` internally,
        # which may emit the same absent-file DEBUG line to stdout; keep the
        # ``--json`` stdout clean by redirecting during the write too.
        with _quiet_stdout():
            save_targets_config(cfg_path, save_entries)
    except (ConfigError, ValidationError) as exc:
        typer.echo(f"hostlens target import: failed to write targets config: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    _status(f"imported {len(save_entries)} target(s) into {cfg_path}")
