"""Unit tests for the backend-injectable ``run_diagnosis_pipeline`` core (task 1.1).

Spec: ``openspec/changes/wire-demo-to-report/specs/demo-cli-command/spec.md``
(design D-1 / D-7).

``run_diagnosis_pipeline`` is the shared Planner → seed → Diagnostician → assemble
core that both ``--intent`` (real backend + wall clock) and ``demo`` (offline
``PlaybackBackend`` + frozen clock) drive. These tests inject a scripted
``FakeBackend`` + a real ``ToolContext`` factory (one ``LocalTarget`` + the
builtin ``hello.echo`` inspector) and assert:

- the core assembles a ``Report`` and ``report_target_name`` may differ from
  ``target_lookup_name`` (the two-target-name split, D-1);
- ``planner_result_sink`` is a no-op when ``None`` (byte-equivalent to the old
  seam) and receives the ``PlannerResult`` when supplied;
- the D-7 stable seeding sort assigns F1/F2 identically regardless of the
  collector's same-response append order, and the sort key is a superset of
  every per-finding field ``_render_findings_block`` renders.

``asyncio_mode = "auto"`` (pyproject) — no marker needed; every backend is fake.
"""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING, Any, cast

import pytest
import structlog

from hostlens.agent.backend import (
    LLMBackend,
    MessageResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from hostlens.agent.backends.fake import FakeBackend
from hostlens.agent.diagnostician import SeededFinding, _render_findings_block
from hostlens.agent.planner import PlannerResult
from hostlens.cli._intent import (
    _seed_findings_from_snapshot,
    _seeding_sort_key,
    run_diagnosis_pipeline,
)
from hostlens.core.config import AgentSettings, Settings
from hostlens.inspectors.registry import (
    InspectorRegistry,
    build_registry_from_search_paths,
)
from hostlens.inspectors.result import InspectorResult
from hostlens.reporting.models import Evidence, Finding, Report
from hostlens.targets.config import LocalEntry
from hostlens.targets.registry import TargetRegistry
from hostlens.tools.base import NoopApprovalService, ToolContext
from hostlens.tools.finding_store import FindingStore

if TYPE_CHECKING:
    from hostlens.targets.base import ExecutionTarget


_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="LocalTarget requires POSIX (Linux/macOS)",
)

_LOOKUP_NAME = "local-host"
_RUN_INSPECTOR_INPUT = {"target_name": _LOOKUP_NAME, "inspector_name": "hello.echo"}


# --------------------------------------------------------------------------- #
# Backend scripting helpers (mirror test_inspect_intent_report.py)
# --------------------------------------------------------------------------- #


def _msg(*, content: list[Any], stop_reason: str) -> MessageResponse:
    return MessageResponse(
        id="msg_x",
        model="claude-test",
        role="assistant",
        content=content,
        stop_reason=cast(Any, stop_reason),
        usage=Usage(input_tokens=3, output_tokens=2),
    )


def _end_turn(text: str) -> MessageResponse:
    return _msg(content=[TextBlock(type="text", text=text)], stop_reason="end_turn")


def _planner_run_inspector() -> MessageResponse:
    return _msg(
        content=[
            ToolUseBlock(
                type="tool_use", id="tu_plan", name="run_inspector", input=_RUN_INSPECTOR_INPUT
            )
        ],
        stop_reason="tool_use",
    )


def _correlate() -> MessageResponse:
    return _msg(
        content=[
            ToolUseBlock(
                type="tool_use",
                id="tu_corr",
                name="correlate_findings",
                input={
                    "description": "可能是配置漂移",
                    "confidence": "medium",
                    "supporting_findings": ["F1"],
                    "suggested_actions": ["复查配置"],
                },
            )
        ],
        stop_reason="tool_use",
    )


def _happy_script() -> list[MessageResponse]:
    # Planner: run one inspector then finalize; Diagnostician: correlate then finalize.
    return [
        _planner_run_inspector(),
        _end_turn("巡检完成。"),
        _correlate(),
        _end_turn("诊断完成。"),
    ]


def _fake(responses: list[MessageResponse]) -> LLMBackend:
    return cast(LLMBackend, FakeBackend(responses=responses))


def _settings() -> Settings:
    # AgentLoop reads settings.agent.* unconditionally; the default Settings()
    # carries agent=None, so the pipeline tests must supply a real agent block.
    return Settings(agent=AgentSettings())


# --------------------------------------------------------------------------- #
# Real ToolContext factory (one LocalTarget + builtin inspectors)
# --------------------------------------------------------------------------- #


def _make_target_registry() -> TargetRegistry:
    from hostlens.targets.local import LocalTarget

    registry = TargetRegistry()
    entry = LocalEntry(name=_LOOKUP_NAME, type="local", enabled=True)
    target: ExecutionTarget = cast("ExecutionTarget", LocalTarget(name=_LOOKUP_NAME))
    registry.register(target, entry)
    return registry


def _make_inspector_registry() -> InspectorRegistry:
    return build_registry_from_search_paths([], settings=Settings()).registry


def _context_factory(
    target_registry: TargetRegistry,
    inspector_registry: InspectorRegistry,
) -> Any:
    def _make() -> ToolContext:
        return ToolContext(
            target_registry=target_registry,
            inspector_registry=inspector_registry,
            config=Settings(),
            logger=structlog.get_logger("test_run_diagnosis_pipeline"),
            approval_service=NoopApprovalService(),
            cancel=asyncio.Event(),
        )

    return _make


# --------------------------------------------------------------------------- #
# Pipeline: assembles a Report; report name may differ from lookup name
# --------------------------------------------------------------------------- #


@_POSIX_ONLY
async def test_pipeline_assembles_report_distinct_target_names() -> None:
    backend = _fake(_happy_script())
    factory = _context_factory(_make_target_registry(), _make_inspector_registry())

    report = await run_diagnosis_pipeline(
        backend,
        _settings(),
        factory,
        report_target_name="demo:cpu_saturation",  # display label ≠ lookup key
        target_lookup_name=_LOOKUP_NAME,
        target_type="local",
        intent="检查健康",
    )

    assert isinstance(report, Report)
    # The display label is written verbatim; the registry lookup key is NOT.
    assert report.meta is not None
    assert report.meta.target_name == "demo:cpu_saturation"
    # The single hypothesis the script recorded survived into the Report.
    assert len(report.hypotheses) == 1


@_POSIX_ONLY
async def test_pipeline_unknown_lookup_name_raises() -> None:
    """The generic guard: target_lookup_name absent from the supplied registry
    fails before any backend call (KeyError from TargetRegistry.get)."""

    backend = _fake(_happy_script())
    factory = _context_factory(_make_target_registry(), _make_inspector_registry())

    with pytest.raises(KeyError):
        await run_diagnosis_pipeline(
            backend,
            _settings(),
            factory,
            report_target_name="demo:x",
            target_lookup_name="not-registered",
            target_type="local",
            intent="检查健康",
        )


# --------------------------------------------------------------------------- #
# planner_result_sink: no-op when None, receives PlannerResult when supplied
# --------------------------------------------------------------------------- #


@_POSIX_ONLY
async def test_pipeline_sink_none_is_noop_equivalent() -> None:
    """sink=None must not change behaviour: the Report is still assembled."""

    report = await run_diagnosis_pipeline(
        _fake(_happy_script()),
        _settings(),
        _context_factory(_make_target_registry(), _make_inspector_registry()),
        report_target_name=_LOOKUP_NAME,
        target_lookup_name=_LOOKUP_NAME,
        target_type="local",
        intent="检查健康",
        planner_result_sink=None,
    )
    assert isinstance(report, Report)


@_POSIX_ONLY
async def test_pipeline_sink_receives_planner_result() -> None:
    captured: list[PlannerResult] = []

    await run_diagnosis_pipeline(
        _fake(_happy_script()),
        _settings(),
        _context_factory(_make_target_registry(), _make_inspector_registry()),
        report_target_name=_LOOKUP_NAME,
        target_lookup_name=_LOOKUP_NAME,
        target_type="local",
        intent="检查健康",
        planner_result_sink=captured.append,
    )

    assert len(captured) == 1
    assert isinstance(captured[0], PlannerResult)
    # The sink fires after planner.run with the real Planner-phase result.
    assert captured[0].intent == "检查健康"


# --------------------------------------------------------------------------- #
# D-7 stable seeding sort: F1/F2 deterministic across append orders
# --------------------------------------------------------------------------- #


def _ir(*, name: str, version: str, finding: Finding) -> InspectorResult:
    return InspectorResult(
        name=name,
        version=version,
        status="ok",
        target_name=_LOOKUP_NAME,
        duration_seconds=0.5,
        findings=[finding],
        error=None,
        missing=[],
    )


def test_seeding_sort_stable_across_append_orders() -> None:
    """A two-inspector snapshot fed in EITHER append order yields the same
    F1/F2 → finding-id mapping (D-7)."""

    f_cpu = Finding(severity="warning", message="cpu high")
    f_mem = Finding(severity="info", message="mem ok")
    cpu_ir = _ir(name="linux.cpu", version="1.0.0", finding=f_cpu)
    mem_ir = _ir(name="linux.mem", version="2.1.0", finding=f_mem)

    order_a = _seed_findings_from_snapshot([cpu_ir, mem_ir], FindingStore())
    order_b = _seed_findings_from_snapshot([mem_ir, cpu_ir], FindingStore())

    label_to_id_a = {s.label: s.finding.id for s in order_a}
    label_to_id_b = {s.label: s.finding.id for s in order_b}

    assert label_to_id_a == label_to_id_b
    # Sanity: the keys are F1/F2 and resolve to the two distinct stamped ids.
    assert set(label_to_id_a) == {"F1", "F2"}
    assert label_to_id_a["F1"] != label_to_id_a["F2"]


def test_seeding_sort_key_covers_every_rendered_field() -> None:
    """The D-7 sort key is a superset of every per-finding field
    ``_render_findings_block`` renders: perturbing any rendered field changes
    the key, so a key tie ⇒ a byte-identical rendered line (single-direction
    superset, no fail-loud guard needed)."""

    base = Finding(
        severity="warning",
        message="cpu high",
        tags=["a", "b"],
        inspector_name="linux.cpu",
        inspector_version="1.0.0",
    )
    base_key = _seeding_sort_key(base)

    # Every per-finding field the renderer emits, perturbed one at a time. Each
    # perturbation MUST move the key (proves the field is in the key).
    perturbations = [
        base.model_copy(update={"severity": "critical"}),
        base.model_copy(update={"inspector_name": "linux.mem"}),
        base.model_copy(update={"tags": ["b", "a"]}),  # original order, not sorted
        base.model_copy(
            update={"evidence": [Evidence(kind="metric", metric_name="cpu", metric_value=99.0)]}
        ),  # len changes 0→1
        base.model_copy(update={"message": "cpu critical"}),
    ]
    for perturbed in perturbations:
        assert _seeding_sort_key(perturbed) != base_key

    # Superset invariant tied to the REAL renderer: every perturbation that
    # changes a per-finding render field also changes the rendered line, AND it
    # changes the key (asserted above). Conversely, a key tie ⇒ a byte-identical
    # rendered line. Here we assert the forward direction against the actual
    # ``_render_findings_block`` output so a future render-field addition that is
    # NOT mirrored into the key (rendered line differs but key ties) trips it.
    def _render_line(f: Finding) -> str:
        return _render_findings_block([SeededFinding(label="F1", finding=f)])

    for perturbed in perturbations:
        assert _render_line(perturbed) != _render_line(base)
        # rendered line differs ⇒ the key must differ too (key ⊇ render fields).
        assert _seeding_sort_key(perturbed) != base_key
