"""`SchedulerRunner` deterministic-mode routing + fleet notify (Group F §5.3 / §6.1).

Spec:
  * scheduler-engine §需求:job 执行必须按 mode 路由 ... 并按结果映射 RunStatus
    (§场景:deterministic 模式逐 target 跑固定集产多 target Report /
     §场景:deterministic 全无结果落 failed / §场景:agent 模式行为不变)
  * deterministic-inspection-mode §需求:多 target 必须聚合成一份报告、severity
    全队聚合供路由

The collection phase (`run_deterministic_inspection`) is stubbed with canned
cross-target `InspectorResult`s — Group D exercises it end-to-end against real
targets/inspectors; here the runner contract is isolated: `manifest.mode`
routing, the shared `RunStatus` mapping (fleet `meta.status` ok/partial,
no-result → `failed` with the deterministic note never `failed_api_unavailable`),
the `targets` ledger column listing the whole fleet, and the cross-fleet
`aggregate_severity` driving `only_if` notify routing.

Every backend is fake and every fire is driven through `trigger` (the shared
job body); nothing depends on real timing or a real wall clock. `Settings` are
constructed inline (no dev `.env` read), and `backend_factory` is injected, so
these tests need no env isolation.

``asyncio_mode = "auto"`` (pyproject) — no marker needed.
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
from hostlens.core.config import AgentSettings, Settings
from hostlens.inspectors.registry import InspectorRegistry, build_registry_from_search_paths
from hostlens.inspectors.result import InspectorResult
from hostlens.notifiers.base import Notifier, NotifyPayload, NotifyResult
from hostlens.orchestration import deterministic as det
from hostlens.reporting.models import Finding, Report, Severity
from hostlens.reporting.store import ReportStore
from hostlens.scheduler.runner import SchedulerRunner
from hostlens.scheduler.schema import (
    IntervalSpec,
    NotifyConfig,
    ReportConfig,
    ScheduleManifest,
    ScheduleSpec,
)
from hostlens.scheduler.store import RunStatus, RunStore
from hostlens.targets.config import LocalEntry
from hostlens.targets.registry import TargetRegistry
from hostlens.tools.base import NoopApprovalService, ToolContext

if TYPE_CHECKING:
    from pathlib import Path

    from hostlens.targets.base import ExecutionTarget

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="LocalTarget requires POSIX (Linux/macOS)",
)

_FLEET = ["host-a", "host-b"]
_INSPECTOR = "linux.cpu.top_processes"


# --------------------------------------------------------------------------- #
# Wiring helpers (mirror tests/scheduler/test_runner.py)
# --------------------------------------------------------------------------- #


def _settings() -> Settings:
    return Settings(agent=AgentSettings())


def _make_target_registry(names: list[str]) -> TargetRegistry:
    from hostlens.targets.local import LocalTarget

    registry = TargetRegistry()
    for name in names:
        entry = LocalEntry(name=name, type="local", enabled=True)
        target: ExecutionTarget = cast("ExecutionTarget", LocalTarget(name=name))
        registry.register(target, entry)
    return registry


def _make_inspector_registry() -> InspectorRegistry:
    return build_registry_from_search_paths([], settings=Settings()).registry


def _context_factory(target_registry: TargetRegistry, inspector_registry: InspectorRegistry) -> Any:
    def _make() -> ToolContext:
        return ToolContext(
            target_registry=target_registry,
            inspector_registry=inspector_registry,
            config=Settings(),
            logger=structlog.get_logger("test_runner_deterministic"),
            approval_service=NoopApprovalService(),
            cancel=asyncio.Event(),
        )

    return _make


def _det_manifest(
    *,
    name: str = "daily-health-fleet",
    targets: list[str] | None = None,
    inspectors: list[str] | None = None,
    notify: list[NotifyConfig] | None = None,
) -> ScheduleManifest:
    return ScheduleManifest(
        name=name,
        schedule=ScheduleSpec(interval=IntervalSpec(hours=24), timezone="UTC"),
        mode="deterministic",
        targets=targets if targets is not None else list(_FLEET),
        intent="fleet health",
        inspectors=inspectors if inspectors is not None else [_INSPECTOR],
        report=ReportConfig(),
        notify=notify if notify is not None else [],
    )


def _build_runner(
    *,
    manifest: ScheduleManifest,
    run_store: RunStore,
    report_store: ReportStore,
    channels: dict[str, Notifier] | None = None,
    backend_factory: Any = None,
) -> SchedulerRunner:
    target_registry = _make_target_registry(list(manifest.targets))
    inspector_registry = _make_inspector_registry()
    return SchedulerRunner(
        [manifest],
        run_store=run_store,
        report_store=report_store,
        settings=_settings(),
        backend_factory=(
            backend_factory
            if backend_factory is not None
            else (lambda: cast(LLMBackend, FakeBackend(responses=[])))
        ),
        context_factory=_context_factory(target_registry, inspector_registry),
        target_registry=target_registry,
        channels=channels,
    )


def _stores(tmp_path: Path) -> tuple[RunStore, ReportStore]:
    return (
        RunStore(db_path=tmp_path / "runs.db"),
        ReportStore(db_path=tmp_path / "reports.db", orphan_dir=tmp_path / "orphans"),
    )


# --------------------------------------------------------------------------- #
# Canned collection + narrate replay
# --------------------------------------------------------------------------- #


def _result(
    *,
    target_name: str,
    findings: list[Finding] | None = None,
    status: str = "ok",
    missing: list[str] | None = None,
) -> InspectorResult:
    # The InspectorResult model enforces cross-field invariants: ok ⇒ no error,
    # target_unreachable/exception ⇒ non-empty error, requires_unmet ⇒ missing.
    error = None if status in {"ok", "requires_unmet"} else f"{status} on {target_name}"
    return InspectorResult(
        name=_INSPECTOR,
        version="1.0.0",
        status=cast("Any", status),
        target_name=target_name,
        duration_seconds=0.1,
        findings=findings or [],
        error=error,
        missing=missing or [],
    )


def _finding(message: str, severity: str = "warning") -> Finding:
    return Finding(severity=cast("Any", severity), message=message)


def _patch_collection(monkeypatch: pytest.MonkeyPatch, results: list[InspectorResult]) -> None:
    """Stub the deterministic collection with canned cross-target results.

    Patches the name `run_deterministic_pipeline` resolves at call time
    (`det.run_deterministic_inspection`), so the runner → pipeline → assembly
    path runs for real over these pinned results.
    """

    async def _fake_collect(
        context_factory: Any,
        targets: Any,
        *,
        inspectors: Any = None,
        inspector_parameters: Any = None,
        concurrency: int = det.DEFAULT_DETERMINISTIC_CONCURRENCY,
    ) -> list[InspectorResult]:
        return results

    monkeypatch.setattr(det, "run_deterministic_inspection", _fake_collect)


def _msg(*, content: list[Any], stop_reason: str) -> MessageResponse:
    return MessageResponse(
        id="msg_x",
        model="claude-test",
        role="assistant",
        content=content,
        stop_reason=cast("Any", stop_reason),
        usage=Usage(input_tokens=5, output_tokens=3),
    )


def _end_turn(text: str) -> MessageResponse:
    return _msg(content=[TextBlock(type="text", text=text)], stop_reason="end_turn")


def _correlate(*, labels: list[str]) -> MessageResponse:
    return _msg(
        content=[
            ToolUseBlock(
                type="tool_use",
                id="tu_1",
                name="correlate_findings",
                input={
                    "description": "cross-host load",
                    "confidence": "high",
                    "supporting_findings": labels,
                    "suggested_actions": ["investigate"],
                },
            )
        ],
        stop_reason="tool_use",
    )


# ===========================================================================
# 5.3b — deterministic multi-target Report → Run(ok), targets list the fleet
# ===========================================================================


@_POSIX_ONLY
async def test_deterministic_fire_persists_fleet_run_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_store, report_store = _stores(tmp_path)
    _patch_collection(
        monkeypatch,
        [
            _result(target_name="host-a", findings=[_finding("a warm", "warning")]),
            _result(target_name="host-b", findings=[_finding("b crit", "critical")]),
        ],
    )
    manifest = _det_manifest()
    runner = _build_runner(
        manifest=manifest,
        run_store=run_store,
        report_store=report_store,
        backend_factory=lambda: cast(
            LLMBackend, FakeBackend(responses=[_end_turn("fleet under load")])
        ),
    )

    run = await runner.trigger("daily-health-fleet")

    # All-ok collection (no real degradation) → ok.
    assert run.status is RunStatus.OK
    assert run.report_id is not None
    assert run.report_hash is not None
    assert run.report_storage == "db"
    # The ledger row lists the WHOLE fleet, not just targets[0].
    assert run.targets == ["host-a", "host-b"]
    assert run.started_at is not None

    # The persisted Report is the multi-target fleet Report.
    report = await report_store.get_run(run.report_id)
    assert report is not None
    assert report.meta is not None
    assert report.meta.schedule_name == "daily-health-fleet"
    assert report.meta.target_id.startswith("fleet:")
    assert report.target_name == "host-a,host-b"
    assert {f.target_name for f in report.findings} == {"host-a", "host-b"}


# ===========================================================================
# 5.3b (partial) — a real failure on one host degrades the fleet Report → partial
# ===========================================================================


@_POSIX_ONLY
async def test_deterministic_real_failure_maps_to_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_store, report_store = _stores(tmp_path)
    _patch_collection(
        monkeypatch,
        [
            _result(target_name="host-a", findings=[_finding("a warm", "warning")]),
            # A genuine failure (unreachable host) → fleet status degrades.
            _result(target_name="host-b", status="target_unreachable"),
        ],
    )
    manifest = _det_manifest()
    runner = _build_runner(
        manifest=manifest,
        run_store=run_store,
        report_store=report_store,
        backend_factory=lambda: cast(LLMBackend, FakeBackend(responses=[_end_turn("partial")])),
    )

    run = await runner.trigger("daily-health-fleet")

    assert run.status is RunStatus.PARTIAL
    assert run.report_id is not None
    assert run.targets == ["host-a", "host-b"]


# ===========================================================================
# 5.3b (requires_unmet) — capability mismatch does NOT degrade the fleet Report
# ===========================================================================


@_POSIX_ONLY
async def test_deterministic_requires_unmet_stays_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_store, report_store = _stores(tmp_path)
    _patch_collection(
        monkeypatch,
        [
            _result(target_name="host-a", findings=[_finding("a warm", "warning")]),
            # host-b lacks the capability → requires_unmet (expected skip on a
            # heterogeneous fleet, must NOT degrade the report to partial).
            _result(target_name="host-b", status="requires_unmet", missing=["mysql"]),
        ],
    )
    manifest = _det_manifest()
    runner = _build_runner(
        manifest=manifest,
        run_store=run_store,
        report_store=report_store,
        backend_factory=lambda: cast(LLMBackend, FakeBackend(responses=[_end_turn("ok")])),
    )

    run = await runner.trigger("daily-health-fleet")

    assert run.status is RunStatus.OK
    report = await report_store.get_run(run.report_id) if run.report_id else None
    assert report is not None
    assert report.meta is not None
    assert report.meta.status.value == "ok"
    # The requires_unmet result is still recorded verbatim (never folded away).
    statuses = {ir.status for ir in report.meta.inspectors_used}
    assert "requires_unmet" in statuses


# ===========================================================================
# 5.3c — deterministic no-result → failed with the deterministic note,
# NEVER failed_api_unavailable.
# ===========================================================================


@_POSIX_ONLY
async def test_deterministic_no_result_maps_to_failed_not_api_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_store, report_store = _stores(tmp_path)
    _patch_collection(monkeypatch, [])  # zero InspectorResult → pipeline returns None
    manifest = _det_manifest()
    runner = _build_runner(
        manifest=manifest,
        run_store=run_store,
        report_store=report_store,
    )

    run = await runner.trigger("daily-health-fleet")

    assert run.status is RunStatus.FAILED
    assert run.status is not RunStatus.FAILED_API_UNAVAILABLE
    assert run.error == "deterministic inspection produced no inspector results"
    assert run.report_id is None
    assert run.targets == ["host-a", "host-b"]


# ===========================================================================
# 5.3a — agent mode is unchanged: an agent manifest never reaches the
# deterministic pipeline (the collection stub would raise if it did).
# ===========================================================================


@_POSIX_ONLY
async def test_agent_mode_does_not_route_to_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_store, report_store = _stores(tmp_path)

    # If the agent path mistakenly routed to the deterministic pipeline this
    # stub would be hit; make it explode so a misroute is loud.
    async def _explode(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("agent mode must not call the deterministic collection")

    monkeypatch.setattr(det, "run_deterministic_inspection", _explode)

    agent_manifest = ScheduleManifest(
        name="nightly",
        schedule=ScheduleSpec(interval=IntervalSpec(minutes=10), timezone="UTC"),
        targets=["host-a"],
        intent="检查健康",
        report=ReportConfig(),
    )
    # Planner runs one inspector then finalizes; Diagnostician finalizes.
    script = [
        _msg(
            content=[
                ToolUseBlock(
                    type="tool_use",
                    id="tu_plan",
                    name="run_inspector",
                    input={"target_name": "host-a", "inspector_name": "hello.echo"},
                )
            ],
            stop_reason="tool_use",
        ),
        _end_turn("巡检完成。"),
        _end_turn("诊断完成。"),
    ]
    runner = _build_runner(
        manifest=agent_manifest,
        run_store=run_store,
        report_store=report_store,
        backend_factory=lambda: cast(LLMBackend, FakeBackend(responses=script)),
    )

    run = await runner.trigger("nightly")

    assert run.status is RunStatus.OK
    # Agent mode records the single target, not a fleet.
    assert run.targets == ["host-a"]


# ===========================================================================
# 6.1 — fleet Report routes through the existing notify path: aggregate_severity
# is the cross-fleet max, only_if decides per channel.
# ===========================================================================


class _RecordingNotifier:
    """A channel whose ``send`` always succeeds and records the severity it saw."""

    name = "recording"

    def __init__(self, instance_name: str) -> None:
        self.instance_name = instance_name
        self.seen_severities: list[Severity] = []

    def validate_config(self, cfg: dict[str, object]) -> None:  # pragma: no cover - unused
        _ = cfg

    def render(self, report: Report, *, severity: Severity) -> NotifyPayload:
        _ = report
        self.seen_severities.append(severity)
        return NotifyPayload(channel=self.instance_name, channel_type="recording", body="hi")

    async def send(self, payload: NotifyPayload) -> NotifyResult:
        return NotifyResult(channel=payload.channel, status="sent", attempts=1)


@_POSIX_ONLY
async def test_fleet_report_routes_via_aggregate_severity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_store, report_store = _stores(tmp_path)
    # host-a is only warning; host-b is critical. The cross-fleet aggregate is
    # `critical`, so `severity >= critical` on the on-channel passes while the
    # `severity > critical` off-channel is skipped.
    _patch_collection(
        monkeypatch,
        [
            _result(target_name="host-a", findings=[_finding("a warm", "warning")]),
            _result(target_name="host-b", findings=[_finding("b crit", "critical")]),
        ],
    )
    on = _RecordingNotifier("on-channel")
    off = _RecordingNotifier("off-channel")
    manifest = _det_manifest(
        notify=[
            NotifyConfig(channel="on-channel", only_if="severity >= critical"),
            NotifyConfig(channel="off-channel", only_if="severity > critical"),
        ]
    )
    runner = _build_runner(
        manifest=manifest,
        run_store=run_store,
        report_store=report_store,
        channels={
            "on-channel": cast(Notifier, on),
            "off-channel": cast(Notifier, off),
        },
        backend_factory=lambda: cast(LLMBackend, FakeBackend(responses=[_end_turn("load")])),
    )

    run = await runner.trigger("daily-health-fleet")

    assert run.status is RunStatus.OK
    by_channel = {r.channel: r.status for r in run.notify_results}
    assert by_channel == {"on-channel": "sent", "off-channel": "skipped"}
    # The channel that fired saw the cross-fleet aggregate severity (critical),
    # i.e. the routing input spans BOTH hosts — host-b's critical lifted the
    # whole-fleet severity even though host-a was only warning.
    assert on.seen_severities == ["critical"]


@_POSIX_ONLY
async def test_fleet_correlate_hypothesis_anchors_on_fleet_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End-to-end through the runner: a narrate correlate call lands a hypothesis
    # on the persisted fleet Report with real cross-host finding-id anchors.
    run_store, report_store = _stores(tmp_path)
    _patch_collection(
        monkeypatch,
        [
            _result(target_name="host-a", findings=[_finding("a warm", "warning")]),
            _result(target_name="host-b", findings=[_finding("b crit", "critical")]),
        ],
    )
    manifest = _det_manifest()
    runner = _build_runner(
        manifest=manifest,
        run_store=run_store,
        report_store=report_store,
        backend_factory=lambda: cast(
            LLMBackend,
            FakeBackend(
                responses=[_correlate(labels=["F1", "F2"]), _end_turn("two hosts under pressure")]
            ),
        ),
    )

    run = await runner.trigger("daily-health-fleet")

    assert run.status is RunStatus.OK
    assert run.report_id is not None
    report = await report_store.get_run(run.report_id)
    assert report is not None
    assert len(report.hypotheses) == 1
    finding_ids = {f.id for f in report.findings}
    assert set(report.hypotheses[0].supporting_findings) <= finding_ids
