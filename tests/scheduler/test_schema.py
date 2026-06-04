"""Tests for `hostlens.scheduler.schema` — field-level manifest contract.

Spec: ``openspec/changes/add-scheduler/specs/schedule-manifest/spec.md``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hostlens.scheduler.schema import (
    IntervalSpec,
    NotifyConfig,
    ReportConfig,
    ScheduleManifest,
    ScheduleSpec,
)


def _valid_interval_manifest() -> dict[str, object]:
    return {
        "name": "nightly",
        "schedule": {"interval": {"hours": 1}, "timezone": "Asia/Shanghai"},
        "targets": ["web-1"],
        "intent": "check disk and load",
    }


def test_valid_manifest_parses() -> None:
    manifest = ScheduleManifest.model_validate(_valid_interval_manifest())

    assert manifest.name == "nightly"
    assert manifest.targets == ["web-1"]
    assert manifest.intent == "check disk and load"
    assert manifest.notify == []
    # report default is consumed format=md, placeholder diff_with_last=False.
    assert manifest.report.format == "md"
    assert manifest.report.diff_with_last is False
    assert manifest.inspectors is None


def test_valid_cron_manifest_parses() -> None:
    data = _valid_interval_manifest()
    data["schedule"] = {"cron": "0 3 * * *", "timezone": "UTC"}
    manifest = ScheduleManifest.model_validate(data)

    assert manifest.schedule.cron == "0 3 * * *"
    assert manifest.schedule.interval is None


def test_unknown_top_level_field_rejected() -> None:
    data = _valid_interval_manifest()
    data["scheduel"] = {"interval": {"hours": 1}, "timezone": "UTC"}  # typo

    with pytest.raises(ValidationError) as exc:
        ScheduleManifest.model_validate(data)

    assert "scheduel" in str(exc.value)


def test_report_format_rejects_non_md_json() -> None:
    data = _valid_interval_manifest()
    data["report"] = {"format": "markdown"}

    with pytest.raises(ValidationError) as exc:
        ScheduleManifest.model_validate(data)

    assert "format" in str(exc.value)


def test_report_format_md_and_json_accepted() -> None:
    for fmt in ("md", "json"):
        data = _valid_interval_manifest()
        data["report"] = {"format": fmt}
        manifest = ScheduleManifest.model_validate(data)
        assert manifest.report.format == fmt


def test_report_extra_field_rejected() -> None:
    data = _valid_interval_manifest()
    data["report"] = {"format": "md", "bogus": 1}

    with pytest.raises(ValidationError):
        ScheduleManifest.model_validate(data)


def test_diff_with_last_parses_but_is_inert() -> None:
    # diff_with_last is parsed (typed) but never consumed in M4: the schema
    # carries the bool; nothing in this layer triggers an assembly-time diff.
    data = _valid_interval_manifest()
    data["report"] = {"diff_with_last": True}
    manifest = ScheduleManifest.model_validate(data)

    assert manifest.report.diff_with_last is True
    # No baseline_ref / diff-section machinery exists on the manifest — the
    # value is a passive flag with no behavioural effect at this layer.
    assert not hasattr(manifest.report, "baseline_ref")


def test_cron_and_interval_mutually_exclusive() -> None:
    with pytest.raises(ValidationError) as exc:
        ScheduleSpec.model_validate(
            {"cron": "0 3 * * *", "interval": {"hours": 1}, "timezone": "UTC"}
        )

    assert "exactly one" in str(exc.value)


def test_cron_and_interval_neither_provided_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        ScheduleSpec.model_validate({"timezone": "UTC"})

    assert "exactly one" in str(exc.value)


def test_cron_non_five_field_rejected() -> None:
    # 6-field (second-level) cron is rejected — only standard 5-field.
    with pytest.raises(ValidationError) as exc:
        ScheduleSpec.model_validate({"cron": "0 0 3 * * *", "timezone": "UTC"})

    assert "5-field" in str(exc.value)


def test_cron_too_few_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        ScheduleSpec.model_validate({"cron": "0 3 *", "timezone": "UTC"})


def test_invalid_timezone_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        ScheduleSpec.model_validate({"interval": {"hours": 1}, "timezone": "Not/AZone"})

    assert "timezone" in str(exc.value)


def test_interval_all_zero_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        IntervalSpec.model_validate({"weeks": 0, "hours": 0})

    assert "positive" in str(exc.value)


def test_interval_all_omitted_rejected() -> None:
    with pytest.raises(ValidationError):
        IntervalSpec.model_validate({})


def test_interval_single_positive_accepted() -> None:
    spec = IntervalSpec.model_validate({"minutes": 5})
    assert spec.minutes == 5


def test_interval_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        IntervalSpec.model_validate({"hours": 1, "fortnights": 2})


def test_notify_placeholder_parses() -> None:
    # notify is typed (channel + only_if) but inert in M4: parsing must
    # succeed without any send / only_if evaluation.
    data = _valid_interval_manifest()
    data["notify"] = [{"channel": "telegram", "only_if": "severity == 'critical'"}]
    manifest = ScheduleManifest.model_validate(data)

    assert len(manifest.notify) == 1
    assert manifest.notify[0].channel == "telegram"
    assert manifest.notify[0].only_if == "severity == 'critical'"


def test_notify_config_rejects_unknown_field() -> None:
    # M5 tightens NotifyConfig to extra="forbid": a misspelled / unknown
    # sub-field (e.g. only_iff) is fail-loud, matching the manifest's
    # fail-loud basis (schedule-manifest spec §需求:notify 在 M5 被消费).
    with pytest.raises(ValidationError) as exc:
        NotifyConfig.model_validate({"channel": "lark", "only_iff": "x"})

    assert "only_iff" in str(exc.value)


def test_report_config_defaults() -> None:
    cfg = ReportConfig()
    assert cfg.format == "md"
    assert cfg.diff_with_last is False


@pytest.mark.parametrize("bad_name", ["a/b", "a\\b", "../x", " x "])
def test_name_with_path_separator_or_whitespace_rejected(bad_name: str) -> None:
    data = _valid_interval_manifest()
    data["name"] = bad_name

    with pytest.raises(ValidationError) as exc:
        ScheduleManifest.model_validate(data)

    assert "job_id" in str(exc.value)


def test_valid_job_id_name_accepted() -> None:
    data = _valid_interval_manifest()
    data["name"] = "demo-local-health"

    manifest = ScheduleManifest.model_validate(data)

    assert manifest.name == "demo-local-health"
