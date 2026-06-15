"""``hostlens mcp`` Typer subcommand group — stdio MCP Server.

Spec: ``openspec/changes/add-mcp-server-surface/specs/mcp-cli-command/spec.md``.

``mcp serve`` starts a foreground stdio MCP Server wired to the real
``ToolRegistry`` (``register_default_tools``) and a per-dispatch
``ToolContext`` factory. The official ``mcp`` SDK is an optional dependency:
when it is not installed the command prints an install hint to stderr and exits
1 without a Python traceback.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
import typer
from pydantic import ValidationError

from hostlens.core.config import Settings, load_settings
from hostlens.core.exceptions import BackendDaemonUnsafe, ConfigError, ToolPolicyViolation
from hostlens.core.logging import configure_logging
from hostlens.core.redact import redact_text
from hostlens.inspectors.registry import InspectorRegistry, build_registry_from_search_paths
from hostlens.notifiers.base import ChannelTypeRegistry, register_default_notifiers
from hostlens.notifiers.config import load_channels
from hostlens.reporting.store import ReportStore
from hostlens.scheduler.loader import load_schedules
from hostlens.scheduler.store import RunStore
from hostlens.targets.config import TargetsConfig, load_targets_config
from hostlens.targets.registry import TargetRegistry, build_registry_from_config
from hostlens.tools.base import NoopApprovalService, ToolContext
from hostlens.tools.default_tools import register_default_tools
from hostlens.tools.management_tools import (
    ManagementToolDeps,
    make_build_runner,
    make_daemon_safe_backend_factory,
    make_load_channel_summaries,
    register_mcp_management_tools,
)
from hostlens.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from collections.abc import Callable as _Callable

    from mcp.server import Server

    from hostlens.agent.backend import LLMBackend
    from hostlens.notifiers.base import Notifier

# cwd-relative schedules dir, matching ``cli/schedule.py`` ``_SCHEDULES_DIR``
# and the doctor ``_check_schedules`` convention. Resolved at call time.
_SCHEDULES_DIR = Path("schedules")

__all__ = ["MCP_INSTALL_HINT", "mcp_app"]

MCP_INSTALL_HINT = 'pip install "hostlens[mcp]"'

mcp_app = typer.Typer(
    name="mcp",
    help="Start the Hostlens MCP Server (stdio transport).",
    no_args_is_help=True,
    add_completion=False,
)


@mcp_app.callback()
def _root() -> None:
    """Force Typer into multi-command mode so ``serve`` stays addressable."""


def _import_mcp_server() -> tuple[
    Callable[[ToolRegistry, Callable[[], ToolContext]], Server],
    Callable[[Server], Coroutine[Any, Any, None]],
]:
    """Lazy-import MCP server symbols (optional ``mcp`` SDK dependency)."""

    from hostlens.mcp_server.server import build_server, run_stdio

    return build_server, run_stdio


def _load_settings_or_exit() -> Settings:
    try:
        return load_settings()
    except ConfigError as exc:
        typer.echo(f"hostlens: configuration error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


def _build_target_registry(settings: Settings) -> TargetRegistry:
    if not settings.targets_config_path.exists():
        return build_registry_from_config(TargetsConfig(version="1", targets=[]), settings)
    try:
        config = load_targets_config(settings.targets_config_path)
        return build_registry_from_config(config, settings)
    except (ConfigError, ValidationError) as exc:
        typer.echo(f"hostlens mcp serve: failed to load targets config: {exc}", err=True)
        raise typer.Exit(code=2) from exc


def _build_inspector_registry(settings: Settings) -> InspectorRegistry:
    return build_registry_from_search_paths(
        settings.inspectors_search_paths,
        settings=settings,
    ).registry


def _context_factory(
    settings: Settings,
    target_registry: TargetRegistry,
    inspector_registry: InspectorRegistry,
    logger: structlog.stdlib.BoundLogger,
) -> Callable[[], ToolContext]:
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


def _build_channels(settings: Settings) -> dict[str, Notifier]:
    """Load the notifier channels from ``notifiers.yaml`` (raises ``ConfigError``).

    Built inline (not imported from ``cli/schedule.py``) so ``cli/mcp.py`` does
    not reverse-depend on another CLI module's private ``_build_channels`` —
    each surface owns its own assembly. A missing / empty file yields an empty
    map; a malformed config raises ``ConfigError`` for the caller to map to an
    exit code (never a raw traceback).
    """

    registry = ChannelTypeRegistry()
    register_default_notifiers(registry)
    return load_channels(settings, registry)


def _build_management_deps(
    settings: Settings,
    target_registry: TargetRegistry,
    inspector_registry: InspectorRegistry,
    logger: structlog.stdlib.BoundLogger,
    backend_factory: _Callable[[], LLMBackend],
) -> ManagementToolDeps:
    """Assemble ``ManagementToolDeps`` from ``settings`` (may raise ``ConfigError``).

    ``run_store`` / ``report_store`` are constructed once and shared between the
    deps container and the ``build_runner`` factory (same instances). Channel
    loading is fail-loud: a malformed ``notifiers.yaml`` raises ``ConfigError``,
    which the caller maps to exit 2.
    """

    run_store = RunStore()
    report_store = ReportStore()
    channels = _build_channels(settings)
    build_runner = make_build_runner(
        settings=settings,
        run_store=run_store,
        report_store=report_store,
        channels=channels,
        backend_factory=backend_factory,
        logger=logger,
    )
    return ManagementToolDeps(
        load_manifests=lambda: load_schedules(_SCHEDULES_DIR, target_registry, inspector_registry),
        run_store=run_store,
        report_store=report_store,
        load_channel_summaries=make_load_channel_summaries(settings),
        build_runner=build_runner,
    )


@mcp_app.command("serve")
def serve_cmd() -> None:
    """Start the MCP Server on stdio (foreground; managed by the MCP host)."""

    # Assembly order is fixed (mcp-cli-command spec / design D-9): the mcp SDK
    # import check runs FIRST so a "SDK missing + notifiers.yaml unreadable"
    # combination deterministically exits 1 (SDK-missing wins) instead of being
    # pre-empted by the ConfigError → exit 2 path during deps construction.
    try:
        build_server, run_stdio = _import_mcp_server()
    except ImportError:
        typer.echo(
            f"hostlens mcp serve: MCP SDK not installed. Install with: {MCP_INSTALL_HINT}",
            err=True,
        )
        raise typer.Exit(code=1) from None

    settings = _load_settings_or_exit()
    configure_logging(settings.log_mode)
    logger = structlog.get_logger("hostlens.mcp.serve")

    target_registry = _build_target_registry(settings)
    inspector_registry = _build_inspector_registry(settings)

    registry = ToolRegistry()
    register_default_tools(registry)

    # Daemon-safe backend factory (mcp-cli-command spec / design D-8): the MCP
    # server is a long-running process accepting remote-LLM commands, so the
    # management-tool backend factory must flip ``daemon_mode=True`` to arm
    # ``create_backend``'s daemon-safety gate. The SAME factory instance feeds
    # both the boot-time eager probe below and every per-fire reconstruction
    # via ``build_runner`` (the same-source invariant).
    backend_factory = make_daemon_safe_backend_factory(settings)

    # Management-tool dependency construction is fail-loud: an unreadable /
    # malformed ``notifiers.yaml`` surfaces as ``ConfigError`` here, before any
    # running state — map it to a clean exit 2 (config error), never a raw
    # traceback (exit 1 stays reserved for SDK / policy / backend rejections).
    try:
        deps = _build_management_deps(
            settings, target_registry, inspector_registry, logger, backend_factory
        )
    except ConfigError as exc:
        typer.echo(
            f"hostlens mcp serve: configuration error: {redact_text(str(exc))}",
            err=True,
        )
        raise typer.Exit(code=2) from None

    register_mcp_management_tools(registry, deps=deps)

    context_factory = _context_factory(settings, target_registry, inspector_registry, logger)

    # Eager backend probe at boot (mcp-cli-command spec / design D-8): construct
    # one backend through the same daemon-safe factory so a daemon-unsafe /
    # unimplemented backend is rejected before we enter the running state
    # (rather than only on the first ``run_schedule_now``). The instance is
    # discarded; per-fire backends are rebuilt through the same factory.
    #   - BackendDaemonUnsafe / NotImplementedError → exit 1 (backend not
    #     available; same semantic class as the SDK-missing / policy rejections).
    #     ``NotImplementedError`` MUST be caught explicitly: the placeholder
    #     bedrock / vertex / claude_subscription backends raise it before the
    #     daemon gate, and the daemon ``_serve`` path does not catch it.
    #   - ConfigError → exit 2 (config error, e.g. backend block missing).
    try:
        backend_factory()
    except (BackendDaemonUnsafe, NotImplementedError) as exc:
        typer.echo(
            f"hostlens mcp serve: backend not available for serve — {redact_text(str(exc))}",
            err=True,
        )
        raise typer.Exit(code=1) from None
    except ConfigError as exc:
        typer.echo(
            f"hostlens mcp serve: backend configuration error: {redact_text(str(exc))}",
            err=True,
        )
        raise typer.Exit(code=2) from None

    # build_server eager-runs the fail-closed projection self-check; a registry
    # whose mcp-surface tool forgot to declare sensitive_output raises here.
    # Surface that as a clean exit, not a raw traceback.
    try:
        server = build_server(registry, context_factory)
    except ToolPolicyViolation as exc:
        typer.echo(f"hostlens mcp serve: refused to start — {exc}", err=True)
        raise typer.Exit(code=1) from None

    asyncio.run(run_stdio(server))
