"""Tests for ``InventorySource`` registry + content-sniffing dispatch (task 1.2).

Spec: ``inventory-source/spec.md`` §需求:source registry 必须显式装配.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hostlens.core.exceptions import ConfigError
from hostlens.targets.inventory.base import (
    InventorySource,
    InventorySourceRegistry,
    register_default_sources,
)
from hostlens.targets.inventory.models import CandidateTarget


class _FakeSource:
    """Minimal in-test source with a controllable ``can_handle``."""

    def __init__(self, name: str, *, handles: bool) -> None:
        self.name = name
        self._handles = handles

    def can_handle(self, ref: str) -> bool:
        return self._handles

    def parse(self, ref: str) -> list[CandidateTarget]:
        return []


def _registry(*sources: _FakeSource) -> InventorySourceRegistry:
    registry = InventorySourceRegistry()
    for source in sources:
        registry.register(source)
    return registry


# ---------------------------------------------------------------------------
# explicit --source preferred over sniffing
# ---------------------------------------------------------------------------


def test_explicit_source_skips_sniffing() -> None:
    a = _FakeSource("a", handles=True)
    b = _FakeSource("b", handles=True)  # would also match if sniffed
    registry = _registry(a, b)
    assert registry.resolve("whatever", source="a") is a


def test_explicit_unknown_source_raises_unknown() -> None:
    registry = _registry(_FakeSource("a", handles=True))
    with pytest.raises(ConfigError) as excinfo:
        registry.resolve("whatever", source="nope")
    assert excinfo.value.kind == "unknown_source"


# ---------------------------------------------------------------------------
# sniffing: unique / ambiguous / no-match
# ---------------------------------------------------------------------------


def test_sniff_unique_match() -> None:
    a = _FakeSource("a", handles=True)
    b = _FakeSource("b", handles=False)
    registry = _registry(a, b)
    assert registry.resolve("ref") is a


def test_sniff_multi_match_raises_ambiguous() -> None:
    registry = _registry(_FakeSource("a", handles=True), _FakeSource("b", handles=True))
    with pytest.raises(ConfigError) as excinfo:
        registry.resolve("ref")
    assert excinfo.value.kind == "ambiguous_source"


def test_sniff_no_match_raises_unknown() -> None:
    registry = _registry(_FakeSource("a", handles=False))
    with pytest.raises(ConfigError) as excinfo:
        registry.resolve("ref")
    assert excinfo.value.kind == "unknown_source"


def test_duplicate_registration_rejected() -> None:
    registry = InventorySourceRegistry()
    registry.register(_FakeSource("a", handles=True))
    with pytest.raises(ConfigError) as excinfo:
        registry.register(_FakeSource("a", handles=False))
    assert excinfo.value.kind == "duplicate_source"


# ---------------------------------------------------------------------------
# default assembly + first-batch reconciliation
# ---------------------------------------------------------------------------


def test_register_default_sources_assembles_two() -> None:
    registry = InventorySourceRegistry()
    register_default_sources(registry)
    assert registry.names == ["ssh_config", "yaml"]
    assert isinstance(registry.get("ssh_config"), InventorySource)
    assert isinstance(registry.get("yaml"), InventorySource)


def test_tizi_hosts_no_suffix_uniquely_ssh_config(tmp_path: Path) -> None:
    """``~/tizi/hosts`` (no extension) sniffs to ssh_config only, not yaml."""

    hosts = tmp_path / "hosts"
    hosts.write_text(
        "Host bwg bandwagon\n  HostName 100.76.213.134\n  User root\n",
        encoding="utf-8",
    )
    registry = InventorySourceRegistry()
    register_default_sources(registry)
    resolved = registry.resolve(str(hosts))
    assert resolved.name == "ssh_config"
