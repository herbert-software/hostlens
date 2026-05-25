"""Tests for the M1-integrated `list_inspectors` ToolSpec handler.

Covers the inspector-plugin-system spec MODIFIED block in
`tool-registry-capability-layer/spec.md` §需求:M2 首批 ToolSpec... §场景:
list_inspectors handler:

(a) Real registry containing builtin `hello.echo` + `system.uptime` →
    `ListInspectorsOutput.inspectors` has both entries, sorted by name in
    dictionary order; `tags` / `compatible_target_kinds` are themselves
    sorted dictionary order (prompt-cache prefix stability).
(b) `tag="linux"` filter returns only `system.uptime` (which carries the
    `linux` tag; `hello.echo` carries `demo` / `hello`).
(c) `target_kind="ssh"` filter returns both (both builtins declare
    `targets: [local, ssh]`).
(d) No filter returns both, sorted ascending by name.
"""

from __future__ import annotations

import asyncio

import structlog

from hostlens.core.config import Settings
from hostlens.inspectors.registry import (
    InspectorRegistry,
    build_registry_from_search_paths,
)
from hostlens.targets.registry import TargetRegistry
from hostlens.tools.base import NoopApprovalService, ToolContext
from hostlens.tools.default_tools import list_inspectors_handler
from hostlens.tools.schemas.list_inspectors import (
    ListInspectorsInput,
    ListInspectorsOutput,
)


def _make_inspector_registry() -> InspectorRegistry:
    return build_registry_from_search_paths([], settings=Settings()).registry


def _ctx(inspector_registry: InspectorRegistry) -> ToolContext:
    return ToolContext(
        target_registry=TargetRegistry(),
        inspector_registry=inspector_registry,
        config=Settings(),
        logger=structlog.get_logger("test_list_inspectors"),
        approval_service=NoopApprovalService(),
        cancel=asyncio.Event(),
    )


# ---------------------------------------------------------------------------
# (a) No filter — both builtins, sorted ascending; nested lists sorted too
# ---------------------------------------------------------------------------


def test_list_inspectors_no_filter_returns_both_builtins_sorted() -> None:
    ctx = _ctx(_make_inspector_registry())

    async def go() -> ListInspectorsOutput:
        return await list_inspectors_handler(ListInspectorsInput(), ctx)

    output = asyncio.run(go())
    names = [summary.name for summary in output.inspectors]
    assert names == ["hello.echo", "system.uptime"]
    for summary in output.inspectors:
        assert summary.tags == sorted(summary.tags)
        assert summary.compatible_target_kinds == sorted(
            summary.compatible_target_kinds
        )


# ---------------------------------------------------------------------------
# (b) tag="linux" → only system.uptime
# ---------------------------------------------------------------------------


def test_list_inspectors_tag_linux_filters_to_system_uptime_only() -> None:
    ctx = _ctx(_make_inspector_registry())

    async def go() -> ListInspectorsOutput:
        return await list_inspectors_handler(ListInspectorsInput(tag="linux"), ctx)

    output = asyncio.run(go())
    assert [s.name for s in output.inspectors] == ["system.uptime"]


# ---------------------------------------------------------------------------
# (c) target_kind="ssh" → both builtins
# ---------------------------------------------------------------------------


def test_list_inspectors_target_kind_ssh_returns_both() -> None:
    ctx = _ctx(_make_inspector_registry())

    async def go() -> ListInspectorsOutput:
        return await list_inspectors_handler(
            ListInspectorsInput(target_kind="ssh"), ctx
        )

    output = asyncio.run(go())
    assert [s.name for s in output.inspectors] == ["hello.echo", "system.uptime"]


# ---------------------------------------------------------------------------
# (d) Unknown tag → empty
# ---------------------------------------------------------------------------


def test_list_inspectors_unknown_tag_returns_empty() -> None:
    ctx = _ctx(_make_inspector_registry())

    async def go() -> ListInspectorsOutput:
        return await list_inspectors_handler(
            ListInspectorsInput(tag="nonexistent"), ctx
        )

    output = asyncio.run(go())
    assert output.inspectors == []
