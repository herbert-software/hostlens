"""Tests for `DiagnosticianResult` (task 1.1).

Covers the frozen field set, `diagnostician_loop` being optional, and
`model_dump_json` round-trip serialisability.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hostlens.agent.diagnostician import DiagnosticianResult
from hostlens.agent.loop import LoopResult, LoopUsage
from hostlens.agent.planner import PlannerResult
from hostlens.reporting.models import (
    Finding,
    ReportStatus,
    RootCauseHypothesis,
    compute_finding_id,
)


def _loop_result(terminal: str = "ok") -> LoopResult:
    return LoopResult(
        final_text="diagnosis narrative",
        tool_invocations=[],
        turns=2,
        terminal_status=terminal,  # type: ignore[arg-type]
        usage_totals=LoopUsage(),
        stop_reason="end_turn",
    )


def _planner_result() -> PlannerResult:
    return PlannerResult(
        narrative="planner narrative",
        findings=[Finding(severity="warning", message="raw finding")],
        loop_result=_loop_result(),
        intent="why slow",
    )


def _stamped_finding(message: str = "stamped") -> Finding:
    fid = compute_finding_id("linux.load", "1.0.0", message)
    return Finding(
        severity="warning",
        message=message,
        id=fid,
        inspector_name="linux.load",
        inspector_version="1.0.0",
    )


def test_field_set_is_exactly_six_entries() -> None:
    expected = {
        "narrative",
        "findings",
        "hypotheses",
        "status",
        "planner_result",
        "diagnostician_loop",
    }
    assert set(DiagnosticianResult.model_fields.keys()) == expected
    assert len(DiagnosticianResult.model_fields) == 6


def test_construct_full_result() -> None:
    stamped = _stamped_finding()
    hypo = RootCauseHypothesis(
        description="load spike from runaway process",
        confidence="medium",
        supporting_findings=[stamped.id or ""],
        suggested_actions=["restart service"],
    )
    result = DiagnosticianResult(
        narrative="narrative",
        findings=[stamped],
        hypotheses=[hypo],
        status=ReportStatus.OK,
        planner_result=_planner_result(),
        diagnostician_loop=_loop_result(),
    )
    assert result.status is ReportStatus.OK
    assert result.findings[0].id == stamped.id
    assert result.hypotheses[0].supporting_findings == [stamped.id]


def test_diagnostician_loop_can_be_none() -> None:
    result = DiagnosticianResult(
        narrative="",
        findings=[],
        hypotheses=[],
        status=ReportStatus.DEGRADED_RATE_LIMITED,
        planner_result=_planner_result(),
        diagnostician_loop=None,
    )
    assert result.diagnostician_loop is None


def test_is_frozen() -> None:
    result = DiagnosticianResult(
        narrative="",
        findings=[],
        hypotheses=[],
        status=ReportStatus.OK,
        planner_result=_planner_result(),
        diagnostician_loop=None,
    )
    with pytest.raises(ValidationError):
        result.narrative = "mutated"  # type: ignore[misc]


def test_model_dump_json_serialisable() -> None:
    stamped = _stamped_finding()
    result = DiagnosticianResult(
        narrative="n",
        findings=[stamped],
        hypotheses=[
            RootCauseHypothesis(
                description="d",
                confidence="high",
                supporting_findings=[stamped.id or ""],
            )
        ],
        status=ReportStatus.OK,
        planner_result=_planner_result(),
        diagnostician_loop=_loop_result(),
    )
    json_text = result.model_dump_json()
    assert '"status":"ok"' in json_text
    # Round-trip back to confirm the JSON is a faithful, re-parseable encoding.
    reparsed = DiagnosticianResult.model_validate_json(json_text)
    assert reparsed.findings[0].id == stamped.id


def test_redact_preserves_secret_shaped_tag_without_validation_error() -> None:
    """`redact_diagnostician_result_for_render` must NOT mangle `Finding.tags`.

    A tag is constrained to `^[a-z][a-z0-9_-]*$`. A secret-shaped tag like
    `sk-aaaaaaaaaaaaaaaaaaaaaa` is a *legal* tag that ALSO matches the `sk-…`
    redact pattern — if the redactor ran `redact_text` over it, the masked value
    (`sk-a...aaaa`, with a `.`) would violate the Tag pattern and make
    `model_validate` raise. Tags must be preserved verbatim (parity with the
    Report path's `_redact_finding`), while the finding message is still redacted.
    """
    from hostlens.reporting._redact import redact_diagnostician_result_for_render

    secret_tag = "sk-aaaaaaaaaaaaaaaaaaaaaa"  # legal Tag AND matches sk- pattern
    secret_msg = "leak sk-deadbeefcafef00d1234567890ABCDEF here"
    finding = Finding(
        severity="critical",
        message=secret_msg,
        tags=[secret_tag],
        id=compute_finding_id("linux.load", "1.0.0", secret_msg),
        inspector_name="linux.load",
        inspector_version="1.0.0",
    )
    result = DiagnosticianResult(
        narrative="n",
        findings=[finding],
        hypotheses=[],
        status=ReportStatus.OK,
        planner_result=_planner_result(),
        diagnostician_loop=_loop_result(),
    )

    # Must not raise ValidationError (regression: tags were being redacted).
    redacted = redact_diagnostician_result_for_render(result)

    # Tag preserved verbatim; message secret masked.
    assert redacted.findings[0].tags == [secret_tag]
    assert "sk-deadbeefcafef00d1234567890ABCDEF" not in redacted.findings[0].message
    # Source untouched.
    assert result.findings[0].message == secret_msg


def test_redact_preserves_finding_identity_and_hypothesis_cross_reference() -> None:
    """Identifier fields must survive redaction verbatim (parity with `_redact_finding`).

    `redact_diagnostician_result_for_render` must NOT run `redact_text` over
    `Finding.id` / `inspector_name` / `inspector_version` or over
    `RootCauseHypothesis.supporting_findings` ids — otherwise an identifier that
    matched a redact pattern would either fail validation or silently break the
    hypothesis→finding cross-reference (evidence-link contract). Regression for
    the Cursor Bugbot finding; locks parity with the typed Report-path redactors.
    """
    from hostlens.reporting._redact import redact_diagnostician_result_for_render

    stamped = _stamped_finding("load spike")
    hypo = RootCauseHypothesis(
        description="leak sk-deadbeefcafef00d1234567890ABCDEF in description",
        confidence="high",
        supporting_findings=[stamped.id or ""],
        suggested_actions=["restart sk-deadbeefcafef00d1234567890ABCDEF"],
    )
    result = DiagnosticianResult(
        narrative="n",
        findings=[stamped],
        hypotheses=[hypo],
        status=ReportStatus.OK,
        planner_result=_planner_result(),
        diagnostician_loop=_loop_result(),
    )

    redacted = redact_diagnostician_result_for_render(result)

    # Identity fields preserved verbatim.
    assert redacted.findings[0].id == stamped.id
    assert redacted.findings[0].inspector_name == "linux.load"
    assert redacted.findings[0].inspector_version == "1.0.0"
    # Cross-reference intact: supporting_findings still points at a present id.
    assert redacted.hypotheses[0].supporting_findings == [stamped.id]
    assert redacted.hypotheses[0].supporting_findings[0] == redacted.findings[0].id
    # Free-text in the hypothesis is still redacted.
    assert "sk-deadbeefcafef00d1234567890ABCDEF" not in redacted.hypotheses[0].description
    assert all(
        "sk-deadbeefcafef00d1234567890ABCDEF" not in a
        for a in redacted.hypotheses[0].suggested_actions
    )
