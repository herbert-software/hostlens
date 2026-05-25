"""Tests for `hostlens.inspectors.schema.FindingRule` DSL validation.

The four covered failure modes are the static checks the loader needs to
catch **before** any runtime evaluation: malformed `for_each` form,
non-compilable `when`, aggregate-mode `message` referencing per-iteration
attributes, and severity outside the three-value enum.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hostlens.inspectors.schema import FindingRule


class TestFindingRuleSeverity:
    @pytest.mark.parametrize("severity", ["info", "warning", "critical"])
    def test_valid_severity_accepted(self, severity: str) -> None:
        rule = FindingRule(when="1", severity=severity, message="x")  # type: ignore[arg-type]
        assert rule.severity == severity

    @pytest.mark.parametrize("severity", ["high", "error", "INFO", "Warning", ""])
    def test_invalid_severity_rejected(self, severity: str) -> None:
        with pytest.raises(ValidationError):
            FindingRule(when="1", severity=severity, message="x")  # type: ignore[arg-type]


class TestFindingRuleForEach:
    def test_for_each_missing_as_separator_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            FindingRule(
                for_each="processes p",
                when="p.cpu > 70",
                severity="warning",
                message="x",
            )
        assert "finding_when_invalid" in exc_info.value.errors()[0]["msg"]

    def test_for_each_valid_form_accepted(self) -> None:
        rule = FindingRule(
            for_each="rows as r",
            when="r.cpu > 70",
            severity="warning",
            message="cpu high: {r.cpu}",
        )
        assert rule.for_each == "rows as r"

    def test_for_each_var_name_with_uppercase_rejected(self) -> None:
        with pytest.raises(ValidationError):
            FindingRule(
                for_each="rows as R",
                when="R.cpu > 70",
                severity="warning",
                message="x",
            )

    def test_for_each_iterable_expr_with_function_call_accepted(self) -> None:
        # `processes` may be unbound at validation context but the expression
        # itself parses — validator must not reject this.
        rule = FindingRule(
            for_each="processes as p",
            when="p.cpu > 70",
            severity="warning",
            message="high cpu: {p.cpu}",
        )
        assert rule.for_each == "processes as p"


class TestFindingRuleWhen:
    def test_when_syntax_error_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            FindingRule(when="p.cpu > >", severity="warning", message="x")
        msg = exc_info.value.errors()[0]["msg"]
        assert "finding_when_invalid" in msg

    def test_when_referencing_unbound_name_accepted(self) -> None:
        # NameNotDefined / FunctionNotDefined at empty context = "compiles".
        rule = FindingRule(
            when="len(processes) > 5", severity="info", message="too many"
        )
        assert rule.when == "len(processes) > 5"

    def test_when_lambda_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            FindingRule(when="(lambda: 1)()", severity="info", message="x")
        assert "finding_when_invalid" in exc_info.value.errors()[0]["msg"]


class TestFindingRuleAggregateMessage:
    def test_aggregate_mode_with_attr_reference_rejected(self) -> None:
        # for_each=None means aggregate mode; the message references {p.cpu}
        # which has no per-iteration binding context — must reject statically.
        with pytest.raises(ValidationError) as exc_info:
            FindingRule(
                when="len(processes) > 5",
                severity="info",
                message="Found {p.command} using lots of CPU",
            )
        assert (
            "finding_message_invalid_aggregate_ref" in exc_info.value.errors()[0]["msg"]
        )

    def test_aggregate_mode_with_plain_field_ref_accepted(self) -> None:
        # `{name}` (no dot) is valid in aggregate mode — refers to an output
        # field or parameter, not a per-iteration attribute.
        rule = FindingRule(
            when="len(processes) > 5",
            severity="info",
            message="Found too many: {name}",
        )
        assert rule.message == "Found too many: {name}"

    def test_for_each_mode_with_attr_reference_accepted(self) -> None:
        # In for_each mode the loop variable's attributes are legitimate.
        rule = FindingRule(
            for_each="processes as p",
            when="p.cpu > 70",
            severity="warning",
            message="cpu={p.cpu} cmd={p.command}",
        )
        assert rule.for_each == "processes as p"


class TestFindingRuleStaticAstGate:
    """The schema-layer compile gate must apply the same static AST rejection
    that the runtime DSL evaluator uses.

    Without the gate, constructs like `__import__('os')` slip past
    `SimpleEval.eval(expr)` at empty context (it merely surfaces a
    NameNotDefined on the `__import__` name) and only fail at runtime —
    violating the loader's "static rejection" principle.
    """

    @pytest.mark.parametrize(
        "expr",
        [
            "__import__('os')",
            "(lambda: 1)()",
            "[x for x in range(10)]",
            "''.__class__",
            "().__class__.__base__.__subclasses__()",
        ],
    )
    def test_dangerous_expression_rejected_at_load(self, expr: str) -> None:
        with pytest.raises(ValidationError) as exc_info:
            FindingRule(when=expr, severity="info", message="x")
        assert "finding_when_invalid" in exc_info.value.errors()[0]["msg"]

    def test_valid_expression_accepted(self) -> None:
        # Sanity: a benign `len(...)` reference must still pass.
        rule = FindingRule(
            when="len(processes) > 0", severity="info", message="x"
        )
        assert rule.when == "len(processes) > 0"

    @pytest.mark.parametrize(
        "iterable_expr",
        [
            "__import__('os')",
            "[x for x in range(10)]",
            "(lambda: [])()",
        ],
    )
    def test_dangerous_for_each_iterable_rejected(self, iterable_expr: str) -> None:
        with pytest.raises(ValidationError) as exc_info:
            FindingRule(
                for_each=f"{iterable_expr} as p",
                when="True",
                severity="info",
                message="x",
            )
        assert "finding_when_invalid" in exc_info.value.errors()[0]["msg"]


class TestFindingRuleFrozen:
    def test_instance_is_immutable(self) -> None:
        rule = FindingRule(when="1", severity="info", message="x")
        with pytest.raises(ValidationError):
            rule.when = "0"  # type: ignore[misc]

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            FindingRule(
                when="1",
                severity="info",
                message="x",
                weird_extra="no",  # type: ignore[call-arg]
            )
