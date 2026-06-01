"""Tests for `hostlens.inspectors.result` — `Finding` and `InspectorResult`.

`InspectorResult.status` carries four cross-field invariants that the
M2 Planner Agent relies on: `ok` must have no error / no missing,
`requires_unmet` must have non-empty missing, and the three exception-class
states (`timeout` / `target_unreachable` / `exception`) must not carry any
missing entries. Each rule has its own test.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hostlens.inspectors.result import Finding, InspectorResult

# --------------------------------------------------------------------------- #
# Finding
# --------------------------------------------------------------------------- #


class TestFinding:
    def test_minimal_finding_accepted(self) -> None:
        f = Finding(severity="info", message="hello")
        assert f.severity == "info"
        assert f.evidence == []

    @pytest.mark.parametrize("severity", ["info", "warning", "critical"])
    def test_all_severities_accepted(self, severity: str) -> None:
        f = Finding(severity=severity, message="x")  # type: ignore[arg-type]
        assert f.severity == severity

    @pytest.mark.parametrize("severity", ["high", "error", "INFO"])
    def test_invalid_severity_rejected(self, severity: str) -> None:
        with pytest.raises(ValidationError):
            Finding(severity=severity, message="x")  # type: ignore[arg-type]

    def test_evidence_dict_form_rejected(self) -> None:
        # `evidence: list[Evidence]` post-`add-report-data-model` BREAKING.
        # Passing the legacy `dict[str, str]` shape must surface as a
        # `ValidationError` (no silent coercion) — callers must construct
        # `Evidence` instances explicitly.
        with pytest.raises(ValidationError):
            Finding(severity="info", message="x", evidence={"k": "v"})  # type: ignore[arg-type]

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Finding(severity="info", message="x", not_a_field="abc")  # type: ignore[call-arg]

    def test_identity_fields_default_to_none(self) -> None:
        # `id` / `inspector_name` / `inspector_version` are M3 add-only
        # identity fields that default to None on direct M1/M2 construction;
        # `Report.from_inspector_results` populates them on the flattened copies.
        f = Finding(severity="info", message="x")
        assert f.id is None
        assert f.inspector_name is None
        assert f.inspector_version is None

    def test_identity_fields_accepted_when_set(self) -> None:
        f = Finding(
            severity="info",
            message="x",
            id="deadbeef",
            inspector_name="linux.memory.pressure",
            inspector_version="1.0.0",
        )
        assert f.id == "deadbeef"
        assert f.inspector_name == "linux.memory.pressure"
        assert f.inspector_version == "1.0.0"

    def test_instance_is_immutable(self) -> None:
        f = Finding(severity="info", message="x")
        with pytest.raises(ValidationError):
            f.severity = "warning"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# InspectorResult — status invariants
# --------------------------------------------------------------------------- #


def _base_kwargs() -> dict[str, object]:
    return {
        "name": "hello.echo",
        "version": "1.0.0",
        "target_name": "local-host",
        "duration_seconds": 0.5,
    }


class TestInspectorResultOk:
    def test_ok_with_no_error_no_missing_accepted(self) -> None:
        r = InspectorResult(status="ok", **_base_kwargs())  # type: ignore[arg-type]
        assert r.status == "ok"
        assert r.error is None
        assert r.missing == []

    def test_ok_with_error_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            InspectorResult(status="ok", error="bad", **_base_kwargs())  # type: ignore[arg-type]
        assert "ok_status_with_error" in exc_info.value.errors()[0]["msg"]

    def test_ok_with_missing_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            InspectorResult(status="ok", missing=["env:X"], **_base_kwargs())  # type: ignore[arg-type]
        assert "ok_status_with_missing" in exc_info.value.errors()[0]["msg"]


class TestInspectorResultRequiresUnmet:
    def test_requires_unmet_with_missing_accepted(self) -> None:
        r = InspectorResult(
            status="requires_unmet",
            missing=["env:PGPASSWORD"],
            **_base_kwargs(),  # type: ignore[arg-type]
        )
        assert r.missing == ["env:PGPASSWORD"]

    def test_requires_unmet_without_missing_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            InspectorResult(status="requires_unmet", **_base_kwargs())  # type: ignore[arg-type]
        assert "requires_unmet_status_without_missing" in exc_info.value.errors()[0]["msg"]


class TestInspectorResultExceptionStatuses:
    @pytest.mark.parametrize("status", ["timeout", "target_unreachable", "exception"])
    def test_status_without_missing_accepted(self, status: str) -> None:
        r = InspectorResult(status=status, error="something", **_base_kwargs())  # type: ignore[arg-type]
        assert r.status == status
        assert r.missing == []

    @pytest.mark.parametrize("status", ["timeout", "target_unreachable", "exception"])
    def test_status_with_missing_rejected(self, status: str) -> None:
        with pytest.raises(ValidationError) as exc_info:
            InspectorResult(
                status=status,
                missing=["env:X"],
                error="x",
                **_base_kwargs(),  # type: ignore[arg-type]
            )
        assert f"{status}_status_with_missing" in exc_info.value.errors()[0]["msg"]

    @pytest.mark.parametrize("status", ["timeout", "target_unreachable", "exception"])
    def test_status_without_error_rejected(self, status: str) -> None:
        """Archived inspector-plugin-system spec §需求:`InspectorResult` Pydantic
        模型字段集 — `status != "ok"` requires a non-empty error description so
        the rendered Report surfaces the root cause."""

        with pytest.raises(ValidationError) as exc_info:
            InspectorResult(status=status, **_base_kwargs())  # type: ignore[arg-type]
        assert f"{status}_status_without_error" in exc_info.value.errors()[0]["msg"]

    @pytest.mark.parametrize("status", ["timeout", "target_unreachable", "exception"])
    def test_status_with_blank_error_rejected(self, status: str) -> None:
        with pytest.raises(ValidationError) as exc_info:
            InspectorResult(status=status, error="   ", **_base_kwargs())  # type: ignore[arg-type]
        assert f"{status}_status_without_error" in exc_info.value.errors()[0]["msg"]


class TestInspectorResultMisc:
    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            InspectorResult(
                status="ok",
                unknown_field="x",  # type: ignore[call-arg]
                **_base_kwargs(),  # type: ignore[arg-type]
            )

    def test_instance_is_immutable(self) -> None:
        r = InspectorResult(status="ok", **_base_kwargs())  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            r.status = "exception"  # type: ignore[misc]

    def test_findings_can_be_attached(self) -> None:
        f = Finding(severity="info", message="hi")
        r = InspectorResult(status="ok", findings=[f], **_base_kwargs())  # type: ignore[arg-type]
        assert r.findings == [f]
