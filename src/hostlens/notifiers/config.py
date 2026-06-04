"""``notifiers.yaml`` loader — parse, ``${ENV_VAR}`` injection, channel build.

Spec: ``openspec/changes/add-notifier-channels/specs/notify-routing/spec.md``
(§需求:通道配置必须从 `notifiers.yaml` 加载并解析 `${ENV_VAR}`). Design D-1 / D-3.

The loader turns ``~/.config/hostlens/notifiers.yaml`` (path from
``Settings.notifiers_config_path``) into a ``{instance_name: Notifier}``
map ready for the scheduler. It is fail-loud throughout:

- every ``channels.<name>`` entry MUST carry a ``type`` that resolves to a
  registered ``ChannelTypeRegistry`` entry (unknown ``type`` → raise);
- ``${ENV_VAR}`` placeholders in field values are resolved at **load time**
  from ``os.environ``; a reference to an *unset* variable raises (naming the
  variable) rather than silently resolving to ``""``;
- after construction each channel's ``validate_config`` MUST pass — required
  fields must be **present and non-empty** (an empty string counts as
  missing).

Secrets (token / webhook / sign secret) are expected to arrive via
``${ENV_VAR}`` injection and are never written back to disk or into any Run
record by this module.

Channel construction contract (resolved here because the ``Notifier``
Protocol fixes only ``validate_config`` / ``render`` / ``send`` and leaves
the constructor open): the registered class is instantiated as
``cls(instance_name=<name>, config=<resolved_cfg>)``, where ``config`` is
the entry dict with ``type`` removed and ``${ENV_VAR}`` already expanded.
The built-in adapters (group C) satisfy this keyword-only constructor.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from hostlens.core.exceptions import ConfigError

if TYPE_CHECKING:
    from hostlens.core.config import Settings
    from hostlens.notifiers.base import ChannelTypeRegistry, Notifier

__all__ = ["load_channels"]


# Matches a single ``${...}`` placeholder. The body is captured up to the
# **first** closing brace, so ``${${A}}`` matches the *outer* ``${${A}`` (body
# ``${A``), which is then looked up as an env var literally named ``${A`` and —
# being unset — fails loud, rather than nest-expanding. The result of an
# expansion is never re-scanned (single-layer, per spec). A bare ``$`` or a
# malformed ``${X`` (no closing brace) does not match and is kept literally.
_PLACEHOLDER_PATTERN: re.Pattern[str] = re.compile(r"\$\{([^}]*)\}")


def _expand_value(value: str, *, channel: str, field: str) -> str:
    """Resolve every ``${ENV_VAR}`` placeholder in ``value`` (single layer).

    Behaviour (spec §需求:通道配置必须从 `notifiers.yaml` 加载并解析
    `${ENV_VAR}`):

    - ``${VAR}`` → ``os.environ[VAR]``; an unset variable raises
      ``ConfigError`` naming the variable (never resolved to ``""``).
    - ``${}`` (empty variable name) raises ``ConfigError`` — it is illegal,
      not kept literally and not looked up as ``os.environ[""]``.
    - a bare ``$`` or a malformed ``${X`` (missing ``}``) does not match the
      placeholder pattern and is preserved verbatim.
    - expansion is **single-layer**: the substituted value is not re-scanned
      for further ``${...}`` (``${${A}}`` does not nest-expand).

    The substitution is applied in one ``re.sub`` pass, so injected content
    that happens to contain ``${...}`` is left untouched.
    """

    def _replace(match: re.Match[str]) -> str:
        var_name = match.group(1)
        if var_name == "":
            raise ConfigError(
                kind="empty_env_var_name",
                channel=channel,
                field=field,
            )
        resolved = os.environ.get(var_name)
        if resolved is None:
            raise ConfigError(
                kind="missing_env_var",
                var_name=var_name,
                channel=channel,
                field=field,
            )
        return resolved

    return _PLACEHOLDER_PATTERN.sub(_replace, value)


def _expand_entry(channel: str, entry: dict[str, object]) -> dict[str, object]:
    """Return ``entry`` with ``${ENV_VAR}`` expanded in every string value.

    Only top-level string values are expanded (channel configs are flat
    key→scalar maps). Non-string values pass through unchanged.
    """

    expanded: dict[str, object] = {}
    for key, value in entry.items():
        if isinstance(value, str):
            expanded[key] = _expand_value(value, channel=channel, field=key)
        else:
            expanded[key] = value
    return expanded


def _parse_yaml(path: Path) -> dict[str, object]:
    """Read + parse ``notifiers.yaml``; return the ``channels`` mapping.

    An absent or empty file yields an empty channel map (no channels
    configured is a valid state, not an error). A non-mapping top level, a
    non-mapping ``channels``, or unparsable YAML all raise ``ConfigError``.
    """

    if not path.exists():
        return {}

    try:
        raw_text = path.read_text()
    except OSError as exc:
        raise ConfigError(
            "failed to read notifiers.yaml",
            kind="notifiers_yaml_unreadable",
            original=exc,
            path=str(path),
        ) from exc
    try:
        parsed = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError(
            "failed to parse notifiers.yaml",
            kind="yaml_parse_error",
            original=exc,
            path=str(path),
        ) from exc

    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ConfigError(
            "notifiers.yaml top-level must be a mapping",
            kind="invalid_top_level",
            path=str(path),
        )

    channels = parsed.get("channels", {})
    if channels is None:
        return {}
    if not isinstance(channels, dict):
        raise ConfigError(
            "notifiers.yaml `channels` must be a mapping",
            kind="invalid_channels",
            path=str(path),
        )
    return channels


def load_channels(
    settings: Settings,
    registry: ChannelTypeRegistry,
) -> dict[str, Notifier]:
    """Load + assemble every channel from ``settings.notifiers_config_path``.

    For each ``channels.<name>`` entry:

    1. require a ``type`` string and resolve it to a class via ``registry``
       (an unknown ``type`` raises ``ConfigError`` naming it);
    2. expand ``${ENV_VAR}`` placeholders across the entry's string values
       (unset variable / empty ``${}`` → fail-loud);
    3. construct the channel as ``cls(instance_name=<name>, config=<resolved>)``;
    4. call ``validate_config(<resolved>)`` so required fields are present
       **and** non-empty before the channel is handed to the scheduler.

    Returns the ``{instance_name: Notifier}`` map. An absent / empty config
    file yields an empty map. Raises ``ConfigError`` on any malformed entry,
    unknown type, missing env var, or failed ``validate_config`` so a broken
    channel never silently disappears.
    """

    channels = _parse_yaml(settings.notifiers_config_path)
    built: dict[str, Notifier] = {}

    for name, entry in channels.items():
        if not isinstance(entry, dict):
            raise ConfigError(
                "channel entry must be a mapping",
                kind="invalid_channel_entry",
                channel=str(name),
            )

        channel_type = entry.get("type")
        if not isinstance(channel_type, str) or channel_type == "":
            raise ConfigError(
                "channel entry missing `type`",
                kind="missing_channel_type",
                channel=str(name),
            )

        try:
            notifier_cls = registry.get(channel_type)
        except KeyError as exc:
            raise ConfigError(
                "unknown channel type",
                kind="unknown_channel_type",
                channel=str(name),
                channel_type=channel_type,
            ) from exc

        resolved = _expand_entry(str(name), entry)
        config = {key: value for key, value in resolved.items() if key != "type"}

        try:
            notifier = notifier_cls(instance_name=str(name), config=config)  # type: ignore[call-arg]
            notifier.validate_config(config)
        except (ValueError, TypeError) as exc:
            # Adapters fail-loud with ``ValueError`` on a missing / empty /
            # mistyped required field. All callers (schedule / doctor / notify)
            # only catch ``ConfigError``, so convert here — otherwise a bare
            # missing field crashes them with a raw traceback. ``validate_config``
            # messages reference field *names*, never secret values.
            raise ConfigError(
                "channel config is invalid",
                kind="invalid_channel_config",
                channel=str(name),
                channel_type=channel_type,
                reason=str(exc),
            ) from exc
        built[str(name)] = notifier

    return built
