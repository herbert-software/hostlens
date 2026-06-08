"""Tests for `McpToolsAdapter.dispatch` exception wrapping per mcp-tool-adapter spec."""

from __future__ import annotations

import asyncio

import pytest

from hostlens.core.exceptions import ToolPolicyViolation
from hostlens.mcp_server.tools_adapter import McpToolsAdapter
from hostlens.tools.base import ToolContext
from hostlens.tools.registry import ToolRegistry

from ._helpers import EmptyInput, EmptyOutput, ctx_factory, make_ctx, make_spec


async def _runtime_error_handler(args: EmptyInput, ctx: ToolContext) -> EmptyOutput:
    raise RuntimeError("connect failed user=admin@10.0.0.5 sk-abcdefghijklmnopqrstuvwx")


async def _policy_violation_handler(args: EmptyInput, ctx: ToolContext) -> EmptyOutput:
    raise ToolPolicyViolation(
        tool_name="handler_raised_tool",
        surface="mcp",
        violated_field="target_constraints",
        reason="target_constraint_violated",
    )


async def _cancelled_handler(args: EmptyInput, ctx: ToolContext) -> EmptyOutput:
    raise asyncio.CancelledError("simulated Ctrl-C")


async def test_handler_runtime_error_is_wrapped_and_scrubbed() -> None:
    reg = ToolRegistry()
    reg.register(
        make_spec(name="leaky_tool", handler=_runtime_error_handler, sensitive_output=False)
    )
    adapter = McpToolsAdapter(reg, ctx_factory())

    result = await adapter.dispatch("leaky_tool", {}, make_ctx())

    assert result["is_error"] is True
    assert result["error_kind"] == "RuntimeError"
    assert result["tool_name"] == "leaky_tool"

    message = str(result["message"])
    cause = str(result["cause"])
    forbidden = ["admin", "10.0.0.5", "sk-abcdefghijklmnopqrstuvwx"]
    for needle in forbidden:
        assert needle not in message, f"message leaked {needle!r}: {message!r}"
        assert needle not in cause, f"cause leaked {needle!r}: {cause!r}"


async def test_handler_tool_policy_violation_propagates_unwrapped() -> None:
    reg = ToolRegistry()
    reg.register(
        make_spec(name="policy_tool", handler=_policy_violation_handler, sensitive_output=False)
    )
    adapter = McpToolsAdapter(reg, ctx_factory())

    with pytest.raises(ToolPolicyViolation) as ei:
        await adapter.dispatch("policy_tool", {}, make_ctx())
    err = ei.value
    assert err.tool_name == "handler_raised_tool"
    assert err.violated_field == "target_constraints"
    assert err.reason == "target_constraint_violated"


async def test_handler_cancelled_error_propagates_unwrapped() -> None:
    reg = ToolRegistry()
    reg.register(
        make_spec(name="cancelled_tool", handler=_cancelled_handler, sensitive_output=False)
    )
    adapter = McpToolsAdapter(reg, ctx_factory())

    with pytest.raises(asyncio.CancelledError):
        await adapter.dispatch("cancelled_tool", {}, make_ctx())


async def test_dispatch_key_error_propagates_unwrapped() -> None:
    adapter = McpToolsAdapter(ToolRegistry(), ctx_factory())

    with pytest.raises(KeyError):
        await adapter.dispatch("missing_tool", {}, make_ctx())


async def test_dispatch_timeout_returns_error_envelope() -> None:
    reg = ToolRegistry()

    async def _slow_handler(args: EmptyInput, ctx: ToolContext) -> EmptyOutput:
        await asyncio.sleep(5)
        return EmptyOutput()

    reg.register(
        make_spec(name="slow_tool", timeout=0.5, handler=_slow_handler, sensitive_output=False)
    )
    adapter = McpToolsAdapter(reg, ctx_factory())

    result = await adapter.dispatch("slow_tool", {}, make_ctx())

    assert result["is_error"] is True
    assert result["error_kind"] == "TimeoutError"
    assert result["tool_name"] == "slow_tool"
    assert "message" in result
    assert "cause" in result
