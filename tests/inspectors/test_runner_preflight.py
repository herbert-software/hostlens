"""Tests for `InspectorRunner._preflight`.

The 6 preflight steps run in fixed order. For each `requires_unmet` cause
we verify: (a) the correct `missing` list is returned, (b) the order
contract holds (cheaper steps fail before more expensive probes), (c)
``shlex.quote`` wraps every binary / file path before string substitution
into the probe command — including the adversarial case where a manifest
field-layer regex would have been bypassed (we use
``InspectorManifest.model_construct`` to skip Pydantic validation and
inject a payload that contains shell metacharacters).
"""

from __future__ import annotations

import shlex
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

from hostlens.core.config import Settings
from hostlens.inspectors.runner import InspectorRunner
from hostlens.inspectors.schema import (
    CollectSpec,
    InspectorManifest,
    ParseSpec,
)
from hostlens.targets.base import Capability, ExecResult
from hostlens.targets.registry import TargetRegistry


def _logger() -> structlog.stdlib.BoundLogger:
    return structlog.get_logger("test")  # type: ignore[no-any-return]


def _runner() -> InspectorRunner:
    return InspectorRunner(
        TargetRegistry(),
        settings=Settings(),
        logger=_logger(),
    )


def _make_target(
    *,
    name: str = "t1",
    type_: str = "local",
    capabilities: set[Capability] | None = None,
    exec_results: dict[str, ExecResult] | None = None,
    default_exit: int = 0,
) -> Any:
    """Build a stub ExecutionTarget with a counting AsyncMock exec.

    `exec_results` maps a substring (matched via `in`) to the result; first
    match wins. Any unmatched cmd returns ExecResult with `default_exit`.
    """

    target = MagicMock()
    target.name = name
    target.type = type_
    target.capabilities = capabilities if capabilities is not None else set()

    async def _exec(cmd: str, *, timeout: int, env: dict[str, str] | None = None) -> ExecResult:
        if exec_results:
            for substring, result in exec_results.items():
                if substring in cmd:
                    return result
        return ExecResult(
            exit_code=default_exit,
            stdout="",
            stderr="",
            duration_seconds=0.01,
            timed_out=False,
        )

    target.exec = AsyncMock(side_effect=_exec)
    return target


def _make_manifest(
    *,
    targets: list[str] | None = None,
    requires_capabilities: list[str] | None = None,
    requires_binaries: list[str] | None = None,
    requires_files: list[str] | None = None,
    privilege: str = "none",
    secrets: list[str] | None = None,
) -> InspectorManifest:
    return InspectorManifest(
        name="test.preflight",
        version="1.0.0",
        description="test",
        targets=targets or ["local"],  # type: ignore[arg-type]
        requires_capabilities=requires_capabilities or [],
        requires_binaries=requires_binaries or [],
        requires_files=requires_files or [],
        privilege=privilege,  # type: ignore[arg-type]
        secrets=secrets or [],
        collect=CollectSpec(command="echo ok"),
        parse=ParseSpec(format="raw"),
        output_schema={"type": "object"},
        findings=[],
    )


# ---------------------------------------------------------------------- #
# Step 1: target type mismatch
# ---------------------------------------------------------------------- #


async def test_target_type_incompatible_returns_target_type() -> None:
    runner = _runner()
    manifest = _make_manifest(targets=["ssh"])
    target = _make_target(type_="local")
    status, missing, _err = await runner._preflight(manifest, target, allow_privileged=False)
    assert status == "requires_unmet"
    assert missing == ["target_type"]
    # exec should NOT have been called — step 1 returns immediately.
    assert target.exec.call_count == 0


# ---------------------------------------------------------------------- #
# Step 2: capabilities missing — and order priority over binaries
# ---------------------------------------------------------------------- #


async def test_missing_capability_returns_sorted_caps() -> None:
    runner = _runner()
    manifest = _make_manifest(requires_capabilities=["systemd"])
    target = _make_target(capabilities={Capability.SHELL, Capability.FILE_READ})
    status, missing, _err = await runner._preflight(manifest, target, allow_privileged=False)
    assert status == "requires_unmet"
    assert missing == ["systemd"]


async def test_capability_step_runs_before_binary_step() -> None:
    """When both capability and binary are missing, step 2 wins."""

    runner = _runner()
    manifest = _make_manifest(
        requires_capabilities=["systemd"],
        requires_binaries=["nginx"],
    )
    target = _make_target(
        capabilities={Capability.SHELL},
        exec_results={
            "command -v nginx": ExecResult(
                exit_code=127,
                stdout="",
                stderr="",
                duration_seconds=0.01,
                timed_out=False,
            )
        },
    )
    status, missing, _err = await runner._preflight(manifest, target, allow_privileged=False)
    assert status == "requires_unmet"
    assert missing == ["systemd"]
    # Binary probe must NOT have run since step 2 returned early.
    assert target.exec.call_count == 0


# ---------------------------------------------------------------------- #
# Step 3: privilege opt-in
# ---------------------------------------------------------------------- #


async def test_privilege_required_without_opt_in() -> None:
    runner = _runner()
    manifest = _make_manifest(privilege="sudo")
    target = _make_target(capabilities={Capability.SHELL})
    status, missing, _err = await runner._preflight(manifest, target, allow_privileged=False)
    assert status == "requires_unmet"
    assert missing == ["privilege_opt_in"]


async def test_privilege_required_with_opt_in_passes() -> None:
    runner = _runner()
    manifest = _make_manifest(privilege="sudo")
    target = _make_target(capabilities={Capability.SHELL})
    status, missing, _err = await runner._preflight(manifest, target, allow_privileged=True)
    assert status == "ok"
    assert missing == []


# ---------------------------------------------------------------------- #
# Step 4: env secrets
# ---------------------------------------------------------------------- #


async def test_env_secret_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PGPASSWORD", raising=False)
    runner = _runner()
    manifest = _make_manifest(secrets=["PGPASSWORD"])
    target = _make_target(capabilities={Capability.SHELL})
    status, missing, _err = await runner._preflight(manifest, target, allow_privileged=False)
    assert status == "requires_unmet"
    assert missing == ["env:PGPASSWORD"]


async def test_env_secret_collects_all_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VAR_A", raising=False)
    monkeypatch.delenv("VAR_B", raising=False)
    monkeypatch.setenv("VAR_C", "ok")
    runner = _runner()
    manifest = _make_manifest(secrets=["VAR_A", "VAR_B", "VAR_C"])
    target = _make_target(capabilities={Capability.SHELL})
    status, missing, _err = await runner._preflight(manifest, target, allow_privileged=False)
    assert status == "requires_unmet"
    # Both missing secrets are reported; the present one is not.
    assert set(missing) == {"env:VAR_A", "env:VAR_B"}


# ---------------------------------------------------------------------- #
# Step 5: binary probes
# ---------------------------------------------------------------------- #


async def test_binary_missing() -> None:
    runner = _runner()
    manifest = _make_manifest(requires_binaries=["nginx"])
    target = _make_target(
        capabilities={Capability.SHELL},
        exec_results={
            "command -v nginx": ExecResult(
                exit_code=1,
                stdout="",
                stderr="",
                duration_seconds=0.01,
                timed_out=False,
            ),
        },
    )
    status, missing, _err = await runner._preflight(manifest, target, allow_privileged=False)
    assert status == "requires_unmet"
    assert missing == ["bin:nginx"]


async def test_binary_probe_uses_shlex_quote() -> None:
    """Even a legal binary name must be shlex.quote-d before substitution."""

    runner = _runner()
    manifest = _make_manifest(requires_binaries=["echo"])
    target = _make_target(capabilities={Capability.SHELL})
    await runner._preflight(manifest, target, allow_privileged=False)
    # The exec arg must be shlex.quote-d.
    args, _kwargs = target.exec.call_args
    cmd = args[0]
    assert cmd == f"command -v {shlex.quote('echo')}"


# ---------------------------------------------------------------------- #
# Step 6: file probes — incl. shlex.quote defense-in-depth
# ---------------------------------------------------------------------- #


async def test_file_missing() -> None:
    runner = _runner()
    manifest = _make_manifest(requires_files=["/etc/nginx/nginx.conf"])
    target = _make_target(
        capabilities={Capability.SHELL},
        exec_results={
            "/etc/nginx/nginx.conf": ExecResult(
                exit_code=1,
                stdout="",
                stderr="",
                duration_seconds=0.01,
                timed_out=False,
            ),
        },
    )
    status, missing, _err = await runner._preflight(manifest, target, allow_privileged=False)
    assert status == "requires_unmet"
    assert missing == ["file:/etc/nginx/nginx.conf"]


async def test_file_probe_uses_shlex_quote_for_legal_path() -> None:
    runner = _runner()
    manifest = _make_manifest(requires_files=["/tmp/a"])
    target = _make_target(capabilities={Capability.SHELL})
    await runner._preflight(manifest, target, allow_privileged=False)
    args, _kwargs = target.exec.call_args
    cmd = args[0]
    # shlex.quote wraps `/tmp/a` in single quotes (or nothing on POSIX safe
    # input — but shlex.quote always returns the literal as-is for safe
    # paths). Verify exact string.
    assert cmd == f"[ -r {shlex.quote('/tmp/a')} ]"


async def test_file_probe_shlex_quote_defense_against_bypass() -> None:
    """Adversarial: use model_construct to inject a payload past Pydantic.

    If runner concatenated the path without `shlex.quote`, the `; rm -rf /`
    suffix would execute as a separate shell command. With `shlex.quote`
    the whole string is a single literal argument to `[ -r ... ]`.
    """

    runner = _runner()
    payload = "/tmp/x; rm -rf /"
    manifest = InspectorManifest.model_construct(
        name="test.adversarial",
        version="1.0.0",
        description="test",
        tags=[],
        targets=["local"],
        requires_capabilities=[],
        requires_binaries=[],
        requires_files=[payload],
        privilege="none",
        parameters=None,
        secrets=[],
        collect=CollectSpec(command="echo ok"),
        parse=ParseSpec(format="raw"),
        output_schema={"type": "object"},
        findings=[],
    )
    target = _make_target(capabilities={Capability.SHELL})
    await runner._preflight(manifest, target, allow_privileged=False)
    args, _kwargs = target.exec.call_args
    cmd = args[0]
    # The whole payload must be quoted as a single literal — shlex.quote
    # escapes the `; rm -rf /` into a quoted block, so the cmd string
    # contains a literal that is NOT interpreted as multiple commands.
    expected = f"[ -r {shlex.quote(payload)} ]"
    assert cmd == expected
    # Sanity: shlex.quote produces single-quoted form for paths containing
    # spaces or shell metacharacters.
    assert shlex.quote(payload) != payload
    assert "'" in shlex.quote(payload)


async def test_parallel_probes_use_asyncio_gather() -> None:
    """Multiple binary probes should run concurrently via asyncio.gather."""

    runner = _runner()
    manifest = _make_manifest(requires_binaries=["a", "b", "c"])
    target = _make_target(capabilities={Capability.SHELL})
    await runner._preflight(manifest, target, allow_privileged=False)
    # All three probes happened.
    assert target.exec.call_count == 3


async def test_all_passes_returns_ok() -> None:
    runner = _runner()
    manifest = _make_manifest(requires_binaries=["echo"], requires_files=["/tmp"])
    target = _make_target(capabilities={Capability.SHELL})
    status, missing, _err = await runner._preflight(manifest, target, allow_privileged=False)
    assert status == "ok"
    assert missing == []


# ---------------------------------------------------------------------- #
# Preflight ``TargetError`` → ``target_unreachable``
#
# When the probe call site raises ``TargetError`` (e.g. SSH connection
# drops mid-probe) the runner contract maps that to
# ``status=target_unreachable`` with ``error=err.kind``. Without the
# wrap, the exception would escape ``run()`` and violate the per-call-site
# exception contract.
# ---------------------------------------------------------------------- #


def _make_target_raising(
    *,
    kind: str,
    capabilities: set[Capability] | None = None,
) -> Any:
    """Stub target whose ``exec`` raises ``TargetError(kind=...)`` on every call."""

    from hostlens.core.exceptions import TargetError

    target = MagicMock()
    target.name = "t1"
    target.type = "local"
    target.capabilities = capabilities if capabilities is not None else {Capability.SHELL}
    target.exec = AsyncMock(side_effect=TargetError(kind=kind))
    return target


async def test_target_error_in_binary_probe_maps_to_target_unreachable() -> None:
    runner = _runner()
    manifest = _make_manifest(requires_binaries=["nginx"])
    target = _make_target_raising(kind="ssh_connection_lost")
    status, missing, err = await runner._preflight(manifest, target, allow_privileged=False)
    assert status == "target_unreachable"
    assert missing == []
    assert err == "ssh_connection_lost"


async def test_target_error_in_file_probe_maps_to_target_unreachable() -> None:
    runner = _runner()
    manifest = _make_manifest(requires_files=["/etc/nginx/nginx.conf"])
    target = _make_target_raising(kind="ssh_connection_lost")
    status, missing, err = await runner._preflight(manifest, target, allow_privileged=False)
    assert status == "target_unreachable"
    assert missing == []
    assert err == "ssh_connection_lost"


async def test_run_with_target_error_in_preflight_returns_target_unreachable() -> None:
    """End-to-end: TargetError during preflight surfaces as a final
    ``InspectorResult.status == "target_unreachable"`` rather than
    escaping ``run()``.
    """

    from hostlens.inspectors.schema import CollectSpec, ParseSpec

    runner = _runner()
    manifest = InspectorManifest(
        name="test.preflight",
        version="1.0.0",
        description="test",
        targets=["local"],
        requires_binaries=["nginx"],
        collect=CollectSpec(command="echo ok"),
        parse=ParseSpec(format="raw"),
        output_schema={"type": "object"},
        findings=[],
    )
    target = _make_target_raising(kind="ssh_connection_lost")
    result = await runner.run(manifest, target)
    assert result.status == "target_unreachable"
    assert result.error == "ssh_connection_lost"
    assert result.missing == []
