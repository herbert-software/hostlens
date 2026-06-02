"""Internal helper: produce a redacted deep-copy of a `Report` for
rendering.

`redact_report_for_render(report)` walks the report and applies
`hostlens.core.redact.redact_text` to every string field listed in the
`report-data-model` capability spec (§需求:`render_markdown` /
`render_json` 必须在渲染边界对字符串字段过 `core/redact.py`). The
source `Report` is never mutated — a fresh `Report` instance is
returned, suitable for `render_markdown.render` / `model_dump_json`.

Fields that are NOT redacted (per spec):

- `Report.report_id` (UUID)
- `Report.schema_version` (Literal)
- `Report.started_at` / `Report.finished_at` (datetime)
- `Evidence.exit_code` (int)
- `InspectorResult.duration_seconds` (float)
- `Evidence.metric_value` when stored as float (str values are redacted)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hostlens.core.redact import is_sensitive_key, redact_text
from hostlens.reporting.models import (
    Evidence,
    Finding,
    Report,
    ReportMeta,
    RootCauseHypothesis,
)

if TYPE_CHECKING:
    from hostlens.agent.diagnostician import DiagnosticianResult

__all__ = ["redact_diagnostician_result_for_render", "redact_report_for_render"]


def _mask_subtree(value: Any) -> Any:
    """Aggressively replace every string inside `value` (and its nested
    children) with a fully masked placeholder. Used when an adjacent
    dict key name flagged the entire subtree as secret-bearing.
    """
    if isinstance(value, str):
        return "****"
    if isinstance(value, dict):
        return {k: _mask_subtree(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_subtree(item) for item in value]
    return value


def _redact_structured(value: Any) -> Any:
    """Recursively redact strings inside arbitrary JSON-like structures
    (dict / list / str / primitive). Used for `Evidence.data`, for
    `InspectorResult.output`, and for the whole
    `DiagnosticianResult.model_dump()` graph (including nested loop telemetry —
    `planner_result` / `diagnostician_loop` and their
    `tool_invocations[*].input` / `.output` dicts, which carry raw
    `run_inspector` output = unredacted findings + evidence).

    Strings pass through `redact_text`; non-str scalars (int / bool / None /
    StrEnum value / Literal) are returned unchanged so a `model_validate`
    round-trip preserves their types. When walking a dict, keys matching
    `is_sensitive_key` mask the associated value subtree wholesale (handles
    JSON-like ``{"password": "<the-value>"}`` shapes that bare-keyword regex
    matching cannot reach).
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        result: dict[Any, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and is_sensitive_key(k):
                result[k] = _mask_subtree(v)
            else:
                result[k] = _redact_structured(v)
        return result
    if isinstance(value, list):
        return [_redact_structured(item) for item in value]
    return value


def _redact_evidence(evidence: Evidence) -> Evidence:
    """Return a new `Evidence` with all string fields redacted.

    `kind`, `exit_code`, `truncated`, and float-typed `metric_value` are
    passed through unchanged. `metric_value` is only redacted when it is
    stored as a `str` (the `float | str | None` union).
    """
    metric_value: float | str | None = evidence.metric_value
    if isinstance(metric_value, str):
        metric_value = redact_text(metric_value)

    return Evidence(
        kind=evidence.kind,
        command=redact_text(evidence.command) if evidence.command is not None else None,
        stdout=redact_text(evidence.stdout) if evidence.stdout is not None else None,
        stderr=redact_text(evidence.stderr) if evidence.stderr is not None else None,
        exit_code=evidence.exit_code,
        path=redact_text(evidence.path) if evidence.path is not None else None,
        excerpt=redact_text(evidence.excerpt) if evidence.excerpt is not None else None,
        metric_name=(
            redact_text(evidence.metric_name) if evidence.metric_name is not None else None
        ),
        metric_value=metric_value,
        data=_redact_structured(evidence.data) if evidence.data is not None else None,
        truncated=evidence.truncated,
    )


def _redact_finding(finding: Finding) -> Finding:
    # `tags` are not passed through `redact_text`: every tag is already
    # constrained to the safe character set `^[a-z][a-z0-9_-]*$` by the
    # `Tag` Pydantic annotation (see `reporting.models.Tag`), so there
    # is nothing for the keyword-assignment / JWT / `sk-...` regexes to
    # match. Running redact_text on them is not just wasted work — if a
    # future redact rule mangled a tag string the redacted value would
    # violate the pattern constraint and `Finding(tags=...)` here would
    # raise ValidationError, breaking the renderer.
    # `id` / `inspector_name` / `inspector_version` are passed through
    # verbatim: `id` is a sha256 fingerprint and the inspector name /
    # version are identifiers, none of which carry secrets. They must be
    # threaded through this reconstruction or the redacted copy would
    # silently drop the M3 identity fields (breaking diff and
    # hypothesis-reference anchors downstream).
    return Finding(
        severity=finding.severity,
        message=redact_text(finding.message),
        evidence=[_redact_evidence(e) for e in finding.evidence],
        tags=list(finding.tags),
        id=finding.id,
        inspector_name=finding.inspector_name,
        inspector_version=finding.inspector_version,
    )


def _redact_inspector_result(ir: Any) -> Any:
    """Return a new `InspectorResult` with string fields redacted.

    Typed as `Any` to keep the module free of an `inspectors.result`
    import at module load (would re-introduce the circular import the
    package design avoids). The function is called with a real
    `InspectorResult`; we reconstruct via its class to preserve type.
    """
    cls = type(ir)
    return cls(
        name=redact_text(ir.name),
        version=redact_text(ir.version),
        status=ir.status,
        target_name=redact_text(ir.target_name),
        duration_seconds=ir.duration_seconds,
        output=_redact_structured(ir.output),
        findings=[_redact_finding(f) for f in ir.findings],
        error=redact_text(ir.error) if ir.error is not None else None,
        missing=[redact_text(m) for m in ir.missing],
    )


def _redact_meta(meta: ReportMeta) -> ReportMeta:
    """Return a new `ReportMeta` with its free-text string fields redacted.

    Top-level string fields that can carry user-supplied content
    (`target_name` / `intent` / `target_id` / `schedule_name`) pass through
    `redact_text`. Each `inspectors_used[].name` / `.version` is redacted for
    parity with `inspector_results[].name` / `.version` (a no-op for normal
    identifiers like `linux.cpu` / `1.0.0`; only fires on secret-pattern
    names). Numeric / enum / nested-model fields (`status`, `token_usage`,
    `duration_seconds`, `timestamp`, `baseline_ref`) are passed through
    unchanged — they are machine values, not secret-bearing free text.
    """
    return meta.model_copy(
        update={
            "target_name": redact_text(meta.target_name),
            "target_id": redact_text(meta.target_id),
            "intent": redact_text(meta.intent) if meta.intent is not None else None,
            "schedule_name": (
                redact_text(meta.schedule_name) if meta.schedule_name is not None else None
            ),
            "inspectors_used": [
                run.model_copy(
                    update={
                        "name": redact_text(run.name),
                        "version": redact_text(run.version),
                    }
                )
                for run in meta.inspectors_used
            ],
        }
    )


def _redact_hypothesis(hypothesis: RootCauseHypothesis) -> RootCauseHypothesis:
    """Return a new `RootCauseHypothesis` with free-text strings redacted.

    `description` and each entry of `suggested_actions` pass through
    `redact_text`. `confidence` (enum) and `supporting_findings` (finding
    `id` hashes) are passed through unchanged.
    """
    return hypothesis.model_copy(
        update={
            "description": redact_text(hypothesis.description),
            "suggested_actions": [redact_text(a) for a in hypothesis.suggested_actions],
        }
    )


def redact_report_for_render(report: Report) -> Report:
    """Return a redacted deep-copy of `report` suitable for rendering.

    The source `report` is not modified. The returned `Report` has the
    same `report_id` / `schema_version` / timestamps as the source, and
    redacted strings on every other path enumerated in the spec.

    `meta` and `hypotheses` are threaded through (and their free-text
    string fields redacted). The redacted copy **must** preserve `meta`
    when the source carries one — otherwise `render_json` would drop it
    and a round-trip through `ReportStore` would lose the report's run
    metadata. `meta is None` (legacy schema-1.0) stays None.
    """
    return Report(
        report_id=report.report_id,
        schema_version=report.schema_version,
        intent=redact_text(report.intent) if report.intent is not None else None,
        target_name=redact_text(report.target_name),
        inspector_results=[_redact_inspector_result(ir) for ir in report.inspector_results],
        findings=[_redact_finding(f) for f in report.findings],
        started_at=report.started_at,
        finished_at=report.finished_at,
        metadata={k: redact_text(v) for k, v in report.metadata.items()},
        meta=_redact_meta(report.meta) if report.meta is not None else None,
        hypotheses=[_redact_hypothesis(h) for h in report.hypotheses],
    )


def _redact_tool_invocation(inv: Any) -> Any:
    """Redact a `ToolInvocation`'s model-controlled surfaces (loop telemetry).

    `tool_name` (model-controlled — a hallucinated name can be free text) passes
    through `redact_text`; the `input` / `output` / `error` dicts (which carry raw
    `run_inspector` output = findings + evidence) go through `_redact_structured`.
    `tool_use_id` is an SDK identifier, preserved. The `output` xor `error`
    invariant is kept (only the populated one is updated). Typed `Any` to avoid a
    module-load import of `agent.loop` (mirrors `_redact_inspector_result`).
    """
    update: dict[str, Any] = {
        "tool_name": redact_text(inv.tool_name),
        "input": _redact_structured(inv.input),
    }
    if inv.output is not None:
        update["output"] = _redact_structured(inv.output)
    if inv.error is not None:
        update["error"] = _redact_structured(inv.error)
    return inv.model_copy(update=update)


def _redact_loop_result(loop_result: Any) -> Any:
    """Redact a `LoopResult`'s free-text + telemetry; preserve machine fields.

    `final_text` (model narrative) is redacted; `tool_invocations` are redacted
    per-invocation. `turns` / `terminal_status` / `usage_totals` / `stop_reason`
    are loop/SDK-controlled enums/counts, preserved unchanged.
    """
    return loop_result.model_copy(
        update={
            "final_text": redact_text(loop_result.final_text),
            "tool_invocations": [_redact_tool_invocation(i) for i in loop_result.tool_invocations],
        }
    )


def _redact_planner_result(planner_result: Any) -> Any:
    """Redact a `PlannerResult`: narrative + intent + typed findings + loop."""
    return planner_result.model_copy(
        update={
            "narrative": redact_text(planner_result.narrative),
            "intent": redact_text(planner_result.intent),
            "findings": [_redact_finding(f) for f in planner_result.findings],
            "loop_result": _redact_loop_result(planner_result.loop_result),
        }
    )


def redact_diagnostician_result_for_render(
    result: DiagnosticianResult,
) -> DiagnosticianResult:
    """Return a redacted deep-copy of `result` suitable for rendering.

    The source `result` is not mutated. This brings the `--intent` path to the
    same redaction standard as the `Report` render path **by reusing the same
    typed redactors** (`_redact_finding` / `_redact_hypothesis`): free-text
    string fields pass through the `core/redact` boundary, while **identifier
    fields are preserved verbatim** — `Finding.id` / `inspector_name` /
    `inspector_version` / `tags` (via `_redact_finding`) and
    `RootCauseHypothesis.supporting_findings` (via `_redact_hypothesis`). This is
    deliberate: those are content fingerprints / identifiers, not secrets, and
    running `redact_text` over them would either break the `Tag` pattern
    constraint (→ `ValidationError`) or silently corrupt the hypothesis→finding
    cross-reference (`supporting_findings` no longer matching `findings[*].id`),
    breaking the evidence-link contract. The genuinely untyped loop telemetry
    (`planner_result` / `diagnostician_loop` and their `tool_invocations` dicts,
    which carry raw inspector output) is redacted via `_redact_structured`.
    """
    from hostlens.agent.diagnostician import DiagnosticianResult

    return DiagnosticianResult(
        narrative=redact_text(result.narrative),
        findings=[_redact_finding(f) for f in result.findings],
        hypotheses=[_redact_hypothesis(h) for h in result.hypotheses],
        status=result.status,
        planner_result=_redact_planner_result(result.planner_result),
        diagnostician_loop=(
            _redact_loop_result(result.diagnostician_loop)
            if result.diagnostician_loop is not None
            else None
        ),
    )
