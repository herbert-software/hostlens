"""Hostlens core exception hierarchy.

M0 introduced exactly four classes: HostlensError, ConfigError, TargetError,
InspectorError.

M2 (`add-tool-registry-capability-layer`) extends this module with two
additional subclasses to support the Tool Registry capability layer:
ToolError and ToolPolicyViolation. The latter carries a fully constrained
structured-field set (all four fields drawn from bounded value domains) so
that its string representation cannot become a prompt/log injection or
secret-leak surface.

M2 (`add-llm-backend-protocol`) extends this module with five
backend-domain exception subclasses (``BackendError`` plus four
specializations): ``BackendUnavailable`` / ``BackendRateLimited`` /
``BackendCapabilityViolation`` / ``BackendDaemonUnsafe``. ``BackendError``
implements a deliberately defensive ``__str__`` (no ``cause.__dict__`` /
``response.headers`` dump; cause text passes through ``redact_text``
before being truncated to 200 chars) so an upstream SDK exception with
secret-carrying attributes cannot leak through the exception surface.

M1 (`add-execution-target-abstraction`) extends `ConfigError` and
`TargetError` with structured `kind` + `**extra` fields so that loader /
target layer error sites can attach machine-readable error codes and
structured context (e.g. `kind="missing_env_var", var_name="X",
target="prod-web"`) without losing M0 positional-message backward
compatibility.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal, get_args

from hostlens.core.redact import redact_text

__all__ = [
    "BackendCapabilityViolation",
    "BackendDaemonUnsafe",
    "BackendError",
    "BackendRateLimited",
    "BackendUnavailable",
    "ConfigError",
    "HostlensError",
    "InspectorError",
    "ReplayMiss",
    "TargetError",
    "ToolError",
    "ToolPolicyViolation",
    "UnexpectedStopReason",
]


# Mirror of `ToolSpec.name` regex. Enforced at `ToolPolicyViolation.__init__`
# time so a caller cannot smuggle paths / IPs / free text into `tool_name`
# and have them surface via `__str__` (defeating the structured-field
# design that exists precisely to make this class injection-safe).
_TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


# Max length of attacker-controlled input echoed back in a ValueError message.
# Applied to ALL four ToolPolicyViolation field validators so a non-Literal
# value (path / token / IP) cannot leak in full via the exception message.
_PREVIEW_MAX_LEN = 32


def _preview(value: object) -> str:
    """Return a safe-to-echo preview of an attacker-controlled value.

    Strings are truncated to `_PREVIEW_MAX_LEN` chars; non-strings are
    represented by their type name (never `repr`-ed in full).
    """

    if isinstance(value, str):
        return value[:_PREVIEW_MAX_LEN]
    return type(value).__name__


def _format_extra(extra: dict[str, object]) -> str:
    """Render an ``extra`` dict as ``key=value`` pairs, sorted by key.

    Used by ``ConfigError`` / ``TargetError`` ``__str__`` so structured
    context is reproducible across calls (test snapshots) and human-readable
    without dumping the whole dict's repr.
    """

    if not extra:
        return ""
    return " ".join(f"{key}={extra[key]}" for key in sorted(extra))


# ---------------------------------------------------------------------------
# Literal value domains for ToolPolicyViolation
# ---------------------------------------------------------------------------

ToolPolicySurface = Literal["agent", "mcp", "cli"]
ToolPolicyViolatedField = Literal[
    "surfaces",
    "side_effects",
    "requires_approval",
    "sensitive_output",
    "permissions",
    "target_constraints",
]
ToolPolicyReason = Literal[
    "not_exposed_to_surface",
    "side_effects_not_permitted",
    "approval_flow_not_supported",
    "sensitive_output_not_declared",
    "missing_required_permission",
    "target_constraint_violated",
]


class HostlensError(Exception):
    """Base exception for all Hostlens-defined errors."""


class ConfigError(HostlensError):
    """Raised when configuration loading or validation fails.

    M1 extension (`add-execution-target-abstraction`): accepts an optional
    structured ``kind`` (e.g. ``"missing_env_var"``) plus arbitrary
    ``**extra`` keyword fields (``var_name=...``, ``target=...``,
    ``field=...``) so loader call sites can attach machine-readable error
    codes for doctor / structured logging. ``original`` still chains the
    underlying exception (e.g. a ``pydantic.ValidationError`` captured by
    ``load_settings()``).

    Backward compatible with the M0 call style ``ConfigError("invalid
    yaml")`` and ``ConfigError("invalid yaml", original=e)``.

    ``__str__`` format:
        ``"{kind}: {message} key=value ..."`` when ``kind`` is set, else
        ``"{message} key=value ..."``; the trailing ``key=value`` list is
        only appended when ``extra`` is non-empty.

    Spec contract (per CLAUDE.md / proposal): callers MUST NOT put raw
    secret values into ``extra`` — pass references like ``var_name=...``
    instead, never the value itself.
    """

    def __init__(
        self,
        message: str | None = None,
        *,
        kind: str | None = None,
        original: Exception | None = None,
        **extra: object,
    ) -> None:
        super().__init__(message if message is not None else "")
        self.message: str | None = message
        self.kind: str | None = kind
        self.original: Exception | None = original
        self.extra: dict[str, object] = dict(extra)

    def __str__(self) -> str:
        parts: list[str] = []
        body = self.message if self.message is not None else ""
        if self.kind is not None:
            parts.append(f"{self.kind}: {body}" if body else self.kind)
        elif body:
            parts.append(body)
        extra_str = _format_extra(self.extra)
        if extra_str:
            parts.append(extra_str)
        return " ".join(parts)


class ReplayMiss(HostlensError):  # noqa: N818 - spec mandates this exact name (no "Error" suffix)
    """Raised when a ``ReplayTarget`` exec/read_file misses its fixture.

    Spec: ``add-incident-pack/specs/replay-execution-target/spec.md``
    §需求:回放命令匹配与未命中语义.

    Inherits ``HostlensError`` and **deliberately NOT** ``TargetError``: a
    fixture miss is an infra / programming error (the recorded fixture has
    drifted from the Inspector commands), not a transport failure of a real
    target. If it subclassed ``TargetError`` the runner's
    ``except TargetError`` would map it to ``status="target_unreachable"`` —
    silently swallowing command drift as a benign "target down" result and
    destroying the loud-failure contract.

    ``kind`` is one of ``"exec"`` / ``"read_file"`` (which call missed);
    ``cmd`` carries the missed command / path. Both are recorded so callers
    (and the ``__str__`` surface) can identify the drift. The fields come
    from rendered Inspector commands (already sh-quoted by the manifest
    renderer) — no secret values, since secrets travel via ``env`` which is
    never part of the match key.
    """

    def __init__(self, *, kind: Literal["exec", "read_file"], cmd: str) -> None:
        super().__init__(kind)
        self.kind: Literal["exec", "read_file"] = kind
        self.cmd: str = cmd

    def __str__(self) -> str:
        return f"ReplayMiss(kind={self.kind}, cmd={self.cmd!r})"


class TargetError(HostlensError):
    """Raised on ExecutionTarget errors (used from M1+).

    Carries a structured ``kind`` (e.g. ``"ssh_auth_failed"`` /
    ``"duplicate_target"`` / ``"file_too_large"`` / ``"invalid_target_name"``
    / ``"target_entry_name_mismatch"`` / ``"target_disabled"`` /
    ``"ssh_connection_lost"`` / ``"ssh_connect_timeout"`` /
    ``"ssh_connect_failed"`` / ``"sftp_unavailable"``) so CLI / doctor
    can render machine-readable error codes without parsing free text.

    Field ``target`` is the **target identifier** the error is about
    (``None`` when not applicable). ``**extra`` collects any other
    structured context (``path=``, ``size=``, ``entry_name=``,
    ``host=``, etc.); callers MUST NOT include raw secret values.

    Spec: ``execution-target/spec.md`` §需求:`ExecutionTarget` Protocol
    必须定义完整接口 / `TargetRegistry` 必须按 name 索引 / ``hostlens
    target`` CLI 命令集.
    """

    def __init__(
        self,
        kind: str,
        *,
        target: str | None = None,
        original: Exception | None = None,
        **extra: object,
    ) -> None:
        # Build a stable base message so ``args[0]`` is non-empty even
        # before ``__str__`` is called (helps debuggers that print
        # ``exc.args``).
        super().__init__(kind)
        self.kind: str = kind
        self.target: str | None = target
        self.original: Exception | None = original
        self.extra: dict[str, object] = dict(extra)

    def __str__(self) -> str:
        parts: list[str] = [self.kind]
        if self.target is not None:
            parts.append(f"target={self.target}")
        extra_str = _format_extra(self.extra)
        if extra_str:
            parts.append(extra_str)
        return " ".join(parts)


InspectorErrorKind = Literal[
    "manifest_parse_error",
    "manifest_validation_error",
    "manifest_too_large",
    "unquoted_parameter_in_command",
    "unquoted_array_parameter_in_command",
    "array_parameter_items_type_undetermined",
    "parameter_missing_charset_constraint",
    "secret_inlined_in_command",
    "unsafe_raw_not_supported_in_m1",
    "command_template_invalid",
    "finding_when_invalid",
    "finding_message_invalid_aggregate_ref",
    "parameter_reserved_window_name",
    "duplicate_inspector",
    "inspector_not_found",
    "parse_json_not_object",
]


# Pre-compute the allowed-kind set once; used by ``InspectorError.__init__``
# to validate the ``kind`` argument without re-introspecting the ``Literal``
# on every construction.
_INSPECTOR_ERROR_KINDS: frozenset[str] = frozenset(get_args(InspectorErrorKind))


class InspectorError(HostlensError):
    """Raised on Inspector loading or execution errors (M1+).

    Spec: ``inspector-plugin-system/spec.md`` §需求:``InspectorError``
    必须扩展支持结构化字段.

    All parameters are **keyword-only** — positional construction (with
    a bare string as the first arg) raises ``TypeError`` so legacy M0
    free-text call sites are forced to migrate to the structured form.
    ``kind`` is constrained to the 15-value M1 enum
    (``InspectorErrorKind``); any other value raises ``ValueError`` at
    construction time so a typo cannot silently surface in logs.

    ``__str__`` format:
        ``"{kind}: key=value key=value ..."`` — the ``kind`` is always the
        prefix (it is the machine-readable error code surfaced to doctor /
        CLI / structured logging); structured fields with non-``None``
        values are appended as ``key=value`` pairs sorted by key for
        snapshot stability. ``errors`` (Pydantic / loader detail list) is
        rendered as ``errors=<N items>`` rather than the full payload to
        keep the rendering log-friendly; callers wanting full details
        should inspect the attribute directly.

    Spec contract (per CLAUDE.md §4.5 / proposal): callers MUST NOT put
    raw secret values into ``extra`` — pass references like
    ``secret="PGPASSWORD"`` instead, never the value itself.
    """

    def __init__(
        self,
        *,
        kind: InspectorErrorKind,
        path: Path | None = None,
        inspector: str | None = None,
        parameter: str | None = None,
        secret: str | None = None,
        field: str | None = None,
        index: int | None = None,
        existing_path: Path | None = None,
        new_path: Path | None = None,
        errors: list[dict[str, Any]] | None = None,
        original: Exception | None = None,
        **extra: object,
    ) -> None:
        if kind not in _INSPECTOR_ERROR_KINDS:
            allowed = tuple(sorted(_INSPECTOR_ERROR_KINDS))
            raise ValueError(f"InspectorError.kind must be one of {allowed}, got {kind!r}")

        # ``args[0]`` is the kind so debuggers / unittest output that print
        # ``exc.args`` show the machine-readable error code rather than an
        # empty string.
        super().__init__(kind)
        self.kind: InspectorErrorKind = kind
        self.path: Path | None = path
        self.inspector: str | None = inspector
        self.parameter: str | None = parameter
        self.secret: str | None = secret
        self.field: str | None = field
        self.index: int | None = index
        self.existing_path: Path | None = existing_path
        self.new_path: Path | None = new_path
        self.errors: list[dict[str, Any]] | None = errors
        self.original: Exception | None = original
        self.extra: dict[str, object] = dict(extra)

    def __str__(self) -> str:
        # Collect named structured fields whose value is non-None into a
        # ``{key: value}`` dict for sorted rendering. Keep ``errors`` /
        # ``original`` out of the rendered surface (large payloads / repr
        # noise); callers can pull them off the attribute directly.
        rendered: dict[str, str] = {}
        if self.path is not None:
            rendered["path"] = str(self.path)
        if self.inspector is not None:
            rendered["inspector"] = self.inspector
        if self.parameter is not None:
            rendered["parameter"] = self.parameter
        if self.secret is not None:
            rendered["secret"] = self.secret
        if self.field is not None:
            rendered["field"] = self.field
        if self.index is not None:
            rendered["index"] = str(self.index)
        if self.existing_path is not None:
            rendered["existing_path"] = str(self.existing_path)
        if self.new_path is not None:
            rendered["new_path"] = str(self.new_path)
        if self.errors is not None:
            rendered["errors"] = f"<{len(self.errors)} items>"
        for key in sorted(self.extra):
            rendered[key] = str(self.extra[key])

        if not rendered:
            return f"{self.kind}:"
        body = " ".join(f"{key}={rendered[key]}" for key in sorted(rendered))
        return f"{self.kind}: {body}"


class ToolError(HostlensError):
    """Base class for Tool Registry / ToolSpec related errors (M2+)."""


class ToolPolicyViolation(ToolError):  # noqa: N818 - spec mandates this exact name (no "Error" suffix)
    """Raised when a policy gate rejects a ToolSpec dispatch attempt.

    All four structured fields are drawn from a constrained value domain so
    that `__str__` / `__repr__` output cannot include user-supplied data,
    paths, IPs, or secrets:

    - `tool_name`: indirectly constrained by ToolSpec.name regex
      `^[a-z][a-z0-9_]*$`.
    - `surface`, `violated_field`, `reason`: `Literal[...]` enums; non-member
      values raise `ValueError` at `__init__` time.

    This design closes the prompt-injection / log-injection / secret-leak
    surface that a free-text `reason: str` would otherwise open.
    """

    def __init__(
        self,
        *,
        tool_name: str,
        surface: ToolPolicySurface,
        violated_field: ToolPolicyViolatedField,
        reason: ToolPolicyReason,
    ) -> None:
        allowed_surfaces = get_args(ToolPolicySurface)
        allowed_fields = get_args(ToolPolicyViolatedField)
        allowed_reasons = get_args(ToolPolicyReason)

        # All four ValueError paths truncate any attacker-controlled input to
        # 32 chars before echoing — otherwise the exception itself becomes a
        # prompt-injection / log-injection / secret-leak vector when callers
        # pass paths / tokens / IPs as invalid values.
        if surface not in allowed_surfaces:
            preview = _preview(surface)
            raise ValueError(
                f"surface must be one of {allowed_surfaces}, got (truncated) {preview!r}"
            )
        if violated_field not in allowed_fields:
            preview = _preview(violated_field)
            raise ValueError(
                f"violated_field must be one of {allowed_fields}, got (truncated) {preview!r}"
            )
        if reason not in allowed_reasons:
            preview = _preview(reason)
            raise ValueError(
                f"reason must be one of {allowed_reasons}, got (truncated) {preview!r}"
            )
        if not isinstance(tool_name, str) or _TOOL_NAME_PATTERN.fullmatch(tool_name) is None:
            preview = _preview(tool_name)
            raise ValueError(
                f"tool_name must match {_TOOL_NAME_PATTERN.pattern!r}, got (truncated) {preview!r}"
            )

        self.tool_name: str = tool_name
        self.surface: ToolPolicySurface = surface
        self.violated_field: ToolPolicyViolatedField = violated_field
        self.reason: ToolPolicyReason = reason
        super().__init__(self.__str__())

    def __str__(self) -> str:
        return (
            f"ToolPolicyViolation(tool={self.tool_name}, "
            f"surface={self.surface}, "
            f"field={self.violated_field}, "
            f"reason={self.reason})"
        )


# ---------------------------------------------------------------------------
# add-llm-backend-protocol: backend-domain exceptions
# ---------------------------------------------------------------------------

# Maximum length of the cause text embedded in BackendError.__str__. Keeps
# the rendered exception log-friendly even when the upstream SDK error
# message is unexpectedly large, and provides a hard cap on how much
# (potentially partially-scrubbed) text reaches downstream logs.
_BACKEND_CAUSE_PREVIEW_MAX_LEN = 200

# Maximum length of the cause request_id embedded in BackendError.__str__.
# Anthropic request ids are short (~40 chars in practice); the cap is a
# defense-in-depth bound, not a feature.
_BACKEND_REQUEST_ID_MAX_LEN = 40


# Allowed value domain for ``BackendCapabilityViolation.capability``. The
# field mirrors ``BackendCapabilities`` field names; the Literal makes
# Literal-violation a ``ValueError`` at construction time so a typo can't
# silently land in logs.
BackendCapabilityName = Literal[
    "prompt_caching",
    "tool_use",
    "structured_output",
    "parallel_tool_use",
    "extended_thinking",
    "vision",
    "streaming",
]


# Allowed value domain for ``BackendCapabilityViolation.attempted_feature``.
# Constrained to a closed set so the field cannot become a free-text
# prompt/log injection vector. Add a new value here only when a new
# capability-gate scenario lands.
BackendAttemptedFeature = Literal[
    "cache_control_in_system_block",
    "cache_control_in_messages_block",
    "cache_control_in_tools_array",
    "tools_array_non_empty",
]


# Allowed value domain for ``BackendDaemonUnsafe.reason``. Same prompt-/
# log-injection reasoning as ``BackendAttemptedFeature``.
BackendDaemonUnsafeReason = Literal[
    "subscription_in_daemon",
    "concurrent_request_limit_exceeded",
]


_BACKEND_CAPABILITY_NAMES: frozenset[str] = frozenset(get_args(BackendCapabilityName))
_BACKEND_ATTEMPTED_FEATURES: frozenset[str] = frozenset(get_args(BackendAttemptedFeature))
_BACKEND_DAEMON_UNSAFE_REASONS: frozenset[str] = frozenset(get_args(BackendDaemonUnsafeReason))


def _extract_cause_text(cause: Exception | None) -> str:
    """Return a safe-to-render text for ``cause`` without dumping internals.

    Fallback order (each step must not itself raise):

    1. ``cause is None`` → ``""``.
    2. ``cause.message`` is a ``str`` → use it.
    3. ``cause.args`` non-empty and ``args[0]`` is a ``str`` → use ``args[0]``.
    4. ``cause.args`` non-empty and ``args[0]`` is not a ``str`` →
       ``type(args[0]).__name__`` (avoid stringifying arbitrary objects).
    5. otherwise → ``type(cause).__name__``.

    The function deliberately avoids:

    - ``str(cause)`` (which can call SDK-defined ``__str__`` that dumps
      ``response.headers`` / ``body``)
    - ``cause.__dict__`` / ``cause.response.headers`` / ``cause.body``
      (each can carry api_key / Authorization values)

    The returned text is intended to be passed through ``redact_text``
    *afterwards* by the caller; this function only does the safe extraction.
    """

    if cause is None:
        return ""
    # 1) ``cause.message`` preferred over args[0] when present and string
    #    (some SDK exceptions like anthropic.* expose ``.message`` separately
    #    from ``args``).
    message = getattr(cause, "message", None)
    if isinstance(message, str):
        return message
    args = getattr(cause, "args", ())
    if args:
        first = args[0]
        if isinstance(first, str):
            return first
        return type(first).__name__
    return type(cause).__name__


class BackendError(HostlensError):
    """Raised for any LLM backend communication error.

    Base for all ``Backend*`` subclasses. Carries the backend identifier
    (``backend_name``) so multi-backend deployments can attribute errors,
    plus optional ``kind`` (e.g. ``"auth_invalid"``) and ``cause``
    (the upstream SDK exception, for chaining without dumping its
    internals).

    ``__str__`` is deliberately defensive: cause text is extracted via
    ``_extract_cause_text`` (never ``str(cause)``), passed through
    ``redact_text`` to mask api_keys / JWT / bearer tokens, then truncated
    to a fixed length. ``cause.status_code`` / ``cause.request_id`` are
    pulled by ``getattr`` (returning ``None`` if absent) and never reach
    raw ``__dict__`` / ``response`` dumps.
    """

    def __init__(
        self,
        message: str = "",
        *,
        backend_name: str,
        kind: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.message: str = message
        self.backend_name: str = backend_name
        self.kind: str | None = kind
        self.cause: Exception | None = cause

    def __str__(self) -> str:
        cause_text_raw = _extract_cause_text(self.cause)
        cause_text_redacted = (
            redact_text(cause_text_raw)[:_BACKEND_CAUSE_PREVIEW_MAX_LEN] if cause_text_raw else ""
        )
        status = getattr(self.cause, "status_code", None) if self.cause is not None else None
        request_id_raw = getattr(self.cause, "request_id", None) if self.cause is not None else None
        if isinstance(request_id_raw, str):
            request_id: str | None = request_id_raw[:_BACKEND_REQUEST_ID_MAX_LEN]
        else:
            request_id = None
        return (
            f"BackendError(backend={self.backend_name}, "
            f"kind={self.kind}, "
            f"cause={cause_text_redacted}, "
            f"status={status}, "
            f"request_id={request_id})"
        )


class BackendUnavailable(BackendError):  # noqa: N818 - spec mandates this exact name (no "Error" suffix)
    """Raised when the backend is unreachable (network / 5xx / DNS / timeout)."""


class BackendRateLimited(BackendError):  # noqa: N818 - spec mandates this exact name (no "Error" suffix)
    """Raised when the backend returns 429 / 529 / soft subscription limit.

    ``retry_after_seconds`` is the parsed ``retry-after`` header value when
    present (typical for 429); ``None`` for 529 / overload events where the
    upstream does not provide a hint (the Agent loop falls back to a fixed
    backoff per ARCHITECTURE.md §9 Failure Semantics).
    """

    def __init__(
        self,
        *,
        backend_name: str,
        retry_after_seconds: float | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(
            "",
            backend_name=backend_name,
            kind="rate_limited",
            cause=cause,
        )
        self.retry_after_seconds: float | None = retry_after_seconds

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base} retry_after={self.retry_after_seconds}"


class BackendCapabilityViolation(BackendError):  # noqa: N818 - spec mandates this exact name (no "Error" suffix)
    """Raised when a request uses a feature the backend declared unsupported.

    Constructed at backend ``messages_create`` entry — e.g. the Agent loop
    injected ``cache_control`` blocks against a backend that declared
    ``capabilities.prompt_caching=False``. Per CLAUDE.md §4.11 rule #2 the
    backend MUST raise rather than silently strip the field, so the
    capability declaration stays observable.

    Both ``capability`` and ``attempted_feature`` are drawn from constrained
    ``Literal`` sets; non-member values raise ``ValueError`` at construction
    time (defense against accidental free-text leakage into structured
    logs).
    """

    def __init__(
        self,
        *,
        backend_name: str,
        capability: BackendCapabilityName,
        attempted_feature: BackendAttemptedFeature,
    ) -> None:
        if capability not in _BACKEND_CAPABILITY_NAMES:
            preview = _preview(capability)
            raise ValueError(
                f"capability must be one of {sorted(_BACKEND_CAPABILITY_NAMES)}, "
                f"got (truncated) {preview!r}"
            )
        if attempted_feature not in _BACKEND_ATTEMPTED_FEATURES:
            preview = _preview(attempted_feature)
            raise ValueError(
                f"attempted_feature must be one of {sorted(_BACKEND_ATTEMPTED_FEATURES)}, "
                f"got (truncated) {preview!r}"
            )
        super().__init__(
            "",
            backend_name=backend_name,
            kind="capability_violation",
            cause=None,
        )
        self.capability: BackendCapabilityName = capability
        self.attempted_feature: BackendAttemptedFeature = attempted_feature

    def __str__(self) -> str:
        return (
            f"BackendCapabilityViolation(backend={self.backend_name}, "
            f"capability={self.capability}, "
            f"attempted_feature={self.attempted_feature})"
        )


class BackendDaemonUnsafe(BackendError):  # noqa: N818 - spec mandates this exact name (no "Error" suffix)
    """Raised when ``BackendDiagnostics.ensure_safe_for_daemon`` rejects.

    M2 has no scheduler daemon yet so this is currently a contract-only
    class — it ships now so M5 Scheduler / M10.5 ``ClaudeSubscriptionBackend``
    land on the existing exception type rather than introducing a new
    public symbol later.

    ``reason`` is constrained to a Literal set (same prompt-/log-injection
    discipline as ``BackendCapabilityViolation.attempted_feature``).
    """

    def __init__(
        self,
        *,
        backend_name: str,
        reason: BackendDaemonUnsafeReason,
    ) -> None:
        if reason not in _BACKEND_DAEMON_UNSAFE_REASONS:
            preview = _preview(reason)
            raise ValueError(
                f"reason must be one of {sorted(_BACKEND_DAEMON_UNSAFE_REASONS)}, "
                f"got (truncated) {preview!r}"
            )
        super().__init__(
            "",
            backend_name=backend_name,
            kind="daemon_unsafe",
            cause=None,
        )
        self.reason: BackendDaemonUnsafeReason = reason

    def __str__(self) -> str:
        return f"BackendDaemonUnsafe(backend={self.backend_name}, reason={self.reason})"


# ---------------------------------------------------------------------------
# add-agent-loop-skeleton: Agent loop control-flow exceptions
# ---------------------------------------------------------------------------


class UnexpectedStopReason(HostlensError):  # noqa: N818 - intentional control-flow name (no "Error" suffix)
    """Raised when the model returns a ``stop_reason`` Hostlens never solicits.

    Hostlens does not send ``stop_sequences`` and does not use server-tool
    pause, so ``"stop_sequence"`` / ``"pause_turn"`` arriving from the backend
    means either the request was constructed wrong or the provider changed
    behavior — both must fail loud rather than be mapped to a degraded status
    (which would hide the cause). The offending value is carried verbatim
    because ``MessageResponse.stop_reason`` is a closed Literal set with no
    free-text / secret content.
    """

    def __init__(self, stop_reason: str) -> None:
        super().__init__(stop_reason)
        self.stop_reason: str = stop_reason

    def __str__(self) -> str:
        return f"UnexpectedStopReason(stop_reason={self.stop_reason})"
