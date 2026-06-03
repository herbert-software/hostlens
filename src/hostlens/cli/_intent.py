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
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from rich.console import Console
from rich.live import Live
from rich.tree import Tree

from hostlens.agent.backend import create_backend
from hostlens.agent.diagnostician import (
    DiagnosticianAgent,
    DiagnosticianResult,
    SeededFinding,
    run_diagnosis,
)
from hostlens.agent.events import (
    ModelResponded,
    RunFinalized,
    ToolCompleted,
    ToolStarted,
    TurnStarted,
)
from hostlens.agent.loop import LoopUsage
from hostlens.agent.planner import PlannerAgent, PlannerResult
from hostlens.core.redact import redact_text
from hostlens.reporting._redact import redact_report_for_render
from hostlens.reporting.models import (
    Finding,
    Report,
    ReportStatus,
    TokenUsage,
    compute_finding_id,
)
from hostlens.reporting.render_json import render as render_json
from hostlens.tools.base import NoopApprovalService, ToolContext
from hostlens.tools.default_tools import register_default_tools
from hostlens.tools.diagnostician_tools import register_diagnostician_tools
from hostlens.tools.finding_store import FindingStore
from hostlens.tools.inspector_result_collector import InspectorResultCollector
from hostlens.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from collections.abc import Callable

    import structlog

    from hostlens.agent.backend import LLMBackend
    from hostlens.agent.events import LoopEvent, LoopObserver
    from hostlens.core.config import Settings
    from hostlens.inspectors.registry import InspectorRegistry
    from hostlens.inspectors.result import InspectorResult
    from hostlens.targets.registry import TargetRegistry

__all__ = [
    "RichLiveObserver",
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


def _seeding_sort_key(finding: Finding) -> tuple[str, str, str, tuple[str, ...], int, str]:
    """Deterministic sort key for Planner-phase findings before ``store.seed`` (D-7).

    F1/F2/… labels are assigned positionally by ``FindingStore.seed`` over the
    list order, and the collector's same-response parallel append order is NOT
    stable across runs (collector docstring). Without a stable sort the seeded
    order — and hence the ``_render_findings_block`` text fed into the
    Diagnostician's first user message — would jitter, making the diagnosis-phase
    request key non-deterministic (offline cassette replay would then miss
    non-deterministically) and the authored ``[F1]`` references point at a
    different finding run-to-run.

    The key is the **superset** of every per-finding field
    ``_render_findings_block`` actually renders
    (``severity inspector tags evidence :: message``), so a key tie ⇒ the
    rendered line is byte-identical AND (since ``compute_finding_id`` hashes
    name/version/message) the id is identical → the tie is harmless and needs no
    fail-loud guard (design D-7, single-direction superset):

    - ``tags`` uses ``tuple(f.tags)`` in **original order** (matching the
      rendered ``",".join(f.tags)``), NOT ``sorted`` — a same-set different-order
      tag list renders differently (``a,b`` vs ``b,a``) so must not tie;
    - ``evidence`` uses ``len(f.evidence)`` (the render only emits the count);
    - the key carries ``inspector_version`` (which the render does NOT emit): the
      key is allowed to be strictly larger than the render projection, never
      smaller.

    The stamped ``inspector_name`` / ``inspector_version`` are guaranteed non-None
    (the seed helper stamps them from the source ``InspectorResult`` before
    sorting), so the key reads them directly.
    """
    return (
        finding.inspector_name or "",
        finding.inspector_version or "",
        finding.severity,
        tuple(finding.tags),
        len(finding.evidence),
        finding.message,
    )


def _seed_findings_from_snapshot(
    planner_snapshot: list[InspectorResult],
    store: FindingStore,
) -> list[SeededFinding]:
    """Stamp + seed the Planner-phase findings into ``store`` (design D-2 step 1).

    Iterates the collector's Planner-phase ``InspectorResult`` snapshot — which
    natively preserves the inspector grouping (each result carries its own
    ``name`` / ``version`` / ``findings``), so there is **no registry re-lookup**
    (the collector supplies the real ``InspectorResult.version`` directly, with
    no version reverse-lookup that a TOCTOU inspector unload could break). Each
    finding's id is stamped with ``compute_finding_id(name, version, message)``
    — the SAME content-deterministic function ``Report.from_inspector_results``
    uses after diagnosis, so the FindingStore ids and the final
    ``Report.findings`` ids are naturally equal (design D-3).

    After stamping (so ``inspector_name`` / ``inspector_version`` / ``id`` are
    filled) and **before** ``store.seed`` the stamped findings are sorted by
    ``_seeding_sort_key`` (design D-7): this makes the positional F1/F2 label
    assignment — and the diagnosis-phase messages derived from it —
    deterministic across runs. The stamped, sorted findings are seeded into
    ``store`` (the Diagnostician needs labeled findings to reference) and
    returned as ``(label, finding)`` pairs.
    """
    stamped: list[Finding] = []
    for ir in planner_snapshot:
        for finding in ir.findings:
            stamped.append(
                finding.model_copy(
                    update={
                        "inspector_name": ir.name,
                        "inspector_version": ir.version,
                        "id": compute_finding_id(ir.name, ir.version, finding.message),
                    }
                )
            )
    stamped.sort(key=_seeding_sort_key)
    labels = store.seed(stamped)
    return [
        SeededFinding(label=label, finding=finding)
        for label, finding in zip(labels, stamped, strict=True)
    ]


def _sum_loop_usage(*usages: LoopUsage) -> TokenUsage:
    """Field-level sum of one-or-more ``LoopUsage`` into a ``TokenUsage`` (D-6).

    The Planner and Diagnostician each run an independent ``AgentLoop`` with its
    own ``LoopUsage``; the assembled ``Report.meta.token_usage`` must reflect the
    WHOLE intent run, so the two are summed field-by-field (including the cache
    counters) — never just the diagnosis loop's.
    """
    return TokenUsage(
        input_tokens=sum(u.input_tokens for u in usages),
        output_tokens=sum(u.output_tokens for u in usages),
        cache_creation_input_tokens=sum(u.cache_creation_input_tokens for u in usages),
        cache_read_input_tokens=sum(u.cache_read_input_tokens for u in usages),
    )


def _assemble_report(
    target: str,
    intent: str,
    collector_snapshot: list[InspectorResult],
    diag_result: DiagnosticianResult,
    started_at: datetime,
    finished_at: datetime,
    *,
    token_usage: TokenUsage,
    target_type: str,
) -> Report:
    """Assemble the authoritative ``Report`` from the post-diagnosis snapshot.

    ``status`` override (design D-5): the reconciled ``diag_result.status`` is
    passed verbatim when it is a degraded / empty_response value (those cannot
    be re-derived from ``InspectorResult`` statuses — ``_derive_report_status``
    never produces ``empty_response``), otherwise ``status=None`` lets
    ``_derive_report_status`` apply the §9 rules (all-ok → ok / non-ok-only-
    timeout-with-an-ok → ok / unreachable·exception·requires_unmet or all-timeout
    → partial). Never pass ``ok`` explicitly (it would bypass the partial
    derivation and mask inspector-level loss).

    The Diagnostician's hypotheses are projected into ``Report.hypotheses`` and
    its narrative into ``Report.metadata[_DIAGNOSIS_NARRATIVE_KEY]`` via
    ``model_copy`` (the factory does not take them). The id-consistency
    invariant is then asserted: every ``hypotheses[*].supporting_findings`` id
    must be present in ``Report.findings`` (fail-loud on a dangling reference —
    the CLI boundary maps the raised error to ``internal: ... → exit 2``;
    design D-3).
    """
    status_override: ReportStatus | None = (
        diag_result.status if diag_result.status != ReportStatus.OK else None
    )

    report = Report.from_inspector_results(
        target,
        collector_snapshot,
        intent=intent,
        started_at=started_at,
        finished_at=finished_at,
        status=status_override,
        token_usage=token_usage,
        target_type=target_type,
    )
    report = report.model_copy(
        update={
            "hypotheses": list(diag_result.hypotheses),
            "metadata": {**report.metadata, _DIAGNOSIS_NARRATIVE_KEY: diag_result.narrative},
        }
    )

    finding_ids = {f.id for f in report.findings}
    for hypothesis in report.hypotheses:
        for ref in hypothesis.supporting_findings:
            if ref not in finding_ids:
                raise ValueError(
                    "intent report id-consistency invariant violated: hypothesis "
                    f"supporting_findings id {ref!r} is not present in Report.findings; "
                    "refusing to persist a report with a dangling reference"
                )

    return report


async def run_diagnosis_pipeline(
    backend: LLMBackend,
    settings: Settings,
    context_factory: Callable[[], ToolContext],
    report_target_name: str,
    target_lookup_name: str,
    target_type: str,
    intent: str,
    *,
    tool_clock: Callable[[], datetime] | None = None,
    observer: LoopObserver | None = None,
    planner_result_sink: Callable[[PlannerResult], None] | None = None,
) -> Report | None:
    """Run Planner → seed → Diagnostician → assemble ``Report`` (D-1/D-2/D-5).

    The backend-injectable core shared by ``--intent`` (real
    ``AnthropicAPIBackend`` via ``create_backend`` + wall clock) and ``demo``
    (offline ``PlaybackBackend`` + frozen clock): the ``backend`` /
    ``context_factory`` / ``tool_clock`` that ``run_intent_diagnosis`` used to
    build internally are now injected, so neither path duplicates the two-loop
    timing / id-consistency / status-reconcile contract (design D-1). The SAME
    ``backend`` instance reaches BOTH the ``PlannerAgent`` and the
    ``DiagnosticianAgent``; it is handed only to the two agents' loops, never
    into any ``ToolContext`` (ADR-008).

    A single per-run ``InspectorResultCollector`` is injected into BOTH the
    Planner's default-tools registry and the Diagnostician's registry, so it
    accumulates every ``InspectorResult`` across both loops (design D-1). The
    orchestration operates the collector at TWO time points (design D-2):

    1. **Before diagnosis** — seed the ``FindingStore`` from the Planner-phase
       snapshot (ids stamped by ``compute_finding_id``, no registry re-lookup,
       then sorted by ``_seeding_sort_key`` for deterministic F1/F2 labels — D-7).
    2. **After diagnosis** — snapshot the FULL collector (Planner + every
       ``request_more_inspection`` supplement) and assemble the authoritative
       ``Report`` via ``Report.from_inspector_results``.

    Two target names (design D-1): ``report_target_name`` is the display label
    written into ``Report.target_name`` (``demo:<scenario>`` for demo, the real
    target name for ``--intent``); ``target_lookup_name`` is the registry lookup
    key fed to ``register_diagnostician_tools(target_name=)`` (the key
    ``request_more_inspection`` resolves against ``ctx.target_registry.get``).
    ``--intent`` passes the same string for both; demo splits them. A generic
    guard asserts ``target_lookup_name`` is present in the supplied context's
    target registry (the host-specific ``lookup == DEMO_TARGET_NAME`` invariant
    is the demo-assembly side's job — this core must not hard-code it or it would
    break the real ``--intent`` target; tasks 1.1 / 2.1).

    ``register_default_tools`` is called with BOTH ``collector=`` and
    ``clock=tool_clock`` so a frozen-clock caller (demo) keeps its
    ``sampling_window`` commands byte-stable (cassette request key would miss
    otherwise — design D-1 tool_clock).

    ``planner_result_sink`` (when non-None) is invoked exactly once right after
    ``planner.run`` returns, while the ``PlannerResult`` is still in hand — the
    mount point a recording harness uses to project the Planner-only incident
    snapshot in a single pass (design D-3.5 step3). ``--intent`` passes ``None``
    → no-op → behaviour byte-identical to the pre-refactor seam.

    ``started_at`` / ``finished_at`` are taken here (``LoopResult`` carries no
    timestamps): once before the Planner runs and once after the diagnosis loop.

    Returns ``None`` for the **no-result** path: when the collector is empty
    (zero ``InspectorResult`` — the Planner never successfully ran an inspector,
    e.g. ``failed_api_unavailable`` before any tool call, OR the model never
    called ``run_inspector``). ``from_inspector_results`` raises ``ValueError``
    on an empty list, so the emptiness is checked BEFORE assembly and the CLI
    maps ``None`` to the no-result degradation (stderr note + empty stdout +
    exit 2 + no persist). A ``CassetteMiss`` (offline replay drift) is NOT
    caught here — it propagates verbatim to the caller boundary (design D-1).
    Every non-empty path returns a faithful ``Report``.

    ``observer`` is passed straight through to BOTH agent runs, so the Planner
    and Diagnostician progress trees both stream to the CLI's stderr sink.

    ``target_type`` comes from the caller's already-resolved
    ``ExecutionTarget.type`` and is threaded into the factory, so SSH/Docker/K8s
    intent reports record their real target type instead of the factory default
    ``local`` (which would pollute persisted metadata).
    """
    probe_ctx = context_factory()
    probe_ctx.target_registry.get(target_lookup_name)

    collector = InspectorResultCollector()

    planner_registry = ToolRegistry()
    register_default_tools(planner_registry, collector=collector, clock=tool_clock)
    planner = PlannerAgent(backend, planner_registry, settings, context_factory)

    started_at = datetime.now(UTC)
    planner_result = await planner.run(intent, observer=observer)

    if planner_result_sink is not None:
        planner_result_sink(planner_result)

    if planner_result.loop_result.terminal_status == "failed_api_unavailable":
        # The Planner never reached the API: the collector is empty and there is
        # nothing to diagnose (reconcile_status would raise on this status).
        # Return None so the CLI takes the no-result path.
        return None

    # Seed the FindingStore from the Planner-phase collector snapshot (design
    # D-2 step 1). The store stays the single label authority; the Diagnostician
    # references findings by the labels seeded here.
    store = FindingStore()
    seeded = _seed_findings_from_snapshot(collector.snapshot(), store)

    def _make_diag_agent() -> DiagnosticianAgent:
        # Built lazily by run_diagnosis only on the Planner-ok path, so a Planner
        # degrade constructs no registry / agent at all (zero factory calls). The
        # SAME collector is injected so request_more_inspection supplements land
        # in the snapshot the Report is assembled from.
        diag_registry = ToolRegistry()
        register_diagnostician_tools(
            diag_registry,
            finding_store=store,
            target_name=target_lookup_name,
            clock=tool_clock,
            collector=collector,
        )
        return DiagnosticianAgent(backend, diag_registry, settings, context_factory)

    diag_result = await run_diagnosis(
        planner_result, seeded, store, _make_diag_agent, observer=observer
    )

    finished_at = datetime.now(UTC)

    # No-result is "the collector is empty" (zero InspectorResult), NOT "no
    # successful inspector": a Planner that finalized ok but whose model never
    # called run_inspector also lands here, and from_inspector_results raises on
    # an empty list — so check emptiness before assembly.
    full_snapshot = collector.snapshot()
    if not full_snapshot:
        return None

    diag_loop = diag_result.diagnostician_loop
    diag_usage = diag_loop.usage_totals if diag_loop is not None else LoopUsage()
    token_usage = _sum_loop_usage(planner_result.loop_result.usage_totals, diag_usage)

    return _assemble_report(
        report_target_name,
        intent,
        full_snapshot,
        diag_result,
        started_at,
        finished_at,
        token_usage=token_usage,
        target_type=target_type,
    )


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
