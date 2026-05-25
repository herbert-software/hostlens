"""Shared pytest fixtures for the Hostlens test suite.

`tool_registry` and `tool_context_factory` are the M2 fixtures used by
multiple test modules — each test that depends on them receives an
independent instance (function scope, no module-level state).

M1 migration: `tool_context_factory` allocates a real
`hostlens.targets.registry.TargetRegistry` (with one `stub-target`
LocalTarget by default) **and** a real
`hostlens.inspectors.registry.InspectorRegistry` populated by
`build_registry_from_search_paths([], settings=Settings())` (builtin
hello.echo + system.uptime). Both stub fallbacks (`_StubTargetRegistry`,
`_StubInspectorRegistry`) are gone — per
`add-inspector-plugin-system` spec §需求:M2 首批 ToolSpec... §场景:
list_inspectors handler 投影真实 InspectorRegistry 数据, tests must use
the real registry types so the `ToolContext` field-type contract is
exercised end-to-end.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
import structlog

from hostlens.core.config import Settings
from hostlens.inspectors.registry import (
    InspectorRegistry,
    build_registry_from_search_paths,
)
from hostlens.targets.config import LocalEntry, TargetsConfig
from hostlens.targets.registry import TargetRegistry, build_registry_from_config
from hostlens.tools.base import NoopApprovalService, ToolContext
from hostlens.tools.default_tools import register_default_tools
from hostlens.tools.registry import ToolRegistry


def _default_target_registry() -> TargetRegistry:
    """Build a registry with a single safe LocalTarget so the default
    `list_targets_handler` path returns a non-empty list under the
    fixture. Callers needing custom topology pass their own registry
    via `target_registry=`.
    """
    config = TargetsConfig(
        version="1",
        targets=[LocalEntry(name="stub-target", type="local", enabled=True)],
    )
    return build_registry_from_config(config, Settings())


def _default_inspector_registry() -> InspectorRegistry:
    """Build the real `InspectorRegistry` from the builtin search path
    only (no user paths). M1 builtins are `hello.echo` + `system.uptime`,
    so the default fixture has two inspectors available — enough to
    exercise `list_inspectors_handler` without forcing each test to wire
    its own registry.
    """
    return build_registry_from_search_paths([], settings=Settings()).registry


@pytest.fixture
def tool_registry() -> ToolRegistry:
    """A fresh `ToolRegistry` with the M2 default ToolSpec batch
    pre-registered. Each test receives its own instance — mutating the
    fixture cannot leak to other tests.
    """
    reg = ToolRegistry()
    register_default_tools(reg)
    return reg


@pytest.fixture
def tool_context_factory() -> Callable[..., ToolContext]:
    """Return a callable that produces a fresh `ToolContext` per call.

    Each invocation allocates a fresh real `TargetRegistry` (with one
    `stub-target` LocalTarget by default), a real `InspectorRegistry`
    populated from the builtin search path, a new `asyncio.Event`, and a
    new `NoopApprovalService`. Callers can pass `target_registry=` /
    `inspector_registry=` to override either while keeping the other
    dependencies fixture-provided.
    """

    def _make(
        *,
        target_registry: TargetRegistry | None = None,
        inspector_registry: InspectorRegistry | None = None,
    ) -> ToolContext:
        return ToolContext(
            target_registry=target_registry or _default_target_registry(),
            inspector_registry=inspector_registry or _default_inspector_registry(),
            config=Settings(),
            logger=structlog.get_logger("tool_context_factory"),
            approval_service=NoopApprovalService(),
            cancel=asyncio.Event(),
        )

    return _make
