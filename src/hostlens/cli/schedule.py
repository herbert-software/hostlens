"""``hostlens schedule`` Typer subcommand group — scheduler driver + ledger.

Spec: ``openspec/changes/add-scheduler/specs/schedule-cli-command/spec.md``
(design D-5 / D-9 / D-12).

Five subcommands over the ``schedules/*.yaml`` manifests and the scheduler
runtime:

- ``list`` — every loaded manifest + its ``next_fire_time``.
- ``run`` — foreground scheduling loop until Ctrl-C / SIGTERM (dev/debug).
- ``daemon`` — long-running daemon (same loop, logs to a file).
- ``trigger <name>`` — fire one manifest immediately via the SAME job body
  the timer uses, landing a ``Run`` + ``Report`` (fail-loud on unknown name).
- ``status`` — the most-recent ``Run`` rows + a status-count distribution.

All five subcommands first load + validate every manifest (``load_schedules``);
an invalid manifest is fail-loud (exit 2, stderr file + reason) — none of them
silently continue.

``run`` / ``daemon`` flip ``settings.daemon_mode`` so the existing
``is_daemon_mode`` seam fires ``create_backend``'s daemon-safety gate (design
D-12). A daemon-unsafe backend (``ClaudeSubscriptionBackend``) raises
``BackendDaemonUnsafe`` at boot → exit 1, never enters the loop.

Exit-code contract (aligned with the project-wide CLI semantics): ``0``
success; ``2`` config/manifest load error — the input files are invalid
(malformed settings, targets config, or schedule manifest); ``1`` business
failure — loaded fine but the operation is refused or fails (unknown trigger
name, daemon-unsafe backend, root EUID for the scheduling commands, a job that
raised at runtime).
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn
from zoneinfo import ZoneInfo

import typer
from pydantic import ValidationError

from hostlens.agent.backend import create_backend
from hostlens.core.config import Settings, load_settings
from hostlens.core.exceptions import BackendDaemonUnsafe, ConfigError
from hostlens.core.logging import configure_logging
from hostlens.core.redact import redact_text
from hostlens.notifiers.base import ChannelTypeRegistry, register_default_notifiers
from hostlens.notifiers.config import load_channels
from hostlens.scheduler.loader import load_schedules
from hostlens.scheduler.runner import SchedulerRunner
from hostlens.scheduler.store import RunStore
from hostlens.targets.config import TargetsConfig, load_targets_config
from hostlens.targets.registry import build_registry_from_config
from hostlens.targets.ssh import _DEFAULT_COLD_CONNECT_RETRY_BUDGET_SECONDS

if TYPE_CHECKING:
    from collections.abc import Callable

    import structlog

    from hostlens.agent.backend import LLMBackend
    from hostlens.inspectors.registry import InspectorRegistry
    from hostlens.notifiers.base import Notifier
    from hostlens.scheduler.schema import ScheduleManifest
    from hostlens.scheduler.store import Run
    from hostlens.targets.registry import TargetRegistry
    from hostlens.tools.base import ToolContext

__all__ = ["schedule_app"]


schedule_app = typer.Typer(
    name="schedule",
    help="Drive the scheduler: list / run / daemon / trigger / status.",
    no_args_is_help=True,
    add_completion=False,
)


@schedule_app.callback()
def _root() -> None:
    """Force Typer into multi-command mode (same guard as other groups)."""


# cwd-relative, matching the proposal Demo Path (``cat > schedules/...``) and
# the doctor ``_check_schedules`` convention. Resolved at call time.
_SCHEDULES_DIR = Path("schedules")


# Default daemon log file: under the same XDG data root as runs.db / reports.db
# (``~/.local/share/hostlens/logs/scheduler-daemon.log``), overridable via
# ``--log-file``.
def _default_log_file() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "hostlens" / "logs" / "scheduler-daemon.log"


# --------------------------------------------------------------------------- #
# Shared assembly
# --------------------------------------------------------------------------- #


def _fail(message: str) -> NoReturn:
    """Business failure (exit 1): config loaded fine but the operation is refused."""
    typer.echo(f"hostlens schedule: {message}", err=True)
    raise typer.Exit(code=1)


def _fail_config(message: str) -> NoReturn:
    """Config/manifest load failure (exit 2): the input files are invalid.

    Consistent across settings / targets / manifest loading so scripts can
    distinguish "your config is wrong" (2) from "loaded fine but refused" (1),
    matching the project-wide CLI convention (0 ok / 1 business / 2 config).
    """
    typer.echo(f"hostlens schedule: {message}", err=True)
    raise typer.Exit(code=2)


def _refuse_root(verb: str) -> None:
    """Exit 1 when running as root for a scheduling-class command (CLAUDE.md §4.5).

    ``run`` / ``daemon`` / ``trigger`` drive real work (LLM calls, db writes
    under the user's data dir); running them as root would create root-owned
    runs.db / reports.db files the daemon user cannot later rewrite. Read-only
    ``list`` / ``status`` tolerate root.
    """

    if os.geteuid() == 0:
        typer.echo(
            f"hostlens schedule {verb}: refusing to run as root (EUID=0).",
            err=True,
        )
        typer.echo(
            "Run as a regular user; if you must deploy a daemon as root, "
            "run under a dedicated unprivileged service account.",
            err=True,
        )
        raise typer.Exit(code=1)


def _load_settings_or_exit() -> Settings:
    try:
        return load_settings()
    except ConfigError as exc:
        # core/config already redacted sensitive field values.
        _fail_config(f"configuration error: {exc}")


def _build_target_registry(settings: Settings) -> TargetRegistry:
    # Schedule (fleet) path opts into the cold-connect retry budget so a
    # daemon-driven巡检 tolerates Tailscale 冷路径 first-connect (spec 决策
    # 1). Shared by list/trigger/daemon/status — list/status never
    # ``target.exec`` so the budget is inert there (no split needed).
    if not settings.targets_config_path.exists():
        # Missing targets file is legitimate (empty registry), not an error.
        return build_registry_from_config(
            TargetsConfig(version="1", targets=[]),
            settings,
            cold_connect_retry_budget_seconds=_DEFAULT_COLD_CONNECT_RETRY_BUDGET_SECONDS,
        )
    try:
        config = load_targets_config(settings.targets_config_path)
        return build_registry_from_config(
            config,
            settings,
            cold_connect_retry_budget_seconds=_DEFAULT_COLD_CONNECT_RETRY_BUDGET_SECONDS,
        )
    except (ConfigError, ValidationError) as exc:
        _fail_config(f"failed to load targets config: {exc}")


def _build_channels(settings: Settings) -> dict[str, Notifier]:
    """Load the notifier channels from ``notifiers.yaml`` (fail-loud, exit 2).

    A missing / empty file yields an empty map (no channels configured is a
    valid state); a malformed config (unknown type / unset env var / empty
    required field / unparsable YAML) is fail-loud with a clean exit 2,
    matching ``_build_target_registry`` — never a raw traceback.
    """

    registry = ChannelTypeRegistry()
    register_default_notifiers(registry)
    try:
        return load_channels(settings, registry)
    except ConfigError as exc:
        _fail_config(f"failed to load notifier channels: {exc}")


def _build_inspector_registry(settings: Settings) -> InspectorRegistry:
    from hostlens.inspectors.registry import build_registry_from_search_paths

    return build_registry_from_search_paths(
        settings.inspectors_search_paths,
        settings=settings,
    ).registry


def _load_manifests_or_exit(
    settings: Settings,
    target_registry: TargetRegistry,
    inspector_registry: InspectorRegistry,
) -> list[ScheduleManifest]:
    """Load + validate every manifest; fail-loud (exit 2) on the first invalid one."""

    try:
        return load_schedules(_SCHEDULES_DIR, target_registry, inspector_registry)
    except ConfigError as exc:
        file = getattr(exc, "file", None)
        prefix = f"{file}: " if file else ""
        _fail_config(f"invalid schedule manifest: {prefix}{exc}")


def _context_factory(
    settings: Settings,
    target_registry: TargetRegistry,
    inspector_registry: InspectorRegistry,
    logger: structlog.stdlib.BoundLogger,
) -> Callable[[], ToolContext]:
    """Build a fresh-``ToolContext``-per-dispatch factory (backend never enters
    the context — ADR-008).
    """

    from hostlens.tools.base import NoopApprovalService, ToolContext

    def _make() -> ToolContext:
        return ToolContext(
            target_registry=target_registry,
            inspector_registry=inspector_registry,
            config=settings,
            logger=logger,
            approval_service=NoopApprovalService(),
            cancel=asyncio.Event(),
        )

    return _make


def _build_runner(
    settings: Settings,
    manifests: list[ScheduleManifest],
    target_registry: TargetRegistry,
    inspector_registry: InspectorRegistry,
    logger: structlog.stdlib.BoundLogger,
) -> SchedulerRunner:
    """Assemble a ``SchedulerRunner`` over fresh per-fire backend + context.

    ``create_backend`` is the daemon-safety gate: when ``settings.daemon_mode``
    is True the factory calls ``ensure_safe_for_daemon`` (design D-12). The
    factory is called per fire (one fresh backend per job) so a daemon-unsafe
    backend surfaces at boot (eager construction in ``_serve``) and per fire.
    """

    from hostlens.reporting.store import ReportStore

    def backend_factory() -> LLMBackend:
        return create_backend(settings)

    channels = _build_channels(settings)

    try:
        return SchedulerRunner(
            manifests,
            run_store=RunStore(),
            report_store=ReportStore(),
            settings=settings,
            backend_factory=backend_factory,
            context_factory=_context_factory(settings, target_registry, inspector_registry, logger),
            target_registry=target_registry,
            channels=channels,
            grace_seconds=settings.daemon.shutdown_grace_seconds,
        )
    except ConfigError as exc:
        # Assembly-time manifest/channel validation (e.g. a notify.channel not
        # present in notifiers.yaml) is a configuration error — map it to the
        # CLI's clean exit-2 instead of letting it surface as a raw traceback.
        _fail_config(f"invalid schedule/notify wiring: {exc}")


# --------------------------------------------------------------------------- #
# `hostlens schedule list`
# --------------------------------------------------------------------------- #


def _next_fire_time(manifest: ScheduleManifest) -> datetime | None:
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    tz = ZoneInfo(manifest.schedule.timezone)
    now = datetime.now(tz)
    spec = manifest.schedule
    if spec.cron is not None:
        trigger: CronTrigger | IntervalTrigger = CronTrigger.from_crontab(spec.cron, timezone=tz)
    else:
        interval = spec.interval
        assert interval is not None
        trigger = IntervalTrigger(
            weeks=interval.weeks,
            days=interval.days,
            hours=interval.hours,
            minutes=interval.minutes,
            seconds=interval.seconds,
            timezone=tz,
        )
    fire: datetime | None = trigger.get_next_fire_time(None, now)
    return fire


def _schedule_expr(manifest: ScheduleManifest) -> str:
    spec = manifest.schedule
    if spec.cron is not None:
        return f"cron({spec.cron})"
    interval = spec.interval
    assert interval is not None
    parts = [
        f"{v}{u}"
        for v, u in (
            (interval.weeks, "w"),
            (interval.days, "d"),
            (interval.hours, "h"),
            (interval.minutes, "m"),
            (interval.seconds, "s"),
        )
        if v
    ]
    return f"interval({'+'.join(parts)})"


@schedule_app.command("list")
def list_cmd() -> None:
    """List every loaded manifest with its ``next_fire_time``."""

    settings = _load_settings_or_exit()
    configure_logging(settings.log_mode)
    target_registry = _build_target_registry(settings)
    inspector_registry = _build_inspector_registry(settings)
    manifests = _load_manifests_or_exit(settings, target_registry, inspector_registry)

    if not manifests:
        typer.echo("no schedules configured; add a manifest under schedules/*.yaml")
        return

    for manifest in manifests:
        fire = _next_fire_time(manifest)
        fire_str = fire.isoformat() if fire is not None else "<none>"
        typer.echo(
            f"{manifest.name}\ttargets={','.join(manifest.targets)}\t"
            f"{_schedule_expr(manifest)}\tnext_fire_time={fire_str}"
        )


# --------------------------------------------------------------------------- #
# `hostlens schedule trigger`
# --------------------------------------------------------------------------- #


@schedule_app.command("trigger")
def trigger_cmd(
    name: str = typer.Argument(..., help="Manifest name to fire immediately."),
) -> None:
    """Fire one manifest immediately via the shared job body (fail-loud on unknown name)."""

    _refuse_root("trigger")
    settings = _load_settings_or_exit()
    configure_logging(settings.log_mode)
    import structlog

    logger = structlog.get_logger("hostlens.schedule.trigger")
    target_registry = _build_target_registry(settings)
    inspector_registry = _build_inspector_registry(settings)
    manifests = _load_manifests_or_exit(settings, target_registry, inspector_registry)

    if name not in {m.name for m in manifests}:
        _fail(f"unknown schedule name: {name!r} (not in loaded manifests)")

    runner = _build_runner(settings, manifests, target_registry, inspector_registry, logger)
    try:
        run = asyncio.run(runner.trigger(name))
    except Exception as exc:
        # ``runner.trigger`` already recorded a failed Run before re-raising; the
        # CLI boundary turns that into a clean fail-loud exit (no raw traceback),
        # consistent with the rest of the command's exit contract. Detail is
        # logged through structlog (redacted); the stderr message is redacted too.
        redacted = redact_text(str(exc))
        logger.error("schedule.trigger_failed", name=name, error=redacted)
        _fail(f"trigger {name!r} failed: {redacted}")
    typer.echo(
        f"triggered {name}: run_id={run.run_id} status={run.status} "
        f"report_id={run.report_id or '<none>'}"
    )


# --------------------------------------------------------------------------- #
# `hostlens schedule run` / `hostlens schedule daemon`
# --------------------------------------------------------------------------- #


@schedule_app.command("run")
def run_cmd() -> None:
    """Foreground scheduling loop until SIGTERM / SIGINT (dev/debug)."""

    _refuse_root("run")
    _serve(daemon=False, log_file=None)


@schedule_app.command("daemon")
def daemon_cmd(
    log_file: str | None = typer.Option(
        None,
        "--log-file",
        help="Daemon log file (default ~/.local/share/hostlens/logs/scheduler-daemon.log).",
    ),
) -> None:
    """Long-running daemon (same loop as ``run``, logging to a file)."""

    _refuse_root("daemon")
    _serve(daemon=True, log_file=Path(log_file) if log_file is not None else None)


def _serve(*, daemon: bool, log_file: Path | None) -> None:
    settings = _load_settings_or_exit()

    if daemon:
        # Route logs to a file and announce the path on stderr so operators can
        # find it. Logging is configured to write the file before any job runs.
        actual_log = log_file if log_file is not None else _default_log_file()
        actual_log.parent.mkdir(parents=True, exist_ok=True)
        _configure_file_logging(settings, actual_log)
        typer.echo(f"hostlens schedule daemon: logging to {actual_log}", err=True)
    else:
        configure_logging(settings.log_mode)

    import structlog

    logger = structlog.get_logger("hostlens.schedule.daemon" if daemon else "hostlens.schedule.run")

    # Flip daemon_mode so create_backend's existing gate fires (design D-12).
    # A daemon-unsafe backend (ClaudeSubscriptionBackend) raises here at boot.
    daemon_settings = settings.model_copy(update={"daemon_mode": True})

    target_registry = _build_target_registry(daemon_settings)
    inspector_registry = _build_inspector_registry(daemon_settings)
    manifests = _load_manifests_or_exit(daemon_settings, target_registry, inspector_registry)

    try:
        # Eager backend construction at boot so the daemon-safety gate fails
        # loudly before we enter the scheduling loop (rather than only on the
        # first fire). The instance is discarded; the runner builds its own
        # per-fire backends through the same factory.
        create_backend(daemon_settings)
    except BackendDaemonUnsafe as exc:
        _fail(f"backend not safe for daemon mode: {exc}")
    except ConfigError as exc:
        _fail_config(f"backend configuration error: {exc}")

    runner = _build_runner(daemon_settings, manifests, target_registry, inspector_registry, logger)
    asyncio.run(_serve_loop(runner, logger))


def _configure_file_logging(settings: Settings, log_file: Path) -> None:
    """Configure structlog JSON + redaction writing to ``log_file``.

    Reuses the project's shared processor chain (redact_sensitive at the head,
    JSON renderer) so credential values never reach the file; only the sink is
    swapped from stdout to the file handle.
    """

    import structlog

    from hostlens.core.logging import _shared_processors  # processor chain SOT

    handle = log_file.open("a", encoding="utf-8")
    structlog.configure(
        processors=[*_shared_processors(), structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(0),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=handle),
        cache_logger_on_first_use=False,
    )


async def _serve_loop(runner: SchedulerRunner, logger: structlog.stdlib.BoundLogger) -> None:
    """Start the scheduler, install signal handlers, await a stop signal."""

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _on_signal() -> None:
        # Idempotent: graceful_stop itself guards against re-entry; setting the
        # event twice is harmless.
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        # add_signal_handler is unavailable on some platforms / non-main
        # threads; the foreground run path still stops on KeyboardInterrupt.
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, _on_signal)

    runner.start()
    logger.info("scheduler.started", jobs=len(runner.scheduler.get_jobs()))
    try:
        await stop.wait()
    finally:
        logger.info("scheduler.stopping")
        await runner.graceful_stop()
        logger.info("scheduler.stopped")


# --------------------------------------------------------------------------- #
# `hostlens schedule status`
# --------------------------------------------------------------------------- #


@schedule_app.command("status")
def status_cmd(
    name: str | None = typer.Option(
        None,
        "--name",
        help="Filter to a single manifest (must be a loaded manifest name).",
    ),
    limit: int = typer.Option(20, "--limit", help="Max recent Run rows to show."),
    json_output: bool = typer.Option(
        False, "--json", help="Emit {runs, status_counts} JSON to stdout."
    ),
) -> None:
    """Show the most-recent ``Run`` rows + a status-count distribution.

    Empty history is exit 0 (a valid state). ``--name`` pointing at an
    unloaded manifest is fail-loud (exit 1), matching ``trigger``.
    """

    settings = _load_settings_or_exit()
    configure_logging(settings.log_mode)
    target_registry = _build_target_registry(settings)
    inspector_registry = _build_inspector_registry(settings)
    manifests = _load_manifests_or_exit(settings, target_registry, inspector_registry)

    if name is not None and name not in {m.name for m in manifests}:
        _fail(f"unknown schedule name: {name!r} (not in loaded manifests)")

    runs = asyncio.run(RunStore().list_recent(schedule_name=name, limit=limit))

    status_counts: dict[str, int] = {}
    for run in runs:
        key = str(run.status)
        status_counts[key] = status_counts.get(key, 0) + 1

    if json_output:
        payload = {
            "runs": [_run_json(run) for run in runs],
            "status_counts": status_counts,
        }
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if not runs:
        typer.echo("无 Run 记录")
        return

    for run in runs:
        typer.echo(
            f"{run.triggered_at.isoformat()}\t{run.run_id}\t{run.schedule_name}\t"
            f"{run.status}\treport_id={run.report_id or '<none>'}"
        )
    dist = " ".join(f"{k}={v}" for k, v in sorted(status_counts.items()))
    typer.echo(f"status_counts: [{dist}]")


def _run_json(run: Run) -> dict[str, object]:
    return {
        "run_id": run.run_id,
        "schedule_name": run.schedule_name,
        "triggered_at": run.triggered_at.isoformat(),
        "status": str(run.status),
        "report_id": run.report_id,
    }
