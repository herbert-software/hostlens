"""End-to-end docker-dispatch replay tests (enable-docker-inspector-targets §4).

Proves the docker dispatch path runs end-to-end for one representative inspector
per cohort class (service / runtime / process / network), per authoring-contract
§场景:docker 派发路径必须有代表性回放验证.

Strategy (design Decision 3): the collector command string is **orthogonal** to
the target type — the same recorded command replays identically whether
dispatched through a DockerTarget or an SSHTarget. So instead of recording real
container fixtures, each test reuses the inspector's existing local/ssh fixture
and flips its top-level ``impersonate`` to ``docker``. This drives
``ReplayTarget.type == "docker"`` so the runner preflight's
``target.type in manifest.targets`` gate passes for the docker-declaring
manifest, and the full ``preflight → render → collect → parse → findings`` path
runs offline, ending in ``InspectorResult.status == "ok"``.

``replay.misses == []`` on every run asserts the rendered command matches the
recorded fixture byte-for-byte — i.e. flipping ``impersonate`` does NOT perturb
the command, confirming target-type / collector orthogonality.

Per memory ``project_test_sibling_helper_import_ci`` this module imports only
from ``hostlens.*`` (no ``tests.inspectors.*`` sibling import) so console
``pytest`` (pythonpath=src, no ``tests/__init__.py``) does not crash. Snapshot
string assertions use ``.rstrip("\\n")`` to tolerate trailing-newline drift.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import structlog

import hostlens.inspectors as _inspectors_pkg
from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.result import InspectorResult
from hostlens.inspectors.runner import InspectorRunner
from hostlens.targets.registry import TargetRegistry
from hostlens.targets.replay import ReplayTarget

_FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"

# JVM/Go runtime fixtures with a sampling_window were recorded under this clock;
# only jvm.gc needs it, but using it uniformly is harmless for the others.
_FROZEN_DT = datetime(2026, 1, 2, 12, 0, 0, tzinfo=UTC)


def _builtin_root() -> Path:
    pkg_file = _inspectors_pkg.__file__
    assert pkg_file is not None
    return Path(pkg_file).parent / "builtin"


def _runner() -> InspectorRunner:
    return InspectorRunner(
        TargetRegistry(),
        settings=Settings(),
        logger=structlog.get_logger("docker-dispatch-test"),
        clock=lambda: _FROZEN_DT,
    )


def _docker_fixture(src: Path, tmp_path: Path) -> Path:
    """Copy an existing local/ssh fixture, flip ``impersonate`` to ``docker``,
    and return the path to the rewritten temp fixture. Everything else (recorded
    commands, capabilities, files) is preserved byte-for-byte."""

    data = json.loads(src.read_text(encoding="utf-8"))
    data["impersonate"] = "docker"
    dst = tmp_path / src.name
    dst.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return dst


async def _run_docker(
    manifest_rel: str,
    fixture_rel: str,
    tmp_path: Path,
    parameters: dict[str, Any] | None = None,
) -> tuple[ReplayTarget, InspectorResult]:
    manifest = load_manifest(_builtin_root() / manifest_rel)
    docker_fx = _docker_fixture(_FIXTURE_ROOT / fixture_rel, tmp_path)
    replay = ReplayTarget("docker-host", fixture=docker_fx)
    assert replay.type == "docker"
    result = await _runner().run(manifest, replay, parameters)
    return replay, result


# --------------------------------------------------------------------------- #
# 4.1 — service class representative: redis.memory_usage
# --------------------------------------------------------------------------- #


async def test_service_redis_memory_usage_docker_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # redis.memory_usage declares secrets:[HOSTLENS_REDIS_PASSWORD]; the healthy
    # fixture was recorded against a no-auth instance, so an empty value
    # reproduces the recorded (no REDISCLI_AUTH) command path.
    monkeypatch.setenv("HOSTLENS_REDIS_PASSWORD", "")

    replay, result = await _run_docker(
        "redis/memory_usage.yaml",
        "redis/memory_usage_healthy.json",
        tmp_path,
    )

    assert replay.type == "docker"
    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {"used_memory": 1042080, "maxmemory": 0, "used_pct": None}
    assert result.findings == []


# --------------------------------------------------------------------------- #
# 4.2 — runtime class representative: go.heap
# --------------------------------------------------------------------------- #


async def test_runtime_go_heap_docker_dispatch(tmp_path: Path) -> None:
    replay, result = await _run_docker(
        "go/heap.yaml",
        "go/heap_ok.json",
        tmp_path,
    )

    assert replay.type == "docker"
    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {"heap_inuse_bytes": 100000000, "heap_alloc_bytes": 80000000}
    assert result.findings == []


# --------------------------------------------------------------------------- #
# 4.3 — process class representative: linux.process.zombies (ps axo → PID ns)
# --------------------------------------------------------------------------- #


async def test_process_zombies_docker_dispatch(tmp_path: Path) -> None:
    replay, result = await _run_docker(
        "linux/process_zombies.yaml",
        "os_process/process_zombies_ok.json",
        tmp_path,
    )

    assert replay.type == "docker"
    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {"zombie_count": 0, "results": []}
    assert result.findings == []


# --------------------------------------------------------------------------- #
# 4.4 — network class representative: net.listening_ports (container netns view)
# --------------------------------------------------------------------------- #


async def test_net_listening_ports_docker_dispatch(tmp_path: Path) -> None:
    replay, result = await _run_docker(
        "net/listening_ports.yaml",
        "os_net/listening_ports_ok.json",
        tmp_path,
        parameters={"allowed_ports": [22, 443]},
    )

    assert replay.type == "docker"
    assert replay.misses == []
    assert result.status == "ok"
    assert result.findings == []


# --------------------------------------------------------------------------- #
# Snapshot of the docker dispatch cohort (4.5: .rstrip("\n") tolerance). The
# serialized {name: status} map across all four classes is the cohort snapshot —
# a single regression lock that every representative dispatched green on docker.
# --------------------------------------------------------------------------- #

_EXPECTED_SNAPSHOT = """\
go.heap=ok
linux.process.zombies=ok
net.listening_ports=ok
redis.memory_usage=ok
"""


async def test_docker_dispatch_cohort_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOSTLENS_REDIS_PASSWORD", "")

    cases: list[tuple[str, str, dict[str, Any] | None]] = [
        ("redis/memory_usage.yaml", "redis/memory_usage_healthy.json", None),
        ("go/heap.yaml", "go/heap_ok.json", None),
        ("linux/process_zombies.yaml", "os_process/process_zombies_ok.json", None),
        (
            "net/listening_ports.yaml",
            "os_net/listening_ports_ok.json",
            {"allowed_ports": [22, 443]},
        ),
    ]

    rows: list[str] = []
    for i, (manifest_rel, fixture_rel, params) in enumerate(cases):
        manifest = load_manifest(_builtin_root() / manifest_rel)
        sub = tmp_path / str(i)
        sub.mkdir()
        replay, result = await _run_docker(manifest_rel, fixture_rel, sub, params)
        assert replay.type == "docker"
        assert replay.misses == []
        rows.append(f"{manifest.name}={result.status}")

    snapshot = "\n".join(sorted(rows))
    assert snapshot.rstrip("\n") == _EXPECTED_SNAPSHOT.rstrip("\n")
