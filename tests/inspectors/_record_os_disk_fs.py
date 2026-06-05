"""One-shot fixture recorder for the wave-1 disk + filesystem inspectors.

Group 2 of `add-os-shell-inspectors-wave1`: `linux.disk.io`,
`linux.disk.smart`, `linux.fs.mount_health`, `linux.fs.logrotate`. These OS
probes are Linux-only (they read /proc, /sys, /var/lib/logrotate and use GNU
`findmnt` / `date -d`) so we do NOT need a real Linux host — we reuse the pilot
`_CaptureTarget` pattern established by `_record_os_compute_memory.py`: drive
the **real** `InspectorRunner` against a target that

  * answers `command -v X` binary probes with a synthetic path,
  * answers `[ -r P ]` file probes empty, and
  * returns a hand-crafted `main_stdout` for the rendered collect command,

while recording every exact rendered command into a sink. Because the command
strings are captured verbatim from the real renderer (never hand-written), the
fixture can never drift from what `ReplayTarget` will look up at snapshot time
(byte-level match, Authoring Contract / design D-7).

`linux.disk.io` differences /proc/diskstats counters by sampling twice with an
in-command `sleep` (design D-3). `_CaptureTarget.exec` never actually executes
the shell — it returns the canned `main_stdout` for the rendered command — so
the `sleep` does not run during recording; the crafted stdout is simply the
post-difference JSON the awk pipeline would emit for the scenario.

Each scenario asserts (generation sanity) that the crafted stdout drives the
inspector to `status=ok`; abnormal scenarios further assert at least one finding
fired so we never commit a no-op fixture.

Run it to (re)write the fixtures:

    .venv-impl/bin/python tests/inspectors/_record_os_disk_fs.py

NOT collected by pytest (no `test_` prefix).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.runner import InspectorRunner
from hostlens.targets.base import Capability, ExecResult
from hostlens.targets.registry import TargetRegistry

_BUILTIN_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "hostlens" / "inspectors" / "builtin" / "linux"
)
_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "os_disk_fs"

_PROBE_PREFIX = "command -v "
_FILE_PROBE_PREFIX = "[ -r "


class _CaptureTarget:
    """Generation-only target: returns canned stdout and records every command.

    Binary probes (``command -v X``) succeed with a synthetic path; file
    probes (``[ -r P ]``) succeed empty; everything else is the inspector's
    main command and returns ``main_stdout``. Each call is appended to ``sink``
    so the fixture captures the exact rendered command strings.
    """

    type = "local"

    def __init__(
        self,
        name: str,
        *,
        capabilities: set[Capability],
        main_stdout: str,
        sink: list[dict[str, Any]],
    ) -> None:
        self.name = name
        self.capabilities = capabilities
        self._main_stdout = main_stdout
        self._sink = sink

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        del timeout, env
        if cmd.startswith(_PROBE_PREFIX):
            binary = cmd[len(_PROBE_PREFIX) :].strip().strip("'\"")
            stdout = f"/usr/bin/{binary}\n"
        elif cmd.startswith(_FILE_PROBE_PREFIX):
            stdout = ""
        else:
            stdout = self._main_stdout
        self._sink.append(
            {"cmd": cmd, "stdout": stdout, "stderr": "", "exit_code": 0, "duration_seconds": 0.0}
        )
        return ExecResult(
            exit_code=0, stdout=stdout, stderr="", duration_seconds=0.0, timed_out=False
        )

    async def read_file(self, path: str) -> bytes:  # pragma: no cover - unused here
        raise AssertionError(f"_CaptureTarget.read_file unexpectedly called: {path!r}")


@dataclass(frozen=True)
class _Scenario:
    inspector: str  # manifest file stem under builtin/linux/
    out_name: str  # fixture basename
    main_stdout: str  # the JSON object the collector pipeline would emit
    expect_findings: bool  # abnormal scenarios must produce >=1 finding


# The crafted JSON objects below are exactly what each inspector's collector
# pipeline emits on a host in the given state. They are the scenario data we
# author (design D-7: the recorder authors the per-scenario stdout, the runner
# captures the verbatim command).
_SCENARIOS: tuple[_Scenario, ...] = (
    # ---- linux.disk.io --------------------------------------------------- #
    _Scenario(
        inspector="disk_io",
        out_name="disk_io_saturated.json",
        # Sorted by device (the collector's `sort` stage).
        main_stdout=(
            '{"results":['
            '{"device":"nvme0n1","util_pct":"12.0","await_ms":"0.40","ops":300},'
            '{"device":"sda","util_pct":"98.5","await_ms":"42.30","ops":1200}'
            "]}"
        ),
        expect_findings=True,
    ),
    _Scenario(
        inspector="disk_io",
        out_name="disk_io_high_latency.json",
        main_stdout=(
            '{"results":[{"device":"sdb","util_pct":"40.0","await_ms":"75.50","ops":500}]}'
        ),
        expect_findings=True,
    ),
    _Scenario(
        inspector="disk_io",
        out_name="disk_io_ok.json",
        # Sorted by device (the collector's `sort` stage).
        main_stdout=(
            '{"results":['
            '{"device":"nvme0n1","util_pct":"0.0","await_ms":"0.00","ops":0},'
            '{"device":"sda","util_pct":"5.0","await_ms":"1.20","ops":80}'
            "]}"
        ),
        expect_findings=False,
    ),
    # ---- linux.disk.smart ------------------------------------------------ #
    _Scenario(
        inspector="disk_smart",
        out_name="disk_smart_failed.json",
        main_stdout=(
            '{"results":['
            '{"device":"sda","rotational":1,"size_gib":"931.5","smart_health":"FAILED"},'
            '{"device":"nvme0n1","rotational":0,"size_gib":"476.9","smart_health":"PASSED"}'
            "]}"
        ),
        expect_findings=True,
    ),
    _Scenario(
        inspector="disk_smart",
        out_name="disk_smart_ok.json",
        main_stdout=(
            '{"results":['
            '{"device":"sda","rotational":1,"size_gib":"931.5","smart_health":"PASSED"},'
            '{"device":"nvme0n1","rotational":0,"size_gib":"476.9","smart_health":"unknown"}'
            "]}"
        ),
        expect_findings=False,
    ),
    # ---- linux.fs.mount_health ------------------------------------------- #
    _Scenario(
        inspector="fs_mount_health",
        out_name="fs_mount_health_readonly.json",
        main_stdout=(
            '{"results":['
            '{"target":"/","source":"/dev/sda1","fstype":"ext4","read_only":false},'
            '{"target":"/data","source":"/dev/sdb1","fstype":"xfs","read_only":true}'
            "]}"
        ),
        expect_findings=True,
    ),
    _Scenario(
        inspector="fs_mount_health",
        out_name="fs_mount_health_ok.json",
        main_stdout=(
            '{"results":['
            '{"target":"/","source":"/dev/sda1","fstype":"ext4","read_only":false},'
            '{"target":"/data","source":"/dev/sdb1","fstype":"xfs","read_only":false}'
            "]}"
        ),
        expect_findings=False,
    ),
    # ---- linux.fs.logrotate ---------------------------------------------- #
    _Scenario(
        inspector="fs_logrotate",
        out_name="fs_logrotate_stale.json",
        main_stdout=('{"days_since_last":"21.0","last_date":"2024-1-15","tracked_files":12}'),
        expect_findings=True,
    ),
    _Scenario(
        inspector="fs_logrotate",
        out_name="fs_logrotate_ok.json",
        main_stdout=('{"days_since_last":"0.5","last_date":"2024-2-5","tracked_files":12}'),
        expect_findings=False,
    ),
)


async def _record(scenario: _Scenario) -> None:
    settings = Settings()
    logger = structlog.get_logger("os-disk-fs-record")
    manifest = load_manifest(_BUILTIN_DIR / f"{scenario.inspector}.yaml")

    cap_values: set[str] = {"shell"} | set(manifest.requires_capabilities)
    capabilities = {Capability(value) for value in cap_values}

    recorded: list[dict[str, Any]] = []
    runner = InspectorRunner(TargetRegistry(), settings=settings, logger=logger)
    target = _CaptureTarget(
        "capture-host",
        capabilities=capabilities,
        main_stdout=scenario.main_stdout,
        sink=recorded,
    )
    result = await runner.run(manifest, target)

    # Generation sanity: the crafted stdout MUST parse cleanly, and abnormal
    # scenarios MUST produce a finding so we never commit a no-op fixture.
    assert result.status == "ok", (
        f"{scenario.out_name}: status={result.status} error={result.error}"
    )
    if scenario.expect_findings:
        assert result.findings, (
            f"{scenario.out_name}: expected a finding but got none — check main_stdout"
        )
    else:
        assert not result.findings, (
            f"{scenario.out_name}: expected no finding but got {result.findings}"
        )

    # Dedup by command (ReplayTarget rejects duplicate command keys on load).
    commands: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in recorded:
        if entry["cmd"] in seen:
            continue
        seen.add(entry["cmd"])
        commands.append(entry)

    fixture = {
        "impersonate": "local",
        "capabilities": sorted(cap_values),
        "commands": commands,
        "files": {},
    }
    _FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = _FIXTURE_DIR / scenario.out_name
    path.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path}")


async def _main() -> None:
    for scenario in _SCENARIOS:
        await _record(scenario)


if __name__ == "__main__":
    asyncio.run(_main())
