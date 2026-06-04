"""Unit tests for the飞书 Lark channel adapter (task 5.4).

Spec: ``openspec/changes/add-notifier-channels/specs/notifier-lark/spec.md``.

Covers:

- rendered body is a parseable interactive card (``msg_type`` + ``card``);
- header colour varies by aggregate severity;
- HMAC-SHA256 signature matches a fixed vector byte-for-byte;
- a configured ``secret`` attaches ``timestamp`` + ``sign``;
- no ``secret`` → POST body carries no ``sign`` field;
- webhook URL / secret never leak into the result ``error``.

All HTTP goes through ``httpx.MockTransport`` — no real API is contacted.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime

import httpx
import pytest

from hostlens.inspectors.result import InspectorResult
from hostlens.notifiers.lark import LarkNotifier, compute_lark_sign
from hostlens.reporting.models import Finding, Report

_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/abc123secrethook"
_SECRET = "fixedLarkSignSecret"


def _report(severity: str = "critical") -> Report:
    finding = Finding(severity=severity, message="disk full")  # type: ignore[arg-type]
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


def _notifier(handler: httpx.MockTransport, *, secret: str | None = None) -> LarkNotifier:
    config: dict[str, object] = {"webhook_url": _WEBHOOK}
    if secret is not None:
        config["secret"] = secret
    return LarkNotifier(
        instance_name="ops-lk",
        config=config,
        client=httpx.AsyncClient(transport=handler),
    )


# --------------------------------------------------------------------------- #
# Signing vector
# --------------------------------------------------------------------------- #


def test_compute_lark_sign_matches_reference_vector() -> None:
    timestamp = "1700000000"
    key = f"{timestamp}\n{_SECRET}".encode()
    expected = base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode("utf-8")
    assert compute_lark_sign(timestamp, _SECRET) == expected


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #


def test_render_produces_parseable_interactive_card() -> None:
    notifier = _notifier(httpx.MockTransport(lambda r: httpx.Response(200)))
    payload = notifier.render(_report(), severity="critical")
    card = json.loads(payload.body)
    assert card["msg_type"] == "interactive"
    assert "card" in card
    assert payload.truncated is False


def test_render_over_limit_truncates_to_parseable_card(monkeypatch: pytest.MonkeyPatch) -> None:
    import hostlens.notifiers.lark as lark_mod

    # Shrink the card limit so a multi-finding report overflows; the clipped
    # body must drop elements yet remain valid JSON with truncated=True.
    monkeypatch.setattr(lark_mod, "_LARK_CARD_LIMIT", 400)
    finding = Finding(severity="critical", message="x" * 200)
    ir = InspectorResult(
        name="linux.disk",
        version="1.0.0",
        status="ok",
        target_name="web-1",
        duration_seconds=0.1,
        output={},
        findings=[finding, finding, finding],
    )
    report = Report.from_inspector_results(
        "web-1", [ir], started_at=datetime(2026, 1, 1), finished_at=datetime(2026, 1, 1)
    )
    notifier = _notifier(httpx.MockTransport(lambda r: httpx.Response(200)))
    payload = notifier.render(report, severity="critical")
    assert payload.truncated is True
    card = json.loads(payload.body)  # must remain parseable
    assert card["msg_type"] == "interactive"


def test_render_header_color_by_severity() -> None:
    notifier = _notifier(httpx.MockTransport(lambda r: httpx.Response(200)))
    crit = json.loads(notifier.render(_report("critical"), severity="critical").body)
    warn = json.loads(notifier.render(_report("warning"), severity="warning").body)
    assert crit["card"]["header"]["template"] == "red"
    assert warn["card"]["header"]["template"] == "orange"


# --------------------------------------------------------------------------- #
# validate_config
# --------------------------------------------------------------------------- #


def test_validate_config_requires_webhook_url() -> None:
    notifier = _notifier(httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(ValueError, match="webhook_url"):
        notifier.validate_config({})


def test_validate_config_secret_optional() -> None:
    notifier = _notifier(httpx.MockTransport(lambda r: httpx.Response(200)))
    notifier.validate_config({"webhook_url": _WEBHOOK})  # no raise


# --------------------------------------------------------------------------- #
# send
# --------------------------------------------------------------------------- #


async def test_send_with_secret_attaches_correct_sign() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.read().decode("utf-8"))
        return httpx.Response(200, json={"code": 0, "msg": "success"})

    notifier = _notifier(httpx.MockTransport(handler), secret=_SECRET)
    payload = notifier.render(_report(), severity="critical")
    result = await notifier.send(payload)

    assert result.status == "sent"
    body = seen["body"]
    assert isinstance(body, dict)
    assert "timestamp" in body
    assert "sign" in body
    # Recompute the sign from the timestamp the adapter actually sent.
    expected = compute_lark_sign(str(body["timestamp"]), _SECRET)
    assert body["sign"] == expected


async def test_send_without_secret_omits_sign() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.read().decode("utf-8"))
        return httpx.Response(200, json={"code": 0})

    notifier = _notifier(httpx.MockTransport(handler))  # no secret
    payload = notifier.render(_report(), severity="critical")
    result = await notifier.send(payload)

    assert result.status == "sent"
    body = seen["body"]
    assert isinstance(body, dict)
    assert "sign" not in body
    assert "timestamp" not in body


async def test_send_business_failure_records_failed_without_secret_leak() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 19021, "msg": "sign match fail"})

    notifier = _notifier(httpx.MockTransport(handler), secret=_SECRET)
    payload = notifier.render(_report(), severity="critical")
    result = await notifier.send(payload)

    assert result.status == "failed"
    assert result.error is not None
    assert _SECRET not in result.error
    assert "abc123secrethook" not in result.error


async def test_send_2xx_without_code_records_failed_not_sent() -> None:
    # A 200 body with no ``code`` (proxy / gateway page) must NOT be reported
    # as sent — only an explicit ``code == 0`` is success.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    notifier = _notifier(httpx.MockTransport(handler))
    payload = notifier.render(_report(), severity="critical")
    result = await notifier.send(payload)

    assert result.status == "failed"


async def test_send_2xx_json_list_records_failed_not_sent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    notifier = _notifier(httpx.MockTransport(handler))
    payload = notifier.render(_report(), severity="critical")
    result = await notifier.send(payload)

    assert result.status == "failed"
    assert result.error is not None
