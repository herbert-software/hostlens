"""One-shot fixture recorder for the wave-1 CPU + memory inspectors (dev-tool).

This is the **pilot** recorder that establishes the fixture-generation pattern
for `add-os-shell-inspectors-wave1`: the OS/Linux shell probes are Linux-only
(they read /proc and /sys) so we do NOT need a real Linux host. Instead we use
the `_CaptureTarget` pattern (lifted verbatim from `tests/incidents/_generate.py`):
drive the **real** `InspectorRunner` against a target that

  * answers `command -v X` binary probes with a synthetic path,
  * answers `[ -r P ]` file probes empty, and
  * returns a hand-crafted `main_stdout` for the rendered collect command,

while recording every exact rendered command into a sink. Because the command
strings are captured verbatim from the real renderer (never hand-written), the
fixture can never drift from what `ReplayTarget` will look up at snapshot time
(byte-level match, Authoring Contract / design D-7).

The crafted `main_stdout` is the JSON object the collector pipeline would emit
on the target host for the given scenario — this is the one piece we author
(the scenario data), exactly as `tests/incidents/_generate.py` authors its
`main_stdout`.

Each scenario asserts (generation sanity, mirroring `_generate.py`) that the
crafted stdout drives the inspector to `status=ok`; abnormal scenarios further
assert at least one finding fired so we never commit a no-op fixture.

Run it to (re)write the fixtures:

    .venv-impl/bin/python tests/inspectors/_record_os_compute_memory.py

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
_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "os_compute_memory"

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


# The crafted JSON objects below are exactly what each inspector's awk pipeline
# emits on a host in the given state. They are the scenario data we author.
_SCENARIOS: tuple[_Scenario, ...] = (
    # ---- linux.cpu.throttling ------------------------------------------- #
    _Scenario(
        inspector="cpu_throttling",
        out_name="cpu_throttling_high.json",
        main_stdout=(
            '{"nr_periods":1000,"nr_throttled":420,'
            '"throttled_usec":12345678,"throttled_pct":"42.00"}'
        ),
        expect_findings=True,
    ),
    _Scenario(
        inspector="cpu_throttling",
        out_name="cpu_throttling_ok.json",
        main_stdout=(
            '{"nr_periods":1000,"nr_throttled":0,"throttled_usec":0,"throttled_pct":"0.00"}'
        ),
        expect_findings=False,
    ),
    # ---- linux.cpu.cpufreq ---------------------------------------------- #
    _Scenario(
        inspector="cpu_cpufreq",
        out_name="cpu_cpufreq_powersave.json",
        main_stdout=(
            '{"governor":"powersave","cur_freq_khz":800000,'
            '"max_freq_khz":3600000,"freq_pct":"22.2"}'
        ),
        expect_findings=True,
    ),
    _Scenario(
        inspector="cpu_cpufreq",
        out_name="cpu_cpufreq_ok.json",
        main_stdout=(
            '{"governor":"performance","cur_freq_khz":3600000,'
            '"max_freq_khz":3600000,"freq_pct":"100.0"}'
        ),
        expect_findings=False,
    ),
    # ---- linux.memory.swap ---------------------------------------------- #
    _Scenario(
        inspector="memory_swap",
        out_name="memory_swap_high.json",
        main_stdout=(
            '{"swap_total_kb":8388608,"swap_free_kb":419430,"swappiness":60,"swap_used_pct":"95.0"}'
        ),
        expect_findings=True,
    ),
    _Scenario(
        inspector="memory_swap",
        out_name="memory_swap_ok.json",
        main_stdout=(
            '{"swap_total_kb":8388608,"swap_free_kb":8388608,"swappiness":10,"swap_used_pct":"0.0"}'
        ),
        expect_findings=False,
    ),
    # ---- linux.memory.hugepages ----------------------------------------- #
    _Scenario(
        inspector="memory_hugepages",
        out_name="memory_hugepages_idle.json",
        main_stdout=(
            '{"hugepages_total":1024,"hugepages_free":1010,'
            '"hugepagesize_kb":2048,"hugepages_free_pct":"98.6"}'
        ),
        expect_findings=True,
    ),
    _Scenario(
        inspector="memory_hugepages",
        out_name="memory_hugepages_ok.json",
        main_stdout=(
            '{"hugepages_total":0,"hugepages_free":0,'
            '"hugepagesize_kb":2048,"hugepages_free_pct":"0.0"}'
        ),
        expect_findings=False,
    ),
)


async def _record(scenario: _Scenario) -> None:
    settings = Settings()
    logger = structlog.get_logger("os-compute-memory-record")
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

    # Generation sanity (mirrors tests/incidents/_generate.py): the crafted
    # stdout MUST parse cleanly, and abnormal scenarios MUST produce a finding
    # so we never commit a no-op fixture.
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
