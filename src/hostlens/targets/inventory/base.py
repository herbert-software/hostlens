"""``InventorySource`` Protocol + registry + content-sniffing dispatch.

Spec: ``inventory-source/spec.md`` §需求:source registry 必须显式装配.

Dispatch semantics (this repo's first content-sniffing registry, distinct
from notifier / inspector explicit-type lookup):

- CLI ``--source`` explicit selection always wins; sniffing is skipped.
- Default: each registered source's ``can_handle`` is consulted.
- Multiple ``can_handle`` matches → ``ConfigError(kind="ambiguous_source")``
  (CLI maps to exit 2) — never silently take the first.
- No match → ``ConfigError(kind="unknown_source")``.

The registry is assembled explicitly via ``register_default_sources`` (no
module-level singleton, mirroring ``register_default_tools``).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from hostlens.core.exceptions import ConfigError
from hostlens.targets.inventory.models import CandidateTarget

__all__ = [
    "InventorySource",
    "InventorySourceRegistry",
    "register_default_sources",
]


@runtime_checkable
class InventorySource(Protocol):
    """One pluggable inventory source.

    ``parse`` MUST be pure parsing: no network connection, no DNS
    resolution (probing belongs to ``TargetProbe``, orthogonal to parsing).
    A malformed source (yaml syntax error / unparseable ssh_config) raises
    ``ConfigError`` — never a "could not connect" failure.
    """

    name: str

    def can_handle(self, ref: str) -> bool:
        """Sniff (by suffix / content) whether ``ref`` belongs to this source."""
        ...

    def parse(self, ref: str) -> list[CandidateTarget]:
        """Parse ``ref`` into a list of candidates (no IO beyond reading ``ref``)."""
        ...


class InventorySourceRegistry:
    """Holds registered sources keyed by ``name`` and resolves a ``ref``.

    Resolution is explicit-first then sniff: ``resolve(ref, source=...)``
    looks the named source up directly (skipping ``can_handle``); without an
    explicit ``source`` it consults every source's ``can_handle`` and
    enforces the single-match invariant.
    """

    def __init__(self) -> None:
        self._sources: dict[str, InventorySource] = {}

    def register(self, source: InventorySource) -> None:
        if source.name in self._sources:
            raise ConfigError(
                "duplicate inventory source registration",
                kind="duplicate_source",
                source=source.name,
            )
        self._sources[source.name] = source

    @property
    def names(self) -> list[str]:
        return sorted(self._sources)

    def get(self, name: str) -> InventorySource:
        """Return the source registered under ``name``.

        Raises ``ConfigError(kind="unknown_source")`` for an unregistered
        name (the CLI ``--source`` explicit path) so an unknown ``--source``
        value maps to a structured exit-2 error rather than a bare KeyError.
        """

        source = self._sources.get(name)
        if source is None:
            raise ConfigError(
                "unknown inventory source",
                kind="unknown_source",
                source=name,
                known=",".join(self.names),
            )
        return source

    def resolve(self, ref: str, *, source: str | None = None) -> InventorySource:
        """Pick the source for ``ref`` (explicit ``source`` wins, else sniff).

        - ``source`` given → ``get(source)`` (no ``can_handle`` sniffing).
        - else → consult every ``can_handle``; exactly one match is
          required. Zero → ``unknown_source``; many → ``ambiguous_source``
          (never silently take the first).
        """

        if source is not None:
            return self.get(source)

        matches = [src for src in self._sources.values() if src.can_handle(ref)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ConfigError(
                "no inventory source recognized this ref; pass --source explicitly",
                kind="unknown_source",
                known=",".join(self.names),
            )
        raise ConfigError(
            "multiple inventory sources matched; pass --source explicitly",
            kind="ambiguous_source",
            matched=",".join(sorted(src.name for src in matches)),
        )


def register_default_sources(registry: InventorySourceRegistry) -> None:
    """Assemble the first-version source set onto ``registry``.

    Imported lazily so ``base.py`` carries no hard dependency on the
    concrete sources (keeps the Protocol module importable on its own and
    avoids an import cycle if a source ever needs registry types).
    """

    from hostlens.targets.inventory.sources.ssh_config import SshConfigSource
    from hostlens.targets.inventory.sources.yaml import YamlSource

    registry.register(SshConfigSource())
    registry.register(YamlSource())
