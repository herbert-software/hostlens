"""Shared harness for the incident-pack snapshot tests (M2.8 group 4).

The "double replay layer" in one place:

- execution layer → ``ReplayTarget`` (canned command output from a committed
  ``src/hostlens/demo/scenarios/<key>/fixture.json`` fixture),
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
from hostlens.demo.assets import source_tree_path
from hostlens.inspectors.registry import build_registry_from_search_paths
from hostlens.targets.config import ReplayEntry, TargetsConfig
from hostlens.targets.registry import build_registry_from_config
from hostlens.tools.base import NoopApprovalService, ToolContext
from hostlens.tools.default_tools import register_default_tools
from hostlens.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from collections.abc import Callable

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

SNAPSHOTS_DIR = Path(__file__).parent / "snapshots"

SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}


def build_incident_planner(
    backend: LLMBackend,
    *,
    fixture_name: str,
) -> tuple[PlannerAgent, ReplayTarget]:
    """Assemble a ``PlannerAgent`` over the committed fixture for ``fixture_name``.

    The fixture now lives under ``src/hostlens/demo/scenarios/<key>/fixture.json``
    (the demo package is the single SOT). ``source_tree_path`` returns the real
    source-tree path — safe here because ``_harness`` only runs from the source
    tree / editable install, never from an installed wheel.
    """
    return build_incident_planner_over_fixture(
        backend, fixture_path=source_tree_path(fixture_name, "fixture")
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


def build_incident_pipeline_inputs(
    backend: LLMBackend,
    *,
    fixture_path: Path,
) -> tuple[LLMBackend, Callable[[], ToolContext], ReplayTarget, Settings]:
    """Assemble the 4-tuple ``run_diagnosis_pipeline`` needs for a full incident run.

    Mirrors ``demo.assembly.build_demo_pipeline``'s 4-tuple shape
    ``(backend, context_factory, replay_target, settings)`` so the incident
    generator can drive the SAME shared Planner→Diagnostician→Report core the
    ``--intent`` path uses, capturing BOTH phases' requests through the supplied
    ``RecordingBackend`` (design D-3.5 step 2 / tasks 3.1).

    The ``backend`` is returned verbatim (it is the recorder). ``context_factory``
    reuses the incident ``ReplayTarget`` (registered under ``incident-host``) + the
    builtin ``InspectorRegistry``. The frozen clock is injected into the tool
    registry by ``run_diagnosis_pipeline`` (``tool_clock=frozen_clock``), not here,
    so the factory only carries the registries / settings / logger.

    ``settings`` is the env-stripped ``Settings(agent=AgentSettings())`` whose
    ``agent.primary_model`` defaults to ``claude-opus-4-7`` — byte-identical to the
    historical recording model (the request key's ``model`` source). Returning
    this instance (the caller passes it straight to the pipeline) keeps the
    record-time and replay-time ``model`` identical, the D-3.5 byte-identity
    prerequisite.
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

    return backend, context_factory, replay_target, settings


# ``IncidentScenario.diag_supporting`` defaults to ``("F1",)``: F1 is the first
# label ``FindingStore.seed`` assigns under the D-7 stable sort, so it is
# guaranteed present for every scenario (each scenario seeds ≥1 finding). A
# scenario whose hypothesis spans multiple findings overrides ``diag_supporting``
# (and ``diag_confidence``) per diagnostician rule 5. The recording generator
# prints the full D-7-sorted seeded list so a maintainer can verify each authored
# ``F#`` label points at a real finding (design D-3.5 / tasks 3.1).


def build_planner_responses(scenario: IncidentScenario) -> list[MessageResponse]:
    """The two scripted Planner-phase turns (one parallel tool-use + narrative).

    Turn 1 emits one ``run_inspector`` ``tool_use`` block per scenario inspector
    (the Agent picking the scenario's core inspectors). Turn 2 ends the Planner
    loop with the scenario narrative. Usage numbers are fixed so the projected
    token totals are deterministic.
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


def build_diagnostician_responses(scenario: IncidentScenario) -> list[MessageResponse]:
    """The two scripted Diagnostician-phase turns (one ``correlate_findings`` + finalize).

    Turn 1 records exactly one root-cause hypothesis via ``correlate_findings``,
    referencing the scenario's authored ordinal labels (``scenario.diag_supporting``,
    default ``("F1",)`` — the first D-7-sorted seeded finding, always present) —
    NOT a ``finding.id`` (``diagnostician_tools.py``: the model references ordinal
    labels). ``confidence`` is the scenario's authored ``diag_confidence``
    (default ``high``) so a scenario can satisfy diagnostician rule 5. It
    deliberately never calls ``request_more_inspection`` so the diagnosis phase
    adds zero target commands and the fixture stays untouched (design D-3). Turn 2
    finalizes the diagnosis loop with a short narrative. Usage numbers are fixed
    for determinism.
    """
    correlate = MessageResponse(
        id="msg_incident_diag_turn1",
        model="claude-test",
        role="assistant",
        content=[
            ToolUseBlock(
                type="tool_use",
                id="tu_diag_0",
                name="correlate_findings",
                input={
                    "description": scenario.hypothesis,
                    "confidence": scenario.diag_confidence,
                    "supporting_findings": list(scenario.diag_supporting),
                    "suggested_actions": list(scenario.suggested_actions),
                },
            )
        ],
        stop_reason="tool_use",
        usage=Usage(input_tokens=1700, output_tokens=110),
    )
    finalize = MessageResponse(
        id="msg_incident_diag_turn2",
        model="claude-test",
        role="assistant",
        content=[TextBlock(type="text", text=scenario.diagnosis_narrative)],
        stop_reason="end_turn",
        usage=Usage(input_tokens=1900, output_tokens=160),
    )
    return [correlate, finalize]


def build_authored_responses(scenario: IncidentScenario) -> list[MessageResponse]:
    """Full-chain scripted responses: 2 Planner turns + 2 Diagnostician turns.

    The loop contract requires every ``tool_use`` response be followed by a
    backend call returning ``end_turn``, so the happy path is exactly four
    ordered ``MessageResponse`` objects (design D-3.5 step 1):
    ``planner_tool_use`` → ``planner_end_turn`` → ``diag_correlate_tool_use`` →
    ``diag_end_turn``. A single ``FakeBackend`` over this list serves BOTH loops;
    the ``RecordingBackend`` captures both phases' requests into one cassette
    (matched by request key, not order — design D-2).
    """
    return build_planner_responses(scenario) + build_diagnostician_responses(scenario)


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
