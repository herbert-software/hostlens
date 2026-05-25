"""Tests for ``hostlens.targets.base.Capability`` enum.

Spec: ``openspec/changes/add-execution-target-abstraction/specs/execution-target/spec.md``
§需求:`Capability` Enum 必须含 M1 最小集且与 ToolRegistry allowlist 严格相等.

Three scenarios are covered, mirroring the three spec scenarios for this
need:

1. The enum has **exactly** the M1 minimum set of 5 members — no more
   (no future-milestone placeholders), no less.
2. Every member value is the lowercase form of the member name.
3. ``frozenset({c.value for c in Capability})`` is strictly equal to
   ``hostlens.tools.schemas.list_targets.CAPABILITY_ALLOWLIST``.
"""

from __future__ import annotations

from hostlens.targets.base import Capability
from hostlens.tools.schemas.list_targets import CAPABILITY_ALLOWLIST


def test_capability_has_exactly_m1_minimum_set() -> None:
    """M1 lock: exactly 5 members; refusing to pre-allocate M8/M9 names.

    Adding a member here is a breaking change for downstream allowlists
    and Inspector manifest schemas — it must go through its own proposal
    (M8 ``K8S_EXEC``, M9 ``FILE_WRITE``, etc.).
    """

    assert set(Capability.__members__.keys()) == {
        "SHELL",
        "FILE_READ",
        "SSH",
        "SYSTEMD",
        "DOCKER_CLI",
    }


def test_capability_values_are_lowercase() -> None:
    """Member values use lowercase snake_case matching the member name.

    docs/ARCHITECTURE.md §5 locks this: tokens flowing through yaml /
    Tool Registry summaries / manifests are the lowercase form so the
    enum is unambiguous wire-side.
    """

    for member in Capability:
        assert member.value == member.name.lower()


def test_capability_allowlist_strictly_equals_enum_values() -> None:
    """Spec §场景:capabilities 与 CAPABILITY_ALLOWLIST 严格相等.

    ``CAPABILITY_ALLOWLIST`` MUST be derived from the enum (not a
    hardcoded literal set), so this test will fail loudly the moment the
    two drift — for example if someone adds an enum member but forgets
    to bump the allowlist (or vice versa).
    """

    assert frozenset({c.value for c in Capability}) == CAPABILITY_ALLOWLIST
