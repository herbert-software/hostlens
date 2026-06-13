"""Pydantic schemas for the `diff_reports` ToolSpec.

`diff_reports(report_id_a, report_id_b)` computes a regression diff between
two persisted reports, both keyed by their **report-store key**
(`report_id`, NOT scheduler ledger `run_id`). The handler retrieves both
via `ReportStore.get_run` and calls `reporting.diff.compute_diff(a, b)`
directly — it does **not** import `cli/reports.py`'s `_compute_diff_or_exit`
(that raises `typer.Exit`, unusable inside the MCP process). A cross-target
`ValueError` from `compute_diff` is self-caught and surfaced as a structured
error envelope; a missing report is a plain `ToolError` not-found.

`report_id_a` is the baseline, `report_id_b` the current — matching
`compute_diff(baseline, current)`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from hostlens.reporting.diff import RegressionDiff

__all__ = [
    "DiffReportsInput",
    "DiffReportsOutput",
]


class DiffReportsInput(BaseModel):
    """Input schema for `diff_reports` — two report-store keys.

    `report_id_a` is the baseline; `report_id_b` is the current report.
    """

    model_config = ConfigDict(extra="forbid")

    report_id_a: str = Field(min_length=1)
    report_id_b: str = Field(min_length=1)


class DiffReportsOutput(BaseModel):
    """Output schema for `diff_reports` — the computed regression diff."""

    model_config = ConfigDict(extra="forbid")

    diff: RegressionDiff
