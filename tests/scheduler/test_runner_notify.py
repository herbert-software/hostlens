"""Integration tests for Scheduler↔Notifier wiring (task 6.5).

Spec: ``openspec/changes/add-notifier-channels/specs/scheduler-engine/spec.md``
(§需求:runner 必须在 Report 持久化后派发 notify 并落地结果) +
``schedule-manifest/spec.md`` (§需求:notify 在 M5 被消费). Design D-7 / D-8.

Covers:

- a Report-producing fire routes per ``only_if`` → ``sent`` + ``skipped``
  records, ``RunStatus`` stays ``ok``;
- a channel ``send`` raising → ``failed`` record, no bubble, status unchanged,
  other channels still dispatch;
- an ``only_if`` runtime evaluation error → ``failed`` record, no bubble,
  other channels still dispatch;
- a no-Report status (``failed``) → no dispatch, ``notify_results == []``;
- an unknown channel reference is fail-loud at assembly time;
- ``schedule list``-style load with notify + no ``notifiers.yaml`` works;
- an M4 ``notify_results: []`` row round-trips through ``RunStore``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest
import structlog

from hostlens.agent.backend import LLMBackend
from hostlens.agent.backends.fake import FakeBackend
from hostlens.core.config import AgentSettings, Settings
from hostlens.core.exceptions import ConfigError
from hostlens.inspectors.registry import (
    InspectorRegistry,
    build_registry_from_search_paths,
)
from hostlens.inspectors.result import InspectorResult
from hostlens.notifiers.base import Notifier, NotifyPayload, NotifyResult
from hostlens.reporting.models import (
    Finding,
    Report,
    ReportStatus,
    Severity,
    TokenUsage,
)
from hostlens.reporting.store import ReportStore
from hostlens.scheduler.runner import SchedulerRunner
from hostlens.scheduler.schema import (
    IntervalSpec,
    NotifyConfig,
    ReportConfig,
    ScheduleManifest,
    ScheduleSpec,
)
from hostlens.scheduler.store import Run, RunStatus, RunStore
from hostlens.targets.config import LocalEntry
from hostlens.targets.registry import TargetRegistry
from hostlens.tools.base import NoopApprovalService, ToolContext

if TYPE_CHECKING:
    from pathlib import Path

    from hostlens.targets.base import ExecutionTarget

_TARGET = "local-host"


# --------------------------------------------------------------------------- #
# Fake channel adapters (satisfy the Notifier Protocol structurally)
# --------------------------------------------------------------------------- #


class _RecordingNotifier:
    """A channel whose ``send`` always succeeds and records the calls."""

    name = "recording"

    def __init__(self, instance_name: str) -> None:
        self.instance_name = instance_name
        self.sent: list[NotifyPayload] = []

    def validate_config(self, cfg: dict[str, object]) -> None:  # pragma: no cover - unused
        _ = cfg

    def render(self, report: Report, *, severity: Severity) -> NotifyPayload:
        _ = (report, severity)
        return NotifyPayload(channel=self.instance_name, channel_type="recording", body="hi")

    async def send(self, payload: NotifyPayload) -> NotifyResult:
        self.sent.append(payload)
        return NotifyResult(channel=payload.channel, status="sent", attempts=1)


class _ExplodingSendNotifier:
    """A channel whose ``send`` raises a transport-shaped error with a secret."""

    name = "exploding"

    def __init__(self, instance_name: str) -> None:
        self.instance_name = instance_name

    def validate_config(self, cfg: dict[str, object]) -> None:  # pragma: no cover - unused
        _ = cfg

    def render(self, report: Report, *, severity: Severity) -> NotifyPayload:
        _ = (report, severity)
        return NotifyPayload(channel=self.instance_name, channel_type="exploding", body="hi")

    async def send(self, payload: NotifyPayload) -> NotifyResult:
        _ = payload
        raise RuntimeError("boom https://api.telegram.org/bot123456:SECRETTOKEN/sendMessage")


# --------------------------------------------------------------------------- #
# Wiring helpers (mirror tests/scheduler/test_runner.py)
# --------------------------------------------------------------------------- #


def _settings() -> Settings:
    return Settings(agent=AgentSettings())


def _make_target_registry() -> TargetRegistry:
    from hostlens.targets.local import LocalTarget

    registry = TargetRegistry()
    entry = LocalEntry(name=_TARGET, type="local", enabled=True)
    target: ExecutionTarget = cast("ExecutionTarget", LocalTarget(name=_TARGET))
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
            logger=structlog.get_logger("test_runner_notify"),
            approval_service=NoopApprovalService(),
            cancel=asyncio.Event(),
        )

    return _make


def _manifest(notify: list[NotifyConfig]) -> ScheduleManifest:
    return ScheduleManifest(
        name="nightly",
        schedule=ScheduleSpec(interval=IntervalSpec(minutes=10), timezone="UTC"),
        targets=[_TARGET],
        intent="检查健康",
        report=ReportConfig(),
        notify=notify,
    )


def _runner(
    *,
    manifest: ScheduleManifest,
    channels: dict[str, Notifier],
    run_store: RunStore,
    report_store: ReportStore,
) -> SchedulerRunner:
    target_registry = _make_target_registry()
    inspector_registry = _make_inspector_registry()
    return SchedulerRunner(
        [manifest],
        run_store=run_store,
        report_store=report_store,
        settings=_settings(),
        backend_factory=lambda: cast(LLMBackend, FakeBackend(responses=[])),
        context_factory=_context_factory(target_registry, inspector_registry),
        target_registry=target_registry,
        channels=channels,
    )


def _report(*, severity: Severity = "critical") -> Report:
    ir = InspectorResult(
        name="hello.echo",
        version="1.0.0",
        status="ok",
        target_name=_TARGET,
        duration_seconds=0.1,
        findings=[Finding(severity=severity, message="something")],
        error=None,
        missing=[],
    )
    return Report.from_inspector_results(
        _TARGET,
        [ir],
        intent="检查健康",
        started_at=datetime(2026, 5, 26, tzinfo=UTC),
        finished_at=datetime(2026, 5, 26, tzinfo=UTC),
        status=ReportStatus.OK,
        token_usage=TokenUsage(),
        target_type="local",
    )


def _stores(tmp_path: Path) -> tuple[RunStore, ReportStore]:
    return (
        RunStore(db_path=tmp_path / "runs.db"),
        ReportStore(db_path=tmp_path / "reports.db", orphan_dir=tmp_path / "orphans"),
    )


# --------------------------------------------------------------------------- #
# 1. Report-producing fire → sent + skipped, RunStatus unchanged
# --------------------------------------------------------------------------- #


async def test_report_fire_routes_sent_and_skipped(tmp_path: Path) -> None:
    run_store, report_store = _stores(tmp_path)
    sender = _RecordingNotifier("on-channel")
    skipper = _RecordingNotifier("off-channel")
    manifest = _manifest(
        [
            # critical report ⇒ this passes
            NotifyConfig(channel="on-channel", only_if="severity >= warning"),
            # never sends (only_if false)
            NotifyConfig(channel="off-channel", only_if="severity > critical"),
        ]
    )
    runner = _runner(
        manifest=manifest,
        channels={"on-channel": cast(Notifier, sender), "off-channel": cast(Notifier, skipper)},
        run_store=run_store,
        report_store=report_store,
    )

    run = await runner._map_outcome(
        manifest=manifest,
        target_name=_TARGET,
        triggered_at=datetime(2026, 5, 26, tzinfo=UTC),
        started_at=datetime(2026, 5, 26, tzinfo=UTC),
        report=_report(severity="critical"),
        terminal_status="ok",
    )

    assert run.status is RunStatus.OK
    by_channel = {r.channel: r.status for r in run.notify_results}
    assert by_channel == {"on-channel": "sent", "off-channel": "skipped"}
    assert len(sender.sent) == 1
    assert len(skipper.sent) == 0


# --------------------------------------------------------------------------- #
# 2. Channel send raising → failed, no bubble, status unchanged, others still go
# --------------------------------------------------------------------------- #


async def test_channel_send_exception_isolated(tmp_path: Path) -> None:
    run_store, report_store = _stores(tmp_path)
    good = _RecordingNotifier("good")
    bad = _ExplodingSendNotifier("bad")
    manifest = _manifest([NotifyConfig(channel="bad"), NotifyConfig(channel="good")])
    runner = _runner(
        manifest=manifest,
        channels={"bad": cast(Notifier, bad), "good": cast(Notifier, good)},
        run_store=run_store,
        report_store=report_store,
    )

    run = await runner._map_outcome(
        manifest=manifest,
        target_name=_TARGET,
        triggered_at=datetime(2026, 5, 26, tzinfo=UTC),
        started_at=datetime(2026, 5, 26, tzinfo=UTC),
        report=_report(),
        terminal_status="ok",
    )

    assert run.status is RunStatus.OK  # notify failure never changes RunStatus
    by_channel = {r.channel: r for r in run.notify_results}
    assert by_channel["bad"].status == "failed"
    assert by_channel["good"].status == "sent"  # other channel still dispatched
    # The secret embedded in the exception is redacted before persistence.
    assert by_channel["bad"].error is not None
    assert "SECRETTOKEN" not in by_channel["bad"].error


# --------------------------------------------------------------------------- #
# 3. only_if runtime error → failed, isolated, others still dispatch
# --------------------------------------------------------------------------- #


async def test_only_if_runtime_error_isolated(tmp_path: Path) -> None:
    run_store, report_store = _stores(tmp_path)
    good = _RecordingNotifier("good")
    typo = _RecordingNotifier("typo")
    manifest = _manifest(
        [
            # ``severty`` is an undefined name — passes load-time AST gate but
            # raises at run time (NameNotDefined), which must isolate to failed.
            NotifyConfig(channel="typo", only_if="severty >= warning"),
            NotifyConfig(channel="good", only_if="severity >= warning"),
        ]
    )
    runner = _runner(
        manifest=manifest,
        channels={"typo": cast(Notifier, typo), "good": cast(Notifier, good)},
        run_store=run_store,
        report_store=report_store,
    )

    run = await runner._map_outcome(
        manifest=manifest,
        target_name=_TARGET,
        triggered_at=datetime(2026, 5, 26, tzinfo=UTC),
        started_at=datetime(2026, 5, 26, tzinfo=UTC),
        report=_report(),
        terminal_status="ok",
    )

    assert run.status is RunStatus.OK
    by_channel = {r.channel: r.status for r in run.notify_results}
    assert by_channel["typo"] == "failed"
    assert by_channel["good"] == "sent"
    assert len(typo.sent) == 0  # never rendered/sent after only_if failure


# --------------------------------------------------------------------------- #
# 3b. dispatch_notify=False suppresses the whole notify stage (only_if + send)
# while still persisting the Report and leaving the rest of the Run unchanged.
# --------------------------------------------------------------------------- #


async def test_dispatch_notify_false_suppresses_routing_and_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_store, report_store = _stores(tmp_path)
    sender = _RecordingNotifier("on-channel")
    # A configured channel with an only_if so we can prove BOTH the routing
    # evaluation and the send are skipped — an empty notify_results alone is
    # vacuous (it also holds when no channel is configured).
    manifest = _manifest([NotifyConfig(channel="on-channel", only_if="severity >= warning")])

    # Spy on the routing entry point the runner imports; if suppression works
    # it is never evaluated.
    import hostlens.scheduler.runner as runner_module

    routing_calls = 0
    real_should_send = runner_module.should_send

    async def _spy_should_send(*args: Any, **kwargs: Any) -> Any:
        nonlocal routing_calls
        routing_calls += 1
        return await real_should_send(*args, **kwargs)

    monkeypatch.setattr(runner_module, "should_send", _spy_should_send)

    runner = _runner(
        manifest=manifest,
        channels={"on-channel": cast(Notifier, sender)},
        run_store=run_store,
        report_store=report_store,
    )

    report = _report(severity="critical")
    suppressed = await runner._map_outcome(
        manifest=manifest,
        target_name=_TARGET,
        triggered_at=datetime(2026, 5, 26, tzinfo=UTC),
        started_at=datetime(2026, 5, 26, tzinfo=UTC),
        report=report,
        terminal_status="ok",
        dispatch_notify=False,
    )

    # Report persisted + RunStatus / report_id intact, notify fully suppressed.
    assert suppressed.status is RunStatus.OK
    assert suppressed.report_id is not None
    assert suppressed.notify_results == []
    # The load-bearing assertions: routing was never evaluated and nothing was
    # sent (not merely an empty list, which holds even with no channel).
    assert routing_calls == 0
    assert len(sender.sent) == 0

    # Crosscheck: the default dispatch_notify=True path on the same manifest
    # DOES route + send (proving the channel is wired and only_if passes), and
    # yields the same status / a resolvable report_id.
    routing_calls = 0
    sender.sent.clear()
    dispatched = await runner._map_outcome(
        manifest=manifest,
        target_name=_TARGET,
        triggered_at=datetime(2026, 5, 26, tzinfo=UTC),
        started_at=datetime(2026, 5, 26, tzinfo=UTC),
        report=_report(severity="critical"),
        terminal_status="ok",
    )
    assert dispatched.status is suppressed.status
    assert dispatched.report_id is not None
    assert routing_calls == 1
    assert len(sender.sent) == 1
    assert [r.status for r in dispatched.notify_results] == ["sent"]


# --------------------------------------------------------------------------- #
# 4. No-Report status → no dispatch, notify_results == []
# --------------------------------------------------------------------------- #


async def test_no_report_status_does_not_dispatch(tmp_path: Path) -> None:
    run_store, report_store = _stores(tmp_path)
    sender = _RecordingNotifier("on-channel")
    manifest = _manifest([NotifyConfig(channel="on-channel")])
    runner = _runner(
        manifest=manifest,
        channels={"on-channel": cast(Notifier, sender)},
        run_store=run_store,
        report_store=report_store,
    )

    # report=None with terminal_status="ok" → empty-collection → failed.
    run = await runner._map_outcome(
        manifest=manifest,
        target_name=_TARGET,
        triggered_at=datetime(2026, 5, 26, tzinfo=UTC),
        started_at=datetime(2026, 5, 26, tzinfo=UTC),
        report=None,
        terminal_status="ok",
    )

    assert run.status is RunStatus.FAILED
    assert run.notify_results == []
    assert len(sender.sent) == 0


# --------------------------------------------------------------------------- #
# 5. Unknown channel reference is fail-loud at assembly time
# --------------------------------------------------------------------------- #


def test_unknown_channel_fails_loud_at_assembly(tmp_path: Path) -> None:
    run_store, report_store = _stores(tmp_path)
    manifest = _manifest([NotifyConfig(channel="does-not-exist")])
    with pytest.raises(ConfigError, match="unknown channel"):
        _runner(
            manifest=manifest,
            channels={"some-other": cast(Notifier, _RecordingNotifier("some-other"))},
            run_store=run_store,
            report_store=report_store,
        )


# --------------------------------------------------------------------------- #
# 6. schedule list-style load with notify but no notifiers.yaml works
# --------------------------------------------------------------------------- #


def test_load_with_notify_does_not_require_notifiers_yaml(tmp_path: Path) -> None:
    import textwrap

    from hostlens.scheduler.loader import load_schedules
    from hostlens.targets.config import TargetsConfig
    from hostlens.targets.registry import build_registry_from_config

    (tmp_path / "a.yaml").write_text(
        textwrap.dedent(
            """
            name: nightly
            schedule:
              interval:
                hours: 1
              timezone: UTC
            targets:
              - web-1
            intent: check
            notify:
              - channel: ops-telegram
                only_if: "severity >= warning"
            """
        ).lstrip("\n")
    )
    registry = build_registry_from_config(
        TargetsConfig(version="1", targets=[LocalEntry(name="web-1", type="local")]),
        Settings(),
    )

    # No notifiers.yaml is read, no channel-existence check — only the only_if
    # syntax is validated at load time. Loading must succeed.
    manifests = load_schedules(tmp_path, registry)
    assert len(manifests) == 1
    assert manifests[0].notify[0].channel == "ops-telegram"


def test_load_rejects_invalid_only_if(tmp_path: Path) -> None:
    import textwrap

    from hostlens.scheduler.loader import load_schedules
    from hostlens.targets.config import TargetsConfig
    from hostlens.targets.registry import build_registry_from_config

    (tmp_path / "a.yaml").write_text(
        textwrap.dedent(
            """
            name: nightly
            schedule:
              interval:
                hours: 1
              timezone: UTC
            targets:
              - web-1
            intent: check
            notify:
              - channel: ops-telegram
                only_if: ""
            """
        ).lstrip("\n")
    )
    registry = build_registry_from_config(
        TargetsConfig(version="1", targets=[LocalEntry(name="web-1", type="local")]),
        Settings(),
    )

    with pytest.raises(ConfigError) as exc:
        load_schedules(tmp_path, registry)
    assert "only_if" in str(exc.value)


# --------------------------------------------------------------------------- #
# 7. M4 empty notify_results array round-trips through RunStore
# --------------------------------------------------------------------------- #


async def test_m4_empty_notify_results_roundtrips(tmp_path: Path) -> None:
    run_store = RunStore(db_path=tmp_path / "runs.db")
    run = Run(
        run_id="r1",
        schedule_name="nightly",
        triggered_at=datetime(2026, 5, 26, tzinfo=UTC),
        started_at=None,
        status=RunStatus.MISSED,
        targets=[_TARGET],
        notify_results=[],
    )
    await run_store.save(run)

    rows = await run_store.list_recent(limit=10)
    assert len(rows) == 1
    assert rows[0].notify_results == []


async def test_notify_results_deserialize_as_notifyresult(tmp_path: Path) -> None:
    run_store = RunStore(db_path=tmp_path / "runs.db")
    run = Run(
        run_id="r1",
        schedule_name="nightly",
        triggered_at=datetime(2026, 5, 26, tzinfo=UTC),
        started_at=datetime(2026, 5, 26, tzinfo=UTC),
        finished_at=datetime(2026, 5, 26, tzinfo=UTC),
        status=RunStatus.OK,
        report_id="rep1",
        report_hash="abc",
        report_storage="db",
        targets=[_TARGET],
        notify_results=[NotifyResult(channel="x", status="sent", attempts=1)],
    )
    await run_store.save(run)

    rows = await run_store.list_recent(limit=10)
    assert len(rows) == 1
    assert isinstance(rows[0].notify_results[0], NotifyResult)
    assert rows[0].notify_results[0].channel == "x"
