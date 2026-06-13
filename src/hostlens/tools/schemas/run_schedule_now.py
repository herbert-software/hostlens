"""Pydantic schemas for the `run_schedule_now` ToolSpec.

`run_schedule_now(name)` reuses the scheduler runner's trigger path with
notify dispatch suppressed (`dispatch_notify=False`): it runs the schedule's
bound diagnosis pipeline, persists a `Report`, and records a `Run`, but sends
to no channel.

The **two-ID contract** (design D-7.3) is load-bearing in the output:

- `run_id` is the **scheduler ledger** id (a fresh UUID per fire) — NOT a
  valid key for `show_report`.
- `report_id` is the **report-store** key (= `ReportStore.save` return =
  `Report.meta.run_id`) — the key a remote LLM must feed `show_report` /
  `diff_reports`. It is `None` for a no-Report status (`failed_*`).

`status` is the `RunStatus` string (`ok` / `partial` / `failed_api_unavailable`
/ `failed`); the runner never constructs `budget_exhausted`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "RunScheduleNowInput",
    "RunScheduleNowOutput",
]


class RunScheduleNowInput(BaseModel):
    """Input schema for `run_schedule_now` — one schedule name."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)


class RunScheduleNowOutput(BaseModel):
    """Output schema for `run_schedule_now`.

    Carries both IDs (`run_id` ledger / `report_id` report-store key) plus
    the terminal `status`. Feed `report_id` (NOT `run_id`) to `show_report`;
    `report_id` is `None` when no Report was produced.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: str
    report_id: str | None
