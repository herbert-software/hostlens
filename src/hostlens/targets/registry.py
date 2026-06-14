"""``TargetRegistry`` — name-indexed ``ExecutionTarget`` registry.

Spec: ``openspec/changes/add-execution-target-abstraction/specs/execution-target/spec.md``
§需求:`TargetRegistry` 必须按 name 索引且同时持有 target 实例与配置元数据.

The registry stores two parallel indexes — one for the runtime
``ExecutionTarget`` instance, one for its source ``TargetEntry`` (which
carries metadata that does **not** live on the Protocol: ``display_name`` /
``description`` / ``tags`` / ``enabled``). Splitting the storage keeps the
``ExecutionTarget`` Protocol minimal while still letting downstream
consumers (``list_targets_handler``, ``hostlens target list``,
``hostlens doctor``) pull metadata without a getattr-on-instance dance.

``build_registry_from_config`` is the public factory: given a parsed
``TargetsConfig`` plus runtime ``Settings`` it instantiates the concrete
``LocalTarget`` / ``SSHTarget`` objects and registers them. Settings is
passed explicitly (not pulled from a module-level singleton) so test
fixtures can drive registry assembly with custom Settings.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final, cast

from hostlens.core.exceptions import ConfigError, TargetError

if TYPE_CHECKING:
    from hostlens.core.config import Settings
    from hostlens.targets.base import ExecutionTarget
    from hostlens.targets.config import TargetEntry, TargetsConfig

__all__ = [
    "TargetRegistry",
    "build_one_target",
    "build_registry_from_config",
]


# Mirror of the ``ExecutionTarget.name`` regex; enforced here as the
# third (and last) defence-in-depth point per spec §需求:`ExecutionTarget`
# Protocol 必须定义完整接口.
_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_\-]{0,63}$")


class TargetRegistry:
    """Name-indexed registry holding both targets and their config metadata.

    The registry is intentionally simple — it is a pair of dicts keyed by
    target ``name`` with read-only access methods. Connection lifecycle
    (SSH control connections, asyncssh state) is owned entirely by the
    target instances, not by the registry. ``register`` is the single
    write entry point so all validation (name regex, name-vs-entry
    coherence, duplicate detection) happens in one place.
    """

    def __init__(self) -> None:
        self._targets: dict[str, ExecutionTarget] = {}
        self._entries: dict[str, TargetEntry] = {}

    def register(self, target: ExecutionTarget, entry: TargetEntry) -> None:
        """Add ``target`` + its source ``entry`` to the registry.

        Validation order (each step is a distinct failure mode the spec
        scenarios assert on, so do not re-order without amending):

        1. ``target.name == entry.name`` — guards against metadata
           binding to the wrong target instance.
        2. ``target.name`` matches the spec regex — the third defence
           layer behind the loader and the per-implementation
           ``__init__``; catches callers that bypass both (e.g. tests
           that hand-craft an ``ExecutionTarget`` mock).
        3. Name uniqueness — duplicates raise without mutating any state
           so the registry never ends up partially updated.

        After validation we inject the entry onto the target instance
        (``target._entry = entry``). This is the documented contract
        that ``LocalTarget`` / ``SSHTarget`` rely on to read
        ``enabled`` / ``connect_timeout`` / host / user / credentials.
        Bypassing ``register`` (constructing a target directly in a unit
        test) leaves ``_entry`` as ``None``, which the targets treat as
        "enabled" — that matches the test-friendly fallback documented
        on each implementation.
        """

        if target.name != entry.name:
            raise TargetError(
                kind="target_entry_name_mismatch",
                target=target.name,
                entry_name=entry.name,
            )
        if _NAME_PATTERN.fullmatch(target.name) is None:
            raise TargetError(kind="invalid_target_name", target=target.name)
        if target.name in self._targets:
            raise TargetError(kind="duplicate_target", target=target.name)

        self._targets[target.name] = target
        self._entries[target.name] = entry
        # The concrete targets accept this as a documented injection point
        # — keep the attribute name in sync with ``LocalTarget._entry`` /
        # ``SSHTarget._entry``.
        target._entry = entry  # type: ignore[attr-defined]

    def get(self, name: str) -> ExecutionTarget:
        """Return the registered target for ``name``.

        Raises ``KeyError`` (not ``TargetError``) when missing —
        "lookup miss" is not a Hostlens business error per spec
        §场景:get 未找到 raise KeyError.
        """

        return self._targets[name]

    def get_entry(self, name: str) -> TargetEntry:
        """Return the source ``TargetEntry`` for ``name``.

        Raises ``KeyError`` for a missing name (same rationale as
        ``get``).
        """

        return self._entries[name]

    def names(self) -> set[str]:
        """Return the set of registered target names (order undefined)."""

        return set(self._targets.keys())

    def list_entries(self) -> list[TargetEntry]:
        """Return all registered entries sorted by name (deterministic)."""

        return [self._entries[name] for name in sorted(self._entries.keys())]

    def list(self) -> list[ExecutionTarget]:
        """Return all registered targets sorted by name (deterministic).

        Defined last in the class body so any earlier method whose
        return type is ``list[...]`` (e.g. ``list_entries``) does not
        get its annotation rebound to this method by Python's class
        scope rules. ``list`` shadows the builtin name; that is
        intentional — the spec mandates exactly this method name on
        ``TargetRegistry``.
        """

        return [self._targets[name] for name in sorted(self._targets.keys())]


def build_registry_from_config(
    config: TargetsConfig,
    settings: Settings,
) -> TargetRegistry:
    """Instantiate ``LocalTarget`` / ``SSHTarget`` instances and register them.

    ``settings`` is threaded through explicitly (instead of letting the
    targets reach into a module-level singleton) so test fixtures can
    drive registry assembly with a custom ``Settings`` instance.
    ``SSHTarget`` reads ``ssh.idle_timeout_seconds`` from the ambient
    ``Settings()`` lazily on first ``exec``; passing ``settings`` here
    keeps that wiring honest and lets future SSH settings (keepalive,
    channel limits) land without re-plumbing the
    factory signature.
    """

    # Import lazily to keep top-level imports cheap and to avoid the
    # Windows-only ``ImportError`` from ``hostlens.targets.local`` when a
    # caller only needs ``TargetRegistry`` (the class itself is
    # platform-agnostic).
    from hostlens.targets.docker import DockerTarget
    from hostlens.targets.kubernetes import KubernetesTarget
    from hostlens.targets.local import LocalTarget
    from hostlens.targets.replay import ReplayTarget
    from hostlens.targets.ssh import SSHTarget

    # ``settings`` is threaded into SSHTarget via the private
    # ``_settings`` kwarg so SSHTarget reads the fixture-driven value
    # rather than constructing its own ``Settings()`` instance. This
    # is what makes test fixtures' Settings monkey-patches actually
    # land on the target — without injection, SSHTarget's lazy
    # ``Settings()`` would silently re-read process env vars instead.

    registry = TargetRegistry()
    for entry in config.targets:
        # Build the concrete target first under its narrow type, then
        # cast to the Protocol for ``register`` — assigning the narrow
        # type directly into an ``ExecutionTarget`` annotated variable
        # confuses mypy because the ``type`` field's ``Literal`` set is
        # wider on the Protocol than on the implementation (the
        # invariance is intentional, see ExecutionTarget docstring).
        target: ExecutionTarget
        if entry.type == "local":
            target = cast("ExecutionTarget", LocalTarget(name=entry.name))
        elif entry.type == "ssh":
            target = cast("ExecutionTarget", SSHTarget(name=entry.name, _settings=settings))
        elif entry.type == "replay":
            # Read-only replay target (incident-pack). No secrets, no write
            # path → not subject to the EUID==0 write guard.
            target = cast("ExecutionTarget", ReplayTarget(name=entry.name, fixture=entry.fixture))
        elif entry.type == "docker":
            # Read-only docker target. Construction is pure (no docker call /
            # no daemon dial); the client is built lazily on first exec.
            target = cast("ExecutionTarget", DockerTarget(name=entry.name))
        elif entry.type == "k8s":
            # Read-only k8s target. Construction is pure (no kubeconfig load /
            # no API-server dial); both clients are built lazily on first exec.
            target = cast("ExecutionTarget", KubernetesTarget(name=entry.name))
        else:  # pragma: no cover - Pydantic discriminator excludes other values
            raise ConfigError(
                kind="unknown_target_type",
                type=entry.type,
                target=entry.name,
            )
        registry.register(target, entry)
    return registry


def build_one_target(entry: TargetEntry, settings: Settings) -> ExecutionTarget:
    """Construct + register a single ``TargetEntry`` and return its target.

    The probe path (``hostlens target import``) needs a single live target
    from one promoted ``TargetEntry``. It MUST reuse the same
    construct-then-register path as the multi-target factory so the target's
    ``_entry`` gets injected by ``TargetRegistry.register`` — a bare
    ``SSHTarget(name=...)`` leaves ``_entry=None`` and the first ``exec``
    raises ``TargetError(kind="ssh_no_entry")`` (the whole first-batch SSH
    cohort would then fail). Wrapping the single entry in a 1-entry
    ``TargetsConfig`` and threading it through ``build_registry_from_config``
    reuses all of that construct+register logic without re-copying the
    ``_entry`` injection — there is exactly one construction SOT.

    ``settings`` is consumed only by the SSH branch (mirrors
    ``build_registry_from_config``'s ``_settings`` injection); the local
    branch ignores it but the parameter is kept for signature symmetry.
    """

    from hostlens.targets.config import TargetsConfig

    config = TargetsConfig(version="1", targets=[entry])
    return build_registry_from_config(config, settings).get(entry.name)
