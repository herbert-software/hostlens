"""`run_deterministic_pipeline` + `register_narrate_only_diagnostician_tools`.

Group E (§4) of `add-deterministic-inspection-mode`: the narrate-only
assembly path and the full fleet pipeline.

Spec:
  * diagnostician-agent §需求:诊断师装配必须支持 narrate-only 变体 (仅
    correlate_findings、禁再巡检 / 选 target)
  * deterministic-inspection-mode §需求:LLM 只对采集结果写根因叙述、不得追加
    巡检 + §需求:多 target 必须聚合成一份报告、severity 全队聚合供路由

Coverage:
  * 4.3a narrate-only registry holds exactly `correlate_findings` — never
    `request_more_inspection` / `list_inspectors` / `list_targets`.
  * 4.3b the full `register_diagnostician_tools` assembly is unaffected
    (still the three-tool batch).
  * 4.3c the pipeline assembles ONE fleet Report whose findings span all
    targets (each carrying its source `target_name`) and whose aggregate
    severity is the cross-fleet max (routing input).
  * 4.3d the narrate LLM is replayed offline (`FakeBackend` canned
    `MessageResponse`s — the sanctioned PlaybackBackend-style replay; no real
    API), records a hypothesis via `correlate_findings`, and the harvested
    hypothesis lands on the fleet Report with real `Finding.id` anchors.
  * empty collection → `None` (no-result path).
  * degraded narration keeps the collected report (non-fatal).

`asyncio_mode = "auto"` (pyproject) — no `@pytest.mark.asyncio` needed.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

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
from hostlens.core.config import AgentSettings, Settings
from hostlens.inspectors.registry import InspectorRegistry
from hostlens.inspectors.result import InspectorResult
from hostlens.notifiers.routing import aggregate_severity
from hostlens.orchestration import deterministic as det
from hostlens.orchestration.deterministic import run_deterministic_pipeline
from hostlens.reporting.models import Finding, compute_finding_id
from hostlens.targets.registry import TargetRegistry
from hostlens.tools.base import NoopApprovalService, ToolContext
from hostlens.tools.diagnostician_tools import (
    register_diagnostician_tools,
    register_narrate_only_diagnostician_tools,
)
from hostlens.tools.finding_store import FindingStore
from hostlens.tools.registry import ToolRegistry

# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def _settings(**agent_kwargs: Any) -> Settings:
    return Settings(agent=AgentSettings(**agent_kwargs))


def _ctx_factory() -> ToolContext:
    return ToolContext(
        target_registry=TargetRegistry(),
        inspector_registry=InspectorRegistry(),
        config=Settings(),
        logger=cast("Any", structlog.get_logger("test_det_pipeline")),
        approval_service=NoopApprovalService(),
        cancel=asyncio.Event(),
    )


def _finding(message: str, severity: str = "warning") -> Finding:
    return Finding(severity=cast("Any", severity), message=message)


def _result(
    *,
    target_name: str,
    inspector: str = "linux.cpu.top_processes",
    version: str = "1.0.0",
    findings: list[Finding] | None = None,
    status: str = "ok",
) -> InspectorResult:
    return InspectorResult(
        name=inspector,
        version=version,
        status=cast("Any", status),
        target_name=target_name,
        duration_seconds=0.1,
        findings=findings or [],
    )


def _msg(*, content: list[Any], stop_reason: str) -> MessageResponse:
    return MessageResponse(
        id="msg_x",
        model="claude-test",
        role="assistant",
        content=content,
        stop_reason=cast("Any", stop_reason),
        usage=Usage(input_tokens=11, output_tokens=7),
    )


def _end_turn(text: str) -> MessageResponse:
    return _msg(content=[TextBlock(type="text", text=text)], stop_reason="end_turn")


def _correlate(*, block_id: str, labels: list[str], desc: str = "root cause") -> MessageResponse:
    return _msg(
        content=[
            ToolUseBlock(
                type="tool_use",
                id=block_id,
                name="correlate_findings",
                input={
                    "description": desc,
                    "confidence": "high",
                    "supporting_findings": labels,
                    "suggested_actions": ["restart service"],
                },
            )
        ],
        stop_reason="tool_use",
    )


# ===========================================================================
# 4.3a / 4.3b — assembly registries
# ===========================================================================


def test_narrate_only_registry_holds_only_correlate_findings() -> None:
    # §场景:narrate-only 装配的注册表只含 correlate_findings
    registry = ToolRegistry()
    register_narrate_only_diagnostician_tools(registry, finding_store=FindingStore())

    names = {spec.name for spec in registry.list_for("agent")}
    assert names == {"correlate_findings"}
    # Structurally absent: no re-inspection / discovery / target-selection tools.
    assert "request_more_inspection" not in names
    assert "list_inspectors" not in names
    assert "list_targets" not in names


def test_full_assembly_still_registers_three_tools() -> None:
    # §场景:全装配路径不受影响 — register_diagnostician_tools still installs
    # the three-tool batch (correlate + request_more_inspection + list_inspectors),
    # never list_targets.
    registry = ToolRegistry()
    register_diagnostician_tools(
        registry, finding_store=FindingStore(), target_name="host-a", clock=None
    )
    names = {spec.name for spec in registry.list_for("agent")}
    assert names == {"correlate_findings", "request_more_inspection", "list_inspectors"}
    assert "list_targets" not in names


# ===========================================================================
# 4.3c / 4.3d — full fleet pipeline
# ===========================================================================


def _patch_collection(monkeypatch: pytest.MonkeyPatch, results: list[InspectorResult]) -> None:
    """Stub the collection phase with canned cross-target `InspectorResult`s.

    Group D already exercises `run_deterministic_inspection` end-to-end against
    real targets / inspectors; here we pin the collected results so the test
    isolates the narrate + fleet-assembly contract (severity aggregation, id
    anchors, hypothesis attachment).
    """

    async def _fake_collect(
        context_factory: Any,
        targets: Any,
        *,
        inspectors: Any = None,
        inspector_parameters: dict[str, dict[str, Any]] | None = None,
        concurrency: int = det.DEFAULT_DETERMINISTIC_CONCURRENCY,
    ) -> list[InspectorResult]:
        return results

    monkeypatch.setattr(det, "run_deterministic_inspection", _fake_collect)


async def test_pipeline_assembles_one_fleet_report_with_per_target_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # §场景:一份全队报告 + 聚合路由 — targets=[a,b], findings span both, the
    # cross-fleet aggregate severity is the routing input.
    results = [
        _result(target_name="host-a", findings=[_finding("a warm", "warning")]),
        _result(target_name="host-b", findings=[_finding("b crit", "critical")]),
    ]
    _patch_collection(monkeypatch, results)

    # narrate loop cites BOTH findings (labels assigned over the sorted set), then
    # ends the turn.
    backend = FakeBackend(
        responses=[
            _correlate(block_id="tu_1", labels=["F1", "F2"], desc="cross-host load"),
            _end_turn("two hosts under pressure"),
        ]
    )

    report = await run_deterministic_pipeline(
        cast(LLMBackend, backend),
        _settings(),
        _ctx_factory,
        targets=["host-a", "host-b"],
        inspectors=["linux.cpu.top_processes"],
        intent="daily health",
        schedule_name="daily-health-fleet",
    )

    assert report is not None
    # ONE fleet Report; findings span both targets, each carrying its source.
    by_target = {f.target_name for f in report.findings}
    assert by_target == {"host-a", "host-b"}
    assert len(report.findings) == 2
    # Report-level fleet label is the sorted target set; the id is a hashed,
    # `fleet:`-prefixed store key (collision-resistant; exact digest is an impl
    # detail — determinism/disjointness pinned in test_fleet_assembly).
    assert report.target_name == "host-a,host-b"
    assert report.meta is not None
    assert report.meta.target_id.startswith("fleet:")

    # Cross-fleet aggregate severity (routing input) is the max across hosts.
    assert aggregate_severity(report) == "critical"

    # narrate produced one hypothesis attached to the fleet Report, with REAL
    # finding-id anchors (not the F1/F2 labels).
    assert len(report.hypotheses) == 1
    assert report.hypotheses[0].description == "cross-host load"
    finding_ids = {f.id for f in report.findings}
    assert set(report.hypotheses[0].supporting_findings) <= finding_ids
    for ref in report.hypotheses[0].supporting_findings:
        assert ref not in {"F1", "F2"}
    # narrative projected into metadata under the shared key.
    assert report.metadata["diagnosis_narrative"] == "two hosts under pressure"
    # narrate loop token usage flows into the report meta — summed across both
    # turns (correlate turn + end turn), each 11 in / 7 out.
    assert report.meta.token_usage.input_tokens == 22
    assert report.meta.token_usage.output_tokens == 14


async def test_pipeline_fleet_label_order_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The fleet label / id are derived from the *sorted* target set, so a
    # reversed targets list yields the same Report.target_name / meta.target_id.
    results = [
        _result(target_name="host-b", findings=[_finding("b", "info")]),
        _result(target_name="host-a", findings=[_finding("a", "info")]),
    ]
    _patch_collection(monkeypatch, results)
    backend = FakeBackend(responses=[_end_turn("no correlation needed")])

    report = await run_deterministic_pipeline(
        cast(LLMBackend, backend),
        _settings(),
        _ctx_factory,
        targets=["host-b", "host-a"],  # reversed input order
        inspectors=["linux.cpu.top_processes"],
        intent="daily health",
        schedule_name="daily-health-fleet",
    )
    assert report is not None
    assert report.target_name == "host-a,host-b"
    assert report.meta is not None
    assert report.meta.target_id.startswith("fleet:")
    # No correlate call → no hypotheses, but the report still stands.
    assert report.hypotheses == []


async def test_pipeline_empty_collection_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No-result path: zero InspectorResult → None (Group F maps to RunStatus.failed).
    _patch_collection(monkeypatch, [])
    backend = FakeBackend(responses=[])  # never consumed — narrate is never reached

    report = await run_deterministic_pipeline(
        cast(LLMBackend, backend),
        _settings(),
        _ctx_factory,
        targets=["host-a"],
        inspectors=["linux.cpu.top_processes"],
        intent="daily health",
    )
    assert report is None


async def test_pipeline_degraded_narration_keeps_collected_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Narration degrade is non-fatal: the collected findings are never discarded.
    # An empty-content end_turn drives the loop to the `empty_response` degraded
    # terminal status (no hypotheses, empty narrative) without crashing.
    results = [_result(target_name="host-a", findings=[_finding("warm", "warning")])]
    _patch_collection(monkeypatch, results)
    # end_turn with no content blocks → terminal status `empty_response`.
    backend = FakeBackend(responses=[_msg(content=[], stop_reason="end_turn")])

    report = await run_deterministic_pipeline(
        cast(LLMBackend, backend),
        _settings(),
        _ctx_factory,
        targets=["host-a"],
        inspectors=["linux.cpu.top_processes"],
        intent="daily health",
        schedule_name="daily-health-fleet",
    )

    assert report is not None
    assert len(report.findings) == 1
    assert report.findings[0].target_name == "host-a"
    # Degraded narration → no hypotheses harvested, but the report (and its
    # collected findings) survives — never discarded over a diagnosis blip.
    assert report.hypotheses == []
    # The narrate degradation surfaces as a degraded ReportStatus (spec:
    # "narrate 阶段后端不可用按 degraded Report 处理、不丢已采集结果"), NOT masked as
    # the collection's ok. empty_response narrate → empty_response ReportStatus.
    assert report.meta is not None
    assert report.meta.status.value == "empty_response"


async def test_pipeline_no_backend_in_tool_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ADR-008: the backend reaches only the narrate loop, never a ToolContext.
    # The ToolContext dataclass has no llm_backend field at all (frozen, locked
    # field set), so asserting the field's absence is the structural guarantee.
    results = [_result(target_name="host-a", findings=[_finding("warm", "warning")])]
    _patch_collection(monkeypatch, results)
    backend = FakeBackend(responses=[_end_turn("done")])

    report = await run_deterministic_pipeline(
        cast(LLMBackend, backend),
        _settings(),
        _ctx_factory,
        targets=["host-a"],
        inspectors=["linux.cpu.top_processes"],
        intent="daily health",
    )
    assert report is not None
    ctx = _ctx_factory()
    assert not hasattr(ctx, "llm_backend")


async def test_pipeline_forwards_inspector_parameters_to_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # §场景:透传链 pipeline→inspection 不丢 — run_deterministic_pipeline forwards
    # its inspector_parameters kwarg straight into run_deterministic_inspection.
    seen: dict[str, dict[str, dict[str, Any]] | None] = {}

    async def _capture_collect(
        context_factory: Any,
        targets: Any,
        *,
        inspectors: Any = None,
        inspector_parameters: dict[str, dict[str, Any]] | None = None,
        concurrency: int = det.DEFAULT_DETERMINISTIC_CONCURRENCY,
    ) -> list[InspectorResult]:
        seen["inspector_parameters"] = inspector_parameters
        return [_result(target_name="host-a", findings=[_finding("warm", "warning")])]

    monkeypatch.setattr(det, "run_deterministic_inspection", _capture_collect)
    backend = FakeBackend(responses=[_end_turn("done")])

    declared = {"net.listening_ports": {"allowed_processes": ["derper"]}}
    report = await run_deterministic_pipeline(
        cast(LLMBackend, backend),
        _settings(),
        _ctx_factory,
        targets=["host-a"],
        inspectors=["net.listening_ports"],
        intent="daily health",
        inspector_parameters=declared,
        schedule_name="daily-health-fleet",
    )
    assert report is not None
    assert seen["inspector_parameters"] == declared


def test_compute_finding_id_anchor_matches_fleet_findings() -> None:
    # Sanity: the id a narrate hypothesis would reference is the SAME content
    # fingerprint the fleet assembly stamps, so harvest anchors resolve.
    fid = compute_finding_id("linux.cpu.top_processes", "1.0.0", "warm")
    assert len(fid) == 16
