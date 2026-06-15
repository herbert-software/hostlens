"""Syrupy snapshot test for ``render_markdown.render(report)``.

Spec: ``openspec/changes/add-report-data-model/specs/inspect-cli-command/spec.md``
§需求:`hostlens inspect` 集成测试必须覆盖 demo path 第 4 / 5 / 6 / 7 步
("至少 1 个测试用例必须使用 syrupy snapshot 断言 markdown 渲染的字节级输出").

The non-determinism in a `Report` lives in two places:

- ``Report.report_id`` is freshly minted by ``uuid4()`` on every run.
- ``Report.started_at`` / ``Report.finished_at`` track wall-clock time.

To keep the snapshot stable we construct the report with **pinned**
values for both fields rather than relying on a syrupy matcher: this is
the simplest correct path and keeps the snapshot reviewable as plain
markdown. The snapshot file lives at
``tests/cli/__snapshots__/test_inspect_snapshot.ambr`` (default syrupy
location) and is checked into git as part of the M1.6 PR. CI re-runs
``pytest tests/cli/test_inspect_snapshot.py`` without
``--snapshot-update`` and the assertion fails if the bytes drift.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from syrupy.assertion import SnapshotAssertion

from hostlens.inspectors.result import InspectorResult
from hostlens.reporting.models import Evidence, Finding, Report
from hostlens.reporting.render_markdown import render

# Pinned values: stable UUID + frozen UTC timestamps so the snapshot is
# fully deterministic without needing a syrupy matcher hook.
_PINNED_REPORT_ID = UUID("00000000-0000-0000-0000-000000000001")
_PINNED_STARTED_AT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_PINNED_FINISHED_AT = datetime(2026, 1, 1, 12, 0, 1, tzinfo=UTC)


def _build_report() -> Report:
    """Construct a deterministic Report with one inspector + one finding."""

    finding = Finding(
        severity="info",
        message="hello received: hello",
        evidence=[
            Evidence(
                kind="command_output",
                command="echo hello",
                stdout="hello\n",
                exit_code=0,
            ),
        ],
        tags=["demo"],
    )
    inspector_result = InspectorResult(
        name="hello.echo",
        version="1.0.0",
        status="ok",
        target_name="local-host",
        duration_seconds=0.01,
        output={"raw": "hello\n"},
        findings=[finding],
    )
    return Report(
        report_id=_PINNED_REPORT_ID,
        schema_version="1.0",
        intent=None,
        target_name="local-host",
        inspector_results=[inspector_result],
        findings=[finding],
        started_at=_PINNED_STARTED_AT,
        finished_at=_PINNED_FINISHED_AT,
        metadata={},
    )


def test_render_markdown_snapshot(snapshot: SnapshotAssertion, shanghai_tz: None) -> None:
    """Byte-level snapshot of ``render_markdown.render`` output.

    Spec §场景:syrupy snapshot 测试通过. The snapshot pins the entire
    markdown body; any drift (escaping, ordering, header text) fails this
    assertion. The ``shanghai_tz`` fixture pins ``TZ`` so the host-local
    timestamp rendering (UTC storage → local display) is deterministic
    across a UTC CI runner and a local dev box. To update intentionally,
    re-run with ``pytest --snapshot-update`` after spec/design approval.
    """

    rendered = render(_build_report())
    assert rendered == snapshot
