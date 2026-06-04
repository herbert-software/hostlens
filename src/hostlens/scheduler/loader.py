"""Schedule manifest loader — scans ``schedules/*.yaml``, fail-loud validation.

Spec: ``openspec/changes/add-scheduler/specs/schedule-manifest/spec.md``
§需求:加载器必须扫描 `schedules/*.yaml` 并在加载时 fail-loud 校验.

`load_schedules` is the single entry point. It scans a directory for
``*.yaml`` files, parses each into a `ScheduleManifest` (field-level checks
live on the model), then runs the **load-time semantic checks** that the
schema cannot express alone:

  (a) every ``targets`` member is registered in the injected
      `TargetRegistry`;
  (b) ``targets`` has **exactly one** member (M4 single-target; multi-target
      fan-out is a non-goal — design D-2 / spec §需求:M4 每个 manifest 必须
      恰好一个 target);
  (c) ``name`` is globally unique across all files;
  (d) ``intent`` is non-blank.

Any invalid manifest is **fail-loud**: a `ConfigError` is raised whose
message carries the offending **file name + field + reason**. The loader
never silently skips a file and never defers validation to fire time — per
design D-6 the `schedule list` / `daemon` / `trigger` entry points trigger
this load so an invalid manifest stops them before any real scheduling.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from pydantic import ValidationError

from hostlens.core.exceptions import ConfigError
from hostlens.scheduler.schema import ScheduleManifest

if TYPE_CHECKING:
    from hostlens.targets.registry import TargetRegistry

__all__ = ["load_schedules"]


def load_schedules(
    schedules_dir: Path,
    target_registry: TargetRegistry,
) -> list[ScheduleManifest]:
    """Load + validate every ``*.yaml`` under ``schedules_dir``.

    ``target_registry`` is **injected** (not pulled from a module-level
    singleton) so test fixtures can drive validation with a custom / empty
    registry. A missing or empty directory yields an empty list (no error —
    "no schedules configured" is a valid state).

    Raises ``ConfigError`` (with ``kind`` + ``file`` + ``field`` + reason)
    on the first invalid manifest. Files are processed in sorted order so
    the error surface is deterministic.
    """

    if not schedules_dir.is_dir():
        return []

    manifests: list[ScheduleManifest] = []
    seen_names: dict[str, str] = {}

    for path in sorted(schedules_dir.glob("*.yaml")):
        manifest = _load_one(path)

        # (d) intent non-blank — schema enforces min_length=1, but a
        # whitespace-only intent passes that and is semantically empty.
        if not manifest.intent.strip():
            raise ConfigError(
                "intent must be non-blank",
                kind="schedule_intent_blank",
                file=path.name,
                field="intent",
            )

        # (b) M4 single-target. The schema allows list[str] (>=1) to keep
        # the field shape forward-compatible with fan-out; the loader is the
        # M4 gate that rejects >=2.
        if len(manifest.targets) != 1:
            raise ConfigError(
                "M4 only supports a single target; multi-target fan-out is not implemented",
                kind="schedule_multi_target_unsupported",
                file=path.name,
                field="targets",
                count=len(manifest.targets),
            )

        # (a) every target must be registered.
        registered = target_registry.names()
        for target_name in manifest.targets:
            if target_name not in registered:
                raise ConfigError(
                    "target is not registered in the TargetRegistry",
                    kind="schedule_target_not_registered",
                    file=path.name,
                    field="targets",
                    target=target_name,
                )

        # (c) name globally unique across files.
        if manifest.name in seen_names:
            raise ConfigError(
                "duplicate schedule name across files",
                kind="schedule_duplicate_name",
                file=path.name,
                field="name",
                name=manifest.name,
                first_seen_in=seen_names[manifest.name],
            )
        seen_names[manifest.name] = path.name

        manifests.append(manifest)

    return manifests


def _load_one(path: Path) -> ScheduleManifest:
    """Parse a single manifest file into a `ScheduleManifest` (fail-loud)."""

    raw = path.read_text()
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(
            "manifest YAML parse error",
            kind="schedule_manifest_parse_error",
            file=path.name,
            original=exc,
        ) from exc

    if not isinstance(data, dict):
        raise ConfigError(
            f"manifest root must be a YAML mapping, got {type(data).__name__}",
            kind="schedule_manifest_not_object",
            file=path.name,
        )

    try:
        return ScheduleManifest.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(
            "manifest schema validation failed",
            kind="schedule_manifest_validation_error",
            file=path.name,
            original=exc,
        ) from exc
