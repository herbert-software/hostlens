"""Curated built-in health inspector set for deterministic inspection mode.

`DEFAULT_HEALTH_INSPECTORS` is the named constant used by the
deterministic scheduler path (`mode=deterministic`) when a manifest does
**not** declare an explicit `inspectors:` list: every target in the fleet
runs this fixed set, filtered per-target by capability (`requires_unmet`
is an expected skip on heterogeneous fleets, never a failure).

It is a deliberately *curated* selection of existing registry inspector
canonical names, NOT an auto-derived "run everything" set:

  * Auto-running every registered inspector would be slow and noisy
    (e.g. running `mysql.*` on every host). Curation keeps the default
    daily-health run fast and signal-dense.
  * The trade-off is drift: a newly added inspector does not enter the
    default set automatically. To opt one in, add its name here
    explicitly (and the registry-membership test in
    `tests/inspectors/test_health_default_set.py` will keep the set from
    referencing a name that no longer exists).

Domain coverage (one inspector per core health domain — spec §需求:
deterministic 模式的 inspector 集由内置健康默认集或 `manifest.inspectors`
权威决定):

  * cpu              — `linux.cpu.top_processes`
  * memory           — `linux.memory.pressure`
  * disk capacity    — `linux.disk.usage`
  * inode            — `linux.fs.inode_pressure`
  * system load      — `linux.system.load_avg`
  * systemd services — `linux.systemd.failed_units`
  * recent error log — `log.tail.error_burst`
  * network          — `net.listening_ports`

Every member targets `local` / `ssh` (the managed-host fleet shape), so
the set is meaningful across an SSH fleet; on a target lacking a required
capability the runner records `requires_unmet` (treated as an expected
skip by the fleet status derivation).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["DEFAULT_HEALTH_INSPECTORS", "resolve_inspector_set"]


DEFAULT_HEALTH_INSPECTORS: tuple[str, ...] = (
    "linux.cpu.top_processes",
    "linux.memory.pressure",
    "linux.disk.usage",
    "linux.fs.inode_pressure",
    "linux.system.load_avg",
    "linux.systemd.failed_units",
    "log.tail.error_burst",
    "net.listening_ports",
)


def resolve_inspector_set(inspectors: Sequence[str] | None) -> tuple[str, ...]:
    """Resolve the authoritative inspector set for a deterministic run.

    `inspectors is None` (manifest declared no `inspectors:`) → the curated
    `DEFAULT_HEALTH_INSPECTORS`. A non-None list → that list verbatim, as
    the **authoritative** set (deterministic mode does not treat
    `manifest.inspectors` as a soft hint and never unions it with the
    default set — spec §场景:显式 inspectors 变权威集). An explicitly empty
    list is honoured as "run nothing" (the caller / loader is responsible
    for rejecting an empty list if that is undesirable; this resolver does
    not silently fall back to the default for `[]`, which would resurrect
    the soft-hint behaviour the spec forbids).
    """
    if inspectors is None:
        return DEFAULT_HEALTH_INSPECTORS
    return tuple(inspectors)
