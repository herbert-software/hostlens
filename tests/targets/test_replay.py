"""Behavioural tests for :class:`hostlens.targets.replay.ReplayTarget`.

Spec: ``openspec/changes/add-incident-pack/specs/replay-execution-target/spec.md``

These tests exercise the **real** ReplayTarget against on-disk JSON fixtures
(written to ``tmp_path``) — no mocking of subprocess / filesystem. The whole
point of ReplayTarget is that no real subprocess or file access ever happens,
so the tests also assert that a miss never reaches the host (it raises
``ReplayMiss`` for an obviously-absent path / command instead of touching the
real FS).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hostlens.core.config import Settings
from hostlens.core.exceptions import ConfigError, ReplayMiss
from hostlens.targets.base import Capability, ExecResult
from hostlens.targets.config import load_targets_config
from hostlens.targets.registry import build_registry_from_config
from hostlens.targets.replay import ReplayTarget


def _write_fixture(tmp_path: Path, data: dict[str, Any], name: str = "fixture.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return path


_BASE_FIXTURE: dict[str, Any] = {
    "impersonate": "local",
    "capabilities": ["shell", "file_read"],
    "commands": [
        {
            "cmd": "cat /proc/loadavg",
            "stdout": "0.50 0.40 0.30 1/200 12345\n",
            "stderr": "",
            "exit_code": 0,
            "duration_seconds": 0.01,
        }
    ],
    "files": {"/etc/hostname": "incident-host\n"},
}


# --------------------------------------------------------------------------- #
# exec hit / miss
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_exec_hit_returns_recorded_result(tmp_path: Path) -> None:
    fixture = _write_fixture(tmp_path, _BASE_FIXTURE)
    target = ReplayTarget(name="replay-host", fixture=fixture)

    result = await target.exec("cat /proc/loadavg", timeout=10)

    assert isinstance(result, ExecResult)
    assert result.stdout == "0.50 0.40 0.30 1/200 12345\n"
    assert result.stderr == ""
    assert result.exit_code == 0
    assert result.duration_seconds == 0.01
    assert result.timed_out is False
    assert target.misses == []


@pytest.mark.asyncio
async def test_exec_hit_ignores_trailing_whitespace_per_line(tmp_path: Path) -> None:
    fixture = _write_fixture(tmp_path, _BASE_FIXTURE)
    target = ReplayTarget(name="replay-host", fixture=fixture)

    # Per-line trailing whitespace is normalised by the match key, so the same
    # single-line command with trailing spaces still hits.
    result = await target.exec("cat /proc/loadavg   ", timeout=10)
    assert result.stdout == "0.50 0.40 0.30 1/200 12345\n"
    assert target.misses == []


@pytest.mark.asyncio
async def test_exec_hit_multiline_command_per_line_rstrip(tmp_path: Path) -> None:
    # The match key rstrips each line independently then re-joins, so trailing
    # whitespace on any line of a multi-line command is normalised.
    data = {
        **_BASE_FIXTURE,
        "commands": [{"cmd": "line1\nline2", "stdout": "ok\n", "stderr": "", "exit_code": 0}],
    }
    fixture = _write_fixture(tmp_path, data, name="multiline.json")
    target = ReplayTarget(name="replay-host", fixture=fixture)

    result = await target.exec("line1  \nline2\t", timeout=10)
    assert result.stdout == "ok\n"
    assert target.misses == []


@pytest.mark.asyncio
async def test_exec_env_accepted_but_not_part_of_match(tmp_path: Path) -> None:
    fixture = _write_fixture(tmp_path, _BASE_FIXTURE)
    target = ReplayTarget(name="replay-host", fixture=fixture)

    result = await target.exec("cat /proc/loadavg", timeout=10, env={"SECRET": "x"})
    assert result.stdout == "0.50 0.40 0.30 1/200 12345\n"
    assert target.misses == []


@pytest.mark.asyncio
async def test_exec_miss_raises_replay_miss_and_records(tmp_path: Path) -> None:
    fixture = _write_fixture(tmp_path, _BASE_FIXTURE)
    target = ReplayTarget(name="replay-host", fixture=fixture)

    # An obviously-real command that would have side effects if executed —
    # proving no real subprocess is ever spawned on a miss.
    with pytest.raises(ReplayMiss) as exc_info:
        await target.exec("touch /tmp/replay_should_never_run", timeout=10)

    assert exc_info.value.kind == "exec"
    assert exc_info.value.cmd == "touch /tmp/replay_should_never_run"
    assert not Path("/tmp/replay_should_never_run").exists()
    # Miss recorded even though it also raised — strict-consumption guard.
    assert target.misses == [{"kind": "exec", "cmd": "touch /tmp/replay_should_never_run"}]


# --------------------------------------------------------------------------- #
# read_file hit / miss
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_read_file_hit_returns_recorded_bytes(tmp_path: Path) -> None:
    fixture = _write_fixture(tmp_path, _BASE_FIXTURE)
    target = ReplayTarget(name="replay-host", fixture=fixture)

    content = await target.read_file("/etc/hostname")
    assert content == b"incident-host\n"
    assert target.misses == []


@pytest.mark.asyncio
async def test_read_file_miss_raises_replay_miss_and_records(tmp_path: Path) -> None:
    fixture = _write_fixture(tmp_path, _BASE_FIXTURE)
    target = ReplayTarget(name="replay-host", fixture=fixture)

    with pytest.raises(ReplayMiss) as exc_info:
        await target.read_file("/etc/passwd")

    assert exc_info.value.kind == "read_file"
    assert exc_info.value.cmd == "/etc/passwd"
    assert target.misses == [{"kind": "read_file", "cmd": "/etc/passwd"}]


# --------------------------------------------------------------------------- #
# runtime type impersonation + capabilities
# --------------------------------------------------------------------------- #


def test_runtime_type_defaults_to_local(tmp_path: Path) -> None:
    data = dict(_BASE_FIXTURE)
    data.pop("impersonate", None)
    fixture = _write_fixture(tmp_path, data)
    target = ReplayTarget(name="replay-host", fixture=fixture)
    assert target.type == "local"


def test_runtime_type_equals_impersonate_declaration(tmp_path: Path) -> None:
    data = {**_BASE_FIXTURE, "impersonate": "ssh"}
    fixture = _write_fixture(tmp_path, data)
    target = ReplayTarget(name="replay-host", fixture=fixture)
    assert target.type == "ssh"


def test_impersonate_docker_loads_and_type_is_docker(tmp_path: Path) -> None:
    # enable-docker-inspector-targets: `docker` 进入 impersonate 取值域, 使 docker
    # 派发路径可被离线回放 (runner preflight `target.type in manifest.targets` 透明通过).
    data = {**_BASE_FIXTURE, "impersonate": "docker"}
    fixture = _write_fixture(tmp_path, data)
    target = ReplayTarget(name="replay-host", fixture=fixture)
    assert target.type == "docker"


@pytest.mark.parametrize("impersonate", ["k8s", "kubernetes", "replay"])
def test_impersonate_unimplemented_type_rejected(tmp_path: Path, impersonate: str) -> None:
    # impersonate 只能冒充已实现的 target 类型; 冒充未实现类型造成 preflight 假性通过,
    # 故 fixture 加载期必须 raise (fail-loud).
    data = {**_BASE_FIXTURE, "impersonate": impersonate}
    fixture = _write_fixture(tmp_path, data)
    with pytest.raises(ConfigError) as exc_info:
        ReplayTarget(name="replay-host", fixture=fixture)
    assert exc_info.value.kind == "replay_fixture_invalid"


def test_capabilities_equal_fixture_declaration(tmp_path: Path) -> None:
    data = {**_BASE_FIXTURE, "capabilities": ["shell", "systemd"]}
    fixture = _write_fixture(tmp_path, data)
    target = ReplayTarget(name="replay-host", fixture=fixture)
    assert target.capabilities == {Capability.SHELL, Capability.SYSTEMD}


# --------------------------------------------------------------------------- #
# config-driven construction
# --------------------------------------------------------------------------- #


def test_build_from_config(tmp_path: Path) -> None:
    fixture = _write_fixture(tmp_path, _BASE_FIXTURE)
    targets_yaml = tmp_path / "targets.yaml"
    targets_yaml.write_text(
        f"version: '1'\ntargets:\n  - name: replay-host\n    type: replay\n    fixture: {fixture}\n"
    )

    config = load_targets_config(targets_yaml)
    registry = build_registry_from_config(config, Settings())

    target = registry.get("replay-host")
    assert isinstance(target, ReplayTarget)
    assert target.type == "local"
    assert target.capabilities == {Capability.SHELL, Capability.FILE_READ}


@pytest.mark.asyncio
async def test_build_from_config_target_is_usable(tmp_path: Path) -> None:
    fixture = _write_fixture(tmp_path, _BASE_FIXTURE)
    targets_yaml = tmp_path / "targets.yaml"
    targets_yaml.write_text(
        f"version: '1'\ntargets:\n  - name: replay-host\n    type: replay\n    fixture: {fixture}\n"
    )

    registry = build_registry_from_config(load_targets_config(targets_yaml), Settings())
    target = registry.get("replay-host")

    result = await target.exec("cat /proc/loadavg", timeout=10, env={"X": "y"})
    assert result.stdout == "0.50 0.40 0.30 1/200 12345\n"


# --------------------------------------------------------------------------- #
# strict-consumption across a runner run + ReplayMiss not mapped to
# target_unreachable
# --------------------------------------------------------------------------- #


def _make_manifest(command: str, *, requires_binaries: list[str] | None = None) -> Any:
    """Build a minimal local-target InspectorManifest for runner integration."""

    from hostlens.inspectors.schema import InspectorManifest

    manifest_dict: dict[str, Any] = {
        "name": "test.replay.probe",
        "version": "1.0.0",
        "description": "replay integration probe",
        "targets": ["local"],
        "collect": {"command": command, "timeout_seconds": 5},
        "parse": {"format": "raw"},
        "output_schema": {
            "type": "object",
            "properties": {"raw": {"type": "string"}},
        },
    }
    if requires_binaries:
        manifest_dict["requires_binaries"] = requires_binaries
    return InspectorManifest.model_validate(manifest_dict)


def _make_runner(registry: Any) -> Any:
    import structlog

    from hostlens.inspectors.runner import InspectorRunner

    return InspectorRunner(
        registry,
        settings=Settings(),
        logger=structlog.get_logger("test"),
    )


@pytest.mark.asyncio
async def test_all_hits_leaves_misses_empty(tmp_path: Path) -> None:
    fixture = _write_fixture(tmp_path, _BASE_FIXTURE)
    target = ReplayTarget(name="replay-host", fixture=fixture)

    # Two hits, no probes (no requires_binaries) → misses stays empty.
    await target.exec("cat /proc/loadavg", timeout=10)
    await target.read_file("/etc/hostname")
    assert target.misses == []


@pytest.mark.asyncio
async def test_replay_miss_not_mapped_to_target_unreachable(tmp_path: Path) -> None:
    # Fixture records the preflight probe but NOT the main command → the main
    # exec misses. Because ReplayMiss inherits HostlensError (not TargetError),
    # the runner's ``except TargetError`` must NOT catch it: it propagates out
    # of run() rather than becoming a status=target_unreachable result.
    data: dict[str, Any] = {
        "impersonate": "local",
        "capabilities": ["shell"],
        "commands": [
            {"cmd": "command -v cat", "stdout": "/usr/bin/cat", "stderr": "", "exit_code": 0}
        ],
        "files": {},
    }
    fixture = _write_fixture(tmp_path, data)
    target = ReplayTarget(name="replay-host", fixture=fixture)
    runner = _make_runner(_empty_registry())

    manifest = _make_manifest("cat /proc/loadavg", requires_binaries=["cat"])

    with pytest.raises(ReplayMiss):
        await runner.run(manifest, target)

    assert target.misses == [{"kind": "exec", "cmd": "cat /proc/loadavg"}]


@pytest.mark.asyncio
async def test_full_run_all_hits_misses_empty(tmp_path: Path) -> None:
    # Fixture records both the preflight probe and the main command → a full
    # runner.run consumes everything and ``target.misses`` is empty.
    data: dict[str, Any] = {
        "impersonate": "local",
        "capabilities": ["shell"],
        "commands": [
            {"cmd": "command -v cat", "stdout": "/usr/bin/cat", "stderr": "", "exit_code": 0},
            {
                "cmd": "cat /proc/loadavg",
                "stdout": "0.5 0.4 0.3 1/200 1\n",
                "stderr": "",
                "exit_code": 0,
            },
        ],
        "files": {},
    }
    fixture = _write_fixture(tmp_path, data)
    target = ReplayTarget(name="replay-host", fixture=fixture)
    runner = _make_runner(_empty_registry())

    manifest = _make_manifest("cat /proc/loadavg", requires_binaries=["cat"])

    result = await runner.run(manifest, target)

    assert result.status == "ok"
    assert target.misses == []


def _empty_registry() -> Any:
    from hostlens.targets.registry import TargetRegistry

    return TargetRegistry()
