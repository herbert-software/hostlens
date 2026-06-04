"""APScheduler ``AsyncIOScheduler`` wrapper — job registration + result mapping.

Spec: ``openspec/changes/add-scheduler/specs/scheduler-engine/spec.md``
(design D-1 / D-2b / D-3b / D-7 / D-11).

`SchedulerRunner` registers each `ScheduleManifest` as one `AsyncIOScheduler`
job (`job_id == manifest.name`), reuses the delivery-layer-agnostic
`run_diagnosis_pipeline` as the job body, and maps the pipeline outcome to a
`RunStatus`:

- pipeline returns a `Report` → `ReportStore.save` then a `Run` with
  `report_id` + `report_hash` (`ok` for `ReportStatus.OK`, `partial` for any
  degraded status, including `degraded_token_budget` / `degraded_max_turns` —
  **never** `budget_exhausted`, which M4 never constructs; design D-3b);
- pipeline returns `None` with sink-captured ``terminal_status ==
  "failed_api_unavailable"`` → `failed_api_unavailable` (no Report);
- pipeline returns `None` otherwise (empty collection) → `failed` with an
  error note (design D-2b);
- a save that degraded to an orphan JSON file
  (`SaveResult.stored_as_orphan`) → `partial` + `report_storage="orphan"`
  rather than a silently-`ok` row whose `report_id` `get_run` cannot resolve
  (design D-11).

The job body always writes its own terminal `Run`; the APScheduler event
listener writes a `Run` only for the scheduling-layer outcomes the job body
never reached: `EVENT_JOB_MISSED → missed`, `EVENT_JOB_MAX_INSTANCES →
skipped_due_to_running`, `EVENT_JOB_ERROR → failed`. `EVENT_JOB_EXECUTED`
writes nothing (the job body already persisted; design D-7), so the two
sources never double-write the same `Run`.

Dependencies are injected through the constructor (no module-level singleton):
the `RunStore` / `ReportStore` / `Settings`, a `backend_factory` (one fresh
`LLMBackend` per fire — the backend reaches only the agent loops, never a
`ToolContext`, ADR-008), a `context_factory` (fresh `ToolContext` per
dispatch), and the `TargetRegistry` used to resolve each manifest's single
target's type.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, cast

import structlog
from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_MAX_INSTANCES,
    EVENT_JOB_MISSED,
    JobExecutionEvent,
    JobSubmissionEvent,
)
from apscheduler.schedulers import SchedulerNotRunningError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from hostlens.core.redact import redact_text
from hostlens.orchestration.pipeline import run_diagnosis_pipeline
from hostlens.reporting.models import Report, ReportStatus
from hostlens.scheduler.store import Run, RunStatus, RunStore, compute_report_hash

if TYPE_CHECKING:
    from collections.abc import Callable

    from apscheduler.events import SchedulerEvent

    from hostlens.agent.backend import LLMBackend
    from hostlens.agent.planner import PlannerResult
    from hostlens.core.config import Settings
    from hostlens.reporting.store import ReportStore
    from hostlens.scheduler.schema import ScheduleManifest
    from hostlens.targets.registry import TargetRegistry
    from hostlens.tools.base import ToolContext

__all__ = ["SchedulerRunner"]

_log = structlog.get_logger(__name__)

# Fixed cron misfire grace (design D-7): a conservative 5-minute window that
# absorbs a brief machine sleep / stall without misreading a normal delay as a
# miss. M4 does not make this per-manifest configurable (non-goal).
_CRON_MISFIRE_GRACE_SECONDS = 300

# Interval misfire grace floor (design D-7): half the interval, never below
# 30s so a fast interval (e.g. 10s) still tolerates a short stall.
_INTERVAL_MISFIRE_FLOOR_SECONDS = 30

# Default graceful-shutdown grace (design D-5, unrelated to misfire grace):
# how long ``graceful_stop`` waits for an in-flight job to finish naturally
# before force-cancelling it. Conservative fixed default; injectable so tests
# drive the force-cancel path with a tiny value.
#
# NOTE: this 30s is only the library-internal fallback for code that
# constructs ``SchedulerRunner`` directly without passing ``grace_seconds``
# (tests, advanced lib use). The production source of truth is
# ``DaemonSettings.shutdown_grace_seconds`` (default 120s); the daemon / run
# CLI paths both go through ``cli/schedule.py:_build_runner``, which injects
# that settings value — they never rely on this default.
_GRACE_SECONDS = 30.0


def _interval_total_seconds(manifest: ScheduleManifest) -> int:
    """Total seconds an `IntervalSpec` represents (design D-7).

    ``weeks*604800 + days*86400 + hours*3600 + minutes*60 + seconds``;
    omitted fields default to 0 on the schema model.
    """
    spec = manifest.schedule.interval
    if spec is None:  # pragma: no cover - guarded by caller (interval branch only)
        raise ValueError("interval misfire grace requested for a non-interval manifest")
    return (
        spec.weeks * 604800
        + spec.days * 86400
        + spec.hours * 3600
        + spec.minutes * 60
        + spec.seconds
    )


class SchedulerRunner:
    """Owns an `AsyncIOScheduler`, one job per `ScheduleManifest`.

    The scheduler / job registration / listener are wired in `__init__`;
    `start` / `shutdown` drive the underlying scheduler. `trigger` runs one
    manifest's job body immediately on the calling event loop (the same body
    the timer fires), so tests and the `schedule trigger` CLI share one
    execution path without depending on real timing.
    """

    def __init__(
        self,
        manifests: list[ScheduleManifest],
        *,
        run_store: RunStore,
        report_store: ReportStore,
        settings: Settings,
        backend_factory: Callable[[], LLMBackend],
        context_factory: Callable[[], ToolContext],
        target_registry: TargetRegistry,
        clock: Callable[[], datetime] | None = None,
        grace_seconds: float = _GRACE_SECONDS,
    ) -> None:
        self._manifests = {m.name: m for m in manifests}
        self._run_store = run_store
        self._report_store = report_store
        self._settings = settings
        self._backend_factory = backend_factory
        self._context_factory = context_factory
        self._target_registry = target_registry
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._grace_seconds = grace_seconds

        # In-flight job tasks (design D-5). The ``AsyncIOExecutor`` creates the
        # job task internally and never hands it back, so each job body
        # registers ``asyncio.current_task()`` here on entry (before any
        # ``await``) and discards it in ``finally``. ``graceful_stop`` waits on
        # this set, force-cancels survivors past the grace, then drains them.
        self._inflight: set[asyncio.Task[Run]] = set()
        # Fire-and-forget listener saves (missed / skipped / failed Run rows).
        # The event loop only holds a weak reference to a bare ``create_task``,
        # so we keep a strong reference here until the save settles; the
        # done-callback discards it and surfaces save failures (§4.6 leaves a
        # trace for every fire). ``graceful_stop`` drains this set before
        # shutting the scheduler down so a stop never loses these rows.
        self._listener_tasks: set[asyncio.Task[None]] = set()
        # Idempotent stop guard: a second SIGTERM/SIGINT during shutdown must
        # not re-cancel tasks (would re-raise CancelledError outside the job
        # body's already-executed ``except`` block).
        self._stopping = False

        self._scheduler = AsyncIOScheduler()
        self._register_jobs()
        self._scheduler.add_listener(
            self._on_scheduler_event,
            EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES | EVENT_JOB_ERROR | EVENT_JOB_EXECUTED,
        )

    @property
    def scheduler(self) -> AsyncIOScheduler:
        return self._scheduler

    def start(self) -> None:
        self._scheduler.start()

    def shutdown(self, *, wait: bool = False) -> None:
        self._scheduler.shutdown(wait=wait)

    async def graceful_stop(self) -> None:
        """SIGTERM/SIGINT graceful shutdown sequence (design D-5).

        Idempotent: a second call (or a second signal that routed here) while
        a stop is already in progress is a no-op, so survivors are never
        re-cancelled.

        Sequence:

        1. ``scheduler.pause()`` — stop dispatching new fires (no new job
           starts).
        2. ``asyncio.wait(inflight, timeout=grace)`` — bounded wait for the
           current job(s) to finish naturally. A job that completes here lands
           its real terminal status (NOT ``daemon_stopped``).
        3. force-cancel everything still pending past the grace; its job body
           ``except CancelledError`` writes a shielded ``daemon_stopped`` row.
        4. ``await asyncio.gather(*pending)`` — drain the cancelled tasks so
           their shielded saves complete before the event loop closes (shield
           protects the save from cancel; drain keeps the loop alive until the
           save finishes — both required, D-5).
        5. ``scheduler.shutdown(wait=False)`` — APScheduler ``shutdown()`` is
           synchronous and returns None; ``wait=True`` here would self-deadlock
           on this loop, so we drained ourselves above and pass ``wait=False``.
        """

        if self._stopping:
            return
        self._stopping = True

        # ``pause`` / ``shutdown`` raise ``SchedulerNotRunningError`` if the
        # scheduler was never started (e.g. graceful_stop driven directly in a
        # test, or a boot that aborted before ``start``). Stopping an
        # already-stopped scheduler is a no-op, so we tolerate that — the
        # in-flight drain below is what actually matters.
        with suppress(SchedulerNotRunningError):
            self._scheduler.pause()

        inflight = set(self._inflight)
        if inflight:
            _done, pending = await asyncio.wait(inflight, timeout=self._grace_seconds)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        # Drain fire-and-forget listener saves so a stop never loses the
        # missed / skipped / failed Run rows scheduled by the event listener.
        if self._listener_tasks:
            await asyncio.gather(*list(self._listener_tasks), return_exceptions=True)

        with suppress(SchedulerNotRunningError):
            self._scheduler.shutdown(wait=False)

    # ------------------------------------------------------------------ #
    # Job registration
    # ------------------------------------------------------------------ #

    def _register_jobs(self) -> None:
        for manifest in self._manifests.values():
            trigger, misfire_grace = self._build_trigger(manifest)
            self._scheduler.add_job(
                self._run_job,
                trigger=trigger,
                id=manifest.name,
                args=[manifest.name],
                max_instances=1,
                coalesce=True,
                misfire_grace_time=misfire_grace,
            )

    def _build_trigger(
        self, manifest: ScheduleManifest
    ) -> tuple[CronTrigger | IntervalTrigger, int]:
        spec = manifest.schedule
        if spec.cron is not None:
            trigger: CronTrigger | IntervalTrigger = CronTrigger.from_crontab(
                spec.cron, timezone=spec.timezone
            )
            return trigger, _CRON_MISFIRE_GRACE_SECONDS

        interval = spec.interval
        if interval is None:  # pragma: no cover - schema XOR guarantees one is set
            raise ValueError(f"manifest {manifest.name!r} has neither cron nor interval")
        trigger = IntervalTrigger(
            weeks=interval.weeks,
            days=interval.days,
            hours=interval.hours,
            minutes=interval.minutes,
            seconds=interval.seconds,
            timezone=spec.timezone,
        )
        grace = max(_INTERVAL_MISFIRE_FLOOR_SECONDS, _interval_total_seconds(manifest) // 2)
        return trigger, grace

    # ------------------------------------------------------------------ #
    # Job body (shared by the timer and `trigger`)
    # ------------------------------------------------------------------ #

    async def trigger(self, name: str) -> Run:
        """Run one manifest's job body immediately, returning the persisted `Run`.

        Raises `KeyError` for an unknown name (fail-loud).
        """
        if name not in self._manifests:
            raise KeyError(f"unknown schedule name: {name!r}")
        manifest = self._manifests[name]
        triggered_at = self._clock()
        try:
            return await self._run_job(name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The timer path routes unexpected job exceptions through APScheduler's
            # EVENT_JOB_ERROR listener, which persists a Run(failed). `trigger`
            # bypasses that listener (it calls `_run_job` directly), so persist the
            # failure row here to keep both execution paths consistent (§4.6), then
            # re-raise so the CLI still exits non-zero.
            run = Run(
                run_id=str(uuid.uuid4()),
                schedule_name=manifest.name,
                triggered_at=triggered_at,
                started_at=triggered_at,
                finished_at=self._clock(),
                status=RunStatus.FAILED,
                report_id=None,
                error=redact_text(str(exc)),
                targets=list(manifest.targets),
                inspectors=[],
                report_hash=None,
                report_storage=None,
            )
            await self._run_store.save(run)
            raise

    async def _run_job(self, name: str) -> Run:
        # Register this task in the in-flight set BEFORE any ``await`` so a
        # SIGTERM arriving mid-job sees it (design D-5). ``current_task()`` is
        # never None inside a running coroutine.
        task = asyncio.current_task()
        assert task is not None
        inflight_task: asyncio.Task[Run] = cast("asyncio.Task[Run]", task)
        self._inflight.add(inflight_task)

        manifest = self._manifests[name]
        target_name = manifest.targets[0]
        triggered_at = self._clock()
        started_at = self._clock()
        try:
            target_type = self._target_registry.get(target_name).type

            captured: dict[str, str | None] = {"terminal_status": None}

            def _sink(result: PlannerResult) -> None:
                captured["terminal_status"] = result.loop_result.terminal_status

            backend = self._backend_factory()
            try:
                # Only ``intent`` drives the pipeline: the Planner autonomously
                # selects inspectors from the registry (Agent-loop design,
                # CLAUDE.md §4.2). ``manifest.inspectors`` is a soft hint parsed
                # but NOT consumed in M4 — injecting it into the Planner context
                # is a later milestone (see schedule-manifest spec).
                report = await run_diagnosis_pipeline(
                    backend,
                    self._settings,
                    self._context_factory,
                    report_target_name=target_name,
                    target_lookup_name=target_name,
                    target_type=target_type,
                    intent=manifest.intent,
                    planner_result_sink=_sink,
                    schedule_name=manifest.name,
                )
            except asyncio.CancelledError:
                # Force-cancelled while the pipeline was still executing (design
                # D-5). Persist a terminal ``daemon_stopped`` row, then re-raise.
                # The save MUST be shielded — this coroutine is already
                # cancelled, so an unshielded ``await`` would re-raise
                # CancelledError immediately and the row would never reach
                # runs.db. ``graceful_stop`` then drains this task (await it) so
                # the shielded save completes before the loop closes — shield +
                # drain, both required (design D-5).
                daemon_stopped = Run(
                    run_id=str(uuid.uuid4()),
                    schedule_name=manifest.name,
                    triggered_at=triggered_at,
                    started_at=started_at,
                    finished_at=self._clock(),
                    status=RunStatus.DAEMON_STOPPED,
                    targets=[target_name],
                )
                await asyncio.shield(self._run_store.save(daemon_stopped))
                raise

            # The pipeline produced a result. The terminal write (report_store +
            # run_store) MUST complete atomically: a cancel arriving here is past
            # the daemon_stopped window, so it must not split the write or land a
            # second daemon_stopped row (one fire → one Run). Run the finalize as
            # an explicit task and shield the await; if a late cancel lands we
            # ``await`` that same task to completion in the ``except`` (the
            # shielded write keeps running) BEFORE propagating — so the row is
            # persisted deterministically. A bare ``await asyncio.shield(coro)``
            # would NOT suffice: on cancel the shielded coroutine detaches and
            # ``graceful_stop`` only drains this job task (already finished by
            # raising), never the detached write → the row could be lost.
            finalize_task: asyncio.Task[Run] = asyncio.ensure_future(
                self._finalize_outcome(
                    manifest=manifest,
                    target_name=target_name,
                    triggered_at=triggered_at,
                    started_at=started_at,
                    report=report,
                    terminal_status=captured["terminal_status"],
                )
            )
            try:
                return await asyncio.shield(finalize_task)
            except asyncio.CancelledError:
                # Cancel during the terminal write: let the shielded write finish
                # and await it (row lands), then re-raise — no daemon_stopped here
                # (past the pipeline window), so still one fire → one Run.
                await finalize_task
                raise
        finally:
            self._inflight.discard(inflight_task)

    async def _finalize_outcome(
        self,
        *,
        manifest: ScheduleManifest,
        target_name: str,
        triggered_at: datetime,
        started_at: datetime,
        report: Report | None,
        terminal_status: str | None,
    ) -> Run:
        """Map the pipeline outcome to a `Run` and persist it atomically.

        Wrapped in a single `asyncio.shield` by the caller so both the
        report_store and run_store writes complete even if a cancel lands
        after the pipeline (design D-5 / "one fire → one Run").
        """
        run = await self._map_outcome(
            manifest=manifest,
            target_name=target_name,
            triggered_at=triggered_at,
            started_at=started_at,
            report=report,
            terminal_status=terminal_status,
        )
        await self._run_store.save(run)
        return run

    async def _map_outcome(
        self,
        *,
        manifest: ScheduleManifest,
        target_name: str,
        triggered_at: datetime,
        started_at: datetime,
        report: Report | None,
        terminal_status: str | None,
    ) -> Run:
        finished_at = self._clock()
        run_id = str(uuid.uuid4())

        if report is None:
            if terminal_status == "failed_api_unavailable":
                return Run(
                    run_id=run_id,
                    schedule_name=manifest.name,
                    triggered_at=triggered_at,
                    started_at=started_at,
                    finished_at=finished_at,
                    status=RunStatus.FAILED_API_UNAVAILABLE,
                    targets=[target_name],
                )
            return Run(
                run_id=run_id,
                schedule_name=manifest.name,
                triggered_at=triggered_at,
                started_at=started_at,
                finished_at=finished_at,
                status=RunStatus.FAILED,
                error="pipeline produced no inspector results",
                targets=[target_name],
            )

        # Report path: persist first, then write a Run pointing at it.
        # ``Report.from_inspector_results`` always sets meta; assert to narrow.
        assert report.meta is not None
        report_status = report.meta.status
        run_status = RunStatus.OK if report_status == ReportStatus.OK else RunStatus.PARTIAL

        report_hash = compute_report_hash(report)
        save_result = await self._report_store.save(report)
        storage: Literal["db", "orphan"]
        if save_result.stored_as_orphan:
            # An orphan save is still "a Report exists" but get_run cannot
            # resolve it; never silently write ok/db (design D-11).
            run_status = RunStatus.PARTIAL
            storage = "orphan"
        else:
            storage = "db"

        inspectors = [ir.name for ir in report.meta.inspectors_used]
        return Run(
            run_id=run_id,
            schedule_name=manifest.name,
            triggered_at=triggered_at,
            started_at=started_at,
            finished_at=finished_at,
            status=run_status,
            report_id=save_result.run_id,
            report_hash=report_hash,
            report_storage=storage,
            targets=[target_name],
            inspectors=inspectors,
        )

    # ------------------------------------------------------------------ #
    # Scheduling-layer listener
    # ------------------------------------------------------------------ #

    def _on_scheduler_event(self, event: SchedulerEvent) -> None:
        """Translate a scheduling-layer event into a no-Report `Run`.

        Runs synchronously in the scheduler's dispatch; the async
        `RunStore.save` is scheduled onto the running event loop. Only the
        three scheduling-layer outcomes the job body never reached are
        persisted here — `EVENT_JOB_EXECUTED` writes nothing (the job body
        already persisted its own terminal Run; design D-7).
        """
        run = self._event_to_run(event)
        if run is None:
            return
        task = asyncio.get_running_loop().create_task(self._run_store.save(run))
        self._listener_tasks.add(task)
        task.add_done_callback(self._on_listener_save_done)

    def _on_listener_save_done(self, task: asyncio.Task[object]) -> None:
        """Discard a settled listener save; surface a failed save (§4.6)."""
        self._listener_tasks.discard(cast("asyncio.Task[None]", task))
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _log.error("scheduler.listener_run_save_failed", error=str(exc))

    def _event_to_run(self, event: SchedulerEvent) -> Run | None:
        """Map a listener event to a `Run`, or `None` to write nothing.

        Pure (no IO) so tests can assert the mapping without an event loop.
        """
        if event.code == EVENT_JOB_EXECUTED:
            # The job body already persisted ok/partial/failed_api_unavailable;
            # writing here would double-write the same Run (design D-7).
            return None

        job_id = event.job_id
        if job_id not in self._manifests:
            return None
        manifest = self._manifests[job_id]
        target_name = manifest.targets[0]
        triggered_at = self._event_scheduled_time(event)

        if event.code == EVENT_JOB_MISSED:
            status = RunStatus.MISSED
        elif event.code == EVENT_JOB_MAX_INSTANCES:
            status = RunStatus.SKIPPED_DUE_TO_RUNNING
        elif event.code == EVENT_JOB_ERROR:
            status = RunStatus.FAILED
        else:  # pragma: no cover - listener mask only delivers the four codes
            return None

        error: str | None = None
        if status is RunStatus.FAILED and isinstance(event, JobExecutionEvent):
            error = (
                redact_text(str(event.exception)) if event.exception is not None else "job raised"
            )

        # A FAILED job already started (it raised mid-run); approximate its
        # start with the event time. missed/skipped never started — the
        # invariant requires started_at=None there.
        started_at = triggered_at if status is RunStatus.FAILED else None

        return Run(
            run_id=str(uuid.uuid4()),
            schedule_name=manifest.name,
            triggered_at=triggered_at,
            started_at=started_at,
            status=status,
            error=error,
            targets=[target_name],
        )

    def _event_scheduled_time(self, event: SchedulerEvent) -> datetime:
        """The fire time an event refers to, falling back to the clock.

        `JobExecutionEvent` / `JobSubmissionEvent` carry a `scheduled_run_time`
        (tz-aware); `EVENT_JOB_MAX_INSTANCES` is a submission event that may
        carry a list, so we take the earliest. Falls back to the injected
        clock when absent.
        """
        if isinstance(event, JobExecutionEvent) and event.scheduled_run_time is not None:
            scheduled: datetime = event.scheduled_run_time
            return scheduled
        if isinstance(event, JobSubmissionEvent) and event.scheduled_run_times:
            earliest: datetime = min(event.scheduled_run_times)
            return earliest
        return self._clock()
