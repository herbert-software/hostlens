"""Scheduler execution ledger — `RunStatus` / `Run` / `RunStore`.

A `Run` is one entry in the scheduler's execution ledger: every fire of a
schedule (whether it produced a `Report`, was missed, skipped, or failed)
leaves exactly one `Run`. `RunStatus` is the eight-value closed set aligned
with docs/ARCHITECTURE.md §7; it is a **separate** enum from
`reporting.models.ReportStatus` (which only covers states that yield a
Report). The two must not be merged.

`Run` enforces four hard invariants at construction time via a
`model_validator(mode="after")` (fail-loud — a violated invariant raises
`ValidationError` rather than persisting dirty ledger data):

1. `report_id is None` iff `status not in {ok, partial}`.
2. `started_at is None` when `status in {missed, skipped_due_to_running,
   budget_exhausted}` (these are adjudicated before the job body ever
   runs).
3. `report_hash is not None` only when `status in {ok, partial}`.
4. `report_storage is not None` iff `status in {ok, partial}`.

`RunStore` mirrors `ReportStore`: its own injectable SQLite database
(default `~/.local/share/hostlens/runs.db`, **never** the same file as
`reports.db`), WAL journal, no module-level connection, every database
call wrapped in `asyncio.to_thread` per the async-first constraint.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from hostlens.notifiers.base import NotifyResult
from hostlens.reporting.models import Report
from hostlens.reporting.render_json import render

__all__ = ["Run", "RunStatus", "RunStore", "compute_report_hash"]


class RunStatus(StrEnum):
    """Closed eight-value set for `Run.status`, aligned with
    docs/ARCHITECTURE.md §7.

    Separate from `reporting.models.ReportStatus`: `ReportStatus` covers
    only states that yield a `Report`, while `RunStatus` also models the
    no-Report scheduling outcomes (`missed` / `skipped_due_to_running` /
    `failed*` / `daemon_stopped` / `budget_exhausted`).
    """

    OK = "ok"
    PARTIAL = "partial"
    BUDGET_EXHAUSTED = "budget_exhausted"
    MISSED = "missed"
    SKIPPED_DUE_TO_RUNNING = "skipped_due_to_running"
    FAILED_API_UNAVAILABLE = "failed_api_unavailable"
    FAILED = "failed"
    DAEMON_STOPPED = "daemon_stopped"


# Statuses that carry a Report (and therefore a report_id / report_hash /
# report_storage). The four invariants are all expressed relative to this set.
_REPORT_STATUSES: frozenset[RunStatus] = frozenset({RunStatus.OK, RunStatus.PARTIAL})

# Statuses adjudicated before the job body ever started (no started_at).
_NEVER_STARTED_STATUSES: frozenset[RunStatus] = frozenset(
    {
        RunStatus.MISSED,
        RunStatus.SKIPPED_DUE_TO_RUNNING,
        RunStatus.BUDGET_EXHAUSTED,
    }
)


def compute_report_hash(report: Report) -> str:
    """Integrity anchor for a `Report`.

    `sha256(render(report).encode()).hexdigest()` over the same
    deterministic redacted JSON bytes that `ReportStore.save` persists
    (`reporting.render_json.render`). Deterministic for a given `Report`
    object; not a cross-run dedup key (the report embeds run-specific
    timestamps).
    """
    return hashlib.sha256(render(report).encode()).hexdigest()


class Run(BaseModel):
    """One entry in the scheduler execution ledger.

    `notify_results` is `list[NotifyResult]` from M5 on: the runner fills it
    with one `NotifyResult` per routed channel after the Report is persisted,
    and it stays `[]` for any no-Report status. The M4 placeholder typed it
    `list[object]` (the `NotifyResult` type did not exist yet); the field is
    serialized into `runs.db` as the same JSON column (schema unchanged), and
    an M4 row whose `notify_results` is the empty array `[]` deserializes as
    a valid empty list with no migration.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    schedule_name: str
    triggered_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    status: RunStatus
    report_id: str | None = None
    error: str | None = None
    notify_results: list[NotifyResult] = Field(default_factory=list)
    targets: list[str]
    inspectors: list[str] = Field(default_factory=list)
    report_hash: str | None = None
    report_storage: Literal["db", "orphan"] | None = None

    @field_validator("triggered_at", "started_at", "finished_at", mode="after")
    @classmethod
    def _require_tz_aware(cls, value: datetime | None, info: ValidationInfo) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError(f"{info.field_name} must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _enforce_invariants(self) -> Self:
        has_report = self.status in _REPORT_STATUSES

        if (self.report_id is None) == has_report:
            raise ValueError(
                "report_id must be set iff status in {ok, partial}; "
                f"got status={self.status!s}, report_id={self.report_id!r}"
            )

        if self.status in _NEVER_STARTED_STATUSES and self.started_at is not None:
            raise ValueError(
                f"started_at must be None for status={self.status!s} (job body never started)"
            )

        if self.report_hash is not None and not has_report:
            raise ValueError(
                f"report_hash is only allowed for status in {{ok, partial}}; got {self.status!s}"
            )

        if (self.report_storage is not None) != has_report:
            raise ValueError(
                "report_storage must be set iff status in {ok, partial}; "
                f"got status={self.status!s}, report_storage={self.report_storage!r}"
            )

        return self


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    schedule_name TEXT NOT NULL,
    triggered_at TEXT NOT NULL,
    status TEXT NOT NULL,
    run_json TEXT NOT NULL
)
"""

_CREATE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_runs_schedule_triggered "
    "ON runs (schedule_name, triggered_at DESC)"
)


def _default_db_path() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "hostlens" / "runs.db"


class RunStore:
    """SQLite-backed persistence for `Run`, mirroring `ReportStore`.

    The database path is injected so tests use a temporary file; the
    default is `$XDG_DATA_HOME/hostlens/runs.db` (else
    `~/.local/share/hostlens/runs.db`) — a **different file** from
    `ReportStore`'s `reports.db`. No module-level connection is held:
    every method opens its own short-lived `sqlite3.Connection` inside
    `asyncio.to_thread`.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path if db_path is not None else _default_db_path()

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_CREATE_TABLE)
        conn.execute(_CREATE_INDEX)
        return conn

    async def save(self, run: Run) -> None:
        await asyncio.to_thread(self._save_sync, run)

    def _save_sync(self, run: Run) -> None:
        row = (
            run.run_id,
            run.schedule_name,
            # UTC-normalised so the TEXT index column sorts in true time order
            # across rows written with different tz offsets (job body uses UTC,
            # the listener uses the manifest tz). ``run_json`` keeps the
            # original tz-aware value untouched.
            run.triggered_at.astimezone(UTC).isoformat(),
            str(run.status),
            run.model_dump_json(),
        )
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO runs (run_id, schedule_name, triggered_at, status, run_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    row,
                )
        finally:
            conn.close()

    async def list_recent(self, *, schedule_name: str | None = None, limit: int = 20) -> list[Run]:
        """Return the most-recent `Run` rows, newest `triggered_at` first.

        When `schedule_name` is given, only that schedule's runs are
        returned. Capped at `limit`.
        """
        return await asyncio.to_thread(self._list_recent_sync, schedule_name, limit)

    def _list_recent_sync(self, schedule_name: str | None, limit: int) -> list[Run]:
        conn = self._connect()
        try:
            if schedule_name is not None:
                cursor = conn.execute(
                    "SELECT run_json FROM runs WHERE schedule_name = ? "
                    "ORDER BY triggered_at DESC, rowid DESC LIMIT ?",
                    (schedule_name, limit),
                )
            else:
                cursor = conn.execute(
                    "SELECT run_json FROM runs ORDER BY triggered_at DESC, rowid DESC LIMIT ?",
                    (limit,),
                )
            rows = cursor.fetchall()
        finally:
            conn.close()

        return [Run.model_validate_json(row[0]) for row in rows]
