"""Public reporting API.

This package exposes the `Severity` / `Evidence` / `Finding` / `Report`
data models and the markdown / json renderers. Imports here are kept
side-effect free: no IO, no registry assembly, no `model_rebuild` calls.

`Report` carries a forward-reference to
`hostlens.inspectors.result.InspectorResult` that is resolved at the
bottom of `inspectors/result.py` via `Report.model_rebuild(...)` — this
package deliberately does not trigger that rebuild so `import
hostlens.reporting` stays cheap and deterministic. Callers that
construct a `Report` (CLI, Agent loop, tests) must first import
`hostlens.inspectors.result` (already on the natural entrypoint paths).
"""

from __future__ import annotations

from hostlens.reporting.models import Evidence, Finding, Report, Severity
from hostlens.reporting.render_json import render as render_json
from hostlens.reporting.render_markdown import render as render_markdown
from hostlens.reporting.store import ReportStore, RunIndexRow, SaveResult

__all__ = [
    "Evidence",
    "Finding",
    "Report",
    "ReportStore",
    "RunIndexRow",
    "SaveResult",
    "Severity",
    "render_json",
    "render_markdown",
]
