"""``yaml`` inventory source — parses the Hostlens standard inventory schema.

Spec: ``inventory-source/spec.md`` §需求:`yaml` source.

This source parses **only** the Hostlens standard inventory schema (defined
by the spec), NOT arbitrary third-party YAML. tizi's ``inventory.yml`` (with
``tailscale_ipv4`` etc. non-standard field names) is out of scope and is
handled via ``--source ssh_config`` against ``~/tizi/hosts`` instead.

Schema:

- Optional top-level ``defaults`` (dict) — merged into each host entry
  **after filtering to that entry's type's allowed field set** (entry's own
  fields win). Filtering is mandatory: ``defaults: {user: root}`` must be
  skipped for ``local`` entries (``LocalEntry`` has no ``user``), else the
  whole batch of local entries would fail promotion.
- Every other top-level key is a **group key** (arbitrary name); its value
  maps host-identifier → host-entry dict.
- Each host entry: ``type`` is ``Literal["local", "ssh"]`` (other values →
  ``ConfigError``); ``ssh`` requires ``host``; ``local`` allows only
  ``type``. Credential refs are ``password_env`` / ``passphrase_env`` /
  ``key_path``; the ``*_env`` values must match ``^[A-Z_][A-Z0-9_]*$``.
- A plaintext ``password`` / ``passphrase`` field is rejected fail-closed.
"""

from __future__ import annotations

import os
from typing import Any

import yaml

from hostlens.core.exceptions import ConfigError
from hostlens.targets.inventory.models import (
    _ENV_VAR_NAME_PATTERN,
    CandidateTarget,
    normalize_target_name,
    reject_normalized_name_collisions,
    resolve_key_path,
)

__all__ = ["YamlSource"]

# Field names that, if present in a source entry, are a plaintext secret and
# must trigger fail-closed rejection (spec §需求:来源含明文密钥必须 fail-closed).
_PLAINTEXT_SECRET_FIELDS: frozenset[str] = frozenset({"password", "passphrase"})

# Allowed field set per target type (excluding ``type`` itself). Used both
# to filter ``defaults`` and to validate that an entry carries no unknown
# field. Mirrors ``LocalEntry`` / ``SSHEntry`` connection-relevant fields
# the source layer produces (``CandidateTarget`` is the promotion target).
_LOCAL_FIELDS: frozenset[str] = frozenset()
_SSH_FIELDS: frozenset[str] = frozenset(
    {"host", "user", "port", "password_env", "passphrase_env", "key_path"}
)
_ALLOWED_FIELDS: dict[str, frozenset[str]] = {
    "local": _LOCAL_FIELDS,
    "ssh": _SSH_FIELDS,
}


class YamlSource:
    """Parses a Hostlens standard inventory YAML into ``CandidateTarget`` list."""

    name = "yaml"

    def can_handle(self, ref: str) -> bool:
        """Match ``.yml`` / ``.yaml`` extension with a top-level mapping."""

        lowered = ref.lower()
        if not (lowered.endswith(".yml") or lowered.endswith(".yaml")):
            return False
        try:
            with open(os.path.expanduser(ref), encoding="utf-8") as handle:
                parsed = yaml.safe_load(handle.read())
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            return False
        return isinstance(parsed, dict)

    def parse(self, ref: str) -> list[CandidateTarget]:
        text = self._read_ref(ref)
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ConfigError(
                "failed to parse yaml inventory",
                kind="yaml_parse_error",
                original=exc,
            ) from exc

        if parsed is None:
            return []
        if not isinstance(parsed, dict):
            raise ConfigError(
                "yaml inventory top-level must be a mapping",
                kind="invalid_top_level",
            )

        raw_defaults = parsed.get("defaults", {})
        if not isinstance(raw_defaults, dict):
            raise ConfigError(
                "yaml inventory 'defaults' must be a mapping",
                kind="invalid_defaults",
            )

        candidates: list[CandidateTarget] = []
        for group_key, group in parsed.items():
            if group_key == "defaults":
                continue
            if not isinstance(group, dict):
                raise ConfigError(
                    "yaml inventory group must map host identifiers to entries",
                    kind="invalid_group",
                    group=str(group_key),
                )
            for raw_identifier, entry in group.items():
                if not isinstance(entry, dict):
                    raise ConfigError(
                        "yaml inventory host entry must be a mapping",
                        kind="invalid_entry",
                        entry=str(raw_identifier),
                    )
                candidates.append(self._build_candidate(str(raw_identifier), entry, raw_defaults))
        reject_normalized_name_collisions(candidates)
        return candidates

    # -- internal -----------------------------------------------------------

    @staticmethod
    def _read_ref(ref: str) -> str:
        path = os.path.expanduser(ref)
        try:
            with open(path, encoding="utf-8") as handle:
                return handle.read()
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(
                "failed to read yaml inventory source (not readable / not UTF-8)",
                kind="yaml_read_error",
                path=path,
                original=exc,
            ) from exc

    def _build_candidate(
        self,
        raw_identifier: str,
        entry: dict[Any, Any],
        defaults: dict[Any, Any],
    ) -> CandidateTarget:
        # Reject plaintext secrets by field NAME (not by sniffing values),
        # before reading any value — the plaintext must never enter the
        # model / a log / the ConfigError. ``field`` carries only the field
        # name (``"password"``), never the value.
        for secret_field in _PLAINTEXT_SECRET_FIELDS:
            if secret_field in entry or secret_field in defaults:
                raise ConfigError(
                    "plaintext secret field not allowed; use a *_env reference",
                    kind="plaintext_secret_forbidden",
                    field=secret_field,
                )

        target_type = entry.get("type")
        if target_type not in ("local", "ssh"):
            raise ConfigError(
                "yaml inventory entry 'type' must be 'local' or 'ssh'",
                kind="invalid_target_type",
                entry=raw_identifier,
                got=str(target_type),
            )

        allowed = _ALLOWED_FIELDS[target_type]
        # Merge type-filtered defaults under the entry's explicit fields.
        merged: dict[str, Any] = {key: value for key, value in defaults.items() if key in allowed}
        for key, value in entry.items():
            if key == "type":
                continue
            merged[key] = value

        unknown = set(merged) - allowed
        if unknown:
            raise ConfigError(
                "yaml inventory entry has unsupported field(s)",
                kind="invalid_entry_field",
                entry=raw_identifier,
                field=",".join(sorted(unknown)),
            )

        name = normalize_target_name(raw_identifier)
        metadata = {"source": "yaml", "raw_identifier": raw_identifier}

        if target_type == "local":
            return CandidateTarget(name=name, type="local", source_metadata=metadata)

        host = merged.get("host")
        if not host:
            raise ConfigError(
                "ssh inventory entry missing required field 'host'; "
                "if this is an OpenSSH config use --source ssh_config",
                kind="missing_required_field",
                entry=raw_identifier,
                field="host",
            )

        for env_field in ("password_env", "passphrase_env"):
            env_value = merged.get(env_field)
            if env_value is not None and _ENV_VAR_NAME_PATTERN.fullmatch(str(env_value)) is None:
                raise ConfigError(
                    "credential env reference is not a valid env var name",
                    kind="invalid_env_var_name",
                    entry=raw_identifier,
                    field=env_field,
                )

        port_value = merged.get("port")
        port: int | None
        if port_value is not None:
            # ``bool`` is an ``int`` subclass — ``port: true`` would otherwise
            # silently become 1; reject it (and any non-numeric) explicitly.
            if isinstance(port_value, bool):
                raise ConfigError(
                    "yaml inventory 'port' must be an integer",
                    kind="invalid_entry",
                    entry=raw_identifier,
                )
            try:
                port = int(port_value)
            except (ValueError, TypeError) as exc:
                raise ConfigError(
                    "yaml inventory 'port' must be an integer",
                    kind="invalid_entry",
                    entry=raw_identifier,
                ) from exc
            if not 1 <= port <= 65535:
                raise ConfigError(
                    "yaml inventory 'port' must be in 1..65535",
                    kind="invalid_entry",
                    entry=raw_identifier,
                )
        else:
            port = None
        return CandidateTarget(
            name=name,
            type="ssh",
            host=str(host),
            user=str(merged["user"]) if merged.get("user") is not None else None,
            port=port,
            password_env=merged.get("password_env"),
            passphrase_env=merged.get("passphrase_env"),
            key_path=(
                resolve_key_path(str(merged["key_path"]))
                if merged.get("key_path") is not None
                else None
            ),
            source_metadata=metadata,
        )
