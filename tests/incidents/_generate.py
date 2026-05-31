"""Generator for incident-pack fixtures + cassettes + snapshots (M2.8 group 4).

This is the documented **re-record** procedure (group 5). It is an env-gated
pytest test so it runs inside the normal pytest sys.path / import setup but is
skipped in CI:

    HOSTLENS_GENERATE_INCIDENTS=1 pytest tests/incidents/_generate.py -q
    # one scenario only:
    HOSTLENS_GENERATE_INCIDENTS=1 HOSTLENS_GENERATE_ONLY=cpu_saturation \
        pytest tests/incidents/_generate.py -q

For each scenario it:

1. Builds the ReplayTarget fixture by running every inspector through the real
   ``InspectorRunner`` (frozen clock) against a ``_CaptureTarget`` that records
   each exact rendered command + canned failure stdout. No command is
   hand-computed — preflight probes + the rendered main command are captured
   verbatim, so the fixture can never drift from the rendered string.
2. Records the LLM cassette by driving the real Planner pipeline with a
   ``RecordingBackend`` wrapping a scripted ``FakeBackend`` — zero API key, the
   recorded requests are byte-identical to what ``PlaybackBackend`` replays.
3. Writes the deterministic snapshot from the recording run's result.

Both artifacts are committed; the snapshot tests only replay.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
import structlog
from _harness import (
    SNAPSHOTS_DIR,
    build_authored_responses,
    build_incident_planner,
    frozen_clock,
    project_planner_result,
)
from _scenarios import SCENARIOS, IncidentScenario
from support.cassette_recording import RecordingBackend

from hostlens.agent.backends.fake import FakeBackend
from hostlens.core.config import AgentSettings, Settings
from hostlens.demo.assets import source_tree_path
from hostlens.inspectors.registry import build_registry_from_search_paths
from hostlens.inspectors.runner import InspectorRunner
from hostlens.targets.base import Capability, ExecResult
from hostlens.targets.registry import TargetRegistry

_PROBE_PREFIX = "command -v "
_FILE_PROBE_PREFIX = "[ -r "


class _CaptureTarget:
    """Generation-only target: returns canned stdout and records every command.

    Binary probes (``command -v X``) succeed with a synthetic path; file
    probes (``[ -r P ]``) succeed empty; everything else is the inspector's
    main command and returns ``main_stdout``. Each call is appended to ``sink``
    so the fixture captures the exact rendered command strings.
    """

    type = "local"

    def __init__(
        self,
        name: str,
        *,
        capabilities: set[Capability],
        main_stdout: str,
        sink: list[dict[str, Any]],
    ) -> None:
        self.name = name
        self.capabilities = capabilities
        self._main_stdout = main_stdout
        self._sink = sink

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        del timeout, env
        if cmd.startswith(_PROBE_PREFIX):
            binary = cmd[len(_PROBE_PREFIX) :].strip().strip("'\"")
            stdout = f"/usr/bin/{binary}\n"
        elif cmd.startswith(_FILE_PROBE_PREFIX):
            stdout = ""
        else:
            stdout = self._main_stdout
        self._sink.append(
            {"cmd": cmd, "stdout": stdout, "stderr": "", "exit_code": 0, "duration_seconds": 0.0}
        )
        return ExecResult(
            exit_code=0, stdout=stdout, stderr="", duration_seconds=0.0, timed_out=False
        )

    async def read_file(self, path: str) -> bytes:  # pragma: no cover - unused by these inspectors
        raise AssertionError(f"_CaptureTarget.read_file unexpectedly called: {path!r}")


async def _build_fixture(scenario: IncidentScenario) -> None:
    settings = Settings(agent=AgentSettings())
    logger = structlog.get_logger("incident-generate")
    inspector_registry = build_registry_from_search_paths([], settings=settings).registry

    # Capability union across the scenario's inspectors (every one needs shell).
    cap_values: set[str] = {"shell"}
    for call in scenario.inspectors:
        manifest = inspector_registry.get(call.name)
        cap_values |= set(manifest.requires_capabilities)
    capabilities = {Capability(value) for value in cap_values}

    recorded: list[dict[str, Any]] = []
    runner = InspectorRunner(TargetRegistry(), settings=settings, logger=logger, clock=frozen_clock)
    for call in scenario.inspectors:
        manifest = inspector_registry.get(call.name)
        target = _CaptureTarget(
            "capture-host",
            capabilities=capabilities,
            main_stdout=call.main_stdout,
            sink=recorded,
        )
        params = dict(call.params)
        result = await runner.run(manifest, target, parameters=params)
        # Generation sanity: the canned stdout MUST drive the inspector to a
        # finding, otherwise the snapshot would be empty and the fixture wrong.
        assert result.status == "ok", f"{call.name}: status={result.status} error={result.error}"
        assert result.findings, f"{call.name}: produced no findings — check main_stdout"

    # Dedup by command (ReplayTarget rejects duplicate command keys on load).
    commands: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in recorded:
        if entry["cmd"] in seen:
            continue
        seen.add(entry["cmd"])
        commands.append(entry)

    fixture = {
        "impersonate": "local",
        "capabilities": sorted(cap_values),
        "commands": commands,
        "files": {},
    }
    # Writer #1: write the committed asset in the source tree (NOT an as_file
    # read-only temp copy). basename is now ``<key>/fixture.json`` (design D2).
    fixture_path = source_tree_path(scenario.key, "fixture")
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    fixture_path.write_text(
        json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


async def _record_cassette_and_snapshot(scenario: IncidentScenario) -> None:
    # Writer #1 (cassette half): source-tree path, now ``<key>/cassette.jsonl``.
    cassette_path = source_tree_path(scenario.key, "cassette")
    cassette_path.parent.mkdir(parents=True, exist_ok=True)
    recorder = RecordingBackend(
        cassette_path=cassette_path,
        inner=FakeBackend(responses=build_authored_responses(scenario)),  # type: ignore[arg-type]
    )
    planner, target = build_incident_planner(recorder, fixture_name=scenario.key)
    result = await planner.run(scenario.intent)
    recorder.flush(persist=True)

    assert target.misses == [], f"{scenario.key}: ReplayTarget misses {target.misses}"
    assert result.findings, f"{scenario.key}: planner produced no findings"
    assert cassette_path.exists(), f"{scenario.key}: cassette not written (sensitive gate?)"

    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_path = SNAPSHOTS_DIR / f"{scenario.key}.md"
    snapshot_path.write_text(project_planner_result(result), encoding="utf-8")


def _selected_scenarios() -> list[IncidentScenario]:
    only = os.environ.get("HOSTLENS_GENERATE_ONLY")
    if not only:
        return list(SCENARIOS)
    keys = {k.strip() for k in only.split(",") if k.strip()}
    return [s for s in SCENARIOS if s.key in keys]


@pytest.mark.skipif(
    not os.environ.get("HOSTLENS_GENERATE_INCIDENTS"),
    reason="generator is opt-in via HOSTLENS_GENERATE_INCIDENTS=1",
)
async def test_generate_incident_artifacts() -> None:
    scenarios = _selected_scenarios()
    assert scenarios, "HOSTLENS_GENERATE_ONLY matched no scenarios"
    for scenario in scenarios:
        await _build_fixture(scenario)
        await _record_cassette_and_snapshot(scenario)
