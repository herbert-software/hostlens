"""Hostlens runtime settings.

M0 scope per openspec/changes/bootstrap-project-skeleton/specs/core-services/spec.md:
loads only from environment variables (prefix `HOSTLENS_`) and `.env`. YAML
sources (`~/.config/hostlens/*.yaml`) are deferred to M1+.

`Settings()` constructs directly through Pydantic and raises
`pydantic.ValidationError` on bad input — that path is library-internal /
advanced-user only. Application entry points must use `load_settings()`,
which converts `ValidationError` into `ConfigError` and redacts values of
any field whose name matches `_SENSITIVE_FIELD_PATTERN`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from hostlens.core.exceptions import ConfigError

__all__ = [
    "AgentSettings",
    "BackendSettings",
    "DaemonSettings",
    "Settings",
    "SshSettings",
    "load_settings",
]


# Allowed values for ``BackendSettings.type``. Constrained to a closed set so a
# typo cannot silently pass through the schema layer and only surface as a
# ``KeyError`` deep inside ``create_backend``. New backends added in M10.5 / 1.0
# must extend this Literal and add a corresponding factory branch.
BackendType = Literal[
    "anthropic_api",
    "fake",
    "playback",
    "bedrock",
    "vertex",
    "claude_subscription",
]


class SshSettings(BaseModel):
    """SSH-related runtime settings.

    M1 scope (per execution-target spec §需求:SSHTarget) is intentionally a
    single field — `connect_timeout` is a per-target override on
    `TargetEntry`, not a global default, and future fields (e.g. keepalive
    interval) join this namespace as they land.
    """

    model_config = ConfigDict(extra="forbid")

    idle_timeout_seconds: int = 300


class BackendSettings(BaseModel):
    """LLM backend configuration namespace (M2 add-llm-backend-protocol).

    Per CLAUDE.md §4.11 rule #4, ``backend`` is a separate namespace from
    ``agent``: backend manages "who we talk to / how we authenticate"; agent
    manages "which model / behavior params". Splitting them means a future
    Bedrock / Vertex switch only touches ``backend.type`` and not the model
    layer — and conversely, swapping ``primary_model`` keeps the existing
    transport untouched.

    The ``model_validator`` below gates the **type-specific required fields**
    at schema-load time so a config file with ``type=anthropic_api`` but a
    missing ``api_key`` fails loudly at startup rather than at first LLM
    call. ``bedrock`` / ``vertex`` / ``claude_subscription`` types are
    intentionally **not** gated here so a future config file can land
    ahead of the backend implementation (NotImplementedError fires in
    ``create_backend`` instead — spec §场景:backend.type = bedrock 加载阶段
    不 raise).
    """

    model_config = ConfigDict(extra="forbid")

    type: BackendType
    # ``SecretStr`` ensures ``model_dump_json()`` outputs ``"**********"``;
    # the raw value is only accessible via ``.get_secret_value()``.
    api_key: SecretStr | None = None
    base_url: HttpUrl | None = None
    cassette_path: Path | None = None
    # Reserved placeholders for M10.5 (bedrock) / 1.0 (vertex) /
    # M10.5 experimental (claude_subscription). Validation for these
    # types lives in ``create_backend``, not here.
    aws_region: str | None = None
    aws_profile: str | None = None
    oauth_token: SecretStr | None = None
    accept_subscription_risks: bool = False
    # Suppress provider-default extended-thinking output for thinking-default-on
    # Anthropic-compatible endpoints (e.g. DeepSeek-over-anthropic). Only the
    # ``anthropic_api`` path consumes this in ``create_backend``; it is
    # intentionally decoupled from ``type`` (no cross-field validation) so any
    # type may set it and it is a silent no-op on non-anthropic_api paths.
    disable_thinking: bool = False

    @model_validator(mode="after")
    def _validate_type_specific_required_fields(self) -> BackendSettings:
        """Gate ``api_key`` / ``cassette_path`` requirements by ``type``.

        Only the two M2-implemented types (``anthropic_api`` / ``playback``)
        get their required fields enforced here; bedrock / vertex /
        claude_subscription are schema-only placeholders so a config file
        can ship before the backend impl lands.
        """

        if self.type == "anthropic_api" and self.api_key is None:
            raise ValueError("api_key required for type=anthropic_api")
        if self.type == "playback" and self.cassette_path is None:
            raise ValueError("cassette_path required for type=playback")
        return self


class AgentSettings(BaseModel):
    """Agent-layer runtime settings (M2 add-llm-backend-protocol).

    Holds the model identifiers and runtime budgets the Agent loop reads;
    decoupled from ``BackendSettings`` per CLAUDE.md §4.11 rule #4 so
    switching transport (``anthropic_api`` → ``bedrock``) does not require
    rewriting the agent block.

    The numeric bounds (``ge`` / ``le`` on ``Field``) defend against typos
    that would otherwise burn a full ``token_budget_input=10_000_000``
    quota in a single Agent turn. Bounds are aligned with the Anthropic
    Messages API current limits.
    """

    model_config = ConfigDict(extra="forbid")

    primary_model: str = "claude-opus-4-7"
    fallback_model: str | None = None
    # ``health_check_model`` defaults to a cheaper Haiku tier so doctor /
    # ``BackendDiagnostics.health_check`` ping calls do not consume Opus
    # quota (spec §需求:Settings 必须支持 backend 与 agent 两个独立 namespace).
    health_check_model: str = "claude-haiku-4-5"
    max_turns: int = Field(default=20, ge=1, le=100)
    token_budget_input: int = Field(default=100_000, ge=1, le=1_000_000)
    token_budget_output: int = Field(default=30_000, ge=1, le=200_000)


class DaemonSettings(BaseModel):
    """Scheduler-daemon runtime settings.

    ``shutdown_grace_seconds`` is how long ``graceful_stop`` waits for an
    in-flight job to finish naturally before force-cancelling it. The default
    of 120s replaces the old fixed 30s, which was smaller than a single LLM
    API timeout (60s) and so force-cancelled jobs still legitimately waiting
    on the model. The ``ge=1`` lower bound forbids 0/negative (which would
    make ``asyncio.wait(timeout=0)`` cancel every in-flight job immediately,
    i.e. no graceful stop); ``le=600`` caps how long a shutdown can stall.

    Held as a ``default_factory`` namespace on ``Settings`` (like ``ssh``,
    NOT optional like ``agent``) so the grace is always readable on the
    shutdown path.
    """

    model_config = ConfigDict(extra="forbid")

    shutdown_grace_seconds: float = Field(default=120.0, ge=1, le=600)


_SENSITIVE_FIELD_PATTERN: re.Pattern[str] = re.compile(
    r"(?i)(key|token|secret|password|credential)"
)
"""Field-name regex used by `load_settings()` to redact values in error messages.

Names are tested with `re.search` (case-insensitive), so any substring match
triggers redaction — e.g. `anthropic_api_key`, `auth_token`, `db_password`.
"""

_REDACTED: str = "***"


class Settings(BaseSettings):
    """Hostlens runtime settings loaded from env + `.env`.

    Direct construction (`Settings()`) raises `pydantic.ValidationError` on
    bad input. Application code should call `load_settings()` instead so
    sensitive field values are redacted in the surfaced `ConfigError`.
    """

    model_config = SettingsConfigDict(
        env_prefix="HOSTLENS_",
        env_file=".env",
        env_nested_delimiter="__",
        extra="ignore",
    )

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_mode: Literal["dev", "prod"] = "prod"
    # Scheduler daemon-mode flag (M4 add-scheduler, design D-12). The
    # ``schedule daemon`` / ``schedule run`` entry points flip this to True
    # (via ``model_copy``) before calling ``create_backend`` so the existing
    # ``is_daemon_mode`` seam fires the backend daemon-safety gate. Defaults
    # to False so every other code path (CLI one-shots, tests) keeps the M2
    # behavior. NOT read from the environment in normal use, but it lives on
    # ``Settings`` so the locked ``is_daemon_mode(settings) -> bool`` signature
    # carries it without a separate contextvar.
    daemon_mode: bool = False
    config_dir: Path = Path("~/.config/hostlens").expanduser()
    targets_config_path: Path = Path("~/.config/hostlens/targets.yaml").expanduser()
    notifiers_config_path: Path = Path("~/.config/hostlens/notifiers.yaml").expanduser()
    ssh: SshSettings = Field(default_factory=SshSettings)
    # Daemon-level runtime params. Uses
    # ``default_factory`` (like ``ssh``, not optional like ``agent``) so
    # ``daemon.shutdown_grace_seconds`` is always readable on the shutdown
    # path. env override: ``HOSTLENS_DAEMON__SHUTDOWN_GRACE_SECONDS`` (double
    # underscore routes into the namespace; the single-underscore
    # ``HOSTLENS_DAEMON_MODE`` flat field above is unaffected).
    daemon: DaemonSettings = Field(default_factory=DaemonSettings)
    inspectors_search_paths: Annotated[list[Path], NoDecode] = Field(
        default_factory=lambda: [Path("~/.config/hostlens/inspectors").expanduser()]
    )
    # M2 add-llm-backend-protocol: both namespaces default to ``None`` so M0
    # / M1 configs without LLM blocks load cleanly. ``create_backend`` raises
    # ``ConfigError`` when ``backend is None`` and an LLM feature is used.
    backend: BackendSettings | None = None
    agent: AgentSettings | None = None

    @field_validator("inspectors_search_paths", mode="before")
    @classmethod
    def _split_inspectors_search_paths(cls, value: Any) -> Any:
        """Parse env override for `inspectors_search_paths` as Unix-PATH-style.

        pydantic-settings would otherwise JSON-decode this list-typed env
        value before any validator runs; the `NoDecode` annotation on the
        field disables that, so the env source hands us the raw `str` and
        we apply the documented `:`-separated contract here:

        - empty string → empty list (`HOSTLENS_INSPECTORS_SEARCH_PATHS=""`)
        - `"/a"` → `[Path("/a")]`
        - `"/a:/b"` → `[Path("/a"), Path("/b")]` (order preserved)
        - each path is `expanduser()`-ed so `~/x` resolves consistently
        - **empty path segments are dropped** (`":/a"` / `"/a::/b"` → drop the
          empty parts) so users can't silently inject the current working
          directory into the inspector search path via a stray colon — that
          would make a manifest under `$PWD` shadow trusted locations

        Non-string inputs (default factory list, programmatic construction)
        are passed through unchanged for the regular pydantic coercion path.
        """

        if not isinstance(value, str):
            return value
        if value == "":
            return []
        return [Path(part).expanduser() for part in value.split(":") if part]


def _is_sensitive(field_name: str) -> bool:
    return _SENSITIVE_FIELD_PATTERN.search(field_name) is not None


def _format_validation_error(ve: ValidationError) -> str:
    """Render a `ValidationError` into a single human-readable string.

    For each underlying error we emit `<field>: <msg> (input=<value>)`,
    substituting `_REDACTED` for the input when the field name matches
    `_SENSITIVE_FIELD_PATTERN`. The expected-values context (when present)
    is included so callers see e.g. the valid enum members.
    """

    lines: list[str] = []
    for error in ve.errors():
        loc: tuple[Any, ...] = error.get("loc", ())
        field_name = ".".join(str(part) for part in loc) if loc else "<root>"
        msg = error.get("msg", "")
        raw_input = error.get("input")
        display_input: str = _REDACTED if _is_sensitive(field_name) else repr(raw_input)

        ctx = error.get("ctx") or {}
        expected = ctx.get("expected") if isinstance(ctx, dict) else None
        expected_suffix = f" expected={expected}" if expected else ""

        lines.append(f"{field_name}: {msg} (input={display_input}){expected_suffix}")

    header = f"{len(lines)} configuration error{'s' if len(lines) != 1 else ''}"
    return header + ":\n" + "\n".join(lines)


def load_settings() -> Settings:
    """Build `Settings` from env + `.env`, redacting sensitive values on error.

    Raises:
        ConfigError: when Pydantic validation fails. The message contains
            the offending field names, expected types/values, and actual
            input — except for fields whose names match
            `_SENSITIVE_FIELD_PATTERN`, whose values are replaced with
            `"***"`. The original `ValidationError` is chained via
            `ConfigError.original` for callers that need raw details.
    """

    try:
        return Settings()
    except ValidationError as ve:
        message = _format_validation_error(ve)
        raise ConfigError(message, original=ve) from ve
