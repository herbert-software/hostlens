"""Snapshot tests for the ``docker.networks`` builtin inspector.

These run the **real** ``InspectorRunner`` against ``ReplayTarget`` fixtures
recorded from a live Docker daemon (see ``tests/inspectors/fixtures/docker/
networks_*.json``), so they exercise the full
``preflight → render → collect → parse → findings`` path **offline**.

The inspector joins ``docker network ls`` with ``docker network inspect`` inside
the collector, excludes the built-in networks (bridge / host / none), and counts
user-defined networks with an empty ``Containers`` map as dangling. It emits a
total count ``dangling_networks`` plus a top-N truncated ``results`` list. The
Finding DSL only compares the ready scalar ``dangling_networks`` against
``warn_count`` (default 1, frozen) — NO ``for_each`` (the output is list-shaped
but the finding is a pure scalar comparison; the two are orthogonal).

Recorded scenarios (see ``_record_docker_images_networks.py``):

  * ``semantic_abnormal`` — two created unattached user-defined networks
    (``hostlens-rec-net-1`` / ``-2``) → ``dangling_networks=2`` → a ``warning``
    AT THE DEFAULT threshold (warn_count=1).
  * ``healthy`` — recorded before any test network existed; the operator's
    in-use networks are not dangling → ``dangling_networks=0`` → zero findings.
  * ``daemon_down`` — the Docker daemon is unreachable so ``docker network ls``
    exits non-zero with empty stdout → ``status=exception`` (fail-loud honesty
    lock): a dead daemon must NOT be fabricated into a healthy empty set.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import ClassVar

import pytest
import structlog

from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.result import InspectorResult
from hostlens.inspectors.runner import InspectorRunner
from hostlens.targets.base import Capability, ExecResult
from hostlens.targets.registry import TargetRegistry
from hostlens.targets.replay import ReplayTarget

_MANIFEST_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "hostlens"
    / "inspectors"
    / "builtin"
    / "docker"
    / "networks.yaml"
)
_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "docker"


def _runner() -> InspectorRunner:
    return InspectorRunner(
        TargetRegistry(),
        settings=Settings(),
        logger=structlog.get_logger("docker-networks-test"),
    )


async def _run(fixture: str) -> tuple[ReplayTarget, InspectorResult]:
    manifest = load_manifest(_MANIFEST_PATH)
    replay = ReplayTarget("docker", fixture=_FIXTURE_DIR / fixture)
    result = await _runner().run(manifest, replay, {})
    return replay, result


def test_manifest_loads_cleanly() -> None:
    manifest = load_manifest(_MANIFEST_PATH)
    assert manifest.name == "docker.networks"
    assert manifest.parse.format == "json"
    assert manifest.requires_binaries == ["docker", "jq", "timeout"]
    assert manifest.secrets == []
    assert not (_MANIFEST_PATH.parent / "hook.py").exists()


async def test_semantic_abnormal_warning_at_default_thresholds() -> None:
    """Two unattached user-defined networks → dangling_networks=2 → warning AT
    THE DEFAULT threshold (warn_count=1). Asserts severity + message semantics
    (spec R5.1) and that ``results`` carries the created network names.
    """

    # No threshold override — exercises the DEFAULT warn_count=1 (frozen).
    replay, result = await _run("networks_semantic_abnormal.json")

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {
        "dangling_networks": 2,
        "results": [
            {"name": "hostlens-rec-net-1", "driver": "bridge"},
            {"name": "hostlens-rec-net-2", "driver": "bridge"},
        ],
    }
    names = {r["name"] for r in result.output["results"]}
    assert {"hostlens-rec-net-1", "hostlens-rec-net-2"} <= names
    assert [(f.severity, f.message) for f in result.findings] == [
        ("warning", "2 dangling Docker network(s) (unused user-defined)"),
    ]


def _manifest_jq_program() -> str:
    """Extract the EXACT jq program from the manifest's ``collect.command`` and
    render its single ``{{ max_results }}`` placeholder to ``50``.

    The jq program is delimited by ``jq -c '`` and its matching closing ``'`` (the
    program body uses only double quotes internally — no single quote — so the
    first ``'`` after ``jq -c '`` closes it). We assert the extracted body is a
    genuine substring of the manifest command so a future drift in the manifest's
    exclusion logic forces this test to track it (drift-proof: this is the real
    manifest jq, not a copy)."""

    command = load_manifest(_MANIFEST_PATH).collect.command
    match = re.search(r"jq -c '(.*?)'", command, flags=re.DOTALL)
    assert match is not None, command
    program = match.group(1)
    assert program in command  # drift guard: the body really is the manifest's jq
    return program.replace("{{ max_results }}", "50")


def test_jq_filter_excludes_swarm_and_builtin_networks() -> None:
    """Drive the manifest's REAL jq exclusion program directly (offline, no
    docker/swarm) over a synthetic ``docker network inspect`` array covering all
    five exclusion/inclusion classes. Unlike a ReplayTarget fixture (which replays
    the collector's POST-jq stdout — the jq never re-runs), this executes the jq
    program itself, truly locking the exclusion logic.

    Classes:
      * ``bridge``            — built-in by name → excluded
      * ``ingress``           — swarm overlay, ``.Ingress == true`` → excluded
      * ``docker_gwbridge``   — swarm bridge, excluded by name → excluded
      * ``hostlens-used-net`` — user-defined but has a container → not dangling
      * ``hostlens-dangling-net`` — user-defined, empty Containers → the ONLY
        dangling network that survives.
    """

    networks = [
        {"Name": "bridge", "Containers": {}},
        {"Name": "ingress", "Ingress": True, "Containers": {}},
        {"Name": "docker_gwbridge", "Containers": {}},
        {"Name": "hostlens-dangling-net", "Containers": {}},
        {"Name": "hostlens-used-net", "Containers": {"abc123": {"Name": "c1"}}},
    ]

    # Resolve jq via PATH (portable: Homebrew on macOS puts it at
    # /opt/homebrew/bin/jq, not /usr/bin/jq); skip cleanly when absent.
    jq = shutil.which("jq")
    if jq is None:
        pytest.skip("jq not found on PATH")
    proc = subprocess.run(
        [jq, "-c", _manifest_jq_program()],
        input=json.dumps(networks),
        capture_output=True,
        text=True,
        check=True,
    )
    out = json.loads(proc.stdout)

    assert out["dangling_networks"] == 1, out
    names = {r["name"] for r in out["results"]}
    assert names == {"hostlens-dangling-net"}, names
    for excluded in ("bridge", "ingress", "docker_gwbridge", "hostlens-used-net"):
        assert excluded not in names, (excluded, names)


async def test_healthy_no_findings() -> None:
    """No dangling user-defined networks (built-ins and in-use networks
    excluded) → dangling_networks=0 → zero findings."""

    replay, result = await _run("networks_healthy.json")

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {"dangling_networks": 0, "results": []}
    assert result.findings == []


async def test_daemon_down_fails_loud() -> None:
    """Docker daemon unreachable → status=exception, NOT a fabricated healthy
    empty set. ``docker network ls`` exits non-zero with empty stdout, so the
    runner collapses to status=exception.
    """

    replay, result = await _run("networks_daemon_down.json")

    assert replay.misses == []
    assert result.status != "ok"
    assert result.status == "exception"
    assert result.output == {}
    assert result.findings == []


async def test_negative_max_results_rejected_before_command() -> None:
    """``max_results`` has a schema lower bound (minimum: 1); a 0 or negative
    value is rejected by parameter validation BEFORE the collector renders, so a
    pathological ``[0:-1]`` jq slice (near-full table) can never happen → the run
    collapses to status=exception (bounded top-N guarantee)."""

    manifest = load_manifest(_MANIFEST_PATH)
    replay = ReplayTarget("docker", fixture=_FIXTURE_DIR / "networks_healthy.json")
    result = await _runner().run(manifest, replay, {"max_results": -1})

    assert result.status == "exception"
    assert result.error is not None
    assert result.error.startswith("parameter_validation_failed"), result.error


class _NoBinaryTarget:
    """Stub target where the named ``command -v X`` probe fails (binary absent)."""

    type = "local"
    name = "no-binary-host"
    capabilities: ClassVar[set[Capability]] = {
        Capability.SHELL,
        Capability.FILE_READ,
        Capability.DOCKER_CLI,
    }

    def __init__(self, missing: str) -> None:
        self._missing = missing

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        del timeout, env
        if cmd.startswith("command -v "):
            probed = cmd.removeprefix("command -v ").strip()
            code = 1 if probed == self._missing else 0
            return ExecResult(
                exit_code=code,
                stdout="" if code else f"/usr/bin/{probed}\n",
                stderr="",
                duration_seconds=0.0,
                timed_out=False,
            )
        raise AssertionError(f"collector must not run when {self._missing} is absent: {cmd!r}")

    async def read_file(self, path: str) -> bytes:
        raise AssertionError(f"read_file must not be reached: {path!r}")


async def _requires_unmet_for_missing(missing: str) -> InspectorResult:
    manifest = load_manifest(_MANIFEST_PATH)
    target = _NoBinaryTarget(missing)
    return await _runner().run(manifest, target, {})  # type: ignore[arg-type]


async def test_missing_docker_binary_requires_unmet() -> None:
    result = await _requires_unmet_for_missing("docker")
    assert result.status == "requires_unmet"
    assert result.findings == []
    assert any(m.startswith("bin:") for m in result.missing), result.missing


async def test_missing_jq_binary_requires_unmet() -> None:
    result = await _requires_unmet_for_missing("jq")
    assert result.status == "requires_unmet"
    assert any(m.startswith("bin:") for m in result.missing), result.missing


async def test_missing_timeout_binary_requires_unmet() -> None:
    # `timeout` (coreutils) is the NEW system-binary premise for this batch.
    result = await _requires_unmet_for_missing("timeout")
    assert result.status == "requires_unmet"
    assert any(m.startswith("bin:") for m in result.missing), result.missing
