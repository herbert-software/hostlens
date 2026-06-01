"""Tests for the regression diff engine (`hostlens.reporting.diff`).

Covers spec report-regression-diff §需求 逐条对照:

- `RegressionDiff` extra=forbid + `diff_skipped_reason` 闭集
- `compute_diff` 规则 0-7:
  - 同报告 diff 自身无回归
  - current 新增 finding 进 added / baseline 独有进 resolved
  - severity 变化进 changed_severity 而非 added+resolved
  - 含 None id 的 finding 跳过 (missing_finding_ids)
  - meta=None 的 legacy 报告跳过且不 None-deref
  - current.meta 缺失但 baseline.meta 在仍投影 baseline_meta
  - 基线非 ok 跳过 / force 覆盖
  - inspector 版本升级时其 finding 排除
  - 跨 target raise ValueError
  - schema 版本不一致跳过 (schema_changed)
"""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

import hostlens.inspectors.result  # noqa: F401  # triggers Report.model_rebuild
from hostlens.inspectors.result import InspectorResult
from hostlens.reporting.diff import (
    FindingFingerprint,
    RegressionDiff,
    SeverityChange,
    compute_diff,
)
from hostlens.reporting.models import (
    Finding,
    Report,
    ReportStatus,
    Severity,
)


def _t0() -> datetime:
    return datetime(2026, 5, 26, 12, 0, 0)


def _t1() -> datetime:
    return datetime(2026, 5, 26, 12, 0, 2)


def _ir(
    name: str,
    *,
    version: str = "1.0",
    status: str = "ok",
    findings: list[Finding] | None = None,
    duration_seconds: float = 0.1,
) -> InspectorResult:
    kwargs: dict[str, object] = {
        "name": name,
        "version": version,
        "status": status,
        "target_name": "t",
        "duration_seconds": duration_seconds,
        "findings": findings if findings is not None else [],
    }
    if status in ("timeout", "target_unreachable", "exception"):
        kwargs["error"] = f"{status} happened"
    if status == "requires_unmet":
        kwargs["missing"] = ["needs_root"]
    return InspectorResult(**kwargs)  # type: ignore[arg-type]


def _f(message: str, severity: Severity = "info") -> Finding:
    return Finding(severity=severity, message=message)


def _report(
    inspector_results: list[InspectorResult],
    *,
    target_id: str | None = None,
    status: ReportStatus | None = None,
) -> Report:
    return Report.from_inspector_results(
        "t",
        inspector_results,
        started_at=_t0(),
        finished_at=_t1(),
        target_id=target_id,
        status=status,
    )


# --------------------------------------------------------------------------- #
# RegressionDiff model
# --------------------------------------------------------------------------- #


def test_regression_diff_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        RegressionDiff(baseline_meta=None, not_a_field="x")  # type: ignore[call-arg]


def test_diff_skipped_reason_is_closed_set() -> None:
    with pytest.raises(ValidationError):
        RegressionDiff(baseline_meta=None, diff_skipped_reason="whatever")  # type: ignore[arg-type]


def test_diff_skipped_reason_accepts_three_legal_values_and_none() -> None:
    for v in (None, "baseline_not_ok", "schema_changed", "missing_finding_ids"):
        d = RegressionDiff(baseline_meta=None, diff_skipped_reason=v)  # type: ignore[arg-type]
        assert d.diff_skipped_reason == v


def test_regression_diff_defaults() -> None:
    d = RegressionDiff(baseline_meta=None)
    assert d.added == []
    assert d.resolved == []
    assert d.changed_severity == []
    assert d.inspector_upgraded == []
    assert d.dst_boundary_crossed is False
    assert d.diff_skipped_reason is None


def test_finding_fingerprint_field_set() -> None:
    fp = FindingFingerprint(id="abc", inspector_name="insp.a", severity="warning", message="m")
    assert fp.id == "abc"
    assert fp.inspector_name == "insp.a"
    assert fp.severity == "warning"


def test_severity_change_field_set() -> None:
    sc = SeverityChange(id="abc", from_severity="warning", to_severity="critical", message="m")
    assert sc.from_severity == "warning"
    assert sc.to_severity == "critical"


# --------------------------------------------------------------------------- #
# compute_diff — no-regression / added / resolved
# --------------------------------------------------------------------------- #


def test_same_report_diffs_to_no_regression() -> None:
    r = _report([_ir("insp.a", findings=[_f("m1"), _f("m2")])])
    d = compute_diff(r, r)
    assert d.added == []
    assert d.resolved == []
    assert d.changed_severity == []
    assert d.diff_skipped_reason is None
    assert d.baseline_meta is not None


def test_current_new_finding_goes_to_added() -> None:
    baseline = _report([_ir("insp.a", findings=[_f("A")])])
    current = _report([_ir("insp.a", findings=[_f("A"), _f("B")])])
    d = compute_diff(baseline, current)
    assert [fp.message for fp in d.added] == ["B"]
    assert d.resolved == []
    assert d.changed_severity == []


def test_baseline_only_finding_goes_to_resolved() -> None:
    baseline = _report([_ir("insp.a", findings=[_f("A"), _f("B")])])
    current = _report([_ir("insp.a", findings=[_f("A")])])
    d = compute_diff(baseline, current)
    assert [fp.message for fp in d.resolved] == ["B"]
    assert d.added == []
    assert d.changed_severity == []


# --------------------------------------------------------------------------- #
# compute_diff — changed_severity
# --------------------------------------------------------------------------- #


def test_severity_change_goes_to_changed_severity_not_added_resolved() -> None:
    # Same (inspector, version, message) → same id; only severity differs.
    baseline = _report([_ir("insp.a", findings=[_f("disk 95%", severity="warning")])])
    current = _report([_ir("insp.a", findings=[_f("disk 95%", severity="critical")])])
    d = compute_diff(baseline, current)
    assert len(d.changed_severity) == 1
    sc = d.changed_severity[0]
    assert sc.from_severity == "warning"
    assert sc.to_severity == "critical"
    assert sc.message == "disk 95%"
    assert d.added == []
    assert d.resolved == []
    # the changed-severity finding's id must not surface in added/resolved
    changed_id = sc.id
    assert all(fp.id != changed_id for fp in d.added)
    assert all(fp.id != changed_id for fp in d.resolved)


# --------------------------------------------------------------------------- #
# compute_diff — None id / meta=None gates
# --------------------------------------------------------------------------- #


def test_none_id_finding_skips_diff() -> None:
    # A directly-constructed Report with a finding lacking id (not via factory).
    baseline = _report([_ir("insp.a", findings=[_f("A")])])
    # current carries a finding with id=None (legacy/direct construction).
    bare_finding = Finding(severity="info", message="A")  # id is None
    current = baseline.model_copy(update={"findings": [bare_finding]})
    d = compute_diff(baseline, current)
    assert d.diff_skipped_reason == "missing_finding_ids"
    assert d.added == []
    assert d.resolved == []
    assert d.changed_severity == []
    # baseline.meta is present → baseline_meta projected even on skip
    assert d.baseline_meta is not None


def test_meta_none_on_baseline_skips_without_none_deref() -> None:
    baseline = _report([_ir("insp.a", findings=[_f("A")])])
    current = _report([_ir("insp.a", findings=[_f("A")])])
    baseline_no_meta = baseline.model_copy(update={"meta": None})
    # Must not raise AttributeError on `.meta.target_id`.
    d = compute_diff(baseline_no_meta, current)
    assert d.diff_skipped_reason == "missing_finding_ids"
    assert d.added == []
    assert d.resolved == []
    assert d.changed_severity == []
    # baseline.meta is None → baseline_meta is None
    assert d.baseline_meta is None


def test_meta_none_on_current_skips_but_projects_baseline_meta() -> None:
    baseline = _report([_ir("insp.a", findings=[_f("A")])])
    current = _report([_ir("insp.a", findings=[_f("A")])])
    current_no_meta = current.model_copy(update={"meta": None})
    d = compute_diff(baseline, current_no_meta)
    assert d.diff_skipped_reason == "missing_finding_ids"
    assert d.added == []
    # current.meta is None but baseline.meta is present → still projected
    assert d.baseline_meta is not None
    assert baseline.meta is not None
    assert d.baseline_meta.run_id == baseline.meta.run_id


# --------------------------------------------------------------------------- #
# compute_diff — baseline status gate + force
# --------------------------------------------------------------------------- #


def test_baseline_not_ok_skips_diff() -> None:
    baseline = _report([_ir("insp.a", findings=[_f("A")])], status=ReportStatus.PARTIAL)
    current = _report([_ir("insp.a", findings=[_f("A"), _f("B")])])
    d = compute_diff(baseline, current, force=False)
    assert d.diff_skipped_reason == "baseline_not_ok"
    assert d.added == []
    assert d.resolved == []
    assert d.changed_severity == []
    assert d.baseline_meta is not None


def test_force_overrides_non_ok_baseline() -> None:
    baseline = _report([_ir("insp.a", findings=[_f("A")])], status=ReportStatus.PARTIAL)
    current = _report([_ir("insp.a", findings=[_f("A"), _f("B")])])
    d = compute_diff(baseline, current, force=True)
    assert d.diff_skipped_reason is None
    assert [fp.message for fp in d.added] == ["B"]


# --------------------------------------------------------------------------- #
# compute_diff — inspector version alignment
# --------------------------------------------------------------------------- #


def test_inspector_upgrade_excludes_its_findings() -> None:
    # baseline version 1.0 has finding "old"; current version 1.1 has "new".
    baseline = _report([_ir("linux.disk.usage", version="1.0", findings=[_f("old")])])
    current = _report([_ir("linux.disk.usage", version="1.1", findings=[_f("new")])])
    d = compute_diff(baseline, current)
    assert d.inspector_upgraded == ["linux.disk.usage"]
    # version-bumped inspector's findings must not surface as added/resolved
    assert d.added == []
    assert d.resolved == []
    assert d.changed_severity == []


def test_inspector_upgrade_isolated_other_inspector_still_diffs() -> None:
    # insp.a upgraded (excluded); insp.b stable (its added finding still shows).
    baseline = _report(
        [
            _ir("insp.a", version="1.0", findings=[_f("a-old")]),
            _ir("insp.b", version="2.0", findings=[_f("b1")]),
        ]
    )
    current = _report(
        [
            _ir("insp.a", version="1.1", findings=[_f("a-new")]),
            _ir("insp.b", version="2.0", findings=[_f("b1"), _f("b2")]),
        ]
    )
    d = compute_diff(baseline, current)
    assert d.inspector_upgraded == ["insp.a"]
    assert [fp.message for fp in d.added] == ["b2"]
    assert d.resolved == []


# --------------------------------------------------------------------------- #
# compute_diff — per-target isolation + schema alignment
# --------------------------------------------------------------------------- #


def test_cross_target_diff_raises() -> None:
    baseline = _report([_ir("insp.a", findings=[_f("A")])], target_id="host-a")
    current = _report([_ir("insp.a", findings=[_f("A")])], target_id="host-b")
    with pytest.raises(ValueError, match="across targets"):
        compute_diff(baseline, current)


def test_schema_mismatch_skips_diff() -> None:
    baseline = _report([_ir("insp.a", findings=[_f("A")])])
    current = _report([_ir("insp.a", findings=[_f("A"), _f("B")])])
    assert baseline.meta is not None
    # Force a report_schema_version mismatch on the baseline meta.
    bumped_meta = baseline.meta.model_copy(update={"report_schema_version": "9.9"})
    baseline_bumped = baseline.model_copy(update={"meta": bumped_meta})
    d = compute_diff(baseline_bumped, current)
    assert d.diff_skipped_reason == "schema_changed"
    assert d.added == []
    assert d.resolved == []
    assert d.changed_severity == []
    assert d.baseline_meta is not None


# --------------------------------------------------------------------------- #
# compute_diff — baseline_meta projection
# --------------------------------------------------------------------------- #


def test_baseline_meta_projects_inspector_versions() -> None:
    baseline = _report(
        [
            _ir("insp.a", version="1.0", findings=[_f("A")]),
            _ir("insp.b", version="2.5", findings=[_f("B")]),
        ]
    )
    current = _report(
        [
            _ir("insp.a", version="1.0", findings=[_f("A")]),
            _ir("insp.b", version="2.5", findings=[_f("B")]),
        ]
    )
    d = compute_diff(baseline, current)
    assert d.baseline_meta is not None
    assert d.baseline_meta.inspector_versions == {"insp.a": "1.0", "insp.b": "2.5"}
    assert baseline.meta is not None
    assert d.baseline_meta.run_id == baseline.meta.run_id


def test_dst_boundary_crossed_always_false() -> None:
    r = _report([_ir("insp.a", findings=[_f("A")])])
    d = compute_diff(r, r)
    assert d.dst_boundary_crossed is False
