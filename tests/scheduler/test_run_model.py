"""Unit tests for `RunStatus` (eight values) and `Run` invariants."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from hostlens.inspectors.result import InspectorResult
from hostlens.reporting.models import Evidence, Finding, Report
from hostlens.reporting.render_json import render
from hostlens.scheduler.store import Run, RunStatus, compute_report_hash

_T = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)


def _build_report() -> Report:
    ev = Evidence(kind="command_output", command="uptime", stdout="up 1 day")
    finding = Finding(severity="info", message="host healthy", evidence=[ev])
    ir = InspectorResult(
        name="linux.uptime",
        version="1.0.0",
        status="ok",
        target_name="local-host",
        duration_seconds=0.01,
        output={},
        findings=[finding],
        error=None,
        missing=[],
    )
    return Report.from_inspector_results(
        "local-host",
        [ir],
        started_at=_T,
        finished_at=_T,
    )


def _run(**overrides: object) -> Run:
    base: dict[str, object] = {
        "run_id": "run-1",
        "schedule_name": "nightly",
        "triggered_at": _T,
        "status": RunStatus.OK,
        "targets": ["local-host"],
    }
    base.update(overrides)
    return Run(**base)  # type: ignore[arg-type]


def test_run_status_has_exactly_eight_members() -> None:
    assert {s.value for s in RunStatus} == {
        "ok",
        "partial",
        "budget_exhausted",
        "missed",
        "skipped_due_to_running",
        "failed_api_unavailable",
        "failed",
        "daemon_stopped",
    }
    assert len(list(RunStatus)) == 8


# Invariant 1: report_id is None iff status not in {ok, partial}


def test_ok_without_report_id_rejected() -> None:
    with pytest.raises(ValidationError):
        _run(status=RunStatus.OK, report_id=None, started_at=_T, report_storage="db")


def test_missed_with_report_id_rejected() -> None:
    with pytest.raises(ValidationError):
        _run(status=RunStatus.MISSED, report_id="r1")


def test_partial_with_report_id_accepted() -> None:
    run = _run(
        status=RunStatus.PARTIAL,
        report_id="r1",
        started_at=_T,
        report_storage="db",
    )
    assert run.report_id == "r1"


# Invariant 2: started_at is None for missed / skipped / budget_exhausted


def test_skipped_with_started_at_rejected() -> None:
    with pytest.raises(ValidationError):
        _run(status=RunStatus.SKIPPED_DUE_TO_RUNNING, report_id=None, started_at=_T)


def test_missed_with_started_at_rejected() -> None:
    with pytest.raises(ValidationError):
        _run(status=RunStatus.MISSED, report_id=None, started_at=_T)


def test_budget_exhausted_with_started_at_rejected() -> None:
    with pytest.raises(ValidationError):
        _run(status=RunStatus.BUDGET_EXHAUSTED, report_id=None, started_at=_T)


def test_failed_with_started_at_accepted() -> None:
    run = _run(
        status=RunStatus.FAILED,
        report_id=None,
        started_at=_T,
        finished_at=_T,
        error="boom",
    )
    assert run.started_at == _T


def test_failed_api_unavailable_with_started_at_accepted() -> None:
    run = _run(status=RunStatus.FAILED_API_UNAVAILABLE, report_id=None, started_at=_T)
    assert run.started_at == _T


# Invariant 3: report_hash only allowed when status in {ok, partial}


def test_no_report_status_with_report_hash_rejected() -> None:
    with pytest.raises(ValidationError):
        _run(status=RunStatus.MISSED, report_id=None, report_hash="abc")


# Invariant 4: report_storage is not None iff status in {ok, partial}


def test_no_report_status_with_report_storage_rejected() -> None:
    with pytest.raises(ValidationError):
        _run(status=RunStatus.MISSED, report_id=None, report_storage="db")


def test_partial_missing_report_storage_rejected() -> None:
    with pytest.raises(ValidationError):
        _run(status=RunStatus.PARTIAL, report_id="r1", started_at=_T, report_storage=None)


# tz-aware timestamps required


def test_naive_triggered_at_rejected() -> None:
    with pytest.raises(ValidationError):
        _run(triggered_at=datetime(2026, 5, 26, 12, 0, 0))


def test_naive_started_at_rejected() -> None:
    with pytest.raises(ValidationError):
        _run(
            status=RunStatus.OK,
            report_id="r1",
            report_storage="db",
            started_at=datetime(2026, 5, 26, 12, 0, 0),
        )


def test_tz_aware_timestamps_accepted() -> None:
    run = _run(
        status=RunStatus.OK,
        report_id="r1",
        report_storage="db",
        started_at=_T,
        finished_at=_T,
    )
    assert run.triggered_at == _T


# report_hash determinism


def test_report_hash_deterministic_and_matches_render() -> None:
    report = _build_report()
    h1 = compute_report_hash(report)
    h2 = compute_report_hash(report)
    assert h1 == h2
    assert h1 == hashlib.sha256(render(report).encode()).hexdigest()
