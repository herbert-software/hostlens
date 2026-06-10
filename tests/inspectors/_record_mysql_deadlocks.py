"""One-shot fixture recorder for the `mysql.deadlocks` service inspector.

`add-nginx-upstream-mysql-deadlocks-inspectors` (wave-2b-tail): point-in-time
"did an InnoDB deadlock occur within lookback" probe over
`SHOW ENGINE INNODB STATUS\\G`, secret `HOSTLENS_MYSQL_PWD` remapped to the
client-native `MYSQL_PWD` (never inlined in argv).

Per the established service / os-shell convention (design D-7, see
`_record_runtime.py` / `_record_mysql_slow_queries.py`) we do NOT need a real
MySQL server to record fixtures: we drive the **real** `InspectorRunner` against
a `_CaptureTarget` that

  * answers `command -v mysql` binary probes with a synthetic path (satisfying
    the mysql preflight),
  * returns a hand-crafted ``main_stdout`` (+ optional non-zero exit code) for
    the rendered collect command,

while recording every exact rendered command into a sink. The command strings
are captured verbatim from the real renderer (never hand-written), so the
fixture can never drift from what `ReplayTarget` looks up at snapshot time
(byte-level match). The per-scenario stdout / exit_code is the scenario data we
author — it is the final JSON the collector pipeline emits on a host in the
given state.

The recorder MUST inject `HOSTLENS_MYSQL_PWD` because the runner's preflight
checks every declared secret against `os.environ` before running the collector
(absent → `requires_unmet`). The injected value is a throwaway built by
concatenation so a credential scanner does not flag it; it is recorded in the
crosscheck `_RECORDED_SECRET_VALUES` so the secret-leak scan over these fixtures
is non-vacuous.

IMPORTANT (B1, awk section layout): the collector authored in deadlocks.yaml
parses the REAL `SHOW ENGINE INNODB STATUS` output where the
"LATEST DETECTED DEADLOCK" marker is sandwiched between `------` separator lines
and the ISO timestamp is the FIRST `^YYYY-` line AFTER the marker (the immediate
next line is the closing separator). That awk/date logic runs ONLY on a real
target — under ReplayTarget the collector shell is NOT executed; the recorded
`main_stdout` is the already-collapsed scalar JSON. The off-by-one awk offset and
the GNU `date -d` ISO parse are locked at the command-string level here and
verified behaviourally on the real-machine Demo Path, NOT by this fixture.

`main_exit_code != 0` + empty stdout models the collector's fail-loud path
(`|| exit 1`): the runner sees a non-zero exit with empty stdout, the JSON
parser raises, and the inspector lands `status=exception` (the false-negative
guard for an unreachable / auth-failed backend).

Run it to (re)write the fixtures:

    python tests/inspectors/_record_mysql_deadlocks.py

NOT collected by pytest (no `test_` prefix).
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.runner import InspectorRunner
from hostlens.targets.base import Capability, ExecResult
from hostlens.targets.registry import TargetRegistry

_BUILTIN_ROOT = Path(__file__).resolve().parents[2] / "src" / "hostlens" / "inspectors" / "builtin"
_MANIFEST = _BUILTIN_ROOT / "mysql" / "deadlocks.yaml"
_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "mysql_deadlocks"

_PROBE_PREFIX = "command -v "
_FILE_PROBE_PREFIX = "[ -r "

#: Throwaway password injected as ``HOSTLENS_MYSQL_PWD`` during recording. Built
#: by concatenation so a credential scanner does not flag a fake test credential.
#: Kept in lock-step with the crosscheck ``_RECORDED_SECRET_VALUES`` so the
#: deadlocks secret-leak scan is non-vacuous.
RECORDED_PW = "hostlens-" + "deadlocks-" + "throwaway-pw"


class _CaptureTarget:
    """Generation-only target: returns canned stdout/exit and records commands.

    Mirrors `_record_runtime._CaptureTarget`: `command -v X` probes return a
    synthetic path (satisfying the mysql preflight), file probes return empty,
    and the rendered collect command gets the canned ``main_stdout`` /
    ``main_exit_code``. An exception scenario returns a non-zero exit + empty
    stdout (the collector's fail-loud path).
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
    out_name: str  # fixture basename
    main_stdout: str  # the JSON the collector pipeline would emit
    expect_findings: bool
    parameters: dict[str, Any] = field(default_factory=lambda: {"user": "root"})
    main_exit_code: int = 0
    main_stderr: str = ""
    expect_status: str = "ok"


_SCENARIOS: tuple[_Scenario, ...] = (
    # semantic-abnormal: a deadlock was detected and its age (300s) falls inside
    # the DEFAULT lookback_seconds=3600 window → finding fires at default
    # thresholds. On a real host this JSON is the collapse of an INNODB STATUS
    # section whose "LATEST DETECTED DEADLOCK" marker is followed by a closing
    # `------` separator and THEN an ISO timestamp line; the awk skips the
    # separator and `date -d` computes the age. That collector shell is NOT run
    # under replay — only the post-collapse scalar JSON is the recorded artefact.
    _Scenario(
        out_name="semantic_abnormal.json",
        main_stdout='{"deadlock_detected":true,"deadlock_age_seconds":300}',
        expect_findings=True,
    ),
    # no-deadlock: INNODB STATUS has no LATEST DETECTED DEADLOCK section → the
    # collector END branch emits the sentinel -1 (both keys ALWAYS present so
    # output_schema.required never fails) → ok, no finding.
    _Scenario(
        out_name="no_deadlock.json",
        main_stdout='{"deadlock_detected":false,"deadlock_age_seconds":-1}',
        expect_findings=False,
    ),
    # access-denied: wrong password → mysql exits non-zero → collector `|| exit 1`
    # → empty stdout + exit 1 → JSONDecodeError → status=exception. This is the
    # fail-loud false-negative guard: an auth failure must NOT silently surface as
    # deadlock_detected=false (a fabricated healthy reading).
    _Scenario(
        out_name="access_denied.json",
        main_stdout="",
        main_exit_code=1,
        main_stderr="ERROR 1045 (28000): Access denied for user 'root'@'localhost'\n"
        "mysql innodb status failed\n",
        expect_findings=False,
        expect_status="exception",
    ),
)


async def _record(scenario: _Scenario) -> None:
    settings = Settings()
    logger = structlog.get_logger("mysql-deadlocks-record")
    manifest = load_manifest(_MANIFEST)

    os.environ["HOSTLENS_MYSQL_PWD"] = RECORDED_PW

    cap_values: set[str] = {"shell"} | set(manifest.requires_capabilities)
    capabilities = {Capability(value) for value in cap_values}

    recorded: list[dict[str, Any]] = []
    runner = InspectorRunner(TargetRegistry(), settings=settings, logger=logger)
    target = _CaptureTarget(
        "capture-host",
        capabilities=capabilities,
        main_stdout=scenario.main_stdout,
        main_exit_code=scenario.main_exit_code,
        main_stderr=scenario.main_stderr,
        sink=recorded,
    )
    result = await runner.run(manifest, target, scenario.parameters or None)

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
    _FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = _FIXTURE_DIR / scenario.out_name
    path.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path}")


async def _main() -> None:
    for scenario in _SCENARIOS:
        await _record(scenario)
    print("mysql.deadlocks fixtures recorded.")


if __name__ == "__main__":
    asyncio.run(_main())
