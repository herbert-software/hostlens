"""Snapshot + contract tests for the 5 K8s control-plane builtin inspectors.

``add-k8s-control-plane-inspectors`` group 3. The inspectors
(``k8s.pods.{oom_killed,evicted,stuck_pending}`` / ``k8s.nodes.conditions`` /
``k8s.events.warnings``) are kubectl control-plane probes that run on a
management host with a kubeconfig (``targets: [local, ssh]``); their collector is
a single rendered shell script ``set -- … ; kubectl get … -o json | jq -c '…'``.

This module has three independent test layers:

1. **Replay snapshot tests** — run the real ``InspectorRunner`` against
   ``ReplayTarget`` fixtures recorded by ``_record_k8s.py`` (a ``_CaptureTarget``
   returning the author-crafted *post-jq* JSON each collector would emit). These
   lock the ``parse → output_schema → findings DSL`` path offline. Every run
   asserts ``replay.misses == []`` so a drift between the rendered command and
   the recorded fixture fails loud.

2. **Direct-jq variant tests** — extract the manifest's REAL jq program and drive
   it over crafted *raw* kubectl JSON. ``_CaptureTarget`` never executes the
   collector shell (design D-7), so the five K8s-specific input variants (events
   double-form + MicroTime, ``containerStatuses: null``, Evicted missing
   ``status.message``, node missing a condition type) are locked HERE — they are
   pre-jq raw input that the snapshot fixtures (post-jq) cannot exercise.

3. **Parameter / manifest contract tests** — injection rejection + acceptance via
   the parameter ``pattern`` (driven through the runner so the rejection proves
   the payload never reaches a shell-evaluated position), and a manifest-shape
   contract asserted against the RAW YAML source (so ``privilege: none`` /
   ``timeout_seconds`` are proven *explicit*, not schema defaults).

Per memory ``project_test_sibling_helper_import_ci`` this module imports its
sibling helper as ``from inspectors._record_k8s`` (NOT ``tests.inspectors.…``):
console ``pytest`` runs with ``pythonpath=src`` and no ``tests/__init__.py`` on
the path, so the ``tests.`` form passes locally but crashes in CI. Snapshot
string lists are tuples of ``(severity, message)`` so trailing-newline drift is
irrelevant; where a raw text body is compared it is ``.rstrip("\\n")``-normalised.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, ClassVar

import pytest
import structlog
import yaml

from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.result import InspectorResult
from hostlens.inspectors.runner import InspectorRunner
from hostlens.targets.base import Capability, ExecResult
from hostlens.targets.registry import TargetRegistry
from hostlens.targets.replay import ReplayTarget

# Sibling helper import — see module docstring (CI pythonpath convention).
from inspectors._record_k8s import _CaptureTarget  # noqa: F401  (re-export sanity)

_BUILTIN_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "hostlens" / "inspectors" / "builtin" / "k8s"
)
_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "k8s"


def _runner() -> InspectorRunner:
    return InspectorRunner(
        TargetRegistry(),
        settings=Settings(),
        logger=structlog.get_logger("k8s-test"),
    )


async def _run(
    inspector: str,
    fixture: str,
    parameters: dict[str, Any] | None = None,
) -> tuple[ReplayTarget, InspectorResult]:
    manifest = load_manifest(_BUILTIN_DIR / f"{inspector}.yaml")
    replay = ReplayTarget("k8s-host", fixture=_FIXTURE_DIR / fixture)
    result = await _runner().run(manifest, replay, parameters=parameters)
    return replay, result


def _findings(result: InspectorResult) -> list[tuple[str, str]]:
    return [(f.severity, f.message) for f in result.findings]


# --------------------------------------------------------------------------- #
# k8s.pods.oom_killed
# --------------------------------------------------------------------------- #


async def test_oom_killed_present_critical() -> None:
    replay, result = await _run("pods_oom_killed", "oom_killed_present.json")

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {
        "results": [
            {"name": "web-0", "namespace": "prod", "container": "app", "restart_count": 5},
            {"name": "worker-2", "namespace": "batch", "container": "job", "restart_count": 1},
        ]
    }
    assert _findings(result) == [
        ("critical", "container prod/web-0:app was OOMKilled (restartCount=5)"),
        ("critical", "container batch/worker-2:job was OOMKilled (restartCount=1)"),
    ]


async def test_oom_killed_empty_set_ok_no_findings() -> None:
    """kubectl succeeds with an empty match set → {"results":[]} → status=ok, no
    finding (honest empty, NOT fabricated from a failure)."""

    replay, result = await _run("pods_oom_killed", "oom_killed_empty.json")

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {"results": []}
    assert result.findings == []


async def test_oom_killed_api_unreachable_fails_loud() -> None:
    """API server unreachable → collector exits non-zero with empty stdout →
    status=exception, NOT a fabricated healthy empty set."""

    replay, result = await _run("pods_oom_killed", "oom_killed_unreachable.json")

    assert replay.misses == []
    assert result.status == "exception"
    assert result.output == {}
    assert result.findings == []


# --------------------------------------------------------------------------- #
# k8s.pods.evicted
# --------------------------------------------------------------------------- #


async def test_evicted_present_warning_including_missing_message() -> None:
    """Two evicted pods, the second missing status.message (collector jq `// ""`
    fell back to an empty summary) — both produce a warning, neither fails the
    output_schema string check (K8s-specific variant d)."""

    replay, result = await _run("pods_evicted", "evicted_present.json")

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {
        "results": [
            {
                "name": "api-7",
                "namespace": "prod",
                "message": "Pod The node was low on resource: memory.",
            },
            {"name": "api-9", "namespace": "prod", "message": ""},
        ]
    }
    assert _findings(result) == [
        (
            "warning",
            "pod prod/api-7 evicted (reason: Pod The node was low on resource: memory.)",
        ),
        ("warning", "pod prod/api-9 evicted (reason: )"),
    ]


async def test_evicted_empty_set_ok_no_findings() -> None:
    replay, result = await _run("pods_evicted", "evicted_empty.json")

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {"results": []}
    assert result.findings == []


async def test_evicted_namespace_typo_fails_loud() -> None:
    """A non-existent namespace: the collector's `kubectl get namespace` pre-check
    exits non-zero with empty stdout → status=exception (NOT a silently-blessed
    `{"results":[]}` false negative)."""

    replay, result = await _run(
        "pods_evicted", "evicted_namespace_typo.json", parameters={"namespace": "nosuchns"}
    )

    assert replay.misses == []
    assert result.status == "exception"
    assert result.output == {}
    assert result.findings == []


# --------------------------------------------------------------------------- #
# k8s.pods.stuck_pending — the full 2-D severity matrix in one fixture
# --------------------------------------------------------------------------- #


async def test_stuck_pending_severity_matrix() -> None:
    """Four rows exercise every matrix cell: unschedulable+old → critical;
    unschedulable+young → warning; pending+old → warning; young+schedulable →
    no finding. Findings are emitted rule-by-rule (the critical rule first, then
    the two warning rules) so the order is deterministic."""

    replay, result = await _run("pods_stuck_pending", "stuck_pending_matrix.json")

    assert replay.misses == []
    assert result.status == "ok"
    assert _findings(result) == [
        (
            "critical",
            "pod prod/unsched-old unschedulable for 1800s (scheduler verdict: Unschedulable)",
        ),
        (
            "warning",
            "pod prod/unsched-young unschedulable (age 120s, autoscaler may still recover)",
        ),
        (
            "warning",
            "pod prod/pending-old Pending 1800s without Unschedulable verdict "
            "(PodScheduled reason: none)",
        ),
    ]


async def test_stuck_pending_empty_set_ok_no_findings() -> None:
    """No Pending pods (the field-selector returned an empty set) → no finding —
    a Pending pod existing is itself normal; "stuck" is the alert signal."""

    replay, result = await _run("pods_stuck_pending", "stuck_pending_empty.json")

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {"results": []}
    assert result.findings == []


# --------------------------------------------------------------------------- #
# k8s.nodes.conditions
# --------------------------------------------------------------------------- #


async def test_nodes_conditions_unhealthy() -> None:
    """node-b NotReady (Ready=False) and node-c missing Ready report (Ready
    fell back to "Unknown") both produce critical; the pressure-True columns
    produce warning. Covers K8s-specific variant (e): node-b missing PIDPressure
    and node-c missing several condition types fell back to "Unknown"."""

    replay, result = await _run("nodes_conditions", "nodes_unhealthy.json")

    assert replay.misses == []
    assert result.status == "ok"
    assert _findings(result) == [
        ("critical", "node node-b not Ready (Ready=False)"),
        ("critical", "node node-c not Ready (Ready=Unknown)"),
        ("warning", "node node-b under MemoryPressure"),
        ("warning", "node node-c under DiskPressure"),
    ]


async def test_nodes_conditions_healthy_no_findings() -> None:
    replay, result = await _run("nodes_conditions", "nodes_healthy.json")

    assert replay.misses == []
    assert result.status == "ok"
    assert result.findings == []


# --------------------------------------------------------------------------- #
# k8s.events.warnings
# --------------------------------------------------------------------------- #


async def test_events_over_threshold_warning() -> None:
    """Aggregations whose summed count ≥ min_count (default 3) produce a warning;
    Events are indirect evidence so severity is never critical."""

    replay, result = await _run("events_warnings", "events_over_threshold.json")

    assert replay.misses == []
    assert result.status == "ok"
    assert _findings(result) == [
        ("warning", "5 Warning events reason=BackOff kind=Pod ns=prod"),
        ("warning", "4 Warning events reason=NodeNotReady kind=Node ns=default"),
    ]


async def test_events_below_threshold_no_findings() -> None:
    """An aggregation under min_count produces no finding (noise control)."""

    replay, result = await _run("events_warnings", "events_below_threshold.json")

    assert replay.misses == []
    assert result.status == "ok"
    assert result.findings == []


async def test_events_empty_set_ok_no_findings() -> None:
    replay, result = await _run("events_warnings", "events_empty.json")

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {"results": []}
    assert result.findings == []


# --------------------------------------------------------------------------- #
# Direct-jq variant tests — the 5 K8s-specific raw-input variants (design D5).
#
# `_CaptureTarget` replays the collector's POST-jq stdout (the jq never re-runs
# offline, D-7), so the variants below — which are PRE-jq raw kubectl JSON — are
# locked by driving the manifest's REAL jq program directly. Each test extracts
# the genuine jq program from the manifest (drift guard: a manifest change forces
# this test to track it) and runs it over crafted raw input.
# --------------------------------------------------------------------------- #


def _manifest_jq_program(stem: str) -> str:
    """Extract the EXACT jq program from a manifest's collect.command.

    The program is delimited by ``jq -c '`` and its matching closing ``'`` — the
    program bodies use only double quotes internally (verified for all five
    manifests) so the first ``'`` after ``jq -c '`` closes it. We assert the
    extracted body is a genuine substring of the manifest command so a future
    drift in the jq forces this test to track it (this is the real manifest jq,
    not a copy)."""

    command = load_manifest(_BUILTIN_DIR / f"{stem}.yaml").collect.command
    match = re.search(r"jq -c '(.*?)'", command, flags=re.DOTALL)
    assert match is not None, command
    program = match.group(1)
    assert program in command  # drift guard
    return program


def _require_jq() -> str:
    jq = shutil.which("jq")
    if jq is None:  # pragma: no cover - environment-dependent skip
        pytest.skip("jq not found on PATH")
    return jq


def _run_jq(stem: str, raw_input: dict[str, Any]) -> dict[str, Any]:
    proc = subprocess.run(
        [_require_jq(), "-c", _manifest_jq_program(stem)],
        input=json.dumps(raw_input),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)  # type: ignore[no-any-return]


def test_jq_oom_killed_handles_null_container_statuses() -> None:
    """Variant (c): a Pending pod with ``containerStatuses: null`` (and a pod
    with ``lastState: {}``) must be skipped by the jq ``//`` fallbacks without a
    parse error — only the genuine OOMKilled container survives."""

    raw = {
        "items": [
            {  # Pending pod — containerStatuses is null
                "metadata": {"name": "pending", "namespace": "prod"},
                "status": {"containerStatuses": None},
            },
            {  # genuine OOMKilled evidence
                "metadata": {"name": "web", "namespace": "prod"},
                "status": {
                    "containerStatuses": [
                        {
                            "name": "app",
                            "restartCount": 5,
                            "lastState": {"terminated": {"reason": "OOMKilled"}},
                        }
                    ]
                },
            },
            {  # never terminated — lastState is {}
                "metadata": {"name": "norm", "namespace": "prod"},
                "status": {
                    "containerStatuses": [{"name": "c", "restartCount": 0, "lastState": {}}]
                },
            },
        ]
    }

    out = _run_jq("pods_oom_killed", raw)

    assert out == {
        "results": [{"name": "web", "namespace": "prod", "container": "app", "restart_count": 5}]
    }


def test_jq_evicted_falls_back_on_missing_message() -> None:
    """Variant (d): an Evicted pod with no ``status.message`` falls back to ""
    (jq ``// ""``) — a non-Evicted/Running pod is excluded entirely."""

    raw = {
        "items": [
            {
                "metadata": {"name": "e1", "namespace": "prod"},
                "status": {"phase": "Failed", "reason": "Evicted", "message": "Node out of memory"},
            },
            {  # missing status.message
                "metadata": {"name": "e2", "namespace": "prod"},
                "status": {"phase": "Failed", "reason": "Evicted"},
            },
            {  # not evicted — excluded
                "metadata": {"name": "run", "namespace": "prod"},
                "status": {"phase": "Running"},
            },
        ]
    }

    out = _run_jq("pods_evicted", raw)

    assert out == {
        "results": [
            {"name": "e1", "namespace": "prod", "message": "Node out of memory"},
            {"name": "e2", "namespace": "prod", "message": ""},
        ]
    }


def test_jq_nodes_fall_back_to_unknown_for_missing_condition_types() -> None:
    """Variant (e): a node missing PIDPressure (old cluster) and a node missing
    Ready (kubelet lost) each fall back to "Unknown" for the absent type — no
    parse / schema error."""

    raw = {
        "items": [
            {  # missing PIDPressure
                "metadata": {"name": "n1"},
                "status": {
                    "conditions": [
                        {"type": "Ready", "status": "True"},
                        {"type": "MemoryPressure", "status": "False"},
                        {"type": "DiskPressure", "status": "False"},
                    ]
                },
            },
            {  # only MemoryPressure reported — Ready/Disk/PID fall back to Unknown
                "metadata": {"name": "n2"},
                "status": {"conditions": [{"type": "MemoryPressure", "status": "True"}]},
            },
        ]
    }

    out = _run_jq("nodes_conditions", raw)

    assert out == {
        "results": [
            {
                "name": "n1",
                "ready": "True",
                "memory_pressure": "False",
                "disk_pressure": "False",
                "pid_pressure": "Unknown",
            },
            {
                "name": "n2",
                "ready": "Unknown",
                "memory_pressure": "True",
                "disk_pressure": "Unknown",
                "pid_pressure": "Unknown",
            },
        ]
    }


def test_jq_events_double_form_and_microtime_and_cluster_scoped() -> None:
    """Variants (a)+(b): a legacy event (``count`` + ``lastTimestamp``) and a new
    events.k8s.io event (``lastTimestamp: null`` + ``eventTime`` MicroTime +
    ``series.count``) for the SAME (reason, kind, namespace) aggregate via the
    ``(.count // .series.count // 1)`` ladder; the MicroTime row does NOT perturb
    the aggregate (no time computation). A cluster-scoped Node event (empty
    ``involvedObject.namespace``) keys on the event's own ``metadata.namespace``
    (variant from D1) so it never lands an empty-string namespace mismatch."""

    raw = {
        "items": [
            {  # legacy form — count + lastTimestamp
                "reason": "BackOff",
                "involvedObject": {"kind": "Pod", "namespace": "prod"},
                "metadata": {"namespace": "prod"},
                "count": 2,
                "lastTimestamp": "2024-01-01T00:00:00Z",
            },
            {  # new form — lastTimestamp null, eventTime MicroTime, series.count
                "reason": "BackOff",
                "involvedObject": {"kind": "Pod", "namespace": "prod"},
                "metadata": {"namespace": "prod"},
                "series": {"count": 3},
                "lastTimestamp": None,
                "eventTime": "2024-01-01T00:00:00.123456Z",
            },
            {  # cluster-scoped Node — involvedObject has no namespace
                "reason": "NodeNotReady",
                "involvedObject": {"kind": "Node"},
                "metadata": {"namespace": "default"},
                "count": 1,
            },
        ]
    }

    out = _run_jq("events_warnings", raw)
    by_reason = {r["reason"]: r for r in out["results"]}

    # legacy 2 + new 3 = 5 — the MicroTime row contributed its series.count, not
    # a parse error, and did not perturb the sum.
    assert by_reason["BackOff"] == {
        "reason": "BackOff",
        "kind": "Pod",
        "namespace": "prod",
        "count": 5,
    }
    # cluster-scoped object keyed on the event's own metadata.namespace.
    assert by_reason["NodeNotReady"] == {
        "reason": "NodeNotReady",
        "kind": "Node",
        "namespace": "default",
        "count": 1,
    }


def test_jq_stuck_pending_age_and_scheduled_reason_fallback() -> None:
    """The jq computes age from ``creationTimestamp`` (``fromdateiso8601``, no GNU
    ``date``) and falls back ``// "none"`` when no PodScheduled condition exists.
    A creationTimestamp far in the past makes age deterministically huge
    regardless of ``now``, so we assert the boolean/fallback shape and a lower
    bound on age rather than an exact (non-deterministic) value."""

    raw = {
        "items": [
            {
                "metadata": {
                    "name": "old-stuck",
                    "namespace": "prod",
                    "creationTimestamp": "2020-01-01T00:00:00Z",
                },
                "status": {
                    "conditions": [
                        {"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}
                    ]
                },
            },
            {  # no conditions at all → scheduled_reason "none", unschedulable False
                "metadata": {
                    "name": "fresh",
                    "namespace": "prod",
                    "creationTimestamp": "2020-01-01T00:00:00Z",
                },
                "status": {},
            },
        ]
    }

    out = _run_jq("pods_stuck_pending", raw)
    rows = {r["name"]: r for r in out["results"]}

    assert rows["old-stuck"]["unschedulable"] is True
    assert rows["old-stuck"]["scheduled_reason"] == "Unschedulable"
    assert rows["fresh"]["unschedulable"] is False
    assert rows["fresh"]["scheduled_reason"] == "none"
    # creationTimestamp 2020 → age is many years of seconds regardless of `now`.
    assert rows["old-stuck"]["age_seconds"] > 100_000_000
    assert rows["fresh"]["age_seconds"] > 100_000_000


# --------------------------------------------------------------------------- #
# Parameter injection defense (3.3) — pattern is the second wall behind `| sh`.
#
# Driven through the runner with a probe-only target so the rejection PROVES the
# malicious value never reaches a shell-evaluated position (parameter validation
# runs before the collector renders). Acceptance cases drive a benign-but-exotic
# context/namespace through the runner against a fixture and assert it runs.
# --------------------------------------------------------------------------- #


class _ProbeOnlyTarget:
    """Answers preflight probes but fails loud on the collector command.

    Used by injection-rejection tests: preflight binary/file probes run before
    parameter validation, so they legitimately reach ``exec`` — but the rendered
    collector command (the only place a malicious context/namespace could land in
    a shell-evaluated position) must NEVER run, because validation rejects the
    payload first."""

    type = "local"
    name = "probe-only-host"
    capabilities: ClassVar[set[Capability]] = {Capability.SHELL}

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        del timeout, env
        if cmd.startswith("command -v "):
            binary = cmd[len("command -v ") :].strip().strip("'\"")
            return ExecResult(
                exit_code=0,
                stdout=f"/usr/bin/{binary}\n",
                stderr="",
                duration_seconds=0.0,
                timed_out=False,
            )
        if cmd.startswith("[ -r "):
            return ExecResult(
                exit_code=0, stdout="", stderr="", duration_seconds=0.0, timed_out=False
            )
        raise AssertionError(
            f"collector command must not be reached for a rejected payload: {cmd!r}"
        )

    async def read_file(self, path: str) -> bytes:
        raise AssertionError(f"read_file must not be reached: {path!r}")


# Inspectors that accept BOTH context and namespace (all but nodes.conditions).
_NS_INSPECTORS = ("pods_oom_killed", "pods_evicted", "pods_stuck_pending", "events_warnings")
# All five accept context.
_ALL_INSPECTORS = (*_NS_INSPECTORS, "nodes_conditions")


async def _reject(inspector: str, parameters: dict[str, Any]) -> InspectorResult:
    manifest = load_manifest(_BUILTIN_DIR / f"{inspector}.yaml")
    return await _runner().run(manifest, _ProbeOnlyTarget(), parameters=parameters)  # type: ignore[arg-type]


@pytest.mark.parametrize("inspector", _ALL_INSPECTORS)
async def test_context_injection_payload_rejected_before_command(inspector: str) -> None:
    """A shell-injection payload in ``context`` cannot satisfy
    ``^[A-Za-z0-9_.:/@-]*$`` → parameter_validation_failed BEFORE the collector
    renders (``_ProbeOnlyTarget`` asserts the collector never runs)."""

    result = await _reject(inspector, {"context": "'; kubectl delete pod x; #"})

    assert result.status == "exception"
    assert result.error is not None
    assert result.error.startswith("parameter_validation_failed"), result.error
    assert result.findings == []


@pytest.mark.parametrize("inspector", _NS_INSPECTORS)
async def test_namespace_injection_payload_rejected(inspector: str) -> None:
    """A shell-metacharacter namespace is rejected by the RFC-1123 pattern."""

    result = await _reject(inspector, {"namespace": "'; whoami; #"})

    assert result.status == "exception"
    assert result.error is not None
    assert result.error.startswith("parameter_validation_failed"), result.error


@pytest.mark.parametrize(
    "bad_namespace",
    [
        "--help",  # leading-dash flag injection of the positional pre-check slot
        "-w",  # short flag
        "a-",  # trailing dash — forbidden by the RFC-1123 real-shape pattern
    ],
)
async def test_namespace_leading_or_trailing_dash_rejected(bad_namespace: str) -> None:
    """``--help`` / ``-w`` would slip into the ``kubectl get namespace <ns>``
    pre-check's positional slot as a flag (``kubectl get namespace --help`` exits
    0 without checking anything — the false-negative bypass the pre-check exists
    to kill); ``a-`` is forbidden by the trailing-dash half of the real-shape
    pattern. All three are rejected by the pattern, not the shell."""

    result = await _reject("pods_oom_killed", {"namespace": bad_namespace})

    assert result.status == "exception"
    assert result.error is not None
    assert result.error.startswith("parameter_validation_failed"), result.error


class _AcceptingTarget:
    """Answers preflight probes and returns a fixed empty-results JSON for the
    rendered collector command (whatever it is).

    Used by parameter-ACCEPTANCE tests: a benign-but-exotic context/namespace
    perturbs the rendered command (so a ReplayTarget would miss its fixture), but
    the point of an acceptance test is to prove the value PASSES the pattern and
    drives a clean ``status=ok`` run — not to byte-match a recorded command. This
    target therefore returns ``{"results":[]}`` for any non-probe command."""

    type = "local"
    name = "accepting-host"
    capabilities: ClassVar[set[Capability]] = {Capability.SHELL}

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        del timeout, env
        if cmd.startswith("command -v "):
            binary = cmd[len("command -v ") :].strip().strip("'\"")
            stdout = f"/usr/bin/{binary}\n"
        elif cmd.startswith("[ -r "):
            stdout = ""
        else:
            stdout = '{"results":[]}'
        return ExecResult(
            exit_code=0, stdout=stdout, stderr="", duration_seconds=0.0, timed_out=False
        )

    async def read_file(self, path: str) -> bytes:
        raise AssertionError(f"read_file must not be reached: {path!r}")


async def _accept(inspector: str, parameters: dict[str, Any]) -> InspectorResult:
    manifest = load_manifest(_BUILTIN_DIR / f"{inspector}.yaml")
    return await _runner().run(manifest, _AcceptingTarget(), parameters=parameters)  # type: ignore[arg-type]


@pytest.mark.parametrize("good_namespace", ["a", "123", "kube-system", "default"])
async def test_namespace_valid_values_accepted(good_namespace: str) -> None:
    """RFC-1123 label values (alphanumeric ends, internal dashes) pass the
    pattern and drive a clean status=ok run (the payload is NOT rejected)."""

    result = await _accept("pods_oom_killed", {"namespace": good_namespace})

    assert result.status == "ok"
    assert result.output == {"results": []}


@pytest.mark.parametrize(
    "good_context",
    [
        # EKS ARN — full literal (`:` and `/` chars), NOT an ellipsis.
        "arn:aws:eks:us-east-1:123456789:cluster/prod",
        # GKE context name (`_` chars).
        "gke_proj_zone_name",
        "minikube",
    ],
)
async def test_context_exotic_but_valid_accepted(good_context: str) -> None:
    """EKS ARN (`:/` chars) and GKE (`_` chars) context names pass the pattern
    while every shell metacharacter is excluded; they drive a clean status=ok run.
    """

    result = await _accept("pods_oom_killed", {"context": good_context})

    assert result.status == "ok"
    assert result.output == {"results": []}


# --------------------------------------------------------------------------- #
# Manifest declaration-shape contract (3.6 / spec 需求1 场景:manifest 声明形态).
#
# Asserted against the RAW YAML source (yaml.safe_load) — NOT the loaded
# InspectorManifest object — because the schema defaults ``privilege`` to
# ``"none"``, so an object-level check cannot distinguish an EXPLICIT declaration
# from the default. The spec requires these be visible in the manifest source.
# --------------------------------------------------------------------------- #

# (manifest stem, expected timeout_seconds, declares-namespace-param)
_MANIFEST_SHAPE: tuple[tuple[str, int, bool], ...] = (
    ("pods_oom_killed", 30, True),
    ("pods_evicted", 30, True),
    ("pods_stuck_pending", 30, True),
    ("nodes_conditions", 30, False),
    ("events_warnings", 60, True),
)


def _raw_yaml(stem: str) -> dict[str, Any]:
    return yaml.safe_load(  # type: ignore[no-any-return]
        (_BUILTIN_DIR / f"{stem}.yaml").read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(("stem", "expected_timeout", "has_namespace"), _MANIFEST_SHAPE)
def test_manifest_declares_control_plane_shape(
    stem: str, expected_timeout: int, has_namespace: bool
) -> None:
    raw = _raw_yaml(stem)

    # privilege explicit `none` (source-level, not schema default).
    assert "privilege" in raw, f"{stem}: privilege key absent from source"
    assert raw["privilege"] == "none"

    # targets exactly [local, ssh].
    assert raw["targets"] == ["local", "ssh"], raw["targets"]

    # requires_binaries contains kubectl and jq.
    assert "kubectl" in raw["requires_binaries"]
    assert "jq" in raw["requires_binaries"]

    # collect.timeout_seconds explicit, with the class-specific value.
    assert "timeout_seconds" in raw["collect"], f"{stem}: timeout_seconds absent from source"
    assert raw["collect"]["timeout_seconds"] == expected_timeout

    # context parameter present on all five (type: object wrapper so pattern fires).
    params = raw["parameters"]
    assert params["type"] == "object"
    props = params["properties"]
    assert "context" in props
    assert props["context"]["pattern"] == "^[A-Za-z0-9_.:/@-]*$"
    assert props["context"]["default"] == ""

    # namespace parameter only on the namespaced four (NOT nodes.conditions).
    if has_namespace:
        assert "namespace" in props, f"{stem}: namespace param expected"
        assert props["namespace"]["pattern"] == "^([a-z0-9]([-a-z0-9]*[a-z0-9])?)?$"
        assert props["namespace"]["default"] == ""
    else:
        assert "namespace" not in props, f"{stem}: nodes.conditions must NOT declare namespace"


@pytest.mark.parametrize("stem", [s for s, _, _ in _MANIFEST_SHAPE])
def test_manifest_kubectl_subcommand_is_get_only(stem: str) -> None:
    """The collect.command must use ONLY the read-only ``kubectl get`` verb — no
    write verb (apply / delete / patch / edit / scale / drain / cordon) appears.
    """

    command = load_manifest(_BUILTIN_DIR / f"{stem}.yaml").collect.command

    kubectl_verbs = re.findall(r"\bkubectl\s+(\S+)", command)
    assert kubectl_verbs, f"{stem}: no kubectl invocation found"
    assert set(kubectl_verbs) == {"get"}, f"{stem}: non-get kubectl verbs: {kubectl_verbs}"

    forbidden = (
        "apply",
        "delete",
        "patch",
        "edit",
        "scale",
        "drain",
        "cordon",
        "create",
        "replace",
    )
    for verb in forbidden:
        assert f"kubectl {verb}" not in command, f"{stem}: forbidden write verb kubectl {verb}"
