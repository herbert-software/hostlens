"""Shared pytest fixtures for the Hostlens test suite.

`tool_registry` and `tool_context_factory` are the M2 fixtures used by
multiple test modules — each test that depends on them receives an
independent instance (function scope, no module-level state).

M1 migration: `tool_context_factory` now allocates a real
`hostlens.targets.registry.TargetRegistry` (empty by default; callers
can override via `target_registry=`) — the M2 stub `_StubTargetRegistry`
is gone (the real registry's API replaces `list_summaries()` with
`list()` + `get_entry()`). The `InspectorRegistry` stub remains until
the next proposal lands the real one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
import structlog

from hostlens.core.config import Settings
from hostlens.targets.config import LocalEntry, TargetsConfig
from hostlens.targets.registry import TargetRegistry, build_registry_from_config
from hostlens.tools.base import NoopApprovalService, ToolContext
from hostlens.tools.default_tools import register_default_tools
from hostlens.tools.registry import ToolRegistry


class _StubInspectorSummary:
    """Minimal stub matching `list_inspectors_handler`'s expected
    attribute shape — kept until the inspector plugin proposal lands.
    """

    def __init__(
        self,
        *,
        name: str = "stub-inspector",
        version: str = "1.0.0",
        description: str = "stub inspector for tests",
        tags: list[str] | None = None,
        compatible_target_kinds: list[str] | None = None,
    ) -> None:
        self.name = name
        self.version = version
        self.description = description
        self.tags = tags or []
        self.compatible_target_kinds = compatible_target_kinds or []


class _StubInspectorRegistry:
    """Default stub: one safe inspector so `list_inspectors_handler`
    returns a non-empty list under the fixture.
    """

    def __init__(self, inspectors: list[Any] | None = None) -> None:
        self._inspectors = inspectors if inspectors is not None else [_StubInspectorSummary()]

    def list_summaries(self) -> list[Any]:
        return list(self._inspectors)


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
    `stub-target` LocalTarget by default), a stub `InspectorRegistry`, a
    new `asyncio.Event`, and a new `NoopApprovalService`. Callers can
    pass `target_registry=` / `inspector_registry=` to override either
    while keeping the other dependencies stub-provided.
    """

    def _make(
        *,
        target_registry: TargetRegistry | None = None,
        inspector_registry: Any | None = None,
    ) -> ToolContext:
        return ToolContext(
            target_registry=target_registry or _default_target_registry(),
            inspector_registry=inspector_registry or _StubInspectorRegistry(),
            config=Settings(),
            logger=structlog.get_logger("tool_context_factory"),
            approval_service=NoopApprovalService(),
            cancel=asyncio.Event(),
        )

    return _make
