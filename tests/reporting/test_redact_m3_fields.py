"""Guardrail tests for M3 add-only field threading through
`redact_report_for_render`.

The redactor reconstructs `Report` / `Finding` field by field, so any
new field that is not explicitly threaded would be **silently dropped**
on the redacted copy — the JSON / markdown sink (and the SQLite
`ReportStore` round-trip) would then lose it without any error. These
tests pin the invariant from spec §需求:渲染/落盘边界必须脱敏
`meta`/`hypotheses` 字符串并透传 Finding 身份字段:

- Finding identity fields (`id` / `inspector_name` / `inspector_version`)
  survive redaction verbatim.
- `Report.meta` survives (non-loss) and its free-text strings are
  redacted (`meta.intent` carrying a secret is masked).
- `Report.hypotheses` survives and its free-text strings are redacted.
- A legacy report with `meta is None` redacts without crashing and stays
  `meta is None`.
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from hostlens.inspectors.result import InspectorResult
from hostlens.reporting._redact import redact_report_for_render
from hostlens.reporting.models import (
    Finding,
    Report,
    RootCauseHypothesis,
)

API_KEY = "sk-abcdefghijklmnopqrstuvwxyz1234567890"


def _t() -> datetime:
    return datetime(2026, 5, 26, 12, 0, 0)


def _ir(findings: list[Finding]) -> InspectorResult:
    return InspectorResult(
        name="insp.x",
        version="1.0.0",
        status="ok",
        target_name="t",
        duration_seconds=0.1,
        output={},
        findings=findings,
        error=None,
        missing=[],
    )


def test_redact_preserves_finding_identity_fields() -> None:
    """spec §场景:脱敏拷贝保留 Finding 身份字段 — the factory-populated
    `id` / `inspector_name` / `inspector_version` must survive redaction.
    """
    report = Report.from_inspector_results(
        "t",
        [_ir([Finding(severity="warning", message="cpu high")])],
        started_at=_t(),
        finished_at=_t(),
    )
    src_finding = report.findings[0]
    assert src_finding.id is not None  # factory populates it

    redacted = redact_report_for_render(report)
    out_finding = redacted.findings[0]
    assert out_finding.id == src_finding.id
    assert out_finding.inspector_name == "insp.x"
    assert out_finding.inspector_version == "1.0.0"
    # The nested `inspector_results[].findings` are the *original* findings
    # (the factory only fills identity on the flattened top-level list), so
    # their `id` is None here — the redactor must pass that through verbatim,
    # not invent one.
    src_nested = report.inspector_results[0].findings[0]
    out_nested = redacted.inspector_results[0].findings[0]
    assert out_nested.id == src_nested.id


def test_redact_finding_identity_explicit_construction() -> None:
    """Identity fields survive even when constructed directly (not via the
    factory), matching the spec scenario's explicit `id="abc"` shape.
    """
    finding = Finding(
        severity="info",
        message="x",
        id="abc",
        inspector_name="insp",
        inspector_version="1.0",
    )
    report = Report.from_inspector_results(
        "t",
        [_ir([finding])],
        started_at=_t(),
        finished_at=_t(),
    )
    # The factory overwrites identity on the flattened copy; assert the
    # *redactor* does not drop whatever identity the report carries.
    flattened = report.findings[0]
    redacted = redact_report_for_render(report)
    out = redacted.findings[0]
    assert out.id == flattened.id
    assert out.inspector_name == flattened.inspector_name
    assert out.inspector_version == flattened.inspector_version
    assert out.id is not None


def test_redact_preserves_meta_non_loss() -> None:
    """Factory-built report carries `meta`; redaction must not drop it."""
    report = Report.from_inspector_results(
        "t",
        [_ir([])],
        started_at=_t(),
        finished_at=_t(),
    )
    assert report.meta is not None

    redacted = redact_report_for_render(report)
    assert redacted.meta is not None
    assert redacted.meta.run_id == report.meta.run_id
    assert redacted.meta.status == report.meta.status
    assert redacted.meta.target_type == report.meta.target_type
    # Numeric / nested fields pass through unchanged.
    assert redacted.meta.token_usage == report.meta.token_usage
    assert redacted.meta.duration_seconds == report.meta.duration_seconds
    assert redacted.meta.inspectors_used == report.meta.inspectors_used


def test_redact_meta_intent_secret_masked() -> None:
    """spec §场景:脱敏拷贝保留并脱敏 meta — `meta.intent` carrying a
    secret must be masked while meta itself survives.
    """
    report = Report.from_inspector_results(
        "t",
        [_ir([])],
        started_at=_t(),
        finished_at=_t(),
        intent=f"investigate token={API_KEY}",
    )
    assert report.meta is not None
    assert API_KEY in (report.meta.intent or "")

    redacted = redact_report_for_render(report)
    assert redacted.meta is not None
    assert redacted.meta.intent is not None
    assert API_KEY not in redacted.meta.intent


def test_redact_meta_inspectors_used_name_masked() -> None:
    """`meta.inspectors_used[].name` / `.version` are redacted for parity
    with `inspector_results[].name` / `.version`. A secret-looking inspector
    name must not survive into the redacted copy as plaintext.
    """
    secret_ir = InspectorResult(
        name=f"token={API_KEY}",
        version="1.0.0",
        status="ok",
        target_name="t",
        duration_seconds=0.1,
        output={},
        findings=[],
        error=None,
        missing=[],
    )
    report = Report.from_inspector_results(
        "t",
        [secret_ir],
        started_at=_t(),
        finished_at=_t(),
    )
    assert report.meta is not None
    assert any(API_KEY in run.name for run in report.meta.inspectors_used)

    redacted = redact_report_for_render(report)
    assert redacted.meta is not None
    assert all(API_KEY not in run.name for run in redacted.meta.inspectors_used)
    # A plain semver `version` carries no secret, so redaction is a no-op:
    # diff version-alignment keys on `inspectors_used[].version`, so masking
    # it would break baseline matching.
    assert all(run.version == "1.0.0" for run in redacted.meta.inspectors_used)


def test_redact_meta_target_fields_masked() -> None:
    """`meta.target_name` / `meta.target_id` free-text strings are redacted."""
    report = Report.from_inspector_results(
        f"host token={API_KEY}",
        [_ir([])],
        started_at=_t(),
        finished_at=_t(),
    )
    assert report.meta is not None

    redacted = redact_report_for_render(report)
    assert redacted.meta is not None
    assert API_KEY not in redacted.meta.target_name
    assert API_KEY not in redacted.meta.target_id


def test_redact_hypotheses_preserved_and_masked() -> None:
    """`Report.hypotheses` survives redaction and its free-text strings
    (`description` / `suggested_actions`) are masked; `confidence` and
    `supporting_findings` (id hashes) pass through unchanged.
    """
    hypothesis = RootCauseHypothesis(
        description=f"leak via token={API_KEY}",
        confidence="high",
        supporting_findings=["abc123"],
        suggested_actions=[f"rotate token={API_KEY}", "restart service"],
    )
    base = Report.from_inspector_results(
        "t",
        [_ir([])],
        started_at=_t(),
        finished_at=_t(),
    )
    report = base.model_copy(update={"hypotheses": [hypothesis]})

    redacted = redact_report_for_render(report)
    assert len(redacted.hypotheses) == 1
    out = redacted.hypotheses[0]
    assert API_KEY not in out.description
    assert out.confidence == "high"
    assert out.supporting_findings == ["abc123"]
    assert all(API_KEY not in a for a in out.suggested_actions)
    assert "restart service" in out.suggested_actions


def test_redact_legacy_report_without_meta_does_not_crash() -> None:
    """spec §场景:legacy 无 meta 报告脱敏不崩 — a schema-1.0 report with
    `meta is None` redacts cleanly and stays meta=None.
    """
    legacy = Report(
        report_id=uuid4(),
        schema_version="1.0",
        target_name="t",
        inspector_results=[_ir([Finding(severity="info", message="x")])],
        findings=[Finding(severity="info", message="x")],
        started_at=_t(),
        finished_at=_t(),
    )
    assert legacy.meta is None
    assert legacy.hypotheses == []

    redacted = redact_report_for_render(legacy)
    assert redacted.meta is None
    assert redacted.hypotheses == []
    # Legacy findings have no identity fields; they must stay None.
    assert redacted.findings[0].id is None
    assert redacted.findings[0].inspector_name is None
