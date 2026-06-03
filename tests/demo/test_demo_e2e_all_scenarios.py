"""8-scenario full-chain offline-replay determinism guard (wire-demo-to-report task 3.2).

Spec: ``openspec/changes/wire-demo-to-report/specs/demo-cli-command/spec.md``
(§场景:对已知场景跑通离线回放 / §场景:不触达 API 的结构性保证 / partial 永不触发 /
design D-2 / D-3.6 / D-7).

This generalizes the single ``cpu_saturation`` full-chain test
(``test_demo_replay.py``) to **all 8 bundled scenarios**, parametrized. It is the
**resident CI guard for design D-3.6**: if a future change re-records a scenario's
Planner asset but forgets the Diagnostician record, the seeded findings drift →
the diagnosis phase hits a ``CassetteMiss`` → this test goes red immediately
(rather than silently shipping a 0-hypothesis demo).

Three guards live here:

1. **Per-scenario full-chain replay** — every scenario, driven through the real
   ``build_demo_pipeline`` + ``run_diagnosis_pipeline`` (Planner → Diagnostician
   → ``Report``) under the frozen tool clock, asserts: backend is a
   ``PlaybackBackend`` (structural "never touches the API" proof), the diagnosis
   phase produces no ``CassetteMiss`` (``replay_target.misses == []`` across BOTH
   loops), the ``Report`` carries ≥1 root-cause hypothesis (liveness, not quality),
   and the exit code matches ``_compute_intent_report_exit_code(report)`` and is in
   ``{0, 1}`` (a successfully assembled, non-degraded run — never 2).
2. **fixture ``exit_code == 0`` traversal** — every command in every scenario's
   ``fixture.json`` has ``exit_code == 0``. This is the resident guard for the
   spec's "the 8 demos never trigger ``partial``" claim: if a future scenario
   adds a non-zero-exit command, an inspector would replay non-ok →
   ``_derive_report_status`` → ``partial`` → exit 2, and this assertion goes red
   *before* the e2e exit-code expectation does. (``dependency_unreachable``'s
   "unreachable" is an application-level finding, not an inspector-level
   ``target_unreachable``, so its inspector commands still exit 0 — included.)
3. **second-consumer zero-regression** — the 8 ``tests/incidents/test_<key>.py``
   reuse the SAME (now larger) cassette but run Planner-only via
   ``assert_incident_snapshot``; ``PlaybackBackend`` matches by request key and
   does not force full consumption, so the appended diagnosis record is never
   looked up and the incident snapshots stay byte-stable. That zero-regression is
   guarded by those existing tests (NOT modified here); this file only confirms
   the cassettes exist and asserts the demo (second consumer) reads them cleanly.

``asyncio_mode = "auto"`` (pyproject) — no ``@pytest.mark.asyncio``; every backend
is a ``PlaybackBackend`` so there is no ``@pytest.mark.live`` and nothing hits the
network.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, cast

import pytest

from hostlens.agent.backends.playback import PlaybackBackend
from hostlens.cli._intent import run_diagnosis_pipeline
from hostlens.cli.inspect import _compute_intent_report_exit_code
from hostlens.demo.assembly import DEMO_TARGET_NAME, _frozen_clock, build_demo_pipeline
from hostlens.demo.assets import asset_exists, reader_path
from hostlens.demo.registry import get_scenario, list_scenarios

if TYPE_CHECKING:
    from hostlens.agent.backend import LLMBackend

# The 8 bundled scenario keys, taken from the registry SOT (the same set
# ``demo run`` / ``demo list`` accept) so this guard tracks the registry without
# a hand-maintained literal list.
SCENARIO_KEYS = sorted(scenario.key for scenario in list_scenarios())


@pytest.mark.parametrize("scenario_key", SCENARIO_KEYS)
async def test_full_chain_replay_is_deterministic(scenario_key: str) -> None:
    """Each scenario replays offline to a ≥1-hypothesis Report with no drift.

    Drives the real demo full-chain (Planner → Diagnostician → ``Report``) over
    the packaged assets under the frozen clock and asserts the D-3.6 contract:
    PlaybackBackend, ``misses == []`` (no ``CassetteMiss`` in either loop), ≥1
    hypothesis (liveness), and an exit code that is the SHARED ``--intent`` mapper
    over the assembled Report — in ``{0, 1}`` (assembled + non-degraded, never 2)
    rather than a hard-coded per-scenario value.
    """

    scenario = get_scenario(scenario_key)
    assert scenario is not None

    with contextlib.ExitStack() as stack:
        backend, context_factory, replay_target, settings = build_demo_pipeline(
            scenario_key, exit_stack=stack
        )
        # Structural "never touches the API" proof: the only backend the demo
        # assembly wires is a PlaybackBackend (offline cassette replay).
        assert isinstance(backend, PlaybackBackend)
        report = await run_diagnosis_pipeline(
            cast("LLMBackend", backend),
            settings,
            context_factory,
            report_target_name=f"demo:{scenario_key}",
            target_lookup_name=DEMO_TARGET_NAME,
            target_type=replay_target.type,
            intent=scenario.intent,
            tool_clock=_frozen_clock,
        )
        # D-3.6 / D-7 drift guard: a non-empty ``misses`` means a CassetteMiss /
        # ReplayMiss desynced the request key in either loop. Asserted inside the
        # ExitStack so the asset temp paths are still valid if the message fails.
        assert replay_target.misses == [], (
            f"{scenario_key}: ReplayTarget misses {replay_target.misses} — "
            "demo assembly diverged from the recorded request key (D-3.6 drift)"
        )

    assert report is not None
    assert report.meta is not None
    assert report.meta.target_name == f"demo:{scenario_key}"
    # The 8 bundled scenarios replay every inspector to ok (no partial).
    assert report.meta.status == "ok", f"{scenario_key}: unexpected status {report.meta.status}"

    # ≥1 hypothesis liveness (design D-3.5): the diagnosis phase must have recorded
    # at least one root-cause hypothesis — guards "recorded a 0-hypothesis
    # cassette", NOT hypothesis quality.
    assert len(report.hypotheses) >= 1, f"{scenario_key}: Report carries no hypothesis"

    # Exit code = the SHARED --intent mapper over the assembled Report; for an
    # assembled + non-degraded demo it must be 0 (healthy) or 1 (critical finding),
    # never 2 (which would mean a degraded status slipped through).
    exit_code = _compute_intent_report_exit_code(report)
    assert exit_code in (0, 1), f"{scenario_key}: unexpected exit code {exit_code}"


def test_all_fixture_commands_exit_zero() -> None:
    """Every command in every scenario ``fixture.json`` has ``exit_code == 0``.

    Resident guard for the spec's "the 8 bundled demos never trigger ``partial``"
    claim: a non-zero-exit fixture command would make an inspector replay non-ok →
    ``_derive_report_status`` → ``partial`` → exit 2. Traversing the fixtures
    (rather than eyeballing) means a future scenario that adds a non-zero-exit
    command turns this red immediately. ``dependency_unreachable`` is included:
    its "unreachable" is an application-level finding, not an inspector-level
    ``target_unreachable``, so its inspector commands still exit 0.
    """

    for scenario_key in SCENARIO_KEYS:
        with reader_path(scenario_key, "fixture") as fixture_path:
            fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        commands = fixture["commands"]
        assert commands, f"{scenario_key}: fixture has no commands"
        for command in commands:
            assert command["exit_code"] == 0, (
                f"{scenario_key}: command {command['cmd']!r} has non-zero exit_code "
                f"{command['exit_code']} — would derive a partial Report (exit 2)"
            )


def test_all_scenarios_have_both_packaged_assets() -> None:
    """Each scenario ships both a fixture and a cassette (second-consumer base).

    The second-consumer zero-regression (the 8 ``tests/incidents/test_<key>.py``
    reading the SAME cassette Planner-only) is enforced by those existing tests,
    which this file deliberately does NOT modify. Here we only confirm the shared
    assets are present for every registered scenario, so the demo (second consumer
    of the now-larger cassette) and the incident snapshots both have something to
    read — a missing asset would surface as a clear failure here rather than a
    confusing downstream ``CassetteMiss``.
    """

    for scenario_key in SCENARIO_KEYS:
        assert asset_exists(scenario_key, "fixture"), f"{scenario_key}: missing fixture asset"
        assert asset_exists(scenario_key, "cassette"), f"{scenario_key}: missing cassette asset"
