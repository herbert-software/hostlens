"""Tests for `McpToolsAdapter.list_for_mcp` per mcp-tool-adapter spec."""

from __future__ import annotations

import pytest

from hostlens.core.exceptions import ToolPolicyViolation
from hostlens.mcp_server.tools_adapter import McpToolsAdapter
from hostlens.tools.registry import ToolRegistry

from ._helpers import TypedInput, TypedOutput, ctx_factory, make_spec, typed_ok_handler


def test_list_for_mcp_returns_only_mcp_surface_tools_with_mcp_description() -> None:
    reg = ToolRegistry()
    reg.register(
        make_spec(
            name="a",
            surfaces={"agent"},
            mcp_description="A-mcp",
            sensitive_output=False,
        )
    )
    reg.register(
        make_spec(
            name="b",
            surfaces={"agent", "mcp"},
            mcp_description="B-mcp",
            agent_description="B-agent",
            sensitive_output=False,
        )
    )
    reg.register(
        make_spec(
            name="c",
            surfaces={"mcp"},
            mcp_description="C-mcp",
            sensitive_output=False,
        )
    )
    adapter = McpToolsAdapter(reg, ctx_factory())

    tools = adapter.list_for_mcp()

    assert [tool.name for tool in tools] == ["b", "c"]
    b_tool = next(t for t in tools if t.name == "b")
    assert b_tool.description == "B-mcp"
    assert b_tool.description != "B-agent"


def test_list_for_mcp_input_schema_from_pydantic_projection() -> None:
    reg = ToolRegistry()
    spec = make_spec(
        name="typed_tool",
        input_schema=TypedInput,
        output_schema=TypedOutput,
        handler=typed_ok_handler,
        sensitive_output=False,
    )
    reg.register(spec)
    adapter = McpToolsAdapter(reg, ctx_factory())

    tools = adapter.list_for_mcp()

    assert len(tools) == 1
    assert tools[0].inputSchema == TypedInput.model_json_schema()


def test_list_for_mcp_empty_registry_returns_empty_list() -> None:
    adapter = McpToolsAdapter(ToolRegistry(), ctx_factory())
    assert adapter.list_for_mcp() == []


def test_list_for_mcp_no_mcp_surface_tools_returns_empty_list() -> None:
    reg = ToolRegistry()
    reg.register(make_spec(name="agent_only", surfaces={"agent"}, sensitive_output=False))
    reg.register(make_spec(name="cli_only", surfaces={"cli"}, sensitive_output=False))
    adapter = McpToolsAdapter(reg, ctx_factory())

    assert adapter.list_for_mcp() == []


def test_list_for_mcp_raises_when_sensitive_output_not_declared() -> None:
    reg = ToolRegistry()
    reg.register(make_spec(name="x", surfaces={"mcp"}, sensitive_output=None))
    adapter = McpToolsAdapter(reg, ctx_factory())

    with pytest.raises(ToolPolicyViolation) as ei:
        adapter.list_for_mcp()
    err = ei.value
    assert err.tool_name == "x"
    assert err.surface == "mcp"
    assert err.violated_field == "sensitive_output"
    assert err.reason == "sensitive_output_not_declared"


def test_list_for_mcp_accepts_explicit_sensitive_output_declarations() -> None:
    reg = ToolRegistry()
    reg.register(make_spec(name="y", surfaces={"mcp"}, sensitive_output=True))
    reg.register(make_spec(name="z", surfaces={"mcp"}, sensitive_output=False))
    adapter = McpToolsAdapter(reg, ctx_factory())

    tools = adapter.list_for_mcp()

    assert {tool.name for tool in tools} == {"y", "z"}
