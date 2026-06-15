"""飞书 Lark channel adapter — interactive card + HMAC-SHA256 signing.

Spec: ``openspec/changes/add-notifier-channels/specs/notifier-lark/spec.md``.
Design D-4 (Jinja2 card template), D-5 (``httpx.AsyncClient``), D-6 (HMAC
timestamp signature with stdlib ``hmac`` / ``hashlib`` / ``base64``).

``render`` produces a Lark interactive card JSON string from the redacted
Report (header colour by aggregate severity); ``send`` POSTs it to the
configured webhook, attaching ``timestamp`` + ``sign`` only when a ``secret``
is configured. webhook URL / secret / sign are masked in logs / ``error``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import TYPE_CHECKING, Final

import httpx
import jinja2

from hostlens.notifiers._filters import (
    conf_label,
    coverage_line,
    dedup_findings,
    fmt_time,
    group_by_target,
    section_severity,
    sev_label,
    sort_sev,
)
from hostlens.notifiers.base import (
    DEFAULT_CHANNEL_HARD_TIMEOUT_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    NotifyPayload,
    NotifyResult,
    redact_secret_text,
    send_with_retry,
)
from hostlens.reporting._redact import redact_report_for_render

if TYPE_CHECKING:
    import structlog

    from hostlens.reporting.models import Report, Severity

__all__ = ["LarkNotifier", "compute_lark_sign"]

# Lark interactive cards cap roughly at 30 KB of card JSON; we measure the
# serialized body in code points and clip elements to stay legal.
_LARK_CARD_LIMIT: Final[int] = 30_000

_TEMPLATE_NAME: Final[str] = "report.card.j2"

# Lark header template colours keyed by aggregate severity.
_HEADER_COLORS: Final[dict[str, str]] = {
    "info": "blue",
    "warning": "orange",
    "critical": "red",
}

_SEV_ICONS: Final[dict[str, str]] = {
    "info": "ℹ️",  # noqa: RUF001 — intentional information-source glyph
    "warning": "⚠️",
    "critical": "🔴",
}


def compute_lark_sign(timestamp: str, secret: str) -> str:
    """Return ``base64(HMAC-SHA256(key=f"{timestamp}\\n{secret}", msg=b""))``.

    This is the飞书自定义机器人 signature: the secret-derived key signs an
    **empty** message; the timestamp is a seconds-level Unix epoch string.
    """

    key = f"{timestamp}\n{secret}".encode()
    digest = hmac.new(key, b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _lark_header_color(severity: object) -> str:
    return _HEADER_COLORS.get(str(severity), "blue")


def _sev_icon(severity: object) -> str:
    return _SEV_ICONS.get(str(severity), "•")


def _build_environment() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.PackageLoader("hostlens.notifiers", "templates/lark"),
        autoescape=False,
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["lark_header_color"] = _lark_header_color
    env.filters["sev_icon"] = _sev_icon
    env.filters["sev_label"] = sev_label
    env.filters["conf_label"] = conf_label
    env.filters["coverage"] = coverage_line
    env.filters["fmt_time"] = fmt_time
    env.filters["dedup"] = dedup_findings
    env.filters["sort_sev"] = sort_sev
    env.filters["group_by_target"] = group_by_target
    env.filters["section_severity"] = section_severity
    return env


class LarkNotifier:
    """飞书 Lark custom-bot adapter (channel type ``"lark"``)."""

    name = "lark"

    def __init__(
        self,
        *,
        instance_name: str,
        config: dict[str, object],
        client: httpx.AsyncClient | None = None,
        logger: structlog.BoundLogger | None = None,
    ) -> None:
        self._instance_name = instance_name
        self._config = config
        self._client = client
        self._logger = logger
        self._env = _build_environment()

    def validate_config(self, cfg: dict[str, object]) -> None:
        """Require non-empty ``webhook_url``; ``secret`` is optional."""

        url = cfg.get("webhook_url")
        if not isinstance(url, str) or not url.strip():
            raise ValueError(
                f"lark channel {self._instance_name!r} requires non-empty 'webhook_url'"
            )
        secret = cfg.get("secret")
        if secret is not None and not isinstance(secret, str):
            raise ValueError(
                f"lark channel {self._instance_name!r} 'secret' must be a string when present"
            )

    def render(self, report: Report, *, severity: Severity) -> NotifyPayload:
        redacted = redact_report_for_render(report)
        rendered = self._env.get_template(_TEMPLATE_NAME).render(report=redacted, severity=severity)
        # The template emits valid JSON by construction (every dynamic value
        # passes through ``tojson``). Round-trip through ``json`` to normalise
        # whitespace and to fail loud if the template ever produced invalid
        # JSON.
        card = json.loads(rendered)
        body, truncated = self._serialize_with_limit(card, severity)
        return NotifyPayload(
            channel=self._instance_name,
            channel_type=self.name,
            body=body,
            truncated=truncated,
        )

    async def send(self, payload: NotifyPayload) -> NotifyResult:
        url = str(self._config["webhook_url"])
        body: dict[str, object] = json.loads(payload.body)

        secret = self._config.get("secret")
        if isinstance(secret, str) and secret.strip():
            timestamp = str(int(time.time()))
            body["timestamp"] = timestamp
            body["sign"] = compute_lark_sign(timestamp, secret)

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=10.0)
        try:

            async def do_request() -> httpx.Response:
                return await client.post(url, json=body)

            return await send_with_retry(
                channel=self._instance_name,
                do_request=do_request,
                interpret=self._interpret,
                logger=self._logger,
                max_attempts=DEFAULT_MAX_ATTEMPTS,
                hard_timeout_seconds=DEFAULT_CHANNEL_HARD_TIMEOUT_SECONDS,
            )
        finally:
            if owns_client:
                await client.aclose()

    def _interpret(self, response: httpx.Response) -> NotifyResult:
        """Map a terminal Lark response to a result.

        Lark returns HTTP 200 + ``{"code": 0, "msg": "success"}`` on accept
        and a non-zero ``code`` on business failure. webhook URL / secret are
        never echoed into ``error``.
        """

        status = response.status_code
        if not 200 <= status < 300:
            return NotifyResult(
                channel=self._instance_name,
                status="failed",
                error=redact_secret_text(f"lark HTTP {status}"),
            )

        try:
            data = response.json()
        except ValueError:
            return NotifyResult(
                channel=self._instance_name,
                status="failed",
                error="lark returned a non-JSON 2xx body",
            )

        if not isinstance(data, dict):
            return NotifyResult(
                channel=self._instance_name,
                status="failed",
                error="lark 2xx body is not a JSON object",
            )

        # Lark accepts with exactly ``{"code": 0, ...}``; a missing / non-zero
        # ``code`` (proxy error page, gateway 200, truncated body) is a real
        # failure, not success — never report an absent success flag as sent.
        code = data.get("code")
        if code == 0:
            return NotifyResult(channel=self._instance_name, status="sent")

        msg = data.get("msg")
        reason = redact_secret_text(str(msg)) if msg else f"lark code={code!r}"
        return NotifyResult(channel=self._instance_name, status="failed", error=reason)

    def _serialize_with_limit(
        self, card: dict[str, object], severity: Severity
    ) -> tuple[str, bool]:
        """Serialize ``card`` to JSON, clipping card elements to keep the body
        under the Lark size limit while remaining parseable.

        Truncation works at the element granularity: trailing card body
        elements are dropped (the card stays valid JSON) until the serialized
        form fits. If even the empty-element skeleton exceeds the limit, the
        skeleton is returned anyway (legality over length, spec §需求 render).
        """

        full = json.dumps(card, ensure_ascii=False)
        if len(full) <= _LARK_CARD_LIMIT:
            return full, False

        inner = card.get("card")
        elements = inner.get("elements") if isinstance(inner, dict) else None
        if not isinstance(inner, dict) or not isinstance(elements, list):
            return full, True

        kept = list(elements)
        while kept:
            kept.pop()
            candidate = dict(card)
            candidate_inner = dict(inner)
            candidate_inner["elements"] = kept
            candidate["card"] = candidate_inner
            serialized = json.dumps(candidate, ensure_ascii=False)
            if len(serialized) <= _LARK_CARD_LIMIT:
                return serialized, True

        # Minimal legal skeleton: empty-element card. May still exceed the
        # limit (pathological), but it is valid JSON and ``render`` must not
        # raise.
        skeleton = dict(card)
        skeleton_inner = dict(inner)
        skeleton_inner["elements"] = []
        skeleton["card"] = skeleton_inner
        return json.dumps(skeleton, ensure_ascii=False), True
