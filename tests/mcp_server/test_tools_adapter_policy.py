"""Tests for `McpToolsAdapter.dispatch` policy gates per mcp-tool-adapter spec."""

from __future__ import annotations

import pytest

from hostlens.core.exceptions import ToolError, ToolPolicyViolation
from hostlens.mcp_server.tools_adapter import McpToolsAdapter
from hostlens.tools.base import ToolContext
from hostlens.tools.registry import ToolRegistry

from ._helpers import (
    EmptyInput,
    EmptyOutput,
    TypedInput,
    TypedOutput,
    ctx_factory,
    make_ctx,
    make_spec,
    typed_ok_handler,
)

_handler_called = False


async def _tracking_handler(args: EmptyInput, ctx: ToolContext) -> EmptyOutput:
    global _handler_called
    _handler_called = True
    return EmptyOutput()


@pytest.fixture(autouse=True)
def _reset_handler_called() -> None:
    global _handler_called
    _handler_called = False


async def test_dispatch_raises_when_spec_not_exposed_to_mcp_surface() -> None:
    reg = ToolRegistry()
    reg.register(make_spec(name="q", surfaces={"agent"}, sensitive_output=False))
    adapter = McpToolsAdapter(reg, ctx_factory())

    with pytest.raises(ToolPolicyViolation) as ei:
        await adapter.dispatch("q", {}, make_ctx())
    err = ei.value
    assert err.tool_name == "q"
    assert err.surface == "mcp"
    assert err.violated_field == "surfaces"
    assert err.reason == "not_exposed_to_surface"
    assert _handler_called is False


async def test_dispatch_raises_when_sensitive_output_not_declared_bypassing_list() -> None:
    reg = ToolRegistry()
    reg.register(
        make_spec(
            name="undeclared",
            surfaces={"mcp"},
            side_effects="read",
            requires_approval=False,
            sensitive_output=None,
            handler=_tracking_handler,
        )
    )
    adapter = McpToolsAdapter(reg, ctx_factory())

    with pytest.raises(ToolPolicyViolation) as ei:
        await adapter.dispatch("undeclared", {}, make_ctx())
    err = ei.value
    assert err.tool_name == "undeclared"
    assert err.surface == "mcp"
    assert err.violated_field == "sensitive_output"
    assert err.reason == "sensitive_output_not_declared"
    assert _handler_called is False


async def test_dispatch_raises_when_side_effects_is_write() -> None:
    reg = ToolRegistry()
    reg.register(
        make_spec(
            name="write_tool",
            side_effects="write",
            sensitive_output=False,
            handler=_tracking_handler,
        )
    )
    adapter = McpToolsAdapter(reg, ctx_factory())

    with pytest.raises(ToolPolicyViolation) as ei:
        await adapter.dispatch("write_tool", {}, make_ctx())
    err = ei.value
    assert err.tool_name == "write_tool"
    assert err.surface == "mcp"
    assert err.violated_field == "side_effects"
    assert err.reason == "side_effects_not_permitted"
    assert _handler_called is False


async def test_dispatch_raises_when_requires_approval_is_true() -> None:
    reg = ToolRegistry()
    reg.register(
        make_spec(
            name="approval_tool",
            side_effects="read",
            requires_approval=True,
            sensitive_output=False,
            handler=_tracking_handler,
        )
    )
    adapter = McpToolsAdapter(reg, ctx_factory())

    with pytest.raises(ToolPolicyViolation) as ei:
        await adapter.dispatch("approval_tool", {}, make_ctx())
    err = ei.value
    assert err.tool_name == "approval_tool"
    assert err.surface == "mcp"
    assert err.violated_field == "requires_approval"
    assert err.reason == "approval_flow_not_supported"
    assert _handler_called is False


async def test_dispatch_raises_type_error_on_invalid_args_json() -> None:
    reg = ToolRegistry()
    reg.register(
        make_spec(
            name="typed_tool",
            input_schema=TypedInput,
            output_schema=TypedOutput,
            handler=typed_ok_handler,
            sensitive_output=False,
        )
    )
    adapter = McpToolsAdapter(reg, ctx_factory())

    with pytest.raises(TypeError):
        await adapter.dispatch("typed_tool", {"name": 123}, make_ctx())


async def test_dispatch_raises_tool_error_on_output_schema_mismatch() -> None:
    reg = ToolRegistry()
    reg.register(
        make_spec(
            name="bad_out",
            output_schema=TypedOutput,
            handler=_tracking_handler,
            sensitive_output=False,
        )
    )
    adapter = McpToolsAdapter(reg, ctx_factory())

    with pytest.raises(ToolError):
        await adapter.dispatch("bad_out", {}, make_ctx())


async def test_dispatch_returns_model_dump_on_success_without_is_error() -> None:
    reg = ToolRegistry()
    reg.register(
        make_spec(
            name="typed_tool",
            input_schema=TypedInput,
            output_schema=TypedOutput,
            handler=typed_ok_handler,
            sensitive_output=False,
        )
    )
    adapter = McpToolsAdapter(reg, ctx_factory())

    out = await adapter.dispatch("typed_tool", {"name": "alpha", "version": "1.0.0"}, make_ctx())

    assert isinstance(out, dict)
    assert out == {"ok": True}
    assert "is_error" not in out
