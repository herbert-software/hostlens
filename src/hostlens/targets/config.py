"""``TargetsConfig`` and yaml loader for ``~/.config/hostlens/targets.yaml``.

Spec: ``openspec/changes/add-execution-target-abstraction/specs/execution-target/spec.md``
§需求:`TargetsConfig` 必须从 yaml 加载且环境变量占位展开.

The loader is intentionally narrow: it only

1. Reads the yaml file (returning an empty config if the file is absent),
2. Expands ``${VAR}`` placeholders **only** inside the secret fields
   ``password`` / ``passphrase``, and
3. Hands the resulting dict to Pydantic for schema validation.

Registry assembly (turning ``TargetsConfig`` into ``TargetRegistry``) lives
in ``hostlens.targets.registry.build_registry_from_config`` so this module
stays free of concrete-target imports — that keeps ``Settings``-aware
construction off the loader's plate and lets the loader run safely from
``hostlens doctor`` even when no target can actually connect.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal

import structlog

# ``PyYAML`` ships no PEP 561 marker; ``types-PyYAML`` is a separate dist
# that the project does not currently depend on. Silence the stub
# complaint locally rather than polluting the global mypy config — keeps
# the boundary visible at the import site.
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hostlens.core.exceptions import ConfigError

__all__ = [
    "DockerEntry",
    "LocalEntry",
    "ReplayEntry",
    "SSHEntry",
    "TargetEntry",
    "TargetsConfig",
    "load_targets_config",
]


# Same regex enforced by ``LocalTarget.__init__`` / ``SSHTarget.__init__`` /
# ``TargetRegistry.register`` — see ``execution-target`` spec
# §需求:`ExecutionTarget` Protocol 必须定义完整接口 (regex enforcement point #1).
_NAME_PATTERN: str = r"^[a-z][a-z0-9_\-]{0,63}$"

# Placeholders are only allowed inside these two secret fields. Letting
# ``host`` / ``user`` come from ``${ENV}`` would silently broaden the
# attack surface (env-injected target metadata is hard to audit).
_PLACEHOLDER_ALLOWED_FIELDS: frozenset[str] = frozenset({"password", "passphrase"})

# Matches exactly ``${VAR_NAME}`` — full-string anchored. Any partial
# match (``prefix-${X}``) is rejected so users do not accidentally end up
# with half-expanded literals that look like a legitimate value.
_PLACEHOLDER_PATTERN: re.Pattern[str] = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


_logger = structlog.get_logger(__name__)


class _CommonEntryFields(BaseModel):
    """Fields shared by every ``TargetEntry`` variant.

    Kept on a base class so the SSH / local subclasses inherit the
    ``extra="forbid"`` policy + the name-regex constraint without
    duplicating the field declarations (which would risk drift between
    variants).
    """

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(pattern=_NAME_PATTERN)]
    enabled: bool = True
    display_name: str | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)


class LocalEntry(_CommonEntryFields):
    """``type: local`` target entry — no extra fields beyond the common set."""

    type: Literal["local"]


class SSHEntry(_CommonEntryFields):
    """``type: ssh`` target entry.

    SSH-specific field set is **exactly** the 7 fields below (spec
    §需求:`TargetsConfig` §场景:TargetEntry SSH 字段集严格 enforces this
    via ``extra="forbid"`` — adding e.g. ``agent_forwarding`` raises a
    ``ValidationError`` at load time).
    """

    type: Literal["ssh"]
    host: str
    user: str
    port: int = 22
    key_path: str | None = None
    password: str | None = None
    passphrase: str | None = None
    connect_timeout: int | None = None

    def __repr__(self) -> str:
        """Mask ``password`` / ``passphrase`` values in ``repr``.

        Pydantic's default ``__repr__`` echoes every field value, which
        would leak secrets into structlog renderings and into pytest
        failure output. We override to swap the secret fields for
        ``"***"`` while keeping every other field readable so debugging
        stays cheap. The underlying ``.password`` / ``.passphrase``
        attributes remain unredacted — only the representation lies.
        """

        def _val(name: str) -> str:
            value = getattr(self, name)
            if name in {"password", "passphrase"} and value is not None:
                return "'***'"
            return repr(value)

        fields = (
            "name",
            "type",
            "enabled",
            "display_name",
            "description",
            "tags",
            "host",
            "user",
            "port",
            "key_path",
            "password",
            "passphrase",
            "connect_timeout",
        )
        body = ", ".join(f"{name}={_val(name)}" for name in fields)
        return f"SSHEntry({body})"

    def __str__(self) -> str:
        """Force ``str(entry)`` through the masked ``__repr__``.

        Pydantic's default ``__str__`` formats every field as
        ``name='value'`` regardless of any ``__repr__`` override, which
        would re-leak ``password`` / ``passphrase``. Routing ``__str__``
        through the masked ``__repr__`` closes that gap so
        ``f"{entry}"`` / ``print(entry)`` / structlog's default value
        rendering all surface the scrubbed form.
        """

        return self.__repr__()


class ReplayEntry(_CommonEntryFields):
    """``type: replay`` target entry — drives a ``ReplayTarget`` (incident-pack).

    The discriminator value ``replay`` selects ``ReplayTarget`` during
    registry assembly. ``fixture`` points at the pre-recorded JSON fixture
    (see ``ReplayTarget`` / design D1). This config-layer ``type: replay`` is
    independent of the target's **runtime** ``.type`` (which impersonates the
    fixture's ``local`` / ``ssh``). ReplayTarget is read-only, so this entry
    has no secret fields and is not subject to the write-path EUID==0 guard.
    """

    type: Literal["replay"]
    fixture: str


class DockerEntry(_CommonEntryFields):
    """``type: docker`` target entry — drives a ``DockerTarget``.

    Docker-specific field set is **exactly** ``{container, docker_host}``
    (spec §场景:TargetEntry docker 字段集严格 enforces this via
    ``extra="forbid"`` — adding e.g. ``image`` raises a ``ValidationError``
    at load time).

    ``container`` is required and **non-empty** (``min_length=1``): an empty
    container reference must fail at yaml load rather than surface later as a
    runtime ``container_not_found``.

    ``docker_host`` is validated by the loader (see ``_validate_docker_host``),
    **not** ``Field(pattern=...)``: a bad endpoint must raise ``ConfigError``
    (structured ``kind``) rather than ``pydantic.ValidationError``.
    """

    type: Literal["docker"]
    container: Annotated[str, Field(min_length=1)]
    docker_host: str | None = None


# Discriminator on ``type`` so Pydantic routes ``type: local`` → LocalEntry,
# ``type: ssh`` → SSHEntry, ``type: replay`` → ReplayEntry, ``type: docker`` →
# DockerEntry without manual ``model_validate`` branches. Unknown ``type``
# values raise ``ValidationError`` automatically.
TargetEntry = Annotated[
    LocalEntry | SSHEntry | ReplayEntry | DockerEntry, Field(discriminator="type")
]


class TargetsConfig(BaseModel):
    """Top-level schema for ``~/.config/hostlens/targets.yaml``.

    ``version`` is fixed to the string ``"1"`` for M1; future incompatible
    schema changes bump this value so the loader can refuse to parse a
    newer file with an older Hostlens.
    """

    model_config = ConfigDict(extra="forbid")

    version: Literal["1"]
    targets: list[TargetEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Placeholder expansion
# ---------------------------------------------------------------------------


def _infer_target_name(path: tuple[Any, ...]) -> str:
    """Best-effort lookup of the ``name`` for the target at ``path``.

    The walker tracks the dotted location of the current string under
    inspection (``("targets", 0, "password")`` etc.). When raising a
    ``ConfigError`` we want to tell the user *which* target the bad
    placeholder belongs to — that means peeking at the sibling ``name``
    field of the target entry. ``path[1]`` is the index of the target in
    the ``targets:`` list, but at the point of failure we no longer have
    the original dict in scope; callers thread the surrounding target
    dict in via ``_current_target`` (kept on the stack frame below).
    """

    return _current_target.get("name") or "<unknown>"


# Module-level shim used only by the recursive walker below — mutating
# its single key during the walk lets us bubble the target name up to
# error sites without threading another argument through every recursion
# layer. Recursion is single-threaded (the loader is sync) so no locking
# is needed.
_current_target: dict[str, str | None] = {"name": None}


def _expand_placeholders(
    obj: Any,
    *,
    path: tuple[Any, ...] = (),
) -> Any:
    """Walk ``obj`` and replace ``${VAR}`` placeholders in allowed fields.

    Behaviour summary (matches spec §场景):

    - ``${VAR}`` in ``password`` / ``passphrase`` → looked up from
      ``os.environ``. Missing env var raises
      ``ConfigError(kind="missing_env_var", var_name=..., target=...)``.
    - ``${VAR}`` anywhere else (``host`` / ``user`` / ``port`` / etc.)
      raises ``ConfigError(kind="env_placeholder_not_allowed_here",
      field=..., target=...)``.
    - Strings without placeholders pass through unchanged.
    - Recursion preserves the ``path`` so error messages name the
      offending field.
    """

    if isinstance(obj, dict):
        # If we are stepping into one of the target dicts under
        # ``targets:`` track its ``name`` so descendants can report it.
        is_target_dict = len(path) == 2 and path[0] == "targets" and isinstance(path[1], int)
        if is_target_dict:
            previous_name = _current_target["name"]
            raw_name = obj.get("name")
            _current_target["name"] = raw_name if isinstance(raw_name, str) else None
            try:
                return {
                    key: _expand_placeholders(value, path=(*path, key))
                    for key, value in obj.items()
                }
            finally:
                _current_target["name"] = previous_name
        return {key: _expand_placeholders(value, path=(*path, key)) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_expand_placeholders(item, path=(*path, index)) for index, item in enumerate(obj)]
    if isinstance(obj, str):
        match = _PLACEHOLDER_PATTERN.fullmatch(obj)
        if match is None:
            return obj
        var_name = match.group(1)
        field = path[-1] if path else None
        if not isinstance(field, str) or field not in _PLACEHOLDER_ALLOWED_FIELDS:
            raise ConfigError(
                kind="env_placeholder_not_allowed_here",
                field=str(field) if field is not None else "<root>",
                target=_infer_target_name(path),
            )
        value = os.environ.get(var_name)
        if value is None:
            raise ConfigError(
                kind="missing_env_var",
                var_name=var_name,
                target=_infer_target_name(path),
            )
        return value
    return obj


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def load_targets_config(path: Path, *, expand_env: bool = True) -> TargetsConfig:
    """Load ``targets.yaml`` from ``path``, optionally expanding env-var placeholders.

    Behaviour per spec §需求:`TargetsConfig` 必须从 yaml 加载且环境变量占位展开:

    - File absent → return ``TargetsConfig(version="1", targets=[])`` and
      log DEBUG. **Not** an error; doctor surfaces a hint to run
      ``hostlens target add`` to bootstrap the config.
    - File present but empty → same as absent (treat empty file as
      "version=1, no targets").
    - ``${VAR}`` placeholders are expanded only in ``password`` /
      ``passphrase``. Misplaced placeholders / missing env vars raise
      ``ConfigError`` with a structured ``kind``.
    - Schema violations (unknown ``type``, ``extra`` field, bad
      ``name`` regex) raise ``pydantic.ValidationError`` so the original
      Pydantic location info is preserved for callers (CLI / doctor
      already render ``ValidationError`` nicely).

    ``expand_env=False`` mode (used by ``hostlens target add`` /
    ``remove``): skip the ``${VAR}`` expansion so the loader does NOT
    fail when an existing entry references a secret env var that is
    not currently set. Write commands round-trip the raw yaml via
    ``_load_raw_targets_dict`` anyway; they only need this loader to
    surface schema errors (e.g. a malformed entry already in the file)
    without forcing the operator to export every other entry's secret
    just to add a new one.
    """

    if not path.exists():
        # ``structlog`` is configured with ``PrintLoggerFactory`` which
        # writes to **stdout**; for CLI commands that emit machine-
        # readable JSON to stdout (``hostlens target list --json``,
        # ``hostlens doctor --json``) the INFO line would corrupt the
        # JSON document. Downgrade to ``debug`` so the hint stays
        # available for ad-hoc debugging without leaking onto stdout
        # during normal use. Callers that want to surface a "no
        # targets configured" hint to operators should do so through
        # their own user-facing rendering layer.
        _logger.debug(
            "config file not found, returning empty TargetsConfig",
            path=str(path),
        )
        return TargetsConfig(version="1", targets=[])

    raw_text = path.read_text()
    try:
        parsed = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError(
            "failed to parse targets.yaml",
            kind="yaml_parse_error",
            original=exc,
            path=str(path),
        ) from exc

    if parsed is None:
        # Empty file is allowed; treat as "no targets configured".
        return TargetsConfig(version="1", targets=[])
    if not isinstance(parsed, dict):
        raise ConfigError(
            "targets.yaml top-level must be a mapping",
            kind="invalid_top_level",
            path=str(path),
        )

    # Strip ``${VAR}`` placeholders from secret fields before
    # validation when ``expand_env=False`` so a missing env var does
    # not cascade into a schema failure on the write path. The
    # resulting TargetsConfig is for in-memory validation only,
    # never written back to disk.
    validated_input = _expand_placeholders(parsed) if expand_env else _strip_placeholders(parsed)

    try:
        config = TargetsConfig.model_validate(validated_input)
    except ValidationError:
        # Re-raise unchanged so callers see the Pydantic field locations.
        # The spec explicitly wants ``pydantic.ValidationError`` for
        # schema violations (§场景:unknown type raise / §场景:TargetEntry
        # name 不匹配正则 raise / §场景:TargetEntry SSH 字段集严格).
        raise

    # ``docker_host`` scheme validation runs **after** ``model_validate``
    # because it needs the typed ``DockerEntry`` and must raise
    # ``ConfigError`` (structured ``kind``) rather than ``ValidationError``
    # — so it cannot live on ``Field(pattern=...)``. Placeholder rejection
    # already happened in ``_expand_placeholders`` (before validation), so
    # ``docker_host: ${X}`` never reaches here.
    for entry in config.targets:
        if isinstance(entry, DockerEntry):
            _validate_docker_host(entry)
    return config


_DOCKER_HOST_UNIX_PREFIX: str = "unix:///"


def _validate_docker_host(entry: DockerEntry) -> None:
    """Reject any ``docker_host`` that is not a non-empty local unix socket.

    Acceptance is a narrow exception; rejection is the default catch-all
    (spec §场景:docker_host*). ``docker_host`` is accepted **iff** it
    ``startswith("unix:///")`` (case-sensitive — an absolute socket path,
    three slashes) **and** the socket path after ``unix:///`` is non-empty.

    Everything else raises
    ``ConfigError(kind="docker_host_remote_not_supported", ...)``:

    - remote schemes (``tcp://`` / ``ssh://`` / ``http(s)://`` / ``npipe://``),
    - bare paths without a scheme (``/var/run/docker.sock``),
    - empty socket path (``unix://`` / ``unix:///``),
    - case-mismatched schemes (``UNIX://x``),
    - relative socket paths (``unix://foo``).

    A literal local-socket endpoint (``unix:///var/run/docker.sock``) is the
    only accepted form — proving the validator is not "reject everything".
    """

    host = entry.docker_host
    if host is None:
        return
    if host.startswith(_DOCKER_HOST_UNIX_PREFIX) and host[len(_DOCKER_HOST_UNIX_PREFIX) :]:
        return
    raise ConfigError(
        kind="docker_host_remote_not_supported",
        field="docker_host",
        target=entry.name,
    )


def _strip_placeholders(
    obj: Any,
    *,
    path: tuple[Any, ...] = (),
) -> Any:
    """Walk ``obj`` recursively and replace ``${VAR}`` strings with ``None``
    for fields on the placeholder allowlist (``password`` / ``passphrase``).

    Used by write-path loads (``load_targets_config(expand_env=False)``)
    to make ``targets.yaml`` validate even when some referenced env vars
    aren't currently set. Placeholders in non-allowlisted fields raise
    ``ConfigError(kind="env_placeholder_not_allowed_here")`` — mirroring
    the read path so the write path cannot silently accept misconfiguration
    that the read path rejects.
    """

    if isinstance(obj, dict):
        is_target_dict = len(path) == 2 and path[0] == "targets" and isinstance(path[1], int)
        if is_target_dict:
            previous_name = _current_target["name"]
            raw_name = obj.get("name")
            _current_target["name"] = raw_name if isinstance(raw_name, str) else None
            try:
                return {
                    key: _strip_placeholders(value, path=(*path, key)) for key, value in obj.items()
                }
            finally:
                _current_target["name"] = previous_name
        return {key: _strip_placeholders(value, path=(*path, key)) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_strip_placeholders(item, path=(*path, index)) for index, item in enumerate(obj)]
    if isinstance(obj, str) and _PLACEHOLDER_PATTERN.fullmatch(obj):
        field = path[-1] if path else None
        if not isinstance(field, str) or field not in _PLACEHOLDER_ALLOWED_FIELDS:
            raise ConfigError(
                kind="env_placeholder_not_allowed_here",
                field=str(field) if field is not None else "<root>",
                target=_infer_target_name(path),
            )
        return None
    return obj
