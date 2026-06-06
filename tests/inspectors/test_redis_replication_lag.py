"""Snapshot tests for the ``redis.replication_lag`` replication-inspector-contract probe.

This inspector is the probe of ``add-replication-inspector-spike`` (the second M6
wave-2 spike): it proves the multi-instance / replication contract on ONE DB.
``redis.replication_lag`` reads the replica-side ``INFO replication`` and normalizes
to the three-tuple ``(replication_configured, link_healthy, lag_seconds)`` — where
``lag_seconds`` is the ``link_freshness`` class (``master_last_io_seconds_ago``, NOT
data apply-lag; see design D-1/D-3/D-8).

All fixtures were recorded by ``_record_redis_replication_lag.py`` driving the real
``InspectorRunner`` against a live redis-repl-master + redis-repl-replica topology,
so the recorded command strings are byte-identical to what the runner sends — replay
hits with zero ``misses``.

Two SEMANTICALLY DISTINCT semantic-abnormal fixtures (design D-5):
  * ``link_down``  — the master container was STOPPED (TCP teardown → link flips to
    ``down`` in seconds, NOT dependent on repl-timeout); ``link_healthy=false`` →
    critical "replication link down" at the DEFAULT thresholds.
  * ``link_stale`` — the master event loop was frozen with ``DEBUG SLEEP`` (35s <<
    repl-timeout 3600s, so the link stays ``up``); ``link_healthy=true`` but
    ``lag_seconds=30`` → critical at the DEFAULT thresholds (the freshness path).

The two are recorded WITH a space+glob-metachar password (``p w*d``) injected as
``HOSTLENS_REDIS_PASSWORD``; the recorder redacts every injected secret value, so the
committed fixtures never carry the plaintext (the redaction regression, task 3.3).

``test_conn_refused_fails_loud`` is the honesty regression lock (design D-4): a
conn-refused backend surfaces as ``status=exception``, never a fabricated healthy
object. The role-contextual fail-loud (a standalone ``role:master`` → ``ok`` +
``replication_configured=false``, NOT exception) is exercised by task 2.3 and the
crosscheck, plus ``test_unconfigured_standalone_no_finding`` below against a stub.
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

_FIXTURES = Path(__file__).parent / "fixtures" / "redis_replication_lag"

#: The space+glob-metachar password the two auth (link_down / link_stale) fixtures
#: were recorded with. The redaction regression asserts it never leaks into a fixture.
_SPECIAL_PW = "p w*d"


def _builtin_root() -> Path:
    pkg_file = _inspectors_pkg.__file__
    assert pkg_file is not None
    return Path(pkg_file).parent / "builtin"


def _manifest_path() -> Path:
    return _builtin_root() / "redis" / "replication_lag.yaml"


def _runner() -> InspectorRunner:
    return InspectorRunner(
        TargetRegistry(),
        settings=Settings(),
        logger=structlog.get_logger("redis-replication-lag-test"),
    )


@pytest.fixture(autouse=True)
def _redis_password_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The manifest declares HOSTLENS_REDIS_PASSWORD as a secret; preflight requires
    # it present. The recorded command text is the if/else template (the password is
    # NEVER inlined — it rides REDISCLI_AUTH via env), so it is byte-identical whether
    # the instance had auth or not. An empty value reproduces every fixture's command
    # path (auth and no-auth alike) and replay hits with zero misses.
    monkeypatch.setenv("HOSTLENS_REDIS_PASSWORD", "")


def test_manifest_loads_cleanly() -> None:
    manifest = load_manifest(_manifest_path())
    assert manifest.name == "redis.replication_lag"
    assert manifest.parse.format == "json"
    assert manifest.requires_binaries == ["redis-cli"]
    assert manifest.secrets == ["HOSTLENS_REDIS_PASSWORD"]
    assert "redis6" in manifest.tags
    assert all("+" not in tag for tag in manifest.tags)
    # The three-tuple output_schema with a nullable lag_seconds.
    props = manifest.output_schema["properties"]
    assert set(props) == {"replication_configured", "link_healthy", "lag_seconds"}
    assert props["lag_seconds"]["type"] == ["integer", "null"]
    # lag semantic class declared in the description (heterogeneity contract, D-3).
    assert "link_freshness" in manifest.description
    # Secret reaches the client only via the REDISCLI_AUTH env remap — never argv.
    cmd = manifest.collect.command
    assert "REDISCLI_AUTH" in cmd
    assert "-a " not in cmd
    assert not (_manifest_path().parent / "hook.py").exists()


async def test_healthy_no_findings() -> None:
    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("replrec", fixture=_FIXTURES / "healthy.json")

    result = await _runner().run(manifest, replay, None)

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {
        "replication_configured": True,
        "link_healthy": True,
        "lag_seconds": 0,
    }
    # lag_seconds 0 < warn_seconds(15) → no finding at the defaults.
    assert result.findings == []


async def test_finding_trigger_emits_warning() -> None:
    """finding-trigger: healthy replica + LOWERED warn_seconds=0 fires a warning.
    Validates finding wiring ONLY (at the defaults this same freshness is healthy)."""

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("replrec", fixture=_FIXTURES / "finding_trigger.json")

    result = await _runner().run(manifest, replay, {"warn_seconds": 0, "critical_seconds": 999})

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output["link_healthy"] is True
    # 0 in [warn=0, critical=999) → a single warning.
    assert [f.severity for f in result.findings] == ["warning"]


async def test_link_stale_critical_at_default_thresholds() -> None:
    """semantic-abnormal #2 (freshness path): a REAL stale-but-up link
    (master_last_io_seconds_ago=30 via DEBUG SLEEP, link still up) fires a critical at
    the manifest DEFAULT thresholds. Distinct from link_down: link_healthy stays True."""

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("replrec", fixture=_FIXTURES / "link_stale.json")

    result = await _runner().run(manifest, replay, None)

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {
        "replication_configured": True,
        "link_healthy": True,
        "lag_seconds": 30,
    }
    # 30 >= critical_seconds(30) → a single critical on the freshness path.
    assert [f.severity for f in result.findings] == ["critical"]
    assert "stale" in result.findings[0].message
    assert "30s" in result.findings[0].message


async def test_link_down_critical_at_default_thresholds() -> None:
    """semantic-abnormal #1 (link path): a REAL broken link (master stopped →
    master_link_status=down) fires a critical "link down" at the DEFAULT thresholds.
    Distinct from link_stale: link_healthy is False and lag_seconds is null."""

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("replrec", fixture=_FIXTURES / "link_down.json")

    result = await _runner().run(manifest, replay, None)

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {
        "replication_configured": True,
        "link_healthy": False,
        "lag_seconds": None,
    }
    assert [f.severity for f in result.findings] == ["critical"]
    assert "link down" in result.findings[0].message.lower()


async def test_conn_refused_fails_loud() -> None:
    """Backend unreachable (conn refused) → status=exception, NOT a fabricated
    healthy object (honesty regression lock, design D-4)."""

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("replrec", fixture=_FIXTURES / "conn_refused.json")

    result = await _runner().run(manifest, replay, {"port": 6390})

    assert replay.misses == []
    assert result.status == "exception"
    assert result.output == {}
    assert result.findings == []


# --------------------------------------------------------------------------- #
# Redaction regression (task 3.3): the two auth fixtures carry NO plaintext secret
# and the fixture schema has no per-command ``env`` field.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["link_down", "link_stale"])
def test_auth_fixture_redacts_special_password(name: str) -> None:
    text = (_FIXTURES / f"{name}.json").read_text(encoding="utf-8")
    assert _SPECIAL_PW not in text, name
    import json

    data = json.loads(text)
    for entry in data.get("commands", []):
        assert "env" not in entry, (name, entry)


# --------------------------------------------------------------------------- #
# Role-contextual unconfigured path (design D-4): a standalone role:master emits
# ok + replication_configured=false + no finding (NOT exception, NOT a fabricated
# lag). Exercised here against a stub returning a standalone INFO so the manifest's
# role-branch is hit without a recorded fixture (task 2.3 runs the live variant).
# --------------------------------------------------------------------------- #


class _StandaloneTarget:
    """Stub whose collector returns the standalone (role:master) normalized triple.

    Preflight probes get canned success; the collector returns the role!=slave
    output the manifest's own command would produce for a standalone instance."""

    type = "local"
    name = "standalone-host"
    capabilities: ClassVar[set[Capability]] = {Capability.SHELL, Capability.FILE_READ}

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        del timeout, env
        if cmd.startswith("command -v ") or cmd.startswith("[ -r "):
            return ExecResult(
                exit_code=0, stdout="", stderr="", duration_seconds=0.0, timed_out=False
            )
        return ExecResult(
            exit_code=0,
            stdout='{"replication_configured":false,"link_healthy":false,"lag_seconds":null}',
            stderr="",
            duration_seconds=0.0,
            timed_out=False,
        )

    async def read_file(self, path: str) -> bytes:
        raise AssertionError(f"read_file must not be reached: {path!r}")


async def test_unconfigured_standalone_no_finding() -> None:
    manifest = load_manifest(_manifest_path())
    target = _StandaloneTarget()

    result = await _runner().run(manifest, target, None)  # type: ignore[arg-type]

    assert result.status == "ok"
    assert result.output == {
        "replication_configured": False,
        "link_healthy": False,
        "lag_seconds": None,
    }
    # Unconfigured (replication_configured=false) → NO finding (a standalone is not
    # a fault); the link-down finding requires replication_configured to be true.
    assert result.findings == []


# --------------------------------------------------------------------------- #
# Failure classification (design D-4, inherited base D-3): missing client binary /
# missing declared secret both map to requires_unmet (a graceful skip).
# --------------------------------------------------------------------------- #


class _NoBinaryTarget:
    """Stub where every ``command -v X`` probe fails (binary absent)."""

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
            return ExecResult(
                exit_code=1, stdout="", stderr="", duration_seconds=0.0, timed_out=False
            )
        raise AssertionError(f"collector must not run when redis-cli is absent: {cmd!r}")

    async def read_file(self, path: str) -> bytes:
        raise AssertionError(f"read_file must not be reached: {path!r}")


async def test_missing_redis_cli_requires_unmet() -> None:
    manifest = load_manifest(_manifest_path())
    target = _NoBinaryTarget()

    result: InspectorResult = await _runner().run(manifest, target, None)  # type: ignore[arg-type]

    assert result.status == "requires_unmet"
    assert result.findings == []
    assert any(m.startswith("bin:") for m in result.missing), result.missing


async def test_missing_secret_env_requires_unmet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOSTLENS_REDIS_PASSWORD", raising=False)
    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("replrec", fixture=_FIXTURES / "healthy.json")

    result = await _runner().run(manifest, replay, None)

    assert result.status == "requires_unmet"
    assert result.findings == []
    assert any("HOSTLENS_REDIS_PASSWORD" in m for m in result.missing), result.missing
