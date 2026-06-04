"""Unit tests for the Notifier core abstraction (task 2.5).

Spec: ``openspec/changes/add-notifier-channels/specs/notifier-protocol/spec.md``.

Covers, per the task list:

- ChannelTypeRegistry resolution / unregistered ``get`` raises;
- ``import hostlens.notifiers`` produces no registration side effect;
- ``NotifyResult`` Literal status validation;
- over-limit truncation yields a legal artifact with ``truncated=True``;
- ``send_with_retry`` returns ``failed`` (never raises) on exhausted retries;
- ``error`` carries no plaintext secret (URL-embedded bot token redacted);
- 4xx≠429 is non-retryable (single attempt).

The registry tests use a **local fake** ``Notifier`` rather than calling
``register_default_notifiers`` — the built-in adapter modules are created by
group C, so triggering their real import here would be a cross-group
dependency. The deferred-import body of ``register_default_notifiers`` is
what satisfies the "no import-time side effect" contract; the import-side-
effect test asserts that contract directly.

``asyncio_mode = "auto"`` (pyproject) — async tests need no marker.
"""

from __future__ import annotations

import importlib

import httpx
import pytest
from pydantic import ValidationError

from hostlens.notifiers.base import (
    ChannelTypeRegistry,
    NotifyPayload,
    NotifyResult,
    redact_secret_text,
    send_with_retry,
    truncate_to_limit,
)


class _FakeNotifier:
    """Minimal structural ``Notifier`` for registry tests (no real I/O)."""

    name = "fake"

    def validate_config(self, cfg: dict[str, object]) -> None:  # pragma: no cover - unused
        return None

    def render(self, report: object, *, severity: object) -> NotifyPayload:  # pragma: no cover
        raise NotImplementedError

    async def send(self, payload: NotifyPayload) -> NotifyResult:  # pragma: no cover
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# ChannelTypeRegistry
# --------------------------------------------------------------------------- #


def test_registry_resolves_registered_type() -> None:
    registry = ChannelTypeRegistry()
    registry.register("fake", _FakeNotifier)
    assert registry.get("fake") is _FakeNotifier
    assert registry.types() == ["fake"]


def test_registry_unknown_type_raises_not_none() -> None:
    registry = ChannelTypeRegistry()
    with pytest.raises(KeyError) as exc:
        registry.get("nope")
    assert "nope" in str(exc.value)


def test_registry_rejects_duplicate_registration() -> None:
    registry = ChannelTypeRegistry()
    registry.register("fake", _FakeNotifier)
    with pytest.raises(KeyError):
        registry.register("fake", _FakeNotifier)


def test_importing_notifiers_has_no_registration_side_effect() -> None:
    """Importing the package / base module must not pre-populate a registry."""

    pkg = importlib.import_module("hostlens.notifiers")
    base = importlib.import_module("hostlens.notifiers.base")
    # No module-level singleton registry exists; constructing a fresh one is
    # the only way to obtain channel types, and it starts empty.
    assert not hasattr(pkg, "REGISTRY")
    assert not hasattr(base, "REGISTRY")
    assert ChannelTypeRegistry().types() == []


# --------------------------------------------------------------------------- #
# NotifyResult / NotifyPayload models
# --------------------------------------------------------------------------- #


def test_notify_result_status_literal_enforced() -> None:
    assert NotifyResult(channel="x", status="skipped").status == "skipped"
    failed = NotifyResult(channel="x", status="failed", error="timeout")
    assert failed.status == "failed"
    with pytest.raises(ValidationError):
        NotifyResult(channel="x", status="ok")  # type: ignore[arg-type]


def test_notify_result_detail_is_str_mapping() -> None:
    result = NotifyResult(channel="x", status="sent", detail={"message_id": str(42)})
    assert result.detail == {"message_id": "42"}


def test_notify_payload_defaults() -> None:
    payload = NotifyPayload(channel="ops", channel_type="telegram", body="hi")
    assert payload.truncated is False


# --------------------------------------------------------------------------- #
# Truncation
# --------------------------------------------------------------------------- #


def test_truncate_under_limit_is_untouched() -> None:
    text = "short"
    out, truncated = truncate_to_limit(text, 100)
    assert out == text
    assert truncated is False


def test_truncate_over_limit_flags_and_keeps_char_boundary() -> None:
    text = "x" * 50
    out, truncated = truncate_to_limit(text, 10)
    assert truncated is True
    assert len(out) <= 10
    assert out.endswith("…")


def test_truncate_utf16_counts_astral_as_two_units() -> None:
    # Each rocket emoji is 2 UTF-16 code units. Limit 6 (units) with a 1-unit
    # ellipsis leaves budget 5 → only 2 emoji (4 units) fit.
    text = "🚀" * 5
    out, truncated = truncate_to_limit(text, 6, count_unit="utf16")
    assert truncated is True
    # Must not split a surrogate pair: every kept emoji is intact.
    assert out.count("🚀") == 2
    assert out.endswith("…")


def test_truncate_pathological_tiny_limit_returns_legal_minimum() -> None:
    # Limit smaller than the ellipsis itself: do not raise, return the
    # ellipsis and flag truncated (legality over length).
    out, truncated = truncate_to_limit("abcdef", 0)
    assert truncated is True
    assert out == "…"


# --------------------------------------------------------------------------- #
# redact_secret_text
# --------------------------------------------------------------------------- #


def test_redact_strips_telegram_bot_token_in_url() -> None:
    token = "123456789:AAH-fakeTokenValue_abcdEFGH"
    raw = f"timeout posting https://api.telegram.org/bot{token}/sendMessage"
    out = redact_secret_text(raw)
    assert token not in out
    assert "AAH-fakeTokenValue" not in out
    assert "***" in out


def test_redact_strips_webhook_query_secret() -> None:
    raw = "failed GET https://open.feishu.cn/hook/abc123def456?sign=SECRETSIGN&x=1"
    out = redact_secret_text(raw)
    assert "abc123def456" not in out
    assert "SECRETSIGN" not in out


# --------------------------------------------------------------------------- #
# send_with_retry
# --------------------------------------------------------------------------- #


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://example.test/send")


def _interpret_ok(response: httpx.Response) -> NotifyResult:
    return NotifyResult(channel="c", status="sent", detail={"message_id": "1"})


def _interpret_failed(response: httpx.Response) -> NotifyResult:
    return NotifyResult(channel="c", status="failed", error=f"HTTP {response.status_code}")


async def test_send_success_records_attempts() -> None:
    async def do_request() -> httpx.Response:
        return httpx.Response(200, request=_request())

    result = await send_with_retry(channel="c", do_request=do_request, interpret=_interpret_ok)
    assert result.status == "sent"
    assert result.attempts == 1
    assert result.detail == {"message_id": "1"}


async def test_send_5xx_exhausts_retries_returns_failed_not_raised() -> None:
    calls = 0

    async def do_request() -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, request=_request())

    result = await send_with_retry(
        channel="c",
        do_request=do_request,
        interpret=_interpret_failed,
        max_attempts=3,
        hard_timeout_seconds=5.0,
    )
    assert result.status == "failed"
    assert result.attempts == 3
    assert calls == 3
    assert result.error is not None


async def test_send_4xx_non_429_is_not_retried() -> None:
    calls = 0

    async def do_request() -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, request=_request())

    result = await send_with_retry(
        channel="c",
        do_request=do_request,
        interpret=_interpret_failed,
        max_attempts=3,
    )
    assert result.status == "failed"
    assert result.attempts == 1
    assert calls == 1


async def test_send_transport_error_returns_failed_with_redacted_error() -> None:
    token = "999:AAFsecretTokenZZZ"

    async def do_request() -> httpx.Response:
        raise httpx.ConnectError(
            f"connection refused to https://api.telegram.org/bot{token}/sendMessage"
        )

    result = await send_with_retry(
        channel="c",
        do_request=do_request,
        interpret=_interpret_ok,
        max_attempts=1,
        hard_timeout_seconds=5.0,
    )
    assert result.status == "failed"
    assert result.error is not None
    assert token not in result.error
    assert "AAFsecretTokenZZZ" not in result.error


async def test_send_429_honors_retry_after_then_succeeds() -> None:
    calls = 0

    async def do_request() -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, request=_request())
        return httpx.Response(200, request=_request())

    result = await send_with_retry(
        channel="c",
        do_request=do_request,
        interpret=_interpret_ok,
        max_attempts=3,
        hard_timeout_seconds=5.0,
    )
    assert result.status == "sent"
    assert result.attempts == 2


async def test_send_invalid_url_returns_failed_not_raised() -> None:
    # ``httpx.InvalidURL`` subclasses ``Exception`` directly (not
    # ``TransportError``); ``send`` must never raise, so terminate as failed
    # without ever reaching ``interpret``.
    def _interpret_must_not_run(response: httpx.Response) -> NotifyResult:  # pragma: no cover
        raise AssertionError("interpret must not be called on a request fault")

    async def do_request() -> httpx.Response:
        raise httpx.InvalidURL("bad")

    result = await send_with_retry(
        channel="c",
        do_request=do_request,
        interpret=_interpret_must_not_run,
        max_attempts=3,
        hard_timeout_seconds=5.0,
    )
    assert result.status == "failed"
    assert result.attempts == 1
    assert result.error is not None


async def test_send_interpret_failure_returns_failed_not_raised() -> None:
    async def do_request() -> httpx.Response:
        return httpx.Response(200, json={}, request=_request())

    def _interpret_boom(response: httpx.Response) -> NotifyResult:
        raise RuntimeError("boom")

    result = await send_with_retry(
        channel="c",
        do_request=do_request,
        interpret=_interpret_boom,
        max_attempts=3,
        hard_timeout_seconds=5.0,
    )
    assert result.status == "failed"
    assert result.error is not None
    assert "interpret failed" in result.error
