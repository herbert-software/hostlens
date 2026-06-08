"""One-shot fixture recorder for the security + package os-shell inspectors.

`add-security-baseline-and-package-inspectors` (os-shell wave-2):
`security.failed_logins`, `security.sudo_history`,
`security.world_writable_dirs`, `pkg.pending_updates`,
`pkg.security_patches`, `pkg.held_back`.

These are Linux-only OS probes (journald / find / apt / dnf), so per the
established os-shell convention (design D-7, see `_record_os_disk_fs.py` /
`_record_os_kernel_log.py`) we do NOT need a real Linux host: we drive the
**real** `InspectorRunner` against a `_CaptureTarget` that

  * answers `command -v X` binary probes with a synthetic path,
  * answers `[ -r P ]` file probes empty, and
  * returns a hand-crafted ``main_stdout`` (+ optional non-zero exit code) for
    the rendered collect command,

while recording every exact rendered command into a sink. The command strings
are captured verbatim from the real renderer (never hand-written), so the
fixture can never drift from what `ReplayTarget` looks up at snapshot time
(byte-level match). The per-scenario stdout / exit_code is the scenario data we
author — it is what the collector pipeline emits on a host in the given state.

`main_exit_code != 0` + empty stdout models the collector's fail-loud path
(`|| exit 1`): the runner sees a non-zero exit with empty stdout, the JSON
parser raises, and the inspector lands `status=exception` (the false-negative
guard). The collector's internal shell correctness (pipe-safe, find rc handling
D-6.2, dnf-100 D-6.3) runs on the real target and is locked at the
command-string level by the verbatim capture, not executed during replay.

The two sampling_window inspectors (`failed_logins` / `sudo_history`) render
`--since "{{ window_start }}"`; a frozen clock is injected so the recorded
command string is deterministic and the snapshot test (injecting the SAME
instant) matches byte-for-byte.

Run it to (re)write the fixtures:

    python tests/inspectors/_record_security_pkg.py

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
_FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"

_PROBE_PREFIX = "command -v "
_FILE_PROBE_PREFIX = "[ -r "

# Fixed UTC instant the sampling_window inspectors render against. The snapshot
# test injects the SAME instant so the rendered `--since` strings match the
# recorded command byte-for-byte.
FROZEN_DT = datetime(2026, 1, 2, 12, 0, 0, tzinfo=UTC)


def frozen_clock() -> datetime:
    return FROZEN_DT


class _CaptureTarget:
    """Generation-only target: returns canned stdout/exit and records commands.

    Extends the wave-1 pattern with ``main_exit_code`` / ``main_stderr`` so an
    exception scenario can return a non-zero exit + empty stdout (the
    collector's fail-loud path) instead of always exit 0.
    """

    type = "local"

    def __init__(
        self,
        name: str,
        *,
        capabilities: set[Capability],
        main_stdout: str,
        sink: list[dict[str, Any]],
        main_exit_code: int = 0,
        main_stderr: str = "",
    ) -> None:
        self.name = name
        self.capabilities = capabilities
        self._main_stdout = main_stdout
        self._main_exit_code = main_exit_code
        self._main_stderr = main_stderr
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
            stdout, stderr, code = f"/usr/bin/{binary}\n", "", 0
        elif cmd.startswith(_FILE_PROBE_PREFIX):
            stdout, stderr, code = "", "", 0
        else:
            stdout, stderr, code = self._main_stdout, self._main_stderr, self._main_exit_code
        self._sink.append(
            {
                "cmd": cmd,
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": code,
                "duration_seconds": 0.0,
            }
        )
        return ExecResult(
            exit_code=code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=0.0,
            timed_out=False,
        )

    async def read_file(self, path: str) -> bytes:  # pragma: no cover - unused here
        raise AssertionError(f"_CaptureTarget.read_file unexpectedly called: {path!r}")


@dataclass(frozen=True)
class _Scenario:
    inspector: str  # manifest file stem
    domain: str  # "security" | "pkg" — builtin subdir + fixture subdir
    out_name: str  # fixture basename
    main_stdout: str  # the JSON the collector pipeline would emit
    expect_findings: bool
    main_exit_code: int = 0
    main_stderr: str = ""
    expect_status: str = "ok"


_SCENARIOS: tuple[_Scenario, ...] = (
    # ---- security.failed_logins (sampling_window) ------------------------ #
    _Scenario(
        inspector="failed_logins",
        domain="security",
        out_name="failed_logins_burst.json",
        # Crafted output of the double-unit journalctl|grep -c pipeline; 25 > 20.
        # The recorded command (captured) lists BOTH ssh.service + sshd.service,
        # locking the cross-distro unit OR at the command-string level (D-6.5).
        main_stdout='{"failed":25}',
        expect_findings=True,
    ),
    _Scenario(
        inspector="failed_logins",
        domain="security",
        out_name="failed_logins_ok.json",
        main_stdout='{"failed":3}',  # 3 <= 20 → honest negative, no finding
        expect_findings=False,
    ),
    _Scenario(
        inspector="failed_logins",
        domain="security",
        out_name="failed_logins_unreachable.json",
        # journal unreadable → collector fail-loud `|| exit 1` → empty + exit 1.
        main_stdout="",
        main_exit_code=1,
        main_stderr="journalctl unavailable\n",
        expect_findings=False,
        expect_status="exception",
    ),
    # ---- security.sudo_history (sampling_window) ------------------------- #
    _Scenario(
        inspector="sudo_history",
        domain="security",
        out_name="sudo_history_spike.json",
        main_stdout='{"invocations":120}',  # 120 > 50
        expect_findings=True,
    ),
    _Scenario(
        inspector="sudo_history",
        domain="security",
        out_name="sudo_history_ok.json",
        main_stdout='{"invocations":5}',
        expect_findings=False,
    ),
    _Scenario(
        inspector="sudo_history",
        domain="security",
        out_name="sudo_history_unreachable.json",
        main_stdout="",
        main_exit_code=1,
        main_stderr="journalctl unavailable\n",
        expect_findings=False,
        expect_status="exception",
    ),
    # ---- security.world_writable_dirs (find, D-6.2) --------------------- #
    _Scenario(
        inspector="world_writable_dirs",
        domain="security",
        out_name="world_writable_dirs_found.json",
        # Non-empty results → for_each finding. NOTE: the D-6.2 third state
        # "partial subtree denied (find rc!=0) but data present → ok" is
        # INDISTINGUISHABLE from this fixture at the replay boundary — by the
        # time the collect.command exits, the internal D-6.2 judge has already
        # resolved (out non-empty → no exit 1 → exit 0 + JSON), so branch-b
        # produces the identical (exit 0, JSON) boundary output as this branch-a
        # fixture. A separate partial-denied fixture is therefore architecturally
        # impossible under D-7 (it would be byte-identical). The find-rc judge
        # itself is locked at the command-string level (verbatim capture), not
        # executed under replay. See tasks.md deviation registry.
        main_stdout='{"results":[{"path":"/var/tmp/world-writable"}]}',
        expect_findings=True,
    ),
    _Scenario(
        inspector="world_writable_dirs",
        domain="security",
        out_name="world_writable_dirs_clean.json",
        main_stdout='{"results":[]}',  # rc=0 + empty → honest "no writable dirs"
        expect_findings=False,
    ),
    _Scenario(
        inspector="world_writable_dirs",
        domain="security",
        out_name="world_writable_dirs_unreachable.json",
        # D-6.2: rc != 0 AND empty stdout → collector exit 1 → exception.
        main_stdout="",
        main_exit_code=1,
        main_stderr="find produced no output and failed\n",
        expect_findings=False,
        expect_status="exception",
    ),
    # ---- pkg.pending_updates -------------------------------------------- #
    _Scenario(
        inspector="pending_updates",
        domain="pkg",
        out_name="pending_updates_available.json",
        main_stdout='{"pending":12}',  # 12 > 0
        expect_findings=True,
    ),
    _Scenario(
        inspector="pending_updates",
        domain="pkg",
        out_name="pending_updates_ok.json",
        main_stdout='{"pending":0}',
        expect_findings=False,
    ),
    _Scenario(
        inspector="pending_updates",
        domain="pkg",
        out_name="pending_updates_no_pkg_mgr.json",
        # Neither apt-get nor dnf → collector else-branch exit 1 (or apt/dnf
        # main command failed, D-6.4) → empty + exit 1 → exception, NOT ok-0.
        main_stdout="",
        main_exit_code=1,
        main_stderr="no package manager\n",
        expect_findings=False,
        expect_status="exception",
    ),
    # ---- pkg.security_patches ------------------------------------------- #
    _Scenario(
        inspector="security_patches",
        domain="pkg",
        out_name="security_patches_pending.json",
        # Downstream count + finding lock ONLY (NOT a filter-correctness lock):
        # this is the author-supplied POST-filter count. _CaptureTarget returns
        # this stdout verbatim — the `-security` filter regex never runs during
        # recording, so a mis-written filter would NOT yield 0 here (it is not
        # invoked at all). What this scenario locks is the post-filter
        # count → parse → DSL → finding chain (non-zero ⇒ finding). The filter
        # regex itself is command-string-locked + Demo-Path-verified on a real
        # apt/dnf host. See tasks.md deviation registry.
        main_stdout='{"patches":4}',
        expect_findings=True,
    ),
    _Scenario(
        inspector="security_patches",
        domain="pkg",
        out_name="security_patches_ok.json",
        main_stdout='{"patches":0}',
        expect_findings=False,
    ),
    _Scenario(
        inspector="security_patches",
        domain="pkg",
        out_name="security_patches_failure.json",
        main_stdout="",
        main_exit_code=1,
        main_stderr="package query failed\n",
        expect_findings=False,
        expect_status="exception",
    ),
    # ---- pkg.held_back -------------------------------------------------- #
    _Scenario(
        inspector="held_back",
        domain="pkg",
        out_name="held_back_present.json",
        main_stdout='{"results":[{"package":"nginx"},{"package":"openssl"}]}',
        expect_findings=True,
    ),
    _Scenario(
        inspector="held_back",
        domain="pkg",
        out_name="held_back_ok.json",
        main_stdout='{"results":[]}',
        expect_findings=False,
    ),
    _Scenario(
        inspector="held_back",
        domain="pkg",
        out_name="held_back_failure.json",
        main_stdout="",
        main_exit_code=1,
        main_stderr="apt-mark failed\n",
        expect_findings=False,
        expect_status="exception",
    ),
)


async def _record(scenario: _Scenario) -> None:
    settings = Settings()
    logger = structlog.get_logger("security-pkg-record")
    manifest = load_manifest(_BUILTIN_ROOT / scenario.domain / f"{scenario.inspector}.yaml")

    cap_values: set[str] = {"shell"} | set(manifest.requires_capabilities)
    capabilities = {Capability(value) for value in cap_values}

    recorded: list[dict[str, Any]] = []
    runner = InspectorRunner(TargetRegistry(), settings=settings, logger=logger, clock=frozen_clock)
    target = _CaptureTarget(
        "capture-host",
        capabilities=capabilities,
        main_stdout=scenario.main_stdout,
        main_exit_code=scenario.main_exit_code,
        main_stderr=scenario.main_stderr,
        sink=recorded,
    )
    result = await runner.run(manifest, target)

    assert result.status == scenario.expect_status, (
        f"{scenario.out_name}: status={result.status} (want {scenario.expect_status}) "
        f"error={result.error}"
    )
    if scenario.expect_findings:
        assert result.findings, (
            f"{scenario.out_name}: expected a finding but got none — check main_stdout"
        )
    else:
        assert not result.findings, (
            f"{scenario.out_name}: expected no finding but got {result.findings}"
        )

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
    out_dir = _FIXTURE_ROOT / scenario.domain
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / scenario.out_name
    path.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path}")


async def _main() -> None:
    for scenario in _SCENARIOS:
        await _record(scenario)


if __name__ == "__main__":
    asyncio.run(_main())
