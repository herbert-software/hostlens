"""Pydantic schemas for the `show_report` ToolSpec.

`show_report(report_id)` retrieves one persisted `Report` (findings +
hypotheses) by its **report-store key** — the same `report_id` emitted by
`list_reports` / `get_schedule_status` / `run_schedule_now`, NOT a
scheduler ledger `run_id`.

The output wraps the full `Report` model so the surface shape stays stable
even if `Report` grows fields. A not-found `report_id` is expressed by the
handler as a plain `ToolError` (→ structured not-found envelope via the MCP
dispatch general-except), never a None field on this schema.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from hostlens.reporting.models import Report

__all__ = [
    "ShowReportInput",
    "ShowReportOutput",
]


class ShowReportInput(BaseModel):
    """Input schema for `show_report` — one report-store key."""

    model_config = ConfigDict(extra="forbid")

    report_id: str = Field(min_length=1)


class ShowReportOutput(BaseModel):
    """Output schema for `show_report` — the full retrieved `Report`."""

    model_config = ConfigDict(extra="forbid")

    report: Report
