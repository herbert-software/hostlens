"""Unit tests for `RunStore` persistence and the runs.db / reports.db split."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from hostlens.reporting.store import ReportStore
from hostlens.scheduler.store import Run, RunStatus, RunStore

_BASE = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)


def _run(run_id: str, triggered_at: datetime, schedule_name: str = "nightly") -> Run:
    return Run(
        run_id=run_id,
        schedule_name=schedule_name,
        triggered_at=triggered_at,
        status=RunStatus.MISSED,
        report_id=None,
        targets=["local-host"],
    )


async def test_runs_round_trip_newest_first(tmp_path: Path) -> None:
    store = RunStore(db_path=tmp_path / "runs.db")
    runs = [
        _run("r1", _BASE),
        _run("r2", _BASE + timedelta(minutes=1)),
        _run("r3", _BASE + timedelta(minutes=2)),
    ]
    for run in runs:
        await store.save(run)

    got = await store.list_recent(limit=10)
    assert [r.run_id for r in got] == ["r3", "r2", "r1"]
    assert got[0].schedule_name == "nightly"
    assert got[0].status is RunStatus.MISSED


async def test_list_recent_respects_limit(tmp_path: Path) -> None:
    store = RunStore(db_path=tmp_path / "runs.db")
    for i in range(5):
        await store.save(_run(f"r{i}", _BASE + timedelta(minutes=i)))

    got = await store.list_recent(limit=2)
    assert [r.run_id for r in got] == ["r4", "r3"]


async def test_list_recent_filters_by_schedule_name(tmp_path: Path) -> None:
    store = RunStore(db_path=tmp_path / "runs.db")
    await store.save(_run("a1", _BASE, schedule_name="alpha"))
    await store.save(_run("b1", _BASE + timedelta(minutes=1), schedule_name="beta"))
    await store.save(_run("a2", _BASE + timedelta(minutes=2), schedule_name="alpha"))

    got = await store.list_recent(schedule_name="alpha", limit=10)
    assert [r.run_id for r in got] == ["a2", "a1"]


async def test_list_recent_orders_by_utc_across_tz_offsets(tmp_path: Path) -> None:
    store = RunStore(db_path=tmp_path / "runs.db")
    # A is 2026-01-01T13:00:00+08:00 == 05:00 UTC (earlier); B is
    # 2026-01-01T06:00:00+00:00 == 06:00 UTC (later). A naive TEXT sort on the
    # tz-aware isoformat would mis-rank A before B ("13" > "06").
    a = _run("a", datetime(2026, 1, 1, 13, 0, 0, tzinfo=timezone(timedelta(hours=8))))
    b = _run("b", datetime(2026, 1, 1, 6, 0, 0, tzinfo=UTC))
    await store.save(a)
    await store.save(b)

    got = await store.list_recent(limit=10)
    assert [r.run_id for r in got] == ["b", "a"]
    # run_json keeps the original offset (the index normalisation must not leak).
    by_id = {r.run_id: r for r in got}
    assert by_id["a"].triggered_at.utcoffset() == timedelta(hours=8)


async def test_runs_db_separate_from_reports_db(tmp_path: Path) -> None:
    runs_db = tmp_path / "runs.db"
    reports_db = tmp_path / "reports.db"
    run_store = RunStore(db_path=runs_db)
    report_store = ReportStore(db_path=reports_db, orphan_dir=tmp_path / "orphans")

    await run_store.save(_run("r1", _BASE))

    assert runs_db.exists()
    assert runs_db != reports_db
    # The report store never wrote to runs.db; querying it leaves the run intact.
    assert await report_store.list_runs("local-host", limit=10) == []
    got = await run_store.list_recent(limit=10)
    assert [r.run_id for r in got] == ["r1"]
