"""MCP dispatch regression for default ToolSpecs (tasks 4.2 / 4.3).

Mirrors the agent-surface fixtures in ``tests/tools/test_list_targets_real_registry.py``
and ``tests/tools/test_run_inspector.py`` but routes through
``McpToolsAdapter.dispatch`` so TargetSummary redaction and
``allow_privileged=False`` enforcement hold on the mcp surface.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import cast

import pytest
import structlog

from hostlens.core.config import Settings
from hostlens.inspectors.registry import (
    InspectorRegistry,
    build_registry_from_search_paths,
)
from hostlens.inspectors.schema import (
    CollectSpec,
    InspectorManifest,
    ParseSpec,
)
from hostlens.mcp_server.tools_adapter import McpToolsAdapter
from hostlens.targets.base import ExecutionTarget
from hostlens.targets.config import LocalEntry, SSHEntry, TargetsConfig
from hostlens.targets.registry import TargetRegistry, build_registry_from_config
from hostlens.tools.base import NoopApprovalService, ToolContext
from hostlens.tools.default_tools import register_default_tools
from hostlens.tools.registry import ToolRegistry

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="LocalTarget requires POSIX (Linux/macOS)",
)


def _list_targets_ctx(registry: TargetRegistry) -> ToolContext:
    inspector_registry = build_registry_from_search_paths([], settings=Settings()).registry
    return ToolContext(
        target_registry=registry,
        inspector_registry=inspector_registry,
        config=Settings(),
        logger=structlog.get_logger("test_mcp_list_targets_regression"),
        approval_service=NoopApprovalService(),
        cancel=asyncio.Event(),
    )


def _two_target_registry() -> TargetRegistry:
    """Same fixture as ``test_list_targets_real_registry.test_two_target_scenario``."""
    config = TargetsConfig(
        version="1",
        targets=[
            LocalEntry(
                name="safe-local",
                type="local",
                display_name="Local Dev",
                tags=["dev"],
                enabled=True,
            ),
            SSHEntry(
                name="prod-ssh",
                type="ssh",
                host="example.invalid",
                user="ops",
                display_name="login as admin@10.0.0.5",
                tags=["prod"],
                enabled=True,
                password="TEST_PWD_NOT_A_REAL_SECRET_XYZ",  # pragma: allowlist secret
            ),
        ],
    )
    return build_registry_from_config(config, Settings())


def _make_inspector_registry() -> InspectorRegistry:
    return build_registry_from_search_paths([], settings=Settings()).registry


def _make_target_registry_with_local(name: str = "local-host") -> TargetRegistry:
    from hostlens.targets.local import LocalTarget

    registry = TargetRegistry()
    entry = LocalEntry(name=name, type="local", enabled=True)
    target: ExecutionTarget = cast("ExecutionTarget", LocalTarget(name=name))
    registry.register(target, entry)
    return registry


def _run_inspector_ctx(
    target_registry: TargetRegistry,
    inspector_registry: InspectorRegistry,
) -> ToolContext:
    return ToolContext(
        target_registry=target_registry,
        inspector_registry=inspector_registry,
        config=Settings(),
        logger=structlog.get_logger("test_mcp_run_inspector_regression"),
        approval_service=NoopApprovalService(),
        cancel=asyncio.Event(),
    )


def _mcp_adapter(ctx: ToolContext) -> McpToolsAdapter:
    tool_registry = ToolRegistry()
    register_default_tools(tool_registry)
    return McpToolsAdapter(tool_registry, lambda: ctx)


# ---------------------------------------------------------------------------
# 4.2 — list_targets redaction via McpToolsAdapter.dispatch
# ---------------------------------------------------------------------------


async def test_list_targets_mcp_dispatch_does_not_leak_sensitive_substrings() -> None:
    """Spec §场景:list_targets handler 投影脱敏 — mcp dispatch path.

    Reuses the two-target fixture (safe-local + sensitive prod-ssh) from
    ``tests/tools/test_list_targets_real_registry.py`` and asserts the dict
    returned by ``McpToolsAdapter.dispatch("list_targets", {}, ctx)`` does
    not contain credential / IP / identity substrings after JSON
    serialization.
    """
    registry = _two_target_registry()
    ctx = _list_targets_ctx(registry)
    adapter = _mcp_adapter(ctx)

    result = await adapter.dispatch("list_targets", {}, ctx)

    assert [t["name"] for t in result["targets"]] == ["safe-local"]
    summary = result["targets"][0]
    assert summary["display_name"] == "Local Dev"
    assert summary["tags"] == ["dev"]
    assert summary["kind"] == "local"
    assert summary["capabilities"] == ["file_read", "shell"]

    json_text = json.dumps(result)
    for needle in (
        "TEST_PWD_NOT_A_REAL_SECRET_XYZ",
        "10.0.0.5",
        "admin",
    ):  # pragma: allowlist secret
        assert needle not in json_text, (
            f"forbidden substring {needle!r} leaked into MCP dispatch JSON: {json_text}"
        )


# ---------------------------------------------------------------------------
# 4.3 — run_inspector allow_privileged=False via McpToolsAdapter.dispatch
# ---------------------------------------------------------------------------


@_POSIX_ONLY
async def test_run_inspector_mcp_dispatch_privilege_sudo_yields_empty_findings() -> None:
    """Spec §场景:run_inspector handler 在 mcp surface 同样强制 allow_privileged=False.

    Mirrors ``test_run_inspector_privilege_sudo_yields_empty_findings_on_agent_surface``
    but dispatches through ``McpToolsAdapter`` so MCP remote LLMs cannot opt-in
    to sudo/root inspectors.
    """
    target_registry = _make_target_registry_with_local("local-host")

    sudo_manifest = InspectorManifest.model_construct(
        name="sudo.test",
        version="1.0.0",
        description="sudo-required test manifest",
        tags=[],
        targets=["local"],
        requires_capabilities=[],
        requires_binaries=[],
        requires_files=[],
        privilege="sudo",
        parameters=None,
        secrets=[],
        collect=CollectSpec(command="echo hello", timeout_seconds=5),
        parse=ParseSpec(format="raw"),
        output_schema={
            "type": "object",
            "properties": {"raw": {"type": "string"}},
            "required": ["raw"],
            "additionalProperties": False,
        },
        findings=[],
    )

    inspector_registry = _make_inspector_registry()
    inspector_registry.register(sudo_manifest, source_path=None)
    ctx = _run_inspector_ctx(target_registry, inspector_registry)
    adapter = _mcp_adapter(ctx)

    result = await adapter.dispatch(
        "run_inspector",
        {"target_name": "local-host", "inspector_name": "sudo.test"},
        ctx,
    )

    assert result["target_name"] == "local-host"
    assert result["inspector_name"] == "sudo.test"
    assert result["findings"] == []
