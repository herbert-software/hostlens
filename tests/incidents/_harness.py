"""Shared harness for the incident-pack snapshot tests (M2.8 group 4).

The "double replay layer" in one place:

- execution layer → ``ReplayTarget`` (canned command output from a committed
  ``tests/fixtures/incident_pack/<key>.json`` fixture),
- LLM layer → a backend the caller supplies (``PlaybackBackend`` over a
  committed cassette in the snapshot tests; a ``RecordingBackend`` wrapping a
  scripted ``FakeBackend`` in the generator).

Both halves are driven by the real ``PlannerAgent`` → ``AgentLoop`` →
``ToolsAdapter`` → ``run_inspector`` → ``InspectorRunner`` pipeline under a
**frozen clock**, so the one ``sampling_window`` inspector
(``log.tail.error_burst``) renders byte-stable commands that the ReplayTarget
can match.

``project_planner_result`` renders the deterministic projection the snapshots
compare against (narrative + severity-sorted findings + token totals) —
explicitly excluding duration / Rich decoration / run_id / timestamps
(design D4).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from _scenarios import SCENARIOS_BY_KEY, IncidentScenario

from hostlens.agent.backend import (
    MessageResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from hostlens.agent.planner import PlannerAgent
from hostlens.core.config import AgentSettings, Settings
from hostlens.inspectors.registry import build_registry_from_search_paths
from hostlens.targets.config import ReplayEntry, TargetsConfig
from hostlens.targets.registry import build_registry_from_config
from hostlens.tools.base import NoopApprovalService, ToolContext
from hostlens.tools.default_tools import register_default_tools
from hostlens.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from hostlens.agent.backend import LLMBackend
    from hostlens.agent.planner import PlannerResult
    from hostlens.targets.replay import ReplayTarget

# Fixed UTC instant the whole double-replay layer runs against. Any
# ``sampling_window`` command embeds these timestamps; ReplayTarget matches the
# exact rendered string, so the clock MUST be frozen and identical in the
# generator and the snapshot tests.
FROZEN_DT = datetime(2026, 1, 2, 12, 0, 0, tzinfo=UTC)


def frozen_clock() -> datetime:
    return FROZEN_DT


# `incident-host` matches the ExecutionTarget name regex (`^[a-z][a-z0-9_\-]{0,63}$`).
INCIDENT_TARGET_NAME = "incident-host"

_TESTS_DIR = Path(__file__).parent.parent
FIXTURES_DIR = _TESTS_DIR / "fixtures" / "incident_pack"
CASSETTES_DIR = _TESTS_DIR / "fixtures" / "cassettes"
SNAPSHOTS_DIR = Path(__file__).parent / "snapshots"

SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}


def build_incident_planner(
    backend: LLMBackend,
    *,
    fixture_name: str,
) -> tuple[PlannerAgent, ReplayTarget]:
    """Assemble a ``PlannerAgent`` over the committed fixture for ``fixture_name``."""
    return build_incident_planner_over_fixture(
        backend, fixture_path=FIXTURES_DIR / f"{fixture_name}.json"
    )


def build_incident_planner_over_fixture(
    backend: LLMBackend,
    *,
    fixture_path: Path,
) -> tuple[PlannerAgent, ReplayTarget]:
    """Assemble a ``PlannerAgent`` over a ``ReplayTarget`` + clock-bound tools.

    Returns ``(planner, replay_target)`` so the caller can assert
    ``replay_target.misses == []`` (strict-consumption drift guard) after the
    run. The real builtin ``InspectorRegistry`` is loaded so the 11 incident
    inspectors are reachable; the ToolRegistry is wired with the frozen clock
    via ``register_default_tools(clock=...)`` (Option C). ``fixture_path`` lets
    the drift test point at a deliberately-incomplete fixture.
    """
    settings = Settings(agent=AgentSettings())

    target_registry = build_registry_from_config(
        TargetsConfig(
            version="1",
            targets=[
                ReplayEntry(
                    name=INCIDENT_TARGET_NAME,
                    type="replay",
                    fixture=str(fixture_path),
                )
            ],
        ),
        settings,
    )
    replay_target: ReplayTarget = target_registry.get(INCIDENT_TARGET_NAME)  # type: ignore[assignment]

    inspector_registry = build_registry_from_search_paths([], settings=settings).registry

    tool_registry = ToolRegistry()
    register_default_tools(tool_registry, clock=frozen_clock)

    logger = structlog.get_logger("incident")

    def context_factory() -> ToolContext:
        return ToolContext(
            target_registry=target_registry,
            inspector_registry=inspector_registry,
            config=settings,
            logger=logger,
            approval_service=NoopApprovalService(),
            cancel=asyncio.Event(),
        )

    planner = PlannerAgent(backend, tool_registry, settings, context_factory)
    return planner, replay_target


def build_authored_responses(scenario: IncidentScenario) -> list[MessageResponse]:
    """Two scripted model responses: one parallel tool-use turn + one narrative.

    Turn 1 emits one ``run_inspector`` ``tool_use`` block per scenario
    inspector (the Agent picking the scenario's core inspectors). Turn 2 ends
    the loop with the scenario narrative. Usage numbers are fixed so the
    projected token totals are deterministic.
    """
    tool_blocks: list[ToolUseBlock | TextBlock] = [
        ToolUseBlock(
            type="tool_use",
            id=f"tu_{index}",
            name="run_inspector",
            input={
                "target_name": INCIDENT_TARGET_NAME,
                "inspector_name": call.name,
                "parameters": call.params,
            },
        )
        for index, call in enumerate(scenario.inspectors)
    ]
    turn1 = MessageResponse(
        id="msg_incident_turn1",
        model="claude-test",
        role="assistant",
        content=tool_blocks,
        stop_reason="tool_use",
        usage=Usage(input_tokens=1200, output_tokens=90),
    )
    turn2 = MessageResponse(
        id="msg_incident_turn2",
        model="claude-test",
        role="assistant",
        content=[TextBlock(type="text", text=scenario.narrative)],
        stop_reason="end_turn",
        usage=Usage(input_tokens=1500, output_tokens=140),
    )
    return [turn1, turn2]


async def assert_incident_snapshot(scenario_key: str, backend: LLMBackend) -> None:
    """Drive the double-replay pipeline and assert the deterministic contract.

    1. ``ReplayTarget.misses == []`` — strict-consumption drift guard (the
       primary loud-failure signal; the ReplayMiss exception is absorbed by
       ``ToolsAdapter.dispatch`` inside the pipeline, design D1).
    2. The deterministic projection equals the committed snapshot.
    3. The scenario's core inspectors appear in the ``run_inspector`` tool_use
       sequence, and the report carries at least one finding.
    """
    scenario = SCENARIOS_BY_KEY[scenario_key]
    planner, target = build_incident_planner(backend, fixture_name=scenario_key)
    result = await planner.run(scenario.intent)

    assert target.misses == [], f"{scenario_key}: ReplayTarget misses {target.misses}"

    expected = (SNAPSHOTS_DIR / f"{scenario_key}.md").read_text(encoding="utf-8")
    assert project_planner_result(result) == expected

    invoked = [
        inv.input.get("inspector_name")
        for inv in result.loop_result.tool_invocations
        if inv.tool_name == "run_inspector"
    ]
    for call in scenario.inspectors:
        assert call.name in invoked, f"{scenario_key}: core inspector {call.name} not invoked"

    assert result.findings, f"{scenario_key}: report carries no findings"


def project_planner_result(result: PlannerResult) -> str:
    """Deterministic projection compared against ``snapshots/<key>.md``.

    Excludes every non-deterministic surface (duration / Rich decoration /
    run_id / timestamps). Findings are sorted by ``(severity_rank, message)``
    so the snapshot does not depend on inspector finding order or pipeline
    collection order (design D4).
    """
    lines: list[str] = ["# narrative", result.narrative.rstrip(), "", "# findings"]

    sorted_findings = sorted(
        result.findings,
        key=lambda f: (SEVERITY_RANK.get(f.severity, 99), f.message),
    )
    for finding in sorted_findings:
        tags = ",".join(finding.tags)
        lines.append(f"- [{finding.severity}] {finding.message} (tags: {tags})")

    lines.extend(
        [
            "",
            "# tokens",
            f"input={result.loop_result.usage_totals.input_tokens}",
            f"output={result.loop_result.usage_totals.output_tokens}",
        ]
    )
    return "\n".join(lines) + "\n"
