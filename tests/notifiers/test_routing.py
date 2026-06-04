"""Unit tests for ``notifiers/routing.py`` (task 3.4 — routing half).

Spec: ``openspec/changes/add-notifier-channels/specs/notify-routing/spec.md``
(§需求:`only_if` 路由必须复用硬化 DSL 求值器并对 severity 做有序比较 /
§需求:`only_if` 运行期求值异常必须归类为通道失败且隔离).

Covers:

- aggregate severity = max-by-rank (not string order), empty → info;
- tags union across findings;
- severity-threshold + tag-membership routing true/false;
- empty-string / forbidden ``only_if`` fail loud at load time;
- run-time eval exceptions → ``NotifyResult(failed)``, never bubble,
  including a type **not** in the spec's enumerated list
  (``simpleeval.InvalidExpression``) to prove the catch is wholesale.

``asyncio_mode = "auto"`` (pyproject) — async tests need no marker.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import pytest
import simpleeval

from hostlens.inspectors.result import InspectorResult
from hostlens.notifiers.routing import (
    aggregate_severity,
    collect_tags,
    evaluate_only_if,
    should_send,
    validate_only_if,
)
from hostlens.reporting.models import Finding, Report, Severity


def _report(findings: list[Finding]) -> Report:
    ir = InspectorResult(
        name="x",
        version="1.0.0",
        status="ok",
        target_name="t",
        duration_seconds=0.01,
        output={},
        findings=findings,
        error=None,
        missing=[],
    )
    t = datetime(2026, 6, 4, 12, 0, 0)
    return Report(
        report_id=UUID("12345678-1234-5678-1234-567812345678"),
        schema_version="1.0",
        intent=None,
        target_name="t",
        inspector_results=[ir],
        findings=findings,
        started_at=t,
        finished_at=t,
        metadata={},
    )


# --------------------------------------------------------------------------- #
# aggregate_severity
# --------------------------------------------------------------------------- #


def test_aggregate_severity_empty_is_info() -> None:
    assert aggregate_severity(_report([])) == "info"


@pytest.mark.parametrize(
    ("severities", "expected"),
    [
        (["info"], "info"),
        (["info", "warning"], "warning"),
        (["warning", "critical", "info"], "critical"),
        (["info", "info"], "info"),
    ],
)
def test_aggregate_severity_max_by_rank(severities: list[Severity], expected: Severity) -> None:
    findings = [Finding(severity=s, message=f"m-{i}") for i, s in enumerate(severities)]
    assert aggregate_severity(_report(findings)) == expected


def test_aggregate_severity_not_string_lexicographic() -> None:
    # Lexicographically "critical" < "info" < "warning"; a string-max would
    # pick "warning" here, but the rank-max must pick "critical".
    findings = [
        Finding(severity="warning", message="w"),
        Finding(severity="critical", message="c"),
    ]
    assert aggregate_severity(_report(findings)) == "critical"


# --------------------------------------------------------------------------- #
# collect_tags
# --------------------------------------------------------------------------- #


def test_collect_tags_union_sorted_deduped() -> None:
    findings = [
        Finding(severity="info", message="a", tags=["disk_full", "cpu"]),
        Finding(severity="warning", message="b", tags=["cpu", "mem"]),
    ]
    assert collect_tags(_report(findings)) == ["cpu", "disk_full", "mem"]


def test_collect_tags_empty() -> None:
    assert collect_tags(_report([])) == []


# --------------------------------------------------------------------------- #
# validate_only_if (load time)
# --------------------------------------------------------------------------- #


def test_validate_only_if_accepts_threshold() -> None:
    validate_only_if("severity >= warning")  # no raise


def test_validate_only_if_accepts_whitelisted_call() -> None:
    validate_only_if("len(tags) > 0")  # whitelisted call not rejected


def test_validate_only_if_empty_string_fail_loud() -> None:
    with pytest.raises(simpleeval.FeatureNotAvailable):
        validate_only_if("")


def test_validate_only_if_rejects_lambda() -> None:
    with pytest.raises(simpleeval.FeatureNotAvailable):
        validate_only_if("(lambda: 1)()")


def test_validate_only_if_rejects_dunder_import() -> None:
    with pytest.raises(simpleeval.FeatureNotAvailable):
        validate_only_if("__import__('os')")


# --------------------------------------------------------------------------- #
# evaluate_only_if / should_send (run time, happy path)
# --------------------------------------------------------------------------- #


async def test_severity_threshold_true() -> None:
    report = _report([Finding(severity="warning", message="w")])
    assert await evaluate_only_if("severity >= warning", report) is True
    assert await should_send("c", "severity >= warning", report) is None


async def test_severity_threshold_false_is_skipped() -> None:
    report = _report([Finding(severity="info", message="i")])
    assert await evaluate_only_if("severity >= warning", report) is False
    result = await should_send("c", "severity >= warning", report)
    assert result is not None
    assert result.status == "skipped"
    assert result.channel == "c"


async def test_tag_membership_true_and_false() -> None:
    has_tag = _report([Finding(severity="info", message="i", tags=["disk_full"])])
    no_tag = _report([Finding(severity="info", message="i", tags=["cpu"])])
    assert await evaluate_only_if("'disk_full' in tags", has_tag) is True
    assert await evaluate_only_if("'disk_full' in tags", no_tag) is False
    assert await should_send("c", "'disk_full' in tags", no_tag) is not None


async def test_none_only_if_always_sends() -> None:
    report = _report([Finding(severity="info", message="i")])
    assert await should_send("c", None, report) is None


# --------------------------------------------------------------------------- #
# should_send (run time, failure isolation)
# --------------------------------------------------------------------------- #


async def test_type_mismatch_runtime_failed_not_bubbled() -> None:
    # ``severity`` is an int rank; comparing against a string literal raises
    # TypeError at run time (passes validate_ast at load time).
    report = _report([Finding(severity="warning", message="w")])
    result = await should_send("c", "severity >= 'warning'", report)
    assert result is not None
    assert result.status == "failed"
    assert result.error is not None
    assert result.channel == "c"


async def test_undefined_name_runtime_failed() -> None:
    report = _report([Finding(severity="warning", message="w")])
    result = await should_send("c", "severty >= warning", report)
    assert result is not None
    assert result.status == "failed"


async def test_catch_all_covers_non_enumerated_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Force ``evaluate`` to raise a simpleeval class NOT in the spec's
    # enumerated subset (InvalidExpression) to prove the catch is wholesale
    # rather than a curated tuple of exception types.
    async def _boom(*_a: object, **_k: object) -> object:
        raise simpleeval.InvalidExpression("synthetic non-enumerated failure")

    monkeypatch.setattr("hostlens.notifiers.routing.dsl.evaluate", _boom)

    report = _report([Finding(severity="info", message="i")])
    result = await should_send("c", "severity >= warning", report)
    assert result is not None
    assert result.status == "failed"
    assert result.error is not None
