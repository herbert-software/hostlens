"""Pydantic schemas for the `get_schedule_status` ToolSpec.

`RunSummary` projects one scheduler ledger `Run` into the read-only
surface shape. The two-ID contract (design D-7.3) is load-bearing here:

- `run_id` is the **scheduler ledger** id (a fresh UUID per fire) — NOT a
  valid key for `show_report`.
- `report_id` is the **report-store** key (= `ReportStore.save` return =
  `Report.meta.run_id`) — the key a remote LLM must feed `show_report` /
  `diff_reports`.

A no-Report Run (`failed_*` / `missed` / `skipped_due_to_running` /
`daemon_stopped` / `budget_exhausted`) has `report_id` and `report_hash`
== `None`, so both fields are nullable.

`notify_results` is redacted via `redact_secret_text` in the handler
before reaching the surface (its `error` strings can embed channel
secrets); this schema carries the already-redacted projection.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "GetScheduleStatusInput",
    "GetScheduleStatusOutput",
    "NotifyResultSummary",
    "RunSummary",
]

# Default / max for the `limit` parameter. `RunStore.list_recent` defaults to
# 20 with no upper clamp, so the default-10 / max-100 semantics are enforced
# in the handler (design D-7.5), not the store.
DEFAULT_STATUS_LIMIT: int = 10
MAX_STATUS_LIMIT: int = 100


class NotifyResultSummary(BaseModel):
    """Per-channel notify outcome, with `error` already redacted.

    Projected from `notifiers.base.NotifyResult`; `error` has been passed
    through `redact_secret_text` so no channel secret reaches the surface.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: str
    status: str
    error: str | None = None
    attempts: int = 0


class RunSummary(BaseModel):
    """Read-only projection of one scheduler ledger `Run`.

    `report_id` / `report_hash` are nullable (None for no-Report statuses).
    Feed `report_id` (NOT `run_id`) to `show_report`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    schedule_name: str
    triggered_at: datetime
    status: str
    targets: list[str]
    inspectors: list[str]
    report_id: str | None
    report_hash: str | None
    error: str | None
    notify_results: list[NotifyResultSummary]


class GetScheduleStatusInput(BaseModel):
    """Input schema for `get_schedule_status`.

    `name` optionally filters to one schedule's runs. `limit` is clamped to
    `[1, MAX_STATUS_LIMIT]` in the handler; the schema only enforces the
    `>= 1` floor and default.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    limit: int = Field(default=DEFAULT_STATUS_LIMIT, ge=1)


class GetScheduleStatusOutput(BaseModel):
    """Output schema for `get_schedule_status`."""

    model_config = ConfigDict(extra="forbid")

    runs: list[RunSummary]
