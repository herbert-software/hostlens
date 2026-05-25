"""Tests for `InspectorRunner._evaluate_findings`.

Two evaluation modes — iterative (`for_each`) and aggregate — each with
their own context shape. The precise-except contract (design.md
Decision 7) means: per-rule DSL failures skip that single rule with a
structured warning; runner-internal bugs are NOT swallowed.
"""

from __future__ import annotations

import structlog

from hostlens.core.config import Settings
from hostlens.inspectors.runner import InspectorRunner
from hostlens.inspectors.schema import FindingRule
from hostlens.targets.registry import TargetRegistry


def _runner() -> InspectorRunner:
    return InspectorRunner(
        TargetRegistry(),
        settings=Settings(),
        logger=structlog.get_logger("test"),
    )


# ---------------------------------------------------------------------- #
# Iterative mode
# ---------------------------------------------------------------------- #


async def test_iterative_mode_produces_one_finding_per_match() -> None:
    runner = _runner()
    rule = FindingRule(
        for_each="processes as p",
        when="p > 50",
        severity="warning",
        message="proc value high: {p}",
    )
    output = {"processes": [10, 60, 30, 80]}
    findings = await runner._evaluate_findings([rule], output, None)
    # Only items > 50 produce a finding.
    assert len(findings) == 2
    assert findings[0].message == "proc value high: 60"
    assert findings[1].message == "proc value high: 80"
    assert all(f.severity == "warning" for f in findings)
    # Evidence carries the for_each variable.
    assert findings[0].evidence == {"p": "60"}
    assert findings[1].evidence == {"p": "80"}


async def test_iterative_mode_skip_on_evaluate_failure() -> None:
    """A `when` that references an unbound name skips the iteration but
    other iterations continue. Other RULES are unaffected."""

    runner = _runner()
    rule = FindingRule(
        for_each="items as it",
        when="it.nonexistent > 50",  # AttributeDoesNotExist via simpleeval
        severity="info",
        message="should not appear",
    )
    output = {"items": [1, 2, 3]}
    findings = await runner._evaluate_findings([rule], output, None)
    # Every iteration's `when` fails, so no findings.
    assert findings == []


# ---------------------------------------------------------------------- #
# Aggregate mode
# ---------------------------------------------------------------------- #


async def test_aggregate_mode_produces_zero_when_false() -> None:
    runner = _runner()
    rule = FindingRule(
        when="len(items) > 100",
        severity="info",
        message="too many",
    )
    output = {"items": [1, 2, 3]}
    findings = await runner._evaluate_findings([rule], output, None)
    assert findings == []


async def test_aggregate_mode_produces_one_when_true() -> None:
    runner = _runner()
    rule = FindingRule(
        when="len(items) > 2",
        severity="critical",
        message="items count = {count}",
    )
    output = {"items": [1, 2, 3], "count": 3}
    findings = await runner._evaluate_findings([rule], output, None)
    assert len(findings) == 1
    assert findings[0].severity == "critical"
    assert findings[0].message == "items count = 3"
    assert findings[0].evidence == {}


# ---------------------------------------------------------------------- #
# Per-rule isolation
# ---------------------------------------------------------------------- #


async def test_single_rule_dsl_failure_skips_that_rule_only() -> None:
    """Rule[0]'s `when` raises NameNotDefined; rule[1] still runs."""

    runner = _runner()
    rule_a = FindingRule(
        when="nonexistent_name > 0",
        severity="info",
        message="A",
    )
    rule_b = FindingRule(
        when="x > 0",
        severity="warning",
        message="B-{x}",
    )
    output = {"x": 5}
    findings = await runner._evaluate_findings([rule_a, rule_b], output, None)
    assert len(findings) == 1
    assert findings[0].message == "B-5"
    assert findings[0].severity == "warning"


async def test_format_message_keyerror_skips_rule() -> None:
    """`message` template references a missing variable — rule skipped."""

    runner = _runner()
    # `when` evaluates to True but `message` references an unbound key.
    rule = FindingRule(
        when="x > 0",
        severity="info",
        message="hello {missing_var}",
    )
    output = {"x": 1}
    findings = await runner._evaluate_findings([rule], output, None)
    # KeyError caught at format_message call site → finding skipped.
    assert findings == []


async def test_when_non_bool_truthy_produces_finding() -> None:
    """Spec doesn't require strict bool — Pythonic truthy is enough."""

    runner = _runner()
    rule = FindingRule(
        when="len(items)",  # returns int 3 (truthy)
        severity="info",
        message="count is high",
    )
    output = {"items": [1, 2, 3]}
    findings = await runner._evaluate_findings([rule], output, None)
    assert len(findings) == 1


async def test_when_returns_zero_no_finding() -> None:
    runner = _runner()
    rule = FindingRule(
        when="len(items)",  # returns int 0 (falsy)
        severity="info",
        message="x",
    )
    output: dict[str, list[int]] = {"items": []}
    findings = await runner._evaluate_findings([rule], output, None)
    assert findings == []


async def test_findings_preserve_manifest_order() -> None:
    runner = _runner()
    rules = [FindingRule(when="x > 0", severity="info", message=f"{i}") for i in range(5)]
    output = {"x": 1}
    findings = await runner._evaluate_findings(rules, output, None)
    assert [f.message for f in findings] == ["0", "1", "2", "3", "4"]


# ---------------------------------------------------------------------- #
# Parameters merge into context
# ---------------------------------------------------------------------- #


async def test_parameters_merge_into_context() -> None:
    runner = _runner()
    rule = FindingRule(
        when="threshold > 5",
        severity="info",
        message="threshold is {threshold}",
    )
    output: dict[str, object] = {}
    findings = await runner._evaluate_findings([rule], output, {"threshold": 10})
    assert len(findings) == 1
    assert findings[0].message == "threshold is 10"


async def test_iterative_for_each_non_iterable_skips_rule() -> None:
    """If the iterable expression returns a non-iterable, skip the rule."""

    runner = _runner()
    rule = FindingRule(
        for_each="value as v",
        when="v > 0",
        severity="info",
        message="x",
    )
    output = {"value": 42}  # int, not iterable
    findings = await runner._evaluate_findings([rule], output, None)
    assert findings == []
