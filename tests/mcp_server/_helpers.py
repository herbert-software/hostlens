"""Internal test helpers for McpToolsAdapter test files."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast

import structlog
from pydantic import BaseModel

from hostlens.core.config import Settings
from hostlens.targets.registry import TargetRegistry
from hostlens.tools.base import NoopApprovalService, ToolContext, ToolSpec


class StubInspectorRegistry:
    def list_summaries(self) -> list[object]:
        return []


class EmptyInput(BaseModel):
    pass


class EmptyOutput(BaseModel):
    pass


class TypedInput(BaseModel):
    name: str
    version: str


class TypedOutput(BaseModel):
    ok: bool


async def ok_handler(args: BaseModel, ctx: ToolContext) -> BaseModel:
    return EmptyOutput()


async def typed_ok_handler(args: BaseModel, ctx: ToolContext) -> BaseModel:
    return TypedOutput(ok=True)


def make_ctx() -> ToolContext:
    return ToolContext(
        target_registry=TargetRegistry(),
        inspector_registry=StubInspectorRegistry(),
        config=Settings(),
        logger=cast(structlog.stdlib.BoundLogger, structlog.get_logger("test")),
        approval_service=NoopApprovalService(),
        cancel=asyncio.Event(),
    )


def ctx_factory() -> Callable[[], ToolContext]:
    return make_ctx


def make_spec(
    *,
    name: str = "tool_x",
    surfaces: set[str] | None = None,
    side_effects: str = "none",
    requires_approval: bool = False,
    timeout: float | None = None,
    input_schema: type[BaseModel] = EmptyInput,
    output_schema: type[BaseModel] = EmptyOutput,
    handler: Callable[[BaseModel, ToolContext], Awaitable[BaseModel]] | None = None,
    agent_description: str = "agent desc",
    mcp_description: str = "mcp desc",
    sensitive_output: bool | None = False,
) -> ToolSpec:
    """Build a ToolSpec with MCP-surface-friendly defaults for adapter tests."""
    return ToolSpec(
        name=name,
        version="1.0.0",
        input_schema=input_schema,
        output_schema=output_schema,
        handler=handler if handler is not None else ok_handler,
        agent_description=agent_description,
        mcp_description=mcp_description,
        cli_help=None,
        surfaces=cast(Any, surfaces if surfaces is not None else {"mcp"}),
        side_effects=cast(Any, side_effects),
        requires_approval=requires_approval,
        sensitive_output=sensitive_output,
        timeout=timeout,
    )
