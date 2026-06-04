"""Unit tests for the Telegram channel adapter (task 4.4).

Spec: ``openspec/changes/add-notifier-channels/specs/notifier-telegram/spec.md``.

Covers:

- MarkdownV2 reserved-character escaping (and literal backslash);
- ``validate_config`` fail-loud on missing / empty required fields;
- successful send records the ``message_id`` (``ok == true`` path);
- HTTP 200 + ``ok: false`` records ``failed`` (not ``sent``);
- the bot token never appears in the result ``error`` / ``detail`` or in
  captured log events.

All HTTP goes through ``httpx.MockTransport`` — no real API is contacted.
``asyncio_mode = "auto"`` (pyproject) — async tests need no marker.
"""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest
import structlog

from hostlens.inspectors.result import InspectorResult
from hostlens.notifiers.telegram import TelegramNotifier, _mdv2_escape
from hostlens.reporting.models import Finding, Report

_TOKEN = "123456789:AAH-SecretTelegramTokenValue_abcdEFGH"


def _report(message: str = "ok") -> Report:
    finding = Finding(severity="warning", message=message)
    ir = InspectorResult(
        name="linux.disk",
        version="1.0.0",
        status="ok",
        target_name="web-1",
        duration_seconds=0.1,
        output={},
        findings=[finding],
    )
    return Report.from_inspector_results(
        "web-1",
        [ir],
        started_at=datetime(2026, 1, 1),
        finished_at=datetime(2026, 1, 1),
        intent="nightly check",
    )


def _notifier(handler: httpx.MockTransport, logger: object | None = None) -> TelegramNotifier:
    client = httpx.AsyncClient(transport=handler)
    return TelegramNotifier(
        instance_name="ops-tg",
        config={"bot_token": _TOKEN, "chat_id": "42"},
        client=client,
        logger=logger,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# Rendering / escaping
# --------------------------------------------------------------------------- #


def test_mdv2_escape_reserved_chars_and_backslash() -> None:
    raw = r"a_b*c.d!e\f"
    escaped = _mdv2_escape(raw)
    # Each reserved char is backslash-prefixed; the literal backslash doubled.
    assert escaped == r"a\_b\*c\.d\!e\\f"


def test_render_escapes_reserved_chars_in_finding() -> None:
    notifier = _notifier(httpx.MockTransport(lambda r: httpx.Response(200)))
    payload = notifier.render(_report("disk 95.2% used (warn!)"), severity="warning")
    # The dot / paren / bang in the message must be escaped.
    assert r"95\.2%" in payload.body
    assert r"\(warn\!\)" in payload.body
    assert payload.truncated is False


def test_truncate_does_not_leave_dangling_escape() -> None:
    notifier = _notifier(httpx.MockTransport(lambda r: httpx.Response(200)))
    # A message full of dots (each escaped to ``\.``) far exceeds the 4096
    # limit; the clip must not end on a lone backslash (the lead of an escape
    # whose escaped char was dropped).
    payload = notifier.render(_report("." * 5000), severity="warning")
    assert payload.truncated is True
    core = payload.body[:-1] if payload.body.endswith("…") else payload.body
    trailing_backslashes = len(core) - len(core.rstrip("\\"))
    assert trailing_backslashes % 2 == 0


def test_truncate_keeps_markdownv2_entities_balanced() -> None:
    notifier = _notifier(httpx.MockTransport(lambda r: httpx.Response(200)))
    # A very long intent forces a clip; the clip must not land mid-``*bold*`` /
    # mid-``\`code\``` and leave an unterminated entity (Telegram HTTP 400).
    report = Report.from_inspector_results(
        "web-1",
        [
            InspectorResult(
                name="linux.disk",
                version="1.0.0",
                status="ok",
                target_name="web-1",
                duration_seconds=0.1,
                output={},
                findings=[Finding(severity="warning", message="ok")],
            )
        ],
        started_at=datetime(2026, 1, 1),
        finished_at=datetime(2026, 1, 1),
        intent="I" * 6000,
    )
    payload = notifier.render(report, severity="warning")
    body = payload.body
    assert payload.truncated is True

    utf16_units = sum(2 if ord(c) > 0xFFFF else 1 for c in body)
    assert utf16_units <= 4096

    # Count UNESCAPED structural markers (skip ``\\``-escaped chars).
    backticks = 0
    stars = 0
    i = 0
    while i < len(body):
        if body[i] == "\\":
            i += 2
            continue
        if body[i] == "`":
            backticks += 1
        elif body[i] == "*":
            stars += 1
        i += 1
    assert backticks % 2 == 0
    assert stars % 2 == 0


def _unescaped_count(text: str, marker: str) -> int:
    """Count occurrences of ``marker`` that are not ``\\``-escaped."""

    count = 0
    i = 0
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == marker:
            count += 1
        i += 1
    return count


@pytest.mark.parametrize(
    ("text", "marker", "expected_suffix"),
    [
        ("*bold…", "*", "*…"),
        ("_ital…", "_", "_…"),
        ("`code…", "`", "`…"),
    ],
)
def test_make_markdownv2_legal_balances_each_structural_marker(
    text: str, marker: str, expected_suffix: str
) -> None:
    result = TelegramNotifier._make_markdownv2_legal(text)
    assert result.endswith(expected_suffix)
    # Every structural marker is balanced (even unescaped parity) in the result.
    for m in ("*", "_", "`"):
        assert _unescaped_count(result, m) % 2 == 0


@pytest.mark.parametrize("text", [r"a\*b…", "*ok*…"])
def test_make_markdownv2_legal_leaves_balanced_input_unchanged(text: str) -> None:
    # An escaped star is not a structural marker, and an already-closed span is
    # balanced — neither gets a closer appended.
    result = TelegramNotifier._make_markdownv2_legal(text)
    assert result == text
    for m in ("*", "_", "`"):
        assert _unescaped_count(result, m) % 2 == 0


# --------------------------------------------------------------------------- #
# validate_config
# --------------------------------------------------------------------------- #


def test_validate_config_missing_chat_id_raises() -> None:
    notifier = _notifier(httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(ValueError, match="chat_id"):
        notifier.validate_config({"bot_token": "x"})


def test_validate_config_empty_token_raises() -> None:
    notifier = _notifier(httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(ValueError, match="bot_token"):
        notifier.validate_config({"bot_token": "  ", "chat_id": "42"})


# --------------------------------------------------------------------------- #
# send
# --------------------------------------------------------------------------- #


async def test_send_success_records_message_id_and_posts_markdownv2() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.read().decode("utf-8")
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    notifier = _notifier(httpx.MockTransport(handler))
    payload = notifier.render(_report(), severity="warning")
    result = await notifier.send(payload)

    assert result.status == "sent"
    assert result.detail == {"message_id": "42"}
    assert "/sendMessage" in str(seen["url"])
    assert '"parse_mode":"MarkdownV2"' in str(seen["body"]).replace(" ", "")
    assert '"chat_id":"42"' in str(seen["body"]).replace(" ", "")


async def test_send_http_200_ok_false_records_failed_not_sent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": False, "error_code": 400, "description": "chat not found"}
        )

    notifier = _notifier(httpx.MockTransport(handler))
    payload = notifier.render(_report(), severity="warning")
    result = await notifier.send(payload)

    assert result.status == "failed"
    assert result.error is not None
    assert "chat not found" in result.error


async def test_send_2xx_non_json_records_failed_not_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    notifier = _notifier(httpx.MockTransport(handler))
    payload = notifier.render(_report(), severity="warning")
    result = await notifier.send(payload)

    assert result.status == "failed"
    assert result.error is not None


async def test_send_token_never_leaks_into_error_or_logs() -> None:
    cap = structlog.testing.LogCapture()
    structlog.configure(processors=[cap])
    try:

        def handler(request: httpx.Request) -> httpx.Response:
            # A 5xx so send_with_retry exhausts its budget and logs failure.
            return httpx.Response(503)

        notifier = _notifier(httpx.MockTransport(handler), logger=structlog.get_logger())
        payload = notifier.render(_report(), severity="warning")
        result = await notifier.send(payload)

        assert result.status == "failed"
        assert result.error is not None
        assert _TOKEN not in result.error
        assert "AAH-SecretTelegramTokenValue" not in result.error

        serialized = repr(cap.entries)
        assert _TOKEN not in serialized
        assert "AAH-SecretTelegramTokenValue" not in serialized
    finally:
        structlog.reset_defaults()
