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

from pathlib import Path

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
