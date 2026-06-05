"""One-shot fixture recorder for the wave-1 systemd + cron inspectors (dev-tool).

`add-os-shell-inspectors-wave1` group 5: `linux.systemd.timer_status`,
`linux.systemd.masked`, `linux.cron.last_runs`, `linux.cron.failures`.

Same pattern as the pilot recorder (`_record_os_compute_memory.py`): drive the
**real** `InspectorRunner` against a `_CaptureTarget` that answers binary/file
probes synthetically and returns a hand-crafted `main_stdout` for the rendered
collect command, recording every exact rendered command into a sink. Because
the command strings are captured verbatim from the real renderer (never
hand-written), the fixture can never drift from what `ReplayTarget` will look up
at snapshot time (byte-level match, Authoring Contract / design D-7).

The cron inspectors declare `collect.sampling_window`, so the runner renders
`--since {window_start} --until {window_end}` into the command. To keep that
command byte-stable across record + replay we drive the runner with a **frozen
clock** (design D-3 interval-query path); the snapshot test uses the same frozen
clock. The systemd inspectors carry no window but use the same clock harmlessly.

Run it to (re)write the fixtures:

    .venv-impl/bin/python tests/inspectors/_record_os_systemd_cron.py

NOT collected by pytest (no `test_` prefix).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "os_systemd_cron"

_PROBE_PREFIX = "command -v "
_FILE_PROBE_PREFIX = "[ -r "


def _frozen_clock() -> datetime:
    """Fixed UTC instant so sampling_window commands render byte-stable."""

    return datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC)


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
    main_stdout: str  # the JSON/kv object the collector pipeline would emit
    expect_findings: bool  # abnormal scenarios must produce >=1 finding
    parameters: dict[str, Any] = field(default_factory=dict)


# The crafted objects below are exactly what each inspector's jq/awk pipeline
# emits on a host in the given state. They are the scenario data we author.
_SCENARIOS: tuple[_Scenario, ...] = (
    # ---- linux.systemd.timer_status ------------------------------------- #
    _Scenario(
        inspector="systemd_timer_status",
        out_name="systemd_timer_status_overdue.json",
        main_stdout=(
            '{"results":['
            '{"unit":"logrotate.timer","next_elapse_usec":0,'
            '"last_trigger_usec":1700000000000000,"last_trigger_age_sec":300000},'
            '{"unit":"apt-daily.timer","next_elapse_usec":1704200000000000,'
            '"last_trigger_usec":1704190000000000,"last_trigger_age_sec":3600}'
            "]}"
        ),
        expect_findings=True,
    ),
    _Scenario(
        inspector="systemd_timer_status",
        out_name="systemd_timer_status_ok.json",
        main_stdout=(
            '{"results":['
            '{"unit":"apt-daily.timer","next_elapse_usec":1704200000000000,'
            '"last_trigger_usec":1704190000000000,"last_trigger_age_sec":3600}'
            "]}"
        ),
        expect_findings=False,
    ),
    # ---- linux.systemd.masked ------------------------------------------- #
    _Scenario(
        inspector="systemd_masked",
        out_name="systemd_masked_present.json",
        main_stdout=('{"masked":[{"unit":"rsyslog.service"},{"unit":"apparmor.service"}]}'),
        expect_findings=True,
    ),
    _Scenario(
        inspector="systemd_masked",
        out_name="systemd_masked_none.json",
        main_stdout='{"masked":[]}',
        expect_findings=False,
    ),
    # ---- linux.cron.last_runs ------------------------------------------- #
    _Scenario(
        inspector="cron_last_runs",
        out_name="cron_last_runs_stale.json",
        # Sorted by command (the collector's `sort` stage); the cron `(user) CMD
        # (...)` prefix is stripped to the bare command path by the awk.
        main_stdout=(
            '{"results":['
            '{"command":"/usr/bin/refresh-cache","last_run_age_sec":120},'
            '{"command":"/usr/local/bin/backup.sh","last_run_age_sec":18000}'
            "]}"
        ),
        expect_findings=True,
    ),
    _Scenario(
        inspector="cron_last_runs",
        out_name="cron_last_runs_ok.json",
        main_stdout=('{"results":[{"command":"/usr/bin/refresh-cache","last_run_age_sec":120}]}'),
        expect_findings=False,
    ),
    # ---- linux.cron.failures -------------------------------------------- #
    _Scenario(
        inspector="cron_failures",
        out_name="cron_failures_spike.json",
        main_stdout="failure_count=7\nwindow_seconds=3600\n",
        expect_findings=True,
    ),
    _Scenario(
        inspector="cron_failures",
        out_name="cron_failures_ok.json",
        main_stdout="failure_count=0\nwindow_seconds=3600\n",
        expect_findings=False,
    ),
)


async def _record(scenario: _Scenario) -> None:
    settings = Settings()
    logger = structlog.get_logger("os-systemd-cron-record")
    manifest = load_manifest(_BUILTIN_DIR / f"{scenario.inspector}.yaml")

    cap_values: set[str] = {"shell"} | set(manifest.requires_capabilities)
    capabilities = {Capability(value) for value in cap_values}

    recorded: list[dict[str, Any]] = []
    runner = InspectorRunner(
        TargetRegistry(), settings=settings, logger=logger, clock=_frozen_clock
    )
    target = _CaptureTarget(
        "capture-host",
        capabilities=capabilities,
        main_stdout=scenario.main_stdout,
        sink=recorded,
    )
    result = await runner.run(manifest, target, parameters=scenario.parameters or None)

    # Generation sanity (mirrors the pilot recorder): the crafted stdout MUST
    # parse cleanly, and abnormal scenarios MUST produce a finding so we never
    # commit a no-op fixture.
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
