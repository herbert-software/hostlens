"""Pydantic schemas for the `list_reports` ToolSpec.

`ReportIndexRow` projects one `reporting.store.RunIndexRow` into the
surface shape. The **ID naming contract** (design D-7.3) is enforced here:
`RunIndexRow.run_id` carries a value that is actually the **report-store
key** (= `Report.meta.run_id` = the valid `show_report` key, NOT a
scheduler ledger `run_id`). To avoid reviving the cross-tool ID trap, this
schema exposes that id under the name **`report_id`** — it never emits a
`run_id` key.

`target` is **required** (design D-7.2): `ReportStore.list_runs(target_id)`
has no all-targets enumeration, so a remote LLM enumerates targets via
`list_targets` first, then queries each. `limit` defaults to 20 with **no**
upper clamp (single-target listings are small — a deliberate divergence
from `get_schedule_status`).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ListReportsInput",
    "ListReportsOutput",
    "ReportIndexRow",
]

DEFAULT_REPORTS_LIMIT: int = 20


class ReportIndexRow(BaseModel):
    """Read-only projection of one `ReportStore.list_runs` row.

    `report_id` is the report-store key (the valid `show_report` /
    `diff_reports` key) — exposed under this name, never `run_id`, to keep
    the cross-tool ID contract unambiguous.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    report_id: str
    timestamp: datetime
    status: str
    finding_count: int


class ListReportsInput(BaseModel):
    """Input schema for `list_reports` — `target` is required."""

    model_config = ConfigDict(extra="forbid")

    target: str = Field(min_length=1)
    limit: int = Field(default=DEFAULT_REPORTS_LIMIT, ge=1)


class ListReportsOutput(BaseModel):
    """Output schema for `list_reports`."""

    model_config = ConfigDict(extra="forbid")

    reports: list[ReportIndexRow]
