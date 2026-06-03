"""Demo replay integration test — asserts the *demo* render path + drift guard.

Spec: ``openspec/changes/wire-demo-to-report/specs/demo-cli-command/spec.md``
(§场景:对已知场景跑通离线回放 / §场景:不触达 API 的结构性保证 / D4 / D7).

This drives the real ``build_demo_pipeline`` + ``run_diagnosis_pipeline``
full-chain (Planner → Diagnostician → ``Report``) over the packaged assets under
the frozen tool clock (``FROZEN_DT`` baked into the assembly) and asserts the
**demo's actual render path** — ``render_intent_report(report, "md")``
(``cli/_intent.py``, the function ``demo run`` calls) — against a committed
demo-owned snapshot. The snapshot is the intent-style render (narrative +
``## Findings`` + ``## 根因假设``), NOT the old Planner-only ``PlannerResult``
projection.

The ``replay_target.misses == []`` assertion is the structural proof that the
assembly produced a cassette request key byte-identical to the recording (design
D7 invariant) across BOTH loops: a non-empty ``misses`` would mean the demo
assembly diverged from the recording harness, so the run is not the
deterministic replay the demo promises. The diagnosis phase only calls
``correlate_findings`` (design D3), so it touches no target command — ``misses``
must stay empty.

``asyncio_mode = "auto"`` (pyproject) — no ``@pytest.mark.asyncio``; the backend
is a ``PlaybackBackend`` so there is no ``@pytest.mark.live``.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import cast

from hostlens.agent.backends.playback import PlaybackBackend
from hostlens.cli._intent import render_intent_report, run_diagnosis_pipeline
from hostlens.cli.demo import _order_findings_for_demo
from hostlens.demo.assembly import DEMO_TARGET_NAME, _frozen_clock, build_demo_pipeline
from hostlens.demo.registry import get_scenario

_SNAPSHOTS_DIR = Path(__file__).parent / "snapshots"


async def test_demo_cpu_saturation_render_matches_snapshot() -> None:
    """``render_intent_report`` (md) over cpu_saturation == committed snapshot.

    Also asserts ``replay_target.misses == []`` (D7 request-key invariant across
    both loops) and that the assembled backend is a ``PlaybackBackend`` served to
    the whole pipeline (structural "no API" proof, §场景:不触达 API). The
    ``ExitStack`` holds the reader ``as_file`` contexts for the whole run (design
    D2) and is closed only after ``run_diagnosis_pipeline`` returns.
    """

    scenario = get_scenario("cpu_saturation")
    assert scenario is not None

    with contextlib.ExitStack() as stack:
        backend, context_factory, replay_target, settings = build_demo_pipeline(
            scenario.key, exit_stack=stack
        )
        # Structural "never touches the API" proof: the only backend the demo
        # assembly wires is a PlaybackBackend (never an AnthropicAPIBackend).
        assert isinstance(backend, PlaybackBackend)
        report = await run_diagnosis_pipeline(
            cast("object", backend),  # type: ignore[arg-type]
            settings,
            context_factory,
            report_target_name=f"demo:{scenario.key}",
            target_lookup_name=DEMO_TARGET_NAME,
            target_type=replay_target.type,
            intent=scenario.intent,
            tool_clock=_frozen_clock,
        )
        # D7 invariant — proven inside the ExitStack so the asset temp paths are
        # still valid for the diagnostic message if it ever fails.
        assert replay_target.misses == [], (
            f"cpu_saturation: ReplayTarget misses {replay_target.misses} — "
            "demo assembly diverged from the recording request key"
        )

    assert report is not None
    assert report.meta is not None
    assert report.meta.target_name == "demo:cpu_saturation"
    assert report.meta.status == "ok"
    # >=1 hypothesis liveness guard (design D-3.5): the diagnosis phase must have
    # recorded at least one root-cause hypothesis (not a 0-hypothesis cassette).
    assert len(report.hypotheses) >= 1

    report = _order_findings_for_demo(report)
    rendered = render_intent_report(report, "md")
    # ``render_intent_report`` emits no trailing newline; the committed snapshot
    # carries the repo's mandatory end-of-file newline (enforced by the
    # end-of-file-fixer hook), so normalize it away before the byte comparison.
    expected = (_SNAPSHOTS_DIR / "cpu_saturation.md").read_text(encoding="utf-8").rstrip("\n")
    assert rendered == expected


def test_demo_render_is_deterministic_across_runs() -> None:
    """Two independent full-chain runs render byte-identical md (frozen-clock guard).

    The committed snapshot is a *derived* oracle; this second run guards against
    finding-order / token jitter in ``render_intent_report`` under the frozen
    tool clock + the D-7 deterministic seeding sort, without depending on the
    snapshot file (D6: if order were unstable the snapshot alone would flap
    intermittently rather than fail deterministically).

    Sync test (own ``asyncio.run``) so it manages its event loop independently of
    the auto-mode loop the async tests run in.
    """

    import asyncio

    scenario = get_scenario("cpu_saturation")
    assert scenario is not None

    def _run() -> str:
        with contextlib.ExitStack() as stack:
            backend, context_factory, replay_target, settings = build_demo_pipeline(
                scenario.key, exit_stack=stack
            )
            report = asyncio.run(
                run_diagnosis_pipeline(
                    cast("object", backend),  # type: ignore[arg-type]
                    settings,
                    context_factory,
                    report_target_name=f"demo:{scenario.key}",
                    target_lookup_name=DEMO_TARGET_NAME,
                    target_type=replay_target.type,
                    intent=scenario.intent,
                    tool_clock=_frozen_clock,
                )
            )
            assert replay_target.misses == []
        assert report is not None
        report = _order_findings_for_demo(report)
        return render_intent_report(report, "md")

    assert _run() == _run()
