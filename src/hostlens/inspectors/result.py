"""InspectorResult / Finding Pydantic models.

Both models are frozen and reject unknown fields. `Finding`'s field set
(`severity` / `message` / `evidence: dict[str, str]`) is deliberately kept
in sync with `hostlens.tools.schemas.run_inspector.FindingSummary` so the
M2 `run_inspector` ToolSpec handler can project an `InspectorResult.findings`
list straight to `RunInspectorOutput.findings` without renaming fields.

`InspectorResult.status` is the M1-final five-value closed set; the
`model_validator` enforces cross-field invariants (`ok` ⇒ no error / no
missing; `requires_unmet` ⇒ non-empty missing; others ⇒ no missing).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "Finding",
    "InspectorResult",
    "InspectorStatus",
]


InspectorStatus = Literal[
    "ok",
    "timeout",
    "target_unreachable",
    "requires_unmet",
    "exception",
]


class Finding(BaseModel):
    """Minimal M1 finding model — three fields kept in lockstep with
    `hostlens.tools.schemas.run_inspector.FindingSummary` so the
    `run_inspector` handler projects 1:1.

    M3 (`add-report-data-model`) will extend this with `id`,
    `inspector_run_id`, `seen_at`, etc. as add-only fields; the three
    fields here will stay name- and type-stable.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    severity: Literal["info", "warning", "critical"]
    message: str
    evidence: dict[str, str] = Field(default_factory=dict)


class InspectorResult(BaseModel):
    """Result of one Inspector run on one target.

    `status` is the closed five-value enum the M2 Planner Agent expects.
    Cross-field rules:
      - `ok`              ⇒ `error is None` AND `missing == []`
      - `requires_unmet`  ⇒ `missing` non-empty
      - `timeout` / `target_unreachable` / `exception` ⇒ `missing == []`
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    version: str
    status: InspectorStatus
    target_name: str
    duration_seconds: float
    output: dict[str, Any] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)
    error: str | None = None
    missing: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_status_invariants(self) -> InspectorResult:
        status = self.status

        if status == "ok":
            if self.error is not None:
                raise ValueError(
                    f"ok_status_with_error: status='ok' requires error is None, "
                    f"got error={self.error!r}"
                )
            if self.missing:
                raise ValueError(
                    f"ok_status_with_missing: status='ok' requires missing == [], "
                    f"got missing={self.missing!r}"
                )
        elif status == "requires_unmet":
            if not self.missing:
                raise ValueError(
                    "requires_unmet_status_without_missing: status='requires_unmet' "
                    "requires non-empty missing list"
                )
        elif status in ("timeout", "target_unreachable", "exception"):
            if self.missing:
                raise ValueError(
                    f"{status}_status_with_missing: status={status!r} requires "
                    f"missing == [], got missing={self.missing!r}"
                )

        return self
