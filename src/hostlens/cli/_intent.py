"""``hostlens inspect --intent`` helpers — Planner assembly, Rich Live observer,
and ``PlannerResult`` rendering.

Spec: ``openspec/changes/add-intent-cli/specs/inspect-cli-command/spec.md``.

The Agent layer (``agent/``) stays Rich-free so it remains a pure, readable
demonstration of the hand-written loop (CLAUDE.md §4.1). Rich only enters at the
CLI boundary, so ``RichLiveObserver`` — the live progress sink that implements
``LoopObserver`` — lives here, not in ``agent/``.

Three concerns, three helpers:

- ``RichLiveObserver`` — renders the per-turn / per-tool progress tree to
  **stderr** (so stdout stays a clean report stream). ``on_event`` is wrapped in
  a blanket ``try/except`` that swallows rendering errors (degrading to silence)
  because the loop calls observers with no defensive try/except (design D-2/D-7):
  isolating a Rich glitch is the observer's own responsibility, and a render
  failure must never fail-loud the whole inspection.
- ``build_planner`` — wires ``create_backend`` + a default-tools ``ToolRegistry``
  + a ``ToolContext`` factory into a ``PlannerAgent``. The backend reaches only
  the ``PlannerAgent`` (→ ``AgentLoop``), never the ``ToolContext`` (ADR-008).
  Retained for ``demo run`` / cassette recording (Planner-only flows).
- ``run_intent_diagnosis`` — the ``--intent`` orchestration seam: calls
  ``create_backend`` ONCE, runs the Planner against a per-run
  ``InspectorResultCollector``, seeds the Diagnostician's ``FindingStore`` from
  the Planner-phase collector snapshot (ids stamped by ``compute_finding_id``,
  no registry re-lookup), runs the Diagnostician (reusing the same backend +
  collector + a restricted diagnostician ``ToolRegistry``), then snapshots the
  full collector AFTER the diagnosis loop and assembles a faithful first-class
  ``Report`` via ``Report.from_inspector_results`` (with the Diagnostician's
  hypotheses projected into ``Report.hypotheses`` and its narrative into
  ``Report.metadata["diagnosis_narrative"]``). Returns ``None`` on the
  **no-result** path (the collector is empty — zero ``InspectorResult``).
- ``render_planner_result`` — projects a ``PlannerResult`` to md / json (still
  used by ``demo run``).
- ``render_intent_report`` — projects the assembled ``Report`` to md / json for
  the ``--intent`` path (md is the intent-style renderer; NOT
  ``reporting.render_markdown``).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from rich.console import Console
from rich.live import Live
from rich.tree import Tree

from hostlens.agent.backend import create_backend
from hostlens.agent.events import (
    ModelResponded,
    RunFinalized,
    ToolCompleted,
    ToolStarted,
    TurnStarted,
)
from hostlens.agent.planner import PlannerAgent, PlannerResult
from hostlens.core.redact import redact_text
from hostlens.orchestration.pipeline import (
    _assemble_report,
    _seed_findings_from_snapshot,
    _seeding_sort_key,
    _sum_loop_usage,
    run_diagnosis_pipeline,
)
from hostlens.reporting._redact import redact_report_for_render
from hostlens.reporting.models import Report
from hostlens.reporting.render_json import render as render_json
from hostlens.tools.base import NoopApprovalService, ToolContext
from hostlens.tools.default_tools import register_default_tools
from hostlens.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from collections.abc import Callable

    import structlog

    from hostlens.agent.backend import LLMBackend
    from hostlens.agent.events import LoopEvent, LoopObserver
    from hostlens.core.config import Settings
    from hostlens.inspectors.registry import InspectorRegistry
    from hostlens.targets.registry import TargetRegistry

__all__ = [
    "RichLiveObserver",
    "_assemble_report",
    "_seed_findings_from_snapshot",
    "_seeding_sort_key",
    "_sum_loop_usage",
    "build_planner",
    "render_intent_report",
    "render_planner_result",
    "run_diagnosis_pipeline",
    "run_intent_diagnosis",
]

# Key under which the Diagnostician's narrative is projected into
# ``Report.metadata`` (a ``dict[str, str]``): json keeps it, persistence can
# retrieve it, and the intent-style md renderer reads it back (design D-6).
_DIAGNOSIS_NARRATIVE_KEY = "diagnosis_narrative"


# --------------------------------------------------------------------------- #
# RichLiveObserver
# --------------------------------------------------------------------------- #


class RichLiveObserver:
    """Live progress sink implementing ``LoopObserver`` (design D-7).

    Maintains a Rich ``Tree`` of turns → tool calls (ok / err) refreshed
    incrementally via ``Live`` bound to a **stderr** console, so the rendered
    report on stdout is never contaminated. Under a non-TTY stderr (e.g.
    pytest's ``CliRunner`` or a pipe) Rich auto-degrades to plain line output;
    we never force a TTY.

    ``on_event`` swallows every exception (design D-2/D-7): the loop emits
    events with no defensive try/except, so isolating a render glitch is this
    observer's own boundary responsibility — it must never raise back into the
    loop.
    """

    def __init__(self, console: Console | None = None) -> None:
        self._console = console if console is not None else Console(stderr=True)
        self._tree = Tree("agent run")
        self._live: Live | None = None
        # Per-turn Tree nodes, keyed by 1-based turn, so a tool node can attach
        # under the turn that dispatched it even with out-of-order parallel
        # ToolStarted events (loop guarantees turn-level order, not a total order).
        self._turn_nodes: dict[int, Tree] = {}
        self._tool_nodes: dict[str, Tree] = {}

    def on_event(self, event: LoopEvent) -> None:
        # design D-2/D-7: the loop emits events with no defensive try/except, so
        # a Rich render glitch must never fail-loud the inspection. Degrade to
        # silence — progress is non-essential UI.
        with contextlib.suppress(Exception):
            self._handle(event)

    def _handle(self, event: LoopEvent) -> None:
        match event:
            case TurnStarted(turn=turn):
                self._ensure_live()
                node = self._tree.add(f"turn {turn}")
                self._turn_nodes[turn] = node
                self._refresh()
            case ModelResponded(turn=turn, stop_reason=stop_reason, text=text):
                parent = self._turn_nodes.get(turn, self._tree)
                summary = f"model: stop_reason={stop_reason}"
                if text:
                    # Redact before previewing: the model narrative may restate a
                    # secret-bearing finding, and this is the only stderr surface
                    # that echoes free model text (tool output is never printed).
                    summary = f"{summary} — {_one_line(redact_text(text))}"
                parent.add(summary)
                self._refresh()
            case ToolStarted(turn=turn, tool_name=tool_name, tool_use_id=tool_use_id):
                parent = self._turn_nodes.get(turn, self._tree)
                # Redact the tool name: the loop emits ToolStarted *before* the
                # white-list check, so a model-hallucinated name (model-controlled
                # free text) reaches stderr. No-op for legitimate identifiers.
                node = parent.add(f"tool {redact_text(tool_name)} … running")
                self._tool_nodes[tool_use_id] = node
                self._refresh()
            case ToolCompleted(invocation=invocation):
                started_node = self._tool_nodes.get(invocation.tool_use_id)
                outcome = "err" if invocation.error is not None else "ok"
                label = f"tool {redact_text(invocation.tool_name)} … {outcome}"
                if started_node is not None:
                    started_node.label = label
                else:
                    self._tree.add(label)
                self._refresh()
            case RunFinalized(terminal_status=terminal_status, turns=turns):
                self._tree.add(f"finalized: {terminal_status} ({turns} turns)")
                self._refresh()
                self._stop()

    def _ensure_live(self) -> None:
        # Lazy start on the first event so a no-op run never opens a Live region.
        if self._live is None:
            self._live = Live(self._tree, console=self._console, refresh_per_second=8)
            self._live.start()

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._tree)

    def close(self) -> None:
        # fail-loud loop paths (ToolPolicyViolation 等) and CLI exception paths
        # never emit RunFinalized, so the CLI calls this in a finally to ensure
        # the Live region is torn down. Idempotent: _stop no-ops when Live is
        # already stopped or never started.
        self._stop()

    def _stop(self) -> None:
        # Best-effort teardown: clear ``_live`` FIRST so the observer is left in
        # a stopped state even if ``Live.stop()`` raises, then suppress the stop
        # error. ``close()`` runs in the CLI's ``finally``; a raising teardown
        # would mask the original planner exception per Python finally semantics.
        live, self._live = self._live, None
        if live is not None:
            with contextlib.suppress(Exception):
                live.stop()


def _one_line(text: str, *, limit: int = 120) -> str:
    """Collapse ``text`` to a single trimmed line for the progress tree."""
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


# --------------------------------------------------------------------------- #
# PlannerAgent assembly
# --------------------------------------------------------------------------- #


def build_planner(
    settings: Settings,
    target_registry: TargetRegistry,
    inspector_registry: InspectorRegistry,
    logger: structlog.stdlib.BoundLogger,
) -> PlannerAgent:
    """Assemble a ``PlannerAgent`` for the ``--intent`` path (design D-5).

    ``create_backend`` raises ``ConfigError`` when no backend is configured;
    the caller maps that to exit 3. The ``context_factory`` builds a fresh
    ``ToolContext`` (with a fresh ``asyncio.Event`` cancel token) per dispatch.
    The backend is handed only to ``PlannerAgent`` — never into the
    ``ToolContext`` — so a tool handler can never reach the LLM (ADR-008).
    """
    backend = create_backend(settings)

    registry = ToolRegistry()
    register_default_tools(registry)

    context_factory = _make_context_factory(settings, target_registry, inspector_registry, logger)

    return PlannerAgent(backend, registry, settings, context_factory)


def _make_context_factory(
    settings: Settings,
    target_registry: TargetRegistry,
    inspector_registry: InspectorRegistry,
    logger: structlog.stdlib.BoundLogger,
) -> Callable[[], ToolContext]:
    """Build a ``ToolContext`` factory closure (shared by Planner + Diagnostician).

    Each call produces a fresh ``ToolContext`` with its own ``asyncio.Event``
    cancel token. The backend is deliberately NOT a parameter here — it is never
    threaded into a ``ToolContext`` (ADR-008), so a tool handler can never reach
    the LLM. The Planner and Diagnostician share the SAME ``inspector_registry``
    instance so id stamping reads the same versions the Planner ran against
    (design D-3, no TOCTOU skew).
    """

    def context_factory() -> ToolContext:
        return ToolContext(
            target_registry=target_registry,
            inspector_registry=inspector_registry,
            config=settings,
            logger=logger,
            approval_service=NoopApprovalService(),
            cancel=asyncio.Event(),
        )

    return context_factory


# --------------------------------------------------------------------------- #
# Planner → Diagnostician orchestration
# --------------------------------------------------------------------------- #
#
# ``run_diagnosis_pipeline`` and its private pure-orchestration helpers now live
# in ``hostlens.orchestration.pipeline`` (design D-2: the Scheduler must depend on
# orchestration, not reach back into the CLI layer). They are re-exported here
# (see the top-of-module import) so the existing ``hostlens.cli._intent`` import
# paths — used by ``cli/demo.py``, the incidents generator, and the existing
# tests — keep working unchanged.


async def run_intent_diagnosis(
    settings: Settings,
    target: str,
    intent: str,
    target_registry: TargetRegistry,
    inspector_registry: InspectorRegistry,
    logger: structlog.stdlib.BoundLogger,
    *,
    target_type: str,
    observer: LoopObserver | None = None,
) -> Report | None:
    """``--intent`` thin wrapper over ``run_diagnosis_pipeline`` (design D-1).

    Builds the real backend (``create_backend(settings)`` — raises
    ``ConfigError`` mapped to exit 3 by the CLI when no backend is configured)
    and the real ``ToolContext`` factory over the live ``target_registry`` /
    ``inspector_registry``, then delegates to the shared core with
    ``report_target_name == target_lookup_name == target``, the production wall
    clock (``tool_clock=None``), and no Planner-result sink (``--intent`` does
    not record). Behaviour is byte-identical to the pre-refactor seam; the
    existing ``--intent`` tests are the regression guard.
    """
    backend: LLMBackend = create_backend(settings)
    context_factory = _make_context_factory(settings, target_registry, inspector_registry, logger)

    return await run_diagnosis_pipeline(
        backend,
        settings,
        context_factory,
        report_target_name=target,
        target_lookup_name=target,
        target_type=target_type,
        intent=intent,
        tool_clock=None,
        observer=observer,
    )


# --------------------------------------------------------------------------- #
# PlannerResult rendering
# --------------------------------------------------------------------------- #


def render_planner_result(result: PlannerResult, fmt: str) -> str:
    """Render a ``PlannerResult`` to ``md`` or ``json`` (design D-6).

    json: the verbatim ``PlannerResult`` serialization (narrative / findings /
    loop_result / intent) so downstream can parse it. md: the narrative
    (already markdown) + a findings summary + one telemetry line. Findings come
    straight from already-redacted ``Finding`` objects; this function adds no
    re-derivation and leaks no env vars (CLAUDE.md §4.4 / §7).
    """
    if fmt == "json":
        return result.model_dump_json(indent=2)

    parts: list[str] = [result.narrative.rstrip("\n")]

    if result.findings:
        parts.append("")
        parts.append("## Findings")
        for finding in result.findings:
            tags = f" [{', '.join(finding.tags)}]" if finding.tags else ""
            parts.append(f"- {finding.severity}: {finding.message}{tags}")

    loop = result.loop_result
    usage = loop.usage_totals
    parts.append("")
    parts.append(
        f"turns={loop.turns} status={loop.terminal_status} "
        f"tokens_in={usage.input_tokens} tokens_out={usage.output_tokens}"
    )
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Report rendering (intent-style)
# --------------------------------------------------------------------------- #


def render_intent_report(report: Report, fmt: str) -> str:
    """Render the assembled ``Report`` to ``md`` or ``json`` (design D-6, spec §需求).

    Both surfaces render from a redacted copy (``redact_report_for_render`` —
    the SAME boundary the ``--inspector`` Report render path uses, which masks
    ``metadata`` too so the projected diagnosis narrative is scrubbed): no md /
    json string leaks a secret pattern.

    json: the redacted ``Report`` serialization via ``render_json`` (which
    applies the redaction boundary itself; full field set, round-trippable by
    ``Report.model_validate_json``); the
    Diagnostician's outputs live in ``Report.hypotheses`` /
    ``Report.metadata[diagnosis_narrative]`` / ``Report.meta.status``.

    md: an **intent-style** renderer (deliberately NOT
    ``reporting.render_markdown`` — that one is for the mechanical ``--inspector``
    report: a fixed ``# Hostlens Inspection Report`` title + a meta table + a
    ``## Inspector Results`` raw-JSON dump, and it never reads ``metadata`` /
    renders the narrative). It emits: the diagnosis narrative (from
    ``metadata[diagnosis_narrative]``) + a ``## Findings`` summary (from
    ``Report.findings``) + a ``## 根因假设`` section (from ``Report.hypotheses``)
    + one telemetry line. Three tolerances the spec mandates:

    - **Empty narrative** (degraded paths carry ``""``, or the key is absent):
      render no narrative heading at all — never an empty title.
    - **No hypotheses**: emit the ``_暂无根因假设_`` placeholder.
    - **No findings**: skip the ``## Findings`` heading; narrative + 根因假设
      placeholder + telemetry still render.
    """
    if fmt == "json":
        # ``render_json`` applies ``redact_report_for_render`` itself, so it
        # takes the raw report (do not pre-redact — that would double-mask).
        return render_json(report)

    redacted = redact_report_for_render(report)

    parts: list[str] = []

    narrative = redacted.metadata.get(_DIAGNOSIS_NARRATIVE_KEY, "").rstrip("\n")
    if narrative:
        parts.append(narrative)

    if redacted.findings:
        if parts:
            parts.append("")
        parts.append("## Findings")
        for finding in redacted.findings:
            tags = f" [{', '.join(finding.tags)}]" if finding.tags else ""
            parts.append(f"- {finding.severity}: {finding.message}{tags}")

    if parts:
        parts.append("")
    parts.append("## 根因假设")
    if not redacted.hypotheses:
        parts.append("_暂无根因假设_")
    else:
        for h in redacted.hypotheses:
            parts.append("")
            parts.append(f"### {h.description}")
            parts.append(f"- **Confidence:** {h.confidence}")
            if h.supporting_findings:
                parts.append(f"- **Supporting findings:** {', '.join(h.supporting_findings)}")
            if h.suggested_actions:
                parts.append("- **Suggested actions:**")
                for action in h.suggested_actions:
                    parts.append(f"  - {action}")

    # Telemetry: one line drawn from the assembled Report.meta — turns is not a
    # Report field (the loop counters were summed into token_usage), so the line
    # reports status + token totals (the whole intent run, both loops).
    meta = redacted.meta
    parts.append("")
    if meta is not None:
        usage = meta.token_usage
        parts.append(
            f"status={meta.status} tokens_in={usage.input_tokens} tokens_out={usage.output_tokens}"
        )
    else:
        # The factory always produces a meta; a None here would be a legacy
        # schema-1.0 load, not reachable on the assembled --intent path.
        parts.append("status=unknown tokens_in=0 tokens_out=0")
    return "\n".join(parts)
