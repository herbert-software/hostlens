"""Snapshot tests for the ``redis.slowlog`` metrics-only inspector.

Activity example for the Inspector Authoring Contract (version-sensitive CLI
form): the inspector reports slow-query ``count`` + ``max_micros`` ONLY and
never echoes slow-command argument bytes, so ``redis-cli --json`` can never
emit invalid UTF-8 into stdout (the binary-args boundary that motivated the
metrics-only scope — see the manifest header).

This inspector now follows the service-inspector contract: the secret is
declared as ``HOSTLENS_REDIS_PASSWORD`` (the ``HOSTLENS_`` prefix per the
ssh-execution-target contract) and REMAPPED inside the collector to redis-cli's
native ``REDISCLI_AUTH`` env channel, so the password never reaches argv. The
collector also carries a ``-t 5`` client connect timeout (< the 15s collect
timeout per the contract MUST).

The fixtures were recorded by the dev-tool recorder driving the real
``InspectorRunner`` against the real ``redis`` service (nonempty / empty
slowlog), plus a fail-loud fixture pointing redis-cli at a closed port, so the
recorded command strings are byte-identical to what the runner sends — replay
hits with zero ``misses``. See ``_record_redis_slowlog.py`` for the recorder.

``test_conn_refused_fails_loud`` is the honesty regression lock (Authoring
Contract rule 8): a conn-refused backend must surface as ``status=exception``,
never as a fabricated healthy ``{"count":0}``.

D-7 correctness anchor caveat: the snapshot ``count`` / ``max_micros`` integers
only pin "what the recorder produced on that recording run" — the real anchor
for collector correctness is the finding severity sequence plus the live re-record
against a real redis (task 7.2), NOT the literal scalar values. Do not claim the
snapshot "locks collector correctness".
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

_FIXTURES = Path(__file__).parent / "fixtures" / "redis"

#: The special-char password the special-char fixture was recorded with. Set as
#: the secret env value when replaying that fixture so the rendered command
#: (which references the secret only via env, never argv) matches byte-for-byte.
_SPECIAL_PW = "p w*d"


def _builtin_root() -> Path:
    pkg_file = _inspectors_pkg.__file__
    assert pkg_file is not None
    return Path(pkg_file).parent / "builtin"


def _manifest_path() -> Path:
    return _builtin_root() / "redis" / "slowlog.yaml"


def _runner() -> InspectorRunner:
    return InspectorRunner(
        TargetRegistry(),
        settings=Settings(),
        logger=structlog.get_logger("redis-slowlog-test"),
    )


@pytest.fixture(autouse=True)
def _redis_password_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The manifest declares HOSTLENS_REDIS_PASSWORD as a secret; preflight
    # requires it in the environment. The recorded instances had no auth, so an
    # empty value reproduces the recorded (no REDISCLI_AUTH) command path. Tests
    # that need the auth path (special-char password) or the legacy-env BREAKING
    # regression override this per-test.
    monkeypatch.setenv("HOSTLENS_REDIS_PASSWORD", "")


def test_manifest_loads_cleanly() -> None:
    manifest = load_manifest(_manifest_path())
    assert manifest.name == "redis.slowlog"
    assert manifest.parse.format == "json"
    assert manifest.requires_binaries == ["redis-cli"]
    assert manifest.secrets == ["HOSTLENS_REDIS_PASSWORD"]
    # Version premise declared via tags (no `+`) + free-text description.
    assert "redis6" in manifest.tags
    assert "json-client" in manifest.tags
    assert all("+" not in tag for tag in manifest.tags)
    # Secret reaches the client only via the REDISCLI_AUTH env remap — never argv.
    cmd = manifest.collect.command
    assert "REDISCLI_AUTH" in cmd
    assert '-a "$' not in cmd  # no argv plaintext password
    # No hook.py sibling — the metrics-only path is pure YAML.
    assert not (_manifest_path().parent / "hook.py").exists()


async def test_nonempty_slowlog_derives_findings() -> None:
    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("redisrec", fixture=_FIXTURES / "slowlog_nonempty.json")

    result = await _runner().run(manifest, replay, None)

    assert replay.misses == []
    assert result.status == "ok"
    # Metrics-only output: scalar count + max_micros, no command text. These are
    # the values from THIS recording run (D-7); the correctness anchor is the
    # finding severity sequence below + the live re-record, not the literals.
    assert result.output == {"count": 8, "max_micros": 12}
    # count=8 >= warn_count(1) but < critical_count(10); max_micros(12) <
    # slow_micros(100000) → only the count rule fires (single warning).
    assert [f.severity for f in result.findings] == ["warning"]
    assert "8 slow queries" in result.findings[0].message


async def test_empty_slowlog_no_findings() -> None:
    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("redisrec", fixture=_FIXTURES / "slowlog_empty.json")

    result = await _runner().run(manifest, replay, None)

    assert replay.misses == []
    assert result.status == "ok"
    # Genuine empty slowlog: redis-cli succeeds and returns count=0 (a real
    # integer), so the collector emits a valid {"count":0,...} → status=ok.
    assert result.output == {"count": 0, "max_micros": 0}
    assert result.findings == []


async def test_semantic_abnormal_at_default_thresholds() -> None:
    """semantic-abnormal fixture: a real slow query whose duration crosses the
    DEFAULT ``slow_micros`` (100000) fires the max_micros rule at the manifest
    default thresholds (no override). This is the contract's proof-of-detection
    track for the max_micros JUDGE rail — distinct from the count rule, which is
    near-vacuous at warn_count=1. The healthy nonempty fixture above has
    max_micros=12 and so does NOT trip this rule, proving the rail discriminates.
    """

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("redisrec", fixture=_FIXTURES / "slowlog_semantic_abnormal.json")

    # DEFAULT thresholds — no parameter override.
    result = await _runner().run(manifest, replay, None)

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {"count": 3, "max_micros": 151260}
    # count=3 in [warn=1, critical=10) → a count warning; max_micros(151260) >=
    # slow_micros(100000) → a max_micros warning. Both fire (rule order).
    assert [f.severity for f in result.findings] == ["warning", "warning"]
    # Assert the max_micros rule specifically (NOT just the count rule) — this is
    # the discriminating JUDGE rail this fixture exists to exercise.
    max_micros_findings = [f for f in result.findings if "151260 micros" in f.message]
    assert len(max_micros_findings) == 1
    assert "Redis slowest query took 151260 micros" in max_micros_findings[0].message


async def test_special_char_pw(monkeypatch: pytest.MonkeyPatch) -> None:
    """A password with a space + glob metachar (``p w*d``) replays through the
    REDISCLI_AUTH env channel → status=ok, proving the env remap does NOT
    word-split the secret into bogus args (the failure mode of an unquoted
    ``-a $pwd``), so a legitimate special-char password is never misclassified.

    NOTE: ReplayTarget ignores ``env``, so the value here only needs to be
    present for preflight's secret-presence gate; the password's safety on argv
    is proven by the recorded command string never carrying it. This is a
    command-SAFETY track, NOT a redaction-evidence track — a metrics-only
    inspector emits only integers and never echoes the password to begin with.
    """

    monkeypatch.setenv("HOSTLENS_REDIS_PASSWORD", _SPECIAL_PW)
    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("redisrec", fixture=_FIXTURES / "slowlog_special_char_pw.json")

    result = await _runner().run(manifest, replay, None)

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {"count": 0, "max_micros": 0}
    assert result.findings == []
    # The secret never leaks into the recorded command or its output (it is
    # referenced only via the ${...} env expansion / REDISCLI_AUTH remap).
    recorded = (_FIXTURES / "slowlog_special_char_pw.json").read_text()
    assert _SPECIAL_PW not in recorded


async def test_conn_refused_fails_loud() -> None:
    """Backend unreachable (conn refused) → status=exception, NOT a fabricated
    healthy {"count":0}. The honesty regression lock: the collector exits
    non-zero with empty stdout, so the runner collapses to status=exception
    instead of blessing a dead backend as "healthy" (Authoring Contract rule 8).
    """

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("redisrec", fixture=_FIXTURES / "slowlog_conn_refused.json")

    result = await _runner().run(manifest, replay, {"port": 6390})

    assert replay.misses == []
    assert result.status != "ok"
    assert result.status == "exception"
    assert result.output == {}
    assert result.findings == []


# --------------------------------------------------------------------------- #
# Failure classification (D-3): a missing client binary maps to requires_unmet
# (a graceful skip, NOT an exception). Required by the crosscheck
# `_PROBE_TEST_SOURCES` failure-class meta-guard (this source must literally
# contain `status == "requires_unmet"`).
# --------------------------------------------------------------------------- #


class _NoBinaryTarget:
    """Stub target where every ``command -v X`` probe fails (binary absent)."""

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


async def test_missing_redis_cli_binary_requires_unmet() -> None:
    manifest = load_manifest(_manifest_path())
    target = _NoBinaryTarget()

    result: InspectorResult = await _runner().run(manifest, target, None)  # type: ignore[arg-type]

    # Missing client binary → graceful skip, not a crash and not an exception.
    assert result.status == "requires_unmet"
    assert result.findings == []
    assert any(m.startswith("bin:") for m in result.missing), result.missing


async def test_legacy_redis_password_alone_yields_requires_unmet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BREAKING regression lock: after the migration the inspector requires the
    HOSTLENS_-prefixed secret. Setting ONLY the legacy ``REDIS_PASSWORD`` (and not
    ``HOSTLENS_REDIS_PASSWORD``) must NOT silently authenticate — preflight gates
    on ``HOSTLENS_REDIS_PASSWORD in os.environ``, so the run maps to
    requires_unmet (an honest skip), proving the old env name is no longer honored.
    """

    monkeypatch.delenv("HOSTLENS_REDIS_PASSWORD", raising=False)
    monkeypatch.setenv("REDIS_PASSWORD", "legacy-secret")
    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("redisrec", fixture=_FIXTURES / "slowlog_empty.json")

    result = await _runner().run(manifest, replay, None)

    assert result.status == "requires_unmet"
    assert result.findings == []
    assert result.missing == ["env:HOSTLENS_REDIS_PASSWORD"]
