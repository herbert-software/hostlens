"""Class-lock for the lazy-capability preflight trap (Authoring Contract rule 9).

`InspectorRunner` preflight checks `requires_capabilities` (step 2) **before**
any `exec` / binary probe (step 5). But `docker_cli` and `systemd` are added to
`LocalTarget` / `SSHTarget` only **lazily** — after the first `exec`, via
`_probe_capabilities`. So a builtin that gates on a lazily-probed capability
fails preflight with `requires_unmet` on a perfectly capable host and never
runs (and snapshot tests miss it, because the recorder warms the probe first).

The statically-present, preflight-safe capabilities are exactly the ones a
freshly constructed target already holds:
  - `LocalTarget`: {shell, file_read}
  - `SSHTarget`:   {ssh, shell, file_read}
i.e. the union {shell, file_read, ssh}. Every other enum value
(`docker_cli`, `systemd`) is lazily probed and MUST be gated via
`requires_binaries:` instead (rule 9), never via `requires_capabilities:`.

This test scans every builtin manifest and fails if any declares a
non-static (lazily-probed) capability in `requires_capabilities` — locking
docker, systemd, and any future lazy-capability inspector against the bug.
"""

from __future__ import annotations

import asyncio
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

# Capabilities present at target construction (before any exec). Keep in sync
# with LocalTarget.__init__ / SSHTarget.__init__; anything outside this set is
# lazily probed (see LocalTarget._probe_capabilities) and is unsafe to require.
_STATICALLY_PRESENT_CAPABILITIES = {"shell", "file_read", "ssh"}

_BUILTIN_DIR = Path(__file__).resolve().parents[2] / "src" / "hostlens" / "inspectors" / "builtin"

_BUILTIN_MANIFESTS = sorted(p for p in _BUILTIN_DIR.rglob("*.yaml") if p.name != "hook.py")


@pytest.mark.parametrize("manifest_path", _BUILTIN_MANIFESTS, ids=lambda p: p.stem)
def test_builtin_requires_only_static_capabilities(manifest_path: Path) -> None:
    manifest = load_manifest(manifest_path)
    declared = set(manifest.requires_capabilities)
    lazily_probed = declared - _STATICALLY_PRESENT_CAPABILITIES
    assert not lazily_probed, (
        f"{manifest.name} requires lazily-probed capabilities {sorted(lazily_probed)} "
        f"in requires_capabilities; preflight checks capabilities before any exec, so "
        f"this fails on a capable host with requires_unmet. Gate on requires_binaries "
        f"instead (Authoring Contract rule 9)."
    )


def test_at_least_one_manifest_scanned() -> None:
    # Guard against a glob that silently matches nothing (vacuous parametrize).
    assert len(_BUILTIN_MANIFESTS) >= 12, _BUILTIN_MANIFESTS


# --------------------------------------------------------------------------- #
# add-os-shell-inspectors-wave1 — binary preflight gate (tasks.md §10.2)
# --------------------------------------------------------------------------- #
#
# Spec §场景:缺少所需二进制时优雅 skip 而非崩溃 — when a target lacks a binary
# declared in `requires_binaries`, the runner's preflight must collapse to
# `status=requires_unmet` and skip (it must NOT raise / abort the run). We drive
# the real `InspectorRunner.run` against a stub target that answers every
# `command -v X` probe with exit 1 (binary absent) and assert the inspector is
# skipped with the missing binary surfaced in `missing`. One representative
# inspector per namespace (linux.* / net.* / log.*) is covered.


class _NoBinaryTarget:
    """Stub target where every `command -v X` probe fails (binary absent).

    The file-readability probe (`[ -r P ]`) is answered as readable so the
    binary gate (step 5) is the one that fires for inspectors that also declare
    `requires_files`. Any other (non-probe) command would mean preflight let the
    run proceed past the binary gate — that fails loud via AssertionError.
    """

    type = "local"
    name = "no-binary-host"
    capabilities: ClassVar[set[Capability]] = {Capability.SHELL, Capability.FILE_READ}

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        del timeout, env
        if cmd.startswith("command -v "):
            # Binary not found — `command -v` exits non-zero.
            return ExecResult(
                exit_code=1, stdout="", stderr="", duration_seconds=0.0, timed_out=False
            )
        if cmd.startswith("[ -r "):
            return ExecResult(
                exit_code=0, stdout="", stderr="", duration_seconds=0.0, timed_out=False
            )
        raise AssertionError(
            f"collector command must not run when a required binary is absent: {cmd!r}"
        )

    async def read_file(self, path: str) -> bytes:
        raise AssertionError(f"read_file must not be reached: {path!r}")


def _logger() -> structlog.stdlib.BoundLogger:
    return structlog.get_logger("test")  # type: ignore[no-any-return]


def _runner() -> InspectorRunner:
    return InspectorRunner(TargetRegistry(), settings=Settings(), logger=_logger())


# (registry name, yaml rel path, a binary the inspector declares as required)
_BINARY_GATE_CASES: list[tuple[str, str, str]] = [
    ("linux.process.zombies", "linux/process_zombies.yaml", "ps"),
    ("net.dns.resolve", "net/dns_resolve.yaml", "dig"),
    ("log.exception_burst", "log/exception_burst.yaml", "awk"),
]


@pytest.mark.parametrize(
    "name,rel_path,binary",
    _BINARY_GATE_CASES,
    ids=[c[0] for c in _BINARY_GATE_CASES],
)
def test_missing_binary_skips_with_requires_unmet(name: str, rel_path: str, binary: str) -> None:
    manifest = load_manifest(_BUILTIN_DIR / rel_path)
    assert manifest.name == name
    assert binary in manifest.requires_binaries

    # Some of these inspectors require parameters; pass a benign value so the
    # run reaches preflight (preflight runs before parameter validation, so the
    # values are immaterial — the binary gate fires first regardless).
    parameters: dict[str, object] = {}
    if name == "net.dns.resolve":
        parameters = {"names": ["example.com"]}
    elif name == "log.exception_burst":
        parameters = {"log_path": "/var/log/app.log"}

    target = _NoBinaryTarget()
    result: InspectorResult = asyncio.run(
        _runner().run(manifest, target, parameters=parameters)  # type: ignore[arg-type]
    )

    # Graceful skip — NOT an exception, NOT a crash.
    assert result.status == "requires_unmet"
    assert result.findings == []
    # The skipped run surfaces the missing binary so the report can annotate it.
    assert any(m.startswith("bin:") for m in result.missing), result.missing
