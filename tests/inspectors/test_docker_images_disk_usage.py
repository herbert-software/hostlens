"""Snapshot tests for the ``docker.images.disk_usage`` builtin inspector.

These run the **real** ``InspectorRunner`` against ``ReplayTarget`` fixtures
recorded from a live Docker daemon (see ``tests/inspectors/fixtures/docker/
images_*.json``), so they exercise the full
``preflight → render → collect → parse → findings`` path **offline** — zero
Docker daemon, zero real host.

The inspector parses the percentage Docker self-reports in its ``Reclaimable``
field (e.g. ``"17.31GB (85%)"``) into the numeric ``reclaimable_pct`` — no
string→byte conversion (``size`` / ``reclaimable`` are kept verbatim as
informational strings). The Finding DSL only compares the ready scalar against
``warn_reclaimable_pct`` (default 80.0).

Recorded scenarios (dominant-image-flip recipe, see
``_record_docker_images_networks.py``):

  * ``semantic_abnormal`` — a ~12GB throwaway image left UNUSED dominates
    reclaimable disk → ``reclaimable_pct`` 85 → a ``warning`` AT THE DEFAULT
    threshold (80), a genuine high-reclaimable state, not a lowered inspector
    threshold.
  * ``healthy`` — the same big image PINNED by a running container moves from
    reclaimable to active → ``reclaimable_pct`` 21 → zero findings.
  * ``daemon_down`` — the Docker daemon is unreachable so ``docker system df``
    exits non-zero with empty stdout → ``status=exception`` (the fail-loud
    honesty lock): a dead daemon must NOT be fabricated into a healthy 0%.
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
    / "images_disk_usage.yaml"
)
_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "docker"


def _runner() -> InspectorRunner:
    return InspectorRunner(
        TargetRegistry(),
        settings=Settings(),
        logger=structlog.get_logger("docker-images-disk-usage-test"),
    )


async def _run(fixture: str) -> tuple[ReplayTarget, InspectorResult]:
    manifest = load_manifest(_MANIFEST_PATH)
    replay = ReplayTarget("docker", fixture=_FIXTURE_DIR / fixture)
    result = await _runner().run(manifest, replay, {})
    return replay, result


def test_manifest_loads_cleanly() -> None:
    manifest = load_manifest(_MANIFEST_PATH)
    assert manifest.name == "docker.images.disk_usage"
    assert manifest.parse.format == "json"
    assert manifest.requires_binaries == ["docker", "jq", "timeout"]
    assert manifest.secrets == []
    # No hook.py sibling — pure YAML.
    assert not (_MANIFEST_PATH.parent / "hook.py").exists()


def _manifest_jq_program() -> str:
    """Extract the EXACT ``jq -sc`` program from the manifest's ``collect.command``.

    The program is delimited by ``jq -sc '`` and its matching closing ``'`` (the
    program body uses only double quotes internally — no single quote — so the
    first ``'`` after ``jq -sc '`` closes it). We assert the extracted body is a
    genuine substring of the manifest command so a future drift in the manifest's
    ``// 0`` / missing-Images-row logic forces this test to track it (drift-proof:
    this is the real manifest jq, not a copy)."""

    command = load_manifest(_MANIFEST_PATH).collect.command
    match = re.search(r"jq -sc '(.*?)'", command, flags=re.DOTALL)
    assert match is not None, command
    program = match.group(1)
    assert program in command  # drift guard: the body really is the manifest's jq
    return program


def _run_jq(rows: list[dict[str, object]]) -> subprocess.CompletedProcess[str]:
    """Feed synthetic ``docker system df --format '{{json .}}'`` output (one JSON
    object per LINE, NOT an array) to the manifest's real ``jq -sc`` program — the
    ``-s`` slurps the lines into the array the program maps over, exactly as the
    collector pipes it."""

    # Resolve jq via PATH (portable: Homebrew on macOS puts it at
    # /opt/homebrew/bin/jq, not /usr/bin/jq); skip cleanly when absent rather
    # than raising FileNotFoundError on a fixed absolute path.
    jq = shutil.which("jq")
    if jq is None:
        pytest.skip("jq not found on PATH")
    stdin = "\n".join(json.dumps(row) for row in rows) + "\n"
    return subprocess.run(
        [jq, "-sc", _manifest_jq_program()],
        input=stdin,
        capture_output=True,
        text=True,
    )


def test_jq_reclaimable_zero_when_no_percent() -> None:
    """An Images row whose ``Reclaimable`` carries NO ``(NN%)`` (some docker
    versions emit a bare ``"0B"``) → the ``// 0`` fallback yields
    ``reclaimable_pct == 0`` — a TRUE "nothing to reclaim", parsed without
    error (the defensive ``// 0`` branch, otherwise unreached by the
    ReplayTarget fixtures which all carry a percent)."""

    proc = _run_jq(
        [
            {"Type": "Containers", "Reclaimable": "0B"},
            {"Type": "Images", "Reclaimable": "0B", "Size": "1.2GB"},
            {"Type": "Local Volumes", "Reclaimable": "0B"},
            {"Type": "Build Cache", "Reclaimable": "0B"},
        ]
    )

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["reclaimable_pct"] == 0


def test_jq_reclaimable_parsed_from_percent() -> None:
    """An Images row WITH a ``(59%)`` → ``reclaimable_pct == 59`` — proves the
    capture path actually parses (distinguishing a real 59 from the ``// 0``
    fallback, so ``test_jq_reclaimable_zero_when_no_percent`` is non-vacuous)."""

    proc = _run_jq([{"Type": "Images", "Reclaimable": "4.104GB (59%)", "Size": "7.261GB"}])

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["reclaimable_pct"] == 59


def test_jq_errors_when_no_images_row() -> None:
    """NO ``Type=="Images"`` row → jq ``error(...)`` → NON-ZERO exit (the
    collector then emits empty stdout → status=exception). The missing-Images-row
    anomaly must fail loud, NOT be masked into a fake 0% by the ``// 0`` (design
    D-7 boundary, otherwise unreached by the fixtures)."""

    proc = _run_jq(
        [
            {"Type": "Containers", "Reclaimable": "0B"},
            {"Type": "Local Volumes", "Reclaimable": "0B"},
        ]
    )

    assert proc.returncode != 0, proc.stdout


async def test_semantic_abnormal_warning_at_default_thresholds() -> None:
    """The unused big image dominates reclaimable disk → reclaimable_pct=85 →
    warning AT THE DEFAULT threshold (80), not a lowered inspector threshold.
    Asserts severity + message semantics (spec R5.1).
    """

    # No threshold override — exercises the DEFAULT warn_reclaimable_pct=80.
    replay, result = await _run("images_semantic_abnormal.json")

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {
        "reclaimable_pct": 85,
        "size": "20.15GB",
        "reclaimable": "17.31GB (85%)",
    }
    assert [(f.severity, f.message) for f in result.findings] == [
        (
            "warning",
            "reclaimable Docker image disk at 85% (size=20.15GB, reclaimable=17.31GB (85%))",
        ),
    ]


async def test_healthy_no_findings() -> None:
    """The big image pinned by a running container → reclaimable_pct=21 (below
    the default 80) → zero findings."""

    replay, result = await _run("images_healthy.json")

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {
        "reclaimable_pct": 21,
        "size": "20.15GB",
        "reclaimable": "4.413GB (21%)",
    }
    assert result.findings == []


async def test_daemon_down_fails_loud() -> None:
    """Docker daemon unreachable → status=exception, NOT a fabricated healthy
    0%. The honesty regression lock: ``docker system df`` exits non-zero with
    empty stdout, so the runner collapses to status=exception instead of
    blessing a dead daemon as "nothing reclaimable".
    """

    replay, result = await _run("images_daemon_down.json")

    assert replay.misses == []
    assert result.status != "ok"
    assert result.status == "exception"
    assert result.output == {}
    assert result.findings == []


class _NoBinaryTarget:
    """Stub target where the named ``command -v X`` probe fails (binary absent).

    Declares the docker_cli capability so the run is gated out ONLY by the
    missing binary (not by a capability mismatch), proving the binary-premise
    path.
    """

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
