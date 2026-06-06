"""Snapshot tests for the ``mysql.slow_queries`` service inspector.

Service-inspector-contract probe for ``mysql`` + ``HOSTLENS_MYSQL_PWD``
remapped to ``MYSQL_PWD``, querying ``mysql.slow_log`` with server-side
``NOW() - INTERVAL lookback_seconds`` window aggregation and a monitoring
enablement probe (``slow_log_monitoring_enabled``) at sample time (D-1/D-2).

Fixtures live under ``fixtures/mysql_slow_queries/`` and will be recorded by
the dev-tool recorder driving the real ``InspectorRunner`` against compose
mysql with TABLE slow-log enabled and real ``SELECT SLEEP`` workload for
semantic-abnormal.

Failure-classification locks:
  * ``test_access_denied_fails_loud`` — auth failure → ``status=exception``.
  * ``test_conn_refused_fails_loud`` — unreachable backend → ``status=exception``.
  * ``test_missing_mysql_binary_requires_unmet`` /
    ``test_missing_secret_env_requires_unmet`` — premise gaps →
    ``status=requires_unmet``.

Acceptance sufficiency rests on
``test_semantic_abnormal_warning_at_default_thresholds`` (real accumulated slow
queries crossing the DEFAULT ``warn_count``); healthy fixture proves zero slow
queries in-window with monitoring enabled.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
import structlog

import hostlens.inspectors as _inspectors_pkg
from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.result import InspectorResult
from hostlens.inspectors.runner import InspectorRunner
from hostlens.targets.base import Capability, ExecResult
from hostlens.targets.registry import TargetRegistry
from hostlens.targets.replay import ReplayTarget

_FIXTURES = Path(__file__).parent / "fixtures" / "mysql_slow_queries"


def _builtin_root() -> Path:
    pkg_file = _inspectors_pkg.__file__
    assert pkg_file is not None
    return Path(pkg_file).parent / "builtin"


def _manifest_path() -> Path:
    return _builtin_root() / "mysql" / "slow_queries.yaml"


def _runner() -> InspectorRunner:
    return InspectorRunner(
        TargetRegistry(),
        settings=Settings(),
        logger=structlog.get_logger("mysql-slow-queries-test"),
    )


@pytest.fixture(autouse=True)
def _mysql_pwd_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOSTLENS_MYSQL_PWD", "test-" + "pw")


def test_manifest_loads_cleanly() -> None:
    manifest = load_manifest(_manifest_path())
    assert manifest.name == "mysql.slow_queries"
    assert manifest.parse.format == "json"
    assert manifest.requires_binaries == ["mysql"]
    assert manifest.secrets == ["HOSTLENS_MYSQL_PWD"]
    assert all("+" not in tag for tag in manifest.tags)
    assert not (_manifest_path().parent / "hook.py").exists()


async def test_healthy_no_findings() -> None:
    """Monitoring enabled, zero slow queries in lookback window → ok, no finding."""

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("mysqlrec", fixture=_FIXTURES / "healthy.json")

    result = await _runner().run(manifest, replay, {"user": "root"})

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {
        "slow_query_count": 0,
        "slow_log_monitoring_enabled": True,
    }
    assert result.findings == []


async def test_monitoring_disabled_warning() -> None:
    """``slow_query_log`` OFF or ``log_output=FILE`` only →
    ``slow_log_monitoring_enabled=false`` → warning (honest blind-spot exposure),
    NOT a silent healthy zero count.
    """

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("mysqlrec", fixture=_FIXTURES / "monitoring_disabled.json")

    result = await _runner().run(manifest, replay, {"user": "root"})

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output["slow_log_monitoring_enabled"] is False
    assert [f.severity for f in result.findings] == ["warning"]
    assert "未启用" in result.findings[0].message


async def test_semantic_abnormal_warning_at_default_thresholds() -> None:
    """Real accumulated slow queries in the lookback window →
    ``slow_query_count >= warn_count`` at DEFAULT thresholds — genuine slow-query
    state, not a lowered threshold.
    """

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("mysqlrec", fixture=_FIXTURES / "semantic_abnormal.json")
    warn_default = manifest.parameters["properties"]["warn_count"]["default"]  # type: ignore[index]

    result = await _runner().run(manifest, replay, {"user": "root"})

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output["slow_log_monitoring_enabled"] is True
    assert result.output["slow_query_count"] >= warn_default
    assert [f.severity for f in result.findings] == ["warning"]
    assert str(result.output["slow_query_count"]) in result.findings[0].message


async def test_access_denied_fails_loud() -> None:
    """Auth failure (wrong password) → status=exception, NOT a fabricated
    healthy slow_query_count=0.
    """

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("mysqlrec", fixture=_FIXTURES / "access_denied.json")

    result = await _runner().run(manifest, replay, {"user": "root"})

    assert replay.misses == []
    assert result.status == "exception"
    assert result.output == {}
    assert result.findings == []


async def test_conn_refused_fails_loud() -> None:
    """Backend unreachable (port closed) → status=exception."""

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("mysqlrec", fixture=_FIXTURES / "conn_refused.json")

    result = await _runner().run(manifest, replay, {"user": "root", "port": 13999})

    assert replay.misses == []
    assert result.status == "exception"
    assert result.output == {}
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
        raise AssertionError(f"collector must not run when mysql is absent: {cmd!r}")

    async def read_file(self, path: str) -> bytes:
        raise AssertionError(f"read_file must not be reached: {path!r}")


async def test_missing_mysql_binary_requires_unmet() -> None:
    """A target without the mysql client → preflight requires_unmet skip."""

    manifest = load_manifest(_manifest_path())
    target = _NoBinaryTarget()

    result: InspectorResult = await _runner().run(manifest, target, {"user": "root"})  # type: ignore[arg-type]

    assert result.status == "requires_unmet"
    assert result.findings == []
    assert any(m.startswith("bin:") for m in result.missing), result.missing


async def test_missing_secret_env_requires_unmet(monkeypatch: pytest.MonkeyPatch) -> None:
    """A declared secret absent from the environment → preflight requires_unmet."""

    monkeypatch.delenv("HOSTLENS_MYSQL_PWD", raising=False)
    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("mysqlrec", fixture=_FIXTURES / "healthy.json")

    result = await _runner().run(manifest, replay, {"user": "root"})

    assert result.status == "requires_unmet"
    assert result.missing == ["env:HOSTLENS_MYSQL_PWD"]
    assert result.output == {}
