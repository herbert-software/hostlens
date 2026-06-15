"""`DEFAULT_HEALTH_INSPECTORS` membership + drift guard.

Spec: deterministic-inspection-mode §需求:deterministic 模式的 inspector
集由内置健康默认集或 `manifest.inspectors` 权威决定 — "其成员**必须**全部
存在于 inspector registry(加测试钉死,防 curated 集漂移)".

The guard builds the *real* registry (builtins only, no user paths) and
asserts every curated name resolves. If a builtin inspector is renamed or
removed, this test fails loudly instead of letting a deterministic run hit
`inspector_not_found` at fire time.
"""

from __future__ import annotations

from hostlens.core.config import Settings
from hostlens.inspectors.health import DEFAULT_HEALTH_INSPECTORS, resolve_inspector_set
from hostlens.inspectors.registry import build_registry_from_search_paths


def _builtin_registry_names() -> set[str]:
    result = build_registry_from_search_paths([], settings=Settings())
    return set(result.registry.names())


def test_default_health_inspectors_all_exist_in_registry() -> None:
    registry_names = _builtin_registry_names()
    missing = [name for name in DEFAULT_HEALTH_INSPECTORS if name not in registry_names]
    assert missing == [], (
        f"DEFAULT_HEALTH_INSPECTORS references inspectors absent from the "
        f"builtin registry (curated-set drift): {missing}"
    )


def test_default_health_inspectors_is_non_empty_and_unique() -> None:
    # A curated default set that is empty or duplicated would silently run
    # nothing / double-run a domain on every fleet host.
    assert len(DEFAULT_HEALTH_INSPECTORS) > 0
    assert len(set(DEFAULT_HEALTH_INSPECTORS)) == len(DEFAULT_HEALTH_INSPECTORS)


def test_default_health_inspectors_covers_core_domains() -> None:
    # Spec lists the required core health domains; assert one representative
    # canonical name per domain is present so a future edit that drops a
    # whole domain (e.g. removes the only disk inspector) fails here.
    required = {
        "linux.cpu.top_processes",
        "linux.memory.pressure",
        "linux.disk.usage",
        "linux.fs.inode_pressure",
        "linux.system.load_avg",
        "linux.systemd.failed_units",
        "log.tail.error_burst",
        "net.listening_ports",
    }
    assert required <= set(DEFAULT_HEALTH_INSPECTORS)


def test_resolve_inspector_set_reexport_is_same_object() -> None:
    # `resolve_inspector_set` lives in `inspectors.health` (next to
    # DEFAULT_HEALTH_INSPECTORS); `orchestration.deterministic` re-exports it
    # so the legacy import path still works. Asserting object *identity* (not
    # just behaviour) is the guard: a future "remove unused import" cleanup of
    # the re-export would silently break the deterministic-path import in
    # tests/orchestration/test_deterministic_collection.py — identity catches it.
    from hostlens.orchestration.deterministic import (
        resolve_inspector_set as reexported,
    )

    assert reexported is resolve_inspector_set


def test_resolve_inspector_set_behaviour() -> None:
    assert resolve_inspector_set(None) == DEFAULT_HEALTH_INSPECTORS
    assert resolve_inspector_set(["x"]) == ("x",)
    # Non-None is verbatim, never unioned with the default set.
    assert resolve_inspector_set([]) == ()
