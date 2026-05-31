"""Tests for the M2 first-batch ToolSpec policy metadata per spec
§需求:M2 首批 ToolSpec 必须含 ....

Three scenarios — one per ToolSpec — locking down the exact policy
metadata table that surface adapters (agent / mcp / cli) will consume.
"""

from __future__ import annotations

from datetime import UTC, datetime

from hostlens.tools.default_tools import register_default_tools
from hostlens.tools.registry import ToolRegistry


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_default_tools(reg)
    return reg


def test_run_inspector_metadata_matches_spec_table() -> None:
    spec = _registry().get("run_inspector")
    assert spec.surfaces == {"agent"}
    assert spec.side_effects == "read"
    assert spec.sensitive_output is True
    assert spec.requires_approval is False
    assert spec.timeout == 30.0
    assert spec.version == "1.0.0"
    assert spec.cli_help is None


def test_list_inspectors_metadata_matches_spec_table() -> None:
    spec = _registry().get("list_inspectors")
    assert spec.surfaces == {"agent"}
    assert spec.side_effects == "none"
    assert spec.sensitive_output is False
    assert spec.requires_approval is False
    assert spec.timeout == 5.0
    assert spec.version == "1.0.0"
    assert spec.cli_help is None


def test_list_targets_metadata_matches_spec_table() -> None:
    spec = _registry().get("list_targets")
    assert spec.surfaces == {"agent"}
    assert spec.side_effects == "none"
    assert spec.sensitive_output is True
    assert spec.requires_approval is False
    assert spec.timeout == 5.0
    assert spec.version == "1.0.0"
    assert spec.cli_help is None


def test_clock_bound_run_inspector_is_policy_identical() -> None:
    """incident-pack Option C: registering with a clock must register a
    `run_inspector` whose policy metadata is byte-identical to the default —
    only the handler's clock injection may differ, never the policy surface."""
    default_spec = _registry().get("run_inspector")

    clock_reg = ToolRegistry()
    register_default_tools(clock_reg, clock=lambda: datetime(2026, 1, 2, 12, 0, 0, tzinfo=UTC))
    clock_spec = clock_reg.get("run_inspector")

    assert clock_spec.name == default_spec.name
    assert clock_spec.version == default_spec.version
    assert clock_spec.surfaces == default_spec.surfaces
    assert clock_spec.side_effects == default_spec.side_effects
    assert clock_spec.sensitive_output == default_spec.sensitive_output
    assert clock_spec.requires_approval == default_spec.requires_approval
    assert clock_spec.timeout == default_spec.timeout
    assert clock_spec.cli_help == default_spec.cli_help
    assert clock_spec.input_schema is default_spec.input_schema
    assert clock_spec.output_schema is default_spec.output_schema
    assert clock_spec.agent_description == default_spec.agent_description
    assert clock_spec.mcp_description == default_spec.mcp_description
