"""Tests for `list_targets_handler` against a real `TargetRegistry`
(execution-target M1 follow-on).

Covers:

- tool-registry spec §场景:list_targets handler 投影真实 TargetRegistry 数据
  且应用脱敏 + allowlist — two-target fixture, one clean / one skip.
- tool-registry spec §场景:TargetSummary metadata 字段必须来自 TargetEntry —
  fake `ExecutionTarget` carrying its own `display_name` attribute must
  be ignored in favour of `TargetEntry.display_name`.
"""

from __future__ import annotations

import asyncio
from typing import Literal

import structlog

from hostlens.core.config import Settings
from hostlens.targets.base import Capability
from hostlens.targets.config import LocalEntry, SSHEntry, TargetsConfig
from hostlens.targets.registry import TargetRegistry, build_registry_from_config
from hostlens.tools.base import NoopApprovalService, ToolContext
from hostlens.tools.default_tools import list_targets_handler
from hostlens.tools.schemas.list_targets import ListTargetsInput


class _StubInspectorRegistry:
    def list_summaries(self) -> list[object]:
        return []


def _ctx(registry: TargetRegistry) -> ToolContext:
    return ToolContext(
        target_registry=registry,
        inspector_registry=_StubInspectorRegistry(),
        config=Settings(),
        logger=structlog.get_logger("test_list_targets_real_registry"),
        approval_service=NoopApprovalService(),
        cancel=asyncio.Event(),
    )


# ---------------------------------------------------------------------------
# §场景:list_targets handler 投影真实 TargetRegistry 数据
# ---------------------------------------------------------------------------


def test_two_target_scenario_one_clean_one_skipped() -> None:
    """Construct a real TargetRegistry with:
    - (a) `LocalTarget("safe-local")` + a clean `LocalEntry`
    - (b) `SSHTarget("prod-ssh")` + an `SSHEntry` whose `display_name`
      smuggles `"admin@10.0.0.5"` (IPv4 + scrub-rule hit)

    The handler must:
    - return `safe-local` with `display_name="Local Dev"` /
      `tags=["dev"]` / `capabilities` projected from
      `LocalTarget.capabilities` ∩ `CAPABILITY_ALLOWLIST` in
      lexicographic order;
    - skip `prod-ssh` entirely;
    - emit JSON that does not contain `"TEST_PWD_NOT_A_REAL_SECRET_XYZ"`,
      `"10.0.0.5"`, or `"admin"`.
    """
    config = TargetsConfig(
        version="1",
        targets=[
            LocalEntry(
                name="safe-local",
                type="local",
                display_name="Local Dev",
                tags=["dev"],
                enabled=True,
            ),
            SSHEntry(
                name="prod-ssh",
                type="ssh",
                host="example.invalid",
                user="ops",
                # display_name carries the smuggled IPv4 + "admin" token.
                display_name="login as admin@10.0.0.5",
                tags=["prod"],
                enabled=True,
                # secret stays inside the entry — never reaches the
                # summary regardless of scrub, but we assert it doesn't
                # leak through any path either.
                password="TEST_PWD_NOT_A_REAL_SECRET_XYZ",  # pragma: allowlist secret
            ),
        ],
    )
    registry = build_registry_from_config(config, Settings())
    ctx = _ctx(registry)

    out = asyncio.run(list_targets_handler(ListTargetsInput(), ctx))

    # safe-local survives, prod-ssh is skipped → exactly one summary.
    assert [t.name for t in out.targets] == ["safe-local"]
    summary = out.targets[0]
    assert summary.display_name == "Local Dev"
    assert summary.tags == ["dev"]
    assert summary.kind == "local"
    # LocalTarget's static baseline capabilities are SHELL + FILE_READ.
    # Sorted lexicographically by the handler.
    assert summary.capabilities == ["file_read", "shell"]

    json_text = out.model_dump_json()
    for needle in (
        "TEST_PWD_NOT_A_REAL_SECRET_XYZ",
        "10.0.0.5",
        "admin",
    ):  # pragma: allowlist secret
        assert needle not in json_text, (
            f"forbidden substring {needle!r} leaked into JSON: {json_text}"
        )


# ---------------------------------------------------------------------------
# §场景:TargetSummary metadata 字段必须来自 TargetEntry
# ---------------------------------------------------------------------------


class _FakeTargetWithExtraAttr:
    """Bare-bones `ExecutionTarget`-shaped class with a `display_name`
    attribute that should be ignored by `list_targets_handler` — the
    Protocol does not declare `display_name`, and the handler must pull
    `display_name` from the paired `TargetEntry` instead.

    Using a plain class (not `LocalTarget`) avoids fighting against
    pydantic / dataclass field validation: we need to inject an
    arbitrary attribute that's not part of the `ExecutionTarget`
    Protocol.
    """

    type: Literal["local"] = "local"

    def __init__(self) -> None:
        self.name = "t1"
        self.capabilities: set[Capability] = {Capability.SHELL}
        # The "trap" attribute — same name as a TargetSummary field but
        # not declared on `ExecutionTarget`. The handler must NOT pick
        # this up.
        self.display_name = "FROM_TARGET_INSTANCE"
        # `TargetRegistry.register` injects this after validation.
        self._entry: object | None = None

    async def exec(  # pragma: no cover - never called by list_targets
        self, cmd: str, *, timeout: int, env: dict[str, str] | None = None
    ) -> object:
        raise NotImplementedError

    async def read_file(self, path: str) -> bytes:  # pragma: no cover
        raise NotImplementedError


def test_target_summary_display_name_comes_from_entry_not_target_instance() -> None:
    """When the `ExecutionTarget` instance carries a `display_name`
    attribute (e.g. accidentally added by a custom target impl), the
    handler must still use `TargetEntry.display_name` — the Protocol
    does not declare `display_name` and the spec mandates it sources
    from the entry.
    """
    target = _FakeTargetWithExtraAttr()
    entry = LocalEntry(
        name="t1",
        type="local",
        display_name="FROM_ENTRY",
        enabled=True,
    )
    registry = TargetRegistry()
    registry.register(target, entry)  # type: ignore[arg-type]
    ctx = _ctx(registry)

    out = asyncio.run(list_targets_handler(ListTargetsInput(), ctx))

    assert len(out.targets) == 1
    assert out.targets[0].display_name == "FROM_ENTRY"
    assert out.targets[0].display_name != "FROM_TARGET_INSTANCE"
