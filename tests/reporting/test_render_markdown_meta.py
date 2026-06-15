"""Tests for the meta-table block of `render_markdown.render`.

Covers spec §需求:`render_markdown.render` 必须输出固定 GFM 结构且对
控制字符做转义 — `intent` None → EM DASH placeholder + ISO 8601
timestamps + 2-decimal duration.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from hostlens.inspectors.result import InspectorResult
from hostlens.reporting.models import Report
from hostlens.reporting.render_markdown import render


def _make_report(
    *,
    intent: str | None = None,
    started_at: datetime = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC),
    finished_at: datetime = datetime(2026, 5, 26, 12, 0, 1, 250000, tzinfo=UTC),
) -> Report:
    ir = InspectorResult(
        name="hello.echo",
        version="1.0.0",
        status="ok",
        target_name="local-host",
        duration_seconds=0.05,
        output={},
        findings=[],
        error=None,
        missing=[],
    )
    return Report(
        report_id=UUID("12345678-1234-5678-1234-567812345678"),
        schema_version="1.0",
        intent=intent,
        target_name="local-host",
        inspector_results=[ir],
        findings=[],
        started_at=started_at,
        finished_at=finished_at,
        metadata={},
    )


def test_title_present() -> None:
    out = render(_make_report())
    assert "# Hostlens Inspection Report" in out


def test_meta_table_header_and_separator() -> None:
    out = render(_make_report())
    assert "| Field | Value |" in out
    assert "|---|---|" in out


def test_meta_table_contains_all_fields() -> None:
    out = render(_make_report())
    for field in (
        "report_id",
        "schema_version",
        "target_name",
        "intent",
        "started_at",
        "finished_at",
        "duration_seconds",
    ):
        assert f"| {field} |" in out, f"missing meta row: {field}"


def test_intent_none_renders_em_dash() -> None:
    out = render(_make_report(intent=None))
    # Exactly the U+2014 EM DASH codepoint, single character.
    assert "| intent | — |" in out
    assert "| intent | - |" not in out
    assert "| intent | -- |" not in out


def test_intent_string_passes_through() -> None:
    out = render(_make_report(intent="check db latency"))
    assert "| intent | check db latency |" in out


def test_timestamps_render_in_host_local_tz(shanghai_tz: None) -> None:
    # UTC-stored timestamps render in the host's local timezone (shanghai_tz
    # pins TZ so the assertion is deterministic across CI/UTC and a CST box).
    out = render(_make_report())
    # UTC 12:00 → Asia/Shanghai (+08:00) 20:00 — converted, not raw UTC.
    assert "| started_at | 2026-05-26T20:00:00+08:00 |" in out
    assert "| finished_at | 2026-05-26T20:00:01.250000+08:00 |" in out
    assert "T12:00:00" not in out  # the UTC wall clock must NOT leak through


def test_duration_seconds_two_decimal() -> None:
    out = render(_make_report())
    # finished_at - started_at = 1.25 seconds → 1.25
    assert "| duration_seconds | 1.25 |" in out


def test_duration_zero_renders_two_decimal() -> None:
    t = datetime(2026, 5, 26, 12, 0, 0)
    out = render(_make_report(started_at=t, finished_at=t))
    assert "| duration_seconds | 0.00 |" in out


def test_report_id_rendered_as_uuid_string() -> None:
    out = render(_make_report())
    assert "12345678-1234-5678-1234-567812345678" in out
