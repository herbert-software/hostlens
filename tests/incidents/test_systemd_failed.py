"""Incident snapshot: systemd failed units.

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


async def test_systemd_failed(llm_cassette: Callable[..., LLMBackend]) -> None:
    backend = llm_cassette("incident_systemd_failed")
    await assert_incident_snapshot("systemd_failed", backend)
