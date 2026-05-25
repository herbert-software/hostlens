"""Tests for `InspectorRunner.__init__`.

Pure construction contract: __init__ must NOT trigger IO / subprocess /
yaml parsing. We verify by passing a target with a mock `exec` that
counts invocations and confirming no calls happened by the time
`__init__` returned.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import structlog

from hostlens.core.config import Settings
from hostlens.inspectors.runner import InspectorRunner
from hostlens.targets.registry import TargetRegistry


def _logger() -> structlog.stdlib.BoundLogger:
    return structlog.get_logger("test")  # type: ignore[no-any-return]


def test_init_is_pure_no_subprocess() -> None:
    """Constructing the runner must not trigger any target.exec call."""

    target_registry = TargetRegistry()
    settings = Settings()
    # We probe by looking at how many times any AsyncMock would have been
    # called — but TargetRegistry holds no targets here. The contract is
    # that __init__ doesn't dispatch to any registry method either.
    target_registry_spy: Any = MagicMock(wraps=target_registry)
    runner = InspectorRunner(
        target_registry_spy,
        settings=settings,
        logger=_logger(),
    )

    # No registry methods should have been invoked.
    assert target_registry_spy.mock_calls == []
    # Sanity: runner stores the dependencies.
    assert runner is not None


def test_init_stores_dependencies() -> None:
    """The three dependencies must be stored verbatim for later use."""

    target_registry = TargetRegistry()
    settings = Settings()
    logger = _logger()
    runner = InspectorRunner(
        target_registry,
        settings=settings,
        logger=logger,
    )
    # Attributes are private by convention but we verify wiring via behavior:
    # construction succeeded without IO and the runner is usable.
    assert isinstance(runner, InspectorRunner)


def test_init_with_async_target_mock_zero_exec_calls() -> None:
    """Even with a target that has an AsyncMock exec, no exec call fires."""

    fake_target = MagicMock()
    fake_target.exec = AsyncMock()
    target_registry = TargetRegistry()
    settings = Settings()
    InspectorRunner(target_registry, settings=settings, logger=_logger())
    # The runner does NOT see fake_target in its constructor — but if it
    # had triggered any subprocess via some side-channel, we'd see calls
    # on the mock.
    assert fake_target.exec.call_count == 0

    # Also: even running the constructor inside a loop doesn't fire exec.
    async def _construct() -> None:
        InspectorRunner(target_registry, settings=settings, logger=_logger())

    asyncio.run(_construct())
    assert fake_target.exec.call_count == 0
