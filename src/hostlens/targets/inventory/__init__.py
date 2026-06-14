"""Inventory-source abstraction for ``hostlens target import``.

Spec: ``openspec/changes/add-cli-target-import/specs/inventory-source/spec.md``.

A pluggable layer that turns "one kind of inventory file" into a list of
``CandidateTarget`` — an *unvalidated* nomination intent that the import
pipeline later promotes to a ``TargetEntry``. ``parse`` is pure parsing:
no network, no DNS. Adding a source = one Python implementation class +
explicit registration via ``register_default_sources`` (mirroring
``register_default_tools`` — no module-level singleton).

The dispatch semantics are the repo's first **content sniffing** registry
(``can_handle``); unlike the notifier / inspector explicit type lookup,
``--source`` is always preferred and a multi-match raises an ambiguous
error rather than silently picking the first.
"""

from __future__ import annotations

from hostlens.targets.inventory.base import (
    InventorySource,
    InventorySourceRegistry,
    register_default_sources,
)
from hostlens.targets.inventory.models import CandidateTarget, normalize_target_name

__all__ = [
    "CandidateTarget",
    "InventorySource",
    "InventorySourceRegistry",
    "normalize_target_name",
    "register_default_sources",
]
