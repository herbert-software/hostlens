"""Tests for `hostlens.reporting.store.ReportStore`.

Covers the `report-persistence` capability spec scenarios: save
round-trip / reject-missing-meta / redacted-blob / finding_count index /
primary-key uniqueness / orphan degradation (with UUID guard) /
list_runs ordering+limit / get_run miss / latest_ok_baseline selection
(skip non-ok, exclude current, rowid tie-break, none) / no-delete
boundary.

`asyncio_mode = "auto"` (pyproject) means `async def test_*` run without
an explicit marker.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from hostlens.inspectors.result import InspectorResult
from hostlens.reporting.models import (
    Finding,
    Report,
    ReportStatus,
    compute_finding_id,
)
from hostlens.reporting.store import ReportStore, RunIndexRow, SaveResult

SECRET = "sk-ABCDEFGHIJKLMNOPQRSTUVWX1234"


def _ir(
    name: str,
    findings: list[Finding],
    *,
    status: str = "ok",
    error: str | None = None,
) -> InspectorResult:
    return InspectorResult(
        name=name,
        version="1.0.0",
        status=status,  # type: ignore[arg-type]
        target_name="t",
        duration_seconds=0.1,
        output={},
        findings=findings,
        error=error,
        missing=[],
    )


def _report(
    *,
    target_name: str = "t",
    findings: list[Finding] | None = None,
    started_at: datetime | None = None,
    status: ReportStatus | None = None,
    irs: list[InspectorResult] | None = None,
) -> Report:
    ts = started_at if started_at is not None else datetime(2026, 5, 26, 12, 0, 0)
    fs = findings if findings is not None else [Finding(severity="info", message="hello")]
    inspector_results = irs if irs is not None else [_ir("hello.echo", fs)]
    return Report.from_inspector_results(
        target_name,
        inspector_results,
        started_at=ts,
        finished_at=ts,
        status=status,
    )


def _store(tmp_path: Path) -> ReportStore:
    return ReportStore(
        db_path=tmp_path / "reports.db",
        orphan_dir=tmp_path / "orphan_reports",
    )


# --------------------------------------------------------------------------
# save — round-trip / reject / redaction / finding_count / pk uniqueness
# --------------------------------------------------------------------------


async def test_save_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    report = _report()
    assert report.meta is not None

    result = await store.save(report)
    assert isinstance(result, SaveResult)
    assert result.stored_as_orphan is False
    assert result.orphan_path is None
    assert result.run_id == report.meta.run_id

    fetched = await store.get_run(result.run_id)
    assert fetched is not None
    assert fetched.meta is not None
    assert fetched.meta.run_id == report.meta.run_id
    assert [f.message for f in fetched.findings] == [f.message for f in report.findings]


async def test_save_rejects_missing_meta(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # A legacy schema-1.0 report has no meta; construct one directly.
    report = _report().model_copy(update={"meta": None, "schema_version": "1.0"})
    assert report.meta is None

    with pytest.raises(ValueError):
        await store.save(report)


async def test_save_blob_is_redacted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    report = _report(findings=[Finding(severity="warning", message=f"leaked {SECRET} here")])

    result = await store.save(report)

    # Read the raw column directly — the blob must not contain the plaintext key.
    import sqlite3

    conn = sqlite3.connect(tmp_path / "reports.db")
    try:
        row = conn.execute(
            "SELECT report_json FROM runs WHERE run_id = ?", (result.run_id,)
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert SECRET not in row[0]


async def test_finding_count_index(tmp_path: Path) -> None:
    store = _store(tmp_path)
    findings = [
        Finding(severity="info", message="a"),
        Finding(severity="warning", message="b"),
        Finding(severity="critical", message="c"),
    ]
    report = _report(findings=findings)
    assert report.meta is not None

    await store.save(report)

    import sqlite3

    conn = sqlite3.connect(tmp_path / "reports.db")
    try:
        row = conn.execute(
            "SELECT finding_count FROM runs WHERE run_id = ?", (report.meta.run_id,)
        ).fetchone()
    finally:
        conn.close()

    assert row[0] == 3


async def test_duplicate_run_id_is_not_silently_doubled(tmp_path: Path) -> None:
    store = _store(tmp_path)
    report = _report()
    assert report.meta is not None
    run_id = report.meta.run_id

    await store.save(report)
    # Re-saving the exact same report (same run_id) must not produce two rows;
    # the primary-key collision surfaces as a `sqlite3.IntegrityError` (not
    # degraded to orphan — the INSERT path re-raises it).
    with pytest.raises(sqlite3.IntegrityError):
        await store.save(report)

    rows = await store.list_runs(report.meta.target_id)
    assert sum(1 for r in rows if r.run_id == run_id) == 1


# --------------------------------------------------------------------------
# orphan degradation
# --------------------------------------------------------------------------


async def test_orphan_on_unwritable_db(tmp_path: Path) -> None:
    # Make the db path unwritable by placing it under a *file*, so
    # `mkdir(parents=True)` on the parent raises and the INSERT path fails.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir", encoding="utf-8")
    orphan_dir = tmp_path / "orphan_reports"
    store = ReportStore(db_path=blocker / "nested" / "reports.db", orphan_dir=orphan_dir)

    report = _report()
    assert report.meta is not None

    result = await store.save(report)
    assert result.stored_as_orphan is True
    assert result.orphan_path is not None

    orphan_file = Path(result.orphan_path)
    assert orphan_file.exists()
    assert orphan_file.parent == orphan_dir

    written = Report.model_validate_json(orphan_file.read_text(encoding="utf-8"))
    assert written.meta is not None
    assert written.meta.status == ReportStatus.STORED_AS_ORPHAN


async def test_orphan_blob_is_redacted(tmp_path: Path) -> None:
    # The orphan fallback also writes through `render_json`, so the on-disk
    # JSON must not carry plaintext secrets even on the degraded path.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir", encoding="utf-8")
    orphan_dir = tmp_path / "orphan_reports"
    store = ReportStore(db_path=blocker / "nested" / "reports.db", orphan_dir=orphan_dir)

    report = _report(findings=[Finding(severity="warning", message=f"leaked {SECRET} here")])

    result = await store.save(report)
    assert result.stored_as_orphan is True
    assert result.orphan_path is not None

    raw = Path(result.orphan_path).read_text(encoding="utf-8")
    assert SECRET not in raw


async def test_normal_save_not_orphan(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = await store.save(_report())
    assert result.stored_as_orphan is False
    assert result.orphan_path is None


async def test_orphan_rejects_non_uuid_run_id(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir", encoding="utf-8")
    orphan_dir = tmp_path / "orphan_reports"
    store = ReportStore(db_path=blocker / "nested" / "reports.db", orphan_dir=orphan_dir)

    report = _report()
    assert report.meta is not None
    # Forge a path-traversing run_id; save must refuse to write a file.
    evil_meta = report.meta.model_copy(update={"run_id": "../../etc/passwd"})
    evil_report = report.model_copy(update={"meta": evil_meta})

    with pytest.raises(ValueError):
        await store.save(evil_report)

    assert not orphan_dir.exists() or list(orphan_dir.iterdir()) == []


# --------------------------------------------------------------------------
# list_runs / get_run
# --------------------------------------------------------------------------


async def test_list_runs_descending_and_limited(tmp_path: Path) -> None:
    store = _store(tmp_path)
    base = datetime(2026, 5, 26, 12, 0, 0)
    for i in range(3):
        await store.save(_report(started_at=base + timedelta(minutes=i)))

    rows = await store.list_runs("t", limit=2)
    assert len(rows) == 2
    assert all(isinstance(r, RunIndexRow) for r in rows)
    assert rows[0].timestamp >= rows[1].timestamp
    assert all(r.finding_count == 1 for r in rows)


async def test_list_runs_orders_by_real_time_across_timezones(tmp_path: Path) -> None:
    # The index `timestamp` column is normalized to UTC, so ordering follows
    # real time even when `started_at` carries different offsets. `earlier`
    # is chronologically before `later` (04:00Z vs 06:00Z) but its raw
    # isoformat (`...T12:00:00+08:00`) sorts lexically *after* `later`
    # (`...T06:00:00+00:00`) — a naive TEXT sort would invert the order.
    earlier = datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    later = datetime(2026, 5, 26, 6, 0, 0, tzinfo=UTC)
    assert earlier.astimezone(UTC) < later  # 04:00Z < 06:00Z

    store = _store(tmp_path)
    earlier_report = _report(started_at=earlier, status=ReportStatus.OK)
    later_report = _report(started_at=later, status=ReportStatus.OK)
    assert earlier_report.meta is not None and later_report.meta is not None
    # Insert the chronologically-later run first so rowid cannot accidentally
    # carry the ordering — only the normalized timestamp can.
    await store.save(later_report)
    await store.save(earlier_report)

    rows = await store.list_runs("t", limit=10)
    assert [r.run_id for r in rows] == [
        later_report.meta.run_id,
        earlier_report.meta.run_id,
    ]

    # The baseline anchored on the later run must select the chronologically
    # earlier run, not invert by lexical comparison.
    baseline = await store.latest_ok_baseline("t", before_run_id=later_report.meta.run_id)
    assert baseline is not None
    assert baseline.run_id == earlier_report.meta.run_id


async def test_latest_ok_baseline_scopes_by_schedule_name(tmp_path: Path) -> None:
    store = _store(tmp_path)
    base = datetime(2026, 5, 26, 12, 0, 0)

    sched_x = Report.from_inspector_results(
        "t",
        [_ir("hello.echo", [Finding(severity="info", message="x")])],
        started_at=base,
        finished_at=base,
        status=ReportStatus.OK,
        schedule_name="x",
    )
    sched_y = Report.from_inspector_results(
        "t",
        [_ir("hello.echo", [Finding(severity="info", message="y")])],
        started_at=base + timedelta(minutes=1),
        finished_at=base + timedelta(minutes=1),
        status=ReportStatus.OK,
        schedule_name="y",
    )
    assert sched_x.meta is not None and sched_y.meta is not None
    await store.save(sched_x)
    await store.save(sched_y)

    # Scoped to schedule "x": only the x run is eligible even though y is newer.
    scoped = await store.latest_ok_baseline("t", schedule_name="x")
    assert scoped is not None
    assert scoped.run_id == sched_x.meta.run_id

    # schedule_name=None does not constrain → newest ok run overall (y).
    unscoped = await store.latest_ok_baseline("t")
    assert unscoped is not None
    assert unscoped.run_id == sched_y.meta.run_id


async def test_get_run_missing_returns_none(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = await store.get_run("00000000-0000-0000-0000-000000000000")
    assert result is None


# --------------------------------------------------------------------------
# latest_ok_baseline
# --------------------------------------------------------------------------


async def test_latest_ok_baseline_skips_non_ok(tmp_path: Path) -> None:
    store = _store(tmp_path)
    base = datetime(2026, 5, 26, 12, 0, 0)

    ok_report = _report(started_at=base, status=ReportStatus.OK)
    await store.save(ok_report)
    # Newer run is partial.
    partial_report = _report(
        started_at=base + timedelta(minutes=1),
        status=ReportStatus.PARTIAL,
    )
    await store.save(partial_report)

    baseline = await store.latest_ok_baseline("t")
    assert baseline is not None
    assert ok_report.meta is not None
    assert baseline.run_id == ok_report.meta.run_id
    # inspector_versions projected, never empty.
    assert baseline.inspector_versions == {"hello.echo": "1.0.0"}


async def test_latest_ok_baseline_excludes_current_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    report = _report(status=ReportStatus.OK)
    assert report.meta is not None
    run_id = report.meta.run_id
    await store.save(report)

    # Without before_run_id: returns the single ok run.
    assert (await store.latest_ok_baseline("t")) is not None
    assert (await store.latest_ok_baseline("t")).run_id == run_id  # type: ignore[union-attr]

    # With before_run_id=X (the only run): excluded → None.
    assert (await store.latest_ok_baseline("t", before_run_id=run_id)) is None


async def test_latest_ok_baseline_rowid_tiebreak(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ts = datetime(2026, 5, 26, 12, 0, 0)

    a = _report(started_at=ts, status=ReportStatus.OK)
    b = _report(started_at=ts, status=ReportStatus.OK)
    assert a.meta is not None and b.meta is not None
    # Same timestamp; A inserted first (smaller rowid), then B.
    await store.save(a)
    await store.save(b)

    baseline = await store.latest_ok_baseline("t", before_run_id=b.meta.run_id)
    assert baseline is not None
    assert baseline.run_id == a.meta.run_id


async def test_latest_ok_baseline_none_when_no_ok(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.save(_report(status=ReportStatus.PARTIAL))
    assert (await store.latest_ok_baseline("t")) is None
    # No runs at all for an unknown target.
    assert (await store.latest_ok_baseline("nonexistent")) is None


# --------------------------------------------------------------------------
# storage boundary — no auto-delete / no retention
# --------------------------------------------------------------------------


async def test_save_never_deletes_existing_rows(tmp_path: Path) -> None:
    store = _store(tmp_path)
    base = datetime(2026, 5, 26, 12, 0, 0)
    saved = []
    for i in range(5):
        r = _report(started_at=base + timedelta(minutes=i))
        await store.save(r)
        assert r.meta is not None
        saved.append(r.meta.run_id)

    # All five remain retrievable — save is append-only, no retention sweep.
    rows = await store.list_runs("t", limit=100)
    assert {r.run_id for r in rows} == set(saved)
    for run_id in saved:
        assert (await store.get_run(run_id)) is not None


def test_compute_finding_id_helper_is_stable() -> None:
    # Sanity: the fingerprint used by the factory is deterministic, so
    # round-tripped findings keep their id (relied on by diff downstream).
    a = compute_finding_id("hello.echo", "1.0.0", "hello")
    b = compute_finding_id("hello.echo", "1.0.0", "hello")
    assert a == b
