"""Telegram channel adapter — MarkdownV2 rendering + Bot API ``sendMessage``.

Spec: ``openspec/changes/add-notifier-channels/specs/notifier-telegram/spec.md``.
Design D-4 (Jinja2 template + ``mdv2_escape`` / ``sev_icon`` filter), D-5
(``httpx.AsyncClient``).

The adapter satisfies the ``Notifier`` Protocol (``notifiers/base.py``):
``render`` produces a MarkdownV2 body from the redacted Report; ``send``
POSTs it to ``https://api.telegram.org/bot<token>/sendMessage`` through the
shared bounded-retry helper and never lets a send failure bubble. The bot
token is masked everywhere it could surface (logs / ``error`` / ``detail``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import httpx
import jinja2

from hostlens.notifiers._filters import (
    conf_label,
    coverage_line,
    dedup_findings,
    failed_checks,
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
    truncate_to_limit,
)
from hostlens.reporting._redact import redact_report_for_render

if TYPE_CHECKING:
    import structlog

    from hostlens.reporting.models import Report, Severity

__all__ = ["TelegramNotifier"]

# Telegram caps a text message at 4096 UTF-16 code units (not Python code
# points) — an emoji-heavy body would slip past a naive ``len()`` check.
_TELEGRAM_TEXT_LIMIT: Final[int] = 4096

_TEMPLATE_NAME: Final[str] = "report.md.j2"

# MarkdownV2 reserved characters that must be backslash-escaped inside text
# (https://core.telegram.org/bots/api#markdownv2-style). The literal
# backslash is escaped first so a content ``\`` does not eat the next char.
_MDV2_RESERVED: Final[str] = r"_*[]()~`>#+-=|{}.!"
_MDV2_TRANSLATION: Final[dict[int, str]] = {
    ord("\\"): "\\\\",
    **{ord(ch): "\\" + ch for ch in _MDV2_RESERVED},
}

_SEV_ICONS: Final[dict[str, str]] = {
    "info": "ℹ️",  # noqa: RUF001 — intentional information-source glyph
    "warning": "⚠️",
    "critical": "🔴",
}


def _mdv2_escape(value: object) -> str:
    """Escape MarkdownV2 reserved characters (and literal ``\\``) in ``value``.

    Coerces ``value`` to ``str`` first so non-string fields render safely.
    The literal backslash is translated to ``\\\\`` in the same pass (via the
    translation table) so a content backslash cannot swallow the character
    that follows it.
    """

    return str(value).translate(_MDV2_TRANSLATION)


def _sev_icon(severity: object) -> str:
    """Map a severity string to a leading icon (unknown → a neutral dot)."""

    return _SEV_ICONS.get(str(severity), "•")


def _build_environment() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.PackageLoader("hostlens.notifiers", "templates/telegram"),
        autoescape=False,
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["mdv2_escape"] = _mdv2_escape
    env.filters["sev_icon"] = _sev_icon
    env.filters["sev_label"] = sev_label
    env.filters["conf_label"] = conf_label
    env.filters["coverage"] = coverage_line
    env.filters["failed_checks"] = failed_checks
    env.filters["fmt_time"] = fmt_time
    env.filters["dedup"] = dedup_findings
    env.filters["sort_sev"] = sort_sev
    env.filters["group_by_target"] = group_by_target
    env.filters["section_severity"] = section_severity
    return env


class TelegramNotifier:
    """Telegram Bot API adapter (channel type ``"telegram"``)."""

    name = "telegram"

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
        """Require non-empty ``bot_token`` and ``chat_id`` (empty == missing)."""

        for field in ("bot_token", "chat_id"):
            value = cfg.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"telegram channel {self._instance_name!r} requires non-empty {field!r}"
                )

    def render(self, report: Report, *, severity: Severity) -> NotifyPayload:
        redacted = redact_report_for_render(report)
        body = self._env.get_template(_TEMPLATE_NAME).render(report=redacted, severity=severity)
        clipped, truncated = self._truncate_markdownv2(body)
        return NotifyPayload(
            channel=self._instance_name,
            channel_type=self.name,
            body=clipped,
            truncated=truncated,
        )

    async def send(self, payload: NotifyPayload) -> NotifyResult:
        token = str(self._config["bot_token"])
        chat_id = str(self._config["chat_id"])
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body = {"chat_id": chat_id, "text": payload.body, "parse_mode": "MarkdownV2"}

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
        """Map a terminal (2xx / 4xx≠429) Telegram response to a result.

        Telegram signals business failure with HTTP 200 + ``{"ok": false,
        ...}`` (e.g. chat not found), so success requires both 2xx **and**
        ``ok == true`` with a ``result.message_id``. A 2xx body that is not
        JSON or lacks ``message_id`` is treated as an anomalous failure
        (never crashes). The bot token never appears in ``error`` / ``detail``.
        """

        status = response.status_code
        if not 200 <= status < 300:
            return NotifyResult(
                channel=self._instance_name,
                status="failed",
                error=redact_secret_text(f"telegram HTTP {status}"),
            )

        try:
            data = response.json()
        except ValueError:
            return NotifyResult(
                channel=self._instance_name,
                status="failed",
                error="telegram returned a non-JSON 2xx body",
            )

        if not isinstance(data, dict) or data.get("ok") is not True:
            description = data.get("description") if isinstance(data, dict) else None
            reason = redact_secret_text(str(description)) if description else "telegram ok=false"
            return NotifyResult(channel=self._instance_name, status="failed", error=reason)

        result = data.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if message_id is None:
            return NotifyResult(
                channel=self._instance_name,
                status="failed",
                error="telegram 2xx body missing result.message_id",
            )

        return NotifyResult(
            channel=self._instance_name,
            status="sent",
            detail={"message_id": str(message_id)},
        )

    def _truncate_markdownv2(self, body: str) -> tuple[str, bool]:
        """Truncate ``body`` to the Telegram UTF-16 limit without leaving a
        dangling backslash escape.

        ``truncate_to_limit`` guarantees character-boundary safety; on top of
        that, if the clip lands right after a lone trailing backslash (the
        lead of an escape sequence whose escaped char was dropped), we trim
        that backslash so the body stays MarkdownV2-legal.
        """

        # Reserve one unit per structural marker kind so re-balancing
        # unterminated spans (``_make_markdownv2_legal`` may append up to one
        # closer per marker in ``_STRUCTURAL_MARKERS``) can never push the final
        # body past the Telegram limit. Derived from the marker set so the two
        # constants cannot silently drift apart.
        clipped, truncated = truncate_to_limit(
            body,
            _TELEGRAM_TEXT_LIMIT - len(self._STRUCTURAL_MARKERS),
            count_unit="utf16",
        )
        if not truncated:
            return clipped, False
        clipped = self._make_markdownv2_legal(clipped)
        return clipped, True

    # Structural MarkdownV2 markers the template emits unescaped: bold ``*``,
    # italic ``_`` (the ``_无 findings_`` branch), and code ``` `` ```. Content
    # occurrences of these are backslash-escaped by ``mdv2_escape`` and so are
    # skipped when counting parity below.
    _STRUCTURAL_MARKERS: Final[tuple[str, ...]] = ("*", "_", "`")

    @staticmethod
    def _make_markdownv2_legal(text: str) -> str:
        """Make a clipped body MarkdownV2-legal.

        ``truncate_to_limit`` is markup-blind, so a clip can land (a) right
        after a lone escaping backslash whose escaped char was dropped, or (b)
        inside a ``*bold*`` / ``_italic_`` / ``` `code` ``` span the template
        opened — leaving an unterminated MarkdownV2 entity that Telegram rejects
        with HTTP 400. Content markers are backslash-escaped by ``mdv2_escape``
        and skipped, so only the template's *structural* markers are counted.
        The template's spans never nest (each opens and closes before the next
        opens), so at most one marker is unbalanced at a clip point; we append
        one closer per odd-parity marker to restore legality.
        """

        ellipsis = "…"
        has_ellipsis = text.endswith(ellipsis)
        core = text[: -len(ellipsis)] if has_ellipsis else text

        # (a) Drop a dangling backslash escape lead (odd trailing run).
        trailing = len(core) - len(core.rstrip("\\"))
        if trailing % 2 == 1:
            core = core[:-1]

        # (b) Close any unterminated structural span. Skip escaped chars so a
        # content ``\\*`` / ``\\_`` / ``\\``` never counts as a structural marker.
        counts = dict.fromkeys(TelegramNotifier._STRUCTURAL_MARKERS, 0)
        i = 0
        while i < len(core):
            ch = core[i]
            if ch == "\\":
                i += 2
                continue
            if ch in counts:
                counts[ch] += 1
            i += 1
        for marker, count in counts.items():
            if count % 2 == 1:
                core += marker

        return core + (ellipsis if has_ellipsis else "")
