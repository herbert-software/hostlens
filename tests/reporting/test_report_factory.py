"""Tests for `Report.from_inspector_results` factory.

Covers spec §需求:`Report.from_inspector_results` 工厂方法必须自动
flatten findings 与生成 report_id, plus the explicit metadata-default
assertion called out in tasks.md §2.6.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from hostlens.inspectors.result import InspectorResult
from hostlens.reporting.models import Finding, Report


def _make_ir(name: str, findings: list[Finding]) -> InspectorResult:
    return InspectorResult(
        name=name,
        version="1.0.0",
        status="ok",
        target_name="t",
        duration_seconds=0.1,
        output={},
        findings=findings,
        error=None,
        missing=[],
    )


def _t() -> datetime:
    return datetime(2026, 5, 26, 12, 0, 0)


def test_flatten_findings_across_inspector_results() -> None:
    f1 = Finding(severity="info", message="a")
    f2 = Finding(severity="warning", message="b")
    f3 = Finding(severity="critical", message="c")
    ir_a = _make_ir("a", [f1, f2])
    ir_b = _make_ir("b", [f3])
    r = Report.from_inspector_results(
        "t",
        [ir_a, ir_b],
        started_at=_t(),
        finished_at=_t(),
    )
    # Content and order are preserved (no dedup / sort).
    assert [f.message for f in r.findings] == ["a", "b", "c"]
    assert [f.severity for f in r.findings] == ["info", "warning", "critical"]
    # The factory fills each flattened finding's identity fields from its
    # source InspectorResult (inspector_name / inspector_version / id).
    assert [f.inspector_name for f in r.findings] == ["a", "a", "b"]
    assert {f.inspector_version for f in r.findings} == {"1.0.0"}
    assert all(f.id is not None for f in r.findings)


def test_flatten_does_not_deduplicate() -> None:
    f = Finding(severity="info", message="dup")
    ir_a = _make_ir("a", [f])
    ir_b = _make_ir("b", [f])
    r = Report.from_inspector_results(
        "t",
        [ir_a, ir_b],
        started_at=_t(),
        finished_at=_t(),
    )
    assert len(r.findings) == 2


def test_flatten_does_not_sort() -> None:
    fi = Finding(severity="info", message="i")
    fc = Finding(severity="critical", message="c")
    ir_a = _make_ir("a", [fi, fc])
    r = Report.from_inspector_results(
        "t",
        [ir_a],
        started_at=_t(),
        finished_at=_t(),
    )
    assert r.findings[0].severity == "info"
    assert r.findings[1].severity == "critical"


def test_report_id_is_unique_across_calls() -> None:
    ir = _make_ir("a", [])
    r1 = Report.from_inspector_results("t", [ir], started_at=_t(), finished_at=_t())
    r2 = Report.from_inspector_results("t", [ir], started_at=_t(), finished_at=_t())
    assert r1.report_id != r2.report_id


def test_schema_version_locked() -> None:
    ir = _make_ir("a", [])
    r = Report.from_inspector_results("t", [ir], started_at=_t(), finished_at=_t())
    assert r.schema_version == "1.1"


def test_empty_inspector_results_raises_value_error() -> None:
    with pytest.raises(ValueError, match="from_inspector_results requires at least one"):
        Report.from_inspector_results("t", [], started_at=_t(), finished_at=_t())


def test_metadata_defaults_to_empty_dict_when_none() -> None:
    ir = _make_ir("a", [])
    r = Report.from_inspector_results("t", [ir], started_at=_t(), finished_at=_t(), metadata=None)
    assert r.metadata == {}


def test_metadata_passes_through_when_set() -> None:
    ir = _make_ir("a", [])
    r = Report.from_inspector_results(
        "t",
        [ir],
        started_at=_t(),
        finished_at=_t(),
        metadata={"k": "v"},
    )
    assert r.metadata == {"k": "v"}


def test_intent_passes_through() -> None:
    ir = _make_ir("a", [])
    r = Report.from_inspector_results(
        "t", [ir], started_at=_t(), finished_at=_t(), intent="check db"
    )
    assert r.intent == "check db"
