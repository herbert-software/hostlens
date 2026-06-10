"""Tests for the builtin Inspector manifests.

The end-to-end ``runner.run(...)`` exercise lives alongside the
runner + ToolRegistry dispatch tests; here we pin the static contract:

  * Each builtin yaml passes ``load_manifest`` cleanly (so the loader's
    Jinja2 AST walker / parameter-schema walker / ReDoS detector are all
    happy with the fixtures we ship).
  * ``build_registry_from_search_paths([], settings=Settings())``
    surfaces both manifests with ``errors == []``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

import pytest

import hostlens.inspectors as _inspectors_pkg
from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.registry import build_registry_from_search_paths


def _builtin_root() -> Path:
    """Return the directory holding the shipped builtin manifests.

    Computed exactly the way the registry's resolver does so the test
    can never drift from the production lookup path.
    """

    pkg_file = _inspectors_pkg.__file__
    assert pkg_file is not None
    return Path(pkg_file).parent / "builtin"


# --------------------------------------------------------------------------- #
# hello.echo
# --------------------------------------------------------------------------- #


class TestHelloEcho:
    def test_loader_accepts_echo_manifest(self) -> None:
        manifest = load_manifest(_builtin_root() / "hello" / "echo.yaml")

        assert manifest.name == "hello.echo"
        assert manifest.version == "1.0.0"
        assert manifest.targets == ["local", "ssh"]
        assert "demo" in manifest.tags
        assert manifest.privilege == "none"
        assert manifest.collect.command == "echo hello"
        assert manifest.collect.timeout_seconds == 5
        assert manifest.parse.format == "raw"
        assert manifest.parse.raw_extract_regex is None
        # One aggregate-mode finding referencing the top-level `raw`
        # output field — design Decision 8's minimal `Finding` model
        # mandates the message be solvable at runtime via
        # ``template.format(**output)``.
        assert len(manifest.findings) == 1
        finding = manifest.findings[0]
        assert finding.severity == "info"
        assert finding.for_each is None
        assert "{raw}" in finding.message

    def test_registry_contains_hello_echo(self) -> None:
        result = build_registry_from_search_paths([], settings=Settings())
        manifest = result.registry.get("hello.echo")
        assert manifest.name == "hello.echo"
        assert result.errors == []


# --------------------------------------------------------------------------- #
# system.uptime
# --------------------------------------------------------------------------- #


class TestSystemUptime:
    def test_loader_accepts_uptime_manifest(self) -> None:
        manifest = load_manifest(_builtin_root() / "system" / "uptime.yaml")

        assert manifest.name == "system.uptime"
        assert manifest.version == "1.0.0"
        assert manifest.targets == ["local", "ssh"]
        assert "linux" in manifest.tags
        assert manifest.requires_capabilities == ["shell"]
        assert manifest.requires_binaries == ["uptime"]
        assert manifest.collect.command == "uptime"
        assert manifest.parse.format == "raw"
        assert manifest.parse.columns == ["load1", "load5", "load15"]
        # All three named groups must round-trip via the regex; columns
        # length must match (the ParseSpec model_validator enforces this
        # but pinning the literal column set here documents the contract
        # the runner relies on through `_parse_raw`).
        assert manifest.parse.raw_extract_regex is not None
        assert "(?P<load1>" in manifest.parse.raw_extract_regex
        assert "(?P<load5>" in manifest.parse.raw_extract_regex
        assert "(?P<load15>" in manifest.parse.raw_extract_regex
        # Two aggregate-mode findings staircase warning -> critical.
        assert len(manifest.findings) == 2
        severities = {r.severity for r in manifest.findings}
        assert severities == {"warning", "critical"}

    def test_registry_contains_system_uptime(self) -> None:
        result = build_registry_from_search_paths([], settings=Settings())
        manifest = result.registry.get("system.uptime")
        assert manifest.name == "system.uptime"
        assert result.errors == []


# --------------------------------------------------------------------------- #
# net.tls.chain_validity (add-tls-chain-validity-inspector)
# --------------------------------------------------------------------------- #
#
# Standalone clean-registration assertion: this inspector is a net-domain
# incremental (a single matrix cell), NOT a member of any frozen cohort, so it
# is asserted independently and is deliberately absent from every cohort dict /
# count guard above (append-only freeze discipline).


class TestNetTlsChainValidity:
    def test_registry_contains_net_tls_chain_validity(self) -> None:
        result = build_registry_from_search_paths([], settings=Settings())
        manifest = result.registry.get("net.tls.chain_validity")
        assert manifest.name == "net.tls.chain_validity"
        assert result.errors == []


# --------------------------------------------------------------------------- #
# add-os-shell-inspectors-wave1 — suite-level acceptance (tasks.md §10)
# --------------------------------------------------------------------------- #
#
# The 23 wave-1 OS/Linux shell inspectors, keyed by registry `name` and mapped
# to their on-disk yaml path (relative to `builtin/`). This list is the
# 归档时冻结的清单 the spec §需求:wave-1 必须按域覆盖 references; it must stay in
# lockstep with the manifests shipped under builtin/{linux,net,log}/.

_WAVE1_INSPECTORS: dict[str, str] = {
    # 计算 CPU
    "linux.cpu.throttling": "linux/cpu_throttling.yaml",
    "linux.cpu.cpufreq": "linux/cpu_cpufreq.yaml",
    # 内存
    "linux.memory.swap": "linux/memory_swap.yaml",
    "linux.memory.hugepages": "linux/memory_hugepages.yaml",
    # 磁盘 / FS
    "linux.disk.io": "linux/disk_io.yaml",
    "linux.disk.smart": "linux/disk_smart.yaml",
    "linux.fs.mount_health": "linux/fs_mount_health.yaml",
    "linux.fs.logrotate": "linux/fs_logrotate.yaml",
    # 网络 + DNS + NTP
    "net.connections": "net/connections.yaml",
    "net.listening_ports": "net/listening_ports.yaml",
    "net.dns.resolve": "net/dns_resolve.yaml",
    "net.ntp.drift": "net/ntp_drift.yaml",
    # 进程
    "linux.process.zombies": "linux/process_zombies.yaml",
    "linux.process.total": "linux/process_total.yaml",
    "linux.process.critical_alive": "linux/process_critical_alive.yaml",
    # 服务管理器 + 调度器
    "linux.systemd.timer_status": "linux/systemd_timer_status.yaml",
    "linux.systemd.masked": "linux/systemd_masked.yaml",
    "linux.cron.last_runs": "linux/cron_last_runs.yaml",
    "linux.cron.failures": "linux/cron_failures.yaml",
    # 内核 / 系统
    "linux.system.reboot_required": "linux/system_reboot_required.yaml",
    "linux.kernel.taint": "linux/kernel_taint.yaml",
    "linux.kernel.messages": "linux/kernel_messages.yaml",
    # 日志
    "log.exception_burst": "log/exception_burst.yaml",
}

# The abnormal-scenario snapshots are keyed by the inspector's *yaml stem*
# (e.g. `cpu_throttling`) in the `_run("<stem>", ...)` helpers, not the dotted
# registry name. Map each registry name to its stem so the §10.4 scan can look
# for the stem token in the test_os_* sources.
_WAVE1_STEM_BY_NAME: dict[str, str] = {
    name: rel.rsplit("/", 1)[1].removesuffix(".yaml") for name, rel in _WAVE1_INSPECTORS.items()
}


# --------------------------------------------------------------------------- #
# add-service-inspector-contract-spike — clean registration (tasks.md §4.5)
# --------------------------------------------------------------------------- #
#
# The two service-inspector-contract probes must load cleanly and register with
# `errors == []`, exactly the way the wave-1 inspectors do — so a malformed
# service manifest (or a loader gate the secret/`| sh` patterns trip) fails this
# acceptance gate loud rather than silently shipping a broken builtin.

_SERVICE_PROBES: dict[str, str] = {
    "redis.memory_usage": "redis/memory_usage.yaml",
    "mysql.connection_usage": "mysql/connection_usage.yaml",
}


class TestServiceInspectorContractProbes:
    """tasks.md §4.5 — both spike probes register cleanly, registry errors==[]."""

    @pytest.mark.parametrize(
        "name,rel_path",
        sorted(_SERVICE_PROBES.items()),
        ids=sorted(_SERVICE_PROBES),
    )
    def test_probe_manifest_loads_clean(self, name: str, rel_path: str) -> None:
        manifest = load_manifest(_builtin_root() / rel_path)
        assert manifest.name == name

    def test_probes_register_with_no_errors(self) -> None:
        result = build_registry_from_search_paths([], settings=Settings())
        # The registry must surface BOTH probes and report zero load errors —
        # the same invariant the wave-1 suite locks.
        assert result.errors == []
        registered = set(result.registry.names())
        missing = set(_SERVICE_PROBES) - registered
        assert not missing, f"service probes absent from registry: {sorted(missing)}"


# --------------------------------------------------------------------------- #
# add-single-instance-service-inspectors — wave-2a clean registration (§6.1)
# --------------------------------------------------------------------------- #
#
# The 6 wave-2a single-instance read-only service inspectors, keyed by registry
# `name` → on-disk yaml path (relative to `builtin/`). This is the 归档时冻结的
# wave-2a 清单 the suite spec §需求:wave-2a 必须覆盖归档时冻结的单实例即时只读服务
# 单元格 references; it must stay in lockstep with the manifests shipped under
# builtin/{redis,postgres,docker,nginx}/.

_WAVE2A_INSPECTORS: dict[str, str] = {
    "redis.persistence": "redis/persistence.yaml",
    "postgres.connection_usage": "postgres/connection_usage.yaml",
    "docker.images.disk_usage": "docker/images_disk_usage.yaml",
    "docker.networks": "docker/networks.yaml",
    "nginx.health": "nginx/health.yaml",
    "nginx.config_test": "nginx/config_test.yaml",
}


class TestWave2aSuiteRegistration:
    """tasks.md §6.1 — every wave-2a inspector loads clean + registers."""

    def test_wave2a_count_is_frozen_at_6(self) -> None:
        # The suite spec freezes the wave-2a cohort at exactly 6 inspectors. A
        # drift (someone adds a 7th here without a new change) fails loud.
        assert len(_WAVE2A_INSPECTORS) == 6

    @pytest.mark.parametrize(
        "name,rel_path",
        sorted(_WAVE2A_INSPECTORS.items()),
        ids=sorted(_WAVE2A_INSPECTORS),
    )
    def test_wave2a_manifest_loads_clean(self, name: str, rel_path: str) -> None:
        manifest = load_manifest(_builtin_root() / rel_path)
        # `name` in the yaml must match the registry key we expect.
        assert manifest.name == name

    def test_wave2a_inspectors_all_register_with_no_errors(self) -> None:
        result = build_registry_from_search_paths([], settings=Settings())
        assert result.errors == []
        registered = set(result.registry.names())
        missing = set(_WAVE2A_INSPECTORS) - registered
        assert not missing, f"wave-2a inspectors absent from registry: {sorted(missing)}"


# --------------------------------------------------------------------------- #
# add-log-and-fault-service-inspectors — wave-2b clean registration (§5.1)
# --------------------------------------------------------------------------- #
#
# The 3 wave-2b log/window service inspectors, keyed by registry `name` → on-disk
# yaml path (relative to `builtin/`). This is the 归档时冻结的 wave-2b 清单 the suite
# spec ADDED wave-2b coverage requirement references.

_WAVE2B_INSPECTORS: dict[str, str] = {
    "mysql.slow_queries": "mysql/slow_queries.yaml",
    "postgres.long_queries": "postgres/long_queries.yaml",
    "nginx.error_rate": "nginx/error_rate.yaml",
}


class TestWave2bSuiteRegistration:
    """tasks.md §5.1 — every wave-2b inspector loads clean + registers."""

    def test_wave2b_count_is_frozen_at_3(self) -> None:
        assert len(_WAVE2B_INSPECTORS) == 3

    @pytest.mark.parametrize(
        "name,rel_path",
        sorted(_WAVE2B_INSPECTORS.items()),
        ids=sorted(_WAVE2B_INSPECTORS),
    )
    def test_wave2b_manifest_loads_clean(self, name: str, rel_path: str) -> None:
        manifest = load_manifest(_builtin_root() / rel_path)
        assert manifest.name == name

    def test_wave2b_inspectors_all_register_with_no_errors(self) -> None:
        result = build_registry_from_search_paths([], settings=Settings())
        assert result.errors == []
        registered = set(result.registry.names())
        missing = set(_WAVE2B_INSPECTORS) - registered
        assert not missing, f"wave-2b inspectors absent from registry: {sorted(missing)}"


# --------------------------------------------------------------------------- #
# add-nginx-upstream-mysql-deadlocks-inspectors — wave-2b-tail clean registration
# --------------------------------------------------------------------------- #
#
# The 2 wave-2b-tail service inspectors (nginx.upstream + mysql.deadlocks), keyed
# by registry `name` → on-disk yaml path (relative to `builtin/`). This is the
# 归档时冻结的 wave-2b-tail 清单 the suite spec's first scenario (wave-2b 尾批冻结
# 清单全部干净注册) references.
#
# IMPORTANT — this `wave-2b-tail` cohort is a DISTINCT, NON-COLLIDING symbol from
# the prior `_WAVE2B_INSPECTORS` (==3: slow_queries / long_queries / error_rate)
# above. Reusing `_WAVE2B_INSPECTORS` would shadow it within this module, collide
# with its frozen `== 3` count guard, and pollute the prior batch's semantics. A
# dedicated symbol keeps each cohort's count independent (same先例 as the
# os-shell wave-2 / runtime cohorts using their own symbols).

_WAVE2B_TAIL_INSPECTORS: dict[str, str] = {
    "nginx.upstream": "nginx/upstream.yaml",
    "mysql.deadlocks": "mysql/deadlocks.yaml",
}


class TestWave2bTailSuiteRegistration:
    """add-nginx-upstream-mysql-deadlocks-inspectors — every wave-2b-tail
    inspector loads clean + registers with errors == []. Distinct dedicated
    symbol — NOT the prior `_WAVE2B_INSPECTORS` (==3) guard."""

    def test_wave2b_tail_count_is_frozen_at_2(self) -> None:
        assert len(_WAVE2B_TAIL_INSPECTORS) == 2

    @pytest.mark.parametrize(
        "name,rel_path",
        sorted(_WAVE2B_TAIL_INSPECTORS.items()),
        ids=sorted(_WAVE2B_TAIL_INSPECTORS),
    )
    def test_wave2b_tail_manifest_loads_clean(self, name: str, rel_path: str) -> None:
        manifest = load_manifest(_builtin_root() / rel_path)
        assert manifest.name == name

    def test_wave2b_tail_inspectors_all_register_with_no_errors(self) -> None:
        result = build_registry_from_search_paths([], settings=Settings())
        assert result.errors == []
        registered = set(result.registry.names())
        missing = set(_WAVE2B_TAIL_INSPECTORS) - registered
        assert not missing, f"wave-2b-tail inspectors absent from registry: {sorted(missing)}"


# --------------------------------------------------------------------------- #
# add-security-baseline-and-package-inspectors — os-shell wave-2 (security/pkg)
# clean registration (tasks.md §4.1)
# --------------------------------------------------------------------------- #
#
# The 6 security/pkg os-shell inspectors, keyed by registry `name` → on-disk yaml
# path (relative to `builtin/`). This is the 归档时冻结的 cohort the
# os-shell-inspector-suite spec §需求:安全基线与包管理域必须按域覆盖 references; it
# must stay in lockstep with the manifests shipped under builtin/{security,pkg}/.
#
# IMPORTANT — this cohort is a DISTINCT suite from the service-inspector-suite's
# `_WAVE2A_INSPECTORS` / `_WAVE2B_INSPECTORS` above. Both the service wave-2a
# cohort and THIS os-shell wave-2 cohort happen to be exactly 6 inspectors, so a
# DEDICATED, non-colliding symbol name (`_OS_SHELL_WAVE2_SECURITY_PKG_INSPECTORS`
# / `test_os_shell_wave2_count_is_frozen_at_6`) is used here on purpose: reusing
# the service `_WAVE2A_INSPECTORS` symbol would shadow it within this module and
# silently swallow the service cohort's count guard. These are independent dicts
# that do not touch each other's counts.

_OS_SHELL_WAVE2_SECURITY_PKG_INSPECTORS: dict[str, str] = {
    # 安全基线域 (builtin/security/)
    "security.failed_logins": "security/failed_logins.yaml",
    "security.sudo_history": "security/sudo_history.yaml",
    "security.world_writable_dirs": "security/world_writable_dirs.yaml",
    # 包管理域 (builtin/pkg/)
    "pkg.pending_updates": "pkg/pending_updates.yaml",
    "pkg.security_patches": "pkg/security_patches.yaml",
    "pkg.held_back": "pkg/held_back.yaml",
}


class TestOsShellWave2SecurityPkgRegistration:
    """tasks.md §4.1 — every security/pkg os-shell wave-2 inspector loads clean
    + registers with errors == []. Distinct suite from the service wave-2a/2b
    cohorts above (independent dict, no cross-cohort count coupling)."""

    def test_os_shell_wave2_count_is_frozen_at_6(self) -> None:
        # The suite spec freezes this os-shell security/pkg cohort at exactly 6
        # inspectors (3 security + 3 pkg). A drift (a 7th added here without a new
        # change) fails loud. Dedicated symbol — NOT the service `_WAVE2A_*` guard.
        assert len(_OS_SHELL_WAVE2_SECURITY_PKG_INSPECTORS) == 6

    @pytest.mark.parametrize(
        "name,rel_path",
        sorted(_OS_SHELL_WAVE2_SECURITY_PKG_INSPECTORS.items()),
        ids=sorted(_OS_SHELL_WAVE2_SECURITY_PKG_INSPECTORS),
    )
    def test_os_shell_wave2_manifest_loads_clean(self, name: str, rel_path: str) -> None:
        manifest = load_manifest(_builtin_root() / rel_path)
        # `name` in the yaml must match the registry key we expect.
        assert manifest.name == name

    def test_os_shell_wave2_inspectors_all_register_with_no_errors(self) -> None:
        result = build_registry_from_search_paths([], settings=Settings())
        assert result.errors == []
        registered = set(result.registry.names())
        missing = set(_OS_SHELL_WAVE2_SECURITY_PKG_INSPECTORS) - registered
        assert not missing, f"security/pkg inspectors absent from registry: {sorted(missing)}"


class TestOsShellWave2NoExternalServiceDependency:
    """tasks.md §4.3 — the security/pkg cohort is os-shell, NOT service domain.

    The suite spec §场景:cohort 内 inspector 不得依赖外部服务或语言运行时 forbids
    any inspector here from referencing an external-service client or a language-
    runtime tool in `requires_binaries` or `collect.command`. The forbidden set is
    an EXPLICIT frozenset (not an open "等" / membership-by-convention list) so the
    assertion is non-vacuous — a smuggled `psql` / `docker` / `jstat` reference
    fails loud.
    """

    #: Explicit, closed forbidden set (external-service clients + language-runtime
    #: tools). NOT open-ended — a literal frozenset so the guard cannot pass
    #: vacuously by "等" hand-waving.
    _FORBIDDEN_EXTERNAL_TOOLS: ClassVar[frozenset[str]] = frozenset(
        {"nginx", "mysql", "redis-cli", "psql", "docker", "jstat", "jcmd"}
    )

    @pytest.mark.parametrize(
        "name,rel_path",
        sorted(_OS_SHELL_WAVE2_SECURITY_PKG_INSPECTORS.items()),
        ids=sorted(_OS_SHELL_WAVE2_SECURITY_PKG_INSPECTORS),
    )
    def test_requires_binaries_has_no_external_service_tool(self, name: str, rel_path: str) -> None:
        manifest = load_manifest(_builtin_root() / rel_path)
        leaked = set(manifest.requires_binaries) & self._FORBIDDEN_EXTERNAL_TOOLS
        assert not leaked, (
            f"{name}: requires_binaries references external-service/runtime "
            f"tool(s) {sorted(leaked)} — this cohort is zero-external-dependency "
            f"OS shell only"
        )

    @pytest.mark.parametrize(
        "name,rel_path",
        sorted(_OS_SHELL_WAVE2_SECURITY_PKG_INSPECTORS.items()),
        ids=sorted(_OS_SHELL_WAVE2_SECURITY_PKG_INSPECTORS),
    )
    def test_collect_command_has_no_external_service_tool(self, name: str, rel_path: str) -> None:
        manifest = load_manifest(_builtin_root() / rel_path)
        cmd = manifest.collect.command
        # Whole-word match so a substring (e.g. "dockerized" hypothetical, or a
        # path segment) does not over-trigger; the forbidden tokens are command
        # invocations, so a word-boundary match is the right granularity.
        for tool in self._FORBIDDEN_EXTERNAL_TOOLS:
            assert not re.search(rf"(?<![\w-]){re.escape(tool)}(?![\w-])", cmd), (
                f"{name}: collect.command references external-service/runtime "
                f"tool {tool!r} — this cohort is zero-external-dependency OS shell only"
            )

    def test_cohort_not_in_service_crosscheck_manifests(self) -> None:
        """The security/pkg cohort must NOT be enumerated in the service
        crosscheck's `_ALL_SERVICE_MANIFESTS` (each cohort owns an independent
        dict; the two suites do not touch each other's frozen counts).

        The service crosscheck names are pinned literally here (rather than
        imported from the sibling test module — CI runs with pythonpath=src and
        no `tests/__init__.py`, so a `from tests.inspectors...` import would
        crash). The literal list is the 11 = 2 spike + 6 wave-2a + 3 wave-2b
        service inspectors enumerated in test_service_contract_crosscheck.py's
        `_ALL_SERVICE_MANIFESTS`; the disjointness assertion is what matters."""

        service_crosscheck_names = {
            "redis.memory_usage",
            "mysql.connection_usage",
            "redis.persistence",
            "postgres.connection_usage",
            "docker.images.disk_usage",
            "docker.networks",
            "nginx.health",
            "nginx.config_test",
            "mysql.slow_queries",
            "postgres.long_queries",
            "nginx.error_rate",
        }
        overlap = set(_OS_SHELL_WAVE2_SECURITY_PKG_INSPECTORS) & service_crosscheck_names
        assert not overlap, (
            f"security/pkg os-shell inspectors leaked into the service "
            f"crosscheck cohort: {sorted(overlap)}"
        )


# --------------------------------------------------------------------------- #
# add-runtime-inspectors — runtime cohort clean registration (tasks.md §4.1/4.3)
# --------------------------------------------------------------------------- #
#
# The 5 runtime (语言运行时) inspectors — 3 JVM + 2 Go — keyed by registry `name`
# → on-disk yaml path (relative to `builtin/`). This is the 归档时冻结的 runtime
# 清单 the runtime-inspector-suite spec §需求:runtime-inspector-suite 必须按运行时
# 域覆盖 JVM 与 Go references; it must stay in lockstep with the manifests shipped
# under builtin/{jvm,go}/.
#
# IMPORTANT — this `runtime` cohort is a DISTINCT suite from BOTH:
#   * the service-inspector-suite's `_WAVE2A_INSPECTORS` (==6) /
#     `_WAVE2B_INSPECTORS` (==3) above, and
#   * the os-shell-inspector-suite's `_OS_SHELL_WAVE2_SECURITY_PKG_INSPECTORS`
#     (==6) above and `_WAVE1_INSPECTORS` (==23).
# Per spec §需求 it is另立 capability 而非塞进 os-shell / service 套件 — its
# premise is「目标运行时进程/端点在场」, orthogonal to the os-shell「零外部依赖」
# invariant and the service「单实例 + 连接 secret」shape. A DEDICATED, non-
# colliding symbol name (`_RUNTIME_INSPECTORS` /
# `test_runtime_count_is_frozen_at_5`) is used on purpose: reusing a `_WAVE2A_*`
# / `_OS_SHELL_WAVE2_*` symbol would shadow it within this module and silently
# swallow another cohort's count guard. These are independent dicts that do NOT
# touch each other's frozen counts.

_RUNTIME_INSPECTORS: dict[str, str] = {
    # JVM 域 (builtin/jvm/) — 3 inspector
    "jvm.heap": "jvm/heap.yaml",
    "jvm.gc": "jvm/gc.yaml",
    "jvm.threads": "jvm/threads.yaml",
    # Go 域 (builtin/go/) — 2 inspector
    "go.goroutines": "go/goroutines.yaml",
    "go.heap": "go/heap.yaml",
}


class TestRuntimeSuiteRegistration:
    """tasks.md §4.1 — every runtime inspector loads clean + registers with
    errors == []. Distinct suite from the service wave-2a/2b cohorts and the
    os-shell wave-1 / wave-2 security/pkg cohorts above (independent dict, no
    cross-cohort count coupling)."""

    def test_runtime_count_is_frozen_at_5(self) -> None:
        # The runtime-inspector-suite spec freezes the cohort at exactly 5
        # inspectors (3 JVM + 2 Go — JVM 域 ≥3, Go 域 ≥2). A drift (a 6th added
        # here without a new change) fails loud. Dedicated symbol — NOT the
        # service `_WAVE2A_*` / os-shell `_OS_SHELL_WAVE2_*` guards.
        assert len(_RUNTIME_INSPECTORS) == 5

    @pytest.mark.parametrize(
        "name,rel_path",
        sorted(_RUNTIME_INSPECTORS.items()),
        ids=sorted(_RUNTIME_INSPECTORS),
    )
    def test_runtime_manifest_loads_clean(self, name: str, rel_path: str) -> None:
        manifest = load_manifest(_builtin_root() / rel_path)
        # `name` in the yaml must match the registry key we expect.
        assert manifest.name == name

    def test_runtime_inspectors_all_register_with_no_errors(self) -> None:
        result = build_registry_from_search_paths([], settings=Settings())
        assert result.errors == []
        registered = set(result.registry.names())
        missing = set(_RUNTIME_INSPECTORS) - registered
        assert not missing, f"runtime inspectors absent from registry: {sorted(missing)}"


class TestRuntimeCohortIsDistinctFromOtherSuites:
    """tasks.md §4.3 — the runtime cohort is its own domain (runtime), NOT
    os-shell wave-2 nor service. Prove the dicts are disjoint so no cohort's
    frozen count is silently coupled to another's.

    The service crosscheck names are pinned literally here (rather than imported
    from the sibling test module — CI runs with pythonpath=src and no
    `tests/__init__.py`, so a `from tests.inspectors...` import would crash). The
    literal list is the 11 = 2 spike + 6 wave-2a + 3 wave-2b service inspectors
    enumerated in test_service_contract_crosscheck.py's `_ALL_SERVICE_MANIFESTS`;
    the disjointness assertion is what matters."""

    _SERVICE_CROSSCHECK_NAMES: ClassVar[frozenset[str]] = frozenset(
        {
            "redis.memory_usage",
            "mysql.connection_usage",
            "redis.persistence",
            "postgres.connection_usage",
            "docker.images.disk_usage",
            "docker.networks",
            "nginx.health",
            "nginx.config_test",
            "mysql.slow_queries",
            "postgres.long_queries",
            "nginx.error_rate",
        }
    )

    def test_runtime_disjoint_from_service_crosscheck(self) -> None:
        overlap = set(_RUNTIME_INSPECTORS) & self._SERVICE_CROSSCHECK_NAMES
        assert not overlap, (
            f"runtime inspectors leaked into the service crosscheck cohort: {sorted(overlap)}"
        )

    def test_runtime_disjoint_from_os_shell_wave2_security_pkg(self) -> None:
        overlap = set(_RUNTIME_INSPECTORS) & set(_OS_SHELL_WAVE2_SECURITY_PKG_INSPECTORS)
        assert not overlap, (
            f"runtime inspectors leaked into the os-shell wave-2 security/pkg "
            f"cohort: {sorted(overlap)}"
        )

    def test_runtime_disjoint_from_os_shell_wave1(self) -> None:
        overlap = set(_RUNTIME_INSPECTORS) & set(_WAVE1_INSPECTORS)
        assert not overlap, (
            f"runtime inspectors leaked into the os-shell wave-1 cohort: {sorted(overlap)}"
        )


class TestRuntimeParameterisationAndInjectionSafety:
    """tasks.md §4.3 — spec §需求:套件内每个 inspector 必须参数化目标运行时进程
    或端点且参数注入安全.

    Per the suite spec:
      * every manifest's `parameters` is a complete JSON Schema (type: object +
        properties + additionalProperties: false);
      * every JVM manifest exposes `pid` (type integer) OR `process_pattern`
        (type string) to parameterise the target;
      * every Go manifest exposes `pprof_endpoint` (type string);
      * every string target param inserted into an executable position flows
        through `{{ x | sh }}` AND carries a non-empty `pattern` (so the
        injection面 stays收紧 — a bare `{{ param }}` would re-open it);
      * `pprof_endpoint`'s pattern is a host:port form (contains `:[0-9]`).
    Forbidden-tool reasoning uses an EXPLICIT frozenset so the binary assertion
    is non-vacuous.
    """

    _JVM_NAMES: ClassVar[frozenset[str]] = frozenset({"jvm.heap", "jvm.gc", "jvm.threads"})
    _GO_NAMES: ClassVar[frozenset[str]] = frozenset({"go.goroutines", "go.heap"})

    @pytest.mark.parametrize(
        "name,rel_path",
        sorted(_RUNTIME_INSPECTORS.items()),
        ids=sorted(_RUNTIME_INSPECTORS),
    )
    def test_parameters_is_complete_json_schema(self, name: str, rel_path: str) -> None:
        manifest = load_manifest(_builtin_root() / rel_path)
        params = manifest.parameters
        assert params is not None, name
        assert params.get("type") == "object", name
        assert isinstance(params.get("properties"), dict), name
        # additionalProperties: false —缺 wrapper/松开会让 loader 看不到参数、`| sh`
        # 门与 pattern 双双静默失效。
        assert params.get("additionalProperties") is False, name

    @pytest.mark.parametrize(
        "name,rel_path",
        sorted({k: _RUNTIME_INSPECTORS[k] for k in _JVM_NAMES}.items()),
        ids=sorted(_JVM_NAMES),
    )
    def test_jvm_target_parameterised_by_pid_or_process_pattern(
        self, name: str, rel_path: str
    ) -> None:
        manifest = load_manifest(_builtin_root() / rel_path)
        props = manifest.parameters["properties"]  # type: ignore[index]
        # Contract (spec §参数化目标) is `pid` OR `process_pattern` — require at
        # least one; do NOT mandate both (a compliant JVM inspector may ship only
        # one). Validate the type only for the key(s) actually present.
        has_pid = "pid" in props
        has_pattern = "process_pattern" in props
        assert has_pid or has_pattern, (
            f"{name}: JVM inspector must parameterise the target via pid(int) or "
            f"process_pattern(str)"
        )
        if has_pid:
            assert props["pid"].get("type") == "integer", f"{name}: pid must be type integer"
        if has_pattern:
            assert props["process_pattern"].get("type") == "string", (
                f"{name}: process_pattern must be type string"
            )

    @pytest.mark.parametrize(
        "name,rel_path",
        sorted({k: _RUNTIME_INSPECTORS[k] for k in _GO_NAMES}.items()),
        ids=sorted(_GO_NAMES),
    )
    def test_go_target_parameterised_by_pprof_endpoint(self, name: str, rel_path: str) -> None:
        manifest = load_manifest(_builtin_root() / rel_path)
        props = manifest.parameters["properties"]  # type: ignore[index]
        endpoint = props.get("pprof_endpoint")
        assert isinstance(endpoint, dict), f"{name}: Go inspector must declare pprof_endpoint"
        assert endpoint.get("type") == "string", f"{name}: pprof_endpoint must be type string"

    @pytest.mark.parametrize(
        "name,rel_path",
        sorted(_RUNTIME_INSPECTORS.items()),
        ids=sorted(_RUNTIME_INSPECTORS),
    )
    def test_string_target_params_are_sh_quoted_and_pattern_tightened(
        self, name: str, rel_path: str
    ) -> None:
        manifest = load_manifest(_builtin_root() / rel_path)
        props: dict[str, dict[str, object]] = manifest.parameters["properties"]  # type: ignore[index]
        cmd = manifest.collect.command
        # The string target params that flow into the executable command. `pid`
        # is an integer (no shell-injection surface) and is excluded by design.
        for param in ("process_pattern", "pprof_endpoint"):
            spec = props.get(param)
            if spec is None:
                continue
            assert spec.get("type") == "string", f"{name}: {param} must be type string"
            # Tightened value domain — a non-empty `pattern` is mandatory; a bare
            # interpolation would re-open the injection面.
            pattern = spec.get("pattern")
            assert isinstance(pattern, str) and pattern, (
                f"{name}: {param} must carry a non-empty pattern to收紧取值域"
            )
            # If the param is interpolated into the command it MUST go through
            # `{{ param | sh }}` — never a bare `{{ param }}` in an executable
            # position. Match an actual Jinja interpolation (whitespace-robust)
            # rather than a loose substring, and only enforce `| sh` when such an
            # interpolation is present.
            interp = re.search(r"\{\{\s*" + re.escape(param) + r"\b", cmd)
            if interp:
                assert re.search(r"\{\{\s*" + re.escape(param) + r"\s*\|\s*sh\s*\}\}", cmd), (
                    f"{name}: {param} must be referenced via {{{{ {param} | sh }}}} "
                    f"(no bare interpolation into the command)"
                )

    @pytest.mark.parametrize(
        "name,rel_path",
        sorted({k: _RUNTIME_INSPECTORS[k] for k in _GO_NAMES}.items()),
        ids=sorted(_GO_NAMES),
    )
    def test_pprof_endpoint_pattern_is_host_port_form(self, name: str, rel_path: str) -> None:
        manifest = load_manifest(_builtin_root() / rel_path)
        props = manifest.parameters["properties"]  # type: ignore[index]
        pattern = props["pprof_endpoint"]["pattern"]
        # host:port form — the pattern must constrain a numeric port segment
        # (`:[0-9]`),杜绝 `;` / 空格 / `$()` 注入字符.
        assert ":[0-9]" in pattern, (
            f"{name}: pprof_endpoint pattern must be a host:port form "
            f"(contain ':[0-9]'); got {pattern!r}"
        )


class TestWave1SuiteRegistration:
    """tasks.md §10.1 — every wave-1 inspector loads clean + registers."""

    def test_wave1_count_is_frozen_at_23(self) -> None:
        # The spec freezes the wave-1 list at exactly 23 inspectors. A drift
        # (someone adds a 24th here without a new change) fails loud.
        assert len(_WAVE1_INSPECTORS) == 23

    @pytest.mark.parametrize(
        "name,rel_path",
        sorted(_WAVE1_INSPECTORS.items()),
        ids=sorted(_WAVE1_INSPECTORS),
    )
    def test_wave1_manifest_loads_clean(self, name: str, rel_path: str) -> None:
        manifest = load_manifest(_builtin_root() / rel_path)
        # `name` in the yaml must match the registry key we expect.
        assert manifest.name == name

    def test_wave1_inspectors_all_register_with_no_errors(self) -> None:
        result = build_registry_from_search_paths([], settings=Settings())
        assert result.errors == []
        registered = set(result.registry.names())
        missing = set(_WAVE1_INSPECTORS) - registered
        assert not missing, f"wave-1 inspectors absent from registry: {sorted(missing)}"


class TestWave1NoContractDrift:
    """tasks.md §10.6 — lock the零对外契约变更 invariant.

    The wave-1 suite is pure manifest-bulk: it must NOT widen the capability
    enum or the parse-format set. Import the live schema constants and pin
    them so any future schema change that the suite would have ridden on
    breaks this regression lock instead of silently shipping.
    """

    def test_capability_enum_unchanged(self) -> None:
        from hostlens.inspectors.schema import _ALLOWED_CAPABILITIES
        from hostlens.targets.base import Capability

        expected = frozenset({"shell", "file_read", "ssh", "systemd", "docker_cli"})
        assert set(_ALLOWED_CAPABILITIES) == set(expected)
        # The schema allow-list must equal the live Capability enum values —
        # the two are documented to stay in sync (schema.py comment).
        assert set(_ALLOWED_CAPABILITIES) == {cap.value for cap in Capability}

    def test_parse_format_set_unchanged(self) -> None:
        import typing

        from hostlens.inspectors.schema import ParseSpec

        format_field = ParseSpec.model_fields["format"]
        literal_values = set(typing.get_args(format_field.annotation))
        assert literal_values == {"raw", "table", "json", "kv"}

    def test_wave1_manifests_use_only_allowed_parse_formats(self) -> None:
        for rel_path in _WAVE1_INSPECTORS.values():
            manifest = load_manifest(_builtin_root() / rel_path)
            assert manifest.parse.format in {"raw", "table", "json", "kv"}

    def test_wave1_manifests_require_only_allowed_capabilities(self) -> None:
        allowed = {"shell", "file_read", "ssh", "systemd", "docker_cli"}
        for rel_path in _WAVE1_INSPECTORS.values():
            manifest = load_manifest(_builtin_root() / rel_path)
            assert set(manifest.requires_capabilities) <= allowed


class TestWave1DetectionCoverage:
    """tasks.md §10.4 — prove no no-op inspector ships.

    The spec mandates每个 inspector 至少有一个 fixture 场景其 snapshot 断言了
    finding. The per-inspector abnormal-scenario snapshot tests live in the
    `test_os_*.py` files; here we scan those files' source and assert that
    every wave-1 inspector name has at least one snapshot test that asserts a
    `(severity, message)` finding tuple — an objective, executable guard
    against silently dropping an abnormal-scenario assertion.
    """

    def _test_os_sources(self) -> str:
        test_dir = Path(__file__).resolve().parent
        joined: list[str] = []
        for src in sorted(test_dir.glob("test_os_*.py")):
            joined.append(src.read_text(encoding="utf-8"))
        return "\n".join(joined)

    @pytest.mark.parametrize(
        "name",
        sorted(_WAVE1_INSPECTORS),
        ids=sorted(_WAVE1_INSPECTORS),
    )
    def test_each_inspector_has_a_finding_asserting_snapshot(self, name: str) -> None:
        sources = self._test_os_sources()
        stem = _WAVE1_STEM_BY_NAME[name]
        # The abnormal-scenario test must (a) reference the inspector by its
        # yaml stem in a `_run(...)` call and (b) assert a finding tuple
        # `(f.severity, f.message)`. We require both tokens present in the
        # combined test_os_* sources; the stem is unique per inspector.
        assert f'"{stem}"' in sources, f"no _run(...) reference to inspector stem {stem!r}"
        assert "(f.severity, f.message) for f in result.findings" in sources

    def test_finding_assertion_idiom_present(self) -> None:
        # Guard: if the finding-assertion idiom is ever renamed, the
        # per-inspector check above would pass vacuously on the second token.
        # Pin that the idiom appears at least 23 times (one abnormal snapshot
        # per inspector, at minimum).
        sources = self._test_os_sources()
        occurrences = sources.count("(f.severity, f.message) for f in result.findings")
        assert occurrences >= 23, occurrences
