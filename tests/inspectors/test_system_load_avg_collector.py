"""Offline severity tests for `linux.system.load_avg` (D-7 os-shell convention).

`ground-diagnostician-failure-analysis` group A: the load inspector now gates on
the *sustained* signals (load5 AND load15 per-core) instead of the single-sample
load1, so a one-off process burst on a single-core box no longer false-alarms.

Per the os-shell fixture convention ([[project_d7_os_shell_fixture_convention]])
these run the **real** `InspectorRunner` against a `_CaptureTarget` that answers
the `command -v` binary probes, hand-crafts the kv stdout the collector pipeline
would emit on the host for the given load state, and records the exact rendered
collect command so it can be asserted byte-for-byte (command-string lock — the
collector shell is not offline-validated, its correctness is pinned by the locked
command string + the real-host Demo Path, not by these fixtures).

The two load-bearing anchors:

  * pathology 2 (病 2): load1 high but load5/load15 low → ZERO findings (a
    transient single-sample spike is not a fault),
  * sustained overload: both load5/ncpu and load15/ncpu cross `crit_per_core` →
    a `critical` finding.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.result import InspectorResult
from hostlens.inspectors.runner import InspectorRunner
from hostlens.targets.base import Capability, ExecResult
from hostlens.targets.registry import TargetRegistry

_MANIFEST = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "hostlens"
    / "inspectors"
    / "builtin"
    / "linux"
    / "system_load_avg.yaml"
)

# The exact collect command the renderer produces from the manifest (no Jinja2
# substitution — the load_avg collect block is a static shell snippet). Locking
# it here means any drift in the collector pipeline (e.g. a swap to `uptime`)
# fails this test loudly rather than silently changing what we feed the parser.
_EXPECTED_COLLECT = (
    "read -r l1 l5 l15 _ < /proc/loadavg\n"
    "printf 'load1=%s\\nload5=%s\\nload15=%s\\nncpu=%s\\n' "
    '"$l1" "$l5" "$l15" "$(nproc)"'
)

_PROBE_PREFIX = "command -v "


class _CaptureTarget:
    """Offline target: answers binary probes, returns canned kv stdout for the
    main collect command, and records every rendered command into `commands`.

    `command -v <bin>` probes succeed with a synthetic path (so the capability
    preflight passes for the `[awk, nproc]` requires_binaries); everything else
    is the inspector's main command and returns `main_stdout` (the kv lines the
    collector would print on a host in the authored load state).
    """

    type = "local"

    def __init__(self, name: str, *, main_stdout: str) -> None:
        self.name = name
        self.capabilities: set[Capability] = {Capability.SHELL}
        self._main_stdout = main_stdout
        self.commands: list[str] = []

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        del timeout, env
        self.commands.append(cmd)
        if cmd.startswith(_PROBE_PREFIX):
            binary = cmd[len(_PROBE_PREFIX) :].strip().strip("'\"")
            return ExecResult(
                exit_code=0,
                stdout=f"/usr/bin/{binary}\n",
                stderr="",
                duration_seconds=0.0,
                timed_out=False,
            )
        return ExecResult(
            exit_code=0,
            stdout=self._main_stdout,
            stderr="",
            duration_seconds=0.0,
            timed_out=False,
        )

    async def read_file(self, path: str) -> bytes:  # pragma: no cover - unused
        raise AssertionError(f"_CaptureTarget.read_file unexpectedly called: {path!r}")


def _logger() -> Any:
    return structlog.get_logger("test-load-avg")


def _kv_stdout(*, load1: str, load5: str, load15: str, ncpu: str) -> str:
    """The kv lines the collect command prints for the given load state."""
    return f"load1={load1}\nload5={load5}\nload15={load15}\nncpu={ncpu}\n"


async def _run(main_stdout: str) -> tuple[_CaptureTarget, InspectorResult]:
    manifest = load_manifest(_MANIFEST)
    runner = InspectorRunner(TargetRegistry(), settings=Settings(), logger=_logger())
    target = _CaptureTarget("load-host", main_stdout=main_stdout)
    result = await runner.run(manifest, target)
    return target, result


def _assert_collect_command_locked(target: _CaptureTarget) -> None:
    """The rendered main collect command must match the manifest byte-for-byte
    (command-string lock — probes are filtered out)."""
    main_cmds = [c for c in target.commands if not c.startswith(_PROBE_PREFIX)]
    assert main_cmds == [_EXPECTED_COLLECT], main_cmds


# --------------------------------------------------------------------------- #
# 病 2 anchor: a single-core load1 spike with low load5/load15 → zero findings
# --------------------------------------------------------------------------- #


async def test_load1_spike_with_low_sustained_load_yields_no_finding() -> None:
    # Single-core box: load1 momentarily hit 2.45 (a process burst) but the
    # sustained 5-/15-min averages are idle (0.41 / 0.33). load15/ncpu == 0.33
    # < warn_per_core (1.0), so NOTHING fires — the report is not warned/critical.
    target, result = await _run(_kv_stdout(load1="2.45", load5="0.41", load15="0.33", ncpu="1"))

    _assert_collect_command_locked(target)
    assert result.status == "ok"
    assert result.output == {
        "load1": "2.45",
        "load5": "0.41",
        "load15": "0.33",
        "ncpu": "1",
    }
    assert result.findings == []


# --------------------------------------------------------------------------- #
# Sustained-overload anchor: both load5/ncpu and load15/ncpu >= crit → critical
# --------------------------------------------------------------------------- #


async def test_sustained_double_gate_overload_is_critical() -> None:
    # 4-core box: load5/ncpu == 12.10/4 == 3.025 and load15/ncpu == 9.20/4 ==
    # 2.30 are BOTH >= crit_per_core (2.0) → critical. load1 is irrelevant.
    target, result = await _run(_kv_stdout(load1="6.00", load5="12.10", load15="9.20", ncpu="4"))

    _assert_collect_command_locked(target)
    assert result.status == "ok"
    assert [(f.severity, f.message) for f in result.findings] == [
        (
            "critical",
            "sustained load (5-min 12.10, 15-min 9.20) is critically high for 4 cores",
        ),
    ]


async def test_sustained_warn_band_is_warning_not_critical() -> None:
    # Both per-core ratios in [warn_per_core, crit_per_core): load5/ncpu ==
    # 5.20/4 == 1.30 and load15/ncpu == 4.80/4 == 1.20 → warning (not critical).
    target, result = await _run(_kv_stdout(load1="3.00", load5="5.20", load15="4.80", ncpu="4"))

    _assert_collect_command_locked(target)
    assert result.status == "ok"
    assert [(f.severity, f.message) for f in result.findings] == [
        (
            "warning",
            "sustained load (5-min 5.20, 15-min 4.80) exceeds the 4 available cores",
        ),
    ]


async def test_load5_recovered_load15_tail_is_not_critical() -> None:
    # AND-gate guard: load15/ncpu still high (8.00/4 == 2.0 >= crit) but
    # load5/ncpu has fallen back (3.60/4 == 0.90 < warn_per_core) → the box is
    # recovering, so NEITHER gate's AND condition holds → zero findings.
    target, result = await _run(_kv_stdout(load1="2.00", load5="3.60", load15="8.00", ncpu="4"))

    _assert_collect_command_locked(target)
    assert result.status == "ok"
    assert result.findings == []
