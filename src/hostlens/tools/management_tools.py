"""MCP management ToolSpec batch + `register_mcp_management_tools` assembly.

This module declares the read-only MCP management ToolSpecs and the single
explicit assembly function that registers them. It mirrors
`tools/default_tools.py`:

- `@tool` is a pure spec factory: declaring a spec mutates **no**
  module-level registry (CLAUDE.md §4.10 rule 3).
- Handler dependencies arrive via **closure injection** through
  `ManagementToolDeps`, never through `ToolContext` (frozen at its
  ADR-008 six-field set) and never through a module-level singleton
  (design D-1).

The six read-only query tools (`list_schedules` / `get_schedule_status` /
`list_channels` / `list_reports` / `show_report` / `diff_reports`) are pure
store/loader projections; `run_schedule_now` (the seventh) consumes
`deps.build_runner` to reuse the scheduler trigger path with notify dispatch
suppressed. `register_mcp_management_tools` registers all seven.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from pydantic import BaseModel

from hostlens.agent.backend import create_backend
from hostlens.core.exceptions import ConfigError, ToolError
from hostlens.notifiers.base import redact_secret_text
from hostlens.notifiers.config import _parse_yaml
from hostlens.reporting.diff import compute_diff
from hostlens.reporting.store import ReportStore
from hostlens.scheduler.runner import SchedulerRunner
from hostlens.scheduler.schema import ScheduleManifest
from hostlens.scheduler.store import RunStore
from hostlens.tools.base import NoopApprovalService, ToolContext, ToolSpec
from hostlens.tools.decorators import tool
from hostlens.tools.registry import ToolRegistry
from hostlens.tools.schemas.diff_reports import DiffReportsInput, DiffReportsOutput
from hostlens.tools.schemas.get_schedule_status import (
    MAX_STATUS_LIMIT,
    GetScheduleStatusInput,
    GetScheduleStatusOutput,
    NotifyResultSummary,
    RunSummary,
)
from hostlens.tools.schemas.list_channels import (
    ChannelSummary,
    ListChannelsInput,
    ListChannelsOutput,
)
from hostlens.tools.schemas.list_reports import (
    ListReportsInput,
    ListReportsOutput,
    ReportIndexRow,
)
from hostlens.tools.schemas.list_schedules import (
    ListSchedulesInput,
    ListSchedulesOutput,
    ScheduleNotifyBinding,
    ScheduleSummary,
)
from hostlens.tools.schemas.run_schedule_now import (
    RunScheduleNowInput,
    RunScheduleNowOutput,
)
from hostlens.tools.schemas.show_report import ShowReportInput, ShowReportOutput

if TYPE_CHECKING:
    import structlog

    from hostlens.agent.backend import LLMBackend
    from hostlens.core.config import Settings
    from hostlens.notifiers.base import Notifier

__all__ = [
    "ManagementToolDeps",
    "build_diff_reports_spec",
    "build_get_schedule_status_spec",
    "build_list_channels_spec",
    "build_list_reports_spec",
    "build_list_schedules_spec",
    "build_run_schedule_now_spec",
    "build_show_report_spec",
    "make_build_runner",
    "make_daemon_safe_backend_factory",
    "make_load_channel_summaries",
    "register_mcp_management_tools",
]


# ---------------------------------------------------------------------------
# Dependency container (design D-2) — closure-injected, NOT a ToolContext field
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManagementToolDeps:
    """Frozen dependency container for the MCP management tools.

    Carries the scheduler / report / notifier-side dependencies the handlers
    need that do **not** live on `ToolContext`. Injected once at serve
    assembly via `register_mcp_management_tools(registry, deps=...)` and
    closure-bound into each handler.

    - `load_manifests`: fresh-loads + validates every `schedules/*.yaml`
      (target existence / shape / `only_if` syntax) on each call; raises
      `ConfigError` on a malformed / unknown-target / bad-`only_if` manifest.
      Does **not** validate notify-channel existence (that is checked at
      runner assembly).
    - `run_store`: the scheduler execution ledger (`runs.db`).
    - `report_store`: the report persistence store (`reports.db`).
    - `load_channel_summaries`: fresh-reads `notifiers.yaml` **raw** and
      returns `{name, type}` summaries — it does NOT go through
      `load_channels` (which expands `${ENV_VAR}` into plaintext secrets).
    - `build_runner`: factory producing a `SchedulerRunner` from the current
      `ToolContext` (target / inspector registry) + already-loaded manifests.
      Consumed only by `run_schedule_now` (assembled by a sibling group).
    """

    load_manifests: Callable[[], list[ScheduleManifest]]
    run_store: RunStore
    report_store: ReportStore
    load_channel_summaries: Callable[[], list[ChannelSummary]]
    build_runner: Callable[[ToolContext, list[ScheduleManifest]], SchedulerRunner]


# Mirrors `notifiers/config._PLACEHOLDER_PATTERN`: detects a `${...}` env-var
# placeholder so the raw channel reader can reject one written as a channel key
# or `type` value before it reaches the listing.
_ENV_PLACEHOLDER_PATTERN = re.compile(r"\$\{[^}]*\}")


# ---------------------------------------------------------------------------
# Runner factory (design D-8) — daemon-safe backend + assembly-data-equivalent
# `SchedulerRunner`, consumed only by `run_schedule_now`. Mirrors
# `cli/schedule.py:_build_runner` in *assembly data* (same store / channels /
# registry wiring) but NOT in error handling: an MCP process must never
# `typer.Exit`, so an assembly-time `ConfigError` is allowed to raise here and
# is caught one layer up by the handler / dispatch general-except.
# ---------------------------------------------------------------------------


def make_load_channel_summaries(
    settings: Settings,
) -> Callable[[], list[ChannelSummary]]:
    """Build the `deps.load_channel_summaries` raw-reader closure.

    The returned closure `() -> list[ChannelSummary]` fresh-reads
    `notifiers.yaml` on each call via `_parse_yaml` (the **raw**, un-expanded
    channels mapping — `load_channels`, which expands `${ENV_VAR}` into
    plaintext secrets, is deliberately NOT used) and projects every entry to a
    `{name, type}` summary. Only `name` (the instance key) and `type` ever
    reach the output: credentials (`bot_token` / `webhook_url` / `secret` /
    `chat_id` ...) and their `${ENV_VAR}` literals are dropped at the source
    because nothing else is copied. `ChannelSummary.extra="forbid"` seals the
    shape as a positive whitelist (`mcp-management-tools` spec
    §需求:`list_channels` 必须脱敏).

    An absent / empty `notifiers.yaml` yields an empty list (`_parse_yaml`
    returns an empty mapping). A malformed entry (non-mapping, or missing /
    empty `type`) is fail-loud with `ConfigError`, matching the `load_channels`
    contract so a broken channel never silently disappears from the listing.
    """

    def _load() -> list[ChannelSummary]:
        channels = _parse_yaml(settings.notifiers_config_path)
        summaries: list[ChannelSummary] = []
        for name, entry in channels.items():
            name_text = str(name)
            # The name check is hoisted above the two branches below that echo
            # `channel=name_text`: a `${ENV_VAR}` channel key would otherwise
            # reach the error envelope through those `channel=` kwargs (which
            # `scrub_exception_message` does not strip) whenever the same entry
            # is also malformed. Reject it fail-loud first, never echoing the
            # placeholder literal, so no branch can leak it.
            if _ENV_PLACEHOLDER_PATTERN.search(name_text):
                raise ConfigError(
                    "channel name must not contain a ${...} placeholder",
                    kind="secret_like_channel_field",
                )
            if not isinstance(entry, dict):
                raise ConfigError(
                    "channel entry must be a mapping",
                    kind="invalid_channel_entry",
                    channel=name_text,
                )
            channel_type = entry.get("type")
            if not isinstance(channel_type, str) or channel_type == "":
                raise ConfigError(
                    "channel entry missing `type`",
                    kind="missing_channel_type",
                    channel=name_text,
                )
            # The two branches above echo `name_text` (now guaranteed
            # placeholder-free), not `channel_type`; a `${ENV_VAR}` `type` is
            # rejected here without echoing its literal.
            if _ENV_PLACEHOLDER_PATTERN.search(channel_type):
                raise ConfigError(
                    "channel type must not contain a ${...} placeholder",
                    kind="secret_like_channel_field",
                )
            summaries.append(ChannelSummary(name=name_text, type=channel_type))
        return summaries

    return _load


def make_daemon_safe_backend_factory(settings: Settings) -> Callable[[], LLMBackend]:
    """Build a backend factory bound to a `daemon_mode=True` copy of `settings`.

    The MCP server is a long-running process accepting remote-LLM commands —
    exactly the daemon context CLAUDE.md §4.11 rule 3 forbids running an
    unsafe (e.g. subscription) backend in. ``create_backend`` only fires its
    ``ensure_safe_for_daemon`` gate when ``settings.daemon_mode is True``, but
    ``mcp serve`` does not set that flag, so this helper flips it once on a
    ``model_copy`` and closure-binds the result. Every call to the returned
    factory constructs a fresh backend from that single daemon-safe settings
    object.

    The **same** factory instance is used by serve both for the boot-time
    eager probe and (via ``make_build_runner``) for each per-fire
    construction, so the eager probe and the per-fire path provably bind the
    identical ``daemon_mode=True`` settings (the same-source invariant, design
    D-8). Returning the factory rather than the settings keeps that binding in
    one place.
    """

    daemon_settings = settings.model_copy(update={"daemon_mode": True})

    def _factory() -> LLMBackend:
        return create_backend(daemon_settings)

    return _factory


def make_build_runner(
    *,
    settings: Settings,
    run_store: RunStore,
    report_store: ReportStore,
    channels: dict[str, Notifier],
    backend_factory: Callable[[], LLMBackend],
    logger: structlog.stdlib.BoundLogger,
) -> Callable[[ToolContext, list[ScheduleManifest]], SchedulerRunner]:
    """Build the `deps.build_runner` factory closure.

    The returned closure ``(ctx, manifests) -> SchedulerRunner`` pulls the
    target / inspector registry, settings, and logger from the per-dispatch
    ``ctx`` and combines them with the serve-assembly closure dependencies
    (``run_store`` / ``report_store`` / ``channels`` / ``backend_factory``) at
    the call point. It builds an equivalent per-dispatch ``context_factory``
    (mirroring ``cli/schedule.py:_context_factory``) so each fire gets a fresh
    ``ToolContext`` whose backend never enters the context (ADR-008).

    Assembly-time validation (``SchedulerRunner.__init__`` →
    ``_validate_notify_channels``) may raise ``ConfigError`` for a manifest
    referencing an unknown channel; unlike the CLI's ``_build_runner`` this
    helper does **not** convert that into ``typer.Exit`` (forbidden
    in-process) — it lets the ``ConfigError`` propagate to the
    ``run_schedule_now`` handler / MCP dispatch general-except, which scrubs
    it into a structured error envelope.
    """

    def _build(ctx: ToolContext, manifests: list[ScheduleManifest]) -> SchedulerRunner:
        target_registry = ctx.target_registry
        inspector_registry = ctx.inspector_registry

        def _context_factory() -> ToolContext:
            return ToolContext(
                target_registry=target_registry,
                inspector_registry=inspector_registry,
                config=settings,
                logger=logger,
                approval_service=NoopApprovalService(),
                cancel=asyncio.Event(),
            )

        return SchedulerRunner(
            manifests,
            run_store=run_store,
            report_store=report_store,
            settings=settings,
            backend_factory=backend_factory,
            context_factory=_context_factory,
            target_registry=target_registry,
            channels=channels,
            grace_seconds=settings.daemon.shutdown_grace_seconds,
        )

    return _build


# ---------------------------------------------------------------------------
# Broad handler alias (matches `@tool` contravariant handler shape; see
# default_tools.py for the same cast rationale).
# ---------------------------------------------------------------------------

_BroadHandler = Callable[[BaseModel, Any], Awaitable[BaseModel]]


# ---------------------------------------------------------------------------
# Trigger → next_fire_time (tools/ owns this; MUST NOT import cli/ private
# `_next_fire_time`, per spec — tools never reverse-depend on cli).
# ---------------------------------------------------------------------------


def _next_fire_time(manifest: ScheduleManifest) -> datetime | None:
    """Compute the next fire instant for `manifest` from its trigger spec.

    Re-implemented here (NOT imported from `cli/schedule.py`) so `tools/`
    never reverse-depends on `cli/`. Uses the same apscheduler triggers the
    runner registers, so the value matches what the scheduler would fire.
    """
    tz = ZoneInfo(manifest.schedule.timezone)
    now = datetime.now(tz)
    spec = manifest.schedule
    trigger: CronTrigger | IntervalTrigger
    if spec.cron is not None:
        trigger = CronTrigger.from_crontab(spec.cron, timezone=tz)
    else:
        interval = spec.interval
        assert interval is not None
        trigger = IntervalTrigger(
            weeks=interval.weeks,
            days=interval.days,
            hours=interval.hours,
            minutes=interval.minutes,
            seconds=interval.seconds,
            timezone=tz,
        )
    fire: datetime | None = trigger.get_next_fire_time(None, now)
    return fire


def _schedule_expr(manifest: ScheduleManifest) -> str:
    """Render the human-readable trigger expression for a manifest."""
    spec = manifest.schedule
    if spec.cron is not None:
        return f"cron({spec.cron})"
    interval = spec.interval
    assert interval is not None
    parts = [
        f"{value}{unit}"
        for value, unit in (
            (interval.weeks, "w"),
            (interval.days, "d"),
            (interval.hours, "h"),
            (interval.minutes, "m"),
            (interval.seconds, "s"),
        )
        if value
    ]
    return f"interval({'+'.join(parts)})"


# ---------------------------------------------------------------------------
# Handlers (closure-bound to `deps` by the spec builders below)
# ---------------------------------------------------------------------------


async def _list_schedules_handler(
    args: ListSchedulesInput, ctx: ToolContext, *, deps: ManagementToolDeps
) -> ListSchedulesOutput:
    """Fresh-load every manifest and project to read-only summaries.

    `deps.load_manifests` re-walks `schedules/*.yaml` on every call and
    fail-loud raises `ConfigError` on a malformed / unknown-target /
    bad-`only_if` manifest (the MCP dispatch general-except wraps it into a
    scrubbed envelope — no special handling here). An empty `schedules/`
    dir yields an empty list. Notify-channel existence is NOT validated at
    load (only at runner assembly), so a manifest referencing an unknown
    channel still lists normally.
    """
    del ctx
    manifests = deps.load_manifests()
    summaries = [
        ScheduleSummary(
            name=manifest.name,
            schedule=_schedule_expr(manifest),
            next_fire_time=_next_fire_time(manifest),
            targets=list(manifest.targets),
            intent=manifest.intent,
            notify=[
                ScheduleNotifyBinding(channel=n.channel, only_if=n.only_if) for n in manifest.notify
            ],
        )
        for manifest in manifests
    ]
    return ListSchedulesOutput(schedules=summaries)


async def _get_schedule_status_handler(
    args: GetScheduleStatusInput, ctx: ToolContext, *, deps: ManagementToolDeps
) -> GetScheduleStatusOutput:
    """List the most-recent ledger `Run`s, redacting `notify_results`.

    `limit` is clamped to `[1, MAX_STATUS_LIMIT]` here (the store has no
    upper clamp — design D-7.5). Each `Run` is projected with both ledger
    `run_id` and report-store `report_id` (nullable for no-Report
    statuses); `notify_results[*].error` is passed through
    `redact_secret_text` so no channel secret reaches the surface. An
    absent / empty `runs.db` yields an empty list.
    """
    del ctx
    limit = min(args.limit, MAX_STATUS_LIMIT)
    runs = await deps.run_store.list_recent(schedule_name=args.name, limit=limit)
    summaries = [
        RunSummary(
            run_id=run.run_id,
            schedule_name=run.schedule_name,
            triggered_at=run.triggered_at,
            status=str(run.status),
            targets=list(run.targets),
            inspectors=list(run.inspectors),
            report_id=run.report_id,
            report_hash=run.report_hash,
            error=redact_secret_text(run.error) if run.error is not None else None,
            notify_results=[
                NotifyResultSummary(
                    channel=nr.channel,
                    status=nr.status,
                    error=redact_secret_text(nr.error) if nr.error is not None else None,
                    attempts=nr.attempts,
                )
                for nr in run.notify_results
            ],
        )
        for run in runs
    ]
    return GetScheduleStatusOutput(runs=summaries)


async def _list_channels_handler(
    args: ListChannelsInput, ctx: ToolContext, *, deps: ManagementToolDeps
) -> ListChannelsOutput:
    """Return `{name, type}` for every notifier channel (raw, no `${ENV}`).

    `deps.load_channel_summaries` reads `notifiers.yaml` raw and copies only
    `name` / `type` — credentials and `${ENV_VAR}` literals never enter the
    output (positive whitelist sealed by `ChannelSummary.extra="forbid"`).
    """
    del ctx, args
    return ListChannelsOutput(channels=deps.load_channel_summaries())


async def _list_reports_handler(
    args: ListReportsInput, ctx: ToolContext, *, deps: ManagementToolDeps
) -> ListReportsOutput:
    """List the report index for `args.target`, exposing the id as `report_id`.

    Reuses `ReportStore.list_runs(target_id)` 1:1 (no new store method). The
    `RunIndexRow.run_id` value is the report-store key, re-exposed under the
    name `report_id` (design D-7.3) so it is unambiguously the `show_report`
    key. An absent / empty `reports.db` yields an empty list.
    """
    del ctx
    rows = await deps.report_store.list_runs(args.target, limit=args.limit)
    reports = [
        ReportIndexRow(
            report_id=row.run_id,
            timestamp=row.timestamp,
            status=str(row.status),
            finding_count=row.finding_count,
        )
        for row in rows
    ]
    return ListReportsOutput(reports=reports)


async def _show_report_handler(
    args: ShowReportInput, ctx: ToolContext, *, deps: ManagementToolDeps
) -> ShowReportOutput:
    """Retrieve one `Report` by its report-store key (`report_id`).

    A missing key raises a plain `ToolError` (→ structured not-found
    envelope via MCP dispatch general-except). The error message references
    only the id, never an internal file path.
    """
    del ctx
    report = await deps.report_store.get_run(args.report_id)
    if report is None:
        raise ToolError(
            f"report_not_found: report_id={args.report_id!r} "
            "is not a stored report key (use list_reports to find valid report_id values)"
        )
    return ShowReportOutput(report=report)


async def _diff_reports_handler(
    args: DiffReportsInput, ctx: ToolContext, *, deps: ManagementToolDeps
) -> DiffReportsOutput:
    """Diff two reports keyed by report-store key (baseline `a`, current `b`).

    Both reports are fetched first; a missing key is a plain `ToolError`
    not-found **before** any `compute_diff` call. `compute_diff` only raises
    `ValueError` on a cross-`target_id` pair (schema / baseline mismatches
    are returned as a skipped diff, not raised); that `ValueError` is
    self-caught here and re-raised as a `ToolError` so the MCP dispatch
    surfaces a structured error envelope instead of a bare `ValueError`. The
    cli `_compute_diff_or_exit` is deliberately NOT imported (it raises
    `typer.Exit`, unusable in-process).
    """
    del ctx
    baseline = await deps.report_store.get_run(args.report_id_a)
    if baseline is None:
        raise ToolError(
            f"report_not_found: report_id_a={args.report_id_a!r} is not a stored report key"
        )
    current = await deps.report_store.get_run(args.report_id_b)
    if current is None:
        raise ToolError(
            f"report_not_found: report_id_b={args.report_id_b!r} is not a stored report key"
        )

    try:
        diff = compute_diff(baseline, current)
    except ValueError as exc:
        raise ToolError(f"diff_failed: {exc}") from exc
    return DiffReportsOutput(diff=diff)


async def _run_schedule_now_handler(
    args: RunScheduleNowInput, ctx: ToolContext, *, deps: ManagementToolDeps
) -> RunScheduleNowOutput:
    """Fire one schedule's diagnosis pipeline immediately, suppressing notify.

    Fresh-loads manifests (`deps.load_manifests`), then **pre-checks** the
    name before touching the runner: an unknown name is a plain `ToolError`
    not-found (→ structured envelope via MCP dispatch), so the runner's bare
    `KeyError` (which `dispatch` would pass through unwrapped) never escapes
    and no pipeline runs. A known name builds a runner via `deps.build_runner`
    and triggers it with `dispatch_notify=False`, so the Report is produced +
    persisted but no channel is sent. The output carries the ledger `run_id`,
    `status`, and the report-store `report_id` (None for no-Report statuses);
    callers must feed `report_id` (not `run_id`) to `show_report`.

    This handler introduces **no** new backend / cache behavior — it is a
    thin reuse of `runner.trigger` → `run_diagnosis_pipeline`. Prompt-cache
    effectiveness (the pipeline's two cache layers), Anthropic 429
    retry-after honoring, and the `failed_api_unavailable` (backend outage,
    no Report) / `partial` (token-degraded) status mapping are all the
    pipeline's / backend's existing semantics, exercised by their own tests
    (`tests/agent/test_cache_strategy.py`, the backend retry tests, and
    `tests/scheduler/test_runner.py`). The runner never constructs
    `budget_exhausted`; a backend failure surfaces here as a status string,
    not a raised exception.
    """
    manifests = deps.load_manifests()
    if args.name not in {m.name for m in manifests}:
        raise ToolError(
            f"schedule_not_found: name={args.name!r} is not a loaded schedule "
            "(use list_schedules to see configured schedule names)"
        )

    runner = deps.build_runner(ctx, manifests)
    run = await runner.trigger(args.name, dispatch_notify=False)
    return RunScheduleNowOutput(
        run_id=run.run_id,
        status=str(run.status),
        report_id=run.report_id,
    )


# ---------------------------------------------------------------------------
# Spec builders — each closure-binds `deps` into its handler, then wraps it
# with `@tool`. Mirrors `build_run_inspector_spec` in default_tools.py.
# ---------------------------------------------------------------------------


def build_list_schedules_spec(deps: ManagementToolDeps) -> ToolSpec:
    async def _handler(args: ListSchedulesInput, ctx: ToolContext) -> ListSchedulesOutput:
        return await _list_schedules_handler(args, ctx, deps=deps)

    return tool(
        name="list_schedules",
        version="1.0.0",
        input_schema=ListSchedulesInput,
        output_schema=ListSchedulesOutput,
        agent_description=(
            "List configured schedules with their trigger expression, next fire "
            "time, targets, intent, and per-channel notify routing. Use this to "
            "see what periodic inspections are configured before triggering one."
        ),
        mcp_description=(
            "List the configured schedules read from schedules/*.yaml. Each entry "
            "carries name / schedule expression / next_fire_time / targets / "
            "intent / notify bindings (channel + only_if routing). Notify "
            "only_if is manifest text, not a secret. No credentials are returned."
        ),
        cli_help=None,
        surfaces={"agent", "mcp"},
        side_effects="none",
        sensitive_output=True,
        timeout=10.0,
    )(cast(_BroadHandler, _handler))


def build_get_schedule_status_spec(deps: ManagementToolDeps) -> ToolSpec:
    async def _handler(args: GetScheduleStatusInput, ctx: ToolContext) -> GetScheduleStatusOutput:
        return await _get_schedule_status_handler(args, ctx, deps=deps)

    return tool(
        name="get_schedule_status",
        version="1.0.0",
        input_schema=GetScheduleStatusInput,
        output_schema=GetScheduleStatusOutput,
        agent_description=(
            "Show the most recent scheduler runs (optionally for one schedule). "
            "Each run carries the ledger run_id, status, targets, inspectors, and "
            "a report_id. To open a run's report, pass its report_id (NOT the "
            "ledger run_id) to show_report."
        ),
        mcp_description=(
            "Return the most recent scheduler run ledger entries (optionally "
            "filtered by schedule name; limit defaults to 10, capped at 100). "
            "Each entry has a ledger run_id, status, targets, inspectors, and a "
            "report_id which may be null for no-report runs. Use report_id (not "
            "run_id) with show_report. Notify result errors are redacted."
        ),
        cli_help=None,
        surfaces={"agent", "mcp"},
        side_effects="none",
        sensitive_output=True,
        timeout=10.0,
    )(cast(_BroadHandler, _handler))


def build_list_channels_spec(deps: ManagementToolDeps) -> ToolSpec:
    async def _handler(args: ListChannelsInput, ctx: ToolContext) -> ListChannelsOutput:
        return await _list_channels_handler(args, ctx, deps=deps)

    return tool(
        name="list_channels",
        version="1.0.0",
        input_schema=ListChannelsInput,
        output_schema=ListChannelsOutput,
        agent_description=(
            "List configured notification channels by name and type only. "
            "Credentials, webhook URLs, and signing secrets are never returned."
        ),
        mcp_description=(
            "List the configured notification channels from notifiers.yaml, "
            "exposing only each channel's instance name and type. Bot tokens, "
            "webhook URLs, signing secrets, and ${ENV_VAR} placeholders are never "
            "returned — the output is a strict name/type whitelist."
        ),
        cli_help=None,
        surfaces={"agent", "mcp"},
        side_effects="none",
        sensitive_output=True,
        timeout=10.0,
    )(cast(_BroadHandler, _handler))


def build_list_reports_spec(deps: ManagementToolDeps) -> ToolSpec:
    async def _handler(args: ListReportsInput, ctx: ToolContext) -> ListReportsOutput:
        return await _list_reports_handler(args, ctx, deps=deps)

    return tool(
        name="list_reports",
        version="1.0.0",
        input_schema=ListReportsInput,
        output_schema=ListReportsOutput,
        agent_description=(
            "List stored reports for one target (required), newest first. Each "
            "row has a report_id you can pass to show_report or diff_reports. "
            "Enumerate targets with list_targets first."
        ),
        mcp_description=(
            "List the stored report index for one target (the target argument is "
            "required — there is no all-targets listing; enumerate targets via "
            "list_targets first). Each row carries a report_id (the key for "
            "show_report / diff_reports), timestamp, status, and finding_count."
        ),
        cli_help=None,
        surfaces={"agent", "mcp"},
        side_effects="none",
        sensitive_output=True,
        timeout=10.0,
    )(cast(_BroadHandler, _handler))


def build_show_report_spec(deps: ManagementToolDeps) -> ToolSpec:
    async def _handler(args: ShowReportInput, ctx: ToolContext) -> ShowReportOutput:
        return await _show_report_handler(args, ctx, deps=deps)

    return tool(
        name="show_report",
        version="1.0.0",
        input_schema=ShowReportInput,
        output_schema=ShowReportOutput,
        agent_description=(
            "Retrieve one stored report (findings + root-cause hypotheses) by its "
            "report_id. Get a valid report_id from list_reports, "
            "get_schedule_status, or run_schedule_now."
        ),
        mcp_description=(
            "Retrieve one stored report by its report_id (the report-store key "
            "emitted by list_reports / get_schedule_status / run_schedule_now — "
            "NOT a scheduler ledger run_id). Returns the full report including "
            "findings and hypotheses; an unknown report_id returns a structured "
            "not-found error."
        ),
        cli_help=None,
        surfaces={"agent", "mcp"},
        side_effects="none",
        sensitive_output=True,
        timeout=10.0,
    )(cast(_BroadHandler, _handler))


def build_diff_reports_spec(deps: ManagementToolDeps) -> ToolSpec:
    async def _handler(args: DiffReportsInput, ctx: ToolContext) -> DiffReportsOutput:
        return await _diff_reports_handler(args, ctx, deps=deps)

    return tool(
        name="diff_reports",
        version="1.0.0",
        input_schema=DiffReportsInput,
        output_schema=DiffReportsOutput,
        agent_description=(
            "Compute a regression diff between two stored reports of the same "
            "target: report_id_a is the baseline, report_id_b the current. "
            "Returns added / resolved / severity-changed findings and hypothesis "
            "changes."
        ),
        mcp_description=(
            "Compute a regression diff between two stored reports keyed by "
            "report_id (report_id_a = baseline, report_id_b = current). Both must "
            "be the same target — a cross-target pair returns a structured error, "
            "and an unknown report_id returns a structured not-found. Output "
            "lists added / resolved / changed-severity findings plus hypothesis "
            "changes."
        ),
        cli_help=None,
        surfaces={"agent", "mcp"},
        side_effects="read",
        sensitive_output=True,
        timeout=15.0,
    )(cast(_BroadHandler, _handler))


def build_run_schedule_now_spec(deps: ManagementToolDeps) -> ToolSpec:
    async def _handler(args: RunScheduleNowInput, ctx: ToolContext) -> RunScheduleNowOutput:
        return await _run_schedule_now_handler(args, ctx, deps=deps)

    return tool(
        name="run_schedule_now",
        version="1.0.0",
        input_schema=RunScheduleNowInput,
        output_schema=RunScheduleNowOutput,
        agent_description=(
            "Run a configured schedule's diagnosis pipeline immediately without "
            "sending any notification. Persists a report and returns its "
            "report_id (feed that, NOT the ledger run_id, to show_report). This "
            "invokes the LLM pipeline and is not free; only existing schedules "
            "can be triggered."
        ),
        mcp_description=(
            "Trigger one configured schedule's bound diagnosis pipeline right "
            "now, persisting a report but suppressing all notify dispatch (no "
            "channel is sent). Returns the ledger run_id, the run status (ok / "
            "partial / failed_api_unavailable / failed), and the report-store "
            "report_id — pass report_id (NOT run_id) to show_report to read the "
            "result; report_id is null when no report was produced. This runs "
            "the LLM diagnosis pipeline (it consumes tokens and is not free) and "
            "can only trigger an already-configured schedule; an unknown name "
            "returns a structured not-found error. Confirm the name via "
            "list_schedules first."
        ),
        cli_help=None,
        surfaces={"agent", "mcp"},
        side_effects="read",
        sensitive_output=True,
        timeout=120.0,
    )(cast(_BroadHandler, _handler))


# ---------------------------------------------------------------------------
# Explicit assembly
# ---------------------------------------------------------------------------


def register_mcp_management_tools(registry: ToolRegistry, *, deps: ManagementToolDeps) -> None:
    """Register the MCP management ToolSpecs into `registry`.

    Closure-injects `deps` into every handler (design D-1). Non-idempotent:
    a duplicate call on the same registry raises `ToolError` (duplicate
    name). Registers all seven read-only management tools (the six query
    tools plus `run_schedule_now`).
    """
    registry.register(build_list_schedules_spec(deps))
    registry.register(build_get_schedule_status_spec(deps))
    registry.register(build_list_channels_spec(deps))
    registry.register(build_list_reports_spec(deps))
    registry.register(build_show_report_spec(deps))
    registry.register(build_diff_reports_spec(deps))
    registry.register(build_run_schedule_now_spec(deps))
