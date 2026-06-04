"""Delivery-layer-agnostic Planner → Diagnostician → ``Report`` orchestration.

Spec: ``openspec/changes/add-scheduler/`` (design D-2). Hoisted verbatim from
``cli/_intent.py`` so both the CLI ``--intent`` path and the Scheduler share one
backend-injectable core (no Rich / Typer / CLI context lives here):

- ``run_diagnosis_pipeline`` — the backend-injectable core shared by ``--intent``
  (real ``AnthropicAPIBackend`` + wall clock) and ``demo`` (offline
  ``PlaybackBackend`` + frozen clock).
- ``_seed_findings_from_snapshot`` / ``_seeding_sort_key`` — stamp + deterministically
  seed the Planner-phase findings into the ``FindingStore``.
- ``_sum_loop_usage`` — field-level token-usage summation across both loops.
- ``_assemble_report`` — Report assembly + hypotheses / narrative projection +
  id-consistency invariant.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from hostlens.agent.diagnostician import (
    DiagnosticianAgent,
    DiagnosticianResult,
    SeededFinding,
    run_diagnosis,
)
from hostlens.agent.loop import LoopUsage
from hostlens.agent.planner import PlannerAgent, PlannerResult
from hostlens.reporting.models import (
    Finding,
    Report,
    ReportStatus,
    TokenUsage,
    compute_finding_id,
)
from hostlens.tools.default_tools import register_default_tools
from hostlens.tools.diagnostician_tools import register_diagnostician_tools
from hostlens.tools.finding_store import FindingStore
from hostlens.tools.inspector_result_collector import InspectorResultCollector
from hostlens.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from collections.abc import Callable

    from hostlens.agent.backend import LLMBackend
    from hostlens.agent.events import LoopObserver
    from hostlens.core.config import Settings
    from hostlens.inspectors.result import InspectorResult
    from hostlens.tools.base import ToolContext

__all__ = [
    "run_diagnosis_pipeline",
]

# Key under which the Diagnostician's narrative is projected into
# ``Report.metadata`` (a ``dict[str, str]``): json keeps it, persistence can
# retrieve it, and the intent-style md renderer reads it back (design D-6).
_DIAGNOSIS_NARRATIVE_KEY = "diagnosis_narrative"


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
    schedule_name: str | None = None,
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
        schedule_name=schedule_name,
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
    schedule_name: str | None = None,
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
        schedule_name=schedule_name,
    )
