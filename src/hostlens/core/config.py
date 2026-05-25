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

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from hostlens.core.exceptions import ConfigError

__all__ = [
    "Settings",
    "SshSettings",
    "load_settings",
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
    config_dir: Path = Path("~/.config/hostlens").expanduser()
    targets_config_path: Path = Path("~/.config/hostlens/targets.yaml").expanduser()
    ssh: SshSettings = Field(default_factory=SshSettings)
    inspectors_search_paths: Annotated[list[Path], NoDecode] = Field(
        default_factory=lambda: [Path("~/.config/hostlens/inspectors").expanduser()]
    )

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

        Non-string inputs (default factory list, programmatic construction)
        are passed through unchanged for the regular pydantic coercion path.
        """

        if not isinstance(value, str):
            return value
        if value == "":
            return []
        return [Path(part).expanduser() for part in value.split(":")]


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
