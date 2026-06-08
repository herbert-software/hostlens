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
    # add-service-inspector-contract-spike §4.6 — a missing service client
    # binary (redis-cli / mysql) is a premise gap → requires_unmet skip, NOT
    # an exception or crash that aborts the same run.
    ("redis.memory_usage", "redis/memory_usage.yaml", "redis-cli"),
    ("mysql.connection_usage", "mysql/connection_usage.yaml", "mysql"),
    # add-single-instance-service-inspectors §6.2 — every wave-2a service
    # client binary (redis-cli / psql / curl / nginx / docker) is a premise gap
    # → requires_unmet skip, NOT an exception that aborts the run. The two
    # secret-declaring probes (redis.persistence / postgres.connection_usage)
    # also exercise the gate ordering: the loop below sets each declared secret
    # to "" so the BINARY gate (step 5) — not the secret-env gate (step 4) — is
    # the one under test. The docker/nginx probes declare no secret and have no
    # required parameters, so an empty `parameters={}` reaches preflight.
    ("redis.persistence", "redis/persistence.yaml", "redis-cli"),
    ("postgres.connection_usage", "postgres/connection_usage.yaml", "psql"),
    ("nginx.health", "nginx/health.yaml", "curl"),
    ("nginx.config_test", "nginx/config_test.yaml", "nginx"),
    ("docker.images.disk_usage", "docker/images_disk_usage.yaml", "docker"),
    ("docker.networks", "docker/networks.yaml", "docker"),
    # add-log-and-fault-service-inspectors §5.2 — wave-2b client binaries.
    ("mysql.slow_queries", "mysql/slow_queries.yaml", "mysql"),
    ("postgres.long_queries", "postgres/long_queries.yaml", "psql"),
    ("nginx.error_rate", "nginx/error_rate.yaml", "awk"),
]


@pytest.mark.parametrize(
    "name,rel_path,binary",
    _BINARY_GATE_CASES,
    ids=[c[0] for c in _BINARY_GATE_CASES],
)
def test_missing_binary_skips_with_requires_unmet(
    name: str, rel_path: str, binary: str, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    elif name in (
        "mysql.connection_usage",
        "postgres.connection_usage",
        "mysql.slow_queries",
        "postgres.long_queries",
    ):
        parameters = {"user": "root"}

    # The secret-env gate (preflight step 4) runs BEFORE the binary probe
    # (step 5). For the service probes (which declare a secret) we set the
    # declared secret so the BINARY gate is the one under test fires — without
    # this the missing secret would short-circuit to env:* instead of bin:*.
    for secret in manifest.secrets:
        monkeypatch.setenv(secret, "")

    target = _NoBinaryTarget()
    result: InspectorResult = asyncio.run(
        _runner().run(manifest, target, parameters=parameters)  # type: ignore[arg-type]
    )

    # Graceful skip — NOT an exception, NOT a crash.
    assert result.status == "requires_unmet"
    assert result.findings == []
    # The skipped run surfaces the missing binary so the report can annotate it.
    assert any(m.startswith("bin:") for m in result.missing), result.missing


# --------------------------------------------------------------------------- #
# add-service-inspector-contract-spike — declared-secret preflight gate (§4.6)
# --------------------------------------------------------------------------- #
#
# Spec §场景:声明 secret 即强制其 env 存在 — a manifest that declares
# `secrets: [X]` must skip with `status=requires_unmet` (surfacing `env:X`) when
# `X` is absent from the environment (not even an empty string). This is a
# premise gap, NOT an exception, and must not abort the same run. A no-auth
# instance must export `X=` explicitly to pass.


class _AllBinariesPresentTarget:
    """Stub target where every `command -v X` probe SUCCEEDS (binary present).

    Used to isolate the secret-env gate: with the binary present, the only
    preflight gate left to fire for a probe with a missing declared secret is
    the secret-env gate (step 4). The collector must never run (no fixture).
    """

    type = "local"
    name = "all-binaries-host"
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
            f"collector must not run when a declared secret env is absent: {cmd!r}"
        )

    async def read_file(self, path: str) -> bytes:
        raise AssertionError(f"read_file must not be reached: {path!r}")


# (registry name, yaml rel path, the declared secret env, parameters)
_SECRET_GATE_CASES: list[tuple[str, str, str, dict[str, object]]] = [
    ("redis.memory_usage", "redis/memory_usage.yaml", "HOSTLENS_REDIS_PASSWORD", {}),
    (
        "mysql.connection_usage",
        "mysql/connection_usage.yaml",
        "HOSTLENS_MYSQL_PWD",
        {"user": "root"},
    ),
    # add-single-instance-service-inspectors §6.2 — the two wave-2a secret-
    # declaring probes: an absent declared secret env is a premise gap →
    # requires_unmet (surfacing env:HOSTLENS_*), not an exception.
    (
        "redis.persistence",
        "redis/persistence.yaml",
        "HOSTLENS_REDIS_PASSWORD",
        {},
    ),
    (
        "postgres.connection_usage",
        "postgres/connection_usage.yaml",
        "HOSTLENS_POSTGRES_PASSWORD",
        {"user": "postgres"},
    ),
    # add-log-and-fault-service-inspectors §5.2 — wave-2b secret-declaring probes.
    (
        "mysql.slow_queries",
        "mysql/slow_queries.yaml",
        "HOSTLENS_MYSQL_PWD",
        {"user": "root"},
    ),
    (
        "postgres.long_queries",
        "postgres/long_queries.yaml",
        "HOSTLENS_POSTGRES_PASSWORD",
        {"user": "postgres"},
    ),
]


@pytest.mark.parametrize(
    "name,rel_path,secret,parameters",
    _SECRET_GATE_CASES,
    ids=[c[0] for c in _SECRET_GATE_CASES],
)
def test_missing_secret_env_skips_with_requires_unmet(
    name: str,
    rel_path: str,
    secret: str,
    parameters: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = load_manifest(_BUILTIN_DIR / rel_path)
    assert manifest.name == name
    assert secret in manifest.secrets

    # Secret entirely absent (not even an empty string) → preflight requires_unmet.
    monkeypatch.delenv(secret, raising=False)

    target = _AllBinariesPresentTarget()
    result: InspectorResult = asyncio.run(
        _runner().run(manifest, target, parameters=parameters)  # type: ignore[arg-type]
    )

    # Graceful skip — NOT an exception, NOT a crash; surfaces the missing env.
    assert result.status == "requires_unmet"
    assert result.findings == []
    assert f"env:{secret}" in result.missing, result.missing


# --------------------------------------------------------------------------- #
# add-log-and-fault-service-inspectors — requires_files preflight gate (§5.2)
# --------------------------------------------------------------------------- #
#
# nginx.error_rate declares a static access log path in requires_files; when the
# file is missing/unreadable, preflight must skip with requires_unmet (not exception).

_NGINX_ACCESS_LOG = "/var/log/nginx/access.log"


class _NoAccessLogTarget:
    """Stub target where awk is present but the access log is not readable."""

    type = "local"
    name = "no-access-log-host"
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
            return ExecResult(
                exit_code=0,
                stdout="/usr/bin/awk\n",
                stderr="",
                duration_seconds=0.0,
                timed_out=False,
            )
        if cmd.startswith("[ -r ") and _NGINX_ACCESS_LOG in cmd:
            return ExecResult(
                exit_code=1, stdout="", stderr="", duration_seconds=0.0, timed_out=False
            )
        raise AssertionError(f"collector must not run when access log is absent: {cmd!r}")

    async def read_file(self, path: str) -> bytes:
        raise AssertionError(f"read_file must not be reached: {path!r}")


def test_nginx_error_rate_missing_access_log_skips_with_requires_unmet() -> None:
    """Missing / unreadable access log → requires_files preflight → requires_unmet."""

    manifest = load_manifest(_BUILTIN_DIR / "nginx" / "error_rate.yaml")
    assert manifest.name == "nginx.error_rate"
    assert _NGINX_ACCESS_LOG in manifest.requires_files

    target = _NoAccessLogTarget()
    result: InspectorResult = asyncio.run(
        _runner().run(manifest, target, parameters={})  # type: ignore[arg-type]
    )

    assert result.status == "requires_unmet"
    assert result.findings == []
    assert any(m.startswith("file:") for m in result.missing), result.missing


# --------------------------------------------------------------------------- #
# add-security-baseline-and-package-inspectors — capability/binary gate (§4.2)
# --------------------------------------------------------------------------- #
#
# Spec §场景:cohort 内 inspector 不得依赖外部服务或语言运行时 — every security/pkg
# os-shell inspector must declare `privilege: none`, gate only on the statically-
# present `{shell}` capability, and list ONLY real shell tools in
# requires_binaries (never the interpreter `sh`, never an external-service /
# language-runtime client). The expected binary sets below are pinned literally so
# a manifest that drops a real tool or smuggles `sh` / a service client fails loud.

# (registry name, yaml rel path, expected requires_binaries set)
_OS_SHELL_WAVE2_BINARY_CASES: list[tuple[str, str, set[str]]] = [
    ("security.failed_logins", "security/failed_logins.yaml", {"journalctl", "grep"}),
    ("security.sudo_history", "security/sudo_history.yaml", {"journalctl", "grep"}),
    ("security.world_writable_dirs", "security/world_writable_dirs.yaml", {"find", "awk"}),
    ("pkg.pending_updates", "pkg/pending_updates.yaml", {"grep"}),
    ("pkg.security_patches", "pkg/security_patches.yaml", {"grep"}),
    ("pkg.held_back", "pkg/held_back.yaml", {"awk"}),
]


@pytest.mark.parametrize(
    "name,rel_path,expected_binaries",
    _OS_SHELL_WAVE2_BINARY_CASES,
    ids=[c[0] for c in _OS_SHELL_WAVE2_BINARY_CASES],
)
def test_os_shell_wave2_capability_and_binary_gate(
    name: str, rel_path: str, expected_binaries: set[str]
) -> None:
    manifest = load_manifest(_BUILTIN_DIR / rel_path)
    assert manifest.name == name

    # privilege: none — no sudo / root escalation.
    assert manifest.privilege == "none", name

    # Gate ONLY on the statically-present {shell} capability (preflight runs
    # before any exec, so a lazily-probed capability would falsely requires_unmet
    # on a capable host — Authoring Contract rule 9).
    assert set(manifest.requires_capabilities) == {"shell"}, name

    # requires_binaries are the EXACT real shell tools — never the interpreter
    # `sh` (it is not a `command -v`-probed semantic tool) and never an external-
    # service / runtime client.
    binaries = set(manifest.requires_binaries)
    assert binaries == expected_binaries, name
    assert "sh" not in binaries, f"{name}: `sh` must not be a required binary"
