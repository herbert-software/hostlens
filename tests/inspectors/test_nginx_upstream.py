"""Snapshot tests for the ``nginx.upstream`` service inspector.

``nginx.upstream`` scans the static path ``/var/log/nginx/error.log`` with
``LC_ALL=C awk``, collapsing whole-file upstream-failure events into frozen
scalars (design D-4). It has no deterministic exception path (a file-read
inspector) — failure states are ``requires_unmet`` (missing awk / unreadable
log) or ``ok``.

The post-awk JSON in these fixtures is author-crafted (awk does not run offline,
design D-7): the fixtures lock the **parse + findings DSL**, NOT the awk program
itself — the awk collapse logic is verified by the real-nginx Demo Path. The
recorded *command strings* are captured byte-for-byte from the real renderer (via
``_record_nginx_upstream.py``), so ``ReplayTarget`` matching is guaranteed to hit:
  * ``healthy`` — an error.log with no upstream-failure lines → all counters 0 →
    ok, no finding.
  * ``semantic_abnormal`` — a real accumulated upstream-failure state at the
    default ``warn_count`` → warning finding.
  * ``empty_log`` — empty error.log → END{} zero-object → ok, no finding.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import structlog

import hostlens.inspectors as _inspectors_pkg
from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.result import InspectorResult
from hostlens.inspectors.runner import InspectorRunner
from hostlens.targets.base import Capability, ExecResult
from hostlens.targets.registry import TargetRegistry
from hostlens.targets.replay import ReplayTarget

_FIXTURES = Path(__file__).parent / "fixtures" / "nginx_upstream"
_ERROR_LOG = "/var/log/nginx/error.log"


def _builtin_root() -> Path:
    pkg_file = _inspectors_pkg.__file__
    assert pkg_file is not None
    return Path(pkg_file).parent / "builtin"


def _manifest_path() -> Path:
    return _builtin_root() / "nginx" / "upstream.yaml"


def _runner() -> InspectorRunner:
    return InspectorRunner(
        TargetRegistry(),
        settings=Settings(),
        logger=structlog.get_logger("nginx-upstream-test"),
    )


def test_manifest_loads_cleanly() -> None:
    manifest = load_manifest(_manifest_path())
    assert manifest.name == "nginx.upstream"
    assert manifest.parse.format == "json"
    assert manifest.requires_binaries == ["awk"]
    assert manifest.requires_files == [_ERROR_LOG]
    assert manifest.secrets == []
    assert len(manifest.findings) == 1
    assert "nginx" in manifest.tags
    assert all("+" not in tag for tag in manifest.tags)
    assert not (_manifest_path().parent / "hook.py").exists()


async def test_healthy_no_findings() -> None:
    """An error.log with no upstream-failure lines → status=ok with no finding."""

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("nginxrec", fixture=_FIXTURES / "healthy.json")

    result = await _runner().run(manifest, replay, None)

    assert replay.misses == []
    assert result.status == "ok"
    assert result.findings == []
    assert (
        result.output["upstream_error_count"]
        < manifest.parameters["properties"]["warn_count"]["default"]
    )  # type: ignore[index]


async def test_semantic_abnormal_warning_at_default_thresholds() -> None:
    """A real accumulated upstream-failure state recorded at the default
    ``warn_count`` → warning finding with the declared message semantics."""

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("nginxrec", fixture=_FIXTURES / "semantic_abnormal.json")

    result = await _runner().run(manifest, replay, None)

    assert replay.misses == []
    assert result.status == "ok"
    assert (
        result.output["upstream_error_count"]
        >= manifest.parameters["properties"]["warn_count"]["default"]
    )  # type: ignore[index]
    assert [f.severity for f in result.findings] == ["warning"]
    msg = result.findings[0].message
    assert "upstream" in msg
    assert str(result.output["upstream_error_count"]) in msg


async def test_empty_log_zero_object() -> None:
    """Empty error.log → END{} zero-object → ok, no finding."""

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("nginxrec", fixture=_FIXTURES / "empty_log.json")

    result = await _runner().run(manifest, replay, None)

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {
        "upstream_error_count": 0,
        "timed_out": 0,
        "no_live_upstreams": 0,
        "connect_failed": 0,
        "prematurely_closed": 0,
    }
    assert result.findings == []


class _NoBinaryTarget:
    """Stub target where every ``command -v X`` probe fails (binary absent)."""

    type = "local"
    name = "no-binary-host"
    capabilities: ClassVar[set[Capability]] = {Capability.SHELL}

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
                exit_code=1, stdout="", stderr="", duration_seconds=0.0, timed_out=False
            )
        if cmd.startswith("[ -r "):
            return ExecResult(
                exit_code=0, stdout="", stderr="", duration_seconds=0.0, timed_out=False
            )
        raise AssertionError(f"collector must not run when awk is absent: {cmd!r}")

    async def read_file(self, path: str) -> bytes:
        raise AssertionError(f"read_file must not be reached: {path!r}")


class _NoErrorLogTarget:
    """Stub target where awk is present but the error log is not readable."""

    type = "local"
    name = "no-error-log-host"
    capabilities: ClassVar[set[Capability]] = {Capability.SHELL}

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
        if cmd.startswith("[ -r ") and _ERROR_LOG in cmd:
            return ExecResult(
                exit_code=1, stdout="", stderr="", duration_seconds=0.0, timed_out=False
            )
        raise AssertionError(f"collector must not run when error log is absent: {cmd!r}")

    async def read_file(self, path: str) -> bytes:
        raise AssertionError(f"read_file must not be reached: {path!r}")


async def test_missing_awk_binary_requires_unmet() -> None:
    """A target without awk → preflight requires_unmet skip (a premise gap)."""

    manifest = load_manifest(_manifest_path())
    target = _NoBinaryTarget()

    result: InspectorResult = await _runner().run(manifest, target, None)  # type: ignore[arg-type]

    assert result.status == "requires_unmet"
    assert result.findings == []
    assert any(m.startswith("bin:") for m in result.missing), result.missing


async def test_missing_error_log_requires_unmet() -> None:
    """Missing / unreadable error log → requires_files preflight → requires_unmet
    (NOT exception — nginx.upstream is a static-log file-read inspector)."""

    manifest = load_manifest(_manifest_path())
    target = _NoErrorLogTarget()

    result: InspectorResult = await _runner().run(manifest, target, None)  # type: ignore[arg-type]

    assert result.status == "requires_unmet"
    assert result.findings == []
    assert any(m.startswith("file:") for m in result.missing), result.missing
