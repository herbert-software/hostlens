"""Tests for the `## 根因假设` block of `render_markdown.render`.

Covers spec §需求:`render_markdown.render` 必须输出固定 GFM 结构 — the M3
add-only root-cause section:

- §场景:无假设时根因章节显示占位 — empty `hypotheses` → `_暂无根因假设_`.
- §场景:根因章节位于 Findings 之后 Inspector Results 之前 — the section
  sits between `## Findings` and `## Inspector Results`.
- Non-empty rendering (description / confidence / supporting_findings /
  suggested_actions), the path future Diagnostician work exercises.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from hostlens.inspectors.result import InspectorResult
from hostlens.reporting.models import Finding, Report, RootCauseHypothesis
from hostlens.reporting.render_markdown import render


def _make_report(
    findings: list[Finding],
    *,
    hypotheses: list[RootCauseHypothesis] | None = None,
) -> Report:
    ir = InspectorResult(
        name="x",
        version="1.0.0",
        status="ok",
        target_name="t",
        duration_seconds=0.05,
        output={},
        findings=findings,
        error=None,
        missing=[],
    )
    t = datetime(2026, 5, 26, 12, 0, 0)
    return Report(
        report_id=UUID("12345678-1234-5678-1234-567812345678"),
        schema_version="1.0",
        intent=None,
        target_name="t",
        inspector_results=[ir],
        findings=findings,
        started_at=t,
        finished_at=t,
        metadata={},
        hypotheses=hypotheses or [],
    )


def test_empty_hypotheses_renders_placeholder() -> None:
    out = render(_make_report([Finding(severity="info", message="x")]))
    assert "## 根因假设" in out
    assert "_暂无根因假设_" in out


def test_hypotheses_section_between_findings_and_inspector_results() -> None:
    out = render(_make_report([Finding(severity="info", message="x")]))
    idx_findings = out.index("## Findings")
    idx_hyp = out.index("## 根因假设")
    idx_inspector = out.index("## Inspector Results")
    assert idx_findings < idx_hyp < idx_inspector


def test_non_empty_hypotheses_rendered() -> None:
    hyp = RootCauseHypothesis(
        description="disk pressure cascading to OOM",
        confidence="high",
        supporting_findings=["abc123", "def456"],
        suggested_actions=["free disk space", "raise memory limit"],
    )
    out = render(_make_report([], hypotheses=[hyp]))
    assert "_暂无根因假设_" not in out
    assert "### disk pressure cascading to OOM" in out
    assert "**Confidence:** high" in out
    assert "abc123, def456" in out
    assert "- free disk space" in out
    assert "- raise memory limit" in out
