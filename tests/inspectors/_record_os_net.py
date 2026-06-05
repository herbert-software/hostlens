"""One-shot fixture recorder for the wave-1 network / DNS / NTP inspectors.

Group 3 of `add-os-shell-inspectors-wave1`: `net.connections`,
`net.listening_ports`, `net.dns.resolve`, `net.ntp.drift`. These OS/Linux shell
probes are Linux-only (iproute2 `ss`, `chronyc`, `dig`) so we do NOT need a real
host. We reuse the pilot's `_CaptureTarget` pattern (lifted from
`_record_os_compute_memory.py`): drive the **real** `InspectorRunner` against a
target that

  * answers `command -v X` binary probes with a synthetic path,
  * answers `[ -r P ]` file probes empty, and
  * returns a hand-crafted `main_stdout` for the rendered collect command,

while recording every exact rendered command into a sink. Because the command
strings are captured verbatim from the real renderer (never hand-written), the
fixture can never drift from what `ReplayTarget` looks up at snapshot time
(byte-level match, Authoring Contract / design D-7).

Parameterised inspectors (`net.dns.resolve`, `net.listening_ports`) are run with
the SAME `parameters` the snapshot test passes, so the captured command (which
embeds the rendered, `| map('sh')`-quoted parameter words) matches replay.

Each scenario asserts (generation sanity) that the crafted stdout drives the
inspector to `status=ok`; abnormal scenarios further assert at least one finding
fired so we never commit a no-op fixture (ok scenarios assert none).

Run it to (re)write the fixtures:

    .venv-impl/bin/python tests/inspectors/_record_os_net.py

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
    Path(__file__).resolve().parents[2] / "src" / "hostlens" / "inspectors" / "builtin" / "net"
)
_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "os_net"

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
    inspector: str  # manifest file stem under builtin/net/
    out_name: str  # fixture basename
    main_stdout: str  # the JSON object the collector pipeline would emit
    expect_findings: bool  # abnormal scenarios must produce >=1 finding
    parameters: dict[str, Any] = field(default_factory=dict)


# The crafted JSON objects below are exactly what each inspector's awk/dig
# pipeline emits on a host in the given state. They are the scenario data we
# author.
_SCENARIOS: tuple[_Scenario, ...] = (
    # ---- net.connections ------------------------------------------------ #
    _Scenario(
        inspector="connections",
        out_name="connections_close_wait_leak.json",
        main_stdout=(
            '{"total":1234,"established":420,"time_wait":150,"close_wait":512,'
            '"syn_sent":2,"syn_recv":1,"fin_wait":3,"listen":45}'
        ),
        expect_findings=True,
    ),
    _Scenario(
        inspector="connections",
        out_name="connections_ok.json",
        main_stdout=(
            '{"total":200,"established":120,"time_wait":30,"close_wait":2,'
            '"syn_sent":0,"syn_recv":0,"fin_wait":1,"listen":47}'
        ),
        expect_findings=False,
    ),
    # ---- net.listening_ports -------------------------------------------- #
    _Scenario(
        inspector="listening_ports",
        out_name="listening_ports_unexpected.json",
        main_stdout=(
            '{"results":['
            '{"address":"0.0.0.0","port":22,"wildcard":true,"process":"sshd"},'
            '{"address":"0.0.0.0","port":6379,"wildcard":true,"process":"redis-server"},'
            '{"address":"127.0.0.1","port":5432,"wildcard":false,"process":"postgres"}'
            "]}"
        ),
        expect_findings=True,
        parameters={"allowed_ports": [22, 443]},
    ),
    _Scenario(
        inspector="listening_ports",
        out_name="listening_ports_ok.json",
        main_stdout=(
            '{"results":['
            '{"address":"0.0.0.0","port":22,"wildcard":true,"process":"sshd"},'
            '{"address":"0.0.0.0","port":443,"wildcard":true,"process":"nginx"},'
            '{"address":"127.0.0.1","port":5432,"wildcard":false,"process":"postgres"}'
            "]}"
        ),
        expect_findings=False,
        parameters={"allowed_ports": [22, 443]},
    ),
    # ---- net.dns.resolve ------------------------------------------------ #
    _Scenario(
        inspector="dns_resolve",
        out_name="dns_resolve_failure.json",
        main_stdout=(
            '{"results":['
            '{"name":"example.com","resolved":true,"address":"93.184.216.34"},'
            '{"name":"nonexistent.invalid","resolved":false,"address":""}'
            "]}"
        ),
        expect_findings=True,
        parameters={"names": ["example.com", "nonexistent.invalid"]},
    ),
    _Scenario(
        inspector="dns_resolve",
        out_name="dns_resolve_ok.json",
        main_stdout=(
            '{"results":[{"name":"example.com","resolved":true,"address":"93.184.216.34"}]}'
        ),
        expect_findings=False,
        parameters={"names": ["example.com"]},
    ),
    # ---- net.dns.resolve — injection-safety scenario -------------------- #
    # The malicious-looking payload is rejected by the parameter `pattern`
    # (`^[a-zA-Z0-9.-]+$`) at jsonschema.validate BEFORE the command renders,
    # so this scenario uses a BENIGN name and asserts (in the snapshot test)
    # that the rendered command quotes the name via shlex.quote — the payload
    # rejection is asserted separately in the test. Recording a benign name
    # here keeps the fixture loadable; the test additionally drives a payload
    # through the runner and asserts it never reaches the shell.
    _Scenario(
        inspector="dns_resolve",
        out_name="dns_resolve_injection_safe.json",
        main_stdout=(
            '{"results":[{"name":"safe-host.example","resolved":true,"address":"10.0.0.1"}]}'
        ),
        expect_findings=False,
        parameters={"names": ["safe-host.example"]},
    ),
    # ---- net.ntp.drift -------------------------------------------------- #
    _Scenario(
        inspector="ntp_drift",
        out_name="ntp_drift_high.json",
        main_stdout=(
            '{"offset_seconds":2.345678901,"abs_offset_seconds":2.345678901,'
            '"leap_status":"Normal","synced":true}'
        ),
        expect_findings=True,
    ),
    _Scenario(
        inspector="ntp_drift",
        out_name="ntp_drift_ok.json",
        main_stdout=(
            '{"offset_seconds":0.000012345,"abs_offset_seconds":0.000012345,'
            '"leap_status":"Normal","synced":true}'
        ),
        expect_findings=False,
    ),
)


async def _record(scenario: _Scenario) -> None:
    settings = Settings()
    logger = structlog.get_logger("os-net-record")
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
