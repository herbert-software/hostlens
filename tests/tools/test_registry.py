"""Tests for `ToolRegistry` per spec §需求:ToolRegistry 必须按 name 索引并支持 surface 过滤查询.

Five scenarios:
1. `register` raises `ToolError` on duplicate name, with both module
   names mentioned in the error message.
2. `list_for(surface)` filters by surface and returns specs sorted by name.
3. `dispatch` raises `TypeError` when args type mismatches `spec.input_schema`.
4. `inspect.iscoroutinefunction(ToolRegistry.dispatch)` is `True`.
5. `dispatch` propagates `asyncio.TimeoutError` from `asyncio.wait_for`
   when `spec.timeout` is set.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import cast

import pytest
import structlog
from pydantic import BaseModel

from hostlens.core.config import Settings
from hostlens.core.exceptions import ToolError
from hostlens.targets.registry import TargetRegistry
from hostlens.tools.base import NoopApprovalService, ToolContext, ToolSpec
from hostlens.tools.registry import ToolRegistry


class _In(BaseModel):
    pass


class _Out(BaseModel):
    pass


async def _ok_handler(args: BaseModel, ctx: ToolContext) -> BaseModel:
    return _Out()


async def _slow_handler(args: BaseModel, ctx: ToolContext) -> BaseModel:
    await asyncio.sleep(5)
    return _Out()


def _make_spec(
    *,
    name: str = "x",
    surfaces: set[str] | None = None,
    timeout: float | None = None,
    handler=_ok_handler,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        version="1.0.0",
        input_schema=_In,
        output_schema=_Out,
        handler=handler,
        agent_description="ad",
        mcp_description="md",
        cli_help=None,
        surfaces=cast(set, surfaces if surfaces is not None else {"agent"}),
        side_effects="none",
        timeout=timeout,
    )


class _StubInspectorRegistry:
    """Inspector registry stub kept here until the inspector plugin
    proposal ships the real registry.
    """

    def list_summaries(self) -> list[object]:
        return []


def _make_ctx() -> ToolContext:
    return ToolContext(
        target_registry=TargetRegistry(),
        inspector_registry=_StubInspectorRegistry(),
        config=Settings(),
        logger=structlog.get_logger("test"),
        approval_service=NoopApprovalService(),
        cancel=asyncio.Event(),
    )


def test_register_duplicate_name_raises_tool_error_with_module_info() -> None:
    reg = ToolRegistry()
    spec_a = _make_spec(name="dup")
    spec_b = _make_spec(name="dup")
    reg.register(spec_a)
    with pytest.raises(ToolError) as ei:
        reg.register(spec_b)
    msg = str(ei.value)
    assert "dup" in msg
    assert getattr(spec_a.handler, "__module__", "") in msg
    assert getattr(spec_b.handler, "__module__", "") in msg


def test_list_for_filters_by_surface_and_sorts_by_name() -> None:
    reg = ToolRegistry()
    a = _make_spec(name="alpha", surfaces={"agent"})
    b = _make_spec(name="bravo", surfaces={"agent", "mcp"})
    c = _make_spec(name="charlie", surfaces={"mcp"})
    # Register out of alphabetical order to prove sorting.
    reg.register(c)
    reg.register(a)
    reg.register(b)
    assert [s.name for s in reg.list_for("agent")] == ["alpha", "bravo"]
    assert [s.name for s in reg.list_for("mcp")] == ["bravo", "charlie"]
    assert reg.list_for("cli") == []


def test_dispatch_raises_type_error_on_args_type_mismatch() -> None:
    reg = ToolRegistry()
    reg.register(_make_spec(name="t"))
    ctx = _make_ctx()

    async def go() -> None:
        with pytest.raises(TypeError):
            await reg.dispatch("t", "not_a_pydantic_model", ctx)  # type: ignore[arg-type]

    asyncio.run(go())


def test_dispatch_is_coroutine_function() -> None:
    assert inspect.iscoroutinefunction(ToolRegistry.dispatch)


def test_dispatch_timeout_raises_asyncio_timeout_error() -> None:
    reg = ToolRegistry()
    reg.register(_make_spec(name="slow", timeout=0.05, handler=_slow_handler))
    ctx = _make_ctx()

    async def go() -> None:
        with pytest.raises((asyncio.TimeoutError, TimeoutError)):
            await reg.dispatch("slow", _In(), ctx)

    asyncio.run(go())


def test_dispatch_returns_model_when_handler_ok() -> None:
    reg = ToolRegistry()
    reg.register(_make_spec(name="ok"))
    ctx = _make_ctx()

    async def go() -> None:
        result = await reg.dispatch("ok", _In(), ctx)
        assert isinstance(result, _Out)

    asyncio.run(go())


def test_get_unknown_name_raises_key_error() -> None:
    reg = ToolRegistry()
    with pytest.raises(KeyError):
        reg.get("missing")


def test_names_returns_full_set() -> None:
    reg = ToolRegistry()
    reg.register(_make_spec(name="alpha"))
    reg.register(_make_spec(name="bravo"))
    assert reg.names() == {"alpha", "bravo"}
