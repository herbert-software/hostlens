"""Snapshot tests for the ``postgres.replication_lag`` replication-inspector-contract probe.

This is the postgres wave-3 inspector of ``add-postgres-replication-lag-inspector``:
the FIRST master-side replication probe. Unlike redis/mysql (replica-side, N=1), it
reads the primary's ``pg_stat_replication`` (one row per online standby/walsender) and
reduces the multi-row view via a SINGLE SQL aggregate
(``count(*) / bool_and(coalesce(state::text,'')='streaming') /
FLOOR(EXTRACT(EPOCH FROM max(replay_lag)))::bigint``) into the three-tuple
``(replication_configured, link_healthy, lag_seconds)`` — ``lag_seconds`` is the
``apply_lag`` class (``replay_lag``, an integer, NULL→null).

All fixtures were recorded by ``_record_postgres_replication_lag.py`` driving the real
``InspectorRunner`` against a live pg-repl-primary + 3 standby topology, so the recorded
command strings are byte-identical to what the runner sends — replay hits with zero misses.

Reduction correctness is verified AT RECORD TIME (the recorder recomputes max/AND from a
single-snapshot CTE; replay does NOT re-run the SQL); these snapshots replay the FROZEN
triple to lock the DSL/parse + finding behaviour. The crosscheck
(``test_replication_contract_crosscheck.py``) additionally asserts the master-side
reduction is non-trivial via ``multi_replica.json`` and the empty-set / under-priv guards.

Key postgres-specific fixtures (no redis/mysql analogue):
  * ``multi_replica``      — 3 distinct-lag streaming standbys + 1 ``backup`` walsender →
    ``link_healthy=false`` (AND over a non-streaming row), ``lag_seconds=max``.
  * ``unconfigured``       — empty ``pg_stat_replication`` → ``(false,false,null)``, ok,
    no finding (empty-set guard; master-side cannot detect total disconnect).
  * ``underprivileged_all``— CONNECT-only role; state cols NULL →
    ``bool_and(coalesce(...))=false`` → critical (LOUD, not silent false-healthy).
  * ``idle``               — streaming + idle primary → ``replay_lag`` NULL →
    ``lag_seconds=null``, no finding.

Two SEMANTICALLY DISTINCT semantic-abnormal fixtures:
  * ``link_down`` — a present row in a non-streaming state (``backup`` via throttled
    ``pg_basebackup``; ``catchup`` ships sub-poll-interval on fast loopback and is too
    transient to latch) → ``link_healthy=false`` → critical "link down".
  * ``lagging``   — ``recovery_min_apply_delay`` + primary write loop → ``replay_lag>=30``
    while ``state='streaming'`` → ``link_healthy=true`` → critical apply-lag.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog

import hostlens.inspectors as _inspectors_pkg
from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.runner import InspectorRunner
from hostlens.targets.registry import TargetRegistry
from hostlens.targets.replay import ReplayTarget

_FIXTURES = Path(__file__).parent / "fixtures" / "postgres_replication_lag"
_SPECIAL_PW = "p w*d"
_BASE_PARAMS: dict[str, object] = {"user": "postgres"}


def _builtin_root() -> Path:
    pkg_file = _inspectors_pkg.__file__
    assert pkg_file is not None
    return Path(pkg_file).parent / "builtin"


def _manifest_path() -> Path:
    return _builtin_root() / "postgres" / "replication_lag.yaml"


def _runner() -> InspectorRunner:
    return InspectorRunner(
        TargetRegistry(),
        settings=Settings(),
        logger=structlog.get_logger("postgres-replication-lag-test"),
    )


def _params(**extra: object) -> dict[str, object]:
    return {**_BASE_PARAMS, **extra}


@pytest.fixture(autouse=True)
def _postgres_password_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Manifest declares HOSTLENS_POSTGRES_PASSWORD; preflight requires it present.
    # Recorded command text is byte-identical regardless of value (the password
    # rides PGPASSWORD via env, never inlined) — replay hits with zero misses.
    monkeypatch.setenv("HOSTLENS_POSTGRES_PASSWORD", _SPECIAL_PW)


def test_manifest_loads_cleanly() -> None:
    manifest = load_manifest(_manifest_path())
    assert manifest.name == "postgres.replication_lag"
    assert manifest.parse.format == "json"
    assert manifest.requires_binaries == ["psql"]
    assert manifest.secrets == ["HOSTLENS_POSTGRES_PASSWORD"]
    assert "postgres" in manifest.tags
    props = manifest.output_schema["properties"]
    assert set(props) == {"replication_configured", "link_healthy", "lag_seconds"}
    assert props["lag_seconds"]["type"] == ["integer", "null"]
    assert "apply_lag" in manifest.description
    # Master-side prerequisites declared (no other mechanical gate).
    assert "pg_monitor" in manifest.description
    assert "primary" in manifest.description.lower()
    cmd = manifest.collect.command
    assert "PGPASSWORD" in cmd
    assert "-W" not in cmd
    # NULL-state neutralisation (L1) is in the collector SQL, not bare bool_and.
    assert "coalesce(state" in cmd
    assert not (_manifest_path().parent / "hook.py").exists()


async def test_idle_streaming_null_lag_no_finding() -> None:
    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("replrec", fixture=_FIXTURES / "idle.json")
    result = await _runner().run(manifest, replay, _params())
    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {
        "replication_configured": True,
        "link_healthy": True,
        "lag_seconds": None,
    }
    assert result.findings == []


async def test_healthy_small_lag_no_finding() -> None:
    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("replrec", fixture=_FIXTURES / "healthy.json")
    result = await _runner().run(manifest, replay, _params())
    assert replay.misses == []
    assert result.status == "ok"
    assert result.output["replication_configured"] is True
    assert result.output["link_healthy"] is True
    lag = result.output["lag_seconds"]
    assert lag is not None and lag < 15  # below default warn
    assert result.findings == []


async def test_finding_trigger_emits_warning() -> None:
    """healthy topology + LOWERED warn_seconds=0 fires a warning (wiring only)."""
    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("replrec", fixture=_FIXTURES / "finding_trigger.json")
    result = await _runner().run(manifest, replay, _params(warn_seconds=0, critical_seconds=999))
    assert replay.misses == []
    assert result.status == "ok"
    assert result.output["link_healthy"] is True
    assert [f.severity for f in result.findings] == ["warning"]


async def test_lagging_critical_at_default_thresholds() -> None:
    """semantic-abnormal #2: recovery_min_apply_delay + write loop → replay_lag>=30
    while state='streaming' → link_healthy=true, critical apply-lag. Distinct from
    link_down (link_healthy stays True)."""
    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("replrec", fixture=_FIXTURES / "lagging.json")
    result = await _runner().run(manifest, replay, _params())
    assert replay.misses == []
    assert result.status == "ok"
    assert result.output["link_healthy"] is True
    lag = result.output["lag_seconds"]
    assert lag is not None and lag >= 30
    assert [f.severity for f in result.findings] == ["critical"]
    assert "lag" in result.findings[0].message.lower()


async def test_link_down_critical_at_default_thresholds() -> None:
    """semantic-abnormal #1: a present row in a non-streaming state ('backup') →
    link_healthy=false → critical "link down". Distinct from lagging (link_healthy
    False; a lag finding is suppressed by the link_healthy guard)."""
    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("replrec", fixture=_FIXTURES / "link_down.json")
    result = await _runner().run(manifest, replay, _params())
    assert replay.misses == []
    assert result.status == "ok"
    assert result.output["replication_configured"] is True
    assert result.output["link_healthy"] is False
    assert [f.severity for f in result.findings] == ["critical"]
    assert "link down" in result.findings[0].message.lower()
    assert _SPECIAL_PW not in (_FIXTURES / "link_down.json").read_text()


async def test_multi_replica_reduction_link_false_lag_max() -> None:
    """Master-side reduction over >=2 distinct streaming + 1 non-streaming row:
    link_healthy=false (AND), lag_seconds=max of the non-NULL streaming lags."""
    manifest = load_manifest(_manifest_path())
    crit = manifest.parameters["properties"]["critical_seconds"]["default"]
    replay = ReplayTarget("replrec", fixture=_FIXTURES / "multi_replica.json")
    result = await _runner().run(manifest, replay, _params())
    assert replay.misses == []
    assert result.status == "ok"
    assert result.output["link_healthy"] is False
    assert result.output["lag_seconds"] is not None
    assert result.output["lag_seconds"] >= crit
    assert [f.severity for f in result.findings] == ["critical"]
    assert "link down" in result.findings[0].message.lower()


async def test_unconfigured_empty_set_ok_no_finding() -> None:
    """Empty pg_stat_replication → (false,false,null), ok, no finding (empty-set
    guard; master-side single-primary and total-disconnect are indistinguishable)."""
    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("replrec", fixture=_FIXTURES / "unconfigured.json")
    result = await _runner().run(manifest, replay, _params())
    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {
        "replication_configured": False,
        "link_healthy": False,
        "lag_seconds": None,
    }
    assert result.findings == []


async def test_underprivileged_all_is_loud_critical() -> None:
    """CONNECT-only role: state cols NULL → coalesce→false → link_healthy false →
    critical (LOUD, not silent false-healthy). Regression guard for the coalesce
    neutralisation of bool_and's NULL-ignore (L1)."""
    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("replrec", fixture=_FIXTURES / "underprivileged_all.json")
    result = await _runner().run(manifest, replay, {"user": "lowmon"})
    assert replay.misses == []
    assert result.status == "ok"
    assert result.output["link_healthy"] is False
    assert [f.severity for f in result.findings] == ["critical"]
    assert _SPECIAL_PW not in (_FIXTURES / "underprivileged_all.json").read_text()


async def test_conn_refused_fails_loud() -> None:
    """Backend unreachable → status=exception, never a fabricated healthy object."""
    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("replrec", fixture=_FIXTURES / "conn_refused.json")
    result = await _runner().run(manifest, replay, _params(port=15439))
    assert replay.misses == []
    assert result.status == "exception"
    assert result.output == {}
    assert result.findings == []
