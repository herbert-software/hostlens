"""Collector-execution tests (part A) — STRONG attestation for wave-1 inspectors.

Companion to `test_os_collector_execution.py`: same harness (`ShimShellTarget`
drives the real `InspectorRunner` against rendered `collect.command` through a
real `/bin/sh`, with only the *data-source* commands shimmed to serve
author-controlled RAW input while the *text tools* — awk / jq / sort / head /
cut / grep / wc / tr / sed — stay REAL). Each `_Case` feeds known raw input and
asserts the independently-reasoned derived output + findings, so a wrong awk
field index / jq path / decode error yields a mismatch the snapshot suite (which
replays the collector's final JSON, never the shell) cannot detect.

This module covers 10 inspectors: cpu_throttling / cpu_cpufreq /
memory_hugepages / disk_io / disk_smart / fs_mount_health / fs_logrotate /
listening_ports / dns_resolve / ntp_drift.
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
    inspector: str  # manifest path relative to builtin/, e.g. "linux/cpu_throttling.yaml"
    commands: list[str]  # data-source command names to shim (text tools stay real)
    responses: dict[str, tuple[str, int] | list[tuple[str, int]]]  # exact or sequence
    expected_output: dict[str, Any]
    expected_findings: list[tuple[str, str]]  # (severity, message), [] = none expected
    params: dict[str, Any] = field(default_factory=dict)
    case_id: str = ""


_CASES: tuple[_Case, ...] = (
    # ---- linux.cpu.throttling: 300/1000 periods = 30.00% → critical -------- #
    # awk derives throttled_pct = t/p*100 ("%.2f"); a wrong $2 field index or a
    # swapped nr_periods/nr_throttled match would shift the ratio.
    _Case(
        inspector="linux/cpu_throttling.yaml",
        case_id="cpu_throttling_30pct",
        commands=["cat"],
        responses={
            "cat /sys/fs/cgroup/cpu.stat": (
                "usage_usec 123456789\n"
                "user_usec 80000000\n"
                "system_usec 40000000\n"
                "nr_periods 1000\n"
                "nr_throttled 300\n"
                "throttled_usec 12345678\n",
                0,
            ),
        },
        expected_output={
            "nr_periods": 1000,
            "nr_throttled": 300,
            "throttled_usec": 12345678,
            "throttled_pct": "30.00",
        },
        expected_findings=[
            ("critical", "CPU throttled in 30.00% of periods (300/1000)"),
        ],
    ),
    # ---- linux.cpu.cpufreq: powersave governor at exactly 40.0% ------------ #
    # cur/max = 800000/2000000 = 40.0% ("%.1f"). 40.0 is NOT < 40.0 so only the
    # governar=='powersave' finding fires, not the freq-capped one. Catches a
    # wrong NR-indexed field grab or a cur/max swap.
    _Case(
        inspector="linux/cpu_cpufreq.yaml",
        case_id="cpu_cpufreq_powersave",
        commands=["cat"],
        responses={
            "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor": ("powersave\n", 0),
            "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq": ("800000\n", 0),
            "cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq": ("2000000\n", 0),
        },
        expected_output={
            "governor": "powersave",
            "cur_freq_khz": 800000,
            "max_freq_khz": 2000000,
            "freq_pct": "40.0",
        },
        expected_findings=[
            ("warning", "CPU scaling governor is 'powersave' (cpu0 at 40.0% of max clock)"),
        ],
    ),
    # ---- linux.memory.hugepages: 1024-page pool 100% idle → warning -------- #
    # free/total = 1024/1024 = 100.0% ("%.1f") → reserved-but-idle warning. The
    # free==0 finding does NOT fire (free=1024). Catches a wrong meminfo field
    # index ($2 is the kB value) or a total/free swap.
    _Case(
        inspector="linux/memory_hugepages.yaml",
        case_id="hugepages_idle_pool",
        commands=["cat"],
        responses={
            "cat /proc/meminfo": (
                "MemTotal:       16384000 kB\n"
                "HugePages_Total:    1024\n"
                "HugePages_Free:     1024\n"
                "HugePages_Rsvd:        0\n"
                "Hugepagesize:       2048 kB\n",
                0,
            ),
        },
        expected_output={
            "hugepages_total": 1024,
            "hugepages_free": 1024,
            "hugepagesize_kb": 2048,
            "hugepages_free_pct": "100.0",
        },
        expected_findings=[
            ("warning", "HugePages pool 1024 pages reserved but 100.0% idle (free)"),
        ],
    ),
    # ---- linux.disk.io: two DIFFERING diskstats reads → util 95% critical -- #
    # design D-3: the collector reads /proc/diskstats twice via the SAME command
    # `cat /proc/diskstats` (read → sleep → read). The shim serves a SEQUENCE
    # (`.1` then `.2`) so the two reads differ and real counter deltas are
    # exercised. awk fields: $4=reads $8=writes $13=io_ticks(ms); interval=1 →
    # ms=1000. Deltas: io_ticks 5000→5950 = 950 → util=950/1000*100=95.0;
    # reads 1000→1100 (+100), writes 2000→2050 (+50) → ops=150;
    # await=950/150=6.33ms. loop0 is filtered. A wrong io_ticks field index, a
    # bad delta, or a missing clamp would now surface as a mismatch (the
    # identical-snapshot variant could not).
    _Case(
        inspector="linux/disk_io.yaml",
        case_id="disk_io_saturated",
        commands=["cat"],
        responses={
            "cat /proc/diskstats": [
                (
                    "   8       0 sda 1000 0 0 0 2000 0 0 0 0 5000 0\n"
                    "   7       0 loop0 0 0 0 0 0 0 0 0 0 0 0\n",
                    0,
                ),
                (
                    "   8       0 sda 1100 0 0 0 2050 0 0 0 0 5950 0\n"
                    "   7       0 loop0 0 0 0 0 0 0 0 0 0 0 0\n",
                    0,
                ),
            ],
        },
        expected_output={
            "results": [
                {"device": "sda", "util_pct": "95.0", "await_ms": "6.33", "ops": 150},
            ],
        },
        expected_findings=[
            ("critical", "Disk sda saturated at 95.0% IO utilisation (await=6.33ms)"),
        ],
    ),
    # ---- linux.disk.smart: smartctl FAILED bit → critical ----------------- #
    # ls /sys/block → "sda"; rotational=1; size=2097152 sectors → 1.0 GiB
    # (sectors*512/1073741824); smartctl -H --json returns passed:false → REAL
    # jq parses it → smart_health=FAILED. Catches a wrong jq ladder (`false //
    # empty` would collapse the FAILED bit) or a wrong GiB divisor.
    _Case(
        inspector="linux/disk_smart.yaml",
        case_id="disk_smart_failed",
        commands=["ls", "cat", "smartctl"],
        responses={
            "ls /sys/block": ("sda\n", 0),
            "cat /sys/block/sda/queue/rotational": ("1\n", 0),
            "cat /sys/block/sda/size": ("2097152\n", 0),
            # name-only fallback key: smartctl args vary (/dev/sda) — serve the
            # FAILED health JSON for any smartctl invocation in this scenario.
            "smartctl": ('{"smart_status":{"passed":false}}\n', 0),
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
    # ---- linux.fs.mount_health: ext4 root mounted read-only → critical ----- #
    # findmnt --json tree: jq walks (..|.filesystems?), drops pseudo fstypes,
    # and derives read_only from the OPTIONS string ("ro" member). The REAL jq
    # must split the comma options and detect `ro`. A tmpfs child is filtered.
    _Case(
        inspector="linux/fs_mount_health.yaml",
        case_id="mount_readonly_root",
        commands=["findmnt"],
        responses={
            # name-only fallback key: findmnt args (-o TARGET,...) are fixed but
            # serving under the bare name is simplest and unambiguous here.
            "findmnt": (
                '{"filesystems":['
                '{"target":"/","source":"/dev/sda1","fstype":"ext4","options":"ro,relatime",'
                '"children":['
                '{"target":"/run","source":"tmpfs","fstype":"tmpfs","options":"rw,nosuid"}'
                "]}"
                "]}\n",
                0,
            ),
        },
        expected_output={
            "results": [
                {
                    "target": "/",
                    "source": "/dev/sda1",
                    "fstype": "ext4",
                    "read_only": True,
                },
            ],
        },
        expected_findings=[
            ("critical", "Filesystem / (/dev/sda1, ext4) is mounted read-only"),
        ],
    ),
    # ---- linux.fs.logrotate: newest rotation 2024-01-01, fixed "now" ------- #
    # awk finds the newest YYYY-M-D over the status lines (lexical max of a
    # zero-padded key), GNU `date -d` → epoch, `date +%s` → now. The two date
    # calls take DIFFERENT args, so we use EXACT keys to give distinct epochs:
    #   date -d 2024-01-01 +%s  → 1704067200 (2024-01-01T00:00:00Z)
    #   date +%s                → 1705276800 (2024-01-15T00:00:00Z)
    # delta = (1705276800-1704067200)/86400 = 1209600/86400 = 14.0 → critical.
    # tracked_files = 2 (two `"path` lines). Catches a wrong newest-pick (awk
    # would otherwise emit the older 2023-12-20 date) or a day-divisor error.
    _Case(
        inspector="linux/fs_logrotate.yaml",
        case_id="logrotate_stalled",
        commands=["cat", "date"],
        responses={
            "cat /var/lib/logrotate/status": (
                "logrotate state -- version 2\n"
                '"/var/log/syslog" 2023-12-20-0:0:0\n'
                '"/var/log/auth.log" 2024-1-1-0:0:0\n',
                0,
            ),
            # newest token normalises to "2024-1-1"; awk's cut -f2 yields the
            # un-padded "%d-%d-%d" form, which is what `date -d` receives.
            "date -d 2024-1-1 +%s": ("1704067200\n", 0),
            "date +%s": ("1705276800\n", 0),
        },
        expected_output={
            "days_since_last": "14.0",
            "last_date": "2024-1-1",
            "tracked_files": 2,
        },
        expected_findings=[
            (
                "critical",
                "logrotate stalled: last rotation 14.0 days ago (2024-1-1, 2 files tracked)",
            ),
        ],
    ),
    # ---- net.listening_ports: wildcard :8080 not in allowed → warning ------ #
    # awk splits the Local Address:Port column ($4) on the last colon, detects
    # the 0.0.0.0 wildcard bind, and pulls the process name out of the
    # users:(("name",pid=...)) field. The loopback 127.0.0.1:631 listener is
    # wildcard=false → never flagged. Catches a wrong $4 split or a bad
    # users:(( regex.
    _Case(
        inspector="net/listening_ports.yaml",
        case_id="listening_wildcard_port",
        commands=["ss"],
        responses={
            "ss -tlnp": (
                "State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
                'LISTEN 0      128    0.0.0.0:8080      0.0.0.0:*    users:(("python",pid=42,fd=3))\n'
                'LISTEN 0      128    127.0.0.1:631     0.0.0.0:*    users:(("cupsd",pid=99,fd=7))\n',
                0,
            ),
        },
        expected_output={
            "results": [
                {"address": "0.0.0.0", "port": 8080, "wildcard": True, "process": "python"},
                {"address": "127.0.0.1", "port": 631, "wildcard": False, "process": "cupsd"},
            ],
        },
        expected_findings=[
            ("warning", "Unexpected public listener on port 8080 (0.0.0.0, process=python)"),
        ],
    ),
    # ---- net.dns.resolve: name fails to resolve → critical ----------------- #
    # The collector loops `dig +short A <name>`, pipes through REAL grep (IPv4
    # regex) + head. An empty dig answer → resolved:false. params drive the loop.
    # Catches a broken loop / wrong grep regex (a non-IP line must be rejected).
    _Case(
        inspector="net/dns_resolve.yaml",
        case_id="dns_unresolved",
        commands=["dig"],
        params={"names": ["example.com"]},
        responses={
            # name-only fallback: dig args vary per name — serve empty (NXDOMAIN /
            # no A record) so grep yields nothing → resolved:false.
            "dig": ("\n", 0),
        },
        expected_output={
            "results": [
                {"name": "example.com", "resolved": False, "address": ""},
            ],
        },
        expected_findings=[
            ("critical", "DNS name example.com failed to resolve (no A record)"),
        ],
    ),
    # ---- net.ntp.drift: +1.5s offset → critical ---------------------------- #
    # awk grabs $4 of the "Last offset" line (the signed value), derives the
    # absolute value, reads Leap status verbatim, and flags Reference ID as
    # synced (no 00000000). offset 1.5 → abs 1.5 >= 1.0 → critical. Catches a
    # wrong $4 field index or a broken abs() branch.
    _Case(
        inspector="net/ntp_drift.yaml",
        case_id="ntp_drift_critical",
        commands=["chronyc"],
        responses={
            # name-only fallback: chronyc tracking (single call).
            "chronyc": (
                "Reference ID    : C0248F82 (time.example.com)\n"
                "Stratum         : 2\n"
                "Ref time (UTC)  : Thu Jun 05 00:00:00 2026\n"
                "System time     : 0.000000123 seconds slow of NTP time\n"
                "Last offset     : +1.500000000 seconds\n"
                "RMS offset      : 0.000500000 seconds\n"
                "Frequency       : 1.234 ppm slow\n"
                "Residual freq   : +0.000 ppm\n"
                "Skew            : 0.100 ppm\n"
                "Root delay      : 0.010000000 seconds\n"
                "Root dispersion : 0.001000000 seconds\n"
                "Update interval : 64.0 seconds\n"
                "Leap status     : Normal\n",
                0,
            ),
        },
        expected_output={
            "offset_seconds": 1.5,
            "abs_offset_seconds": 1.5,
            "leap_status": "Normal",
            "synced": True,
        },
        expected_findings=[
            ("critical", "Clock drift 1.5s exceeds 1s (leap status: Normal)"),
        ],
    ),
)


def _logger() -> structlog.stdlib.BoundLogger:
    return structlog.get_logger("collector-exec-test")  # type: ignore[no-any-return]


@pytest.mark.parametrize("case", _CASES, ids=[c.case_id for c in _CASES])
async def test_collector_executes_against_raw_input(case: _Case, tmp_path: Path) -> None:
    bin_dir, data_dir = build_shim_env(tmp_path, commands=case.commands, responses=case.responses)
    manifest = load_manifest(_BUILTIN / case.inspector)
    target = ShimShellTarget(case.case_id.replace("_", "-"), bin_dir=bin_dir, data_dir=data_dir)
    runner = InspectorRunner(TargetRegistry(), settings=Settings(), logger=_logger())

    result = await runner.run(manifest, target, case.params)

    assert result.status == "ok", f"{case.case_id}: status={result.status} error={result.error}"
    assert result.output == case.expected_output
    assert [(f.severity, f.message) for f in result.findings] == case.expected_findings
