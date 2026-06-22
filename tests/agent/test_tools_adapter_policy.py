"""Tests for `ToolsAdapter.dispatch` policy gate per spec
§需求:ToolsAdapter.dispatch 必须执行 policy gate.

Five scenarios:
1. Surface mismatch raises `ToolPolicyViolation`
   (violated_field="surfaces", reason="not_exposed_to_surface").
2. `side_effects ∈ {"write", "destructive"}` raises `ToolPolicyViolation`
   (violated_field="side_effects", reason="side_effects_not_permitted").
   **Covered with BOTH "write" and "destructive" cases.**
3. `requires_approval=True` raises `ToolPolicyViolation`
   (violated_field="requires_approval",
   reason="approval_flow_not_supported").
4. Invalid args_json raises `TypeError` (NOT `ToolPolicyViolation`).
5. Success path returns `result.model_dump()`.
"""

from __future__ import annotations

import asyncio

import pytest

from hostlens.agent.tools_adapter import ToolsAdapter
from hostlens.core.exceptions import ToolPolicyViolation
from hostlens.tools.registry import ToolRegistry

from ._helpers import (
    TypedInput,
    TypedOutput,
    ctx_factory,
    make_ctx,
    make_spec,
    typed_ok_handler,
)


def test_dispatch_raises_when_spec_not_exposed_to_agent_surface() -> None:
    reg = ToolRegistry()
    reg.register(make_spec(name="mcp_only", surfaces={"mcp"}))
    adapter = ToolsAdapter(reg, ctx_factory())

    async def go() -> None:
        with pytest.raises(ToolPolicyViolation) as ei:
            await adapter.dispatch("mcp_only", {}, make_ctx())
        err = ei.value
        assert err.tool_name == "mcp_only"
        assert err.surface == "agent"
        assert err.violated_field == "surfaces"
        assert err.reason == "not_exposed_to_surface"

    asyncio.run(go())


def test_dispatch_raises_when_side_effects_is_write() -> None:
    reg = ToolRegistry()
    reg.register(make_spec(name="write_tool", side_effects="write"))
    adapter = ToolsAdapter(reg, ctx_factory())

    async def go() -> None:
        with pytest.raises(ToolPolicyViolation) as ei:
            await adapter.dispatch("write_tool", {}, make_ctx())
        err = ei.value
        assert err.violated_field == "side_effects"
        assert err.reason == "side_effects_not_permitted"

    asyncio.run(go())


def test_dispatch_raises_when_side_effects_is_destructive() -> None:
    reg = ToolRegistry()
    reg.register(make_spec(name="destructive_tool", side_effects="destructive"))
    adapter = ToolsAdapter(reg, ctx_factory())

    async def go() -> None:
        with pytest.raises(ToolPolicyViolation) as ei:
            await adapter.dispatch("destructive_tool", {}, make_ctx())
        err = ei.value
        assert err.violated_field == "side_effects"
        assert err.reason == "side_effects_not_permitted"

    asyncio.run(go())


def test_dispatch_raises_when_requires_approval_is_true() -> None:
    reg = ToolRegistry()
    reg.register(make_spec(name="approval_tool", side_effects="read", requires_approval=True))
    adapter = ToolsAdapter(reg, ctx_factory())

    async def go() -> None:
        with pytest.raises(ToolPolicyViolation) as ei:
            await adapter.dispatch("approval_tool", {}, make_ctx())
        err = ei.value
        assert err.violated_field == "requires_approval"
        assert err.reason == "approval_flow_not_supported"

    asyncio.run(go())


def test_dispatch_raises_type_error_on_invalid_args_json() -> None:
    reg = ToolRegistry()
    reg.register(
        make_spec(
            name="typed_tool",
            input_schema=TypedInput,
            output_schema=TypedOutput,
            handler=typed_ok_handler,
        )
    )
    adapter = ToolsAdapter(reg, ctx_factory())

    async def go() -> None:
        # TypedInput requires {name: str, version: str}; we send int for name.
        with pytest.raises(TypeError):
            await adapter.dispatch("typed_tool", {"name": 123}, make_ctx())

    asyncio.run(go())


def test_dispatch_returns_model_dump_on_success() -> None:
    reg = ToolRegistry()
    reg.register(
        make_spec(
            name="typed_tool",
            input_schema=TypedInput,
            output_schema=TypedOutput,
            handler=typed_ok_handler,
        )
    )
    adapter = ToolsAdapter(reg, ctx_factory())

    async def go() -> None:
        out = await adapter.dispatch(
            "typed_tool", {"name": "alpha", "version": "1.0.0"}, make_ctx()
        )
        assert isinstance(out, dict)
        # TypedOutput has a single bool field `ok`
        assert out == {"ok": True}

    asyncio.run(go())
