"""Preflight gate tests for docker-typed targets.

Covers the docker half of `enable-docker-inspector-targets`:

- step 1 (target type): a docker-typed target passes against an inspector
  declaring ``docker`` in ``targets``, and is rejected (``requires_unmet
  ["target_type"]``) against an inspector limited to ``[local, ssh]``.
- step 2 (capability fallback): an inspector requiring the ``systemd``
  capability against a docker target that does NOT advertise ``systemd``
  (the common case — most containers have no systemctl) is rejected, so a
  mis-declared host-only inspector is caught by the capability gate even if
  the per-item review missed it.

These exercise the real ``InspectorRunner._preflight`` against stub targets
(an AsyncMock ``exec`` and a controllable ``.type`` / ``.capabilities``),
mirroring ``test_runner_preflight.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import structlog

from hostlens.core.config import Settings
from hostlens.inspectors.runner import InspectorRunner
from hostlens.inspectors.schema import CollectSpec, InspectorManifest, ParseSpec
from hostlens.targets.base import Capability, ExecResult
from hostlens.targets.registry import TargetRegistry


def _logger() -> structlog.stdlib.BoundLogger:
    return structlog.get_logger("test")  # type: ignore[no-any-return]


def _runner() -> InspectorRunner:
    return InspectorRunner(TargetRegistry(), settings=Settings(), logger=_logger())


def _docker_target(*, capabilities: set[Capability] | None = None) -> Any:
    target = MagicMock()
    target.name = "dkr"
    target.type = "docker"
    target.capabilities = (
        capabilities if capabilities is not None else {Capability.SHELL, Capability.FILE_READ}
    )

    async def _exec(cmd: str, *, timeout: int, env: dict[str, str] | None = None) -> ExecResult:
        return ExecResult(exit_code=0, stdout="", stderr="", duration_seconds=0.01, timed_out=False)

    target.exec = AsyncMock(side_effect=_exec)
    return target


def _manifest(
    *,
    targets: list[str],
    requires_capabilities: list[str] | None = None,
) -> InspectorManifest:
    return InspectorManifest(
        name="test.docker_preflight",
        version="1.0.0",
        description="test",
        targets=targets,  # type: ignore[arg-type]
        requires_capabilities=requires_capabilities or [],
        collect=CollectSpec(command="echo ok"),
        parse=ParseSpec(format="raw"),
        output_schema={"type": "object"},
        findings=[],
    )


# --------------------------------------------------------------------------- #
# step 1: target type compatibility
# --------------------------------------------------------------------------- #


async def test_docker_target_passes_when_inspector_declares_docker() -> None:
    runner = _runner()
    manifest = _manifest(targets=["local", "ssh", "docker"])
    target = _docker_target()
    status, missing, _err = await runner._preflight(manifest, target, allow_privileged=False)
    assert status == "ok"
    assert missing == []


async def test_docker_target_rejected_when_inspector_local_ssh_only() -> None:
    runner = _runner()
    manifest = _manifest(targets=["local", "ssh"])
    target = _docker_target()
    status, missing, _err = await runner._preflight(manifest, target, allow_privileged=False)
    assert status == "requires_unmet"
    assert missing == ["target_type"]
    # step 1 returns immediately — no probe exec.
    assert target.exec.call_count == 0


# --------------------------------------------------------------------------- #
# step 2: capability fallback catches mis-declared host-only inspector
# --------------------------------------------------------------------------- #


async def test_systemd_capability_unmet_on_docker_without_systemctl() -> None:
    runner = _runner()
    # An inspector that declares docker but needs systemd — a mis-declaration the
    # per-item review should catch, but the capability gate backstops it.
    manifest = _manifest(targets=["docker"], requires_capabilities=["systemd"])
    target = _docker_target(capabilities={Capability.SHELL, Capability.FILE_READ})
    status, missing, _err = await runner._preflight(manifest, target, allow_privileged=False)
    assert status == "requires_unmet"
    assert missing == ["systemd"]


async def test_systemd_capability_met_when_docker_advertises_systemd() -> None:
    # Sanity counterpart: if the docker target DOES advertise systemd, the gate
    # passes — proving the rejection above is the capability check, not the type.
    runner = _runner()
    manifest = _manifest(targets=["docker"], requires_capabilities=["systemd"])
    target = _docker_target(
        capabilities={Capability.SHELL, Capability.FILE_READ, Capability.SYSTEMD}
    )
    status, missing, _err = await runner._preflight(manifest, target, allow_privileged=False)
    assert status == "ok"
    assert missing == []
