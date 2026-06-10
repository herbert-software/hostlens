"""One-shot fixture recorder for the 5 K8s control-plane inspectors (dev-tool,
NOT collected by pytest — no ``test_`` prefix).

``add-k8s-control-plane-inspectors`` group 3. The five inspectors
(``k8s.pods.{oom_killed,evicted,stuck_pending}`` / ``k8s.nodes.conditions`` /
``k8s.events.warnings``) are kubectl control-plane probes — their collector is a
single rendered shell script (``set -- … ; kubectl get … -o json | jq -c '…'``)
that runs on a management host with a kubeconfig. We do NOT need a real cluster:
we reuse the ``_CaptureTarget`` pattern (lifted from ``_record_os_net.py``):
drive the **real** ``InspectorRunner`` against a target that

  * answers ``command -v X`` binary probes with a synthetic path, and
  * returns a hand-crafted ``main_stdout`` (the JSON the collector's jq pipeline
    WOULD emit) for the rendered collect command,

while recording every exact rendered command into a sink. Because the command
strings are captured verbatim from the real renderer (never hand-written), the
fixture can never drift from what ``ReplayTarget`` looks up at snapshot time
(byte-level match, Authoring Contract / design D-7).

IMPORTANT (D-7): ``_CaptureTarget`` NEVER executes the collector shell — kubectl
and jq do not run offline. So a fixture's ``main_stdout`` is the *post-jq* output
the author crafts; these fixtures lock the **parse + findings DSL**, NOT the
collector shell. The collector's jq logic (null fallbacks, event double-form,
aggregation) is locked SEPARATELY in ``test_k8s_inspectors.py`` by driving the
manifest's REAL jq program over crafted raw kubectl JSON; the end-to-end shell
correctness is the kind real-cluster Demo Path (tasks §5.2).

Run it to (re)write the fixtures::

    PYTHONPATH=src python tests/inspectors/_record_k8s.py
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
    Path(__file__).resolve().parents[2] / "src" / "hostlens" / "inspectors" / "builtin" / "k8s"
)
_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "k8s"

_PROBE_PREFIX = "command -v "
_FILE_PROBE_PREFIX = "[ -r "


class _CaptureTarget:
    """Generation-only target: returns canned stdout and records every command.

    Binary probes (``command -v X``) succeed with a synthetic path; file probes
    (``[ -r P ]``) succeed empty; everything else is the inspector's rendered
    collector script and returns ``main_stdout`` (the post-jq JSON the author
    crafts — the collector shell never runs offline, design D-7). When
    ``main_exit_code`` is non-zero the collector returns that code with empty
    stdout (the fail-loud path: an unreachable API server / namespace pre-check
    failure exits non-zero with no stdout → ``status=exception``).
    """

    type = "local"

    def __init__(
        self,
        name: str,
        *,
        capabilities: set[Capability],
        main_stdout: str,
        main_exit_code: int,
        sink: list[dict[str, Any]],
    ) -> None:
        self.name = name
        self.capabilities = capabilities
        self._main_stdout = main_stdout
        self._main_exit_code = main_exit_code
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
            stdout, exit_code = f"/usr/bin/{binary}\n", 0
        elif cmd.startswith(_FILE_PROBE_PREFIX):
            stdout, exit_code = "", 0
        else:
            stdout, exit_code = self._main_stdout, self._main_exit_code
        self._sink.append(
            {
                "cmd": cmd,
                "stdout": stdout,
                "stderr": "",
                "exit_code": exit_code,
                "duration_seconds": 0.0,
            }
        )
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr="",
            duration_seconds=0.0,
            timed_out=False,
        )

    async def read_file(self, path: str) -> bytes:  # pragma: no cover - unused here
        raise AssertionError(f"_CaptureTarget.read_file unexpectedly called: {path!r}")


@dataclass(frozen=True)
class _Scenario:
    inspector: str  # manifest file stem under builtin/k8s/
    out_name: str  # fixture basename
    main_stdout: str  # the post-jq JSON the collector pipeline would emit
    expect_findings: bool
    parameters: dict[str, Any] = field(default_factory=dict)
    main_exit_code: int = 0
    expect_status: str = "ok"


# The crafted JSON objects below are exactly the post-jq output each inspector's
# collector emits on a cluster in the given state. They are the scenario data we
# author — the collector shell (kubectl + jq) never runs offline (design D-7).
_SCENARIOS: tuple[_Scenario, ...] = (
    # ---- k8s.pods.oom_killed -------------------------------------------- #
    _Scenario(
        inspector="pods_oom_killed",
        out_name="oom_killed_present.json",
        main_stdout=json.dumps(
            {
                "results": [
                    {"name": "web-0", "namespace": "prod", "container": "app", "restart_count": 5},
                    {
                        "name": "worker-2",
                        "namespace": "batch",
                        "container": "job",
                        "restart_count": 1,
                    },
                ]
            }
        ),
        expect_findings=True,
    ),
    _Scenario(
        inspector="pods_oom_killed",
        out_name="oom_killed_empty.json",
        main_stdout=json.dumps({"results": []}),
        expect_findings=False,
    ),
    _Scenario(
        inspector="pods_oom_killed",
        out_name="oom_killed_unreachable.json",
        main_stdout="",
        main_exit_code=1,
        expect_findings=False,
        expect_status="exception",
    ),
    # ---- k8s.pods.evicted ----------------------------------------------- #
    # Includes the K8s-specific variant (d): an Evicted pod missing status.message
    # whose collector fell back to "" (jq `// ""`).
    _Scenario(
        inspector="pods_evicted",
        out_name="evicted_present.json",
        main_stdout=json.dumps(
            {
                "results": [
                    {
                        "name": "api-7",
                        "namespace": "prod",
                        "message": "Pod The node was low on resource: memory.",
                    },
                    {"name": "api-9", "namespace": "prod", "message": ""},
                ]
            }
        ),
        expect_findings=True,
    ),
    _Scenario(
        inspector="pods_evicted",
        out_name="evicted_empty.json",
        main_stdout=json.dumps({"results": []}),
        expect_findings=False,
    ),
    _Scenario(
        inspector="pods_evicted",
        out_name="evicted_namespace_typo.json",
        main_stdout="",
        main_exit_code=1,
        expect_findings=False,
        expect_status="exception",
        parameters={"namespace": "nosuchns"},
    ),
    # ---- k8s.pods.stuck_pending ----------------------------------------- #
    # Three rows drive the full 2-D severity matrix in one fixture:
    #   * unschedulable & age>threshold      -> critical
    #   * unschedulable & age<=threshold     -> warning
    #   * not unschedulable & age>threshold  -> warning
    # A 4th "young & schedulable" row produces NO finding (normal scheduling).
    _Scenario(
        inspector="pods_stuck_pending",
        out_name="stuck_pending_matrix.json",
        main_stdout=json.dumps(
            {
                "results": [
                    {
                        "name": "unsched-old",
                        "namespace": "prod",
                        "age_seconds": 1800,
                        "unschedulable": True,
                        "scheduled_reason": "Unschedulable",
                    },
                    {
                        "name": "unsched-young",
                        "namespace": "prod",
                        "age_seconds": 120,
                        "unschedulable": True,
                        "scheduled_reason": "Unschedulable",
                    },
                    {
                        "name": "pending-old",
                        "namespace": "prod",
                        "age_seconds": 1800,
                        "unschedulable": False,
                        "scheduled_reason": "none",
                    },
                    {
                        "name": "creating-young",
                        "namespace": "prod",
                        "age_seconds": 30,
                        "unschedulable": False,
                        "scheduled_reason": "none",
                    },
                ]
            }
        ),
        expect_findings=True,
    ),
    _Scenario(
        inspector="pods_stuck_pending",
        out_name="stuck_pending_empty.json",
        main_stdout=json.dumps({"results": []}),
        expect_findings=False,
    ),
    # ---- k8s.nodes.conditions ------------------------------------------- #
    # Includes K8s-specific variant (e): a node missing the PIDPressure (and
    # Ready) condition type whose collector fell back to "Unknown" (jq `// `).
    _Scenario(
        inspector="nodes_conditions",
        out_name="nodes_unhealthy.json",
        main_stdout=json.dumps(
            {
                "results": [
                    {
                        "name": "node-a",
                        "ready": "True",
                        "memory_pressure": "False",
                        "disk_pressure": "False",
                        "pid_pressure": "False",
                    },
                    {
                        "name": "node-b",
                        "ready": "False",
                        "memory_pressure": "True",
                        "disk_pressure": "False",
                        "pid_pressure": "Unknown",
                    },
                    {
                        "name": "node-c",
                        "ready": "Unknown",
                        "memory_pressure": "Unknown",
                        "disk_pressure": "True",
                        "pid_pressure": "Unknown",
                    },
                ]
            }
        ),
        expect_findings=True,
    ),
    _Scenario(
        inspector="nodes_conditions",
        out_name="nodes_healthy.json",
        main_stdout=json.dumps(
            {
                "results": [
                    {
                        "name": "node-a",
                        "ready": "True",
                        "memory_pressure": "False",
                        "disk_pressure": "False",
                        "pid_pressure": "False",
                    }
                ]
            }
        ),
        expect_findings=False,
    ),
    # ---- k8s.events.warnings -------------------------------------------- #
    # The post-jq output already carries the summed count; the K8s-specific
    # event double-form / MicroTime / cluster-scoped variants are pre-jq raw
    # input and are locked by the direct-jq test, not by this fixture.
    _Scenario(
        inspector="events_warnings",
        out_name="events_over_threshold.json",
        main_stdout=json.dumps(
            {
                "results": [
                    {"reason": "BackOff", "kind": "Pod", "namespace": "prod", "count": 5},
                    {"reason": "NodeNotReady", "kind": "Node", "namespace": "default", "count": 4},
                ]
            }
        ),
        expect_findings=True,
    ),
    _Scenario(
        inspector="events_warnings",
        out_name="events_below_threshold.json",
        main_stdout=json.dumps(
            {"results": [{"reason": "BackOff", "kind": "Pod", "namespace": "prod", "count": 2}]}
        ),
        expect_findings=False,
    ),
    _Scenario(
        inspector="events_warnings",
        out_name="events_empty.json",
        main_stdout=json.dumps({"results": []}),
        expect_findings=False,
    ),
)


async def _record(scenario: _Scenario) -> None:
    settings = Settings()
    logger = structlog.get_logger("k8s-record")
    manifest = load_manifest(_BUILTIN_DIR / f"{scenario.inspector}.yaml")

    cap_values: set[str] = {"shell"} | set(manifest.requires_capabilities)
    capabilities = {Capability(value) for value in cap_values}

    recorded: list[dict[str, Any]] = []
    runner = InspectorRunner(TargetRegistry(), settings=settings, logger=logger)
    target = _CaptureTarget(
        "capture-host",
        capabilities=capabilities,
        main_stdout=scenario.main_stdout,
        main_exit_code=scenario.main_exit_code,
        sink=recorded,
    )
    result = await runner.run(manifest, target, parameters=scenario.parameters or None)

    assert result.status == scenario.expect_status, (
        f"{scenario.out_name}: status={result.status} (expected "
        f"{scenario.expect_status}) error={result.error}"
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
