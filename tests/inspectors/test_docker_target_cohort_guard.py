"""Meta-guards freezing which builtin inspectors may declare container-class
targets (``docker`` / ``k8s``).

These guards back the container-applicability requirement of the
inspector-authoring contract (§场景:内容式 meta-guard, §场景:奇偶不变量). Two
independent layers protect the cohort:

* **Frozen INCLUDE/EXCLUDE name lists** (``test_include_exclude_*``) — the
  manually-reviewed roster from design Decision 4, with hard count assertions
  (28 / 42 / 70) so a hand-tally drift (a dropped or smuggled manifest) fails
  loudly instead of silently shifting the cohort.
* **Content-based guard** (``test_host_global_marker_*``) — independent of the
  name list: any manifest whose ``collect.command`` reads a host-global,
  non-namespaced source (``/proc/sys/`` / ``/proc/meminfo`` / ``journalctl`` /
  ``/proc/loadavg`` / ``/proc/uptime``) MUST NOT declare ``docker`` or ``k8s``.
  This mechanically traps the dangerous silent-misattribution class even if a
  future author adds such an inspector and wrongly lists a container target by
  domain analogy.

On top of both layers a **parity invariant** holds: container safety is one
property of the collector's read sources, not one per runtime — so every
builtin manifest must satisfy ``("docker" in targets) == ("k8s" in targets)``.

Escape hatch for the parity invariant: a future inspector with a **k8s-only
read source** (e.g. checking ``/var/run/secrets/kubernetes.io/serviceaccount/
token`` expiry — that file does not exist in plain docker containers) may
legitimately declare ``[k8s]`` without ``[docker]``. Doing so MUST be an
explicit decision that simultaneously amends the authoring-contract
container-applicability criterion AND the parity assertion below.

File name kept as-is (renaming would break git blame); this docstring is the
source of truth for the broadened container-class scope.
"""

from __future__ import annotations

from pathlib import Path

import hostlens.inspectors as _inspectors_pkg
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.schema import InspectorManifest

# --------------------------------------------------------------------------- #
# Frozen rosters — design Decision 4 全表. Hard-code by manifest.name so a
# rename/move of a yaml file surfaces as a guard failure, not a silent pass.
# --------------------------------------------------------------------------- #

_INCLUDE: frozenset[str] = frozenset(
    {
        # nginx
        "nginx.config_test",
        "nginx.error_rate",
        "nginx.health",
        # mysql
        "mysql.connection_usage",
        "mysql.replication_lag",
        "mysql.slow_queries",
        # postgres
        "postgres.bloat_tables",
        "postgres.connection_usage",
        "postgres.long_queries",
        "postgres.replication_lag",
        # redis
        "redis.memory_usage",
        "redis.persistence",
        "redis.replication_lag",
        "redis.slowlog",
        # jvm
        "jvm.gc",
        "jvm.heap",
        "jvm.threads",
        # go
        "go.goroutines",
        "go.heap",
        # linux.process (PID-namespace-correct only)
        "linux.process.zombies",
        "linux.process.critical_alive",
        # log (app-log file only)
        "log.exception_burst",
        # net (container netns view)
        "net.connections",
        "net.listening_ports",
        "net.dns.resolve",
        "net.dependency.tcp_check",
        "net.tls.cert_expiry",
        "net.tls.chain_validity",
    }
)

_EXCLUDE: frozenset[str] = frozenset(
    {
        # linux.cpu
        "linux.cpu.cpufreq",
        "linux.cpu.throttling",
        "linux.cpu.top_processes",
        # linux.disk
        "linux.disk.io",
        "linux.disk.smart",
        "linux.disk.usage",
        # linux.fs
        "linux.fs.inode_pressure",
        "linux.fs.logrotate",
        "linux.fs.mount_health",
        # linux.kernel
        "linux.kernel.messages",
        "linux.kernel.oom_killer",
        "linux.kernel.taint",
        # linux.memory
        "linux.memory.hugepages",
        "linux.memory.pressure",
        "linux.memory.swap",
        # linux.process (host-global /proc/sys readers)
        "linux.process.fd_usage",
        "linux.process.total",
        # linux.systemd
        "linux.systemd.failed_units",
        "linux.systemd.masked",
        "linux.systemd.timer_status",
        # linux.cron
        "linux.cron.failures",
        "linux.cron.last_runs",
        # linux.system
        "linux.system.load_avg",
        "linux.system.reboot_required",
        # system
        "system.uptime",
        # log (host journal)
        "log.tail.error_burst",
        # net (host clock)
        "net.ntp.drift",
        # pkg
        "pkg.held_back",
        "pkg.pending_updates",
        "pkg.security_patches",
        # security
        "security.failed_logins",
        "security.sudo_history",
        "security.world_writable_dirs",
        # docker (docker-in-docker)
        "docker.containers.restart_loop",
        "docker.images.disk_usage",
        "docker.networks",
        # k8s (kubectl control-plane view — runs on a management host, NEVER
        # inside a pod; a pod has no kubectl and must not gain cluster read
        # access, so these declare [local, ssh] only, never a container target)
        "k8s.pods.oom_killed",
        "k8s.pods.evicted",
        "k8s.pods.stuck_pending",
        "k8s.nodes.conditions",
        "k8s.events.warnings",
        # demo
        "hello.echo",
    }
)

# Content markers for host-global, NON-namespaced sources. A command touching
# any of these reads a host-shared value inside a container → silent
# misattribution. Independent of the name roster above.
_HOST_GLOBAL_MARKERS: tuple[str, ...] = (
    "/proc/sys/",
    "/proc/meminfo",
    "journalctl",
    "/proc/loadavg",
    "/proc/uptime",
)


def _builtin_root() -> Path:
    pkg_file = _inspectors_pkg.__file__
    assert pkg_file is not None
    return Path(pkg_file).parent / "builtin"


def _load_all_builtin() -> dict[str, InspectorManifest]:
    root = _builtin_root()
    by_name: dict[str, InspectorManifest] = {}
    for path in sorted(root.rglob("*.yaml"), key=str):
        manifest = load_manifest(path)
        assert manifest.name not in by_name, f"duplicate builtin name: {manifest.name}"
        by_name[manifest.name] = manifest
    return by_name


# --------------------------------------------------------------------------- #
# 3.9 — frozen INCLUDE / EXCLUDE roster + hard counts
# --------------------------------------------------------------------------- #


def test_rosters_partition_all_builtins_with_frozen_counts() -> None:
    """INCLUDE(28) + EXCLUDE(42) = every builtin(70), disjoint — a count drift
    (dropped or smuggled manifest) fails here, not silently."""

    assert len(_INCLUDE) == 28
    assert len(_EXCLUDE) == 42
    assert _INCLUDE.isdisjoint(_EXCLUDE)

    by_name = _load_all_builtin()
    all_names = frozenset(by_name)
    assert len(all_names) == 70

    rostered = _INCLUDE | _EXCLUDE
    assert all_names == rostered, {
        "in_registry_not_rostered": sorted(all_names - rostered),
        "rostered_not_in_registry": sorted(rostered - all_names),
    }


def test_include_roster_declares_container_targets() -> None:
    by_name = _load_all_builtin()
    offenders = sorted(
        n for n in _INCLUDE if "docker" not in by_name[n].targets or "k8s" not in by_name[n].targets
    )
    assert not offenders, f"INCLUDE inspectors missing docker/k8s target: {offenders}"


def test_exclude_roster_omits_container_targets() -> None:
    by_name = _load_all_builtin()
    offenders = sorted(
        n for n in _EXCLUDE if "docker" in by_name[n].targets or "k8s" in by_name[n].targets
    )
    assert not offenders, f"EXCLUDE inspectors wrongly declaring container target: {offenders}"


def test_container_target_parity_invariant() -> None:
    """Container safety is a property of the read sources, not of the runtime:
    every builtin must declare ``docker`` and ``k8s`` together or neither.
    Legitimate breakage (k8s-only read source) requires amending the
    authoring-contract criterion and this assertion — see module docstring."""

    by_name = _load_all_builtin()
    offenders = sorted(
        name
        for name, manifest in by_name.items()
        if ("docker" in manifest.targets) != ("k8s" in manifest.targets)
    )
    assert not offenders, f"manifests breaking docker⇔k8s parity: {offenders}"


# --------------------------------------------------------------------------- #
# 3.10 — content-based meta-guard (independent of the name roster)
# --------------------------------------------------------------------------- #


def test_host_global_marker_commands_never_declare_container_targets() -> None:
    """Any manifest whose collect.command reads a host-global, non-namespaced
    source MUST NOT declare ``docker`` or ``k8s`` — mechanical trap for the
    silent misattribution class (inside a pod these markers read **node**
    global values), independent of the hand-maintained roster."""

    by_name = _load_all_builtin()
    offenders: list[tuple[str, list[str]]] = []
    for name, manifest in by_name.items():
        if "docker" not in manifest.targets and "k8s" not in manifest.targets:
            continue
        cmd = manifest.collect.command
        hits = [mk for mk in _HOST_GLOBAL_MARKERS if mk in cmd]
        if hits:
            offenders.append((name, hits))
    assert not offenders, (
        f"manifests reading host-global sources must not declare docker/k8s: {offenders}"
    )


def test_content_guard_actually_catches_marker_manifests() -> None:
    """Meta-guard on the guard: confirm the marker set is non-vacuous — at least
    one builtin command DOES contain a host-global marker (so the guard above is
    exercising real coverage, not trivially passing on an empty match set)."""

    by_name = _load_all_builtin()
    matched = [
        name
        for name, manifest in by_name.items()
        if any(mk in manifest.collect.command for mk in _HOST_GLOBAL_MARKERS)
    ]
    assert matched, "no builtin command contains a host-global marker — guard is vacuous"
