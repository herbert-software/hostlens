"""Snapshot tests for the ``postgres.long_queries`` service inspector.

Service-inspector-contract probe for ``psql`` + ``HOSTLENS_POSTGRES_PASSWORD``
remapped to ``PGPASSWORD``, querying ``pg_stat_activity`` with
``pid <> pg_backend_pid()`` self-exclusion and a frozen scalar aggregate
(``long_query_count`` / ``max_duration_seconds``) at sample time (D-1).

Fixtures live under ``fixtures/postgres_long_queries/`` and will be recorded by
the dev-tool recorder driving the real ``InspectorRunner`` against compose
postgres with a background ``pg_sleep`` workload for semantic-abnormal.

Failure-classification locks:
  * ``test_access_denied_fails_loud`` — auth failure → ``status=exception``.
  * ``test_conn_refused_fails_loud`` — unreachable backend → ``status=exception``.
  * ``test_missing_psql_binary_requires_unmet`` /
    ``test_missing_secret_env_requires_unmet`` — premise gaps →
    ``status=requires_unmet``.

Acceptance sufficiency rests on
``test_semantic_abnormal_warning_at_default_thresholds`` (a real sustained long
query crossing the DEFAULT ``warn_count``); healthy fixture proves self-exclusion
(``long_query_count=0`` even though the inspector's own backend is active).
"""

from __future__ import annotations

import json
import re
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

_FIXTURES = Path(__file__).parent / "fixtures" / "postgres_long_queries"


def _builtin_root() -> Path:
    pkg_file = _inspectors_pkg.__file__
    assert pkg_file is not None
    return Path(pkg_file).parent / "builtin"


def _manifest_path() -> Path:
    return _builtin_root() / "postgres" / "long_queries.yaml"


def _runner() -> InspectorRunner:
    return InspectorRunner(
        TargetRegistry(),
        settings=Settings(),
        logger=structlog.get_logger("postgres-long-queries-test"),
    )


def _recorded_host(fixture: Path) -> str:
    """The ``-h <host>`` value baked into the fixture's main collect command."""

    main = json.loads(fixture.read_text())["commands"][-1]
    match = re.search(r"-h (\S+)", main["cmd"])
    assert match is not None, main["cmd"]
    return match.group(1)


@pytest.fixture(autouse=True)
def _postgres_pwd_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOSTLENS_POSTGRES_PASSWORD", "test-" + "pw")


def test_manifest_loads_cleanly() -> None:
    manifest = load_manifest(_manifest_path())
    assert manifest.name == "postgres.long_queries"
    assert manifest.parse.format == "json"
    assert manifest.requires_binaries == ["psql"]
    assert manifest.secrets == ["HOSTLENS_POSTGRES_PASSWORD"]
    assert all("+" not in tag for tag in manifest.tags)
    assert not (_manifest_path().parent / "hook.py").exists()


async def test_healthy_no_findings() -> None:
    """Healthy instance with no external long queries → count=0.

    Self-exclusion (``pid <> pg_backend_pid()``) ensures the inspector's own
    active backend is NOT counted — count stays 0 even while this query runs.
    """

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("pgrec", fixture=_FIXTURES / "healthy.json")

    result = await _runner().run(manifest, replay, {"user": "postgres"})

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {
        "long_query_count": 0,
        "max_duration_seconds": 0,
    }
    assert result.findings == []


async def test_semantic_abnormal_warning_at_default_thresholds() -> None:
    """A real sustained ``pg_sleep`` backend active past the DEFAULT
    ``threshold_seconds`` (60) → ``long_query_count >= warn_count`` (default 1)
    at DEFAULT thresholds — genuine long-query state, not a lowered threshold.
    """

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("pgrec", fixture=_FIXTURES / "semantic_abnormal.json")

    result = await _runner().run(manifest, replay, {"user": "postgres"})

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output["long_query_count"] >= 1
    assert result.output["max_duration_seconds"] >= 60
    assert [f.severity for f in result.findings] == ["warning"]
    assert "条运行超过" in result.findings[0].message
    assert "60s" in result.findings[0].message


async def test_access_denied_fails_loud() -> None:
    """Auth failure (wrong password) → status=exception, NOT a fabricated
    healthy long_query_count=0.
    """

    manifest = load_manifest(_manifest_path())
    fixture = _FIXTURES / "access_denied.json"
    replay = ReplayTarget("pgrec", fixture=fixture)

    result = await _runner().run(
        manifest, replay, {"user": "postgres", "host": _recorded_host(fixture)}
    )

    assert replay.misses == []
    assert result.status == "exception"
    assert result.output == {}
    assert result.findings == []


async def test_conn_refused_fails_loud() -> None:
    """Backend unreachable (port closed) → status=exception."""

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("pgrec", fixture=_FIXTURES / "conn_refused.json")

    result = await _runner().run(manifest, replay, {"user": "postgres", "port": 15999})

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
        raise AssertionError(f"collector must not run when psql is absent: {cmd!r}")

    async def read_file(self, path: str) -> bytes:
        raise AssertionError(f"read_file must not be reached: {path!r}")


async def test_missing_psql_binary_requires_unmet() -> None:
    """A target without the psql client → preflight requires_unmet skip."""

    manifest = load_manifest(_manifest_path())
    target = _NoBinaryTarget()

    result: InspectorResult = await _runner().run(manifest, target, {"user": "postgres"})  # type: ignore[arg-type]

    assert result.status == "requires_unmet"
    assert result.findings == []
    assert any(m.startswith("bin:") for m in result.missing), result.missing


async def test_missing_secret_env_requires_unmet(monkeypatch: pytest.MonkeyPatch) -> None:
    """A declared secret absent from the environment → preflight requires_unmet."""

    monkeypatch.delenv("HOSTLENS_POSTGRES_PASSWORD", raising=False)
    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("pgrec", fixture=_FIXTURES / "healthy.json")

    result = await _runner().run(manifest, replay, {"user": "postgres"})

    assert result.status == "requires_unmet"
    assert result.missing == ["env:HOSTLENS_POSTGRES_PASSWORD"]
    assert result.output == {}
