"""Core data models for the Tool Registry capability layer (M2).

This module exposes the host-agnostic Layer 1 abstractions that every
surface adapter (agent / mcp / cli) consumes:

- `ToolHandler` Protocol — async callable contract for tool handlers.
- `ApprovalService` Protocol — minimal contract for write-side approval.
- `NoopApprovalService` — M2 stub that always refuses (M9 will replace).
- `InspectorRegistry` Protocol — placeholder until the inspector
  proposal lands the real registry; defined here so `ToolContext` can
  be typed without a forward reference cycle.
- `ToolContext` — frozen dataclass DI container, fields locked to the M2
  set (forbid LLMBackend per ADR-008).
- `ToolSpec` — frozen Pydantic v2 model carrying full policy metadata.

The module is import-side-effect free: importing it MUST NOT mutate any
module-level / global / class-level registry.

`TargetRegistry` is the real M1 class from
`hostlens.targets.registry` — imported (not re-declared) so
`get_type_hints(ToolContext)["target_registry"]` resolves to the real
type per spec §场景:target_registry 是真实 TargetRegistry 类型.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator

from hostlens.core.config import Settings
from hostlens.core.exceptions import ToolPolicyViolation
from hostlens.targets.registry import TargetRegistry

# ---------------------------------------------------------------------------
# Handler & approval Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class ToolHandler(Protocol):
    """Async callable contract for ToolSpec handlers.

    A handler accepts a Pydantic input model and a ToolContext (DI
    container), returning a Pydantic output model. Handlers are pure
    async functions; no `def` (sync) handlers are accepted by ToolSpec.
    """

    async def __call__(
        self, args: BaseModel, ctx: ToolContext
    ) -> BaseModel: ...  # pragma: no cover


@runtime_checkable
class ApprovalService(Protocol):
    """Minimal approval gate contract (M2 stub; M9 ships the real flow)."""

    async def request_approval(self, action: str, reason: str) -> bool: ...  # pragma: no cover


class NoopApprovalService:
    """M2 stub: every approval request is rejected with a structured policy
    violation. Concrete implementations land with the M9 remediation flow.

    `tool_name` is a placeholder snake_case identifier (`noop_approval_service`)
    so it satisfies the ToolSpec name regex even though this stub is not a
    real tool.
    """

    async def request_approval(self, action: str, reason: str) -> bool:
        raise ToolPolicyViolation(
            tool_name="noop_approval_service",
            surface="agent",
            violated_field="requires_approval",
            reason="approval_flow_not_supported_in_m2",
        )


# ---------------------------------------------------------------------------
# Stub registry Protocols (Inspector registry — real registry lands in the
# next proposal `add-inspector-plugin-system`).
# ---------------------------------------------------------------------------


@runtime_checkable
class InspectorRegistry(Protocol):
    """Placeholder Protocol for the Inspector registry."""

    def list_summaries(self) -> list[Any]: ...  # pragma: no cover


# ---------------------------------------------------------------------------
# ToolContext — DI container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolContext:
    """Frozen DI container passed to every ToolHandler.

    M2 field set is **exactly** these six entries (ADR-008 forbids
    `llm_backend` / any LLM call entry point):

    - `target_registry` — real `hostlens.targets.registry.TargetRegistry`
      (M1 landed in the `add-execution-target-abstraction` proposal).
    - `inspector_registry` — Inspector registry (still a stub Protocol
      until the inspector plugin proposal lands).
    - `config` — `Settings` instance (M0).
    - `logger` — bound structlog logger.
    - `approval_service` — concrete `ApprovalService` (never `None`; M2
      uses `NoopApprovalService` to keep the ABI stable while the M9 flow
      is unfinished).
    - `cancel` — `asyncio.Event` for cooperative cancellation propagation
      from the Agent loop / Ctrl-C.
    """

    target_registry: TargetRegistry
    inspector_registry: InspectorRegistry
    config: Settings
    logger: structlog.stdlib.BoundLogger
    approval_service: ApprovalService
    cancel: asyncio.Event


# ---------------------------------------------------------------------------
# ToolSpec — Pydantic capability spec
# ---------------------------------------------------------------------------


_TOOL_NAME_PATTERN = r"^[a-z][a-z0-9_]*$"


class ToolSpec(BaseModel):
    """Host-agnostic capability specification.

    A `ToolSpec` is the single source of truth for an Agent-callable
    capability. Surface adapters (agent / mcp / cli) project the spec into
    host-specific JSON Schema at projection time; ToolSpec itself
    persists **no** host-specific schema (CLAUDE.md §4.10 hard rule #2).

    Frozen + `extra="forbid"` make the schema a stable ABI: any new field
    must be added explicitly and reviewed for downstream impact.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
    )

    # Identity
    name: str = Field(pattern=_TOOL_NAME_PATTERN)
    version: str = Field(min_length=1)

    # Pydantic schemas (NOT host-specific JSON Schemas; adapter projects)
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]

    # Handler — async callable (BaseModel, ToolContext) -> Awaitable[BaseModel]
    handler: Callable[[BaseModel, ToolContext], Awaitable[BaseModel]]

    # Three surface descriptions (different audiences => different copy)
    agent_description: str
    mcp_description: str
    cli_help: str | None

    # Policy metadata (policy gate, not hint)
    surfaces: set[Literal["agent", "mcp", "cli"]]
    side_effects: Literal["none", "read", "write", "destructive"]
    requires_approval: bool = False
    permissions: set[str] = Field(default_factory=set)
    sensitive_output: bool | None = None
    target_constraints: set[str] | None = None
    timeout: float | None = None
    tags: set[str] = Field(default_factory=set)

    @field_validator("input_schema", "output_schema")
    @classmethod
    def _validate_schema_is_basemodel_subclass(cls, value: Any) -> type[BaseModel]:
        if not isinstance(value, type) or not issubclass(value, BaseModel):
            raise ValueError("input_schema / output_schema must be subclass of pydantic.BaseModel")
        return value

    @field_validator("handler")
    @classmethod
    def _validate_handler_is_coroutine_function(
        cls, value: Callable[..., Any]
    ) -> Callable[..., Any]:
        if not inspect.iscoroutinefunction(value):
            raise ValueError("handler must be an async function (`async def ...`)")
        return value

    def __call__(self, *args: object, **kwargs: object) -> None:
        # Per spec §需求:@tool 装饰器必须是纯 spec factory §场景:试图直接调用
        # 装饰后的名字 raise — the decorated name binds to a ToolSpec instance,
        # not a callable. Direct invocation must steer callers toward the two
        # supported entry points instead of silently surfacing Pydantic's
        # generic "object is not callable" message.
        raise TypeError(
            "ToolSpec instances are not callable. "
            "Use `registry.dispatch(name, args, ctx)` (typed) or "
            "`spec.handler(args, ctx)` (escape hatch, tests only) instead."
        )
