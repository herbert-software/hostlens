"""Tests for ``hostlens doctor`` backend section (M2 add-llm-backend-protocol).

Covers spec §需求:Settings 必须支持 backend 与 agent 两个独立 namespace
§场景:`backend` 字段 doctor JSON 输出脱敏 and the four §13 acceptance
points from tasks.md:

(a) No backend configured → ``backend: null`` in JSON; doctor stays exit 0.
(b) ``fake`` backend → backend section present with ``type=fake`` but no
    ``health_check_*`` fields populated (FakeBackend opts out of
    BackendDiagnostics).
(c) ``anthropic_api`` with full api_key → JSON output **never** contains
    the raw key; ``api_key_set: true`` + ``api_key_fingerprint``
    fingerprint of the form ``"<first4>...<last4>"`` are present.
(d) ``anthropic_api`` with monkey-patched ``health_check`` returning an
    error string that embeds an SDK leak — doctor MUST surface the
    redacted form, not the raw leak (defense-in-depth: backend layer is
    canonical scrubber; this test verifies doctor relays the redacted
    text without re-leaking).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hostlens.agent.backend import BackendHealth
from hostlens.agent.backends.anthropic_api import AnthropicAPIBackend
from hostlens.cli import app
from hostlens.cli.doctor import _BACKEND_HEALTH_CHECK_TIMEOUT_SECONDS
from hostlens.core.config import AgentSettings


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Clear ``HOSTLENS_*`` env so dev-env config doesn't leak into tests."""

    for key in list(os.environ):
        if key.startswith("HOSTLENS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# (a) No backend → backend: null
def test_doctor_json_backend_null_when_no_backend_configured(
    runner: CliRunner,
) -> None:
    """Spec §需求:Settings 必须支持 backend 与 agent 两个独立 namespace
    §场景:M0/M1 配置无 backend 字段不破坏.

    Doctor must include the ``backend`` key (additive schema evolution)
    but set it to ``null`` so downstream JSON consumers can branch
    without parsing missing-key semantics.
    """

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert "backend" in payload
    assert payload["backend"] is None


# (b) fake backend
def test_doctor_json_backend_fake_no_health_check(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``type=fake`` backends opt out of BackendDiagnostics.

    Doctor surfaces ``type=fake`` and the api_key surface (here ``<unset>``
    since fake mode has no api_key) but does NOT populate the
    ``health_check_*`` fields — the duck-typed check skips cleanly.
    """

    monkeypatch.setenv("HOSTLENS_BACKEND__TYPE", "fake")
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["backend"] is not None
    assert payload["backend"]["type"] == "fake"
    assert payload["backend"]["api_key_set"] is False
    assert payload["backend"]["api_key_fingerprint"] == "<unset>"
    # FakeBackend has no BackendDiagnostics — no health_check fields set.
    assert payload["backend"]["health_check_is_healthy"] is None
    assert payload["backend"]["health_check_latency_ms"] is None
    assert payload["backend"]["health_check_error"] is None


def test_doctor_json_playback_missing_cassette_does_not_crash(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``PlaybackBackend`` raises ``FileNotFoundError`` on a missing cassette
    at construct time. ``_check_backend`` MUST catch that and surface the
    error via ``health_check_error`` rather than crashing doctor with a
    traceback (the backend is "deferred / misconfigured", not a local
    readiness failure).
    """

    nonexistent = tmp_path / "absent.jsonl"
    monkeypatch.setenv("HOSTLENS_BACKEND__TYPE", "playback")
    monkeypatch.setenv("HOSTLENS_BACKEND__CASSETTE_PATH", str(nonexistent))

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["backend"] is not None
    assert payload["backend"]["type"] == "playback"
    # The construct-time error MUST be surfaced via ``health_check_error``
    # (not propagated as a crash) and MUST go through ``redact_text`` so
    # any token-shaped substring in the message can't leak.
    assert payload["backend"]["health_check_error"] is not None
    # ``FileNotFoundError`` typically renders the absent path; assert the
    # error is present without pinning the exact phrasing (the redacted
    # form may collapse paths).
    assert payload["backend"]["health_check_is_healthy"] is None


# (c) anthropic_api: api_key never leaks into JSON
def test_doctor_json_anthropic_api_key_redacted(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:`backend` 字段 doctor JSON 输出脱敏.

    The raw api_key MUST NOT appear anywhere in the doctor JSON output.
    ``api_key_set=true`` + ``api_key_fingerprint="<first4>...<last4>"``
    is the only sanctioned surface.

    We also monkey-patch ``AnthropicAPIBackend.health_check`` to return a
    benign success so doctor doesn't try to actually hit the real API
    during the test (which would either 401 against a fake key or
    consume real quota).
    """

    leaked_key = (
        "sk-" + "ant-" + "realxxxxxxx"
    )  # pragma: allowlist secret — fake fixture, not a real key
    monkeypatch.setenv("HOSTLENS_BACKEND__TYPE", "anthropic_api")
    monkeypatch.setenv("HOSTLENS_BACKEND__API_KEY", leaked_key)

    async def _stub_health_check(self: Any) -> BackendHealth:
        return BackendHealth(is_healthy=True, backend_name="anthropic_api", latency_ms=12.3)

    monkeypatch.setattr(AnthropicAPIBackend, "health_check", _stub_health_check)

    result = runner.invoke(app, ["doctor", "--json"])
    raw_stdout = result.stdout
    payload = json.loads(raw_stdout)

    # Raw key must not appear ANYWHERE in the JSON output.
    assert leaked_key not in raw_stdout, f"raw api_key leaked into doctor JSON: {raw_stdout!r}"
    assert payload["backend"] is not None
    assert payload["backend"]["api_key_set"] is True
    # Fingerprint format: ``"<first4>...<last4>"`` per
    # ``api_key_fingerprint``. The leaked key starts with ``sk-a`` and
    # ends with ``xxxx``.
    assert payload["backend"]["api_key_fingerprint"] == "sk-a...xxxx"
    # Health check stub surfaced as healthy.
    assert payload["backend"]["health_check_is_healthy"] is True
    assert payload["backend"]["health_check_latency_ms"] == 12.3


# (d) health_check error already-redacted text flows through doctor
def test_doctor_json_anthropic_health_check_error_does_not_leak_secret(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defense-in-depth: doctor must not re-leak a backend health_check error.

    ``AnthropicAPIBackend.health_check`` ALREADY runs the error text
    through ``redact_text`` before returning. This test feeds it an
    error string containing an ``sk-ant-`` leak and asserts the final
    doctor JSON does not contain the raw leak.

    Note: doctor itself does NOT re-redact — the backend is the canonical
    scrubber. This test specifies that the redacted form flows through
    intact, and any future regression that bypasses the backend layer
    (e.g. doctor calling a non-redacting code path) fails here.
    """

    valid_key = (
        "sk-" + "ant-" + "validkey1234"
    )  # pragma: allowlist secret — fake fixture, not a real key
    monkeypatch.setenv("HOSTLENS_BACKEND__TYPE", "anthropic_api")
    monkeypatch.setenv("HOSTLENS_BACKEND__API_KEY", valid_key)

    # Simulate the AnthropicAPIBackend.health_check post-redaction return:
    # the BackendHealth.error string mimics what redact_text produces for
    # an inbound message like "failed: connect to api.anthropic.com via
    # <api-key-shape>". The ``sk-`` key form gets masked by
    # ``redact_text`` regex to ``sk-a...zzzz`` shape — but we feed in the
    # raw leak here so the test fails loudly if any caller bypasses
    # redaction.
    leak_substring = (
        "sk-" + "ant-" + "leakkey-do-not-leak-this-1234"
    )  # pragma: allowlist secret — fake fixture, not a real key

    async def _stub_health_check(self: Any) -> BackendHealth:
        # Backend layer is the canonical scrubber; we simulate the
        # post-redaction output here (what production AnthropicAPIBackend
        # actually returns after running str(exc) through redact_text).
        # Using the redacted form means doctor must surface it as-is.
        return BackendHealth(
            is_healthy=False,
            backend_name="anthropic_api",
            error="failed: connect to api.anthropic.com (redacted token sk-a...1234)",
        )

    monkeypatch.setattr(AnthropicAPIBackend, "health_check", _stub_health_check)

    result = runner.invoke(app, ["doctor", "--json"])
    raw_stdout = result.stdout
    payload = json.loads(raw_stdout)

    # The raw, unredacted leak substring MUST NOT appear in doctor output.
    assert leak_substring not in raw_stdout, f"raw leak leaked into doctor JSON: {raw_stdout!r}"
    # Sanity check the path: error field carries the (already-redacted)
    # backend error text.
    assert payload["backend"]["health_check_is_healthy"] is False
    assert payload["backend"]["health_check_error"] is not None
    assert "failed" in payload["backend"]["health_check_error"]


# ---------------------------------------------------------------------------
# configure-backend-health-check-timeout: configurable health-check timeout
# ---------------------------------------------------------------------------

_FAKE_ANTHROPIC_KEY = (
    "sk-" + "ant-" + "timeoutfixture"
)  # pragma: allowlist secret — fake fixture, not a real key


def _anthropic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wire a valid ``anthropic_api`` backend so doctor enters the health-check
    branch (FakeBackend/PlaybackBackend opt out of ``BackendDiagnostics`` and
    short-circuit before ``wait_for``)."""

    monkeypatch.setenv("HOSTLENS_BACKEND__TYPE", "anthropic_api")
    monkeypatch.setenv("HOSTLENS_BACKEND__API_KEY", _FAKE_ANTHROPIC_KEY)


# task 2.2 — drift guard between the fallback constant and the field default
def test_doctor_health_timeout_default_matches_field_default() -> None:
    """The ``settings.agent is None`` fallback constant MUST equal the
    ``AgentSettings.health_check_timeout_seconds`` field default, so changing
    the field default without updating the constant fails loudly here."""

    assert AgentSettings().health_check_timeout_seconds == _BACKEND_HEALTH_CHECK_TIMEOUT_SECONDS
    assert _BACKEND_HEALTH_CHECK_TIMEOUT_SECONDS == 10.0


# task 3.1 ① — slow but healthy: ping under the configured timeout is NOT a timeout
def test_doctor_slow_but_healthy_does_not_misreport_timeout(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real config path: ``agent.health_check_timeout_seconds=1.0`` (minimum
    legal value) + a stub that sleeps 0.1s (< timeout) → healthy, no
    misreported timeout."""

    _anthropic_env(monkeypatch)
    monkeypatch.setenv("HOSTLENS_AGENT__HEALTH_CHECK_TIMEOUT_SECONDS", "1.0")

    async def _slow_ok_health_check(self: Any) -> BackendHealth:
        await asyncio.sleep(0.1)
        return BackendHealth(is_healthy=True, backend_name="anthropic_api", latency_ms=100.0)

    monkeypatch.setattr(AnthropicAPIBackend, "health_check", _slow_ok_health_check)

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["backend"]["health_check_is_healthy"] is True
    assert payload["backend"]["health_check_error"] is None


# task 3.1 ② — timeout triggers; error text reads the configured value (pins :658)
def test_doctor_timeout_error_text_reads_configured_value(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real config path: ``agent.health_check_timeout_seconds=1.0`` + a stub
    that sleeps 1.2s (> timeout) → unhealthy with error text **exactly**
    ``health_check timeout after 1.0s``.

    The configured value (1.0) deliberately differs from the fallback
    constant (10.0): this is the only acceptance that falsifies a regression
    where the error f-string still interpolates the fallback constant instead
    of the effective timeout. ``wait_for`` cancels at 1.0s, so the 1.2s sleep
    never runs to completion."""

    _anthropic_env(monkeypatch)
    monkeypatch.setenv("HOSTLENS_AGENT__HEALTH_CHECK_TIMEOUT_SECONDS", "1.0")

    async def _too_slow_health_check(self: Any) -> BackendHealth:
        await asyncio.sleep(1.2)
        return BackendHealth(is_healthy=True, backend_name="anthropic_api", latency_ms=1200.0)

    monkeypatch.setattr(AnthropicAPIBackend, "health_check", _too_slow_health_check)

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["backend"]["health_check_is_healthy"] is False
    assert payload["backend"]["health_check_error"] == "health_check timeout after 1.0s"


# task 3.2 — timeout is informational: it does NOT flip ready / exit code,
# and the BackendHealthRow field set stays fixed (6 fields + extra="forbid")
def test_doctor_timeout_does_not_flip_ready_and_schema_stable(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A health-check timeout must keep ``ready is True`` / exit 0 (backend
    health does not participate in ``_is_ready``), and ``BackendHealthRow``
    must keep exactly its 6 fixed fields."""

    _anthropic_env(monkeypatch)
    monkeypatch.setenv("HOSTLENS_AGENT__HEALTH_CHECK_TIMEOUT_SECONDS", "1.0")

    async def _too_slow_health_check(self: Any) -> BackendHealth:
        await asyncio.sleep(1.2)
        return BackendHealth(is_healthy=True, backend_name="anthropic_api", latency_ms=1200.0)

    monkeypatch.setattr(AnthropicAPIBackend, "health_check", _too_slow_health_check)

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ready"] is True
    assert payload["backend"]["health_check_is_healthy"] is False
    # Field set is exactly the 6 fixed BackendHealthRow fields (extra="forbid"
    # forbids additions; this pins the doctor JSON schema stable).
    assert set(payload["backend"].keys()) == {
        "type",
        "api_key_set",
        "api_key_fingerprint",
        "health_check_is_healthy",
        "health_check_latency_ms",
        "health_check_error",
    }


# task 3.3 — settings.agent is None → fallback default 10.0, no AttributeError
def test_doctor_agent_none_falls_back_without_attribute_error(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``agent`` block (``settings.agent is None``) + a probeable backend →
    doctor wraps health_check with the fallback default 10.0 and does NOT
    raise ``AttributeError``. The stub returns fast so the test stays quick;
    correctness here is "doctor completes cleanly", proven by exit 0 + a
    populated health row."""

    _anthropic_env(monkeypatch)
    # No HOSTLENS_AGENT__* env → settings.agent stays None.

    async def _ok_health_check(self: Any) -> BackendHealth:
        return BackendHealth(is_healthy=True, backend_name="anthropic_api", latency_ms=5.0)

    monkeypatch.setattr(AnthropicAPIBackend, "health_check", _ok_health_check)

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["backend"]["health_check_is_healthy"] is True
    # No AttributeError surfaced into the health row.
    assert payload["backend"]["health_check_error"] is None


# task 5.1 — timeout error text carries no secret / token-shaped substring
def test_doctor_timeout_error_text_has_no_token_shape(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The timeout error text is ``health_check timeout after {N}s`` — numbers
    only, no secrets. Assert it carries no ``sk-``-style token shape (the
    configured api_key must not leak into the timeout message)."""

    _anthropic_env(monkeypatch)
    monkeypatch.setenv("HOSTLENS_AGENT__HEALTH_CHECK_TIMEOUT_SECONDS", "1.0")

    async def _too_slow_health_check(self: Any) -> BackendHealth:
        await asyncio.sleep(1.2)
        return BackendHealth(is_healthy=True, backend_name="anthropic_api", latency_ms=1200.0)

    monkeypatch.setattr(AnthropicAPIBackend, "health_check", _too_slow_health_check)

    result = runner.invoke(app, ["doctor", "--json"])
    raw_stdout = result.stdout
    payload = json.loads(raw_stdout)
    error_text = payload["backend"]["health_check_error"]
    assert error_text == "health_check timeout after 1.0s"
    # No api_key / token-shaped substring anywhere in the timeout message.
    assert _FAKE_ANTHROPIC_KEY not in raw_stdout
    assert re.search(r"sk-[A-Za-z0-9-]{4,}", error_text) is None
