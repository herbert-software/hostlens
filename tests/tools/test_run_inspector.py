"""Tests for the M1-integrated `run_inspector` ToolSpec handler.

Covers the inspector-plugin-system spec §需求:M2 首批 ToolSpec 必须含
`run_inspector` / `list_inspectors` / `list_targets` MODIFIED block:

(a) End-to-end dispatch through `ToolRegistry.dispatch("run_inspector", ...)`
    against a real `LocalTarget` + real builtin `hello.echo` manifest —
    `RunInspectorOutput.findings` has exactly one info-level entry whose
    message matches the manifest template.
(b) Target-unreachable status: when `target.exec` raises
    `TargetError(kind="ssh_connection_lost")` the handler returns
    `findings=[]` (M2 schema lacks status; M3 will surface it) and does NOT
    propagate the exception.
(c) `target_not_found`: unknown `target_name` raises `ToolError` (caller
    programming error, not a business failure).
(d) `inspector_not_found`: unknown `inspector_name` raises `ToolError`.
(e) Agent surface forces `allow_privileged=False`: a manifest declaring
    `privilege="sudo"` results in `findings=[]` because the runner returns
    `status="requires_unmet"`. `InspectorManifest.model_construct` is used
    to bypass the Pydantic validator that normally enforces M1's
    accept-list — we want to exercise the runner's privilege gate without
    forking the manifest schema.

All tests use `build_registry_from_search_paths([], settings=Settings())`
to assemble the inspector registry. The post-stub contract requires
unpacking via `result.registry` rather than treating the function's
return as a registry directly.
"""

from __future__ import annotations

import asyncio
import sys
from typing import cast

import pytest
import structlog

from hostlens.core.config import Settings
from hostlens.core.exceptions import TargetError, ToolError
from hostlens.inspectors.registry import (
    InspectorRegistry,
    build_registry_from_search_paths,
)
from hostlens.inspectors.schema import (
    CollectSpec,
    InspectorManifest,
    ParseSpec,
)
from hostlens.targets.base import ExecutionTarget
from hostlens.targets.config import LocalEntry
from hostlens.targets.registry import TargetRegistry
from hostlens.tools.base import NoopApprovalService, ToolContext
from hostlens.tools.default_tools import (
    register_default_tools,
    run_inspector_handler,
)
from hostlens.tools.registry import ToolRegistry
from hostlens.tools.schemas.run_inspector import (
    RunInspectorInput,
    RunInspectorOutput,
)

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="LocalTarget requires POSIX (Linux/macOS)",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_inspector_registry() -> InspectorRegistry:
    return build_registry_from_search_paths([], settings=Settings()).registry


def _make_target_registry_with_local(name: str = "local-host") -> TargetRegistry:
    """Build a `TargetRegistry` containing a single real `LocalTarget`."""

    # Lazy import — `LocalTarget` is POSIX-only and import-time raises on Win.
    from hostlens.targets.local import LocalTarget

    registry = TargetRegistry()
    entry = LocalEntry(name=name, type="local", enabled=True)
    target: ExecutionTarget = cast("ExecutionTarget", LocalTarget(name=name))
    registry.register(target, entry)
    return registry


def _ctx(
    target_registry: TargetRegistry,
    inspector_registry: InspectorRegistry,
) -> ToolContext:
    return ToolContext(
        target_registry=target_registry,
        inspector_registry=inspector_registry,
        config=Settings(),
        logger=structlog.get_logger("test_run_inspector"),
        approval_service=NoopApprovalService(),
        cancel=asyncio.Event(),
    )


# ---------------------------------------------------------------------------
# (a) end-to-end happy path via ToolRegistry.dispatch — Task 13.6
# ---------------------------------------------------------------------------


@_POSIX_ONLY
def test_run_inspector_dispatch_hello_echo_e2e() -> None:
    """Spec §场景:run_inspector handler 通过 InspectorRunner dispatch 真实 inspector.

    Assembles a real `ToolRegistry` via `register_default_tools`, builds a
    real `TargetRegistry` containing a `LocalTarget("local-host")`, and
    dispatches `run_inspector` through the registry. `echo hello` runs on
    the local POSIX host and produces exactly one info-level finding.
    """
    tool_registry = ToolRegistry()
    register_default_tools(tool_registry)

    target_registry = _make_target_registry_with_local("local-host")
    inspector_registry = _make_inspector_registry()
    ctx = _ctx(target_registry, inspector_registry)

    async def go() -> RunInspectorOutput:
        result = await tool_registry.dispatch(
            "run_inspector",
            RunInspectorInput(target_name="local-host", inspector_name="hello.echo"),
            ctx,
        )
        assert isinstance(result, RunInspectorOutput)
        return result

    output = asyncio.run(go())
    assert output.target_name == "local-host"
    assert output.inspector_name == "hello.echo"
    assert len(output.findings) == 1
    finding = output.findings[0]
    assert finding.severity == "info"
    # hello.echo manifest: `message: "hello received: {raw}"` with parse
    # format=raw, so {raw} = stdout of `echo hello` = "hello\n".
    assert finding.message == "hello received: hello\n"


# ---------------------------------------------------------------------------
# (b) target unreachable — runner converts TargetError to status, handler
#     projects empty findings WITHOUT raising
# ---------------------------------------------------------------------------


class _UnreachableTarget:
    """Fake `ExecutionTarget` whose `exec` always raises `TargetError`.

    Used to drive the runner's `target_unreachable` branch without touching
    real subprocess machinery.
    """

    type = "local"
    name = "broken"

    def __init__(self) -> None:
        from hostlens.targets.base import Capability

        self.capabilities: set[Capability] = {Capability.SHELL, Capability.FILE_READ}

    async def exec(self, cmd, *, timeout, env=None):  # type: ignore[no-untyped-def]
        raise TargetError(kind="ssh_connection_lost", target=self.name)

    async def read_file(self, path):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def test_run_inspector_target_unreachable_returns_empty_findings() -> None:
    """Spec §场景:run_inspector handler 在 status != ok 时返回空 findings 不抛异常.

    Drives the runner's Step 8 `except TargetError` branch by using a
    manifest with `requires_binaries=[]` (so the preflight binary probe is
    skipped) and a target whose `exec` always raises
    `TargetError(kind="ssh_connection_lost")`. The handler must catch the
    resulting `status="target_unreachable"` and project to empty findings
    without propagating any exception.
    """
    target_registry = TargetRegistry()
    entry = LocalEntry(name="broken", type="local", enabled=True)
    target = _UnreachableTarget()
    target_registry.register(cast("ExecutionTarget", target), entry)

    # Register a custom manifest with no preflight probes so the runner
    # reaches Step 8 (`target.exec(rendered_cmd, ...)`) where the
    # `except TargetError` block converts the failure to status=
    # "target_unreachable". `hello.echo` is unsuitable here because it
    # declares `requires_binaries=[echo]`, which causes preflight Step 5
    # to raise — that scenario is the runner's responsibility, not the
    # handler's.
    no_probe_manifest = InspectorManifest.model_construct(
        name="probe.free",
        version="1.0.0",
        description="manifest with no preflight probes",
        tags=[],
        targets=["local"],
        requires_capabilities=[],
        requires_binaries=[],
        requires_files=[],
        privilege="none",
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
    inspector_registry.register(no_probe_manifest, source_path=None)

    ctx = _ctx(target_registry, inspector_registry)

    async def go() -> RunInspectorOutput:
        return await run_inspector_handler(
            RunInspectorInput(target_name="broken", inspector_name="probe.free"),
            ctx,
        )

    output = asyncio.run(go())
    assert output.target_name == "broken"
    assert output.inspector_name == "probe.free"
    assert output.findings == []


# ---------------------------------------------------------------------------
# (c) target_not_found — caller programming error -> ToolError
# ---------------------------------------------------------------------------


def test_run_inspector_target_not_found_raises_tool_error() -> None:
    """Spec §场景:run_inspector handler target 不存在 raise ToolError."""
    target_registry = TargetRegistry()  # empty
    inspector_registry = _make_inspector_registry()
    ctx = _ctx(target_registry, inspector_registry)

    async def go() -> None:
        await run_inspector_handler(
            RunInspectorInput(
                target_name="does-not-exist", inspector_name="hello.echo"
            ),
            ctx,
        )

    with pytest.raises(ToolError) as exc_info:
        asyncio.run(go())
    assert "target_not_found" in str(exc_info.value)
    assert "does-not-exist" in str(exc_info.value)


# ---------------------------------------------------------------------------
# (d) inspector_not_found — caller programming error -> ToolError
# ---------------------------------------------------------------------------


@_POSIX_ONLY
def test_run_inspector_inspector_not_found_raises_tool_error() -> None:
    """Spec §场景:run_inspector handler inspector 不存在 raise ToolError."""
    target_registry = _make_target_registry_with_local("local-host")
    inspector_registry = _make_inspector_registry()
    ctx = _ctx(target_registry, inspector_registry)

    async def go() -> None:
        await run_inspector_handler(
            RunInspectorInput(
                target_name="local-host", inspector_name="does.not.exist"
            ),
            ctx,
        )

    with pytest.raises(ToolError) as exc_info:
        asyncio.run(go())
    assert "inspector_not_found" in str(exc_info.value)
    assert "does.not.exist" in str(exc_info.value)


# ---------------------------------------------------------------------------
# (e) privilege="sudo" + agent surface -> requires_unmet -> empty findings
# ---------------------------------------------------------------------------


@_POSIX_ONLY
def test_run_inspector_privilege_sudo_yields_empty_findings_on_agent_surface() -> None:
    """Spec §场景:run_inspector handler 在 agent surface 强制 allow_privileged=False.

    Builds a manifest with `privilege="sudo"` via `model_construct` — the
    inspector-plugin-system spec allows `Literal["none", "sudo", "root"]`
    so we don't need to bypass validation, but we still skip the heavy
    loader path (Jinja2 AST scan etc.) by going through `model_construct`
    directly. The runner's preflight Step 3 returns `requires_unmet` with
    missing=["privilege_opt_in"] when the agent surface dispatches such an
    inspector (Agent never opts in to privilege).
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

    ctx = _ctx(target_registry, inspector_registry)

    async def go() -> RunInspectorOutput:
        return await run_inspector_handler(
            RunInspectorInput(target_name="local-host", inspector_name="sudo.test"),
            ctx,
        )

    output = asyncio.run(go())
    assert output.target_name == "local-host"
    assert output.inspector_name == "sudo.test"
    assert output.findings == []
