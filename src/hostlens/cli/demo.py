"""``hostlens demo`` Typer subcommand group — offline scenario replay.

Spec: ``openspec/changes/wire-demo-to-report/specs/demo-cli-command/spec.md``.

``demo run <scenario>`` runs the full Planner → Diagnostician pipeline
(``ReplayTarget`` + ``PlaybackBackend``) over a packaged incident scenario,
assembles a faithful first-class ``Report`` via the shared
``run_diagnosis_pipeline`` core (design D1), and renders the intent-style report
(narrative + ``## Findings`` + ``## 根因假设``) to stdout. It is fully
self-contained: no real Anthropic API call, no SSH / remote connection, no
``ANTHROPIC_API_KEY`` and no user ``targets.yaml`` required (design D3 / D7).
Progress streams to stderr via ``RichLiveObserver`` (default on; ``--quiet`` /
``--no-progress`` are two spellings of one boolean switch).

``demo list`` enumerates the scenario registry (the single SOT shared with
``demo run``).

Exit code contract (design D4, priority ``3 > 2 > 1 > 0``):

- ``0`` / ``1`` / ``2`` for a *successfully assembled* run — reuses
  ``inspect._compute_intent_report_exit_code`` over the assembled ``Report``
  (``meta.status`` ok + no critical → 0; ok + ≥1 critical → 1; any degraded
  ``meta.status`` — ``degraded_*`` / ``empty_response`` / ``partial`` → 2).
- ``2`` also covers the no-result path (empty collector → ``None`` Report),
  assembly-phase asset corruption (``PlaybackBackend`` ``ValueError`` on bad
  cassette JSON / ``ReplayTarget`` ``ConfigError`` on bad fixture schema),
  runtime Agent drift (``CassetteMiss`` / ``ReplayMiss``) in EITHER loop, and
  ``--persist`` store failure (raise → ``internal:`` / orphan → ``warning:``,
  reusing the ``--intent`` ``(orphaned or persist_failed) and exit_code in (0,1)``
  escalation).
- ``3`` is demo's own caller boundary: unknown scenario / missing packaged
  asset (both caught by the pre-flight check *before* assembly) and ``--output``
  write failure.

A Python traceback never reaches the user: any unexpected exception is wrapped
as a single ``internal: <kind>: <msg>`` stderr line (mirrors ``inspect.py``).
The pre-flight asset resolution runs *before* assembly so an unknown scenario or
un-packaged asset is reliably exit 3, not exit 2 (design D4).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

import click
import structlog
import typer

from hostlens.cli._intent import (
    RichLiveObserver,
    _seeding_sort_key,
    render_intent_report,
    run_diagnosis_pipeline,
)
from hostlens.cli.inspect import (
    _compute_intent_report_exit_code,
    _persist_report,
)
from hostlens.core.logging import configure_logging
from hostlens.demo.assembly import DEMO_TARGET_NAME, _frozen_clock, build_demo_pipeline
from hostlens.demo.assets import asset_exists
from hostlens.demo.registry import DemoScenario, get_scenario, list_scenarios

if TYPE_CHECKING:
    from hostlens.agent.backend import LLMBackend
    from hostlens.reporting.models import Report

__all__ = ["app"]


app = typer.Typer(
    name="demo",
    help="Offline demo scenarios (replay-only, no API key / network required).",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _root() -> None:
    """Force Typer into multi-command mode so ``run`` / ``list`` stay addressable.

    Without an explicit callback a Typer app with a single ``@app.command``
    collapses into single-command mode and the subcommand name disappears from
    ``--help`` — same guard used by ``hostlens inspectors``.
    """


# --------------------------------------------------------------------------- #
# structlog stderr redirect (mirrors inspect.py so stdout stays a clean report)
# --------------------------------------------------------------------------- #


def _redirect_structlog_to_stderr() -> None:
    """Re-bind the structlog factory to ``sys.stderr`` at WARNING+.

    Identical posture to ``inspect.py._redirect_structlog_to_stderr``: the
    default ``PrintLoggerFactory`` writes to stdout, which would contaminate the
    rendered report. We raise the level filter to WARNING so happy-path info
    events stay silent. The caller restores the saved config in a ``finally``.
    """

    current = structlog.get_config()
    structlog.configure(
        processors=current["processors"],
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
        context_class=current["context_class"],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )


# --------------------------------------------------------------------------- #
# Output emission (mirrors inspect.py._emit_output)
# --------------------------------------------------------------------------- #


def _emit_output(rendered: str, output: str | None) -> None:
    """Write ``rendered`` to ``output`` (if given) or stdout.

    ``--output`` write failures map to exit 3 (caller boundary, design D8) with
    a single stderr line; stdout stays silent on that path so a partial
    file-or-stdout interleave never happens.
    """

    if output is None:
        sys.stdout.write(rendered)
        if not rendered.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
        return

    out_path = Path(output)
    try:
        out_path.write_text(
            rendered if rendered.endswith("\n") else rendered + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        typer.echo(f"failed to write output: {exc}", err=True)
        raise typer.Exit(code=3) from exc


# --------------------------------------------------------------------------- #
# Pre-flight asset resolution (runs BEFORE assembly — design D8)
# --------------------------------------------------------------------------- #


def _preflight(scenario: str) -> DemoScenario:
    """Resolve ``scenario`` to a registered ``DemoScenario``, or exit 3.

    Two failure modes, both exit 3 (caller boundary):

    - Unknown scenario (normalized key not in the registry) — emits the
      spec-locked ``unknown scenario`` hint pointing at ``demo list``.
    - Missing packaged asset (``asset_exists`` false for fixture or cassette via
      the ``Traversable.is_file`` API) — emits a ``missing scenario asset`` line.

    Running this *before* assembly is what lets us distinguish exit 3 (asset
    absent / unknown) from exit 2 (asset present but corrupt → assembly raises).
    """

    found = get_scenario(scenario)
    if found is None:
        typer.echo(
            f"unknown scenario: {scenario}; run 'hostlens demo list'",
            err=True,
        )
        raise typer.Exit(code=3)
    if not (asset_exists(found.key, "fixture") and asset_exists(found.key, "cassette")):
        typer.echo(f"missing scenario asset: {found.key}", err=True)
        raise typer.Exit(code=3)
    return found


def _preflight_output(output: str | None) -> None:
    """Fail fast (exit 3) if ``--output`` cannot be written, before any assembly.

    Mirrors the asset pre-flight (design D8): the write target is a caller-side
    usage error, so it must be exit 3 and surface before the progress stream /
    agent run rather than after a full replay.
    """

    if output is None:
        return
    out_path = Path(output)
    parent = out_path.parent
    if (
        out_path.is_dir()
        or not parent.is_dir()
        or not os.access(parent, os.W_OK)
        or (out_path.exists() and not os.access(out_path, os.W_OK))
    ):
        typer.echo(f"failed to write output: {output}", err=True)
        raise typer.Exit(code=3)


# --------------------------------------------------------------------------- #
# `hostlens demo run`
# --------------------------------------------------------------------------- #


@app.command("run")
def run_cmd(
    scenario: str = typer.Argument(
        ...,
        help="Scenario key (from `hostlens demo list`). kebab-case is normalized.",
    ),
    output: str | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Write the rendered report to FILE instead of stdout.",
    ),
    fmt: str = typer.Option(
        "md",
        "--format",
        "-f",
        help="Output format: 'md' or 'json'.",
        click_type=click.Choice(["md", "json"]),
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "--no-progress",
        help="Suppress the live Agent progress stream (report still renders).",
    ),
    persist: bool = typer.Option(
        False,
        "--persist",
        help=(
            "Save the assembled Report to the REAL report store "
            "($XDG_DATA_HOME/hostlens/reports.db, same store 'hostlens reports' "
            "reads). Off by default; the demo Report is tagged target_name="
            "'demo:<scenario>'."
        ),
    ),
) -> None:
    """Replay a packaged incident scenario through the full diagnosis pipeline.

    The scenario runs entirely offline (``ReplayTarget`` + ``PlaybackBackend``):
    Planner → Diagnostician → a faithful first-class ``Report`` (intent-style
    render: narrative + ``## Findings`` + ``## 根因假设``). Progress streams to
    stderr (unless ``--quiet`` / ``--no-progress``) and the rendered report goes
    to stdout (or ``--output``). Exit codes follow the 4-value contract
    documented at the module level (design D4).

    The one telemetry line carries the cassette's RECORDED token counts (a replay
    snapshot value), NOT a real billing measurement — the demo issues no API call.

    Exit codes:
      0  healthy (``meta.status`` ok, no critical finding) — note: the 8 bundled
         incident scenarios all contain a critical finding by design, so a
         successful replay normally exits 1, not 0.
      1  replay succeeded and the Report contains >=1 critical finding (expected
         for the bundled scenarios)
      2  degraded ``meta.status`` / no-result / corrupt asset / replay drift /
         persist failure (``--persist``)
      3  usage error: unknown scenario, missing packaged asset, --output unwritable
    """

    # Pre-flight BEFORE assembly so unknown/missing → exit 3, corrupt → exit 2.
    resolved = _preflight(scenario)
    _preflight_output(output)

    saved_structlog_config = structlog.get_config()
    try:
        # demo is self-contained: never read HOSTLENS_* env / user config (D7).
        # _redirect_structlog_to_stderr() overrides the factory immediately after,
        # so the fixed "prod" mode only sets up the processor chain.
        configure_logging("prod")
        _redirect_structlog_to_stderr()

        observer = None if quiet else RichLiveObserver()
        try:
            report = _run_scenario(resolved.key, resolved.intent, observer)
        finally:
            # fail-loud loop paths don't emit RunFinalized, so close the Live
            # region here regardless of success / degrade / raise.
            if observer is not None:
                observer.close()

        if report is None:
            # No-result path (collector empty — the demo's normal replay never
            # hits this). No Report → nothing to render or persist; one-line
            # degrade reason, empty stdout, skip persist, exit 2 (mirrors
            # --intent).
            typer.echo(
                "hostlens demo: degraded run (no inspector results collected); no report produced",
                err=True,
            )
            raise typer.Exit(code=2)

        # Persist BEFORE rendering (only on explicit --persist) so the Report is
        # on disk even if a later render step fails — mirrors the --intent Report
        # seam (inspect.py:638). no-result already returned above, so persistence
        # is never silently skipped as a fake success.
        persist_failed = False
        orphaned = False
        if persist:
            try:
                orphaned = _persist_report(report)
            except Exception as exc:
                kind = type(exc).__name__
                typer.echo(f"internal: failed to persist report: {kind}: {exc}", err=True)
                persist_failed = True

        rendered = render_intent_report(report, fmt)
        _emit_output(rendered, output)

        exit_code = _compute_intent_report_exit_code(report)
        # Reuse the --intent Report seam: an orphan/persist-fail escalates a
        # healthy (0) OR critical (1) run to 2 — the ``in (0, 1)`` predicate (NOT
        # the --inspector ``== 0`` variant), so a critical-finding demo that
        # orphans still surfaces the persist degradation.
        if (orphaned or persist_failed) and exit_code in (0, 1):
            exit_code = 2
        if exit_code != 0:
            if exit_code == 2 and not (orphaned or persist_failed):
                status = report.meta.status if report.meta is not None else "unknown"
                typer.echo(
                    f"hostlens demo: degraded run (status={status})",
                    err=True,
                )
            raise typer.Exit(code=exit_code)
    except typer.Exit:
        # Re-raise verbatim so the explicit exit codes (here / _emit_output)
        # drive the process exit status.
        raise
    except (KeyboardInterrupt, asyncio.CancelledError, RuntimeError) as exc:
        # Agent cancellation propagates ``asyncio.CancelledError`` verbatim
        # (BaseException, not caught by ``except Exception``); ``asyncio.run``
        # may surface KeyboardInterrupt; ``_run_scenario`` raises RuntimeError on
        # replay drift. Wrap all as a single internal line, never a traceback.
        typer.echo(f"internal: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        # CLI boundary: assembly-phase ValueError (bad cassette) / ConfigError
        # (bad fixture) and runtime CassetteMiss / ReplayMiss all land here as a
        # single ``internal: <kind>: <msg>`` line → exit 2 (design D8). Never a
        # Python traceback.
        typer.echo(f"internal: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    finally:
        # Restore the structlog snapshot so the stderr-bound factory does not
        # outlive this command (matters under pytest's in-process CliRunner).
        structlog.configure(**saved_structlog_config)


def _order_findings_for_demo(report: Report) -> Report:
    """Pin the demo's findings render order deterministically (design D-7 extension).

    Final ``Report.findings`` order follows ``InspectorResultCollector`` append
    order = parallel-inspector handler completion order, which the collector
    declares not stable across runs (the runner's ``asyncio.to_thread`` DSL eval
    can genuinely reorder it). The diagnosis phase is already pinned by the D-7
    seed sort; extend the SAME key to the user-facing render so the offline demo
    is byte-stable across environments. Demo-local — deliberately NOT in the
    shared ``render_intent_report`` (which also serves ``--intent``, out of scope).
    """
    return report.model_copy(update={"findings": sorted(report.findings, key=_seeding_sort_key)})


def _run_scenario(
    scenario_key: str, intent: str, observer: RichLiveObserver | None
) -> Report | None:
    """Assemble + run the full demo pipeline for ``scenario_key`` in an ExitStack.

    Runs Planner → Diagnostician → ``Report`` via the shared
    ``run_diagnosis_pipeline`` core (design D1), over the demo's offline
    ``PlaybackBackend`` + frozen clock. Returns the assembled ``Report`` (or
    ``None`` on the no-result path).

    The ``ExitStack`` MUST wrap the WHOLE ``run_diagnosis_pipeline`` call (both
    loops + seed + assembly + the ``misses`` assertion): the single
    ``PlaybackBackend`` / ``ReplayTarget`` reads its packaged-asset reader across
    both loops, so closing the reader early would make the Diagnostician's
    ``messages_create`` miss (design D2 lifecycle). After the run we assert
    ``replay_target.misses == []`` — the request-key / strict-consumption drift
    guard (design D7): a non-empty ``misses`` means the assembly diverged from
    the recording, so the run is not the deterministic replay the demo promises.

    The demo-side D1 invariant is also asserted here: the diagnostician lookup
    name (``DEMO_TARGET_NAME``) MUST equal the ``ReplayTarget`` registry name, so
    a future ``request_more_inspection`` cassette would resolve the target rather
    than miss (the core deliberately does NOT hard-code this — that would break
    the real ``--intent`` target).
    """

    with contextlib.ExitStack() as stack:
        backend, context_factory, replay_target, settings = build_demo_pipeline(
            scenario_key, exit_stack=stack
        )
        # D-1 invariant: the diagnostician lookup name MUST equal the name the
        # ReplayTarget is actually registered under, so a future
        # ``request_more_inspection`` cassette resolves the target instead of
        # missing. Assert the real registered name, not just the literal constant.
        assert replay_target.name == DEMO_TARGET_NAME
        report = asyncio.run(
            run_diagnosis_pipeline(
                cast("LLMBackend", backend),
                settings,
                context_factory,
                report_target_name=f"demo:{scenario_key}",
                target_lookup_name=DEMO_TARGET_NAME,
                target_type=replay_target.type,
                intent=intent,
                tool_clock=_frozen_clock,
                observer=observer,
            )
        )
        if replay_target.misses:
            raise RuntimeError(
                f"replay drift: {len(replay_target.misses)} unmatched command(s) "
                f"for scenario {scenario_key}"
            )
    return _order_findings_for_demo(report) if report is not None else None


# --------------------------------------------------------------------------- #
# `hostlens demo list`
# --------------------------------------------------------------------------- #


@app.command("list")
def list_cmd() -> None:
    """List available demo scenarios (key + one-line description, registry SOT)."""

    scenarios = list_scenarios()
    if not scenarios:
        typer.echo("无可用场景")
        return
    for scenario in scenarios:
        typer.echo(f"{scenario.key}\t{scenario.description}")
