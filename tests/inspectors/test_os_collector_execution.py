"""Collector-execution tests — STRONG attestation anchor for wave-1 inspectors.

Unlike `test_os_*.py` (which replay `ReplayTarget` fixtures whose stdout is the
collector's *final* JSON, never running the shell), these drive the **real**
`InspectorRunner` against a `ShimShellTarget` that executes each rendered
`collect.command` through a real `/bin/sh` with only the *data-source* commands
shimmed (serving author-controlled RAW input) and the *text tools* (awk / jq /
sort / …) REAL. So the collector's awk/jq derivation actually runs against known
raw input and we assert the independently-reasoned expected output + findings.

Each case is `(raw input → expected output)`: a wrong awk field index, a
mis-ordered decode table, a broken jq path, or a missing fail-loud guard yields
a mismatch the snapshot suite cannot see (its canned stdout bypasses the shell).

See `_shim_exec.py` for the harness. New inspectors register a `_Case` below.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import structlog

from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.runner import InspectorRunner
from hostlens.targets.registry import TargetRegistry

from ._shim_exec import ShimShellTarget, build_shim_env

_BUILTIN = Path(__file__).resolve().parents[2] / "src" / "hostlens" / "inspectors" / "builtin"


@dataclass(frozen=True)
class _Case:
    inspector: str  # manifest path relative to builtin/, e.g. "linux/kernel_taint.yaml"
    commands: list[str]  # data-source command names to shim (text tools stay real)
    responses: dict[str, tuple[str, int] | list[tuple[str, int]]]  # exact or sequence
    expected_output: dict[str, Any]
    expected_findings: list[tuple[str, str]]  # (severity, message), [] = none expected
    params: dict[str, Any] = field(default_factory=dict)
    case_id: str = ""
    # When set, PATH is isolated to bin_dir + curated real tools MINUS these
    # names, so `command -v <name>` fails — forces disk_smart's no-jq awk branch.
    omit_real: frozenset[str] = frozenset()


_CASES: tuple[_Case, ...] = (
    # ---- linux.kernel.taint: 4097 = bit0 (proprietary) + bit12 (OOT) -------- #
    # Catches the round-1 decode-table shift: a buggy `names` list yields
    # "...unsigned-module" instead of "...out-of-tree-module".
    _Case(
        inspector="linux/kernel_taint.yaml",
        case_id="kernel_taint_4097",
        commands=["cat"],
        responses={"cat /proc/sys/kernel/tainted": ("4097\n", 0)},
        expected_output={
            "tainted": 4097,
            "flags": "proprietary-module-loaded,out-of-tree-module",
        },
        expected_findings=[
            (
                "warning",
                "Kernel is tainted (tainted=4097): proprietary-module-loaded,out-of-tree-module",
            ),
        ],
    ),
    # ---- linux.memory.swap: 95.0% used drives the critical finding --------- #
    # Catches a wrong /proc/meminfo field index or division error.
    _Case(
        inspector="linux/memory_swap.yaml",
        case_id="memory_swap_95pct",
        commands=["cat"],
        responses={
            "cat /proc/meminfo": (
                "MemTotal:       16384000 kB\nSwapTotal:       8388608 kB\nSwapFree:         419430 kB\n",
                0,
            ),
            "cat /proc/sys/vm/swappiness": ("60\n", 0),
        },
        expected_output={
            "swap_total_kb": 8388608,
            "swap_free_kb": 419430,
            "swappiness": 60,
            "swap_used_pct": "95.0",
        },
        expected_findings=[
            ("critical", "Swap nearly exhausted at 95.0% used (swappiness=60)"),
        ],
    ),
    # ---- net.connections: state tally over ss -tan ------------------------ #
    # Catches a wrong $1 state-classification or NR>1 total miscount.
    _Case(
        inspector="net/connections.yaml",
        case_id="connections_tally",
        commands=["ss"],
        responses={
            "ss -tan": (
                "State Recv-Q Send-Q Local Peer\n"
                "ESTAB 0 0 a b\nESTAB 0 0 a b\nTIME-WAIT 0 0 a b\n"
                "CLOSE-WAIT 0 0 a b\nCLOSE-WAIT 0 0 a b\nCLOSE-WAIT 0 0 a b\nLISTEN 0 0 a b\n",
                0,
            )
        },
        expected_output={
            "total": 7,
            "established": 2,
            "time_wait": 1,
            "close_wait": 3,
            "syn_sent": 0,
            "syn_recv": 0,
            "fin_wait": 0,
            "listen": 1,
        },
        expected_findings=[],  # all counts below thresholds
    ),
    # ---- linux.disk.smart NO-JQ branch: forces the awk fallback ------------ #
    # The test host has jq, so the other disk_smart case takes the jq branch.
    # `omit_real={"jq"}` isolates PATH so `command -v jq` FAILS → the collector's
    # awk fallback (`tr '\n' ' ' | awk match(smart_status…passed)`) runs. This is
    # the round-5 fix's own branch; without this it would be unanchored. smartctl
    # emits pretty JSON with a self-test `passed:true` BEFORE smart_status's
    # `passed:false` — the scoped-ERE must still report FAILED (not the earlier
    # true), proving the fallback is format-agnostic and brace-scoped.
    _Case(
        inspector="linux/disk_smart.yaml",
        case_id="disk_smart_failed_no_jq",
        commands=["ls", "cat", "smartctl"],
        omit_real=frozenset({"jq"}),
        responses={
            "ls /sys/block": ("sda\n", 0),
            "cat /sys/block/sda/queue/rotational": ("1\n", 0),
            "cat /sys/block/sda/size": ("2097152\n", 0),
            "smartctl": (
                '{\n  "ata_smart_self_test_log": { "passed": true },\n'
                '  "smart_status": { "passed": false }\n}\n',
                0,
            ),
        },
        expected_output={
            "results": [
                {"device": "sda", "rotational": 1, "size_gib": "1.0", "smart_health": "FAILED"},
            ],
        },
        expected_findings=[
            ("critical", "Disk sda reports SMART overall-health FAILED (1.0GiB)"),
        ],
    ),
    # ---- linux.systemd.timer_status FIRED-age branch (shimmed clock) ------- #
    # Anchors the jq `($now - last_trigger)/1e6 | floor` age path: `date` is
    # shimmed to a fixed epoch so the derived age is deterministic. A fired timer
    # (last_trigger>0) with no next elapse, last fired 90000s ago (> the 86400
    # default), drives the "no next elapse + stale" warning.
    _Case(
        inspector="linux/systemd_timer_status.yaml",
        case_id="systemd_timer_fired_stale",
        commands=["systemctl", "date"],
        responses={
            "systemctl": (
                '[{"unit":"backup.timer","next_elapse_realtime":0,'
                '"last_trigger_usec":1699910000000000}]\n',
                0,
            ),
            "date": ("1700000000\n", 0),  # now_us = 1700000000 * 1e6; age = 90000s
        },
        expected_output={
            "results": [
                {
                    "unit": "backup.timer",
                    "next_elapse_usec": 0,
                    "last_trigger_usec": 1699910000000000,
                    "last_trigger_age_sec": 90000,
                }
            ],
        },
        expected_findings=[
            (
                "warning",
                "systemd timer backup.timer has no next elapse scheduled and last fired 90000s ago",
            ),
        ],
    ),
)


def _logger() -> structlog.stdlib.BoundLogger:
    return structlog.get_logger("collector-exec-test")  # type: ignore[no-any-return]


@pytest.mark.parametrize("case", _CASES, ids=[c.case_id for c in _CASES])
async def test_collector_executes_against_raw_input(case: _Case, tmp_path: Path) -> None:
    bin_dir, data_dir = build_shim_env(tmp_path, commands=case.commands, responses=case.responses)
    manifest = load_manifest(_BUILTIN / case.inspector)
    target = ShimShellTarget(
        case.case_id.replace("_", "-"),
        bin_dir=bin_dir,
        data_dir=data_dir,
        omit_real=case.omit_real,
    )
    runner = InspectorRunner(TargetRegistry(), settings=Settings(), logger=_logger())

    result = await runner.run(manifest, target, case.params)

    # The real awk/jq derivation ran against the raw fixture — assert it derived
    # the expected output and findings (not a canned bypass).
    assert result.status == "ok", f"{case.case_id}: status={result.status} error={result.error}"
    assert result.output == case.expected_output
    assert [(f.severity, f.message) for f in result.findings] == case.expected_findings
