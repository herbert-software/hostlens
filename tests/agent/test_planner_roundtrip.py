"""Record→replay round-trip determinism for the synthetic Planner scenario.

Task 6.3 / spec §需求:合成 fixture 必须字节稳定, record→replay 往返不得 miss
§场景:record 后立即 replay 同 scenario 不 miss.

This is the **offline** proof of the cassette loop — no real API. The
``RecordingBackend`` wraps a scripted ``FakeBackend`` inner (not
``AnthropicAPIBackend``), records the byte-stable synthetic multi-turn scenario
to a ``tmp_path`` cassette, then a ``PlaybackBackend`` replays the *same*
scenario over that file. Because the synthetic inputs are byte-stable and the
record / playback request keys come from the single-source
``request_key_for_payload`` helper, every replay turn must hit the freshly
recorded record — a ``CassetteMiss`` here would prove either a non-deterministic
synthetic input or a keying divergence.

``RecordingBackend.__init__`` is typed ``inner: AnthropicAPIBackend`` for
production clarity but consumes it structurally, so the ``FakeBackend`` is
``cast`` in (same pattern as ``tests/support/test_recording_backend.py``).

``asyncio_mode = "auto"`` (pyproject) — no ``@pytest.mark.asyncio`` needed.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from support.cassette_recording import _ACTIVE_CASSETTE_PATHS, RecordingBackend

from hostlens.agent.backends.playback import CassetteMiss, PlaybackBackend
from hostlens.agent.planner import PlannerAgent, PlannerResult

from ._scenario import (
    SCENARIO_INTENT,
    scenario_context_factory,
    scenario_fake_backend,
    scenario_settings,
    scenario_target_registry,
    scenario_tool_registry,
)

if TYPE_CHECKING:
    from hostlens.agent.backend import LLMBackend


@pytest.fixture(autouse=True)
def _clean_active_paths() -> Any:
    # The module-level active-path registry is process-wide; clear it around
    # each test so a leaked path from a sibling test never trips the
    # duplicate-path guard here.
    _ACTIVE_CASSETTE_PATHS.clear()
    yield
    _ACTIVE_CASSETTE_PATHS.clear()


async def _run_planner(backend: LLMBackend) -> PlannerResult:
    target_registry = scenario_target_registry()
    return await PlannerAgent(
        backend,
        scenario_tool_registry(),
        scenario_settings(),
        scenario_context_factory(target_registry),
    ).run(SCENARIO_INTENT)


async def test_record_then_replay_same_scenario_no_miss(tmp_path: Path) -> None:
    cassette_path = tmp_path / "rt.jsonl"

    # --- record: wrap the scripted FakeBackend, drive the scenario, flush. ---
    recorder = RecordingBackend(
        cassette_path=cassette_path,
        inner=cast("Any", scenario_fake_backend()),
    )
    recorded = await _run_planner(cast("LLMBackend", recorder))
    recorder.flush()

    # The scenario is two tool-use turns + one end_turn → three messages_create
    # calls, each with a distinct (growing-messages) request key.
    assert cassette_path.exists()
    lines = [
        line for line in cassette_path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert len(lines) == 3

    # --- replay: same scenario over the just-written cassette, must not miss. ---
    playback = PlaybackBackend(cassette_path=cassette_path)
    try:
        replayed = await _run_planner(cast("LLMBackend", playback))
    except CassetteMiss as exc:  # pragma: no cover - failure path makes the bug legible
        pytest.fail(
            "record→replay round-trip missed — synthetic input is not byte-stable "
            f"or keying diverged: {exc}"
        )

    # Round-trip determinism: replay reproduces the recorded condensed result.
    assert replayed.loop_result.terminal_status == "ok"
    assert recorded.loop_result.terminal_status == "ok"
    assert replayed.narrative == recorded.narrative
    assert [f.message for f in replayed.findings] == [f.message for f in recorded.findings]
    # Two tool-use turns, two stub findings each, preserved in order.
    assert [f.message for f in replayed.findings] == [
        "load average within normal range",
        "uptime 12 days",
        "load average within normal range",
        "uptime 12 days",
    ]
