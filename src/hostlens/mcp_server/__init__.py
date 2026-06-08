"""MCP Server surface — official SDK bridge for remote LLM tool access.

Re-exports the MCP adapter and stdio server entry points. Importing this
package requires the optional ``mcp`` SDK (``pip install "hostlens[mcp]"``);
graceful handling when the SDK is absent lives in ``hostlens.cli.mcp`` (the
``hostlens mcp serve`` command), not here.
"""

from __future__ import annotations

from hostlens.mcp_server.server import build_server, run_stdio
from hostlens.mcp_server.tools_adapter import McpToolsAdapter

__all__ = ["McpToolsAdapter", "build_server", "run_stdio"]
