"""CLI tests for the ``hostlens reports`` subcommand group and
``hostlens inspect --persist``.

Spec: ``openspec/changes/add-report-persistence-and-diff/specs/report-persistence/spec.md``
and ``.../specs/report-regression-diff/spec.md``.

Scenarios covered:

- ``reports show`` unknown run → exit 3, single stderr line, no traceback
  (report-persistence §场景:show 未知 run 退出码 3).
- ``reports list`` empty history → exit 0, hint, no traceback
  (report-persistence §场景:list 空历史退出码 0).
- ``reports list --json`` field-set stability
  (report-persistence §场景:list --json 字段集稳定).
- ``reports diff`` unknown run → exit 3
  (report-regression-diff §场景:未知 run 退出码 3).
- ``reports diff --target`` with no comparable baseline → exit 0
  (report-regression-diff §场景:无基线时退出码 0 / 自动模式不把唯一 run 当自身基线).
- ``inspect --persist`` round-trip: persisted runs are listable + showable
  (report-persistence §场景:--persist 后报告可被 reports list 看到).
- ``--intent`` / ``demo run`` reject / do not expose ``--persist``
  (report-persistence §场景:--intent 与 demo run 不接受 --persist).

The driver is the same ``_run_main`` (sys.argv patch + ``main()`` +
capsys) used across ``tests/cli`` — it exercises the click-UsageError →
exit 3 wrapper in ``hostlens.cli.main`` that ``CliRunner.invoke`` bypasses.
The SQLite store path is redirected by pointing ``XDG_DATA_HOME`` at a tmp
dir, the same knob ``inspect --persist`` and the ``reports`` commands both
read via the default ``ReportStore``.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest
import yaml

import hostlens.inspectors.result  # noqa: F401  (triggers Report.model_rebuild)
from hostlens.cli import main
from hostlens.inspectors.result import InspectorResult
from hostlens.reporting.models import Finding, Report, ReportStatus
from hostlens.reporting.store import ReportStore, SaveResult

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def xdg_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the default ``ReportStore`` at a tmp ``$XDG_DATA_HOME``.

    Both ``inspect --persist`` and the ``reports`` commands construct a
    default ``ReportStore()``, whose db path resolves under
    ``$XDG_DATA_HOME/hostlens/reports.db``.
    """

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def targets_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``Settings.targets_config_path`` at a tmp targets.yaml."""

    path = tmp_path / "targets.yaml"
    path.write_text(
        yaml.safe_dump(
            {"version": "1", "targets": [{"name": "local-host", "type": "local"}]},
            sort_keys=False,
        )
    )
    monkeypatch.setenv("HOSTLENS_TARGETS_CONFIG_PATH", str(path))
    return path


@pytest.fixture
def user_inspectors_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Empty user inspectors dir; builtins (hello.echo) still resolve."""

    user_path = tmp_path / "inspectors"
    user_path.mkdir()
    monkeypatch.setenv("HOSTLENS_INSPECTORS_SEARCH_PATHS", str(user_path))
    return user_path


def _run_main(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[int, str, str]:
    """Invoke ``hostlens.cli.main`` end-to-end and capture (exit, stdout, stderr)."""

    monkeypatch.setattr(sys, "argv", ["hostlens", *argv])
    try:
        main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    else:
        code = 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _seed_report(
    store_dir: Path,
    *,
    target_name: str = "local-host",
    findings: list[Finding] | None = None,
    status: ReportStatus | None = None,
) -> str:
    """Write one report straight into the tmp store, returning its run_id.

    Uses the default ``ReportStore`` path under ``store_dir`` (the
    ``xdg_home`` fixture sets ``XDG_DATA_HOME``) so the seeded run is
    visible to the CLI commands without going through ``inspect``.
    """

    import asyncio

    fs = findings if findings is not None else [Finding(severity="info", message="hello")]
    ir = InspectorResult(
        name="hello.echo",
        version="1.0.0",
        status="ok",
        target_name=target_name,
        duration_seconds=0.1,
        output={},
        findings=fs,
        error=None,
        missing=[],
    )
    ts = datetime(2026, 5, 26, 12, 0, 0)
    report = Report.from_inspector_results(
        target_name, [ir], started_at=ts, finished_at=ts, status=status
    )
    store = ReportStore(
        db_path=store_dir / "hostlens" / "reports.db",
        orphan_dir=store_dir / "hostlens" / "orphan_reports",
    )
    result = asyncio.run(store.save(report))
    return result.run_id


# --------------------------------------------------------------------------- #
# reports show — unknown run → exit 3
# --------------------------------------------------------------------------- #


def test_reports_show_unknown_run_exits_3(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    xdg_home: Path,
) -> None:
    unknown = "00000000-0000-0000-0000-000000000000"
    exit_code, stdout, stderr = _run_main(["reports", "show", unknown], capsys, monkeypatch)

    assert exit_code == 3
    assert stdout == ""
    assert "run not found:" in stderr
    assert "reports list" in stderr
    assert "Traceback" not in stderr
    # Single-line error (no traceback frames).
    assert len(stderr.strip().splitlines()) == 1


def test_reports_show_corrupt_blob_exits_3_no_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    xdg_home: Path,
) -> None:
    """A damaged / manually edited ``report_json`` row (run_id still present)
    makes ``get_run``'s ``Report.model_validate_json`` raise ``ValidationError``.
    ``reports show`` / ``diff`` must surface it as a single stderr line + exit 3,
    never a Python traceback.
    """
    run_id = _seed_report(xdg_home)
    # Corrupt the stored blob in place (valid run_id, invalid report_json).
    db = xdg_home / "hostlens" / "reports.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE runs SET report_json = ? WHERE run_id = ?", ("{not valid", run_id))
        conn.commit()
    finally:
        conn.close()

    show_code, show_out, show_err = _run_main(["reports", "show", run_id], capsys, monkeypatch)
    assert show_code == 3
    assert show_out == ""
    assert "invalid or corrupt" in show_err
    assert "Traceback" not in show_err
    assert len(show_err.strip().splitlines()) == 1

    # The diff read-path loads via the same helper — also no traceback.
    diff_code, _diff_out, diff_err = _run_main(
        ["reports", "diff", run_id, run_id], capsys, monkeypatch
    )
    assert diff_code == 3
    assert "invalid or corrupt" in diff_err
    assert "Traceback" not in diff_err


def test_reports_list_corrupt_index_row_exits_3_no_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    xdg_home: Path,
) -> None:
    """An index row whose ``status`` is not a valid ``ReportStatus`` makes
    ``_list_runs_sync``'s ``RunIndexRow`` construction raise ``ValueError``.
    ``reports list`` must surface it as a single stderr line + exit 3, never a
    Python traceback (parity with the corrupt-blob ``show`` / ``diff`` path).
    """
    _seed_report(xdg_home)
    db = xdg_home / "hostlens" / "reports.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE runs SET status = ?", ("not-a-status",))
        conn.commit()
    finally:
        conn.close()

    list_code, list_out, list_err = _run_main(
        ["reports", "list", "local-host"], capsys, monkeypatch
    )
    assert list_code == 3
    assert list_out == ""
    assert "store unavailable or corrupt" in list_err
    assert "Traceback" not in list_err
    assert len(list_err.strip().splitlines()) == 1


# --------------------------------------------------------------------------- #
# reports list — empty history → exit 0
# --------------------------------------------------------------------------- #


def test_reports_list_empty_history_exits_0(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    xdg_home: Path,
) -> None:
    exit_code, stdout, stderr = _run_main(["reports", "list", "nope-target"], capsys, monkeypatch)

    assert exit_code == 0
    assert "无历史 run" in stdout
    assert "Traceback" not in stderr


def test_reports_list_empty_history_json_is_empty_array(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    xdg_home: Path,
) -> None:
    exit_code, stdout, _ = _run_main(
        ["reports", "list", "nope-target", "--json"], capsys, monkeypatch
    )

    assert exit_code == 0
    assert json.loads(stdout) == []


# --------------------------------------------------------------------------- #
# reports list --json — field-set stability
# --------------------------------------------------------------------------- #


def test_reports_list_json_field_set_stable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    xdg_home: Path,
) -> None:
    _seed_report(xdg_home)

    exit_code, stdout, _ = _run_main(
        ["reports", "list", "local-host", "--json"], capsys, monkeypatch
    )

    assert exit_code == 0
    payload = json.loads(stdout)
    assert isinstance(payload, list)
    assert len(payload) == 1
    assert set(payload[0].keys()) == {
        "run_id",
        "timestamp",
        "status",
        "finding_count",
    }
    assert payload[0]["finding_count"] == 1


# --------------------------------------------------------------------------- #
# reports diff — unknown run → exit 3
# --------------------------------------------------------------------------- #


def test_reports_diff_unknown_run_exits_3(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    xdg_home: Path,
) -> None:
    existing = _seed_report(xdg_home)
    unknown = "11111111-1111-1111-1111-111111111111"

    exit_code, stdout, stderr = _run_main(
        ["reports", "diff", existing, unknown], capsys, monkeypatch
    )

    assert exit_code == 3
    assert stdout == ""
    assert "run not found:" in stderr
    assert "Traceback" not in stderr
    assert len(stderr.strip().splitlines()) == 1


# --------------------------------------------------------------------------- #
# reports diff --target — no comparable baseline → exit 0
# --------------------------------------------------------------------------- #


def test_reports_diff_target_no_baseline_exits_0(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    xdg_home: Path,
) -> None:
    """A target with no runs at all → no baseline → exit 0, not an error."""

    exit_code, stdout, stderr = _run_main(
        ["reports", "diff", "--target", "no-history"], capsys, monkeypatch
    )

    assert exit_code == 0
    assert "无可比基线" in stdout
    assert "Traceback" not in stderr


def test_reports_diff_target_single_ok_run_no_baseline_exits_0(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    xdg_home: Path,
) -> None:
    """A single ok run must not be its own baseline (current is excluded)."""

    _seed_report(xdg_home, target_name="local-host", status=ReportStatus.OK)

    exit_code, stdout, _ = _run_main(
        ["reports", "diff", "--target", "local-host"], capsys, monkeypatch
    )

    assert exit_code == 0
    assert "无可比基线" in stdout


# --------------------------------------------------------------------------- #
# reports diff — explicit two-run, empty diff (same report content)
# --------------------------------------------------------------------------- #


def test_reports_diff_explicit_empty_diff_exits_0(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    xdg_home: Path,
) -> None:
    """Two reports with identical findings → empty added/resolved/changed."""

    run_a = _seed_report(xdg_home, status=ReportStatus.OK)
    run_b = _seed_report(xdg_home, status=ReportStatus.OK)

    exit_code, stdout, stderr = _run_main(["reports", "diff", run_a, run_b], capsys, monkeypatch)

    assert exit_code == 0, stderr
    assert "added (0):" in stdout
    assert "resolved (0):" in stdout
    assert "changed_severity (0):" in stdout


# --------------------------------------------------------------------------- #
# reports diff — cross-target rejection (compute_diff ValueError) → exit 3
# --------------------------------------------------------------------------- #


def test_reports_diff_cross_target_exits_3_no_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    xdg_home: Path,
) -> None:
    """Explicit two-run diff across different targets → exit 3, single stderr
    line, no traceback (report-regression-diff §场景:跨 target diff 被拒绝).

    `compute_diff` raises `ValueError` on a `meta.target_id` mismatch; the
    CLI must catch it instead of letting a Rich traceback surface.
    """

    run_a = _seed_report(xdg_home, target_name="host-a", status=ReportStatus.OK)
    run_b = _seed_report(xdg_home, target_name="host-b", status=ReportStatus.OK)

    exit_code, stdout, stderr = _run_main(["reports", "diff", run_a, run_b], capsys, monkeypatch)

    assert exit_code == 3
    assert stdout == ""
    assert "hostlens reports diff:" in stderr
    assert "Traceback" not in stderr
    assert "Traceback" not in stdout
    assert len(stderr.strip().splitlines()) == 1


# --------------------------------------------------------------------------- #
# inspect --persist round-trip
# --------------------------------------------------------------------------- #


def test_inspect_persist_round_trip_listable_and_showable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    xdg_home: Path,
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """Spec §场景:--persist 后报告可被 reports list 看到.

    Two ``inspect --persist`` runs of the deterministic ``hello.echo``
    inspector → ``reports list`` lists >= 2 runs, each ``reports show``-able.
    """

    for _ in range(2):
        code, _, stderr = _run_main(
            ["inspect", "local-host", "--inspector", "hello.echo", "--persist"],
            capsys,
            monkeypatch,
        )
        assert code == 0, stderr

    code, stdout, _ = _run_main(["reports", "list", "local-host", "--json"], capsys, monkeypatch)
    assert code == 0
    rows = json.loads(stdout)
    assert len(rows) >= 2

    for row in rows:
        # Each persisted run must be retrievable via reports show.
        uuid.UUID(row["run_id"])  # well-formed run id
        show_code, show_out, _ = _run_main(["reports", "show", row["run_id"]], capsys, monkeypatch)
        assert show_code == 0
        assert "Hostlens Inspection Report" in show_out


def test_inspect_persist_double_failure_no_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    xdg_home: Path,
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """When both the db and the orphan dir are unwritable, ``store.save``
    re-raises ``OSError`` rather than silently dropping the report; the
    ``inspect --persist`` boundary must surface it as a single stderr line
    with a non-zero exit, no Python traceback.
    """

    async def _boom(self: ReportStore, report: object) -> object:
        raise OSError("disk full and orphan dir unwritable")

    monkeypatch.setattr(ReportStore, "save", _boom)

    code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--inspector", "hello.echo", "--persist"],
        capsys,
        monkeypatch,
    )

    assert code == 2
    assert "failed to persist report" in stderr
    assert "Traceback" not in stderr
    assert "Traceback" not in stdout


def test_inspect_persist_corrupt_db_no_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    xdg_home: Path,
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """A corrupt ``reports.db`` makes ``store.save`` raise ``sqlite3.Error``
    (e.g. ``DatabaseError: file is not a database``), which is neither
    ``OperationalError`` nor ``OSError`` — the store layer deliberately lets
    it bubble rather than masquerading as an orphan. The ``inspect --persist``
    boundary must still surface it as a single stderr line with exit 2, no
    Python traceback (no raw ``sqlite3.Error`` reaching the user).
    """

    async def _boom(self: ReportStore, report: object) -> object:
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(ReportStore, "save", _boom)

    code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--inspector", "hello.echo", "--persist"],
        capsys,
        monkeypatch,
    )

    assert code == 2
    assert "failed to persist report" in stderr
    assert "DatabaseError" in stderr
    assert "Traceback" not in stderr
    assert "Traceback" not in stdout
    assert len(stderr.strip().splitlines()) == 1


def test_inspect_persist_failure_preserves_critical_exit_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    xdg_home: Path,
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """A persist failure escalates the exit code to 2 only when the
    inspector-derived code is 0. A critical finding's exit 1 takes priority
    and is preserved — the persist-failure warning still surfaces, but the exit
    stays 1 so a real diagnosis is never masked by a storage problem.
    """

    async def _boom(self: ReportStore, report: object) -> object:
        raise OSError("store unavailable")

    monkeypatch.setattr(ReportStore, "save", _boom)
    # Simulate a critical finding (exit 1) independent of the echo inspector.
    monkeypatch.setattr("hostlens.cli.inspect._compute_exit_code", lambda _r: 1)

    code, _stdout, stderr = _run_main(
        ["inspect", "local-host", "--inspector", "hello.echo", "--persist"],
        capsys,
        monkeypatch,
    )

    assert code == 1  # critical finding's exit 1 preserved, not clobbered to 2
    assert "failed to persist report" in stderr  # persist failure still surfaced
    assert "Traceback" not in stderr


def test_inspect_persist_orphan_warns_and_exits_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    xdg_home: Path,
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """When ``store.save`` degrades to an orphan file (returns
    ``SaveResult(stored_as_orphan=True)`` without raising), the CLI takes the
    warning branch — not the Fix A internal-error branch — emitting a
    ``report store unavailable`` warning and escalating exit to 2, with no
    traceback.
    """

    orphan_path = str(xdg_home / "hostlens" / "orphan_reports" / "x.json")

    async def _orphan(self: ReportStore, report: object) -> SaveResult:
        return SaveResult(
            run_id="11111111-1111-1111-1111-111111111111",
            stored_as_orphan=True,
            orphan_path=orphan_path,
        )

    monkeypatch.setattr(ReportStore, "save", _orphan)

    code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--inspector", "hello.echo", "--persist"],
        capsys,
        monkeypatch,
    )

    assert code == 2
    assert "report store unavailable" in stderr
    assert "failed to persist report" not in stderr
    assert "Traceback" not in stderr
    assert "Traceback" not in stdout


def test_inspect_without_persist_does_not_write_store(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    xdg_home: Path,
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """``--persist`` defaults off — a plain inspect leaves the store empty."""

    code, _, stderr = _run_main(
        ["inspect", "local-host", "--inspector", "hello.echo"],
        capsys,
        monkeypatch,
    )
    assert code == 0, stderr

    list_code, list_out, _ = _run_main(
        ["reports", "list", "local-host", "--json"], capsys, monkeypatch
    )
    assert list_code == 0
    assert json.loads(list_out) == []


# --------------------------------------------------------------------------- #
# --intent / demo run do not accept --persist
# --------------------------------------------------------------------------- #


def test_inspect_intent_rejects_persist(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    xdg_home: Path,
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """``--persist`` with ``--intent`` is a usage error (exit 3), no traceback.

    Reaching the rejection does not require a configured backend — the
    flag check fires before any Agent assembly.
    """

    code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "check disk", "--persist"],
        capsys,
        monkeypatch,
    )

    assert code == 3
    assert stdout == ""
    assert "--persist is not supported with --intent" in stderr
    assert "Traceback" not in stderr


def test_demo_run_does_not_expose_persist(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    xdg_home: Path,
) -> None:
    """``demo run`` has no ``--persist`` flag → unknown option is exit 3."""

    code, stdout, stderr = _run_main(
        ["demo", "run", "any-scenario", "--persist"], capsys, monkeypatch
    )

    assert code == 3
    assert stdout == ""
    assert "Traceback" not in stderr
