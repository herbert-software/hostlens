"""Tests for `list_targets_handler` end-to-end redaction.

Constructs a real `TargetRegistry` whose `TargetEntry` metadata carries
obviously sensitive substrings (paths / IPs / usernames / credentials)
embedded in normally-innocent fields like `display_name`, runs the
handler, and asserts that the returned
`ListTargetsOutput.model_dump_json()` cannot leak any of those
substrings.

Real-target migration (M1): the previous M2 stub `_StubTargetRegistry`
fed the handler arbitrary attribute objects; M1 makes the registry
canonical so test fixtures must round-trip through
`build_registry_from_config` (or direct `TargetRegistry.register`) so the
metadata-from-entry contract is exercised end-to-end.
"""

from __future__ import annotations

import asyncio
import logging

import structlog

from hostlens.core.config import Settings
from hostlens.targets.config import LocalEntry, SSHEntry, TargetsConfig
from hostlens.targets.registry import build_registry_from_config
from hostlens.tools.base import NoopApprovalService, ToolContext
from hostlens.tools.default_tools import list_targets_handler
from hostlens.tools.schemas.list_targets import (
    ListTargetsInput,
    TargetSummary,
)


def _make_ctx(registry_config: TargetsConfig) -> ToolContext:
    registry = build_registry_from_config(registry_config, Settings())
    return ToolContext(
        target_registry=registry,
        inspector_registry=_StubInspectorRegistry(),
        config=Settings(),
        logger=structlog.get_logger("test_list_targets_redaction"),
        approval_service=NoopApprovalService(),
        cancel=asyncio.Event(),
    )


class _StubInspectorRegistry:
    """Inspector registry stays a stub — the next proposal lands the real
    `InspectorRegistry`. `list_targets_handler` never consults this in any
    case.
    """

    def list_summaries(self) -> list[object]:
        return []


def test_list_targets_handler_does_not_leak_sensitive_substrings() -> None:
    """A `TargetEntry` whose `display_name` smuggles a path, IPv4, and
    username substring must cause the whole target to be skipped — the
    redacted output cannot contain any of those substrings.
    """
    # The path / IPv4 / "user alice" patterns each independently match
    # `scrub_inventory_string`'s skip rules → entire target skipped.
    fake_display_name = "login as admin@10.0.0.5 path /Users/alice/.ssh/id_rsa"
    config = TargetsConfig(
        version="1",
        targets=[
            SSHEntry(
                name="prod-web",
                type="ssh",
                host="10.0.0.5",
                user="admin",
                # `password` is masked by SSHEntry.__repr__; we only test
                # that user-visible scrubbed fields (display_name) don't
                # leak embedded substrings.
                display_name=fake_display_name,
                description="primary web server",
                tags=["prod", "web"],
                enabled=True,
            )
        ],
    )
    ctx = _make_ctx(config)

    out = asyncio.run(list_targets_handler(ListTargetsInput(), ctx))

    # Field set is exactly the seven entries.
    assert set(TargetSummary.model_fields.keys()) == {
        "name",
        "kind",
        "display_name",
        "description",
        "capabilities",
        "tags",
        "enabled",
    }

    # The target was skipped because of the sensitive display_name.
    assert out.targets == []

    json_text = out.model_dump_json()

    # None of the forbidden substrings (field values) may appear.
    for needle in (
        "/Users/",
        "/home/",
        ".ssh",
        "id_rsa",
        "10.0.0.5",
        "admin",
    ):
        assert needle not in json_text, (
            f"forbidden substring {needle!r} leaked into JSON: {json_text}"
        )


def test_list_targets_handler_returns_safe_planning_fields() -> None:
    """A clean LocalTarget round-trips through the registry → handler →
    summary projection with the planning-useful fields intact (name /
    kind / capabilities / tags / enabled).
    """
    config = TargetsConfig(
        version="1",
        targets=[
            LocalEntry(
                name="prod-web",
                type="local",
                tags=["web", "prod"],
                enabled=True,
            )
        ],
    )
    ctx = _make_ctx(config)

    out = asyncio.run(list_targets_handler(ListTargetsInput(), ctx))
    assert len(out.targets) == 1
    summary = out.targets[0]
    assert summary.name == "prod-web"
    assert summary.kind == "local"
    # LocalTarget's static baseline capabilities are SHELL + FILE_READ
    # (probed extras like DOCKER_CLI / SYSTEMD only show up after the
    # first `exec` call). `list_targets_handler` reads
    # `target.capabilities` without forcing a probe, so we expect just
    # the static baseline here.
    assert summary.capabilities == ["file_read", "shell"]
    assert summary.tags == ["web", "prod"]
    assert summary.enabled is True


def test_list_targets_handler_filters_disabled_by_default() -> None:
    config = TargetsConfig(
        version="1",
        targets=[
            LocalEntry(name="alpha", type="local", enabled=True),
            LocalEntry(name="bravo", type="local", enabled=False),
        ],
    )
    ctx = _make_ctx(config)

    # Default include_disabled=False → only "alpha" survives.
    out = asyncio.run(list_targets_handler(ListTargetsInput(), ctx))
    assert [t.name for t in out.targets] == ["alpha"]

    # include_disabled=True → both survive.
    out2 = asyncio.run(list_targets_handler(ListTargetsInput(include_disabled=True), ctx))
    assert sorted(t.name for t in out2.targets) == ["alpha", "bravo"]


# Ensure structlog at least doesn't blow up under default logging.
logging.getLogger().setLevel(logging.WARNING)
