"""Tests for `hostlens.mcp_server.server` per mcp-server spec."""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
import structlog
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, TextContent

from hostlens.core.config import Settings
from hostlens.core.exceptions import ToolPolicyViolation
from hostlens.inspectors.registry import build_registry_from_search_paths
from hostlens.mcp_server import server as server_mod
from hostlens.mcp_server.server import build_server, run_stdio
from hostlens.mcp_server.tools_adapter import McpToolsAdapter
from hostlens.targets.config import LocalEntry, TargetsConfig
from hostlens.targets.registry import build_registry_from_config
from hostlens.tools.base import NoopApprovalService, ToolContext
from hostlens.tools.default_tools import register_default_tools
from hostlens.tools.registry import ToolRegistry

from ._helpers import (
    EmptyInput,
    EmptyOutput,
    TypedInput,
    TypedOutput,
    ctx_factory,
    make_spec,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _REPO_ROOT / "src"

_handler_called = False


async def _tracking_handler(args: EmptyInput, ctx: ToolContext) -> EmptyOutput:
    global _handler_called
    _handler_called = True
    return EmptyOutput()


async def _leaky_handler(args: EmptyInput, ctx: ToolContext) -> EmptyOutput:
    raise RuntimeError("connect failed user=admin@10.0.0.5 sk-abcdefghijklmnopqrstuvwx")


def _make_tool_context() -> ToolContext:
    config = TargetsConfig(
        version="1",
        targets=[LocalEntry(name="stub-target", type="local", enabled=True)],
    )
    return ToolContext(
        target_registry=build_registry_from_config(config, Settings()),
        inspector_registry=build_registry_from_search_paths([], settings=Settings()).registry,
        config=Settings(),
        logger=structlog.get_logger("test_server"),
        approval_service=NoopApprovalService(),
        cancel=asyncio.Event(),
    )


def _default_registry_with_agent_only_probe() -> ToolRegistry:
    reg = ToolRegistry()
    register_default_tools(reg)
    reg.register(
        make_spec(
            name="agent_only_probe",
            surfaces={"agent"},
            sensitive_output=False,
            handler=_tracking_handler,
        )
    )
    return reg


@pytest.fixture(autouse=True)
def _reset_handler_called() -> None:
    global _handler_called
    _handler_called = False


async def test_list_tools_returns_mcp_surface_only_excludes_agent_only_probe(
    tool_context_factory: Callable[..., ToolContext],
) -> None:
    reg = _default_registry_with_agent_only_probe()
    server = build_server(reg, tool_context_factory)

    async with create_connected_server_and_client_session(server) as session:
        result = await session.list_tools()

    names = {tool.name for tool in result.tools}
    assert {"list_inspectors", "list_targets", "run_inspector"}.issubset(names)
    assert "agent_only_probe" not in names


def test_build_server_eager_raises_when_sensitive_output_not_declared() -> None:
    reg = ToolRegistry()
    reg.register(make_spec(name="undeclared_mcp", surfaces={"mcp"}, sensitive_output=None))

    with pytest.raises(ToolPolicyViolation) as ei:
        build_server(reg, ctx_factory())
    err = ei.value
    assert err.tool_name == "undeclared_mcp"
    assert err.violated_field == "sensitive_output"


async def test_call_tool_list_inspectors_success(
    tool_context_factory: Callable[..., ToolContext],
) -> None:
    reg = _default_registry_with_agent_only_probe()
    server = build_server(reg, tool_context_factory)

    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("list_inspectors", {})

    assert result.isError is False
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, TextContent)
    payload = json.loads(block.text)
    assert "inspectors" in payload
    assert isinstance(payload["inspectors"], list)
    assert len(payload["inspectors"]) >= 1


async def test_call_tool_unregistered_name_returns_is_error(
    tool_context_factory: Callable[..., ToolContext],
) -> None:
    reg = _default_registry_with_agent_only_probe()
    server = build_server(reg, tool_context_factory)

    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("totally_missing_tool", {})

    assert result.isError is True
    text = _text_from_result(result)
    assert "totally_missing_tool" in text or "missing" in text.lower()


async def test_call_tool_invalid_arguments_returns_is_error(
    tool_context_factory: Callable[..., ToolContext],
) -> None:
    reg = ToolRegistry()
    reg.register(
        make_spec(
            name="typed_tool",
            input_schema=TypedInput,
            output_schema=TypedOutput,
            handler=_tracking_handler,
            sensitive_output=False,
        )
    )
    server = build_server(reg, tool_context_factory)

    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("typed_tool", {"name": 123})

    assert result.isError is True
    # _tracking_handler flips the global flag if ever invoked; input validation
    # (TypeError) fires before the handler, so this stays False — falsifiable now.
    assert _handler_called is False


async def test_call_tool_policy_violation_agent_only_returns_is_error() -> None:
    reg = _default_registry_with_agent_only_probe()
    server = build_server(reg, _make_tool_context)

    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("agent_only_probe", {})

    assert result.isError is True
    assert _handler_called is False


async def test_call_tool_tool_error_returns_is_error() -> None:
    reg = ToolRegistry()
    reg.register(
        make_spec(
            name="bad_out",
            output_schema=TypedOutput,
            handler=_tracking_handler,
            sensitive_output=False,
        )
    )
    server = build_server(reg, ctx_factory())

    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("bad_out", {})

    assert result.isError is True
    assert _handler_called is True


async def test_call_tool_handler_exception_scrubbed_end_to_end() -> None:
    reg = ToolRegistry()
    reg.register(make_spec(name="leaky_tool", handler=_leaky_handler, sensitive_output=False))
    server = build_server(reg, ctx_factory())

    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("leaky_tool", {})

    assert result.isError is True
    text = _text_from_result(result)
    forbidden = ["admin", "10.0.0.5", "sk-abcdefghijklmnopqrstuvwx"]
    for needle in forbidden:
        assert needle not in text, f"leaked {needle!r} in {text!r}"


def test_build_server_signature_does_not_accept_llm_backend() -> None:
    sig = inspect.signature(build_server)
    param_names = set(sig.parameters)
    assert "llm_backend" not in param_names
    assert "backend" not in param_names


def test_mcp_tools_adapter_signature_does_not_accept_llm_backend() -> None:
    sig = inspect.signature(McpToolsAdapter.__init__)
    param_names = set(sig.parameters)
    assert "llm_backend" not in param_names
    assert "backend" not in param_names


def test_server_and_adapter_modules_do_not_import_llm_backend() -> None:
    # ADR-008: backend is AgentLoop-private. The contract is "no import / no
    # call", NOT "the string never appears" — a docstring may legitimately
    # name LLMBackend to explain *why* it is absent. So assert against import
    # statements (parsed from the AST), not raw substrings.
    for rel in ("hostlens/mcp_server/server.py", "hostlens/mcp_server/tools_adapter.py"):
        tree = ast.parse((_SRC_ROOT / rel).read_text(encoding="utf-8"))
        imported_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.ImportFrom, ast.Import)):
                imported_names.update(alias.name for alias in node.names)
        assert "LLMBackend" not in imported_names, f"{rel} must not import LLMBackend"
        assert not any("backend" in name.lower() for name in imported_names), (
            f"{rel} must not import any backend symbol"
        )


@asynccontextmanager
async def _empty_stdio_transport():
    read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(0)

    async def stdin_reader() -> None:
        async with read_stream_writer:
            pass

    async def stdout_writer() -> None:
        async with write_stream_reader:
            async for _session_message in write_stream_reader:
                pass

    async with anyio.create_task_group() as tg:
        tg.start_soon(stdin_reader)
        tg.start_soon(stdout_writer)
        yield read_stream, write_stream


async def test_run_stdio_returns_cleanly_on_stdin_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = ToolRegistry()
    server = build_server(reg, ctx_factory())
    monkeypatch.setattr(server_mod, "stdio_server", _empty_stdio_transport)

    await run_stdio(server)


def test_sigterm_exits_without_traceback() -> None:
    script = textwrap.dedent(
        """
        import asyncio
        import structlog
        from hostlens.core.config import Settings
        from hostlens.inspectors.registry import build_registry_from_search_paths
        from hostlens.mcp_server.server import build_server, run_stdio
        from hostlens.targets.config import LocalEntry, TargetsConfig
        from hostlens.targets.registry import TargetRegistry, build_registry_from_config
        from hostlens.tools.base import NoopApprovalService, ToolContext
        from hostlens.tools.default_tools import register_default_tools
        from hostlens.tools.registry import ToolRegistry

        def make_ctx() -> ToolContext:
            config = TargetsConfig(
                version="1",
                targets=[LocalEntry(name="stub-target", type="local", enabled=True)],
            )
            return ToolContext(
                target_registry=build_registry_from_config(config, Settings()),
                inspector_registry=build_registry_from_search_paths([], settings=Settings()).registry,
                config=Settings(),
                logger=structlog.get_logger("sigterm_test"),
                approval_service=NoopApprovalService(),
                cancel=asyncio.Event(),
            )

        reg = ToolRegistry()
        register_default_tools(reg)
        server = build_server(reg, make_ctx)
        asyncio.run(run_stdio(server))
        """
    ).strip()

    env = {**os.environ, "PYTHONPATH": str(_SRC_ROOT)}
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=_REPO_ROOT,
        env=env,
    )
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.05)
        assert proc.poll() is None, "server should stay alive with open stdin"

        proc.send_signal(signal.SIGTERM)
        _, stderr = proc.communicate(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)

    assert proc.returncode is not None
    assert b"Traceback (most recent call last)" not in stderr


def _text_from_result(result: CallToolResult) -> str:
    parts: list[str] = []
    for block in result.content:
        if isinstance(block, TextContent):
            parts.append(block.text)
    return "\n".join(parts)
