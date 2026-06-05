"""One-shot fixture recorder for the wave-1 kernel/system + log inspectors.

`add-os-shell-inspectors-wave1` group 6: `linux.system.reboot_required`,
`linux.kernel.taint`, `linux.kernel.messages`, `log.exception_burst`.

Same `_CaptureTarget` pattern as the pilot `_record_os_compute_memory.py`:
drive the **real** `InspectorRunner` against a target that answers binary /
file probes synthetically and returns a hand-crafted `main_stdout` for the
rendered collect command, recording every exact rendered command into a sink.
Because the command strings are captured verbatim from the real renderer (never
hand-written), the fixture can never drift from what `ReplayTarget` looks up at
snapshot time (byte-level match, Authoring Contract / design D-7).

`linux.kernel.messages` declares a `sampling_window`, so the renderer embeds
`window_start` / `window_end` timestamps. The recorder injects a **frozen
clock** (`FROZEN_DT`) into `InspectorRunner` so those timestamps render byte-
stably; the snapshot test injects the SAME frozen clock so the rendered command
matches the recorded fixture (design D-3, mirroring `tests/incidents/_harness`).

Each abnormal scenario asserts >=1 finding fired (no no-op fixture); ok
scenarios assert zero findings.

Run it to (re)write the fixtures:

    .venv-impl/bin/python tests/inspectors/_record_os_kernel_log.py

NOT collected by pytest (no `test_` prefix).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.runner import InspectorRunner
from hostlens.targets.base import Capability, ExecResult
from hostlens.targets.registry import TargetRegistry

_BUILTIN_ROOT = Path(__file__).resolve().parents[2] / "src" / "hostlens" / "inspectors" / "builtin"
_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "os_kernel_log"

_PROBE_PREFIX = "command -v "
_FILE_PROBE_PREFIX = "[ -r "

# Fixed UTC instant the sampling_window inspector (`linux.kernel.messages`)
# renders against. The snapshot test injects the SAME instant so the rendered
# `--since/--until` strings match the recorded command byte-for-byte.
FROZEN_DT = datetime(2026, 1, 2, 12, 0, 0, tzinfo=UTC)


def frozen_clock() -> datetime:
    return FROZEN_DT


class _CaptureTarget:
    """Generation-only target: returns canned stdout and records every command."""

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
    domain: str  # builtin subdir (linux / log)
    inspector: str  # manifest file stem
    out_name: str  # fixture basename
    main_stdout: str  # the JSON / kv object the collector pipeline would emit
    expect_findings: bool
    parameters: dict[str, Any] | None = None


# The crafted outputs below are exactly what each inspector's collector emits on
# a host in the given state. They are the scenario data we author.
_SCENARIOS: tuple[_Scenario, ...] = (
    # ---- linux.system.reboot_required ----------------------------------- #
    _Scenario(
        domain="linux",
        inspector="system_reboot_required",
        out_name="system_reboot_required_pending.json",
        main_stdout=(
            '{"reboot_required":true,'
            '"reason":"*** System restart required ***",'
            '"pkgs":"linux-image-6.8.0-45-generic libssl3"}'
        ),
        expect_findings=True,
    ),
    _Scenario(
        domain="linux",
        inspector="system_reboot_required",
        out_name="system_reboot_required_ok.json",
        main_stdout='{"reboot_required":false,"reason":"","pkgs":""}',
        expect_findings=False,
    ),
    # ---- linux.kernel.taint --------------------------------------------- #
    _Scenario(
        domain="linux",
        inspector="kernel_taint",
        out_name="kernel_taint_tainted.json",
        main_stdout='{"tainted":4097,"flags":"proprietary-module-loaded,out-of-tree-module"}',
        expect_findings=True,
    ),
    _Scenario(
        domain="linux",
        inspector="kernel_taint",
        out_name="kernel_taint_ok.json",
        main_stdout='{"tainted":0,"flags":""}',
        expect_findings=False,
    ),
    # ---- linux.kernel.messages (sampling_window) ------------------------ #
    _Scenario(
        domain="linux",
        inspector="kernel_messages",
        out_name="kernel_messages_burst.json",
        main_stdout="error_count=37\nwindow_seconds=300\n",
        expect_findings=True,
    ),
    _Scenario(
        domain="linux",
        inspector="kernel_messages",
        out_name="kernel_messages_ok.json",
        main_stdout="error_count=0\nwindow_seconds=300\n",
        expect_findings=False,
    ),
    # ---- log.exception_burst -------------------------------------------- #
    _Scenario(
        domain="log",
        inspector="exception_burst",
        out_name="exception_burst_burst.json",
        main_stdout=(
            '{"results":['
            '{"signature":"java.lang.NullPointerException","count":42},'
            '{"signature":"java.lang.OutOfMemoryError","count":3}'
            "]}"
        ),
        expect_findings=True,
        parameters={"log_path": "/var/log/app.log"},
    ),
    _Scenario(
        domain="log",
        inspector="exception_burst",
        out_name="exception_burst_ok.json",
        main_stdout=('{"results":[{"signature":"java.lang.IllegalStateException","count":2}]}'),
        expect_findings=False,
        parameters={"log_path": "/var/log/app.log"},
    ),
)


async def _record(scenario: _Scenario) -> None:
    settings = Settings()
    logger = structlog.get_logger("os-kernel-log-record")
    manifest = load_manifest(_BUILTIN_ROOT / scenario.domain / f"{scenario.inspector}.yaml")

    cap_values: set[str] = {"shell"} | set(manifest.requires_capabilities)
    capabilities = {Capability(value) for value in cap_values}

    recorded: list[dict[str, Any]] = []
    runner = InspectorRunner(TargetRegistry(), settings=settings, logger=logger, clock=frozen_clock)
    target = _CaptureTarget(
        "capture-host",
        capabilities=capabilities,
        main_stdout=scenario.main_stdout,
        sink=recorded,
    )
    result = await runner.run(manifest, target, parameters=scenario.parameters)

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
