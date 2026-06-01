"""Pydantic schemas for the `run_inspector` ToolSpec.

`FindingSummary` is a type alias of `hostlens.reporting.models.Finding`
(the unified report-data-model SOT introduced by the
`add-report-data-model` proposal). Keeping the alias name preserves the
existing `from hostlens.tools.schemas.run_inspector import FindingSummary`
import path while letting the underlying schema track the canonical
`Finding` definition exactly.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from hostlens.reporting.models import Finding

__all__ = [
    "FindingSummary",
    "RunInspectorInput",
    "RunInspectorOutput",
]


class RunInspectorInput(BaseModel):
    """Input schema for `run_inspector`."""

    model_config = ConfigDict(extra="forbid")

    target_name: str
    inspector_name: str
    parameters: dict[str, str] = Field(default_factory=dict)


# Type alias — `FindingSummary` is exactly the canonical `Finding` model
# from `hostlens.reporting.models`. JSON schema for the ToolSpec surface
# is produced by `Finding.model_json_schema()` at adapter projection
# time, so this alias automatically tracks any field-set evolution of
# `Finding` without requiring schema duplication.
FindingSummary = Finding


class RunInspectorOutput(BaseModel):
    """Output schema for `run_inspector`."""

    model_config = ConfigDict(extra="forbid")

    target_name: str
    inspector_name: str
    findings: list[FindingSummary]

    @field_serializer("findings")
    def _serialize_findings(self, findings: list[Finding]) -> list[dict[str, Any]]:
        # The M3 `add-report-persistence-and-diff` proposal added
        # `id`/`inspector_name`/`inspector_version` to `Finding` (add-only). They
        # belong to the persistence/diff layer, not the Agent's view of the world:
        # the proposal's §对外契约 promises the agent-facing tool projection takes
        # only the necessary fields and does NOT widen the Agent-visible surface.
        # Excluding them here keeps the LLM-facing tool_result byte-for-byte
        # identical to the M2 shape (severity/message/evidence/tags only), so the
        # request-key hash over `messages` is unchanged and all existing
        # incident/demo/planner replay cassettes still hit without re-recording.
        return [
            finding.model_dump(exclude={"id", "inspector_name", "inspector_version"})
            for finding in findings
        ]
