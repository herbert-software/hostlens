"""End-to-end ``hostlens demo run --persist`` + ``reports`` integration (task 4.2).

Spec: ``openspec/changes/wire-demo-to-report/specs/demo-cli-command/spec.md``
(§需求:demo run --persist 落盘 + its 5 场景).

``demo run --persist`` saves the assembled demo ``Report`` to the STANDARD
``ReportStore`` (the same ``$XDG_DATA_HOME/hostlens/reports.db`` ``hostlens
reports`` reads), so a demo run can be replayed offline through the M3.1
persistence loop. The demo Report is tagged ``target_name == demo:<scenario>``.

Every test here MUST ``monkeypatch.setenv("XDG_DATA_HOME", ...)`` to a tmp path
so it persists into an isolated store and never the real user db — ``_persist_report``
constructs ``ReportStore()`` with no path injection and resolves
``$XDG_DATA_HOME/hostlens/reports.db``.

These drive ``hostlens.cli.main`` end to end via ``_run_main`` (so the
UsageError → exit-3 rewrite + the ``standalone_mode=False`` typer.Exit plumbing
run). Every backend is a ``PlaybackBackend`` — nothing hits the network.
``asyncio_mode = "auto"`` (pyproject) — no markers.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from hostlens.cli import main
from hostlens.reporting.diff import compute_diff
from hostlens.reporting.models import Report
from hostlens.reporting.store import ReportStore, SaveResult


def _run_main(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", ["hostlens", *argv])
    try:
        main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    else:
        code = 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def xdg_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the default ``ReportStore`` at a tmp ``$XDG_DATA_HOME`` so
    ``--persist`` + the ``reports`` commands share the same db and the real user
    store is never touched."""

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    return xdg


def _real_user_db_untouched(xdg: Path) -> None:
    """Assert nothing was written outside the isolated tmp store."""

    real = Path.home() / ".local" / "share" / "hostlens" / "reports.db"
    # The isolated store lives under the tmp xdg, never the real home path. We
    # can't reliably assert the real db's absence (a prior real run may exist),
    # but we CAN assert the tmp store is the one that grew.
    assert (xdg / "hostlens" / "reports.db").exists()
    assert real != (xdg / "hostlens" / "reports.db")


# --------------------------------------------------------------------------- #
# --persist saves a faithful Report; reports show retrieves it (target_name marked)
# --------------------------------------------------------------------------- #


def test_demo_persist_round_trips_reports_show_with_demo_target_name(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    xdg_home: Path,
) -> None:
    """``demo run cpu_saturation --persist`` saves the Report (incl. hypotheses);
    ``reports show <run_id>`` retrieves it; ``target_name`` is ``demo:cpu_saturation``.

    Spec §场景:--persist 落盘并可经 reports 取回.
    """

    code, stdout, stderr = _run_main(
        ["demo", "run", "cpu_saturation", "--persist", "-f", "json", "--quiet"],
        capsys,
        monkeypatch,
    )
    assert code == 1, stderr  # ok + critical finding
    report = Report.model_validate_json(stdout)
    assert report.meta is not None
    assert report.meta.target_name == "demo:cpu_saturation"
    assert report.hypotheses
    run_id = report.meta.run_id

    _real_user_db_untouched(xdg_home)

    # reports show retrieves it globally by run_id (the demo: prefix does not block it).
    show_code, show_out, show_err = _run_main(
        ["reports", "show", run_id, "--format", "json"], capsys, monkeypatch
    )
    assert show_code == 0, show_err
    shown = Report.model_validate_json(show_out)
    assert shown.meta is not None
    assert shown.meta.run_id == run_id
    assert shown.meta.target_name == "demo:cpu_saturation"
    assert shown.hypotheses

    # demo:<scenario> works as the per-target query key for reports list.
    list_code, list_out, list_err = _run_main(
        ["reports", "list", "demo:cpu_saturation", "--json"], capsys, monkeypatch
    )
    assert list_code == 0, list_err
    listed = json.loads(list_out)
    assert any(row.get("run_id") == run_id for row in listed)


# --------------------------------------------------------------------------- #
# without --persist nothing is stored
# --------------------------------------------------------------------------- #


def test_demo_run_without_persist_stores_nothing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    xdg_home: Path,
) -> None:
    """``demo run`` (no ``--persist``) only renders; ``reports list`` finds nothing.

    Spec §场景:不传 --persist 不落盘.
    """

    code, stdout, _stderr = _run_main(
        ["demo", "run", "cpu_saturation", "-f", "json", "--quiet"], capsys, monkeypatch
    )
    assert code == 1
    report = Report.model_validate_json(stdout)
    assert report.meta is not None

    # Nothing persisted: the per-target list for this demo is empty.
    list_code, list_out, _ = _run_main(
        ["reports", "list", "demo:cpu_saturation", "--json"], capsys, monkeypatch
    )
    assert list_code == 0
    assert json.loads(list_out) == []


# --------------------------------------------------------------------------- #
# two --persist runs → reports diff (two distinct run_ids, empty finding delta)
# --------------------------------------------------------------------------- #


def test_demo_persist_two_runs_reports_diff_empty_finding_delta(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    xdg_home: Path,
) -> None:
    """Two ``--persist`` runs of the SAME scenario → ``reports diff`` runs and the
    finding-level delta is empty (deterministic replay → identical findings/ids),
    but they ARE two distinct run_ids (``from_inspector_results`` uses uuid4).

    Spec §场景:两次 --persist run 可经 reports diff 离线比对.
    """

    def _persist_run() -> str:
        code, stdout, stderr = _run_main(
            ["demo", "run", "cpu_saturation", "--persist", "-f", "json", "--quiet"],
            capsys,
            monkeypatch,
        )
        assert code == 1, stderr
        report = Report.model_validate_json(stdout)
        assert report.meta is not None
        assert report.meta.status == "ok"
        return report.meta.run_id

    run_a = _persist_run()
    run_b = _persist_run()
    # Distinct run_ids even under deterministic replay (uuid4 per assembly), so a
    # self-diff is meaningful rather than a no-op identity.
    assert run_a != run_b

    diff_code, diff_out, diff_err = _run_main(
        ["reports", "diff", run_a, run_b], capsys, monkeypatch
    )
    assert diff_code == 0, diff_err
    # Deterministic replay → identical findings (incl. ids) → empty finding delta.
    assert "skipped" not in diff_out
    assert "added (0)" in diff_out
    assert "resolved (0)" in diff_out


# --------------------------------------------------------------------------- #
# two --persist runs → hypothesis-level diff is empty (deterministic replay)
# --------------------------------------------------------------------------- #


def test_demo_persist_two_runs_hypothesis_diff_empty(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    xdg_home: Path,
) -> None:
    """Two ``--persist`` runs of the SAME scenario → the hypothesis-level delta is
    empty under deterministic replay.

    Deterministic cassette replay → both runs assemble hypotheses whose
    ``supporting_findings`` resolve to the SAME set of content-derived
    ``Finding.id``s → identical ``frozenset(supporting_findings)`` match keys
    (the empty delta comes from key equality, NOT from ``description`` being
    reproduced verbatim — ``description`` does not participate in matching).

    ``hypothesis_unanchored == 0`` is a HARD assertion (not "scenario
    dependent"): it relies on every demo cassette's ``correlate_findings``
    yielding hypotheses with NON-EMPTY ``supporting_findings``. If a future
    empty-support cassette is recorded, this test goes red immediately rather
    than silently drifting to a non-zero count.

    Run_ids + Reports are captured from the ``demo run -f json`` stdout (the
    existing ``test_demo_persist`` precedent — JSON stdout is allowed; only
    parsing human-readable CLI text is excluded). The diff is asserted directly
    against ``compute_diff``'s ``RegressionDiff`` fields. No real API.

    Spec §场景:同证据集两次确定性巡检的 hypothesis diff 为空 (D-7).
    """

    def _persist_run() -> Report:
        code, stdout, stderr = _run_main(
            ["demo", "run", "cpu_saturation", "--persist", "-f", "json", "--quiet"],
            capsys,
            monkeypatch,
        )
        assert code == 1, stderr
        report = Report.model_validate_json(stdout)
        assert report.meta is not None
        assert report.meta.status == "ok"
        # The cassette must produce anchored hypotheses for the empty-delta /
        # zero-unanchored assertions below to be meaningful.
        assert report.hypotheses
        assert all(h.supporting_findings for h in report.hypotheses)
        return report

    baseline = _persist_run()
    current = _persist_run()
    assert baseline.meta is not None and current.meta is not None
    # Distinct run_ids even under deterministic replay (uuid4 per assembly).
    assert baseline.meta.run_id != current.meta.run_id

    diff = compute_diff(baseline, current)
    assert diff.diff_skipped_reason is None
    # Identical evidence-set keys across the two runs → empty hypothesis delta.
    assert diff.hypothesis_added == []
    assert diff.hypothesis_resolved == []
    assert diff.hypothesis_confidence_changed == []
    assert diff.hypothesis_ambiguous_keys == 0
    # HARD 0: every demo hypothesis carries non-empty supporting_findings.
    assert diff.hypothesis_unanchored == 0


# --------------------------------------------------------------------------- #
# persist failure → exit 2 (raise branch: internal: line)
# --------------------------------------------------------------------------- #


def test_demo_persist_save_raises_exit_2_internal(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    xdg_home: Path,
) -> None:
    """``--persist`` where ``ReportStore.save`` raises → exit 2, single ``internal:``
    line, no traceback (the run was otherwise exit 1).

    Spec §场景:落盘失败升退出码 2 (raise 分支).
    """

    async def _boom(self: object, report: object) -> object:
        raise OSError("disk on fire")

    monkeypatch.setattr(ReportStore, "save", _boom)

    code, _stdout, stderr = _run_main(
        ["demo", "run", "cpu_saturation", "--persist", "--quiet"], capsys, monkeypatch
    )
    assert code == 2
    assert "internal: failed to persist report:" in stderr
    assert "Traceback" not in stderr


# --------------------------------------------------------------------------- #
# persist degrades to orphan → exit 2 (warning: line, NOT internal:)
# --------------------------------------------------------------------------- #


def test_demo_persist_orphan_degrade_exit_2_warning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    xdg_home: Path,
) -> None:
    """``--persist`` where the main store is unwritable but the save degrades to
    an orphan file (``SaveResult.stored_as_orphan=True``, no exception) → exit 2
    with a ``warning:`` line (NOT ``internal:``), report still rendered.

    Spec §场景:落盘降级 orphan 也升退出码 2. We drive the REAL ``_persist_report``
    (so its own ``warning:`` line fires) by stubbing only ``ReportStore.save`` to
    return an orphan ``SaveResult``.
    """

    async def _orphan_save(self: object, report: object) -> SaveResult:
        return SaveResult(
            run_id="demo-orphan-run",
            stored_as_orphan=True,
            orphan_path="/tmp/demo-orphan.json",
        )

    monkeypatch.setattr(ReportStore, "save", _orphan_save)

    code, stdout, stderr = _run_main(
        ["demo", "run", "cpu_saturation", "--persist", "--quiet"], capsys, monkeypatch
    )
    assert code == 2
    # The orphan path emits _persist_report's own ``warning:`` line, not internal:.
    assert "warning:" in stderr
    assert "internal:" not in stderr
    # The report still rendered to stdout.
    assert "## Findings" in stdout


# --------------------------------------------------------------------------- #
# critical finding (exit 1) + orphan → escalated to exit 2 (`in (0, 1)` predicate)
# --------------------------------------------------------------------------- #


def test_demo_persist_critical_plus_orphan_escalates_to_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    xdg_home: Path,
) -> None:
    """A critical-finding demo (exit 1) that orphans must escalate to exit 2.

    Spec §需求:落盘失败必须把退出码升到 2 — this proves the demo reuses the
    ``--intent`` Report seam ``(orphaned or persist_failed) and exit_code in (0, 1)``
    predicate, NOT the ``--inspector`` ``== 0`` variant: cpu_saturation is ok +
    critical → exit_code 1, and an orphan must still push it to 2 (a ``== 0``
    predicate would leave it at 1).
    """

    async def _orphan_save(self: object, report: object) -> SaveResult:
        return SaveResult(
            run_id="demo-orphan-run",
            stored_as_orphan=True,
            orphan_path="/tmp/demo-orphan.json",
        )

    monkeypatch.setattr(ReportStore, "save", _orphan_save)

    code, stdout, stderr = _run_main(
        ["demo", "run", "cpu_saturation", "--persist", "--quiet"], capsys, monkeypatch
    )
    # cpu_saturation is ok + critical (would be exit 1); the orphan escalates to 2.
    assert code == 2
    assert "## Findings" in stdout
    assert "Traceback" not in stderr
