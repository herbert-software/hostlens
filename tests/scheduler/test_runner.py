"""Unit tests for `SchedulerRunner` — job body result mapping + listener (task 4.4).

Spec: ``openspec/changes/add-scheduler/specs/scheduler-engine/spec.md``
(design D-2b / D-3b / D-7 / D-11).

Every backend is fake and every fire is driven by `trigger` (the shared job
body) or by calling the pure mapping helpers directly — nothing depends on
real timing or a real wall clock. Seven scenarios, mirroring the task list:

1. normal Report → `ok` row with report_id / report_hash / report_storage="db";
2. token-budget-degraded Report (`degraded_token_budget`) → `partial`
   (NOT the no-Report `budget_exhausted`);
3. backend unavailable (`terminal_status == failed_api_unavailable`, no Report)
   → `failed_api_unavailable`;
4. empty collection (`terminal_status != failed_api_unavailable`, no Report)
   → `failed` with the no-results note (not mis-recorded as api-unavailable);
5. orphan save (`SaveResult.stored_as_orphan`) → `partial` +
   `report_storage="orphan"`;
6. `EVENT_JOB_MAX_INSTANCES` → `skipped_due_to_running`;
7. `EVENT_JOB_ERROR` → `failed`, and the scheduler stays alive.

``asyncio_mode = "auto"`` (pyproject) — no marker needed.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
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
from hostlens.core.exceptions import BackendUnavailable
from hostlens.inspectors.registry import (
    InspectorRegistry,
    build_registry_from_search_paths,
)
from hostlens.inspectors.result import InspectorResult
from hostlens.reporting.models import Finding, Report, ReportStatus, TokenUsage
from hostlens.reporting.store import ReportStore, SaveResult
from hostlens.scheduler.runner import SchedulerRunner
from hostlens.scheduler.schema import (
    IntervalSpec,
    ReportConfig,
    ScheduleManifest,
    ScheduleSpec,
)
from hostlens.scheduler.store import RunStatus, RunStore, compute_report_hash
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

_TARGET = "local-host"
_RUN_INSPECTOR_INPUT = {"target_name": _TARGET, "inspector_name": "hello.echo"}


# --------------------------------------------------------------------------- #
# Backend scripting + wiring helpers
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


def _happy_script() -> list[MessageResponse]:
    # Planner runs one inspector then finalizes; Diagnostician finalizes.
    return [_planner_run_inspector(), _end_turn("巡检完成。"), _end_turn("诊断完成。")]


def _empty_collection_script() -> list[MessageResponse]:
    # Planner finalizes without ever calling run_inspector → collector stays
    # empty. The pipeline still runs the Diagnostician (which also finalizes)
    # before the post-diagnosis emptiness check returns None with
    # terminal_status == "ok" (NOT api-unavailable).
    return [_end_turn("无需巡检。"), _end_turn("无可诊断。")]


class _UnavailableBackend:
    """Backend that always raises ``BackendUnavailable`` (drives the loop's
    retry-exhausted ``failed_api_unavailable`` path)."""

    name = "unavailable"

    def __init__(self) -> None:
        self.capabilities = FakeBackend(responses=[]).capabilities

    async def messages_create(self, **_kwargs: Any) -> MessageResponse:
        raise BackendUnavailable("simulated outage", backend_name="unavailable")


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
            logger=structlog.get_logger("test_runner"),
            approval_service=NoopApprovalService(),
            cancel=asyncio.Event(),
        )

    return _make


def _manifest(name: str = "nightly") -> ScheduleManifest:
    return ScheduleManifest(
        name=name,
        schedule=ScheduleSpec(interval=IntervalSpec(minutes=10), timezone="UTC"),
        targets=[_TARGET],
        intent="检查健康",
        report=ReportConfig(),
    )


def _build_runner(
    *,
    backend_factory: Any,
    run_store: RunStore,
    report_store: ReportStore,
    manifests: list[ScheduleManifest] | None = None,
) -> SchedulerRunner:
    target_registry = _make_target_registry()
    inspector_registry = _make_inspector_registry()
    return SchedulerRunner(
        manifests if manifests is not None else [_manifest()],
        run_store=run_store,
        report_store=report_store,
        settings=_settings(),
        backend_factory=backend_factory,
        context_factory=_context_factory(target_registry, inspector_registry),
        target_registry=target_registry,
    )


def _stores(tmp_path: Path) -> tuple[RunStore, ReportStore]:
    return (
        RunStore(db_path=tmp_path / "runs.db"),
        ReportStore(db_path=tmp_path / "reports.db", orphan_dir=tmp_path / "orphans"),
    )


# --------------------------------------------------------------------------- #
# 1. normal Report → ok, persisted + retrievable
# --------------------------------------------------------------------------- #


@_POSIX_ONLY
async def test_trigger_ok_persists_run_and_report(tmp_path: Path) -> None:
    run_store, report_store = _stores(tmp_path)
    runner = _build_runner(
        backend_factory=lambda: cast(LLMBackend, FakeBackend(responses=_happy_script())),
        run_store=run_store,
        report_store=report_store,
    )

    run = await runner.trigger("nightly")

    assert run.status is RunStatus.OK
    assert run.report_id is not None
    assert run.report_hash is not None
    assert run.report_storage == "db"
    assert run.targets == [_TARGET]
    assert run.started_at is not None
    # report_id resolves in reports.db (db storage), and the row is in runs.db.
    assert await report_store.get_run(run.report_id) is not None
    persisted = await run_store.list_recent(limit=10)
    assert [r.run_id for r in persisted] == [run.run_id]


# --------------------------------------------------------------------------- #
# 2. token-budget-degraded Report → partial (not budget_exhausted)
# --------------------------------------------------------------------------- #


def _degraded_report(status: ReportStatus) -> Report:
    ir = InspectorResult(
        name="hello.echo",
        version="1.0.0",
        status="ok",
        target_name=_TARGET,
        duration_seconds=0.1,
        findings=[Finding(severity="info", message="ok")],
        error=None,
        missing=[],
    )
    return Report.from_inspector_results(
        _TARGET,
        [ir],
        intent="检查健康",
        started_at=datetime(2026, 5, 26, tzinfo=UTC),
        finished_at=datetime(2026, 5, 26, tzinfo=UTC),
        status=status,
        token_usage=TokenUsage(),
        target_type="local",
    )


@_POSIX_ONLY
async def test_token_budget_report_maps_to_partial(tmp_path: Path) -> None:
    run_store, report_store = _stores(tmp_path)
    runner = _build_runner(
        backend_factory=lambda: cast(LLMBackend, FakeBackend(responses=[])),
        run_store=run_store,
        report_store=report_store,
    )

    report = _degraded_report(ReportStatus.DEGRADED_TOKEN_BUDGET)
    run = await runner._map_outcome(
        manifest=_manifest(),
        target_name=_TARGET,
        triggered_at=datetime(2026, 5, 26, tzinfo=UTC),
        started_at=datetime(2026, 5, 26, tzinfo=UTC),
        report=report,
        terminal_status="degraded_token_budget",
    )

    assert run.status is RunStatus.PARTIAL
    assert run.status is not RunStatus.BUDGET_EXHAUSTED
    assert run.report_id is not None
    assert run.report_storage == "db"
    assert run.report_hash == compute_report_hash(report)


# --------------------------------------------------------------------------- #
# 3. backend unavailable, no Report → failed_api_unavailable
# --------------------------------------------------------------------------- #


@_POSIX_ONLY
async def test_backend_unavailable_maps_to_failed_api_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Zero the loop's unavailable backoff so retries do not sleep in the test.
    monkeypatch.setattr("hostlens.agent.loop._UNAVAILABLE_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    run_store, report_store = _stores(tmp_path)
    runner = _build_runner(
        backend_factory=lambda: cast(LLMBackend, _UnavailableBackend()),
        run_store=run_store,
        report_store=report_store,
    )

    run = await runner.trigger("nightly")

    assert run.status is RunStatus.FAILED_API_UNAVAILABLE
    assert run.report_id is None
    assert run.report_hash is None
    assert run.report_storage is None
    # No Report was written.
    assert await report_store.list_runs(_TARGET, limit=10) == []


# --------------------------------------------------------------------------- #
# 4. empty collection, no Report → failed (not api-unavailable)
# --------------------------------------------------------------------------- #


@_POSIX_ONLY
async def test_empty_collection_maps_to_failed(tmp_path: Path) -> None:
    run_store, report_store = _stores(tmp_path)
    runner = _build_runner(
        backend_factory=lambda: cast(LLMBackend, FakeBackend(responses=_empty_collection_script())),
        run_store=run_store,
        report_store=report_store,
    )

    run = await runner.trigger("nightly")

    assert run.status is RunStatus.FAILED
    assert run.status is not RunStatus.FAILED_API_UNAVAILABLE
    assert run.error == "pipeline produced no inspector results"
    assert run.report_id is None


# --------------------------------------------------------------------------- #
# 5. orphan save → partial + report_storage="orphan"
# --------------------------------------------------------------------------- #


class _OrphanReportStore:
    """ReportStore stub whose ``save`` always degrades to an orphan file."""

    async def save(self, report: Report) -> SaveResult:
        _ = report
        return SaveResult(
            run_id="00000000-0000-0000-0000-000000000001",
            stored_as_orphan=True,
            orphan_path="/tmp/orphan.json",
        )


@_POSIX_ONLY
async def test_orphan_save_maps_to_partial_with_orphan_storage(tmp_path: Path) -> None:
    run_store = RunStore(db_path=tmp_path / "runs.db")
    runner = _build_runner(
        backend_factory=lambda: cast(LLMBackend, FakeBackend(responses=[])),
        run_store=run_store,
        report_store=cast(ReportStore, _OrphanReportStore()),
    )

    report = _degraded_report(ReportStatus.OK)
    run = await runner._map_outcome(
        manifest=_manifest(),
        target_name=_TARGET,
        triggered_at=datetime(2026, 5, 26, tzinfo=UTC),
        started_at=datetime(2026, 5, 26, tzinfo=UTC),
        report=report,
        terminal_status="ok",
    )

    # An ok Report whose save degraded must NOT be written as ok/db.
    assert run.status is RunStatus.PARTIAL
    assert run.report_storage == "orphan"
    assert run.report_id == "00000000-0000-0000-0000-000000000001"
    assert run.report_hash == compute_report_hash(report)


# --------------------------------------------------------------------------- #
# 6. max_instances rejection → skipped_due_to_running
# --------------------------------------------------------------------------- #


def _make_max_instances_event(job_id: str) -> Any:
    from apscheduler.events import EVENT_JOB_MAX_INSTANCES, JobSubmissionEvent

    return JobSubmissionEvent(
        EVENT_JOB_MAX_INSTANCES,
        job_id,
        "default",
        [datetime(2026, 5, 26, 12, 0, tzinfo=UTC)],
    )


@_POSIX_ONLY
async def test_max_instances_event_maps_to_skipped(tmp_path: Path) -> None:
    run_store, report_store = _stores(tmp_path)
    runner = _build_runner(
        backend_factory=lambda: cast(LLMBackend, FakeBackend(responses=[])),
        run_store=run_store,
        report_store=report_store,
    )

    run = runner._event_to_run(_make_max_instances_event("nightly"))

    assert run is not None
    assert run.status is RunStatus.SKIPPED_DUE_TO_RUNNING
    assert run.report_id is None
    assert run.started_at is None
    assert run.targets == [_TARGET]


@_POSIX_ONLY
async def test_executed_event_writes_no_run(tmp_path: Path) -> None:
    from apscheduler.events import EVENT_JOB_EXECUTED, JobExecutionEvent

    run_store, report_store = _stores(tmp_path)
    runner = _build_runner(
        backend_factory=lambda: cast(LLMBackend, FakeBackend(responses=[])),
        run_store=run_store,
        report_store=report_store,
    )

    event = JobExecutionEvent(
        EVENT_JOB_EXECUTED, "nightly", "default", datetime(2026, 5, 26, tzinfo=UTC)
    )
    # EVENT_JOB_EXECUTED must not double-write (job body already persisted).
    assert runner._event_to_run(event) is None


# --------------------------------------------------------------------------- #
# 7. job error → failed, scheduler stays alive
# --------------------------------------------------------------------------- #


@_POSIX_ONLY
async def test_job_error_event_maps_to_failed(tmp_path: Path) -> None:
    from apscheduler.events import EVENT_JOB_ERROR, JobExecutionEvent

    run_store, report_store = _stores(tmp_path)
    runner = _build_runner(
        backend_factory=lambda: cast(LLMBackend, FakeBackend(responses=[])),
        run_store=run_store,
        report_store=report_store,
    )

    event = JobExecutionEvent(
        EVENT_JOB_ERROR,
        "nightly",
        "default",
        datetime(2026, 5, 26, tzinfo=UTC),
        exception=RuntimeError("boom"),
    )
    run = runner._event_to_run(event)

    assert run is not None
    assert run.status is RunStatus.FAILED
    assert run.report_id is None
    assert run.error is not None
    assert "boom" in run.error
    # The scheduler is untouched by a single job error: it is still constructed
    # and able to keep dispatching (no crash on the listener path).
    assert runner.scheduler is not None


@_POSIX_ONLY
async def test_listener_persists_run_on_event_loop(tmp_path: Path) -> None:
    """The live listener (not just the pure mapper) lands a Run via the loop."""
    run_store, report_store = _stores(tmp_path)
    runner = _build_runner(
        backend_factory=lambda: cast(LLMBackend, FakeBackend(responses=[])),
        run_store=run_store,
        report_store=report_store,
    )

    runner._on_scheduler_event(_make_max_instances_event("nightly"))
    # The save is scheduled as a detached task on the running loop. Await every
    # other pending task (the save) before querying, so the read never races the
    # background write on the same SQLite file.
    current = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not current]
    if pending:
        await asyncio.gather(*pending)

    rows = await run_store.list_recent(limit=10)
    assert len(rows) == 1
    assert rows[0].status is RunStatus.SKIPPED_DUE_TO_RUNNING


@_POSIX_ONLY
async def test_unknown_trigger_name_raises(tmp_path: Path) -> None:
    run_store, report_store = _stores(tmp_path)
    runner = _build_runner(
        backend_factory=lambda: cast(LLMBackend, FakeBackend(responses=[])),
        run_store=run_store,
        report_store=report_store,
    )
    with pytest.raises(KeyError):
        await runner.trigger("does-not-exist")


# --------------------------------------------------------------------------- #
# F1 — graceful_stop drains fire-and-forget listener saves (no lost rows)
# --------------------------------------------------------------------------- #


@_POSIX_ONLY
async def test_graceful_stop_drains_listener_save(tmp_path: Path) -> None:
    run_store, report_store = _stores(tmp_path)
    runner = _build_runner(
        backend_factory=lambda: cast(LLMBackend, FakeBackend(responses=[])),
        run_store=run_store,
        report_store=report_store,
    )

    # Schedule a listener save (max-instances → skipped row), then stop without
    # awaiting the detached task: graceful_stop must drain it so the row lands.
    runner._on_scheduler_event(_make_max_instances_event("nightly"))
    await runner.graceful_stop()

    rows = await run_store.list_recent(limit=10)
    assert len(rows) == 1
    assert rows[0].status is RunStatus.SKIPPED_DUE_TO_RUNNING


# --------------------------------------------------------------------------- #
# G2 — listener save failure is surfaced via _log.error (§4.6 留痕 failure half)
# --------------------------------------------------------------------------- #


@_POSIX_ONLY
async def test_listener_save_failure_logs_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_store, report_store = _stores(tmp_path)
    runner = _build_runner(
        backend_factory=lambda: cast(LLMBackend, FakeBackend(responses=[])),
        run_store=run_store,
        report_store=report_store,
    )

    async def _boom(_run: Any) -> None:
        raise RuntimeError("save exploded")

    monkeypatch.setattr(run_store, "save", _boom)

    with structlog.testing.capture_logs() as logs:
        runner._on_scheduler_event(_make_max_instances_event("nightly"))
        current = asyncio.current_task()
        pending = [t for t in asyncio.all_tasks() if t is not current]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        # Let the done-callback (which logs the failure) fire before reading logs.
        await asyncio.sleep(0)

    failures = [e for e in logs if e.get("event") == "scheduler.listener_run_save_failed"]
    assert len(failures) == 1
    assert "save exploded" in failures[0]["error"]


@_POSIX_ONLY
async def test_cancelled_listener_save_logs_no_error(tmp_path: Path) -> None:
    run_store, report_store = _stores(tmp_path)
    runner = _build_runner(
        backend_factory=lambda: cast(LLMBackend, FakeBackend(responses=[])),
        run_store=run_store,
        report_store=report_store,
    )

    async def _never() -> None:
        await asyncio.Event().wait()

    task: asyncio.Task[None] = asyncio.get_running_loop().create_task(_never())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with structlog.testing.capture_logs() as logs:
        runner._on_listener_save_done(cast("asyncio.Task[object]", task))

    assert not [e for e in logs if e.get("event") == "scheduler.listener_run_save_failed"]


# --------------------------------------------------------------------------- #
# F2 — Run.error from a job exception is redacted before persistence
# --------------------------------------------------------------------------- #


@_POSIX_ONLY
async def test_job_error_event_redacts_secret_in_error(tmp_path: Path) -> None:
    from apscheduler.events import EVENT_JOB_ERROR, JobExecutionEvent

    run_store, report_store = _stores(tmp_path)
    runner = _build_runner(
        backend_factory=lambda: cast(LLMBackend, FakeBackend(responses=[])),
        run_store=run_store,
        report_store=report_store,
    )

    secret = "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789"
    event = JobExecutionEvent(
        EVENT_JOB_ERROR,
        "nightly",
        "default",
        datetime(2026, 5, 26, tzinfo=UTC),
        exception=RuntimeError(f"auth failed token={secret}"),
    )
    run = runner._event_to_run(event)

    assert run is not None
    assert run.error is not None
    assert secret not in run.error


# --------------------------------------------------------------------------- #
# F7-1 — graceful_stop is idempotent (a second call does not raise)
# --------------------------------------------------------------------------- #


@_POSIX_ONLY
async def test_graceful_stop_is_idempotent(tmp_path: Path) -> None:
    run_store, report_store = _stores(tmp_path)
    runner = _build_runner(
        backend_factory=lambda: cast(LLMBackend, FakeBackend(responses=[])),
        run_store=run_store,
        report_store=report_store,
    )

    await runner.graceful_stop()
    await runner.graceful_stop()


# --------------------------------------------------------------------------- #
# F7-2 — EVENT_JOB_MISSED → missed (no started_at, no report_id)
# --------------------------------------------------------------------------- #


@_POSIX_ONLY
async def test_missed_event_maps_to_missed(tmp_path: Path) -> None:
    from apscheduler.events import EVENT_JOB_MISSED, JobExecutionEvent

    run_store, report_store = _stores(tmp_path)
    runner = _build_runner(
        backend_factory=lambda: cast(LLMBackend, FakeBackend(responses=[])),
        run_store=run_store,
        report_store=report_store,
    )

    event = JobExecutionEvent(
        EVENT_JOB_MISSED,
        "nightly",
        "default",
        datetime(2026, 5, 26, 12, 0, tzinfo=UTC),
    )
    run = runner._event_to_run(event)

    assert run is not None
    assert run.status is RunStatus.MISSED
    assert run.started_at is None
    assert run.report_id is None
    assert run.targets == [_TARGET]
