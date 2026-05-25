"""Tests for ToolContext per spec §需求:ToolContext 必须包含 M2 字段最小集.

Covers:
1. Field set is exactly the M2 six-entry minimum (no more, no less).
2. ToolContext instances are immutable (frozen dataclass).
3. `approval_service` cannot be `None` — `NoopApprovalService` is the M2 default.
4. `target_registry` resolves to the real `hostlens.targets.registry.TargetRegistry`
   class (M1 landed in `add-execution-target-abstraction`).
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import get_type_hints

import pytest
import structlog

from hostlens.core.config import Settings
from hostlens.targets.registry import TargetRegistry
from hostlens.tools.base import (
    ApprovalService,
    NoopApprovalService,
    ToolContext,
)


class _StubInspectorRegistry:
    def list_summaries(self) -> list[object]:
        return []


def _make_settings() -> Settings:
    return Settings()


def _make_ctx(approval: ApprovalService | None = None) -> ToolContext:
    return ToolContext(
        target_registry=TargetRegistry(),
        inspector_registry=_StubInspectorRegistry(),
        config=_make_settings(),
        logger=structlog.get_logger("test"),
        approval_service=approval if approval is not None else NoopApprovalService(),
        cancel=asyncio.Event(),
    )


def test_tool_context_field_set_is_exactly_m2_minimum() -> None:
    field_names = {f.name for f in dataclasses.fields(ToolContext)}
    assert field_names == {
        "target_registry",
        "inspector_registry",
        "config",
        "logger",
        "approval_service",
        "cancel",
    }
    # Forbid any LLM-call entrypoint (ADR-008).
    forbidden = {"llm_backend", "anthropic_client", "messages_create"}
    assert field_names.isdisjoint(forbidden)


def test_tool_context_is_frozen() -> None:
    ctx = _make_ctx()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.logger = structlog.get_logger("other")  # type: ignore[misc]


def test_tool_context_approval_service_is_not_optional_in_type_hints() -> None:
    """`approval_service` is typed as `ApprovalService` (not `ApprovalService | None`).
    M2 forces callers to pass `NoopApprovalService` as the real placeholder so
    write-side handlers never need a `None` guard.
    """
    hints = get_type_hints(ToolContext)
    assert hints["approval_service"] is ApprovalService


def test_tool_context_target_registry_is_real_class() -> None:
    """Per execution-target spec §场景:target_registry 是真实 TargetRegistry 类型,
    `get_type_hints(ToolContext)["target_registry"]` must resolve to the
    real `hostlens.targets.registry.TargetRegistry` class — NOT a stub
    Protocol or `typing.Any`.

    We use `get_type_hints` (not `__annotations__`) on purpose: the
    module uses `from __future__ import annotations`, which means
    `__annotations__` would give us a string `"TargetRegistry"` rather
    than the resolved type object.
    """
    hints = get_type_hints(ToolContext)
    assert hints["target_registry"] is TargetRegistry


def test_noop_approval_service_always_refuses() -> None:
    from hostlens.core.exceptions import ToolPolicyViolation

    svc = NoopApprovalService()

    async def go() -> None:
        with pytest.raises(ToolPolicyViolation) as ei:
            await svc.request_approval("any", "any")
        err = ei.value
        assert err.tool_name == "noop_approval_service"
        assert err.surface == "agent"
        assert err.violated_field == "requires_approval"
        assert err.reason == "approval_flow_not_supported_in_m2"

    asyncio.run(go())


def test_noop_approval_service_fields_are_in_constrained_domain() -> None:
    """Spec §2.2 (b): NoopApprovalService raises a ToolPolicyViolation whose
    four fields are all drawn from constrained value domains (cannot leak
    user-supplied or secret data).
    """
    from typing import get_args

    from hostlens.core.exceptions import (
        ToolPolicyReason,
        ToolPolicySurface,
        ToolPolicyViolatedField,
        ToolPolicyViolation,
    )

    svc = NoopApprovalService()

    async def go() -> ToolPolicyViolation:
        try:
            await svc.request_approval("a", "b")
        except ToolPolicyViolation as e:
            return e
        raise AssertionError("expected ToolPolicyViolation")

    err = asyncio.run(go())
    assert err.surface in get_args(ToolPolicySurface)
    assert err.violated_field in get_args(ToolPolicyViolatedField)
    assert err.reason in get_args(ToolPolicyReason)
    # tool_name follows the ToolSpec regex
    import re

    assert re.match(r"^[a-z][a-z0-9_]*$", err.tool_name) is not None
