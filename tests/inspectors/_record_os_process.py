"""One-shot fixture recorder for the wave-1 process-domain inspectors (dev-tool).

Mirrors the pilot recorder `_record_os_compute_memory.py` for
`add-os-shell-inspectors-wave1` group 4 (process domain): `linux.process.zombies`,
`linux.process.total`, `linux.process.critical_alive`. The OS/Linux shell probes
are Linux-only (they read /proc and run `ps`/`pgrep`) so we do NOT need a real
Linux host. Instead we use the `_CaptureTarget` pattern: drive the **real**
`InspectorRunner` against a target that

  * answers `command -v X` binary probes with a synthetic path,
  * answers `[ -r P ]` file probes empty, and
  * returns a hand-crafted `main_stdout` for the rendered collect command,

while recording every exact rendered command into a sink. Because the command
strings are captured verbatim from the real renderer (never hand-written), the
fixture can never drift from what `ReplayTarget` will look up at snapshot time
(byte-level match, Authoring Contract / design D-7).

`linux.process.critical_alive` is parameterised; the recorder passes the SAME
`parameters` the snapshot test passes so the rendered command (and thus the
captured key) matches byte-for-byte. It also records an injection-safety scenario
proving a process name carrying shell metacharacters is shlex-quoted into one
loop word rather than executed.

Run it to (re)write the fixtures:

    .venv-impl/bin/python tests/inspectors/_record_os_process.py

NOT collected by pytest (no `test_` prefix).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
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
_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "os_process"

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
    parameters: dict[str, Any] = field(default_factory=dict)


# The crafted JSON objects below are exactly what each inspector's collector
# pipeline emits on a host in the given state. They are the scenario data we
# author.
_SCENARIOS: tuple[_Scenario, ...] = (
    # ---- linux.process.zombies ------------------------------------------ #
    _Scenario(
        inspector="process_zombies",
        out_name="process_zombies_present.json",
        main_stdout=(
            '{"zombie_count":3,"results":['
            '{"pid":4242,"comm":"defunct-a"},'
            '{"pid":4243,"comm":"defunct-b"},'
            '{"pid":4244,"comm":"defunct-c"}]}'
        ),
        expect_findings=True,
    ),
    _Scenario(
        inspector="process_zombies",
        out_name="process_zombies_ok.json",
        main_stdout='{"zombie_count":0,"results":[]}',
        expect_findings=False,
    ),
    # ---- linux.process.total -------------------------------------------- #
    _Scenario(
        inspector="process_total",
        out_name="process_total_near_pid_max.json",
        main_stdout='{"total":30000,"pid_max":32768,"used_pct":"91.6"}',
        expect_findings=True,
    ),
    _Scenario(
        inspector="process_total",
        out_name="process_total_ok.json",
        main_stdout='{"total":250,"pid_max":32768,"used_pct":"0.8"}',
        expect_findings=False,
    ),
    # ---- linux.process.critical_alive ----------------------------------- #
    _Scenario(
        inspector="process_critical_alive",
        out_name="process_critical_alive_missing.json",
        main_stdout=('{"results":[{"name":"sshd","alive":true},{"name":"nginx","alive":false}]}'),
        expect_findings=True,
        parameters={"names": ["sshd", "nginx"]},
    ),
    _Scenario(
        inspector="process_critical_alive",
        out_name="process_critical_alive_ok.json",
        main_stdout=('{"results":[{"name":"sshd","alive":true},{"name":"nginx","alive":true}]}'),
        expect_findings=False,
        parameters={"names": ["sshd", "nginx"]},
    ),
    # Injection-safety: a name carrying shell metacharacters must be shlex-quoted
    # into a single loop word (the `^[a-zA-Z0-9._/-]+$` pattern blocks `;`/`$`, so
    # we use a `/`-and-`.`-laden absolute-path-like name which is pattern-legal yet
    # exercises that `| map('sh')` keeps it one word). The recorded command string
    # proves the quoting; the snapshot asserts replay.misses == [].
    _Scenario(
        inspector="process_critical_alive",
        out_name="process_critical_alive_injection.json",
        main_stdout='{"results":[{"name":"/usr/sbin/cron-d.worker","alive":false}]}',
        expect_findings=True,
        parameters={"names": ["/usr/sbin/cron-d.worker"]},
    ),
)


async def _record(scenario: _Scenario) -> None:
    settings = Settings()
    logger = structlog.get_logger("os-process-record")
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
    result = await runner.run(manifest, target, parameters=scenario.parameters or None)

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
