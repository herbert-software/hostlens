"""End-to-end integration test for the M2 Tool Registry Demo Path.

Covers the three steps documented in
`openspec/changes/add-tool-registry-capability-layer/proposal.md` (Demo
Path section):

1. `register_default_tools(registry)` produces exactly the three M2
   tool names.
2. `ToolsAdapter(registry, ...).list_for_agent()` projects each spec
   into an Anthropic-compatible `{name, description, input_schema}`
   dict.
3. `await adapter.dispatch("list_inspectors", {}, ctx)` traverses the
   full policy gate → handler invocation → output dump path using a
   stub `inspector_registry`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from hostlens.agent.tools_adapter import ToolsAdapter
from hostlens.tools import ToolContext, ToolRegistry, register_default_tools


def test_register_default_tools_yields_exactly_three_names() -> None:
    registry = ToolRegistry()
    register_default_tools(registry)

    assert registry.names() == {"run_inspector", "list_inspectors", "list_targets"}


def test_list_for_agent_projects_three_anthropic_tool_dicts(
    tool_registry: ToolRegistry,
    tool_context_factory: Callable[..., ToolContext],
) -> None:
    adapter = ToolsAdapter(tool_registry, tool_context_factory)
    projections = adapter.list_for_agent()

    # Length: exactly three M2 specs are projected onto the agent surface.
    assert len(projections) == 3

    # Each entry exposes exactly the three Anthropic-required top-level
    # keys (name / description / input_schema), in insertion order.
    for entry in projections:
        assert list(entry.keys()) == ["name", "description", "input_schema"]
        assert isinstance(entry["name"], str)
        assert isinstance(entry["description"], str)
        assert isinstance(entry["input_schema"], dict)

        # input_schema must be a JSON-Schema object describing a Pydantic
        # model: object type + a `properties` mapping.
        schema = entry["input_schema"]
        assert schema.get("type") == "object"
        assert "properties" in schema
        assert isinstance(schema["properties"], dict)


@pytest.mark.asyncio
async def test_dispatch_list_inspectors_walks_full_handler_path(
    tool_registry: ToolRegistry,
    tool_context_factory: Callable[..., ToolContext],
) -> None:
    adapter = ToolsAdapter(tool_registry, tool_context_factory)

    # The default `tool_context_factory` wires a real `InspectorRegistry`
    # populated from the builtin search path (hello.echo + system.uptime).
    # That's enough to walk the entire dispatch path (gates → handler →
    # model_dump).
    ctx = tool_context_factory()
    result: dict[str, Any] = await adapter.dispatch("list_inspectors", {}, ctx)

    # `dispatch` always returns a serialised dict (handler output is
    # `model_dump`-ed). The `list_inspectors` output schema exposes a
    # single `inspectors` key.
    assert isinstance(result, dict)
    assert "inspectors" in result
    assert isinstance(result["inspectors"], list)
    # The default real `InspectorRegistry` ships two M1 builtins —
    # confirming the handler surfaces them as a non-empty list.
    names = [entry["name"] for entry in result["inspectors"]]
    assert names == ["hello.echo", "system.uptime"]
