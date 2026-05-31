"""Incident snapshot: file-descriptor exhaustion.

Double replay layer: ReplayTarget fixture + PlaybackBackend cassette drive the
full ``--intent`` Planner pipeline offline (zero API quota, zero SSH). See
``_harness`` / ``_scenarios`` for the shared machinery and the re-record steps
in ``tests/incidents/README.md``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from _harness import assert_incident_snapshot

if TYPE_CHECKING:
    from collections.abc import Callable

    from hostlens.agent.backend import LLMBackend


async def test_fd_exhaustion(llm_cassette: Callable[..., LLMBackend]) -> None:
    backend = llm_cassette("incident_fd_exhaustion")
    await assert_incident_snapshot("fd_exhaustion", backend)
