"""Tests for `list_targets_handler` capability allowlist enforcement.

A target carrying a non-allowlisted capability (e.g. an injected
out-of-band token / future enum member that hasn't been allowlisted
yet) must NOT surface that capability to the agent. The handler
silently drops tokens outside `CAPABILITY_ALLOWLIST` (defined in
`hostlens.tools.schemas.list_targets`).

We use fake `ExecutionTarget` classes rather than `LocalTarget` /
`SSHTarget` because:

- `LocalTarget` / `SSHTarget` derive `capabilities` from runtime
  probes / static baselines that this allowlist test wants full
  control over.
- Forcing those concrete classes into surrogate capability sets would
  obscure the actual contract we're testing.
"""

from __future__ import annotations

import asyncio
import enum
from typing import Any, Literal

import structlog

from hostlens.core.config import Settings
from hostlens.targets.base import Capability
from hostlens.targets.config import LocalEntry
from hostlens.targets.registry import TargetRegistry
from hostlens.tools.base import NoopApprovalService, ToolContext
from hostlens.tools.default_tools import list_targets_handler
from hostlens.tools.schemas.list_targets import (
    CAPABILITY_ALLOWLIST,
    ListTargetsInput,
)


class _NotACapability(enum.Enum):
    """An enum that is *not* `Capability` — used to simulate a future or
    out-of-band capability token that hasn't been promoted into the M1
    `Capability` set / `CAPABILITY_ALLOWLIST` yet.
    """

    INTERNAL_ADMIN_ROOT = "internal_admin_root"
    SECRET_CAPABILITY = "secret_capability"


class _FakeTarget:
    """Bare-bones `ExecutionTarget` impl letting tests inject arbitrary
    capability sets (including non-`Capability` members) and exact
    `type` literals.

    `LocalTarget` doesn't let us synthesize non-allowlisted tokens — its
    capability set is enum-typed and probed at runtime. We need the
    freedom here to verify the handler's defence against (a) future
    enum members not in the allowlist and (b) accidental bare-string
    capability injection.
    """

    def __init__(
        self,
        *,
        name: str,
        kind: Literal["local", "ssh", "docker", "k8s"] = "local",
        capabilities: set[Any],
    ) -> None:
        self.name = name
        self.type = kind
        self.capabilities = capabilities

    async def exec(  # pragma: no cover - never called by list_targets
        self, cmd: str, *, timeout: int, env: dict[str, str] | None = None
    ) -> object:
        raise NotImplementedError

    async def read_file(self, path: str) -> bytes:  # pragma: no cover
        raise NotImplementedError


def _make_registry(target: _FakeTarget) -> TargetRegistry:
    registry = TargetRegistry()
    entry = LocalEntry(name=target.name, type="local", enabled=True)
    registry.register(target, entry)  # type: ignore[arg-type]
    return registry


def _ctx_with(target: _FakeTarget) -> ToolContext:
    return ToolContext(
        target_registry=_make_registry(target),
        inspector_registry=_StubInspectorRegistry(),
        config=Settings(),
        logger=structlog.get_logger("test_capabilities_allowlist"),
        approval_service=NoopApprovalService(),
        cancel=asyncio.Event(),
    )


class _StubInspectorRegistry:
    def list_summaries(self) -> list[object]:
        return []


def test_non_allowlisted_capability_is_dropped() -> None:
    target = _FakeTarget(
        name="prod-web",
        capabilities={
            Capability.SHELL,
            Capability.FILE_READ,
            _NotACapability.INTERNAL_ADMIN_ROOT,
        },
    )
    ctx = _ctx_with(target)
    out = asyncio.run(list_targets_handler(ListTargetsInput(), ctx))
    assert len(out.targets) == 1
    summary = out.targets[0]
    # Order is lexicographic (sorted) per the handler contract.
    assert summary.capabilities == ["file_read", "shell"]
    assert "internal_admin_root" not in summary.capabilities


def test_allowlist_only_capabilities_survive() -> None:
    """Every capability we feed in is allowlisted; all should survive."""
    target = _FakeTarget(
        name="prod-web",
        capabilities=set(Capability),
    )
    ctx = _ctx_with(target)
    out = asyncio.run(list_targets_handler(ListTargetsInput(), ctx))
    assert len(out.targets) == 1
    summary = out.targets[0]
    assert set(summary.capabilities) == CAPABILITY_ALLOWLIST


def test_all_non_allowlisted_capabilities_yield_empty_capabilities() -> None:
    target = _FakeTarget(
        name="prod-web",
        capabilities={
            _NotACapability.INTERNAL_ADMIN_ROOT,
            _NotACapability.SECRET_CAPABILITY,
        },
    )
    ctx = _ctx_with(target)
    out = asyncio.run(list_targets_handler(ListTargetsInput(), ctx))
    assert len(out.targets) == 1
    summary = out.targets[0]
    assert summary.capabilities == []
