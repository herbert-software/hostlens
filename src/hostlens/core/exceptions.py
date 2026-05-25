"""Hostlens core exception hierarchy.

M0 introduced exactly four classes: HostlensError, ConfigError, TargetError,
InspectorError.

M2 (`add-tool-registry-capability-layer`) extends this module with two
additional subclasses to support the Tool Registry capability layer:
ToolError and ToolPolicyViolation. The latter carries a fully constrained
structured-field set (all four fields drawn from bounded value domains) so
that its string representation cannot become a prompt/log injection or
secret-leak surface.

M1 (`add-execution-target-abstraction`) extends `ConfigError` and
`TargetError` with structured `kind` + `**extra` fields so that loader /
target layer error sites can attach machine-readable error codes and
structured context (e.g. `kind="missing_env_var", var_name="X",
target="prod-web"`) without losing M0 positional-message backward
compatibility.
"""

from __future__ import annotations

import re
from typing import Literal, get_args

__all__ = [
    "ConfigError",
    "HostlensError",
    "InspectorError",
    "TargetError",
    "ToolError",
    "ToolPolicyViolation",
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
    "approval_flow_not_supported_in_m2",
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


class InspectorError(HostlensError):
    """Raised on Inspector loading or execution errors (used from M1+)."""


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
