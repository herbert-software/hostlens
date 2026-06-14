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

import contextlib
import os
import re
import tempfile
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
    "K8sEntry",
    "LocalEntry",
    "ReplayEntry",
    "SSHEntry",
    "TargetEntry",
    "TargetsConfig",
    "load_targets_config",
    "save_targets_config",
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


class K8sEntry(_CommonEntryFields):
    """``type: k8s`` target entry — drives a ``KubernetesTarget``.

    K8s-specific field set is **exactly** ``{pod, namespace, container,
    kubeconfig, context}`` (spec §场景:TargetEntry k8s 字段集严格 enforces this
    via ``extra="forbid"`` — adding e.g. ``image`` raises a ``ValidationError``
    at load time).

    ``pod`` is required and **non-empty** (``min_length=1``): an empty pod
    reference must fail at yaml load rather than surface later as a runtime
    ``pod_not_found``.

    ``container`` semantics differ from ``DockerEntry.container``: here it is an
    optional selector inside a multi-container pod (``None`` → the pod's default
    container, i.e. k8s exec API ``container=None``), whereas
    ``DockerEntry.container`` is a required container reference.

    No secret fields: credentials live in the kubeconfig file content / the
    in-cluster ServiceAccount token, never in yaml. ``kubeconfig`` / ``context``
    are path / name references (the path itself is not a secret). All five
    fields are non-secret, so a ``${VAR}`` placeholder in any of them is
    rejected by the existing placeholder walker (field-name allowlist is
    ``{password, passphrase}``) as ``env_placeholder_not_allowed_here`` — no
    K8sEntry-specific logic needed.
    """

    type: Literal["k8s"]
    pod: Annotated[str, Field(min_length=1)]
    namespace: str = "default"
    container: str | None = None
    kubeconfig: str | None = None
    context: str | None = None


# Discriminator on ``type`` so Pydantic routes ``type: local`` → LocalEntry,
# ``type: ssh`` → SSHEntry, ``type: replay`` → ReplayEntry, ``type: docker`` →
# DockerEntry, ``type: k8s`` → K8sEntry without manual ``model_validate``
# branches. Unknown ``type`` values raise ``ValidationError`` automatically.
TargetEntry = Annotated[
    LocalEntry | SSHEntry | ReplayEntry | DockerEntry | K8sEntry,
    Field(discriminator="type"),
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


# ---------------------------------------------------------------------------
# Persistence: raw round-trip serialisation + atomic write
# ---------------------------------------------------------------------------
#
# These two serialisers live in the loader module (not ``cli/target.py``)
# because ``save_targets_config`` reuses them: a reverse import from
# ``config`` to ``cli`` would create a ``config↔cli`` import cycle and break
# this module's "free of concrete-target imports / safe to import from
# doctor" isolation. They depend only on ``LocalEntry`` / ``SSHEntry`` +
# yaml/Path, so living here does not violate that isolation.


def _load_raw_targets_dict(cfg_path: Path, *, fallback_version: str = "1") -> dict[str, Any]:
    """Return the raw ``yaml.safe_load`` dict for the targets config.

    Critical to credential safety: this path does NOT run
    ``${VAR}`` placeholder expansion (that lives in
    ``load_targets_config``). When ``hostlens target add`` / ``remove`` /
    ``import`` round-trips the file, we MUST keep the placeholder strings
    intact on entries other than the one being written — otherwise the
    loader's eager expansion would surface real secret values, and writing
    the config back would persist them in plaintext to disk.

    Missing or empty files default to a minimal skeleton
    ``{"version": fallback_version, "targets": []}`` so callers can
    treat the result as always-mutable.
    """

    if not cfg_path.exists():
        return {"version": fallback_version, "targets": []}
    text = cfg_path.read_text() or ""
    parsed = yaml.safe_load(text)
    if not isinstance(parsed, dict):
        return {"version": fallback_version, "targets": []}
    parsed.setdefault("version", fallback_version)
    parsed.setdefault("targets", [])
    return parsed


def _entry_to_dict(
    entry: LocalEntry | SSHEntry, *, password_env: str | None, passphrase_env: str | None
) -> dict[str, Any]:
    """Serialise an entry back into the yaml representation.

    Secret fields (``password`` / ``passphrase``) are written as
    ``${VAR}`` placeholders when the corresponding env-var name is passed
    via ``password_env`` / ``passphrase_env`` — matches the spec scenario
    `target add 凭据参数命名一致` and avoids ever writing literal passwords to
    disk. The env names are independent parameters (never read from
    ``entry.password``, which may already hold the expanded plaintext).
    """

    common: dict[str, Any] = {
        "name": entry.name,
        "type": entry.type,
    }
    # ``enabled`` defaults to True; we still write it explicitly so the
    # yaml stays self-describing for operators reading the file.
    if entry.enabled is False:
        common["enabled"] = False
    if entry.display_name is not None:
        common["display_name"] = entry.display_name
    if entry.description is not None:
        common["description"] = entry.description
    if entry.tags:
        common["tags"] = list(entry.tags)

    if isinstance(entry, SSHEntry):
        common["host"] = entry.host
        common["user"] = entry.user
        if entry.port != 22:
            common["port"] = entry.port
        if entry.key_path is not None:
            common["key_path"] = entry.key_path
        if password_env is not None:
            common["password"] = "${" + password_env + "}"
        if passphrase_env is not None:
            common["passphrase"] = "${" + passphrase_env + "}"
        if entry.connect_timeout is not None:
            common["connect_timeout"] = entry.connect_timeout
    return common


def _atomic_write_yaml(path: Path, raw_dict: dict[str, Any]) -> None:
    """Atomically write ``raw_dict`` as yaml to ``path`` with ``0o600`` perms.

    ``targets.yaml`` records host / user / key_path — a lateral-movement
    map — so it must never be world-readable, and a half-written file would
    leave the registry unloadable. The write is therefore atomic and
    permission-tightened:

    - The parent dir is created ``0o700`` if absent, else tightened to
      ``0o700`` (an existing ``0o755`` config dir is narrowed every write,
      so the secret-dir guarantee survives a pre-existing loose dir).
    - A temp file is created with ``mkstemp(dir=<same dir>)`` (same
      filesystem so ``os.replace`` is atomic; unpredictable name defeats
      symlink / predictable-path attacks).
    - ``os.fchmod(fd, 0o600)`` is set explicitly before the rename — not
      relying on the ``mkstemp`` default — so the contract is testable.
    - ``os.replace`` swaps the temp file into place; an interruption leaves
      either the old file or the new file, never a truncated one.

    Filesystem failures (parent not writable, ``mkstemp`` ``OSError``) are
    wrapped in ``ConfigError(kind="targets_config_write_failed")`` so
    callers map them to a structured exit code rather than a bare
    ``OSError`` traceback.
    """

    parent = path.parent
    try:
        if not parent.exists():
            os.makedirs(parent, mode=0o700)
        else:
            os.chmod(parent, 0o700)
    except OSError as exc:
        raise ConfigError(
            "failed to prepare targets config directory",
            kind="targets_config_write_failed",
            original=exc,
            path=str(parent),
        ) from exc

    payload = yaml.safe_dump(raw_dict, sort_keys=False)
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=".targets-", suffix=".yaml.tmp")
    except OSError as exc:
        raise ConfigError(
            "failed to create temporary targets config file",
            kind="targets_config_write_failed",
            original=exc,
            path=str(parent),
        ) from exc

    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
        os.replace(tmp_name, path)
    except OSError as exc:
        # ``os.replace`` failed (or the write did) — drop the temp file so
        # we never leave a half-written ``.targets-*.tmp`` behind, then
        # surface a structured error.
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise ConfigError(
            "failed to write targets config",
            kind="targets_config_write_failed",
            original=exc,
            path=str(path),
        ) from exc


def save_targets_config(
    path: Path,
    entries: list[tuple[LocalEntry | SSHEntry, str | None, str | None]],
) -> None:
    """Idempotently upsert ``entries`` into ``targets.yaml`` (atomic, ``0o600``).

    The inverse of ``load_targets_config`` for the write side, sharing the
    ``_PLACEHOLDER_ALLOWED_FIELDS`` placeholder discipline and the same
    ``targets.yaml`` file (hence it owns the ``TargetsConfig`` persistence
    contract).

    Each element is ``(entry, password_env, passphrase_env)``: the
    credential env names are threaded **separately** so ``_entry_to_dict``
    re-derives the ``${VAR}`` placeholder from the env name (never from
    ``entry.password``, which may hold the expanded plaintext). cred-less /
    key_path entries pass ``None`` for both.

    Behaviour:

    - **Pre-validate** the existing file with
      ``load_targets_config(expand_env=False)`` so a corrupt / misplaced
      placeholder file raises ``ConfigError`` *before* any write (callers
      map to exit 2) rather than silently round-tripping bad data.
    - **Idempotent upsert** by ``name``: an entry whose name already exists
      is skipped (not appended, not overwritten) so re-runs are safe.
    - **``${VAR}`` preservation**: existing entries are round-tripped via
      ``_load_raw_targets_dict`` (no expansion) so their placeholders are
      written back verbatim, never flattened into plaintext secrets.
    - **Atomic ``0o600`` write** via ``_atomic_write_yaml``.
    """

    # Pre-validate the on-disk file. ``expand_env=False`` so unrelated
    # entries referencing currently-unset env vars do not block the write,
    # while a genuinely corrupt file / misplaced placeholder still raises.
    existing = load_targets_config(path, expand_env=False)
    existing_names = {entry.name for entry in existing.targets}

    raw = _load_raw_targets_dict(path, fallback_version=existing.version)
    raw.setdefault("targets", [])

    seen = set(existing_names)
    for entry, password_env, passphrase_env in entries:
        if entry.name in seen:
            continue
        seen.add(entry.name)
        raw["targets"].append(
            _entry_to_dict(entry, password_env=password_env, passphrase_env=passphrase_env)
        )

    _atomic_write_yaml(path, raw)
