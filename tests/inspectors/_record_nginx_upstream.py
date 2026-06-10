"""One-shot fixture recorder for ``nginx.upstream`` (dev-tool, NOT a test).

``add-nginx-upstream-mysql-deadlocks-inspectors`` group 1. ``nginx.upstream`` is
structurally identical to ``nginx.error_rate``: it scans the static path
``/var/log/nginx/error.log`` with a single ``LC_ALL=C awk`` pass, collapsing
whole-file upstream-failure events into frozen scalars (design D-4). It has NO
secret and NO deterministic exception path — failure states are
``requires_unmet`` (missing awk / unreadable log) or ``ok``.

Unlike ``_record_nginx_error_rate.py`` (which drives a docker-compose nginx and
generates REAL traffic), the upstream-failure events we need (``no live
upstreams`` / ``upstream timed out`` / ``connect() failed ... upstream``) require
an upstream pointed at a dead backend under load — awkward to provoke
deterministically in a compose lane. So we use the ``_CaptureTarget`` pattern
(lifted from ``_record_k8s.py`` / ``_record_os_net.py``): drive the **real**
``InspectorRunner`` against a target that

  * answers ``command -v awk`` / ``[ -r /var/log/nginx/error.log ]`` preflight
    probes, and
  * returns a hand-crafted ``main_stdout`` (the post-awk JSON the collector WOULD
    emit for an error.log in the given state) for the rendered collect command,

while recording every exact rendered command into a sink. Because the command
strings are captured verbatim from the real renderer (never hand-written), the
fixture can never drift from what ``ReplayTarget`` looks up at snapshot time
(byte-level match, Authoring Contract / design D-7).

IMPORTANT (D-7): ``_CaptureTarget`` NEVER executes the collector shell — awk does
not run offline. So a fixture's ``main_stdout`` is the *post-awk* output the
author crafts; these fixtures lock the **parse + findings DSL**, NOT the awk
program. The awk collapse logic (merged-regex ``total`` de-dup, per-bucket
counters, ``END{}`` zero-object) is verified separately via the real-nginx Demo
Path (proposal §Demo Path).

Records (to tests/inspectors/fixtures/nginx_upstream/):
  * empty_log.json        — empty error.log → END{} emits the zero-object →
    upstream_error_count=0 → no finding → status=ok.
  * healthy.json          — an error.log with non-upstream warnings only (no
    upstream-failure lines) → all counters 0 → no finding → status=ok.
  * semantic_abnormal.json— a REAL accumulated upstream-failure state: several
    ``no live upstreams`` / ``upstream timed out`` / ``connect() failed``
    events → upstream_error_count >= the DEFAULT warn_count → warning.

The unreadable-log path (status=requires_unmet) is NOT recorded as a fixture: it
is a preflight skip asserted directly in ``test_nginx_upstream.py`` with a stub
target whose ``[ -r ... ]`` probe fails (mirroring ``nginx.error_rate``).

Usage::

    PYTHONPATH=src python tests/inspectors/_record_nginx_upstream.py

Intentionally NOT collected by pytest (no ``test_`` prefix).
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

MANIFEST = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "hostlens"
    / "inspectors"
    / "builtin"
    / "nginx"
    / "upstream.yaml"
)
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "nginx_upstream"

_PROBE_PREFIX = "command -v "
_FILE_PROBE_PREFIX = "[ -r "


class _CaptureTarget:
    """Generation-only target: returns canned stdout and records every command.

    Binary probes (``command -v X``) succeed with a synthetic path; file probes
    (``[ -r P ]``) succeed empty; everything else is the inspector's rendered
    collector awk command and returns ``main_stdout`` (the post-awk JSON the
    author crafts — the awk program never runs offline, design D-7).
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
            {
                "cmd": cmd,
                "stdout": stdout,
                "stderr": "",
                "exit_code": 0,
                "duration_seconds": 0.0,
            }
        )
        return ExecResult(
            exit_code=0,
            stdout=stdout,
            stderr="",
            duration_seconds=0.0,
            timed_out=False,
        )

    async def read_file(self, path: str) -> bytes:  # pragma: no cover - unused here
        raise AssertionError(f"_CaptureTarget.read_file unexpectedly called: {path!r}")


def _obj(
    *,
    total: int,
    timed_out: int,
    no_live: int,
    connect: int,
    premature: int,
) -> str:
    """Render the exact post-awk JSON object the collector END{} would print."""

    return json.dumps(
        {
            "upstream_error_count": total,
            "timed_out": timed_out,
            "no_live_upstreams": no_live,
            "connect_failed": connect,
            "prematurely_closed": premature,
        },
        separators=(",", ":"),
    )


@dataclass(frozen=True)
class _Scenario:
    out_name: str
    main_stdout: str
    expect_findings: bool
    parameters: dict[str, Any] = field(default_factory=dict)


# The crafted JSON objects below are exactly the post-awk output the collector
# END{} emits for an error.log in the given state. They are the scenario data we
# author — the awk program never runs offline (design D-7).
_SCENARIOS: tuple[_Scenario, ...] = (
    # Empty error.log → END{} zero-object (no upstream events, no other lines).
    _Scenario(
        out_name="empty_log.json",
        main_stdout=_obj(total=0, timed_out=0, no_live=0, connect=0, premature=0),
        expect_findings=False,
    ),
    # An error.log with non-upstream warnings only → every counter stays 0
    # (the merged-regex `total` matches no line) → ok, no finding.
    _Scenario(
        out_name="healthy.json",
        main_stdout=_obj(total=0, timed_out=0, no_live=0, connect=0, premature=0),
        expect_findings=False,
    ),
    # A real accumulated upstream-failure state: 4 timed-out + 3 no-live + 2
    # connect-failed + 1 prematurely-closed events (total=10 unique upstream
    # lines) → upstream_error_count=10 >= default warn_count=1 → warning.
    _Scenario(
        out_name="semantic_abnormal.json",
        main_stdout=_obj(total=10, timed_out=4, no_live=3, connect=2, premature=1),
        expect_findings=True,
    ),
)


async def _record(scenario: _Scenario) -> None:
    settings = Settings()
    logger = structlog.get_logger("nginx-upstream-record")
    manifest = load_manifest(MANIFEST)

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
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / scenario.out_name
    path.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path}")


async def _main() -> None:
    for scenario in _SCENARIOS:
        await _record(scenario)


if __name__ == "__main__":
    asyncio.run(_main())
