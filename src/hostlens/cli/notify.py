"""``hostlens notify`` Typer subcommand group — channel introspection,
dry-run rendering, and a real test ping.

Spec: ``openspec/changes/add-notifier-channels/specs/notify-cli-command/spec.md``.
Design D-9.

Three commands over the ``notifiers.yaml`` channel set + the persisted
``ReportStore``:

- ``channels [--json]`` — list every configured channel with its type and
  config-validation status. **Read-only**: never sends, never prints a
  secret value. A missing / unreadable / malformed ``notifiers.yaml`` is a
  readable message + non-crash exit, not a Python traceback.
- ``render --report <id> --channel <name> [--only-if <expr>]`` — load a
  persisted Report, render the target channel's native payload to stdout,
  and (optionally) show the ``only_if`` routing decision. **Dry-run is the
  only behavior**: nothing is ever sent. Unknown report / orphan / unknown
  channel all fail loud with a non-zero exit.
- ``test --channel <name> [--yes]`` — really send one fixed ping message to
  the channel. As an outbound op, a non-TTY run without ``--yes`` exits 1;
  a TTY run confirms interactively. Per spec §需求 (EUID==0 豁免) this does
  **not** trigger the global write-op root refusal — it creates no file and
  changes no inspected-host state.

Exit code contract (project-wide ``3 > 2 > 1 > 0``):

- ``0`` success.
- ``1`` business failure — unknown report / orphan / unknown channel for
  ``render``; a ``test`` send that did not succeed; the non-TTY-no-``--yes``
  guard for ``test``.
- ``2`` configuration error — ``notifiers.yaml`` that is present but
  malformed for ``render`` / ``test`` (a hard fail-loud, distinct from the
  readable empty-state ``channels`` tolerates).
- ``3`` usage error — Typer-rewritten missing/invalid options.

stdout / stderr separation: machine output (channel list / rendered
payload) → stdout; hints / errors → stderr; **no** Python traceback ever
reaches the user.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import httpx
import typer
from pydantic import ValidationError

import hostlens.inspectors.result  # noqa: F401  (triggers Report.model_rebuild)
from hostlens.core.config import Settings, load_settings
from hostlens.core.exceptions import ConfigError
from hostlens.notifiers.base import (
    ChannelTypeRegistry,
    Notifier,
    NotifyPayload,
    NotifyResult,
    register_default_notifiers,
    send_with_retry,
)
from hostlens.notifiers.config import load_channels
from hostlens.notifiers.routing import aggregate_severity, should_send
from hostlens.reporting.models import Report
from hostlens.reporting.store import ReportStore

__all__ = ["notify_app"]


notify_app = typer.Typer(
    name="notify",
    help="Inspect notifier channels, dry-run renders, and send test pings.",
    no_args_is_help=True,
    add_completion=False,
)


@notify_app.callback()
def _root() -> None:
    """Force Typer into multi-command mode so ``channels`` / ``render`` /
    ``test`` stay addressable (same guard used by ``hostlens reports``)."""


def _registry() -> ChannelTypeRegistry:
    registry = ChannelTypeRegistry()
    register_default_notifiers(registry)
    return registry


def _store() -> ReportStore:
    return ReportStore()


def _dumps(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _load_settings_or_exit(command: str) -> Settings:
    try:
        return load_settings()
    except ConfigError as exc:
        typer.echo(f"hostlens notify {command}: configuration error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


# --------------------------------------------------------------------------- #
# `hostlens notify channels`
# --------------------------------------------------------------------------- #


@notify_app.command("channels")
def channels_cmd(
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit the channel list as JSON to stdout instead of a table.",
    ),
) -> None:
    """List configured channels and their config-validation status.

    Read-only: never sends and never prints a secret value. A
    ``notifiers.yaml`` that is absent / unreadable / malformed produces a
    readable message and a non-crash exit (empty list for ``--json``, a hint
    line otherwise) rather than a Python traceback.
    """

    settings = _load_settings_or_exit("channels")
    registry = _registry()

    # ``load_channels`` is fail-loud (it raises on a malformed file / unknown
    # type / missing env var / failed validate_config). For ``channels`` we
    # deliberately do NOT call it; instead ``_collect_channel_rows`` parses the
    # raw yaml itself and validates each channel independently, so one broken
    # channel surfaces as an "invalid" row instead of crashing the whole
    # command — the whole point of ``channels`` is to *surface* config problems.
    rows = _collect_channel_rows(settings, registry)

    if json_output:
        typer.echo(_dumps(rows))
        return

    if not rows:
        path = settings.notifiers_config_path
        if path.exists() and _file_is_malformed(path):
            typer.echo(
                f"notifiers.yaml at {path} is present but malformed; run "
                "'hostlens doctor --check-channels' for the parse error",
                err=True,
            )
        else:
            typer.echo(f"no channels configured; create {path} with a `channels:` mapping")
        return

    for row in rows:
        env_note = (
            "" if row["missing_env_vars"] == [] else f" missing_env={row['missing_env_vars']}"
        )
        reason = "" if row["error"] is None else f" reason={row['error']}"
        typer.echo(f"{row['name']}\ttype={row['type']}\tvalid={row['valid']}{env_note}{reason}")


def _collect_channel_rows(
    settings: Settings,
    registry: ChannelTypeRegistry,
) -> list[dict[str, object]]:
    """Build one status row per configured channel without ever sending.

    Strategy: parse the raw yaml ourselves (so a single bad channel does not
    hide the healthy ones), then for each entry attempt the real
    ``load_channels`` resolution path per channel via a narrowed config. A
    missing file / unreadable file / non-mapping yaml yields an empty list +
    the caller prints a readable hint. The secret values are never copied
    into a row — only the channel ``type`` and a boolean validity verdict.
    """

    raw = _read_raw_channels(settings)
    if raw is None:
        # Absent / unreadable / malformed file. The caller renders the
        # readable empty-state; we return [] (no crash).
        return []

    rows: list[dict[str, object]] = []
    for name, entry in raw.items():
        rows.append(_channel_row(name, entry, registry))
    return rows


def _read_raw_channels(settings: Settings) -> dict[str, object] | None:
    """Return the raw ``channels`` mapping (pre env-expansion) or ``None``.

    ``None`` signals an absent / unreadable / malformed file so the caller
    renders the readable empty-state. A present-but-empty ``channels`` maps
    to ``{}`` (a valid "no channels" state).
    """

    import yaml

    path = settings.notifiers_config_path
    if not path.exists():
        return None
    try:
        text = path.read_text()
    except OSError:
        return None
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        return None
    channels = parsed.get("channels", {})
    if channels is None:
        return {}
    if not isinstance(channels, dict):
        return None
    return channels


def _file_is_malformed(path: Path) -> bool:
    """True if ``path`` exists but does not parse to a ``channels`` mapping.

    Lets ``channels`` report a present-but-broken ``notifiers.yaml`` distinctly
    from a genuinely absent / empty one (both yield an empty row list). An
    empty file or an absent ``channels`` key is a valid empty state, not
    malformed.
    """

    import yaml

    try:
        text = path.read_text()
    except OSError:
        return True
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError:
        return True
    if parsed is None:
        return False
    if not isinstance(parsed, dict):
        return True
    channels = parsed.get("channels", {})
    if channels is None:
        return False
    return not isinstance(channels, dict)


def _channel_row(
    name: object,
    entry: object,
    registry: ChannelTypeRegistry,
) -> dict[str, object]:
    """Classify one channel entry: type, validity, and missing env vars.

    Never reads or echoes a secret value: ``${VAR}`` placeholders are scanned
    for *names* only (so we can report which env vars are unset), and the
    validity verdict comes from constructing the adapter + ``validate_config``
    against env-expanded values that stay local to this function.
    """

    import os
    import re

    name_s = str(name)
    if not isinstance(entry, dict):
        return {
            "name": name_s,
            "type": None,
            "valid": False,
            "missing_env_vars": [],
            "error": "channel entry is not a mapping",
        }

    channel_type = entry.get("type")
    if not isinstance(channel_type, str) or channel_type == "":
        return {
            "name": name_s,
            "type": None,
            "valid": False,
            "missing_env_vars": [],
            "error": "missing `type`",
        }

    # Collect referenced env-var names and flag any that are unset, without
    # ever reading the value into the row. An empty ``${}`` placeholder is
    # illegal (the loader raises on it), so the real load would fail — surface
    # that here rather than optimistically expanding it to "".
    placeholder = re.compile(r"\$\{([^}]*)\}")
    missing_env: list[str] = []
    has_empty_placeholder = False
    for value in entry.values():
        if isinstance(value, str):
            for match in placeholder.finditer(value):
                var = match.group(1)
                if var == "":
                    has_empty_placeholder = True
                elif var not in os.environ:
                    missing_env.append(var)
    missing_env = sorted(set(missing_env))

    try:
        notifier_cls = registry.get(channel_type)
    except KeyError:
        return {
            "name": name_s,
            "type": channel_type,
            "valid": False,
            "missing_env_vars": missing_env,
            "error": f"unknown channel type {channel_type!r}",
        }

    if has_empty_placeholder:
        return {
            "name": name_s,
            "type": channel_type,
            "valid": False,
            "missing_env_vars": missing_env,
            "error": "empty ${} placeholder is illegal",
        }

    if missing_env:
        # Env vars are unresolved, so we cannot run validate_config against a
        # complete config. Report invalid + the missing names (no secret).
        return {
            "name": name_s,
            "type": channel_type,
            "valid": False,
            "missing_env_vars": missing_env,
            "error": "unset env var(s) referenced",
        }

    # Expand placeholders locally (values never leave this function) and run
    # the adapter's own validate_config so the verdict matches assembly.
    expanded: dict[str, object] = {}
    for key, value in entry.items():
        if isinstance(value, str):
            expanded[key] = placeholder.sub(lambda m: os.environ.get(m.group(1), ""), value)
        else:
            expanded[key] = value
    config = {key: value for key, value in expanded.items() if key != "type"}

    try:
        notifier = notifier_cls(instance_name=name_s, config=config)  # type: ignore[call-arg]
        notifier.validate_config(config)
    except (ValueError, TypeError) as exc:
        return {
            "name": name_s,
            "type": channel_type,
            "valid": False,
            "missing_env_vars": [],
            "error": str(exc),
        }

    return {
        "name": name_s,
        "type": channel_type,
        "valid": True,
        "missing_env_vars": [],
        "error": None,
    }


# --------------------------------------------------------------------------- #
# `hostlens notify render`
# --------------------------------------------------------------------------- #


@notify_app.command("render")
def render_cmd(
    report_id: str = typer.Option(
        ...,
        "--report",
        help="Persisted run id (from `hostlens reports list`).",
    ),
    channel: str = typer.Option(
        ...,
        "--channel",
        help="Channel instance name (from `hostlens notify channels`).",
    ),
    only_if: str | None = typer.Option(
        None,
        "--only-if",
        help="Optional only_if expression to show the routing decision "
        "(send / skip + reason) without sending.",
    ),
) -> None:
    """Render ``--channel``'s native payload for ``--report`` to stdout (dry-run).

    Never sends. Unknown report / orphan-stored report / unknown channel all
    fail loud with a non-zero exit and a readable reason; the rendered
    channel-native payload is the only stdout content on success.
    """

    settings = _load_settings_or_exit("render")

    notifier = _resolve_channel_or_exit(settings, channel)
    report = _load_report_or_exit(report_id)

    severity = aggregate_severity(report)

    if only_if is not None:
        decision = asyncio.run(should_send(channel, only_if, report))
        _print_routing_decision(decision)

    payload = notifier.render(report, severity=severity)
    # The adapter render already ran ``redact_report_for_render``; the body is
    # channel-native (Telegram MarkdownV2 text / Lark card JSON string).
    sys.stdout.write(payload.body)
    if not payload.body.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()
    if payload.truncated:
        typer.echo("note: payload was truncated to the channel length limit", err=True)


def _print_routing_decision(decision: NotifyResult | None) -> None:
    """Print the ``only_if`` routing verdict to stderr (stdout stays the payload)."""

    if decision is None:
        typer.echo("routing: send (only_if truthy or absent)", err=True)
    elif decision.status == "skipped":
        typer.echo("routing: skip (only_if evaluated falsy)", err=True)
    else:
        typer.echo(f"routing: failed ({decision.error})", err=True)


def _resolve_channel_or_exit(settings: Settings, channel: str) -> Notifier:
    """Load + return the channel instance, or fail loud (exit 1 / 2).

    A malformed ``notifiers.yaml`` (present but unparsable / unknown type /
    missing env var / failed validate_config) is a configuration error
    (exit 2); an unknown channel name in an otherwise-valid file is a
    business failure (exit 1, symmetric with the unknown-report path).
    """

    registry = _registry()
    try:
        channels = load_channels(settings, registry)
    except ConfigError as exc:
        typer.echo(
            f"hostlens notify render: failed to load notifiers.yaml: {exc}",
            err=True,
        )
        raise typer.Exit(code=2) from exc

    notifier = channels.get(channel)
    if notifier is None:
        known = ", ".join(sorted(channels)) or "<none>"
        typer.echo(
            f"unknown channel: {channel}; configured channels: {known}",
            err=True,
        )
        raise typer.Exit(code=1)
    return notifier


def _load_report_or_exit(report_id: str) -> Report:
    """Load the persisted Report for ``report_id`` or fail loud (exit 1).

    Unknown id / orphan (``get_run`` returns ``None``) → exit 1 with a hint;
    a corrupt stored blob (``ValidationError``) / damaged db (``sqlite3.Error``)
    → exit 1 with a single stderr line, never a Python traceback.
    """

    try:
        report = asyncio.run(_store().get_run(report_id))
    except ValidationError as exc:
        typer.echo(
            f"stored report is invalid or corrupt: {report_id} ({type(exc).__name__})",
            err=True,
        )
        raise typer.Exit(code=1) from exc
    except sqlite3.Error as exc:
        typer.echo(
            f"reports: store unavailable or corrupt: {type(exc).__name__}",
            err=True,
        )
        raise typer.Exit(code=1) from exc

    if report is None:
        typer.echo(
            f"report not found: {report_id}; run 'hostlens reports list <target>' "
            "to see persisted runs (orphan-stored reports are not retrievable here)",
            err=True,
        )
        raise typer.Exit(code=1)
    return report


# --------------------------------------------------------------------------- #
# `hostlens notify test`
# --------------------------------------------------------------------------- #


@notify_app.command("test")
def test_cmd(
    channel: str = typer.Option(
        ...,
        "--channel",
        help="Channel instance name (from `hostlens notify channels`).",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Skip the interactive confirmation (required in non-interactive mode).",
    ),
) -> None:
    """Really send one fixed ping message to ``--channel``.

    Outbound op: a non-TTY run without ``--yes`` exits 1 (never sends); a TTY
    run confirms interactively. Per spec §需求 (EUID==0 豁免) this does NOT
    trigger the global write-op root refusal — it writes no file and changes
    no inspected-host state.

    Exit code: 0 on a successful send; 1 on the non-TTY-no-``--yes`` guard, a
    failed send, or an unknown channel; 2 on a configuration error (malformed
    / unreadable ``notifiers.yaml``), per the module exit-code contract.
    """

    settings = _load_settings_or_exit("test")
    notifier = _resolve_channel_or_exit_for_test(settings, channel)

    if not yes:
        if not sys.stdin.isatty():
            typer.echo(
                "hostlens notify test: --yes required in non-interactive mode",
                err=True,
            )
            raise typer.Exit(code=1)
        confirmed = typer.confirm(f"Send a test ping to channel {channel!r}?", default=False)
        if not confirmed:
            typer.echo("aborted; no message sent")
            raise typer.Exit(code=1)

    payload = _build_ping_payload(notifier, channel)
    result = asyncio.run(notifier.send(payload))

    if result.status == "sent":
        detail = f" detail={result.detail}" if result.detail else ""
        typer.echo(f"sent test ping to {channel} (attempts={result.attempts}){detail}")
        return

    # send() never raises; a non-sent result is a business failure.
    typer.echo(
        f"test ping to {channel} {result.status} (attempts={result.attempts}): {result.error}",
        err=True,
    )
    raise typer.Exit(code=1)


def _resolve_channel_or_exit_for_test(settings: Settings, channel: str) -> Notifier:
    """Same resolution as ``render``: an unknown channel name maps to exit 1
    (business failure), while a malformed ``notifiers.yaml`` (``ConfigError``)
    maps to exit 2 (configuration error), per the module exit-code contract."""

    registry = _registry()
    try:
        channels = load_channels(settings, registry)
    except ConfigError as exc:
        typer.echo(
            f"hostlens notify test: failed to load notifiers.yaml: {exc}",
            err=True,
        )
        raise typer.Exit(code=2) from exc

    notifier = channels.get(channel)
    if notifier is None:
        known = ", ".join(sorted(channels)) or "<none>"
        typer.echo(f"unknown channel: {channel}; configured channels: {known}", err=True)
        raise typer.Exit(code=1)
    return notifier


_PING_TEXT = "hostlens notify test ping"


def _build_ping_payload(notifier: Notifier, channel: str) -> NotifyPayload:
    """Build a fixed, Report-free ping payload in the channel's native shape.

    Telegram bodies are plain MarkdownV2 text; Lark bodies must be card JSON
    that the adapter's ``send`` can ``json.loads``. The ping content is fixed
    so ``test`` has no Report precondition (design 待解决问题: 固定 ping 模板).
    """

    if notifier.name == "lark":
        card = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text", "content": "Hostlens test ping"},
                    "template": "blue",
                },
                "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": _PING_TEXT}}],
            },
        }
        body = json.dumps(card, ensure_ascii=False)
        return NotifyPayload(channel=channel, channel_type=notifier.name, body=body)

    return NotifyPayload(channel=channel, channel_type=notifier.name, body=_PING_TEXT)


# --------------------------------------------------------------------------- #
# doctor `--check-channels` probe (called from cli/doctor.py)
# --------------------------------------------------------------------------- #


def _probe_telegram(config: dict[str, object]) -> tuple[bool, str | None]:
    """Probe a Telegram channel via the Bot API ``getMe`` (no message sent).

    Returns ``(ok, reason)``: ``ok`` is True on HTTP 2xx + ``{"ok": true}``.
    A network / HTTP / business failure yields ``(False, reason)`` with the
    bot token scrubbed. ``getMe`` is read-only — it never delivers a message.
    """

    token = config.get("bot_token")
    if not isinstance(token, str) or not token.strip():
        return False, "missing bot_token"
    url = f"https://api.telegram.org/bot{token}/getMe"

    async def _call() -> NotifyResult:
        async def do_request() -> httpx.Response:
            async with httpx.AsyncClient(timeout=5.0) as client:
                return await client.get(url)

        def interpret(response: httpx.Response) -> NotifyResult:
            from hostlens.notifiers.base import redact_secret_text

            if not 200 <= response.status_code < 300:
                return NotifyResult(
                    channel="telegram",
                    status="failed",
                    error=redact_secret_text(f"getMe HTTP {response.status_code}"),
                )
            try:
                data = response.json()
            except ValueError:
                return NotifyResult(
                    channel="telegram", status="failed", error="getMe non-JSON body"
                )
            if isinstance(data, dict) and data.get("ok") is True:
                return NotifyResult(channel="telegram", status="sent")
            return NotifyResult(channel="telegram", status="failed", error="getMe ok=false")

        return await send_with_retry(
            channel="telegram",
            do_request=do_request,
            interpret=interpret,
            max_attempts=1,
            hard_timeout_seconds=5.0,
        )

    result = asyncio.run(_call())
    if result.status == "sent":
        return True, None
    return False, result.error
