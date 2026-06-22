"""Cross-adapter policy gate consistency between ToolsAdapter and McpToolsAdapter."""

from __future__ import annotations

import pytest
from agent._helpers import make_spec as agent_make_spec

from hostlens.agent.tools_adapter import ToolsAdapter
from hostlens.core.exceptions import ToolPolicyViolation
from hostlens.mcp_server.tools_adapter import McpToolsAdapter
from hostlens.tools.registry import ToolRegistry

from ._helpers import ctx_factory, make_ctx


@pytest.mark.parametrize(
    ("side_effects", "expected_reason"),
    [
        ("write", "side_effects_not_permitted"),
        ("destructive", "side_effects_not_permitted"),
    ],
)
async def test_side_effects_gate_consistent_across_adapters(
    side_effects: str,
    expected_reason: str,
) -> None:
    reg = ToolRegistry()
    spec = agent_make_spec(
        name="dual_surface_write",
        surfaces={"agent", "mcp"},
        side_effects=side_effects,
        sensitive_output=False,
    )
    reg.register(spec)
    agent_adapter = ToolsAdapter(reg, ctx_factory())
    mcp_adapter = McpToolsAdapter(reg, ctx_factory())
    ctx = make_ctx()

    with pytest.raises(ToolPolicyViolation) as agent_ei:
        await agent_adapter.dispatch("dual_surface_write", {}, ctx)
    with pytest.raises(ToolPolicyViolation) as mcp_ei:
        await mcp_adapter.dispatch("dual_surface_write", {}, ctx)

    agent_err = agent_ei.value
    mcp_err = mcp_ei.value
    assert agent_err.tool_name == mcp_err.tool_name == "dual_surface_write"
    assert agent_err.violated_field == mcp_err.violated_field == "side_effects"
    assert agent_err.reason == mcp_err.reason == expected_reason
    assert agent_err.surface == "agent"
    assert mcp_err.surface == "mcp"


async def test_approval_gate_consistent_across_adapters() -> None:
    reg = ToolRegistry()
    spec = agent_make_spec(
        name="dual_surface_approval",
        surfaces={"agent", "mcp"},
        side_effects="read",
        requires_approval=True,
        sensitive_output=False,
    )
    reg.register(spec)
    agent_adapter = ToolsAdapter(reg, ctx_factory())
    mcp_adapter = McpToolsAdapter(reg, ctx_factory())
    ctx = make_ctx()

    with pytest.raises(ToolPolicyViolation) as agent_ei:
        await agent_adapter.dispatch("dual_surface_approval", {}, ctx)
    with pytest.raises(ToolPolicyViolation) as mcp_ei:
        await mcp_adapter.dispatch("dual_surface_approval", {}, ctx)

    agent_err = agent_ei.value
    mcp_err = mcp_ei.value
    assert agent_err.tool_name == mcp_err.tool_name == "dual_surface_approval"
    assert agent_err.violated_field == mcp_err.violated_field == "requires_approval"
    assert agent_err.reason == mcp_err.reason == "approval_flow_not_supported"
    assert agent_err.surface == "agent"
    assert mcp_err.surface == "mcp"
