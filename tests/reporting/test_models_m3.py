"""Tests for the M3 add-only report-data-model extensions.

Covers spec §需求 (逐条对照场景):

- `ReportStatus` 八值闭集 + `failed_api_unavailable` 被拒
- `TokenUsage` / `InspectorRun` / `BaselineRef` / `RootCauseHypothesis` /
  `ReportMeta` 字段集
- `Finding` add-only 身份字段默认 None / legacy dict 加载 / 仍 extra=forbid
- `Report` meta/hypotheses 默认 + schema_version 放宽 1.0/1.1 + legacy 1.0 JSON
- `Report.from_inspector_results` status 派生四场景 + meta 投影 + override +
  token_usage 缺省全零 + 空列表 ValueError
"""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

import hostlens.inspectors.result  # noqa: F401  # triggers Report.model_rebuild
from hostlens.inspectors.result import InspectorResult
from hostlens.reporting.models import (
    BaselineRef,
    Finding,
    InspectorRun,
    Report,
    ReportMeta,
    ReportStatus,
    RootCauseHypothesis,
    TokenUsage,
)


def _t0() -> datetime:
    return datetime(2026, 5, 26, 12, 0, 0)


def _t1() -> datetime:
    return datetime(2026, 5, 26, 12, 0, 2)


def _ir(
    name: str,
    *,
    version: str = "1.0",
    status: str = "ok",
    findings: list[Finding] | None = None,
    duration_seconds: float = 0.1,
) -> InspectorResult:
    kwargs: dict[str, object] = {
        "name": name,
        "version": version,
        "status": status,
        "target_name": "t",
        "duration_seconds": duration_seconds,
        "findings": findings if findings is not None else [],
    }
    if status in ("timeout", "target_unreachable", "exception"):
        kwargs["error"] = f"{status} happened"
    if status == "requires_unmet":
        kwargs["missing"] = ["needs_root"]
    return InspectorResult(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# ReportStatus
# --------------------------------------------------------------------------- #


def test_report_status_eight_legal_values() -> None:
    for v in (
        "ok",
        "partial",
        "degraded_no_planner",
        "degraded_rate_limited",
        "degraded_token_budget",
        "degraded_max_turns",
        "empty_response",
        "stored_as_orphan",
    ):
        assert ReportStatus(v).value == v


def test_report_status_rejects_failed_api_unavailable() -> None:
    with pytest.raises(ValueError):
        ReportStatus("failed_api_unavailable")


# --------------------------------------------------------------------------- #
# TokenUsage
# --------------------------------------------------------------------------- #


def test_token_usage_defaults_all_zero() -> None:
    tu = TokenUsage()
    assert tu.input_tokens == 0
    assert tu.output_tokens == 0
    assert tu.cache_creation_input_tokens == 0
    assert tu.cache_read_input_tokens == 0


def test_token_usage_field_set_strict() -> None:
    with pytest.raises(ValidationError):
        TokenUsage(not_a_field=1)  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# InspectorRun
# --------------------------------------------------------------------------- #


def test_inspector_run_field_set() -> None:
    run = InspectorRun(name="x", version="1.2", status="ok", duration_seconds=0.3, finding_count=1)
    assert run.name == "x"
    assert run.version == "1.2"
    assert run.status == "ok"
    assert run.duration_seconds == 0.3
    assert run.finding_count == 1


def test_inspector_run_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        InspectorRun(name="x", version="1.0", status="bogus", duration_seconds=0.1, finding_count=0)


def test_inspector_run_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        InspectorRun(
            name="x",
            version="1.0",
            status="ok",
            duration_seconds=0.1,
            finding_count=0,
            extra="y",  # type: ignore[call-arg]
        )


# --------------------------------------------------------------------------- #
# BaselineRef
# --------------------------------------------------------------------------- #


def test_baseline_ref_field_set() -> None:
    ref = BaselineRef(
        run_id="r1",
        timestamp=_t0(),
        status=ReportStatus.OK,
        inspector_versions={"insp.a": "1.0"},
        report_schema_version="1.1",
    )
    assert ref.run_id == "r1"
    assert ref.status == ReportStatus.OK
    assert ref.inspector_versions == {"insp.a": "1.0"}
    assert ref.report_schema_version == "1.1"


def test_baseline_ref_inspector_versions_default_empty() -> None:
    ref = BaselineRef(
        run_id="r1", timestamp=_t0(), status=ReportStatus.OK, report_schema_version="1.1"
    )
    assert ref.inspector_versions == {}


# --------------------------------------------------------------------------- #
# RootCauseHypothesis
# --------------------------------------------------------------------------- #


def test_root_cause_hypothesis_field_set() -> None:
    h = RootCauseHypothesis(
        description="oom",
        confidence="high",
        supporting_findings=["abc123"],
        suggested_actions=["restart"],
    )
    assert h.description == "oom"
    assert h.confidence == "high"
    assert h.supporting_findings == ["abc123"]
    assert h.suggested_actions == ["restart"]


def test_root_cause_hypothesis_defaults_empty_lists() -> None:
    h = RootCauseHypothesis(description="x", confidence="low")
    assert h.supporting_findings == []
    assert h.suggested_actions == []


def test_root_cause_hypothesis_rejects_invalid_confidence() -> None:
    with pytest.raises(ValidationError):
        RootCauseHypothesis(description="x", confidence="maybe")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# ReportMeta
# --------------------------------------------------------------------------- #


def test_report_meta_field_set_and_defaults() -> None:
    meta = ReportMeta(
        run_id="r1",
        timestamp=_t0(),
        target_id="t",
        target_name="t",
        target_type="local",
        status=ReportStatus.OK,
        duration_seconds=1.0,
    )
    assert meta.report_schema_version == "1.1"
    assert meta.intent is None
    assert meta.schedule_name is None
    assert meta.inspectors_used == []
    assert meta.token_usage == TokenUsage()
    assert meta.baseline_ref is None
    assert meta.diff_skipped_reason is None


def test_report_meta_target_type_accepts_non_literal() -> None:
    meta = ReportMeta(
        run_id="r1",
        timestamp=_t0(),
        target_id="t",
        target_name="t",
        target_type="replay",
        status=ReportStatus.OK,
        duration_seconds=1.0,
    )
    assert meta.target_type == "replay"


def test_report_meta_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        ReportMeta(
            run_id="r1",
            timestamp=_t0(),
            target_id="t",
            target_name="t",
            target_type="local",
            status=ReportStatus.OK,
            duration_seconds=1.0,
            extra="y",  # type: ignore[call-arg]
        )


# --------------------------------------------------------------------------- #
# Finding add-only identity fields
# --------------------------------------------------------------------------- #


def test_finding_identity_fields_default_none() -> None:
    f = Finding(severity="info", message="ok")
    assert f.id is None
    assert f.inspector_name is None
    assert f.inspector_version is None


def test_finding_accepts_explicit_identity_fields() -> None:
    f = Finding(
        severity="warning",
        message="cpu high",
        id="abc123",
        inspector_name="linux.cpu.top_processes",
        inspector_version="1.0.0",
    )
    assert f.id == "abc123"
    assert f.inspector_name == "linux.cpu.top_processes"
    assert f.inspector_version == "1.0.0"


def test_finding_legacy_dict_without_identity_fields_loads() -> None:
    f = Finding.model_validate({"severity": "info", "message": "x"})
    assert f.id is None
    assert f.inspector_name is None
    assert f.inspector_version is None


def test_finding_still_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        Finding(severity="info", message="x", not_a_field="y")  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# Report meta / hypotheses / schema_version
# --------------------------------------------------------------------------- #


def _meta() -> ReportMeta:
    return ReportMeta(
        run_id="r1",
        timestamp=_t0(),
        target_id="t",
        target_name="t",
        target_type="local",
        status=ReportStatus.OK,
        duration_seconds=1.0,
    )


def _report_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "report_id": __import__("uuid").uuid4(),
        "schema_version": "1.0",
        "target_name": "t",
        "inspector_results": [_ir("a")],
        "started_at": _t0(),
        "finished_at": _t1(),
    }
    base.update(overrides)
    return base


def test_report_schema_version_accepts_1_0_and_1_1() -> None:
    r10 = Report(**_report_kwargs(schema_version="1.0"))  # type: ignore[arg-type]
    assert r10.schema_version == "1.0"
    r11 = Report(**_report_kwargs(schema_version="1.1", meta=_meta()))  # type: ignore[arg-type]
    assert r11.schema_version == "1.1"
    assert r11.meta is not None


def test_report_schema_version_rejects_2_0() -> None:
    with pytest.raises(ValidationError):
        Report(**_report_kwargs(schema_version="2.0"))  # type: ignore[arg-type]


def test_report_meta_default_none_and_hypotheses_default_empty() -> None:
    r = Report(**_report_kwargs())  # type: ignore[arg-type]
    assert r.meta is None
    assert r.hypotheses == []


def test_report_legacy_1_0_json_without_meta_loads() -> None:
    legacy = {
        "report_id": str(__import__("uuid").uuid4()),
        "schema_version": "1.0",
        "target_name": "t",
        "inspector_results": [
            {
                "name": "a",
                "version": "1.0",
                "status": "ok",
                "target_name": "t",
                "duration_seconds": 0.1,
                "findings": [],
            }
        ],
        "started_at": "2026-05-26T12:00:00",
        "finished_at": "2026-05-26T12:00:02",
    }
    r = Report.model_validate(legacy)
    assert r.meta is None
    assert r.hypotheses == []


# --------------------------------------------------------------------------- #
# Factory: identity-field population + meta projection
# --------------------------------------------------------------------------- #


def test_factory_flatten_fills_identity_fields() -> None:
    f1 = Finding(severity="info", message="m1")
    f2 = Finding(severity="warning", message="m2")
    f3 = Finding(severity="critical", message="m3")
    ir_a = _ir("insp.a", version="1.0", findings=[f1, f2])
    ir_b = _ir("insp.b", version="2.0", findings=[f3])
    r = Report.from_inspector_results("t", [ir_a, ir_b], started_at=_t0(), finished_at=_t1())
    assert [f.message for f in r.findings] == ["m1", "m2", "m3"]
    assert r.findings[0].inspector_name == "insp.a"
    assert r.findings[0].inspector_version == "1.0"
    assert r.findings[2].inspector_name == "insp.b"
    assert r.findings[2].inspector_version == "2.0"
    assert all(f.id is not None for f in r.findings)


def test_factory_writes_schema_1_1_and_meta() -> None:
    r = Report.from_inspector_results("t", [_ir("a")], started_at=_t0(), finished_at=_t1())
    assert r.schema_version == "1.1"
    assert r.meta is not None


def test_factory_meta_run_id_equals_report_id() -> None:
    r = Report.from_inspector_results("t", [_ir("a")], started_at=_t0(), finished_at=_t1())
    assert r.meta is not None
    assert r.meta.run_id == str(r.report_id)


def test_factory_meta_target_id_defaults_to_target_name() -> None:
    r = Report.from_inspector_results("t", [_ir("a")], started_at=_t0(), finished_at=_t1())
    assert r.meta is not None
    assert r.meta.target_id == "t"


def test_factory_meta_timestamp_and_duration() -> None:
    r = Report.from_inspector_results("t", [_ir("a")], started_at=_t0(), finished_at=_t1())
    assert r.meta is not None
    assert r.meta.timestamp == _t0()
    assert r.meta.duration_seconds == 2.0


def test_factory_inspectors_used_projection() -> None:
    f1 = Finding(severity="info", message="x")
    f2 = Finding(severity="info", message="y")
    ir = _ir("x", version="1.2", status="ok", findings=[f1, f2], duration_seconds=0.5)
    r = Report.from_inspector_results("t", [ir], started_at=_t0(), finished_at=_t1())
    assert r.meta is not None
    assert r.meta.inspectors_used == [
        InspectorRun(name="x", version="1.2", status="ok", duration_seconds=0.5, finding_count=2)
    ]


def test_factory_token_usage_defaults_all_zero() -> None:
    r = Report.from_inspector_results("t", [_ir("a")], started_at=_t0(), finished_at=_t1())
    assert r.meta is not None
    assert r.meta.token_usage == TokenUsage()


# --------------------------------------------------------------------------- #
# Factory: status derivation (§9 alignment)
# --------------------------------------------------------------------------- #


def test_status_all_ok_derives_ok() -> None:
    r = Report.from_inspector_results(
        "t", [_ir("a"), _ir("b")], started_at=_t0(), finished_at=_t1()
    )
    assert r.meta is not None
    assert r.meta.status == ReportStatus.OK


def test_status_partial_timeout_with_ok_stays_ok() -> None:
    r = Report.from_inspector_results(
        "t",
        [_ir("a", status="ok"), _ir("b", status="timeout")],
        started_at=_t0(),
        finished_at=_t1(),
    )
    assert r.meta is not None
    assert r.meta.status == ReportStatus.OK


def test_status_all_timeout_derives_partial() -> None:
    r = Report.from_inspector_results(
        "t",
        [_ir("a", status="timeout"), _ir("b", status="timeout")],
        started_at=_t0(),
        finished_at=_t1(),
    )
    assert r.meta is not None
    assert r.meta.status == ReportStatus.PARTIAL


def test_status_target_unreachable_derives_partial() -> None:
    r = Report.from_inspector_results(
        "t",
        [_ir("a", status="ok"), _ir("b", status="target_unreachable")],
        started_at=_t0(),
        finished_at=_t1(),
    )
    assert r.meta is not None
    assert r.meta.status == ReportStatus.PARTIAL


def test_status_exception_derives_partial() -> None:
    r = Report.from_inspector_results(
        "t",
        [_ir("a", status="ok"), _ir("b", status="exception")],
        started_at=_t0(),
        finished_at=_t1(),
    )
    assert r.meta is not None
    assert r.meta.status == ReportStatus.PARTIAL


def test_status_requires_unmet_derives_partial() -> None:
    r = Report.from_inspector_results(
        "t",
        [_ir("a", status="ok"), _ir("b", status="requires_unmet")],
        started_at=_t0(),
        finished_at=_t1(),
    )
    assert r.meta is not None
    assert r.meta.status == ReportStatus.PARTIAL


def test_status_override_passes_through() -> None:
    r = Report.from_inspector_results(
        "t",
        [_ir("a")],
        started_at=_t0(),
        finished_at=_t1(),
        status=ReportStatus.DEGRADED_TOKEN_BUDGET,
    )
    assert r.meta is not None
    assert r.meta.status == ReportStatus.DEGRADED_TOKEN_BUDGET


def test_empty_inspector_results_raises_value_error() -> None:
    with pytest.raises(ValueError, match="from_inspector_results requires at least one"):
        Report.from_inspector_results("t", [], started_at=_t0(), finished_at=_t1())
