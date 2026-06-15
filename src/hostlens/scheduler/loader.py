"""Schedule manifest loader — scans ``schedules/*.yaml``, fail-loud validation.

Spec: ``openspec/changes/add-scheduler/specs/schedule-manifest/spec.md``
§需求:加载器必须扫描 `schedules/*.yaml` 并在加载时 fail-loud 校验.

`load_schedules` is the single entry point. It scans a directory for
``*.yaml`` files, parses each into a `ScheduleManifest` (field-level checks
live on the model), then runs the **load-time semantic checks** that the
schema cannot express alone:

  (a) every ``targets`` member is registered in the injected
      `TargetRegistry`;
  (b) ``targets`` cardinality is checked **per ``mode``** — ``mode == "agent"``
      requires **exactly one** member (agent reuses single-target
      `run_diagnosis_pipeline`; multi-target fan-out stays a non-goal for
      agent), while ``mode == "deterministic"`` allows **``>=1``** member
      (deterministic runs the fixed inspector set per target and assembles one
      fleet report — multi-target is its core use). The schema's
      ``min_length=1`` already rejects an empty list in both modes (spec
      §需求:manifest 的 target 基数必须按 mode 决定);
  (c) ``name`` is globally unique across all files;
  (d) ``intent`` is non-blank;
  (e) every ``notify[].only_if`` (when present) is a syntactically valid DSL
      expression (`routing.validate_only_if` → `inspectors.dsl.validate_ast`).
      This is the **load-time** half of the M5 two-stage check: it does NOT
      read ``notifiers.yaml`` or verify the ``channel`` exists, so
      ``schedule list`` never depends on channel configuration; channel
      existence is validated at assembly time by the runner (design D-7).

Any invalid manifest is **fail-loud**: a `ConfigError` is raised whose
message carries the offending **file name + field + reason**. The loader
never silently skips a file and never defers validation to fire time — per
design D-6 the `schedule list` / `daemon` / `trigger` entry points trigger
this load so an invalid manifest stops them before any real scheduling.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import jsonschema
import simpleeval
import yaml
from pydantic import ValidationError

from hostlens.core.exceptions import ConfigError, InspectorError
from hostlens.inspectors.health import resolve_inspector_set
from hostlens.inspectors.runner import coerce_and_validate_parameters
from hostlens.notifiers.routing import validate_only_if
from hostlens.scheduler.schema import ScheduleManifest

if TYPE_CHECKING:
    from hostlens.inspectors.registry import InspectorRegistry
    from hostlens.targets.registry import TargetRegistry

__all__ = ["load_schedules"]


def load_schedules(
    schedules_dir: Path,
    target_registry: TargetRegistry,
    inspector_registry: InspectorRegistry,
) -> list[ScheduleManifest]:
    """Load + validate every ``*.yaml`` under ``schedules_dir``.

    ``target_registry`` / ``inspector_registry`` are **injected** (not pulled
    from module-level singletons) so test fixtures can drive validation with a
    custom / empty registry. A missing or empty directory yields an empty list
    (no error — "no schedules configured" is a valid state). The inspector
    registry mirrors the target registry: ``targets`` membership is checked
    against the target registry, ``inspector_parameters`` against the inspector
    registry (parameter keys must name a registered, parameterised inspector
    inside the deterministic run set).

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

        # (b) per-mode target cardinality. The schema's min_length=1 already
        # rejects an empty list; the loader is the gate that rejects an
        # agent-mode manifest with >=2 targets. deterministic mode runs the
        # fixed set per target and assembles one fleet report, so >=1 is valid.
        if manifest.mode == "agent" and len(manifest.targets) != 1:
            raise ConfigError(
                "agent mode supports only a single target; "
                "multi-target fan-out is a non-goal for agent "
                "(use mode: deterministic for fleet inspection)",
                kind="schedule_multi_target_unsupported",
                file=path.name,
                field="targets",
                count=len(manifest.targets),
            )

        # (b') deterministic mode: an explicit empty `inspectors: []` (distinct
        # from `inspectors: null`, which means "use the default health set")
        # resolves to "run nothing" → every fire would produce a failed Run.
        # Reject it at load so a typo'd empty list fails loud here instead of
        # silently failing every scheduled fire.
        if manifest.mode == "deterministic" and manifest.inspectors == []:
            raise ConfigError(
                "deterministic mode with an explicit empty inspectors list runs "
                "nothing; omit inspectors to use the default health set, or list "
                "at least one inspector",
                kind="schedule_deterministic_empty_inspectors",
                file=path.name,
                field="inspectors",
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

        # (e) every notify only_if (when present) is syntactically valid. This
        # is the load-time half only: channel existence is checked at assembly
        # time so ``schedule list`` never reads notifiers.yaml.
        for notify in manifest.notify:
            if notify.only_if is None:
                continue
            try:
                validate_only_if(notify.only_if)
            except simpleeval.FeatureNotAvailable as exc:
                raise ConfigError(
                    "notify only_if is not a valid expression",
                    kind="schedule_notify_only_if_invalid",
                    file=path.name,
                    field="notify.only_if",
                    channel=notify.channel,
                    only_if=notify.only_if,
                ) from exc

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

        # (f) inspector_parameters: deterministic-only, key in the run set,
        # registered + parameterised inspector, values valid by the inspector's
        # own schema. Every failure is translated to ConfigError (no InspectorError
        # / SchemaError / ValidationError leaks out of the loader contract).
        _validate_inspector_parameters(manifest, inspector_registry, path.name)

        manifests.append(manifest)

    return manifests


def _validate_inspector_parameters(
    manifest: ScheduleManifest,
    inspector_registry: InspectorRegistry,
    file: str,
) -> None:
    """Fail-loud validation of ``inspector_parameters`` (design decision 2).

    Five ordered steps, all mapped to ``ConfigError`` so the loader contract
    only ever surfaces that one type (a bare ``InspectorError`` / ``SchemaError``
    / ``ValidationError`` would crash ``schedule`` CLI / ``doctor`` whose except
    tuples do not accept them):

    1. mode applicability — non-empty parameters under a non-deterministic mode.
    2. key membership — key must be in ``resolve_inspector_set(inspectors)``
       (the explicit ``inspectors:`` set is authoritative, never unioned with
       the default health set).
    3. inspector registered — ``registry.get(key)`` may raise ``InspectorError``
       for a typo'd / unloaded inspector name that passed step 2 (explicit sets
       are returned verbatim, not membership-checked against the registry).
    4. inspector parameterised — a no-``parameters:`` inspector silently drops
       non-empty params at runtime, so the loader rejects them outright.
    5. values valid — the same coerce+validate helper the runner uses, so the
       loader and runner accept the identical parameter set.
    """

    params_by_key = manifest.inspector_parameters
    if not params_by_key:
        return

    # Step 1: agent (or omitted) mode never consumes inspector_parameters;
    # silently accepting them would mislead the user into thinking they apply.
    if manifest.mode != "deterministic":
        raise ConfigError(
            "inspector_parameters only applies to mode: deterministic; "
            "agent mode does not consume per-inspector parameters",
            kind="schedule_inspector_parameters_mode_unsupported",
            file=file,
            field="inspector_parameters",
            mode=manifest.mode,
        )

    run_set = set(resolve_inspector_set(manifest.inspectors))

    for key, params in params_by_key.items():
        # Step 2: key must be in the authoritative deterministic run set.
        if key not in run_set:
            raise ConfigError(
                "inspector_parameters key is not in the deterministic run set; "
                "the explicit inspectors list (or the default health set) is "
                "authoritative",
                kind="schedule_inspector_parameters_key_not_in_set",
                file=file,
                field="inspector_parameters",
                key=key,
            )

        # Step 3: the inspector named by the key must be registered. An explicit
        # inspectors set is returned verbatim by resolve_inspector_set, so a
        # member can be a typo / unloaded user inspector — registry.get raises
        # InspectorError which we translate (never leak).
        try:
            inspector = inspector_registry.get(key)
        except InspectorError as exc:
            raise ConfigError(
                "inspector_parameters key names an inspector that is not "
                "registered (unknown name, or a user inspector that failed to "
                "load)",
                kind="schedule_inspector_parameters_inspector_not_registered",
                file=file,
                field="inspector_parameters",
                key=key,
            ) from exc

        # Step 4: a no-parameters inspector drops params silently at runtime;
        # loader-only production gate rejects them. An empty {} is a no-op.
        if inspector.parameters is None:
            if params:
                raise ConfigError(
                    "inspector_parameters target inspector does not accept "
                    "parameters (its manifest declares no parameters: block)",
                    kind="schedule_inspector_parameters_not_accepted",
                    file=file,
                    field="inspector_parameters",
                    key=key,
                )
            continue

        # Step 5: values valid by the inspector's own schema, via the same
        # helper the runner uses (accept sets identical). Both jsonschema
        # exception classes are caught: ValidationError (bad value / typo'd /
        # pattern-rejected key) and SchemaError (the inspector's own parameters
        # schema is malformed) — only catching the former would let SchemaError
        # escape the loader contract.
        try:
            coerce_and_validate_parameters(params, inspector)
        except (jsonschema.ValidationError, jsonschema.exceptions.SchemaError) as exc:
            raise ConfigError(
                "inspector_parameters value failed validation against the "
                "inspector's parameters schema",
                kind="schedule_inspector_parameters_invalid",
                file=file,
                field="inspector_parameters",
                key=key,
                reason=str(exc).splitlines()[0],
            ) from exc


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
