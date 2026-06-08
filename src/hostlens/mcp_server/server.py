"""MCP Server — official SDK bridge to :class:`McpToolsAdapter`.

Exposes ``list_tools`` / ``call_tool`` over the MCP tools protocol via the
official ``mcp`` SDK. The server is a **tool provider** for remote LLMs — it
must never construct Anthropic requests or hold an :class:`LLMBackend`
(ADR-008).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ContentBlock, TextContent, Tool

from hostlens.agent.tools_adapter import scrub_exception_message
from hostlens.core.exceptions import ToolError, ToolPolicyViolation
from hostlens.mcp_server.tools_adapter import McpToolsAdapter
from hostlens.tools.base import ToolContext
from hostlens.tools.registry import ToolRegistry

__all__ = ["build_server", "run_stdio"]


def _error_result(message: str) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=message)],
        isError=True,
    )


def build_server(
    registry: ToolRegistry,
    context_factory: Callable[[], ToolContext],
) -> Server:
    """Build an MCP ``Server`` wired to *registry* via :class:`McpToolsAdapter`.

    Eagerly calls ``list_for_mcp()`` once at construction so registry specs
    with ``surfaces ∋ "mcp"`` but undeclared ``sensitive_output`` fail
    closed before the server enters a running state.
    """
    adapter = McpToolsAdapter(registry, context_factory)
    adapter.list_for_mcp()

    server = Server("hostlens")

    # mcp SDK is forced to Any (pyproject mypy override), so its decorators are
    # untyped; the handlers themselves are fully typed below.
    @server.list_tools()  # type: ignore[untyped-decorator]
    async def handle_list_tools() -> list[Tool]:
        return adapter.list_for_mcp()

    @server.call_tool(validate_input=False)  # type: ignore[untyped-decorator]
    async def handle_call_tool(
        name: str,
        arguments: dict[str, Any] | None,
    ) -> list[ContentBlock] | CallToolResult:
        # The MCP SDK passes arguments=None when a client calls a tool with no
        # params; normalize so no-arg tools (e.g. list_inspectors) validate an
        # empty dict instead of erroring on model_validate(None).
        ctx = context_factory()
        try:
            result = await adapter.dispatch(name, arguments or {}, ctx)
        except asyncio.CancelledError:
            raise
        except (ToolPolicyViolation, KeyError, TypeError, ToolError) as exc:
            return _error_result(scrub_exception_message(str(exc)))

        if result.get("is_error") is True:
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
                isError=True,
            )
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    return server


async def run_stdio(server: Server) -> None:
    """Run *server* on the official stdio transport until stdin EOF."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )
