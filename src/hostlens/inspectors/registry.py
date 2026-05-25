"""InspectorRegistry + ``build_registry_from_search_paths`` factory.

The registry is the runtime SOT for every Inspector that hostlens knows
about. It is intentionally a plain class (not Pydantic) because its state
is mutable — manifests are registered after loading and looked up by name
at dispatch time.

``build_registry_from_search_paths`` is the assembly entrypoint that
honours the M1 security contract from
``inspector-plugin-system/spec.md`` §需求:``build_registry_from_search_paths``
必须返回 ``(registry, errors)`` 双值, namely:

  * Builtin manifests live under ``src/hostlens/inspectors/builtin/`` and
    that path is **hardcoded** — never read from settings. Builtin
    file-level errors propagate (a broken builtin yaml means a shipped
    bug; we must surface it loudly).
  * User search paths come from ``Settings.inspectors_search_paths`` (or
    are passed directly by callers). File-level errors there are
    **collected** into ``RegistryBuildResult.errors`` so a single broken
    user yaml does not block the rest of the registry from loading.
  * ``duplicate_inspector`` errors (across any combination of
    builtin/user paths) are **always fatal** — silently overriding a
    builtin would let an attacker plant a same-named manifest under
    ``~/.config/hostlens/inspectors`` and run arbitrary commands without
    the user noticing.
"""

from __future__ import annotations

import builtins
from dataclasses import dataclass, field
from pathlib import Path

import hostlens.inspectors as _inspectors_pkg
from hostlens.core.config import Settings
from hostlens.core.exceptions import InspectorError
from hostlens.inspectors import loader as _loader
from hostlens.inspectors.schema import InspectorManifest
from hostlens.tools.schemas.list_inspectors import InspectorSummary

__all__ = [
    "InspectorRegistry",
    "RegistryBuildResult",
    "RegistryLoadError",
    "build_registry_from_search_paths",
]


# --------------------------------------------------------------------------- #
# Collectable error kinds — single-file failures from a user-controlled path
# that should NOT bring the whole registry down. Anything outside this set
# (most importantly ``duplicate_inspector``) is treated as fatal and
# propagates out of ``build_registry_from_search_paths``.
# --------------------------------------------------------------------------- #


_COLLECTABLE_KINDS: frozenset[str] = frozenset(
    {
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
        "parse_json_not_object",
    }
)


# --------------------------------------------------------------------------- #
# InspectorRegistry
# --------------------------------------------------------------------------- #


class InspectorRegistry:
    """In-memory store of registered ``InspectorManifest`` objects.

    The registry indexes by ``manifest.name``. Each entry remembers the
    source ``Path`` the manifest came from (or ``None`` for programmatic
    registration) so duplicate-detection errors can point at both the
    existing and the incoming file.
    """

    def __init__(self) -> None:
        self._entries: dict[str, tuple[InspectorManifest, Path | None]] = {}

    def register(
        self,
        manifest: InspectorManifest,
        source_path: Path | None = None,
    ) -> None:
        """Add ``manifest`` to the registry.

        Raises ``InspectorError(kind="duplicate_inspector")`` if a manifest
        with the same name is already registered. ``existing_path`` on the
        raised error always comes from the previously-registered entry's
        source path, so the diagnostic is consistent regardless of which
        side a caller considers "incoming".
        """

        name = manifest.name
        if name in self._entries:
            _existing_manifest, existing_path = self._entries[name]
            raise InspectorError(
                kind="duplicate_inspector",
                inspector=name,
                existing_path=existing_path,
                new_path=source_path,
            )
        self._entries[name] = (manifest, source_path)

    def get(self, name: str) -> InspectorManifest:
        """Return the manifest registered under ``name``.

        Raises ``InspectorError(kind="inspector_not_found")`` when the name
        is unknown — keeps the failure shape consistent with the rest of
        the loader/registry surface.
        """

        try:
            manifest, _ = self._entries[name]
        except KeyError as exc:
            raise InspectorError(
                kind="inspector_not_found",
                inspector=name,
            ) from exc
        return manifest

    def names(self) -> builtins.list[str]:
        """Return all registered inspector names, sorted ascending."""

        return sorted(self._entries.keys())

    def list(self) -> builtins.list[InspectorManifest]:
        """Return all registered manifests, sorted by name ascending."""

        return [self._entries[name][0] for name in self.names()]

    def list_summaries(self) -> builtins.list[InspectorSummary]:
        """Project each manifest into the M2-locked ``InspectorSummary``.

        ``tags`` and ``compatible_target_kinds`` are sorted in dictionary
        order so the prompt-cache prefix consumed by ``list_inspectors``
        is stable across runs (small list ordering swings would otherwise
        keep busting the prefix).
        """

        summaries: builtins.list[InspectorSummary] = []
        for manifest in self.list():
            summaries.append(
                InspectorSummary(
                    name=manifest.name,
                    version=manifest.version,
                    description=manifest.description,
                    tags=sorted(manifest.tags),
                    compatible_target_kinds=sorted(manifest.targets),
                )
            )
        return summaries


# --------------------------------------------------------------------------- #
# RegistryLoadError / RegistryBuildResult
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RegistryLoadError:
    """Single user-path manifest load failure surfaced by
    ``build_registry_from_search_paths``.

    Built from an ``InspectorError`` whose ``kind`` is in the
    collectable set. ``detail`` is ``str(err)`` at capture time so the
    diagnostic survives even if the original exception object is dropped.
    """

    path: Path
    kind: str
    detail: str


@dataclass(frozen=True)
class RegistryBuildResult:
    """Result of ``build_registry_from_search_paths``.

    ``registry`` always contains every successfully-loaded manifest,
    including the builtins. ``errors`` only carries per-file failures
    from user paths; builtin-level failures propagate instead.
    """

    registry: InspectorRegistry
    errors: list[RegistryLoadError] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# build_registry_from_search_paths
# --------------------------------------------------------------------------- #


def _builtin_dir() -> Path:
    """Return the hardcoded builtin manifest directory.

    Resolved at call time (not import time) so a test that monkeypatches
    ``hostlens.inspectors.__file__`` sees the override. Kept private so
    nothing outside this module can swap the path for a settings value
    (the security contract is "builtins live next to package code").
    """

    pkg_file = _inspectors_pkg.__file__
    if pkg_file is None:  # pragma: no cover - namespace packages won't ship here
        raise RuntimeError(
            "hostlens.inspectors has no __file__; cannot resolve builtin path"
        )
    return Path(pkg_file).parent / "builtin"


def _iter_manifests(directory: Path) -> list[Path]:
    """Return manifests under ``directory`` in deterministic alphabetical
    order. Missing directories yield ``[]`` (callers decide whether that's
    an error)."""

    if not directory.exists():
        return []
    return sorted(directory.rglob("*.yaml"), key=lambda p: str(p))


def build_registry_from_search_paths(
    user_paths: list[Path],
    *,
    settings: Settings,
) -> RegistryBuildResult:
    """Assemble an ``InspectorRegistry`` from the builtin path + user paths.

    Loading order is deterministic: builtin manifests first (alphabetical
    by absolute path), then each ``user_paths`` entry in the order given,
    each entry's manifests alphabetical within itself.

    Error semantics — keep this in lockstep with the spec:

      * Builtin file errors propagate. A broken builtin yaml is a shipped
        bug; surfacing it loudly forces the maintainer to fix the
        package, not the user.
      * User-path file errors whose ``kind`` is collectable (parse /
        validation / shell-injection static rejects / etc.) are appended
        to ``result.errors`` and skipped. Other inspectors continue to
        load.
      * ``duplicate_inspector`` is ALWAYS fatal — it would otherwise let
        an attacker plant a same-named yaml to silently override a
        builtin or another user manifest.

    ``settings`` is accepted to keep the signature stable as future
    knobs (per-directory enable/disable flags, validation strictness) get
    added; the M1 implementation does not read anything from it because
    the builtin path is hardcoded by design.
    """

    del settings  # M1: no setting drives behaviour here (builtin path hardcoded)

    registry = InspectorRegistry()
    errors: list[RegistryLoadError] = []

    # ---- 1. Builtins (fatal on any file error) ----
    builtin_dir = _builtin_dir()
    for manifest_path in _iter_manifests(builtin_dir):
        manifest = _loader.load_manifest(manifest_path)
        registry.register(manifest, source_path=manifest_path)

    # ---- 2. User paths (collect per-file errors, but propagate duplicates) ----
    for user_path in user_paths:
        for manifest_path in _iter_manifests(user_path):
            try:
                manifest = _loader.load_manifest(manifest_path)
            except InspectorError as err:
                if err.kind in _COLLECTABLE_KINDS:
                    errors.append(
                        RegistryLoadError(
                            path=manifest_path,
                            kind=err.kind,
                            detail=str(err),
                        )
                    )
                    continue
                # Any non-collectable error is a fatal contract violation.
                raise
            # `duplicate_inspector` raised here is fatal — do NOT catch.
            registry.register(manifest, source_path=manifest_path)

    return RegistryBuildResult(registry=registry, errors=errors)
