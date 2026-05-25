"""Demo: run an Inspector through the Tool Registry dispatch path.

Step 6 of the ``add-inspector-plugin-system`` Demo Path. Assembles a
``ToolRegistry`` plus a ``ToolContext`` (target + inspector registries)
and dispatches ``run_inspector`` against the builtin ``hello.echo``
Inspector on a ``LocalTarget`` named ``local-host``.

Run from repo root with the venv active:

    python examples/m1-inspectors/dispatch.py

Expected output is a ``RunInspectorOutput`` JSON dump with one info
finding whose ``message`` is ``"hello received: hello\\n"``.
"""

from __future__ import annotations

import asyncio
from typing import cast

import structlog

from hostlens.core.config import Settings
from hostlens.inspectors.registry import build_registry_from_search_paths
from hostlens.targets.base import ExecutionTarget
from hostlens.targets.config import LocalEntry
from hostlens.targets.local import LocalTarget
from hostlens.targets.registry import TargetRegistry
from hostlens.tools.base import NoopApprovalService, ToolContext
from hostlens.tools.default_tools import register_default_tools
from hostlens.tools.registry import ToolRegistry
from hostlens.tools.schemas.run_inspector import RunInspectorInput, RunInspectorOutput


async def main() -> RunInspectorOutput:
    settings = Settings()

    target_registry = TargetRegistry()
    entry = LocalEntry(name="local-host", type="local", enabled=True)
    target: ExecutionTarget = cast("ExecutionTarget", LocalTarget(name="local-host"))
    target_registry.register(target, entry)

    inspector_registry = build_registry_from_search_paths([], settings=settings).registry

    tool_registry = ToolRegistry()
    register_default_tools(tool_registry)

    ctx = ToolContext(
        target_registry=target_registry,
        inspector_registry=inspector_registry,
        config=settings,
        logger=structlog.get_logger("examples.m1-inspectors.dispatch"),
        approval_service=NoopApprovalService(),
        cancel=asyncio.Event(),
    )

    result = await tool_registry.dispatch(
        "run_inspector",
        RunInspectorInput(target_name="local-host", inspector_name="hello.echo"),
        ctx,
    )
    return cast(RunInspectorOutput, result)


if __name__ == "__main__":
    output = asyncio.run(main())
    print(output.model_dump_json(indent=2))
