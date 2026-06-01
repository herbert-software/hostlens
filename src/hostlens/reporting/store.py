"""SQLite persistence for `Report` — `ReportStore`.

`ReportStore` stores each report as a **redacted** JSON blob (the output
of `reporting.render_json.render`, which is the OPERABILITY §7.2
redaction boundary) plus a small set of index columns projected from the in-memory
`report.meta`. Projecting the index columns from the live `report`
object — rather than re-parsing the redacted JSON — keeps the index
reliable even in the (forbidden by construction) case where redaction
mangled a meta field: the identifier columns (`run_id` / `target_id` /
`target_name` / `status` / `report_schema_version` / `timestamp` /
`finding_count`) always reflect the authoritative `meta`.

Design references: `design.md` §决策 6 (store) and the
`report-persistence` capability spec. Key invariants:

- `save` requires `report.meta is not None` (raises `ValueError`
  otherwise — the factory always fills meta, so a None here is a
  programming error and the index cannot be projected without it).
- The `runs` table keeps SQLite's implicit `rowid` (a TEXT primary key
  does not trigger `WITHOUT ROWID`), which is a monotonic insertion
  counter used as the deterministic tie-break for the total order
  `(timestamp DESC, rowid DESC)` — `meta.timestamp` can tie (back-to-back
  inspects sharing `started_at`) or even move backwards (NTP step), so
  ordering on timestamp alone is non-deterministic.
- On INSERT failure (disk full / lock / permission), `save` retries
  once, then degrades to writing an orphan JSON file with
  `meta.status = "stored_as_orphan"` — the report is never silently
  dropped.

`sqlite3` is synchronous, so every database call is wrapped in
`asyncio.to_thread` per the async-first project constraint. There is no
module-level global connection: the db path is injected at construction
so tests use a temporary database.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from hostlens.reporting.models import (
    BaselineRef,
    Report,
    ReportMeta,
    ReportStatus,
)
from hostlens.reporting.render_json import render as render_report_json

__all__ = ["ReportStore", "RunIndexRow", "SaveResult"]


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL,
    target_name TEXT NOT NULL,
    schedule_name TEXT,
    status TEXT NOT NULL,
    report_schema_version TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    finding_count INTEGER NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

_CREATE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_runs_target_status_ts "
    "ON runs (target_id, status, timestamp DESC)"
)


class SaveResult(BaseModel):
    """Outcome of `ReportStore.save`.

    `stored_as_orphan` distinguishes a normal database insert from the
    degraded path where the main store was unwritable and the report was
    written to an orphan JSON file instead. A bare `str` return could not
    express that distinction — callers (CLI) key their exit code on it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    stored_as_orphan: bool
    orphan_path: str | None = None


class RunIndexRow(BaseModel):
    """One row of the run index returned by `ReportStore.list_runs`.

    Deliberately holds only the columns needed to list runs (no full
    `report_json`), so `reports list` can render without loading every
    report blob. The field set is exactly the four columns below; extra
    keys are rejected so the CLI `--json` schema stays stable.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    timestamp: datetime
    status: ReportStatus
    finding_count: int


def _default_db_path() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "hostlens" / "reports.db"


def _default_orphan_dir() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "hostlens" / "orphan_reports"


def _utc_index_timestamp(ts: datetime) -> str:
    """Normalize `ts` to a UTC isoformat string for the index column.

    `timestamp` is compared as TEXT by SQLite (`ORDER BY timestamp DESC`
    and the `before_run_id` total-order clause), so the lexical order must
    match the chronological order. Mixed-offset isoformats (`+00:00` vs
    `+08:00`) sort lexically out of chronological order, so all index
    timestamps are pinned to UTC: tz-aware values convert, naive values are
    treated as UTC. The full-fidelity `meta.timestamp` stays in the blob.
    """
    normalized = ts.astimezone(UTC) if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
    return normalized.isoformat()


class ReportStore:
    """SQLite-backed persistence for `Report`.

    The database path is injected so tests can use a temporary file; the
    CLI passes `$XDG_DATA_HOME/hostlens/reports.db` (default
    `~/.local/share/hostlens/reports.db`). No module-level connection is
    held — every method opens its own short-lived `sqlite3.Connection`
    inside `asyncio.to_thread`, which keeps the store trivially testable
    and avoids cross-event-loop connection sharing.
    """

    def __init__(self, db_path: Path | None = None, *, orphan_dir: Path | None = None) -> None:
        self._db_path = db_path if db_path is not None else _default_db_path()
        self._orphan_dir = orphan_dir if orphan_dir is not None else _default_orphan_dir()

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_CREATE_TABLE)
        conn.execute(_CREATE_INDEX)
        return conn

    async def save(self, report: Report) -> SaveResult:
        """Persist `report` to SQLite, returning a `SaveResult`.

        Requires `report.meta is not None` (the index columns are
        projected from `meta`). On INSERT failure the call retries once
        and then degrades to an orphan file, returning
        `SaveResult(stored_as_orphan=True, ...)` rather than losing the
        report.
        """
        if report.meta is None:
            raise ValueError(
                "ReportStore.save requires report.meta (index columns project from it)"
            )

        return await asyncio.to_thread(self._save_sync, report, report.meta)

    def _save_sync(self, report: Report, meta: ReportMeta) -> SaveResult:
        report_json = render_report_json(report)
        created_at = datetime.now().astimezone().isoformat()
        row = (
            meta.run_id,
            meta.target_id,
            meta.target_name,
            meta.schedule_name,
            str(meta.status),
            meta.report_schema_version,
            _utc_index_timestamp(meta.timestamp),
            len(report.findings),
            report_json,
            created_at,
        )

        for _attempt in range(2):
            try:
                conn = self._connect()
                try:
                    with conn:
                        conn.execute(
                            "INSERT INTO runs (run_id, target_id, target_name, schedule_name, "
                            "status, report_schema_version, timestamp, finding_count, "
                            "report_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            row,
                        )
                finally:
                    conn.close()
                return SaveResult(run_id=meta.run_id, stored_as_orphan=False, orphan_path=None)
            except sqlite3.IntegrityError:
                # Primary-key collision is not a transient failure — surface
                # it instead of producing a duplicate run_id row via orphan
                # fallback. (Spec §场景:run_id 主键唯一.)
                raise
            except (sqlite3.OperationalError, OSError):
                # Disk full / lock / readonly / unable-to-open surface as
                # `sqlite3.OperationalError`; mkdir / write-denied surface as
                # `OSError`. Retry once, then degrade to orphan. Narrowed from
                # `sqlite3.Error` so schema / programming errors
                # (`ProgrammingError` etc.) are not masquerading as orphans.
                continue

        return self._save_orphan(report, meta)

    def _save_orphan(self, report: Report, meta: ReportMeta) -> SaveResult:
        # Validate run_id is a real UUID *before* touching the filesystem:
        # the run_id becomes the orphan filename, so a value containing
        # path separators (`../`) would let a malformed report escape the
        # orphan directory.
        try:
            UUID(meta.run_id)
        except (ValueError, AttributeError) as exc:
            raise ValueError(
                f"refusing to write orphan file: run_id is not a valid UUID ({meta.run_id!r})"
            ) from exc

        orphan_report = report.model_copy(
            update={
                "meta": meta.model_copy(update={"status": ReportStatus.STORED_AS_ORPHAN}),
            }
        )
        orphan_json = render_report_json(orphan_report)

        self._orphan_dir.mkdir(parents=True, exist_ok=True)
        orphan_path = self._orphan_dir / f"{meta.run_id}.json"
        orphan_path.write_text(orphan_json, encoding="utf-8")

        return SaveResult(run_id=meta.run_id, stored_as_orphan=True, orphan_path=str(orphan_path))

    async def list_runs(self, target_id: str, *, limit: int = 20) -> list[RunIndexRow]:
        """Return the run index for `target_id`, newest first.

        Ordered by the total order `(timestamp DESC, rowid DESC)` and
        capped at `limit`.
        """
        return await asyncio.to_thread(self._list_runs_sync, target_id, limit)

    def _list_runs_sync(self, target_id: str, limit: int) -> list[RunIndexRow]:
        conn = self._connect()
        try:
            cursor = conn.execute(
                "SELECT run_id, timestamp, status, finding_count FROM runs "
                "WHERE target_id = ? ORDER BY timestamp DESC, rowid DESC LIMIT ?",
                (target_id, limit),
            )
            rows = cursor.fetchall()
        finally:
            conn.close()

        return [
            RunIndexRow(
                run_id=run_id,
                timestamp=datetime.fromisoformat(timestamp),
                status=ReportStatus(status),
                finding_count=finding_count,
            )
            for run_id, timestamp, status, finding_count in rows
        ]

    async def get_run(self, run_id: str) -> Report | None:
        """Return the full `Report` for `run_id`, or None if absent.

        The stored `report_json` is parsed with `Report.model_validate`;
        rows written by this store always carry `meta` (save rejects
        `meta is None`), so no legacy-meta reconstruction is attempted.
        """
        return await asyncio.to_thread(self._get_run_sync, run_id)

    def _get_run_sync(self, run_id: str) -> Report | None:
        conn = self._connect()
        try:
            cursor = conn.execute("SELECT report_json FROM runs WHERE run_id = ?", (run_id,))
            row = cursor.fetchone()
        finally:
            conn.close()

        if row is None:
            return None
        return Report.model_validate_json(row[0])

    async def latest_ok_baseline(
        self,
        target_id: str,
        *,
        schedule_name: str | None = None,
        before_run_id: str | None = None,
    ) -> BaselineRef | None:
        """Return the most-recent `ok` run for `target_id` as a `BaselineRef`.

        "Most recent" is the total order `(timestamp DESC, rowid DESC)` —
        timestamp alone is not enough because back-to-back inspects can
        share `meta.timestamp` and NTP steps can move it backwards, so the
        monotonic `rowid` is the deterministic tie-break.

        When `before_run_id` is given, only runs strictly earlier than it
        in the total order are eligible (excludes the current run itself
        and anything after it), which prevents a single `ok` run from
        being selected as its own baseline.

        `inspector_versions` is projected from the baseline report's
        `meta.inspectors_used` (name→version); it is never left empty,
        since diff version-alignment depends on it.
        """
        return await asyncio.to_thread(
            self._latest_ok_baseline_sync, target_id, schedule_name, before_run_id
        )

    def _latest_ok_baseline_sync(
        self, target_id: str, schedule_name: str | None, before_run_id: str | None
    ) -> BaselineRef | None:
        conn = self._connect()
        try:
            before_clause = ""
            params: list[object] = [target_id, str(ReportStatus.OK)]
            if schedule_name is not None:
                schedule_clause = "AND schedule_name = ?"
                params.append(schedule_name)
            else:
                schedule_clause = ""

            if before_run_id is not None:
                anchor = conn.execute(
                    "SELECT timestamp, rowid FROM runs WHERE run_id = ?",
                    (before_run_id,),
                ).fetchone()
                if anchor is None:
                    return None
                anchor_ts, anchor_rowid = anchor
                # Strictly earlier in the total order (timestamp, rowid):
                # earlier timestamp, or same timestamp with smaller rowid.
                before_clause = "AND (timestamp < ? OR (timestamp = ? AND rowid < ?))"
                params.extend([anchor_ts, anchor_ts, anchor_rowid])

            query = (
                "SELECT report_json FROM runs "
                f"WHERE target_id = ? AND status = ? {schedule_clause} {before_clause} "
                "ORDER BY timestamp DESC, rowid DESC LIMIT 1"
            )
            row = conn.execute(query, params).fetchone()
        finally:
            conn.close()

        if row is None:
            return None

        baseline = Report.model_validate_json(row[0])
        meta = baseline.meta
        if meta is None:
            return None

        return BaselineRef(
            run_id=meta.run_id,
            timestamp=meta.timestamp,
            status=meta.status,
            inspector_versions={ir.name: ir.version for ir in meta.inspectors_used},
            report_schema_version=meta.report_schema_version,
        )
