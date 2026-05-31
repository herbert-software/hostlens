"""Drift detection for the double replay layer (M2.8 task 4.5).

Two guards, matching design D1:

1. **Unit level** — ``ReplayTarget.exec`` raises ``ReplayMiss`` for an
   unrecorded command and records the miss to ``self.misses``.
2. **Pipeline level** — when an Inspector's rendered command drifts away from a
   committed fixture (here simulated by dropping one main command), the full
   Planner pipeline does NOT silently produce the snapshot: ``target.misses``
   is non-empty. This is the strict-consumption signal that does not depend on
   the ``ReplayMiss`` exception bubbling (``ToolsAdapter.dispatch`` absorbs it;
   the divergent tool_result then also trips ``CassetteMiss`` on the next turn).
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING

import pytest
from _harness import (
    build_incident_planner_over_fixture,
)
from _scenarios import SCENARIOS_BY_KEY

from hostlens.agent.backends.playback import CassetteMiss
from hostlens.core.exceptions import ReplayMiss
from hostlens.demo.assets import source_tree_path
from hostlens.targets.replay import ReplayTarget

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from hostlens.agent.backend import LLMBackend


async def test_replay_target_exec_miss_is_loud(tmp_path: Path) -> None:
    fixture = {
        "impersonate": "local",
        "capabilities": ["shell"],
        "commands": [{"cmd": "echo recorded", "stdout": "recorded\n", "exit_code": 0}],
        "files": {},
    }
    fixture_path = tmp_path / "tiny.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    target = ReplayTarget("drift-host", fixture=fixture_path)

    with pytest.raises(ReplayMiss):
        await target.exec("echo NOT recorded", timeout=5)
    assert target.misses == [{"kind": "exec", "cmd": "echo NOT recorded"}]


async def test_command_drift_trips_strict_consumption(
    llm_cassette: Callable[..., LLMBackend],
    tmp_path: Path,
) -> None:
    # Start from the committed cpu_saturation fixture and drop the load_avg
    # main command — i.e. an Inspector command "changed" without re-recording
    # the fixture. Probes are kept so the drift is on the main command only.
    original = json.loads(source_tree_path("cpu_saturation", "fixture").read_text(encoding="utf-8"))
    drifted_commands = [c for c in original["commands"] if "loadavg" not in c["cmd"]]
    assert len(drifted_commands) == len(original["commands"]) - 1, "expected to drop one command"
    drifted = {**original, "commands": drifted_commands}
    fixture_path = tmp_path / "cpu_saturation_drifted.json"
    fixture_path.write_text(json.dumps(drifted), encoding="utf-8")

    backend = llm_cassette("incident_cpu_saturation")
    planner, target = build_incident_planner_over_fixture(backend, fixture_path=fixture_path)
    scenario = SCENARIOS_BY_KEY["cpu_saturation"]

    # The pipeline either raises (ReplayMiss absorbed → divergent tool_result →
    # CassetteMiss) or returns; either way strict-consumption MUST flag the
    # drift via target.misses.
    with contextlib.suppress(CassetteMiss, ReplayMiss):
        await planner.run(scenario.intent)

    assert target.misses != [], "command drift must be caught by strict-consumption"
