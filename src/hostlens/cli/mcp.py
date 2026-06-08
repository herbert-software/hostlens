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
from typing import TYPE_CHECKING, Any

import structlog
import typer
from pydantic import ValidationError

from hostlens.core.config import Settings, load_settings
from hostlens.core.exceptions import ConfigError, ToolPolicyViolation
from hostlens.core.logging import configure_logging
from hostlens.inspectors.registry import InspectorRegistry, build_registry_from_search_paths
from hostlens.targets.config import TargetsConfig, load_targets_config
from hostlens.targets.registry import TargetRegistry, build_registry_from_config
from hostlens.tools.base import NoopApprovalService, ToolContext
from hostlens.tools.default_tools import register_default_tools
from hostlens.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from mcp.server import Server

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


@mcp_app.command("serve")
def serve_cmd() -> None:
    """Start the MCP Server on stdio (foreground; managed by the MCP host)."""

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
    context_factory = _context_factory(settings, target_registry, inspector_registry, logger)

    # build_server eager-runs the fail-closed projection self-check; a registry
    # whose mcp-surface tool forgot to declare sensitive_output raises here.
    # Surface that as a clean exit, not a raw traceback.
    try:
        server = build_server(registry, context_factory)
    except ToolPolicyViolation as exc:
        typer.echo(f"hostlens mcp serve: refused to start — {exc}", err=True)
        raise typer.Exit(code=1) from None

    asyncio.run(run_stdio(server))
