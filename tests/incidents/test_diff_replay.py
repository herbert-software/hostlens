"""Offline, Agent-free regression-diff replay (add-report-persistence-and-diff §7.3).

Covers spec ``report-regression-diff`` §需求:diff 必须可离线确定性验证:

- §场景:同输入两次机械巡检 diff 为空 — the deterministic ``hello.echo``
  inspector run twice produces two Reports whose findings fingerprint
  identically, so ``compute_diff`` reports no regression.
- §场景:不同严重度场景 diff 出 added critical — a baseline with no critical
  finding (``hello.echo``) vs a current assembled from the ``memory_oom``
  fixture (``linux.memory.pressure`` + ``linux.kernel.oom_killer`` → two
  ``critical`` findings) yields those criticals in ``compute_diff(...).added``.

The whole path is mechanical: ``InspectorRunner.run`` over a ``ReplayTarget``
(canned fixture output) → ``Report.from_inspector_results`` → ``compute_diff``.
It deliberately does **not** go through the ``PlannerAgent`` / ``_harness``
double-replay pipeline — no LLM, no cassette, no SSH, no API quota.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import structlog

import hostlens.inspectors as _inspectors_pkg
from hostlens.core.config import Settings
from hostlens.demo.assets import source_tree_path
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.result import InspectorResult
from hostlens.inspectors.runner import InspectorRunner
from hostlens.reporting.diff import compute_diff
from hostlens.reporting.models import Report
from hostlens.targets.registry import TargetRegistry
from hostlens.targets.replay import ReplayTarget

# Memory-OOM inspectors produce two `critical` findings against the
# committed `memory_oom` fixture (see `tests/incidents/snapshots/memory_oom.md`).
_MEMORY_OOM_INSPECTORS = (
    "linux/memory_pressure.yaml",
    "linux/kernel_oom_killer.yaml",
)

_REPLAY_TARGET_NAME = "diff-replay-host"

_STARTED_AT = datetime(2026, 1, 2, 12, 0, 0, tzinfo=UTC)
_FINISHED_AT = datetime(2026, 1, 2, 12, 0, 1, tzinfo=UTC)

# A self-contained `hello.echo` fixture: the binary probe (`command -v echo`)
# plus the deterministic `echo hello` collect command. `hello.echo` yields a
# single `info` finding — a clean no-critical baseline.
_HELLO_FIXTURE = {
    "impersonate": "local",
    "capabilities": ["shell"],
    "commands": [
        {"cmd": "command -v echo", "stdout": "/usr/bin/echo\n", "exit_code": 0},
        {"cmd": "echo hello", "stdout": "hello\n", "exit_code": 0},
    ],
    "files": {},
}


def _builtin_root() -> Path:
    pkg_file = _inspectors_pkg.__file__
    assert pkg_file is not None
    return Path(pkg_file).parent / "builtin"


def _runner() -> InspectorRunner:
    return InspectorRunner(
        TargetRegistry(),
        settings=Settings(),
        logger=structlog.get_logger("diff-replay-test"),
    )


async def _run_hello_echo(fixture_path: Path) -> InspectorResult:
    manifest = load_manifest(_builtin_root() / "hello" / "echo.yaml")
    target = ReplayTarget(_REPLAY_TARGET_NAME, fixture=fixture_path)
    result = await _runner().run(manifest, target)
    assert result.status == "ok", result.error
    assert target.misses == [], target.misses
    return result


async def _run_memory_oom() -> list[InspectorResult]:
    fixture_path = source_tree_path("memory_oom", "fixture")
    runner = _runner()
    results: list[InspectorResult] = []
    for relpath in _MEMORY_OOM_INSPECTORS:
        manifest = load_manifest(_builtin_root() / relpath)
        target = ReplayTarget(_REPLAY_TARGET_NAME, fixture=fixture_path)
        result = await runner.run(manifest, target)
        assert result.status == "ok", f"{relpath}: {result.error}"
        assert target.misses == [], f"{relpath}: {target.misses}"
        results.append(result)
    return results


def _assemble(results: list[InspectorResult]) -> Report:
    return Report.from_inspector_results(
        _REPLAY_TARGET_NAME,
        results,
        started_at=_STARTED_AT,
        finished_at=_FINISHED_AT,
        target_id="host-diff-replay",
    )


# --------------------------------------------------------------------------- #
# §场景: 同输入两次机械巡检 diff 为空
# --------------------------------------------------------------------------- #


async def test_same_input_twice_yields_empty_diff(tmp_path: Path) -> None:
    """Two deterministic ``hello.echo`` runs → identical fingerprints → empty diff.

    ``report_id`` / ``timestamp`` differ between the two Reports, but the diff
    keys off ``Finding.id`` (inspector_name + version + message), so the
    comparison must report no added / resolved / changed_severity.
    """
    fixture_path = tmp_path / "hello_fixture.json"
    fixture_path.write_text(json.dumps(_HELLO_FIXTURE), encoding="utf-8")

    baseline = _assemble([await _run_hello_echo(fixture_path)])
    current = _assemble([await _run_hello_echo(fixture_path)])

    # Two independent runs really did mint distinct Reports.
    assert baseline.report_id != current.report_id

    diff = compute_diff(baseline, current)

    assert diff.diff_skipped_reason is None
    assert diff.added == []
    assert diff.resolved == []
    assert diff.changed_severity == []


# --------------------------------------------------------------------------- #
# §场景: 不同严重度场景 diff 出 added critical [集成测试, 机械组装]
# --------------------------------------------------------------------------- #


async def test_added_critical_between_baseline_and_oom(tmp_path: Path) -> None:
    """Baseline (no critical) vs current (memory_oom → critical) → added critical.

    The baseline is a single ``hello.echo`` ``info`` finding; the current report
    is assembled from the two memory-OOM inspectors that produce ``critical``
    findings against the committed fixture. ``compute_diff(...).added`` must
    carry those critical fingerprints.
    """
    fixture_path = tmp_path / "hello_fixture.json"
    fixture_path.write_text(json.dumps(_HELLO_FIXTURE), encoding="utf-8")

    baseline = _assemble([await _run_hello_echo(fixture_path)])
    current = _assemble(await _run_memory_oom())

    # Sanity: baseline carries no critical, current carries at least one.
    assert all(f.severity != "critical" for f in baseline.findings)
    current_criticals = [f for f in current.findings if f.severity == "critical"]
    assert current_criticals, "memory_oom fixture produced no critical finding"

    diff = compute_diff(baseline, current)

    assert diff.diff_skipped_reason is None
    # Every current critical finding's fingerprint id is in `added`.
    added_ids = {fp.id for fp in diff.added}
    for critical in current_criticals:
        assert critical.id is not None
        assert critical.id in added_ids, f"missing added critical: {critical.message}"
    # At least one `added` entry is severity critical.
    assert any(fp.severity == "critical" for fp in diff.added)
    # The baseline `info` finding is gone in current → resolved.
    assert any(fp.severity == "info" for fp in diff.resolved)
