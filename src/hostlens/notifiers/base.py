"""Notifier core abstraction — Protocol, registry, payload/result models,
and shared send/redaction/truncation helpers.

Spec: ``openspec/changes/add-notifier-channels/specs/notifier-protocol/spec.md``
(§需求:Notifier 必须是 Protocol 抽象 + 显式装配的通道类型 registry /
§需求:`NotifyPayload` / `NotifyResult` 必须是 Pydantic v2 强类型模型 /
§需求:send 必须有界重试且失败隔离). Design D-1 / D-2 / D-5 / D-7.

This module is **host-agnostic core** (CLAUDE.md §4.4): adding a channel =
adding one adapter file that satisfies the ``Notifier`` Protocol and gets
registered by ``register_default_notifiers``. The registry never mutates a
module-level singleton at import time (CLAUDE.md §4.10 rule 3) — callers
construct a ``ChannelTypeRegistry`` instance and pass it through the
explicit assembly function.
"""

from __future__ import annotations

import asyncio
import math
import random
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

import httpx
from pydantic import BaseModel, ConfigDict, Field

from hostlens.core.redact import redact_text

if TYPE_CHECKING:
    import structlog

    from hostlens.reporting.models import Report, Severity

__all__ = [
    "DEFAULT_CHANNEL_HARD_TIMEOUT_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "ChannelTypeRegistry",
    "Notifier",
    "NotifyPayload",
    "NotifyResult",
    "redact_secret_text",
    "register_default_notifiers",
    "send_with_retry",
    "truncate_to_limit",
]


# --------------------------------------------------------------------------- #
# Operational limits (proposal §Operational Limits)
# --------------------------------------------------------------------------- #

DEFAULT_MAX_ATTEMPTS: int = 3
"""Bounded retry budget for a single channel send (proposal §Operational
Limits). The first try plus up to ``DEFAULT_MAX_ATTEMPTS - 1`` retries."""

DEFAULT_CHANNEL_HARD_TIMEOUT_SECONDS: float = 60.0
"""Per-channel hard ceiling spanning all retries (design D-7 / proposal
§Operational Limits). ``Retry-After`` waits are clamped to fit under what
remains of this budget; once exhausted the send returns ``failed`` rather
than blocking the Run."""

_DEFAULT_BACKOFF_BASE_SECONDS: float = 1.0
"""Exponential backoff base: attempt *n* waits ``base * 2**(n-1)`` plus
jitter (1s / 2s / 4s for the default 3-attempt budget)."""

_BACKOFF_JITTER_SECONDS: float = 0.25
"""Upper bound of the uniform jitter added to each backoff wait so
concurrent channels do not retry in lockstep."""


# --------------------------------------------------------------------------- #
# Pydantic models (design D-2)
# --------------------------------------------------------------------------- #


class NotifyPayload(BaseModel):
    """Channel-native rendered artifact produced by ``Notifier.render``.

    ``body`` is the already-rendered, channel-format string (Telegram =
    MarkdownV2 text; Lark = card JSON string). ``truncated`` is set by the
    adapter when ``render`` had to clip the body to the channel length
    limit — the send still proceeds with the clipped (but format-legal)
    body; ``render`` never raises on overflow.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: str
    channel_type: str
    body: str
    truncated: bool = False


class NotifyResult(BaseModel):
    """Per-channel send outcome, persisted into ``Run.notify_results``.

    ``status`` semantics:

    - ``sent``: the platform accepted the message (HTTP 2xx and the body
      business-success flag, e.g. Telegram ``ok == true``).
    - ``skipped``: ``only_if`` evaluated false — a normal routing skip, not
      an error.
    - ``failed``: any exception in routing (``only_if`` runtime eval) /
      render / send. The send helpers never let those bubble; they return a
      ``failed`` result instead.

    ``error`` is a **persistence path** (it lands in ``runs.db`` via
    ``Run.notify_results``), so callers MUST run any exception-derived text
    through ``redact_secret_text`` before assigning it here — an upstream
    URL like ``…/bot<token>/…`` embedded in an exception ``repr`` would
    otherwise leak the bot token byte-for-byte into the database.

    ``detail`` values are ``str``-typed by contract: platform identifiers
    such as an ``int`` Telegram ``message_id`` MUST be ``str()``-ed before
    insertion so the ``dict[str, str]`` shape holds.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: str
    status: Literal["sent", "skipped", "failed"]
    error: str | None = None
    attempts: int = 0
    detail: dict[str, str] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Notifier Protocol (design D-1)
# --------------------------------------------------------------------------- #


@runtime_checkable
class Notifier(Protocol):
    """Structural interface every channel adapter satisfies.

    ``name`` is the channel **type** key (e.g. ``"telegram"``), not the
    instance name. A concrete adapter is constructed per ``notifiers.yaml``
    ``channels.<name>`` entry; the instance name travels on the
    ``NotifyPayload`` / ``NotifyResult``.

    - ``validate_config(cfg)``: startup-time check; missing / mistyped /
      empty required fields MUST raise (fail-loud at assembly, not at send).
    - ``render(report, *, severity)``: load a Jinja2 template from the
      channel's template dir and produce a channel-native ``NotifyPayload``;
      overflow is clipped (``truncated=True``), never raised.
    - ``send(payload)``: deliver with bounded retry / rate-limit / signing;
      return a ``NotifyResult`` and never bubble a send failure.
    """

    name: str

    def validate_config(self, cfg: dict[str, object]) -> None: ...

    def render(self, report: Report, *, severity: Severity) -> NotifyPayload: ...

    async def send(self, payload: NotifyPayload) -> NotifyResult: ...


# --------------------------------------------------------------------------- #
# ChannelTypeRegistry + explicit assembly (design D-1, CLAUDE.md §4.10 rule 3)
# --------------------------------------------------------------------------- #


class ChannelTypeRegistry:
    """In-memory map of channel **type** key → ``Notifier`` implementation.

    Mirrors ``InspectorRegistry``'s explicit-assembly style: nothing is
    registered at import time. Callers build an instance, then either call
    ``register_default_notifiers(registry)`` or register custom types via
    ``register``. ``get`` raises on an unknown type (never returns ``None``)
    so a typo in a manifest's ``type`` fails loud rather than silently
    dropping a channel.
    """

    def __init__(self) -> None:
        self._types: dict[str, type[Notifier]] = {}

    def register(self, channel_type: str, notifier_cls: type[Notifier]) -> None:
        """Bind ``channel_type`` to ``notifier_cls``.

        Re-registering an existing type raises ``KeyError`` so a second
        adapter cannot silently shadow the first.
        """

        if channel_type in self._types:
            raise KeyError(f"channel type already registered: {channel_type!r}")
        self._types[channel_type] = notifier_cls

    def get(self, channel_type: str) -> type[Notifier]:
        """Return the ``Notifier`` class for ``channel_type``.

        Raises ``KeyError`` for an unknown type — the assembly layer turns
        this into a fail-loud ``ConfigError`` so an unknown ``type:`` in
        ``notifiers.yaml`` never reaches the scheduler.
        """

        try:
            return self._types[channel_type]
        except KeyError as exc:
            known = ", ".join(sorted(self._types)) or "<none>"
            raise KeyError(
                f"unknown channel type {channel_type!r}; registered types: {known}"
            ) from exc

    def types(self) -> list[str]:
        """Return all registered channel type keys, sorted ascending."""

        return sorted(self._types)


def register_default_notifiers(registry: ChannelTypeRegistry) -> None:
    """Register the built-in channel adapters onto ``registry``.

    Adapter modules are imported **inside** the function body, not at module
    top level, so merely importing ``hostlens.notifiers`` registers nothing
    (CLAUDE.md §4.10 rule 3: no import-time global mutation). The two adapter
    modules referenced here are created by group C.
    """

    from hostlens.notifiers.lark import LarkNotifier
    from hostlens.notifiers.telegram import TelegramNotifier

    registry.register("telegram", TelegramNotifier)
    registry.register("lark", LarkNotifier)


# --------------------------------------------------------------------------- #
# Secret redaction for logs + NotifyResult.error (spec §需求 NotifyResult)
# --------------------------------------------------------------------------- #

# Telegram bot-token path segment: ``/bot<digits>:<base64ish>/`` inside an
# API URL. ``redact_text`` (keyword=value / Bearer / JWT / sk-) does not
# cover tokens embedded in a URL *path*, so we erase them structurally here.
_TELEGRAM_BOT_TOKEN_IN_URL = re.compile(r"(?i)/bot[0-9]+:[A-Za-z0-9_-]+")

# Generic webhook secret path segment seen in Lark / DingTalk / WeCom style
# URLs: the trailing opaque token after ``/hook/`` or ``?...key=``-style
# query are masked. We scrub both a ``/hook/<token>`` path tail and any
# ``key=``/``token=``/``sign=`` query parameter value embedded in a URL.
_WEBHOOK_HOOK_PATH = re.compile(r"(?i)(/(?:hook|webhook|send)/)[A-Za-z0-9_-]{8,}")
_URL_SECRET_QUERY = re.compile(r"(?i)([?&](?:key|token|secret|sign|access_token)=)[^&\s\"']+")

_REDACTED = "***"


def redact_secret_text(text: str) -> str:
    """Return ``text`` with channel secrets erased, for logs and
    ``NotifyResult.error``.

    Layers structural URL/token erasure (Telegram ``bot<token>`` path,
    webhook ``/hook/<secret>`` tail, ``key=/token=/sign=`` query values)
    **on top of** the generic ``redact_text`` scanner. The generic scanner
    alone misses URL-path-embedded secrets, so this function is the one
    callers MUST use before persisting an exception-derived string — it
    guarantees the result is byte-for-byte free of the known channel secret
    shapes even when an upstream exception ``repr`` embeds a full request
    URL.
    """

    out = _TELEGRAM_BOT_TOKEN_IN_URL.sub("/bot" + _REDACTED, text)
    out = _WEBHOOK_HOOK_PATH.sub(r"\1" + _REDACTED, out)
    out = _URL_SECRET_QUERY.sub(r"\1" + _REDACTED, out)
    return redact_text(out)


# --------------------------------------------------------------------------- #
# Truncation helper (task 2.4, spec §需求 render 截断)
# --------------------------------------------------------------------------- #


def _utf16_code_units(text: str) -> int:
    """Length of ``text`` in UTF-16 code units (Telegram's count unit).

    Astral-plane code points (emoji etc.) count as 2; everything in the BMP
    counts as 1. Telegram's 4096 limit is measured this way, so a Python
    ``len()`` (code points) would let a heavily-emoji body slip past the
    real API limit.
    """

    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def truncate_to_limit(
    text: str,
    limit: int,
    *,
    count_unit: Literal["code_point", "utf16"] = "code_point",
    ellipsis: str = "…",
) -> tuple[str, bool]:
    """Truncate ``text`` to ``limit`` *units* on a safe character boundary.

    Returns ``(possibly_truncated_text, truncated_flag)``. Length is measured
    in the channel's API unit (``count_unit``): ``"utf16"`` for Telegram's
    UTF-16 code-unit limit (4096), ``"code_point"`` otherwise. Truncation
    always lands on a whole-character boundary (never splits a surrogate
    pair / multi-byte char) and reserves room for ``ellipsis``.

    This is the **plain-text** truncation primitive. Adapters that emit
    structured bodies (Lark card JSON) or escaped text (Telegram MarkdownV2)
    layer their own format-boundary logic on top so a clip never leaves a
    dangling escape or unparsable JSON (spec §需求 render). The decisive rule
    "legality over length" is the adapter's responsibility; this helper only
    guarantees character-boundary safety.
    """

    measure = _utf16_code_units if count_unit == "utf16" else len
    if measure(text) <= limit:
        return text, False

    ell_len = measure(ellipsis)
    budget = limit - ell_len
    if budget <= 0:
        # The ellipsis alone exceeds the limit (pathological tiny limit):
        # return the bare ellipsis and flag truncated rather than raise.
        return ellipsis, True

    # Grow a prefix one whole code point at a time until adding the next one
    # would exceed the budget. Code-point iteration keeps surrogate pairs and
    # multi-byte chars intact by construction.
    kept: list[str] = []
    used = 0
    for ch in text:
        ch_units = measure(ch)
        if used + ch_units > budget:
            break
        kept.append(ch)
        used += ch_units
    return "".join(kept) + ellipsis, True


# --------------------------------------------------------------------------- #
# Shared bounded-retry send helper (spec §需求:send 必须有界重试)
# --------------------------------------------------------------------------- #


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse a numeric ``Retry-After`` header (seconds form) or ``None``.

    Only the integer-seconds form is honoured; the HTTP-date form is
    ignored (treated as no hint) — notification endpoints in practice emit
    the seconds form, and parsing dates would add surface for little gain.
    """

    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        value = float(raw.strip())
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _is_retryable_status(status_code: int) -> bool:
    """Retryable HTTP status set: 429 + any 5xx.

    4xx other than 429 (400 bad request / 401 invalid token …) are
    non-retryable — retrying cannot change the result and only burns the
    budget. Unexpected 3xx is likewise non-retryable (a notification
    endpoint should not redirect; treat it as a config/contract fault).
    """

    return status_code == 429 or 500 <= status_code <= 599


async def send_with_retry(
    *,
    channel: str,
    do_request: Callable[[], Awaitable[httpx.Response]],
    interpret: Callable[[httpx.Response], NotifyResult],
    logger: structlog.BoundLogger | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    hard_timeout_seconds: float = DEFAULT_CHANNEL_HARD_TIMEOUT_SECONDS,
) -> NotifyResult:
    """Run ``do_request`` with bounded exponential backoff + failure isolation.

    ``do_request`` performs one HTTP attempt and returns the
    ``httpx.Response`` (or raises a transport / timeout error). ``interpret``
    maps a *non-retryable* response (2xx success, or 4xx≠429 / unexpected 3xx
    failure) into the terminal ``NotifyResult`` — it owns the
    platform-specific business-success check (e.g. Telegram ``ok``) and
    message-id extraction, and is responsible for redacting any secret it
    puts in ``error`` / ``detail``.

    Retry policy (spec §需求:send 必须有界重试):

    - **Retryable**: 5xx, 429, request ``TimeoutError`` / ``httpx.TimeoutException``,
      and transport-layer transient errors (connection refused / reset / DNS
      via ``httpx.TransportError``). These are retried up to ``max_attempts``
      with ``1s/2s/4s`` exponential backoff + jitter.
    - **429**: honours ``Retry-After`` but the wait is clamped to fit under
      the remaining ``hard_timeout_seconds`` budget.
    - **Non-retryable**: 4xx≠429 and unexpected 3xx — handed to ``interpret``
      immediately (no further attempts).

    The whole call (all attempts + waits) is bounded by
    ``hard_timeout_seconds``; on budget exhaustion or exhausted attempts the
    function returns ``NotifyResult(status="failed", ...)`` and **never**
    raises. ``attempts`` reflects the number of HTTP attempts actually made.
    """

    loop = asyncio.get_running_loop()
    deadline = loop.time() + hard_timeout_seconds
    attempts = 0
    last_error: str = "no attempt made"

    while attempts < max_attempts:
        remaining = deadline - loop.time()
        if remaining <= 0:
            last_error = "channel hard timeout exceeded"
            break
        attempts += 1
        try:
            response = await asyncio.wait_for(do_request(), timeout=remaining)
        except (TimeoutError, httpx.TimeoutException) as exc:
            last_error = redact_secret_text(f"request timeout: {exc!r}")
        except httpx.TransportError as exc:
            last_error = redact_secret_text(f"transport error: {exc!r}")
        except Exception as exc:
            # Non-retryable request fault (e.g. ``httpx.InvalidURL`` from a
            # malformed webhook URL — it subclasses ``Exception`` directly, not
            # ``TransportError``). Retrying cannot change the outcome, and
            # ``send`` must never raise (Notifier Protocol), so terminate as
            # ``failed`` rather than retry or bubble.
            return NotifyResult(
                channel=channel,
                status="failed",
                error=redact_secret_text(f"request error: {exc!r}"),
                attempts=attempts,
            )
        else:
            status = response.status_code
            if not _is_retryable_status(status):
                # Terminal (success or non-retryable failure): let the adapter
                # interpret it. ``interpret`` sets the real attempts count by
                # reading it off the returned result below. Wrap it so a
                # malformed response can never make the helper raise.
                try:
                    result = interpret(response)
                except Exception as exc:
                    return NotifyResult(
                        channel=channel,
                        status="failed",
                        error=redact_secret_text(f"response interpret failed: {exc!r}"),
                        attempts=attempts,
                    )
                return result.model_copy(update={"attempts": attempts})
            last_error = redact_secret_text(f"retryable HTTP {status}")
            retry_after = _retry_after_seconds(response) if status == 429 else None
            wait = _next_backoff(attempts, retry_after=retry_after, deadline=deadline, loop=loop)
            if attempts < max_attempts and wait is not None:
                if logger is not None:
                    logger.warning(
                        "notify.send.retry",
                        channel=channel,
                        attempt=attempts,
                        status=status,
                        wait_seconds=round(wait, 3),
                    )
                await asyncio.sleep(wait)
                continue
            # No budget left for another attempt — fall through to failed.
            break

        # Reached only on a retryable transport/timeout exception.
        wait = _next_backoff(attempts, retry_after=None, deadline=deadline, loop=loop)
        if attempts < max_attempts and wait is not None:
            if logger is not None:
                logger.warning(
                    "notify.send.retry",
                    channel=channel,
                    attempt=attempts,
                    error=last_error,
                    wait_seconds=round(wait, 3),
                )
            await asyncio.sleep(wait)
            continue
        break

    if logger is not None:
        logger.warning("notify.send.failed", channel=channel, attempts=attempts, error=last_error)
    return NotifyResult(channel=channel, status="failed", error=last_error, attempts=attempts)


def _next_backoff(
    attempt: int,
    *,
    retry_after: float | None,
    deadline: float,
    loop: asyncio.AbstractEventLoop,
) -> float | None:
    """Compute the wait before the next attempt, clamped to the deadline.

    Base wait is ``Retry-After`` (when the server supplied it) else the
    exponential ``base * 2**(attempt-1)`` plus jitter. The result is clamped
    so it never pushes past ``deadline``; returns ``None`` only when the
    budget is exhausted (no time left for another attempt). A returned ``0.0``
    is a legitimate "retry immediately" wait (e.g. ``Retry-After: 0``) and is
    distinct from ``None``.
    """

    remaining = deadline - loop.time()
    if remaining <= 0:
        return None

    if retry_after is not None:
        base_wait = retry_after
    else:
        base_wait = _DEFAULT_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
        base_wait += random.uniform(0, _BACKOFF_JITTER_SECONDS)

    return min(base_wait, remaining)
