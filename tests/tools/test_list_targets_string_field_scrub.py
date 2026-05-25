"""Tests for `list_targets_handler` per-string-field scrub paths
(spec §需求:TargetSummary 输出 schema 必须脱敏 §字段值脱敏约束).

Four scenarios covering the four scrub paths against a real
`TargetRegistry` populated by `build_registry_from_config`:

(a) `display_name` containing IPv4 → whole target SKIP, structured
    warning logged with code `sensitive_substring_in_display_name`,
    output JSON contains no IP.
(b) `tags` containing IPv4 → whole target SKIP, warning code
    `sensitive_substring_in_tags`.
(c) `description` containing "user <ident>" → target RETAINED with the
    identifier replaced by `"***"`, prefix + suffix preserved, `alice`
    no longer present.
(d) Tags like `"user-service"` / `"auth-microservice"` do NOT trigger
    skip — compound words are not redacted.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Generator

import pytest
import structlog
from structlog.testing import LogCapture

from hostlens.core.config import Settings
from hostlens.targets.config import LocalEntry, TargetsConfig
from hostlens.targets.registry import build_registry_from_config
from hostlens.tools.base import NoopApprovalService, ToolContext
from hostlens.tools.default_tools import list_targets_handler
from hostlens.tools.schemas.list_targets import ListTargetsInput


class _StubInspectorRegistry:
    def list_summaries(self) -> list[object]:
        return []


@pytest.fixture
def log_capture() -> Generator[LogCapture, None, None]:
    # structlog config is process-global. Snapshot the prior config before
    # reconfiguring so this fixture cannot bleed into later tests in the
    # suite (which would manifest as order-dependent flakiness — every test
    # touching the global logger would end up holding `LogCapture` as the
    # only processor).
    cap = LogCapture()
    prior = structlog.get_config()
    try:
        structlog.configure(
            processors=[cap],
            wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
            cache_logger_on_first_use=False,
        )
        yield cap
    finally:
        structlog.configure(**prior)


def _ctx_with(config: TargetsConfig) -> ToolContext:
    registry = build_registry_from_config(config, Settings())
    return ToolContext(
        target_registry=registry,
        inspector_registry=_StubInspectorRegistry(),
        config=Settings(),
        logger=structlog.get_logger("test_list_targets_scrub"),
        approval_service=NoopApprovalService(),
        cancel=asyncio.Event(),
    )


# ---------------------------------------------------------------------------
# (a) display_name contains IPv4 → SKIP whole target.
# ---------------------------------------------------------------------------


def test_display_name_with_ipv4_triggers_skip(log_capture: LogCapture) -> None:
    config = TargetsConfig(
        version="1",
        targets=[
            LocalEntry(
                name="prod-web",
                type="local",
                display_name="login as admin@10.0.0.5",
            )
        ],
    )
    ctx = _ctx_with(config)
    out = asyncio.run(list_targets_handler(ListTargetsInput(), ctx))

    # Target is gone.
    assert out.targets == []
    # Warning with the expected reason code is present.
    matches = [
        e
        for e in log_capture.entries
        if e.get("event") == "list_targets_skip"
        and e.get("reason") == "sensitive_substring_in_display_name"
    ]
    assert len(matches) == 1, f"expected 1 skip log, got entries={log_capture.entries}"
    # Reason code is logged but the offending value is NOT.
    assert "10.0.0.5" not in str(matches[0])
    # And the rendered output JSON never carries the IP.
    assert "10.0.0.5" not in out.model_dump_json()


# ---------------------------------------------------------------------------
# (b) tags contain IPv4 → SKIP whole target.
# ---------------------------------------------------------------------------


def test_tags_with_ipv4_triggers_skip(log_capture: LogCapture) -> None:
    config = TargetsConfig(
        version="1",
        targets=[
            LocalEntry(
                name="prod-db",
                type="local",
                tags=["prod", "db", "192.168.1.42"],
            )
        ],
    )
    ctx = _ctx_with(config)
    out = asyncio.run(list_targets_handler(ListTargetsInput(), ctx))

    assert out.targets == []
    matches = [
        e
        for e in log_capture.entries
        if e.get("event") == "list_targets_skip"
        and e.get("reason") == "sensitive_substring_in_tags"
    ]
    assert len(matches) == 1
    assert "192.168.1.42" not in out.model_dump_json()


# ---------------------------------------------------------------------------
# (c) description with "user <ident>" → REDACT identifier, keep target.
# ---------------------------------------------------------------------------


def test_description_with_user_keyword_redacts_identifier_token(
    log_capture: LogCapture,
) -> None:
    config = TargetsConfig(
        version="1",
        targets=[
            LocalEntry(
                name="prod-web",
                type="local",
                description="Owned by user alice, contact via slack",
            )
        ],
    )
    ctx = _ctx_with(config)
    out = asyncio.run(list_targets_handler(ListTargetsInput(), ctx))

    assert len(out.targets) == 1
    summary = out.targets[0]
    assert summary.description == "Owned by user ***, contact via slack"
    assert summary.description is not None and "alice" not in summary.description
    # No skip warning emitted.
    skips = [e for e in log_capture.entries if e.get("event") == "list_targets_skip"]
    assert skips == []


# ---------------------------------------------------------------------------
# (d) Compound-word tags must NOT trigger skip.
# ---------------------------------------------------------------------------


def test_compound_word_tags_are_not_skipped(log_capture: LogCapture) -> None:
    config = TargetsConfig(
        version="1",
        targets=[
            LocalEntry(
                name="prod-web",
                type="local",
                tags=["user-service", "auth-microservice"],
            )
        ],
    )
    ctx = _ctx_with(config)
    out = asyncio.run(list_targets_handler(ListTargetsInput(), ctx))

    assert len(out.targets) == 1
    summary = out.targets[0]
    assert summary.tags == ["user-service", "auth-microservice"]
    skips = [e for e in log_capture.entries if e.get("event") == "list_targets_skip"]
    assert skips == []
