"""Deterministic (fleet) inspection — fixed inspector set, run per target.

`run_deterministic_inspection` is the pure-collection core of the
deterministic scheduler mode (`mode=deterministic`). It runs a resolved,
fixed inspector set against **every** target in the fleet through the
existing `InspectorRunner`, and returns the cross-target list of complete
`InspectorResult` objects — nothing more.

Architecture invariants (spec §需求:deterministic 模式必须固定 inspector 集
逐 target 直跑、不走 Planner、不漫游):

  * **No Planner, no LLM**: this function never instantiates a Planner /
    `AgentLoop` and never accepts an `LLMBackend` in its signature. The
    collection phase must not call an LLM (§4.2 "Inspector 只采集不调 LLM"
    + ADR-008). Narration (the one Diagnostician pass over the collected
    results) and Report assembly are the caller's job (Group E / F), not
    this function's.
  * **Fixed coverage, no roaming**: exactly `targets x inspectors` runs;
    the inspector set and the target list are both authoritative inputs —
    the LLM never gets to pick either.
  * **Capability gate reused**: each `(target, inspector)` goes through
    `InspectorRunner.run`, which applies the same preflight capability /
    binary / privilege gate as the agent path. A target that does not
    satisfy an inspector's requirements yields an `InspectorResult` with
    `status="requires_unmet"` — the closed five-value `InspectorStatus`
    set is unchanged (no `skipped` value is added); the *fleet status
    derivation* downstream treats `requires_unmet` as an expected skip.
  * **Per-item failure isolation**: a single `(target, inspector)` run
    that fails never aborts the batch. `InspectorRunner.run` already
    collapses every business failure into an `InspectorResult` status;
    the only thing this layer adds is converting a *caller programming
    error* surfaced by registry lookup (unknown target / inspector name)
    into a fail-loud `ToolError` raised before any run starts — those are
    misconfigurations, not per-host business failures, so they should
    stop the run rather than silently degrade it.
  * **Semaphore-bounded concurrency**: the full `targets x inspectors`
    fan-out runs under one `asyncio.Semaphore`, mirroring
    `TargetProbe.probe_many`, so a large fleet does not open an unbounded
    number of simultaneous SSH execs.

Dependency injection: a `context_factory: Callable[[], ToolContext]`
supplies the `TargetRegistry` / `InspectorRegistry` / `Settings` / logger
/ cancel event (same shape `run_diagnosis_pipeline` uses), so no registry
is read from a module-level singleton (CLAUDE.md §6). The backend is
deliberately absent from the signature.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from hostlens.agent.diagnostician import (
    DiagnosticianAgent,
    SeededFinding,
    harvest_hypotheses,
    reconcile_status,
)
from hostlens.agent.loop import LoopUsage
from hostlens.core.exceptions import InspectorError, ToolError
from hostlens.inspectors.health import resolve_inspector_set
from hostlens.inspectors.runner import InspectorRunner
from hostlens.reporting.models import Finding, Report, TokenUsage
from hostlens.tools.diagnostician_tools import register_narrate_only_diagnostician_tools
from hostlens.tools.finding_store import FindingStore
from hostlens.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from hostlens.agent.backend import LLMBackend
    from hostlens.agent.events import LoopObserver
    from hostlens.core.config import Settings
    from hostlens.inspectors.result import InspectorResult
    from hostlens.inspectors.schema import InspectorManifest
    from hostlens.targets.base import ExecutionTarget
    from hostlens.tools.base import ToolContext

__all__ = [
    "DEFAULT_DETERMINISTIC_CONCURRENCY",
    "resolve_inspector_set",
    "run_deterministic_inspection",
    "run_deterministic_pipeline",
]


# Key under which the narrate-only Diagnostician's narrative is projected into
# ``Report.metadata`` — the same key the agent-mode pipeline uses
# (``orchestration.pipeline._DIAGNOSIS_NARRATIVE_KEY``), so the md renderer
# reads either mode's narrative back through one accessor.
_DIAGNOSIS_NARRATIVE_KEY = "diagnosis_narrative"


# Default fan-out bound for the `targets x inspectors` collection. Caps the
# number of simultaneous SSH execs so a wide fleet does not open a connection
# storm. Mirrors the intent of `TargetProbe`'s probe semaphore; callers may
# override via `concurrency=`.
DEFAULT_DETERMINISTIC_CONCURRENCY = 8


async def run_deterministic_inspection(
    context_factory: Callable[[], ToolContext],
    targets: Sequence[str],
    *,
    inspectors: Sequence[str] | None = None,
    inspector_parameters: dict[str, dict[str, Any]] | None = None,
    concurrency: int = DEFAULT_DETERMINISTIC_CONCURRENCY,
) -> list[InspectorResult]:
    """Run the resolved inspector set against every target; return all results.

    Pure collection: returns the cross-target list of complete
    `InspectorResult` objects (real `status` / `version` / `duration` /
    `findings`, ok and non-ok alike). It does **not** assemble a `Report`,
    run the Diagnostician, or touch any `LLMBackend` — that wiring is the
    caller's (Group E / F).

    Resolution (spec §需求:deterministic 模式的 inspector 集由内置健康默认集
    或 `manifest.inspectors` 权威决定):

      * `inspectors is None` → `DEFAULT_HEALTH_INSPECTORS`.
      * `inspectors` non-None → that list, authoritative (no union with the
        default set, never a soft hint).

    Execution:

      * Builds one `ToolContext` via `context_factory()` to read the
        registries / settings / logger / cancel event. The same context is
        shared across the whole fan-out (its registries are read-only here).
      * Resolves every target name and inspector name up front. An unknown
        target (`KeyError` from `TargetRegistry.get`) or unknown inspector
        (`InspectorError(inspector_not_found)`) raises `ToolError` — a
        fail-loud misconfiguration, not a per-host business failure.
      * Fans out `targets x inspectors` runs under one
        `asyncio.Semaphore(concurrency)`. Each run goes through
        `InspectorRunner.run`, which applies the capability gate and
        collapses every business failure (timeout / unreachable / parse /
        capability) into an `InspectorResult` status. Single-run failures
        are therefore isolated by construction; one bad host or one
        capability mismatch never aborts the batch.

    `allow_privileged=False` matches the agent surface: the deterministic
    fleet path never opts in to sudo/root inspectors. `ctx.cancel` is
    threaded into each run so a daemon SIGTERM / Ctrl-C propagates.

    The returned list groups results target-by-target in the order
    `targets` was given (each target's inspectors in resolved-set order);
    callers that need a different order should sort the result. Identity of
    a finding is content-derived downstream, so this order is presentational
    only.
    """
    ctx = context_factory()
    resolved_inspectors = resolve_inspector_set(inspectors)

    # ---- Resolve every (target, inspector) pair up front (fail-loud) ---- #
    # Unknown names are caller misconfigurations: surface them before any
    # run starts rather than silently dropping a host / inspector. Business
    # failures (unreachable host, missing capability) are NOT resolved here —
    # they surface per-run as an `InspectorResult` status.
    runner = InspectorRunner(
        ctx.target_registry,
        settings=ctx.config,
        logger=ctx.logger,
    )

    pairs: list[tuple[InspectorManifest, ExecutionTarget]] = []
    for target_name in targets:
        try:
            target = ctx.target_registry.get(target_name)
        except KeyError as exc:
            raise ToolError(
                f"target_not_found: target_name={target_name!r} "
                "is not registered in target_registry"
            ) from exc
        for inspector_name in resolved_inspectors:
            try:
                manifest = ctx.inspector_registry.get(inspector_name)
            except InspectorError as exc:
                if exc.kind == "inspector_not_found":
                    raise ToolError(
                        f"inspector_not_found: inspector_name={inspector_name!r} "
                        "is not registered in inspector_registry"
                    ) from exc
                raise
            pairs.append((manifest, target))

    if not pairs:
        return []

    semaphore = asyncio.Semaphore(concurrency)

    async def _bounded(manifest: InspectorManifest, target: ExecutionTarget) -> InspectorResult:
        # Missing key -> None (default params); a present-but-{} entry is a hit
        # and yields {} (semantically "matched, empty params"), not None.
        params = (inspector_parameters or {}).get(manifest.name)
        async with semaphore:
            return await runner.run(
                manifest,
                target,
                parameters=params,
                allow_privileged=False,
                cancel=ctx.cancel,
            )

    return list(await asyncio.gather(*(_bounded(m, t) for m, t in pairs)))


def _narrate_seed_sort_key(
    finding: Finding,
) -> tuple[str, str, str, str, tuple[str, ...], int, str]:
    """Deterministic sort key for the fleet findings before they are seeded.

    F1/F2/… labels are assigned positionally by ``FindingStore.seed`` over the
    list order, and the collection fan-out (`run_deterministic_inspection`)
    gathers `targets x inspectors` concurrently, so the result order — and hence
    the labelled-findings block fed into the narrate loop's first user message —
    is not stable run-to-run. Without a stable sort the cassette request key
    would jitter (offline replay would miss non-deterministically). The key is a
    superset of every field `_render_findings_block` renders, plus the source
    ``target_name`` (which fleet findings carry and single-target ones do not),
    so a key tie ⇒ the rendered line is byte-identical and the tie is harmless.
    """
    return (
        finding.target_name or "",
        finding.inspector_name or "",
        finding.inspector_version or "",
        finding.severity,
        tuple(finding.tags),
        len(finding.evidence),
        finding.message,
    )


async def run_deterministic_pipeline(
    backend: LLMBackend,
    settings: Settings,
    context_factory: Callable[[], ToolContext],
    *,
    targets: list[str],
    inspectors: list[str] | None,
    intent: str,
    inspector_parameters: dict[str, dict[str, Any]] | None = None,
    schedule_name: str | None = None,
    observer: LoopObserver | None = None,
) -> Report | None:
    """Run the full deterministic (fleet) pipeline → one multi-target ``Report``.

    Composition of the three already-built pieces (spec
    deterministic-inspection-mode §需求:LLM 只对采集结果写根因叙述 + §需求:多
    target 必须聚合成一份报告):

    1. **Collect** — ``run_deterministic_inspection`` runs the resolved inspector
       set against every target (no Planner, no LLM in the collection phase;
       the ``backend`` is never threaded into a ``ToolContext`` — ADR-008).
    2. **Assemble** — ``Report.from_fleet_results`` flattens the cross-target
       ``InspectorResult`` list into **one** fleet ``Report``: each finding keeps
       its source ``Finding.target_name``, ``Report.target_name`` is the
       deterministic fleet label, and ``meta.target_id`` is the deterministic
       fleet id. ``status=None`` lets the fleet status derivation treat
       ``requires_unmet`` as an expected skip (a fixed health set on a
       heterogeneous fleet) while real failures still degrade to ``partial``.
    3. **Narrate** — one ``DiagnosticianAgent`` pass assembled through the
       **narrate-only** path (``register_narrate_only_diagnostician_tools`` →
       only ``correlate_findings``), so the LLM can record root-cause hypotheses
       over the collected findings but structurally cannot re-run an inspector
       or pick a target. The harvested hypotheses + narrative are attached to the
       fleet ``Report``.

    The ``backend`` reaches **only** the ``DiagnosticianAgent``'s loop, never any
    ``ToolContext`` (ADR-008 / CLAUDE.md §7).

    No-result path: when the collection yields **zero** ``InspectorResult``
    (every requested ``(target, inspector)`` pair somehow produced nothing — in
    practice impossible since ``InspectorRunner.run`` always returns a result,
    but kept symmetric with ``run_diagnosis_pipeline``'s empty-collector guard),
    ``Report.from_fleet_results`` would raise on an empty list, so emptiness is
    checked first and ``None`` is returned. Group F's runner maps that ``None``
    onto a ``failed`` ``RunStatus``.

    Degraded narration is **non-fatal**: if the narrate loop degrades (rate
    limit / token budget / max turns / API unavailable), the collected findings
    are never discarded — the fleet ``Report`` is still returned, with whatever
    hypotheses converged (possibly none) and the loop's ``final_text`` narrative
    (possibly empty). The report's ``meta.status`` is **reconciled** from both
    phases: the collection outcome (step 2) holds when narration succeeds, but a
    degraded narrate loop (rate-limited / backend-unavailable / max-turns /
    empty) surfaces as a degraded ``ReportStatus`` — the collected findings are
    always kept, never discarded over a diagnosis blip (scheduler-engine spec:
    "narrate 阶段后端不可用按 degraded Report 处理、不丢已采集结果").

    ``observer`` is passed straight through to the narrate loop.
    """
    started_at = datetime.now(UTC)
    inspector_results = await run_deterministic_inspection(
        context_factory,
        targets,
        inspectors=inspectors,
        inspector_parameters=inspector_parameters,
    )

    if not inspector_results:
        return None

    fleet_schedule_name = schedule_name if schedule_name is not None else "deterministic"

    base_report = Report.from_fleet_results(
        inspector_results,
        schedule_name=fleet_schedule_name,
        intent=intent,
        started_at=started_at,
        finished_at=datetime.now(UTC),
    )

    # Seed the FindingStore from the fleet Report's already-stamped findings
    # (from_fleet_results filled id / inspector_name / inspector_version /
    # target_name). A deterministic sort keeps the positional F1/F2 labels — and
    # the narrate loop's first user message — stable across runs.
    sorted_findings = sorted(base_report.findings, key=_narrate_seed_sort_key)
    store = FindingStore()
    labels = store.seed(sorted_findings)
    seeded = [
        SeededFinding(label=label, finding=finding)
        for label, finding in zip(labels, sorted_findings, strict=True)
    ]

    narrate_registry = ToolRegistry()
    register_narrate_only_diagnostician_tools(narrate_registry, finding_store=store)
    diagnostician = DiagnosticianAgent(backend, narrate_registry, settings, context_factory)

    narrate_loop = await diagnostician.run(intent, seeded, observer=observer)
    hypotheses = harvest_hypotheses(narrate_loop, store)

    narrate_usage = narrate_loop.usage_totals
    token_usage = _loop_usage_to_token_usage(narrate_usage)

    # Reconcile status: keep the collection outcome when narration succeeds; a
    # degraded narrate loop surfaces as a degraded ``ReportStatus`` (never masked
    # as the collection's ok), while the collected findings are always kept.
    # Reuses the agent path's ``reconcile_status`` for the degraded mapping
    # (narrate failed_api_unavailable → degraded_no_planner, keep findings;
    # rate-limited / max-turns / empty → same-named ``ReportStatus``).
    if base_report.meta is not None:
        reconciled_status = (
            base_report.meta.status
            if narrate_loop.terminal_status == "ok"
            else reconcile_status("ok", narrate_loop.terminal_status)
        )
        final_meta = base_report.meta.model_copy(
            update={"token_usage": token_usage, "status": reconciled_status}
        )
    else:
        final_meta = None

    report = base_report.model_copy(
        update={
            "hypotheses": list(hypotheses),
            "metadata": {
                **base_report.metadata,
                _DIAGNOSIS_NARRATIVE_KEY: narrate_loop.final_text,
            },
            **({"meta": final_meta} if final_meta is not None else {}),
        }
    )

    finding_ids = {f.id for f in report.findings}
    for hypothesis in report.hypotheses:
        for ref in hypothesis.supporting_findings:
            if ref not in finding_ids:
                raise ValueError(
                    "deterministic fleet report id-consistency invariant violated: "
                    f"hypothesis supporting_findings id {ref!r} is not present in "
                    "Report.findings; refusing to return a report with a dangling reference"
                )

    return report


def _loop_usage_to_token_usage(usage: LoopUsage) -> TokenUsage:
    """Project the narrate loop's ``LoopUsage`` into a ``Report``-level
    ``TokenUsage``. The deterministic pipeline runs a single (narrate) loop, so
    the report's token usage is exactly that loop's totals (no Planner loop to
    sum, unlike the agent-mode ``_sum_loop_usage``).
    """
    return TokenUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_creation_input_tokens=usage.cache_creation_input_tokens,
        cache_read_input_tokens=usage.cache_read_input_tokens,
    )
