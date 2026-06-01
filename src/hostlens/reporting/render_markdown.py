"""Markdown renderer for `Report`.

`render(report)` returns a GitHub-Flavored Markdown string with a fixed
structure (title, meta table, Summary, Findings, Inspector Results).
The renderer is pure: it does not perform IO, does not expand
environment variables in `Evidence.command`, and does not mutate the
input `Report`.

Redaction is applied at the rendering boundary via
`redact_report_for_render(report)`; control-character escaping is
applied to user-supplied string fields (`Evidence.command` / `stdout` /
`stderr` / `excerpt`, `InspectorResult.error`) after redaction.
"""

from __future__ import annotations

import json
from datetime import datetime
from io import StringIO
from typing import TYPE_CHECKING

from hostlens.reporting._redact import redact_report_for_render
from hostlens.reporting.models import (
    Evidence,
    Finding,
    Report,
    RootCauseHypothesis,
    Severity,
)

if TYPE_CHECKING:
    from hostlens.inspectors.result import InspectorResult

__all__ = ["render"]


_SEVERITY_ORDER: tuple[Severity, ...] = ("critical", "warning", "info")
"""Display order for Summary and Findings sections (highest first)."""

_INTENT_NONE_DASH = "—"
"""U+2014 EM DASH — exactly one Unicode codepoint, per spec."""


def _escape_control_chars(s: str) -> str:
    """Escape C0 control characters (`\\x00-\\x1f`) and DEL (`\\x7f`) to
    literal `\\xXX` sequences.

    `\\n` (0x0A) and `\\t` (0x09) are preserved as-is so multi-line
    evidence still renders readably. Every other byte in
    `\\x00-\\x1f` and `\\x7f` is replaced with its `\\xXX` literal
    (e.g. ANSI `\\x1b` becomes the four characters ``\\x1b``).

    C1 control characters (`\\x80-\\x9f`) are deliberately **not**
    escaped in M1; extending the range would need its own OpenSpec
    proposal because legitimate UTF-8 multi-byte sequences share bytes
    in `\\x80-\\xbf`.
    """
    out: list[str] = []
    for ch in s:
        code = ord(ch)
        if ch in ("\n", "\t"):
            out.append(ch)
        elif code <= 0x1F or code == 0x7F:
            out.append(f"\\x{code:02x}")
        else:
            out.append(ch)
    return "".join(out)


def _fmt_dt(dt: datetime) -> str:
    return dt.isoformat()


def _fmt_duration(report: Report) -> str:
    delta = (report.finished_at - report.started_at).total_seconds()
    return f"{delta:.2f}"


def _render_meta_table(report: Report, buf: StringIO) -> None:
    intent_cell = report.intent if report.intent is not None else _INTENT_NONE_DASH
    rows: list[tuple[str, str]] = [
        ("report_id", str(report.report_id)),
        ("schema_version", report.schema_version),
        ("target_name", report.target_name),
        ("intent", intent_cell),
        ("started_at", _fmt_dt(report.started_at)),
        ("finished_at", _fmt_dt(report.finished_at)),
        ("duration_seconds", _fmt_duration(report)),
    ]
    buf.write("| Field | Value |\n")
    buf.write("|---|---|\n")
    for field, value in rows:
        buf.write(f"| {field} | {value} |\n")
    buf.write("\n")


def _render_summary(findings: list[Finding], buf: StringIO) -> None:
    buf.write("## Summary\n\n")
    if not findings:
        buf.write("_No findings._\n\n")
        return
    counts: dict[Severity, int] = {sev: 0 for sev in _SEVERITY_ORDER}
    for f in findings:
        counts[f.severity] += 1
    for sev in _SEVERITY_ORDER:
        buf.write(f"- {sev}: {counts[sev]}\n")
    buf.write("\n")


def _render_evidence_row(idx: int, ev: Evidence, buf: StringIO) -> None:
    """Render one Evidence as a 2-column sub-table inside the details
    block. Per-kind relevant fields only; control-chars escaped on
    text fields (command/stdout/stderr/excerpt)."""
    buf.write(f"**Evidence {idx} — kind: `{ev.kind}`**\n\n")
    buf.write("| Field | Value |\n")
    buf.write("|---|---|\n")

    def _row(name: str, raw: str) -> None:
        buf.write(f"| {name} | `{raw}` |\n")

    if ev.kind == "command_output":
        assert ev.command is not None and ev.stdout is not None
        _row("command", _escape_control_chars(ev.command))
        _row("stdout", _escape_control_chars(ev.stdout))
        if ev.stderr is not None:
            _row("stderr", _escape_control_chars(ev.stderr))
        if ev.exit_code is not None:
            _row("exit_code", str(ev.exit_code))
    elif ev.kind == "file_excerpt":
        assert ev.path is not None and ev.excerpt is not None
        _row("path", ev.path)
        _row("excerpt", _escape_control_chars(ev.excerpt))
    elif ev.kind == "metric":
        assert ev.metric_name is not None and ev.metric_value is not None
        _row("metric_name", ev.metric_name)
        _row("metric_value", str(ev.metric_value))
    elif ev.kind == "structured":
        assert ev.data is not None
        _row("data", json.dumps(ev.data, sort_keys=True))

    if ev.truncated:
        _row("truncated", "true")
    buf.write("\n")


def _render_findings(findings: list[Finding], buf: StringIO) -> None:
    buf.write("## Findings\n\n")
    if not findings:
        buf.write("_No findings._\n\n")
        return
    grouped: dict[Severity, list[Finding]] = {sev: [] for sev in _SEVERITY_ORDER}
    for f in findings:
        grouped[f.severity].append(f)
    for sev in _SEVERITY_ORDER:
        for f in grouped[sev]:
            buf.write(f"### [{sev.upper()}] {f.message}\n\n")
            if f.tags:
                buf.write(f"_tags: {', '.join(f.tags)}_\n\n")
            if f.evidence:
                n = len(f.evidence)
                buf.write(f"<details><summary>Evidence ({n} items)</summary>\n\n")
                for idx, ev in enumerate(f.evidence, start=1):
                    _render_evidence_row(idx, ev, buf)
                buf.write("</details>\n\n")


def _render_hypotheses(hypotheses: list[RootCauseHypothesis], buf: StringIO) -> None:
    """Render the root-cause section. Empty (M3: always) → placeholder."""
    buf.write("## 根因假设\n\n")
    if not hypotheses:
        buf.write("_暂无根因假设_\n\n")
        return
    for h in hypotheses:
        buf.write(f"### {h.description}\n\n")
        buf.write(f"- **Confidence:** {h.confidence}\n")
        if h.supporting_findings:
            buf.write(f"- **Supporting findings:** {', '.join(h.supporting_findings)}\n")
        if h.suggested_actions:
            buf.write("- **Suggested actions:**\n")
            for action in h.suggested_actions:
                buf.write(f"  - {action}\n")
        buf.write("\n")


def _render_inspector_results(
    results: list[InspectorResult],
    buf: StringIO,
) -> None:
    buf.write("## Inspector Results\n\n")
    for ir in results:
        buf.write(f"### {ir.name}\n\n")
        buf.write(f"- **Version:** {ir.version}\n")
        buf.write(f"- **Target:** {ir.target_name}\n")
        buf.write(f"- **Duration (s):** {ir.duration_seconds:.2f}\n")
        buf.write(f"- **Status:** {ir.status}\n")
        if ir.error is not None:
            buf.write(f"- **Error:** {_escape_control_chars(ir.error)}\n")
        if ir.missing:
            buf.write(f"- **Missing:** {', '.join(ir.missing)}\n")
        buf.write("\n")
        buf.write("```json\n")
        buf.write(json.dumps(ir.output, indent=2, sort_keys=True))
        buf.write("\n```\n\n")


def render(report: Report) -> str:
    """Render `report` as a GitHub-Flavored Markdown string.

    Redaction is applied before any field reaches the output buffer;
    the source `report` is not mutated.
    """
    redacted = redact_report_for_render(report)
    buf = StringIO()
    buf.write("# Hostlens Inspection Report\n\n")
    _render_meta_table(redacted, buf)
    _render_summary(redacted.findings, buf)
    _render_findings(redacted.findings, buf)
    _render_hypotheses(redacted.hypotheses, buf)
    _render_inspector_results(redacted.inspector_results, buf)
    return buf.getvalue()
