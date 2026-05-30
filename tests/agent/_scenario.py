"""Byte-stable synthetic Planner scenario shared by the cassette-loop tests.

Task 6.1: a single source of the synthetic multi-turn scenario consumed by
``test_planner_replay.py`` (6.2, real cassette) and the round-trip determinism
test (6.3, ``RecordingBackend`` → ``PlaybackBackend`` over ``tmp_path``).

Everything here is **byte-stable** because the cassette request-key hashes the
whole ``messages`` list (including ``tool_result`` content). If a synthetic
``tool_result`` smuggled a real timestamp / UUID / username / path, the bytes
written at record time would differ from the bytes the loop re-sends at replay
time and the lookup would ``CassetteMiss`` (spec §需求:合成 fixture 必须字节稳定,
record→replay 往返不得 miss). So the stub findings are fixed literals — no
clock, no ``uuid4``, no ``getpass.getuser()``, no ``Path.home()``.

The ``target_registry`` holds a single ``local`` target whose
``TargetEntry.tags`` carry the ``"cassette-synthetic"`` marker so record mode's
``guard_record_targets`` lets it through (a bare local would be treated as the
real machine and rejected).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import structlog

from hostlens.agent.backend import (
    MessageResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from hostlens.agent.backends.fake import FakeBackend
from hostlens.agent.planner import PlannerAgent
from hostlens.core.config import AgentSettings, Settings
from hostlens.reporting.models import Finding
from hostlens.targets.config import LocalEntry, TargetsConfig
from hostlens.targets.registry import TargetRegistry, build_registry_from_config
from hostlens.tools.base import NoopApprovalService, ToolContext, ToolSpec
from hostlens.tools.default_tools import run_inspector
from hostlens.tools.registry import ToolRegistry
from hostlens.tools.schemas.run_inspector import RunInspectorInput, RunInspectorOutput

from ._helpers import StubInspectorRegistry

if TYPE_CHECKING:
    from hostlens.agent.backend import LLMBackend

__all__ = [
    "CASSETTE_NAME",
    "COMMITTED_CASSETTE_PATH",
    "SCENARIO_INTENT",
    "regenerate_committed_cassette",
    "scenario_context_factory",
    "scenario_fake_backend",
    "scenario_settings",
    "scenario_target_registry",
    "scenario_tool_registry",
]

# Explicit semantic cassette name (design D-6: never nodeid-derived). 6.2's
# ``llm_cassette(CASSETTE_NAME, ...)`` maps this to
# ``tests/fixtures/cassettes/planner_health_check.jsonl``.
CASSETTE_NAME = "planner_health_check"

# Absolute path of the committed cassette ``regenerate_committed_cassette``
# writes and ``test_planner_replay.py`` replays.
COMMITTED_CASSETTE_PATH = (
    Path(__file__).parent.parent / "fixtures" / "cassettes" / f"{CASSETTE_NAME}.jsonl"
)

# Frozen intent string — part of the turn-1 ``messages`` and therefore part of
# the request key, so it MUST be a fixed literal.
SCENARIO_INTENT = "检查这台机器的健康状况"

# Synthetic local target name + marker. The marker is the literal the guard
# pins (``_SYNTHETIC_TARGET_TAG`` in cassette_recording.py); kept in sync here.
_SYNTHETIC_TARGET_NAME = "cassette-local"
_SYNTHETIC_TARGET_TAG = "cassette-synthetic"

# Fixed ``run_inspector`` tool_use input (RunInspectorInput is extra="forbid").
_RUN_INSPECTOR_INPUT: dict[str, Any] = {
    "target_name": _SYNTHETIC_TARGET_NAME,
    "inspector_name": "system.uptime",
}


def scenario_settings() -> Settings:
    """Default agent settings — large enough budgets that the 2-turn scenario
    finishes on ``end_turn`` rather than tripping a degraded guard.
    """
    return Settings(agent=AgentSettings())


def scenario_target_registry() -> TargetRegistry:
    """A registry with one ``local`` target tagged ``cassette-synthetic``.

    The tag is what makes record mode's ``guard_record_targets`` treat the
    local target as a byte-stable synthetic stand-in instead of the real
    machine (spec §场景:带 cassette-synthetic 标记的 local 放行).
    """
    config = TargetsConfig(
        version="1",
        targets=[
            LocalEntry(
                name=_SYNTHETIC_TARGET_NAME,
                type="local",
                enabled=True,
                tags=[_SYNTHETIC_TARGET_TAG],
            )
        ],
    )
    return build_registry_from_config(config, Settings())


def _stub_findings() -> list[Finding]:
    # Fixed literals only — no clock / uuid / username / path (byte stability).
    return [
        Finding(severity="info", message="load average within normal range"),
        Finding(severity="info", message="uptime 12 days"),
    ]


async def _stub_handler(args: RunInspectorInput, ctx: ToolContext) -> RunInspectorOutput:
    # Echo the (fixed) input names back; both are byte-stable literals.
    return RunInspectorOutput(
        target_name=args.target_name,
        inspector_name=args.inspector_name,
        findings=_stub_findings(),
    )


def _stub_run_inspector_spec() -> ToolSpec:
    """ToolSpec named exactly ``run_inspector.name`` so the Planner collects
    its output as findings, but whose handler is a byte-stable stub — no real
    inspector / target / SSH infrastructure is touched.
    """
    return ToolSpec(
        name=run_inspector.name,
        version="1.0.0",
        input_schema=RunInspectorInput,
        output_schema=RunInspectorOutput,
        handler=_stub_handler,
        agent_description="run a single inspector against a target",
        mcp_description="stub",
        cli_help=None,
        surfaces=cast(Any, {"agent"}),
        side_effects=cast(Any, "read"),
        requires_approval=False,
        sensitive_output=False,
        timeout=30.0,
    )


def scenario_tool_registry() -> ToolRegistry:
    """A ToolRegistry holding only the byte-stable ``run_inspector`` stub.

    A single-tool registry keeps ``tools_count`` (part of the cassette request
    key) fixed at 1 across record and replay.
    """
    reg = ToolRegistry()
    reg.register(_stub_run_inspector_spec())
    return reg


def scenario_context_factory(
    target_registry: TargetRegistry,
) -> Callable[[], ToolContext]:
    """Build the ``ToolContext`` factory the Planner hands to its ToolsAdapter.

    The synthetic ``run_inspector`` stub ignores the context (it returns fixed
    findings), so a minimal context wired to the synthetic ``target_registry``
    plus a stub ``InspectorRegistry`` is enough to walk the dispatch path.
    """

    def _make() -> ToolContext:
        return ToolContext(
            target_registry=target_registry,
            inspector_registry=cast("Any", StubInspectorRegistry()),
            config=Settings(),
            logger=cast("structlog.stdlib.BoundLogger", structlog.get_logger("scenario")),
            approval_service=NoopApprovalService(),
            cancel=asyncio.Event(),
        )

    return _make


def _msg(*, content: list[Any], stop_reason: str) -> MessageResponse:
    return MessageResponse(
        id="msg_scenario",
        model="claude-test",
        role="assistant",
        content=content,
        stop_reason=cast(Any, stop_reason),
        usage=Usage(input_tokens=1, output_tokens=1),
    )


def scenario_fake_backend() -> FakeBackend:
    """Scripted ``FakeBackend`` driving ≥2 tool-use turns then an end_turn.

    Used as the ``inner`` of ``RecordingBackend`` in the round-trip test (6.3)
    so recording needs no real API. The two distinct ``tool_use`` ids keep the
    two turns' assistant content distinct, and each turn's growing ``messages``
    produces a distinct request key (spec §场景:多轮 scenario 写出多条 record).
    """
    return FakeBackend(
        responses=[
            _msg(
                content=[
                    ToolUseBlock(
                        type="tool_use",
                        id="tu_1",
                        name=run_inspector.name,
                        input=_RUN_INSPECTOR_INPUT,
                    )
                ],
                stop_reason="tool_use",
            ),
            _msg(
                content=[
                    ToolUseBlock(
                        type="tool_use",
                        id="tu_2",
                        name=run_inspector.name,
                        input=_RUN_INSPECTOR_INPUT,
                    )
                ],
                stop_reason="tool_use",
            ),
            _msg(
                content=[TextBlock(type="text", text="机器健康，未发现严重问题。")],  # noqa: RUF001
                stop_reason="end_turn",
            ),
        ]
    )


async def regenerate_committed_cassette(*, cassette_path: Path | None = None) -> Path:
    """Deterministically (re)generate the committed ``planner_health_check`` cassette.

    Drives ``PlannerAgent`` over the *scripted* ``scenario_fake_backend`` (not the
    real Anthropic API) through a ``RecordingBackend``, then flushes the captured
    ``(request, response)`` pairs to ``cassette_path`` (default
    ``COMMITTED_CASSETTE_PATH``). The synthetic scenario is byte-stable, so the
    written request-keys match exactly what ``test_planner_replay.py`` re-sends at
    replay time — every replay turn hits, never a ``CassetteMiss``.

    The resulting cassette is therefore **not** a real-Claude recording: it is a
    confined synthetic fixture generated from a fixed ``FakeBackend`` script.
    Re-run this whenever the scenario script changes (e.g.
    ``python -m tests.agent._scenario``) and re-commit the file. The real-API
    record path (``HOSTLENS_LLM_MODE=record``) stays reserved for future
    scenarios that need genuine Claude behaviour (M2.8+).
    """

    from support.cassette_recording import _ACTIVE_CASSETTE_PATHS, RecordingBackend

    out_path = cassette_path or COMMITTED_CASSETTE_PATH
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # The duplicate-active-path guard is process-wide; clear so a leaked entry
    # from a prior in-process run does not trip construction here.
    _ACTIVE_CASSETTE_PATHS.discard(out_path)

    recorder = RecordingBackend(
        cassette_path=out_path,
        inner=cast("Any", scenario_fake_backend()),
    )
    target_registry = scenario_target_registry()
    planner = PlannerAgent(
        cast("LLMBackend", recorder),
        scenario_tool_registry(),
        scenario_settings(),
        scenario_context_factory(target_registry),
    )
    await planner.run(SCENARIO_INTENT)
    recorder.flush()
    return out_path


if __name__ == "__main__":
    path = asyncio.run(regenerate_committed_cassette())
    print(f"wrote {path}")
