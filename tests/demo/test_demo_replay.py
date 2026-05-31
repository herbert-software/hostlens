"""Demo replay integration test — asserts the *demo* render path + drift guard.

Spec: ``openspec/changes/add-demo-cli/specs/demo-cli-command/spec.md``
(§场景:对已知场景跑通离线回放 / §场景:不触达 API 的结构性保证 / D6 / D7).

This drives the real ``build_demo_planner`` assembly over the packaged assets
under the frozen tool clock (``FROZEN_DT`` baked into the assembly) and asserts
the **demo's actual render path** — ``render_planner_result(result, "md")``
(``cli/_intent.py``, the function ``demo run`` calls) — against a committed
demo-owned snapshot. It deliberately does NOT reuse
``tests/incidents/_harness.project_planner_result`` (design D6): that is a
test-private projection living in a no-``__init__`` dir, and it sorts / formats
findings differently from the function demo run actually invokes, so it would
oracle a path demo never walks and mask finding-order jitter in the real path.

The second assertion — ``replay_target.misses == []`` — is the structural proof
that the assembly produced a cassette request key byte-identical to the
recording (design D7 invariant): a non-empty ``misses`` would mean the demo
assembly diverged from the recording harness, so the run is not the
deterministic replay the demo promises.

``asyncio_mode = "auto"`` (pyproject) — no ``@pytest.mark.asyncio``; the backend
is a ``PlaybackBackend`` so there is no ``@pytest.mark.live``.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from hostlens.agent.backends.playback import PlaybackBackend
from hostlens.cli._intent import render_planner_result
from hostlens.demo.assembly import build_demo_planner
from hostlens.demo.registry import get_scenario

_SNAPSHOTS_DIR = Path(__file__).parent / "snapshots"


async def test_demo_cpu_saturation_render_matches_snapshot() -> None:
    """``render_planner_result`` (md) over cpu_saturation == committed snapshot.

    Also asserts ``replay_target.misses == []`` (D7 request-key invariant) and
    that the assembled backend is a ``PlaybackBackend`` (structural "no API"
    proof, §场景:不触达 API). The ``ExitStack`` holds the reader ``as_file``
    contexts for the whole run (design D2) and is closed only after
    ``PlannerAgent.run()`` returns.
    """

    scenario = get_scenario("cpu_saturation")
    assert scenario is not None

    with contextlib.ExitStack() as stack:
        planner, replay_target = build_demo_planner(scenario.key, exit_stack=stack)
        # Structural "never touches the API" proof: the only backend the demo
        # assembly wires is a PlaybackBackend (never an AnthropicAPIBackend).
        assert isinstance(planner._loop._backend, PlaybackBackend)
        result = await planner.run(scenario.intent)
        # D7 invariant — proven inside the ExitStack so the asset temp paths
        # are still valid for the diagnostic message if it ever fails.
        assert replay_target.misses == [], (
            f"cpu_saturation: ReplayTarget misses {replay_target.misses} — "
            "demo assembly diverged from the recording request key"
        )

    rendered = render_planner_result(result, "md")
    # ``render_planner_result`` emits no trailing newline; the committed snapshot
    # carries the repo's mandatory end-of-file newline (enforced by the
    # end-of-file-fixer hook), so normalize it away before the byte comparison.
    expected = (_SNAPSHOTS_DIR / "cpu_saturation.md").read_text(encoding="utf-8").rstrip("\n")
    assert rendered == expected


def test_demo_render_is_deterministic_across_runs() -> None:
    """Two independent assemblies render byte-identical md (frozen-clock guard).

    The committed snapshot is a *derived* oracle; this second run guards against
    finding-order / token jitter in ``render_planner_result`` under the frozen
    tool clock without depending on the snapshot file (D6: if order were
    unstable the snapshot alone would flap intermittently rather than fail
    deterministically).

    Sync test (own ``asyncio.run``) so it manages its event loop independently
    of the auto-mode loop the async tests run in.
    """

    import asyncio

    scenario = get_scenario("cpu_saturation")
    assert scenario is not None

    def _run() -> str:
        with contextlib.ExitStack() as stack:
            planner, replay_target = build_demo_planner(scenario.key, exit_stack=stack)
            result = asyncio.run(planner.run(scenario.intent))
            assert replay_target.misses == []
        return render_planner_result(result, "md")

    assert _run() == _run()
