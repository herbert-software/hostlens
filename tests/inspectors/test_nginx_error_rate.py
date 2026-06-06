"""Snapshot tests for the ``nginx.error_rate`` service inspector.

``nginx.error_rate`` scans the static path ``/var/log/nginx/access.log`` with
``LC_ALL=C awk``, aggregating whole-file 5xx/total into frozen scalars (design
D-1/D-4). It has no deterministic exception path (file-read inspector) — failure
states are ``requires_unmet`` (missing awk / unreadable log) or ``ok``.

Fixtures were recorded by ``_record_nginx_error_rate.py`` driving the real
``InspectorRunner``:
  * ``healthy`` — high traffic, low 5xx rate → ok, no finding.
  * ``semantic_abnormal`` — real 5xx traffic at default ``warn_pct`` /
    ``min_requests`` → warning finding.
  * ``small_sample`` — e.g. total=1 with a 5xx → ok, no finding (``min_requests``
    gate).
  * ``empty_log`` — zero lines → ok zero-object (``END{}`` divide-by-zero guard).
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

_FIXTURES = Path(__file__).parent / "fixtures" / "nginx_error_rate"
_ACCESS_LOG = "/var/log/nginx/access.log"


def _builtin_root() -> Path:
    pkg_file = _inspectors_pkg.__file__
    assert pkg_file is not None
    return Path(pkg_file).parent / "builtin"


def _manifest_path() -> Path:
    return _builtin_root() / "nginx" / "error_rate.yaml"


def _runner() -> InspectorRunner:
    return InspectorRunner(
        TargetRegistry(),
        settings=Settings(),
        logger=structlog.get_logger("nginx-error-rate-test"),
    )


def test_manifest_loads_cleanly() -> None:
    manifest = load_manifest(_manifest_path())
    assert manifest.name == "nginx.error_rate"
    assert manifest.parse.format == "json"
    assert manifest.requires_binaries == ["awk"]
    assert manifest.requires_files == [_ACCESS_LOG]
    assert manifest.secrets == []
    assert len(manifest.findings) == 1
    assert "nginx" in manifest.tags
    assert all("+" not in tag for tag in manifest.tags)
    assert not (_manifest_path().parent / "hook.py").exists()


async def test_healthy_no_findings() -> None:
    """High traffic, low 5xx rate → status=ok with no finding."""

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("nginxrec", fixture=_FIXTURES / "healthy.json")

    result = await _runner().run(manifest, replay, None)

    assert replay.misses == []
    assert result.status == "ok"
    assert result.findings == []
    assert (
        result.output["total_requests"]
        >= manifest.parameters["properties"]["min_requests"]["default"]
    )  # type: ignore[index]
    assert (
        result.output["error_rate_pct"] < manifest.parameters["properties"]["warn_pct"]["default"]
    )  # type: ignore[index]


async def test_semantic_abnormal_warning_at_default_thresholds() -> None:
    """Real 5xx traffic recorded at default ``warn_pct`` / ``min_requests`` →
    warning finding with the declared message semantics."""

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("nginxrec", fixture=_FIXTURES / "semantic_abnormal.json")

    result = await _runner().run(manifest, replay, None)

    assert replay.misses == []
    assert result.status == "ok"
    assert (
        result.output["error_rate_pct"] >= manifest.parameters["properties"]["warn_pct"]["default"]
    )  # type: ignore[index]
    assert (
        result.output["total_requests"]
        >= manifest.parameters["properties"]["min_requests"]["default"]
    )  # type: ignore[index]
    assert [f.severity for f in result.findings] == ["warning"]
    msg = result.findings[0].message
    assert "5xx error rate" in msg
    assert str(result.output["error_rate_pct"]) in msg


async def test_small_sample_no_finding() -> None:
    """A single request (total=1) with a 5xx does NOT trigger — the
    ``min_requests`` gate suppresses small-sample false positives."""

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("nginxrec", fixture=_FIXTURES / "small_sample.json")

    result = await _runner().run(manifest, replay, None)

    assert replay.misses == []
    assert result.status == "ok"
    assert (
        result.output["total_requests"]
        < manifest.parameters["properties"]["min_requests"]["default"]
    )  # type: ignore[index]
    assert result.findings == []


async def test_empty_log_zero_object() -> None:
    """Empty access log → END{} divide-by-zero guard → ok zero-object."""

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("nginxrec", fixture=_FIXTURES / "empty_log.json")

    result = await _runner().run(manifest, replay, None)

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {
        "total_requests": 0,
        "error_5xx_count": 0,
        "error_rate_pct": 0.0,
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


class _NoAccessLogTarget:
    """Stub target where awk is present but the access log is not readable."""

    type = "local"
    name = "no-access-log-host"
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
        if cmd.startswith("[ -r ") and _ACCESS_LOG in cmd:
            return ExecResult(
                exit_code=1, stdout="", stderr="", duration_seconds=0.0, timed_out=False
            )
        raise AssertionError(f"collector must not run when access log is absent: {cmd!r}")

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


async def test_missing_access_log_requires_unmet() -> None:
    """Missing / unreadable access log → requires_files preflight → requires_unmet."""

    manifest = load_manifest(_manifest_path())
    target = _NoAccessLogTarget()

    result: InspectorResult = await _runner().run(manifest, target, None)  # type: ignore[arg-type]

    assert result.status == "requires_unmet"
    assert result.findings == []
    assert any(m.startswith("file:") for m in result.missing), result.missing
