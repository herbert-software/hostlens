"""Tests for the MCP management ToolSpec batch + assembly (group B).

Covers tasks.md 1.1 / 2.1-2.6 of `add-mcp-readonly-management-tools` plus
the six query tools' double-description contract. The seventh tool
(`run_schedule_now`) is owned by a sibling group and is not exercised here.

Fixtures use real `RunStore` / `ReportStore` over temp SQLite files
(injected `db_path`) and fake `load_manifests` / `load_channel_summaries`
closures — proving handler dependencies come from injection, never globals.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast, get_type_hints

import pytest
import structlog
from pydantic import BaseModel

from hostlens.agent.backend import (
    LLMBackend,
    MessageResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from hostlens.agent.backends.fake import FakeBackend
from hostlens.agent.tools_adapter import scrub_exception_message
from hostlens.core.config import AgentSettings, BackendSettings, Settings
from hostlens.core.exceptions import (
    BackendUnavailable,
    ConfigError,
    ToolError,
    ToolPolicyViolation,
)
from hostlens.inspectors.registry import build_registry_from_search_paths
from hostlens.inspectors.result import InspectorResult
from hostlens.mcp_server.server import build_server
from hostlens.mcp_server.tools_adapter import McpToolsAdapter
from hostlens.notifiers.base import Notifier, NotifyPayload, NotifyResult
from hostlens.reporting.models import Finding, Report, Severity
from hostlens.reporting.store import ReportStore
from hostlens.scheduler.schema import (
    IntervalSpec,
    NotifyConfig,
    ReportConfig,
    ScheduleManifest,
    ScheduleSpec,
)
from hostlens.scheduler.store import Run, RunStatus, RunStore, compute_report_hash
from hostlens.targets.base import ExecutionTarget
from hostlens.targets.config import LocalEntry
from hostlens.targets.local import LocalTarget
from hostlens.targets.registry import TargetRegistry
from hostlens.tools.base import NoopApprovalService, ToolContext, ToolSpec
from hostlens.tools.decorators import tool
from hostlens.tools.management_tools import (
    ManagementToolDeps,
    make_build_runner,
    make_daemon_safe_backend_factory,
    make_load_channel_summaries,
    register_mcp_management_tools,
)
from hostlens.tools.registry import ToolRegistry
from hostlens.tools.schemas.list_channels import ChannelSummary

# Tools owned by group B (the six read-only query tools).
QUERY_TOOLS = frozenset(
    {
        "list_schedules",
        "get_schedule_status",
        "list_channels",
        "list_reports",
        "show_report",
        "diff_reports",
    }
)


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def _ctx() -> ToolContext:
    return ToolContext(
        target_registry=TargetRegistry(),
        inspector_registry=build_registry_from_search_paths([], settings=Settings()).registry,
        config=Settings(),
        logger=structlog.get_logger("test_management_tools"),
        approval_service=NoopApprovalService(),
        cancel=asyncio.Event(),
    )


def _t(minute: int = 0) -> datetime:
    return datetime(2026, 5, 26, 12, minute, 0, tzinfo=UTC)


def _make_report(target_id: str = "host-a", message: str = "disk full") -> Report:
    ir = InspectorResult(
        name="disk.usage",
        version="1.0.0",
        status="ok",
        target_name=target_id,
        duration_seconds=0.1,
        output={},
        findings=[Finding(severity="warning", message=message)],
        error=None,
        missing=[],
    )
    return Report.from_inspector_results(
        target_id,
        [ir],
        started_at=_t(),
        finished_at=_t(1),
        target_id=target_id,
    )


def _manifest_yaml_dict(
    name: str,
    *,
    cron: str = "0 9 * * *",
    target: str = "host-a",
    notify: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "name": name,
        "schedule": {"cron": cron, "timezone": "UTC"},
        "targets": [target],
        "intent": "daily disk check",
    }
    if notify is not None:
        body["notify"] = notify
    return body


def _manifest(
    name: str,
    *,
    notify: list[dict[str, object]] | None = None,
    interval: bool = False,
) -> ScheduleManifest:
    if interval:
        body = {
            "name": name,
            "schedule": {"interval": {"hours": 1}, "timezone": "UTC"},
            "targets": ["host-a"],
            "intent": "hourly check",
        }
        if notify is not None:
            body["notify"] = notify
        return ScheduleManifest.model_validate(body)
    return ScheduleManifest.model_validate(_manifest_yaml_dict(name, notify=notify))


def _deps(
    *,
    run_store: RunStore | None = None,
    report_store: ReportStore | None = None,
    manifests: list[ScheduleManifest] | None = None,
    load_manifests_error: Exception | None = None,
    channels: list[ChannelSummary] | None = None,
    build_runner: object | None = None,
) -> ManagementToolDeps:
    def _load_manifests() -> list[ScheduleManifest]:
        if load_manifests_error is not None:
            raise load_manifests_error
        return list(manifests or [])

    def _load_channels() -> list[ChannelSummary]:
        return list(channels or [])

    def _build_runner(ctx: ToolContext, ms: list[ScheduleManifest]) -> object:  # pragma: no cover
        raise AssertionError("build_runner must not be called by query tools")

    return ManagementToolDeps(
        load_manifests=_load_manifests,
        run_store=run_store if run_store is not None else RunStore(db_path=Path("/nonexistent/x")),
        report_store=report_store
        if report_store is not None
        else ReportStore(db_path=Path("/nonexistent/r")),
        load_channel_summaries=_load_channels,
        build_runner=build_runner if build_runner is not None else _build_runner,  # type: ignore[arg-type]
    )


def _registry(deps: ManagementToolDeps) -> ToolRegistry:
    reg = ToolRegistry()
    register_mcp_management_tools(reg, deps=deps)
    return reg


async def _dispatch(reg: ToolRegistry, name: str, args: dict[str, object]) -> dict[str, object]:
    """Dispatch via the spec handler (escape hatch) and return model_dump."""
    spec = reg.get(name)
    arg_model = spec.input_schema.model_validate(args)
    result = await spec.handler(arg_model, _ctx())
    return result.model_dump()


async def _dispatch_json(reg: ToolRegistry, name: str, args: dict[str, object]) -> str:
    """Dispatch and return the JSON-serialized output (for secret scanning)."""
    spec = reg.get(name)
    arg_model = spec.input_schema.model_validate(args)
    result = await spec.handler(arg_model, _ctx())
    return result.model_dump_json()


# --------------------------------------------------------------------------- #
# 1.1 — assembly skeleton + ToolContext field-set invariant
# --------------------------------------------------------------------------- #


def test_empty_assembly_registers_six_query_tools() -> None:
    reg = _registry(_deps())
    assert reg.names() >= QUERY_TOOLS


def test_tool_context_field_set_is_exactly_six() -> None:
    hints = get_type_hints(ToolContext)
    assert set(hints) == {
        "target_registry",
        "inspector_registry",
        "config",
        "logger",
        "approval_service",
        "cancel",
    }
    # Explicitly assert none of the management deps leaked into ToolContext.
    for forbidden in ("schedule_store", "report_store", "channel_summaries", "run_store"):
        assert forbidden not in hints


# --------------------------------------------------------------------------- #
# 2.1 — list_schedules
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_schedules_projects_fields_and_notify() -> None:
    manifest = _manifest(
        "daily",
        notify=[
            {"channel": "tg", "only_if": "severity >= warning"},
            {"channel": "lark"},
        ],
    )
    reg = _registry(_deps(manifests=[manifest]))
    out = await _dispatch(reg, "list_schedules", {})

    assert len(out["schedules"]) == 1
    sched = out["schedules"][0]
    assert sched["name"] == "daily"
    assert sched["schedule"] == "cron(0 9 * * *)"
    assert sched["targets"] == ["host-a"]
    assert sched["intent"] == "daily disk check"
    assert sched["next_fire_time"] is not None
    assert sched["notify"] == [
        {"channel": "tg", "only_if": "severity >= warning"},
        {"channel": "lark", "only_if": None},
    ]
    # No enabled field anywhere (M4 has no schedule on/off concept).
    assert "enabled" not in sched


@pytest.mark.asyncio
async def test_list_schedules_interval_expr() -> None:
    reg = _registry(_deps(manifests=[_manifest("hourly", interval=True)]))
    out = await _dispatch(reg, "list_schedules", {})
    assert out["schedules"][0]["schedule"] == "interval(1h)"


@pytest.mark.asyncio
async def test_list_schedules_empty_returns_empty_list() -> None:
    reg = _registry(_deps(manifests=[]))
    out = await _dispatch(reg, "list_schedules", {})
    assert out == {"schedules": []}


@pytest.mark.asyncio
async def test_list_schedules_config_error_propagates_from_load() -> None:
    reg = _registry(
        _deps(load_manifests_error=ConfigError("bad manifest", kind="schedule_manifest_invalid"))
    )
    spec = reg.get("list_schedules")
    args = spec.input_schema.model_validate({})
    # fresh-load ConfigError surfaces (the MCP dispatch general-except wraps it
    # into a scrubbed envelope one layer up; the handler does not swallow it).
    with pytest.raises(ConfigError):
        await spec.handler(args, _ctx())


# --------------------------------------------------------------------------- #
# 2.2 — get_schedule_status
# --------------------------------------------------------------------------- #


async def _save_ok_run(store: RunStore, report: Report, *, run_id: str) -> Run:
    run = Run(
        run_id=run_id,
        schedule_name="daily",
        triggered_at=_t(),
        started_at=_t(),
        finished_at=_t(1),
        status=RunStatus.OK,
        report_id=str(report.report_id),
        report_hash=compute_report_hash(report),
        report_storage="db",
        targets=["host-a"],
        inspectors=["disk.usage"],
        notify_results=[
            NotifyResult(
                channel="tg",
                status="failed",
                error="POST https://api.telegram.org/bot123456:SECRETTOKEN/sendMessage failed",
                attempts=3,
            )
        ],
    )
    await store.save(run)
    return run


async def _save_failed_run(store: RunStore, *, run_id: str) -> Run:
    run = Run(
        run_id=run_id,
        schedule_name="daily",
        triggered_at=_t(2),
        status=RunStatus.FAILED_API_UNAVAILABLE,
        targets=["host-a"],
        inspectors=["disk.usage"],
    )
    await store.save(run)
    return run


@pytest.mark.asyncio
async def test_get_schedule_status_two_ids_and_nullable(tmp_path: Path) -> None:
    store = RunStore(db_path=tmp_path / "runs.db")
    report = _make_report()
    await _save_failed_run(store, run_id="ledger-failed")
    await _save_ok_run(store, report, run_id="ledger-ok")

    reg = _registry(_deps(run_store=store))
    out = await _dispatch(reg, "get_schedule_status", {})

    runs = out["runs"]
    assert len(runs) == 2
    by_run_id = {r["run_id"]: r for r in runs}

    ok = by_run_id["ledger-ok"]
    assert ok["run_id"] == "ledger-ok"  # ledger id != report-store key
    assert ok["report_id"] == str(report.report_id)
    assert ok["report_hash"] is not None

    failed = by_run_id["ledger-failed"]
    assert failed["report_id"] is None
    assert failed["report_hash"] is None


@pytest.mark.asyncio
async def test_get_schedule_status_notify_results_redacted(tmp_path: Path) -> None:
    store = RunStore(db_path=tmp_path / "runs.db")
    await _save_ok_run(store, _make_report(), run_id="ledger-ok")

    reg = _registry(_deps(run_store=store))
    serialized = await _dispatch_json(reg, "get_schedule_status", {})
    out = await _dispatch(reg, "get_schedule_status", {})

    assert "SECRETTOKEN" not in serialized
    assert "bot123456:SECRETTOKEN" not in serialized
    # The redacted result still records the channel + failed status.
    nr = out["runs"][0]["notify_results"][0]
    assert nr["channel"] == "tg"
    assert nr["status"] == "failed"


@pytest.mark.asyncio
async def test_get_schedule_status_limit_clamped_to_100(tmp_path: Path) -> None:
    store = RunStore(db_path=tmp_path / "runs.db")
    # Persist 130 failed runs so an unclamped query could return >100.
    for i in range(130):
        run = Run(
            run_id=f"ledger-{i}",
            schedule_name="daily",
            triggered_at=datetime(2026, 5, 26, 12, 0, i % 60, tzinfo=UTC),
            status=RunStatus.FAILED,
            targets=["host-a"],
            inspectors=[],
        )
        await store.save(run)

    reg = _registry(_deps(run_store=store))
    out = await _dispatch(reg, "get_schedule_status", {"limit": 99999})
    assert len(out["runs"]) <= 100


@pytest.mark.asyncio
async def test_get_schedule_status_empty_store_returns_empty(tmp_path: Path) -> None:
    store = RunStore(db_path=tmp_path / "runs.db")
    reg = _registry(_deps(run_store=store))
    out = await _dispatch(reg, "get_schedule_status", {})
    assert out == {"runs": []}


# --------------------------------------------------------------------------- #
# 2.3 — list_channels (positive whitelist)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_channels_name_type_whitelist() -> None:
    channels = [
        ChannelSummary(name="tg", type="telegram"),
        ChannelSummary(name="ops", type="lark"),
    ]
    reg = _registry(_deps(channels=channels))
    out = await _dispatch(reg, "list_channels", {})

    assert out == {
        "channels": [
            {"name": "tg", "type": "telegram"},
            {"name": "ops", "type": "lark"},
        ]
    }
    serialized = json.dumps(out)
    # No raw credential keys, no ${ENV} literals, no expanded values.
    for forbidden in (
        "bot_token",
        "webhook_url",
        "secret",
        "chat_id",
        "${TG_TOKEN}",
        "${HOOK}",
        "${SIGN}",
        "enabled",
        "only_if",
    ):
        assert forbidden not in serialized


def test_channel_summary_forbids_extra_keys() -> None:
    with pytest.raises(ValueError):
        ChannelSummary.model_validate(
            {"name": "tg", "type": "telegram", "bot_token": "${TG_TOKEN}"}
        )


@pytest.mark.asyncio
async def test_list_channels_empty_returns_empty() -> None:
    reg = _registry(_deps(channels=[]))
    out = await _dispatch(reg, "list_channels", {})
    assert out == {"channels": []}


# --------------------------------------------------------------------------- #
# 2.3 — make_load_channel_summaries: the REAL raw reader (security core)
# --------------------------------------------------------------------------- #


def _settings_with_notifiers(path: Path) -> Settings:
    return Settings(notifiers_config_path=path)


def test_real_reader_projects_only_name_and_type(tmp_path: Path) -> None:
    """The real raw reader copies only name/type — never a credential key, an
    expanded value, or a ``${ENV_VAR}`` literal — straight off ``notifiers.yaml``
    without going through ``load_channels`` (which would expand secrets)."""
    notifiers = tmp_path / "notifiers.yaml"
    notifiers.write_text(
        "channels:\n"
        "  tg:\n"
        "    type: telegram\n"
        "    bot_token: ${TG_TOKEN}\n"
        "    chat_id: ${TG_CHAT}\n"
        "  ops:\n"
        "    type: lark\n"
        "    webhook_url: ${HOOK}\n"
        "    secret: ${SIGN}\n",
        encoding="utf-8",
    )
    # ${ENV_VAR}s are deliberately UNSET — load_channels would fail-loud here,
    # but the raw reader never touches them, proving it does not expand.
    reader = make_load_channel_summaries(_settings_with_notifiers(notifiers))
    summaries = reader()

    assert summaries == [
        ChannelSummary(name="tg", type="telegram"),
        ChannelSummary(name="ops", type="lark"),
    ]

    # Every summary carries exactly and only {name, type}.
    for summary in summaries:
        assert set(summary.model_dump().keys()) == {"name", "type"}

    serialized = json.dumps([s.model_dump() for s in summaries])
    for forbidden in (
        "bot_token",
        "webhook_url",
        "secret",
        "chat_id",
        "${TG_TOKEN}",
        "${TG_CHAT}",
        "${HOOK}",
        "${SIGN}",
        "enabled",
        "only_if",
    ):
        assert forbidden not in serialized


def test_real_reader_absent_file_returns_empty(tmp_path: Path) -> None:
    reader = make_load_channel_summaries(_settings_with_notifiers(tmp_path / "missing.yaml"))
    assert reader() == []


def test_real_reader_malformed_entry_raises_config_error(tmp_path: Path) -> None:
    notifiers = tmp_path / "notifiers.yaml"
    # A channel entry missing `type` is fail-loud (matching load_channels).
    notifiers.write_text(
        "channels:\n  tg:\n    bot_token: ${TG_TOKEN}\n",
        encoding="utf-8",
    )
    reader = make_load_channel_summaries(_settings_with_notifiers(notifiers))
    with pytest.raises(ConfigError):
        reader()


def test_real_reader_rejects_env_placeholder_in_channel_name(tmp_path: Path) -> None:
    """A ``${ENV_VAR}`` written as the channel key is fail-loud — and the error
    envelope must not echo the placeholder literal (the leak would otherwise just
    move from the listing to the exception text)."""
    notifiers = tmp_path / "notifiers.yaml"
    notifiers.write_text(
        'channels:\n  "${TG_TOKEN}":\n    type: telegram\n',
        encoding="utf-8",
    )
    reader = make_load_channel_summaries(_settings_with_notifiers(notifiers))
    with pytest.raises(ConfigError) as exc_info:
        reader()
    assert "${TG_TOKEN}" not in str(exc_info.value)


def test_real_reader_rejects_env_placeholder_in_type(tmp_path: Path) -> None:
    """A ``${ENV_VAR}`` written as a channel ``type`` is fail-loud — and the
    error envelope must not echo the placeholder literal."""
    notifiers = tmp_path / "notifiers.yaml"
    notifiers.write_text(
        'channels:\n  tg:\n    type: "${TG_TOKEN}"\n',
        encoding="utf-8",
    )
    reader = make_load_channel_summaries(_settings_with_notifiers(notifiers))
    with pytest.raises(ConfigError) as exc_info:
        reader()
    assert "${TG_TOKEN}" not in str(exc_info.value)


def test_real_reader_rejects_env_placeholder_name_with_non_mapping_entry(tmp_path: Path) -> None:
    """A ``${ENV_VAR}`` channel key whose entry is a non-mapping scalar must hit
    the hoisted name guard, not the `invalid_channel_entry` branch that echoes
    `channel=name_text` — so the placeholder never reaches the error envelope."""
    notifiers = tmp_path / "notifiers.yaml"
    notifiers.write_text(
        'channels:\n  "${TG_TOKEN}": "bad_string"\n',
        encoding="utf-8",
    )
    reader = make_load_channel_summaries(_settings_with_notifiers(notifiers))
    with pytest.raises(ConfigError) as exc_info:
        reader()
    assert "${TG_TOKEN}" not in str(exc_info.value)


def test_real_reader_rejects_env_placeholder_name_with_missing_type(tmp_path: Path) -> None:
    """A ``${ENV_VAR}`` channel key whose entry is an empty mapping (missing
    `type`) must hit the hoisted name guard, not the `missing_channel_type`
    branch that echoes `channel=name_text`."""
    notifiers = tmp_path / "notifiers.yaml"
    notifiers.write_text(
        'channels:\n  "${TG_TOKEN}": {}\n',
        encoding="utf-8",
    )
    reader = make_load_channel_summaries(_settings_with_notifiers(notifiers))
    with pytest.raises(ConfigError) as exc_info:
        reader()
    assert "${TG_TOKEN}" not in str(exc_info.value)


# --------------------------------------------------------------------------- #
# 2.4 — list_reports (report_id naming + injected store proof)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_reports_exposes_report_id_not_run_id(tmp_path: Path) -> None:
    store = ReportStore(db_path=tmp_path / "reports.db")
    report = _make_report(target_id="host-a")
    await store.save(report)

    reg = _registry(_deps(report_store=store))
    out = await _dispatch(reg, "list_reports", {"target": "host-a"})

    assert len(out["reports"]) == 1
    row = out["reports"][0]
    assert "report_id" in row
    assert "run_id" not in row
    assert row["report_id"] == str(report.report_id)
    # The report_id is a valid show_report key (report.report_id is a UUID;
    # model_dump keeps Report.report_id as a UUID object).
    show = await _dispatch(reg, "show_report", {"report_id": row["report_id"]})
    assert show["report"]["report_id"] == report.report_id


@pytest.mark.asyncio
async def test_list_reports_empty_store_returns_empty(tmp_path: Path) -> None:
    store = ReportStore(db_path=tmp_path / "reports.db")
    reg = _registry(_deps(report_store=store))
    out = await _dispatch(reg, "list_reports", {"target": "host-a"})
    assert out == {"reports": []}


def test_list_reports_requires_target() -> None:
    reg = _registry(_deps())
    spec = reg.get("list_reports")
    with pytest.raises(ValueError):
        spec.input_schema.model_validate({})


# --------------------------------------------------------------------------- #
# 2.5 — show_report
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_show_report_returns_report(tmp_path: Path) -> None:
    store = ReportStore(db_path=tmp_path / "reports.db")
    report = _make_report()
    await store.save(report)

    reg = _registry(_deps(report_store=store))
    out = await _dispatch(reg, "show_report", {"report_id": str(report.report_id)})
    assert out["report"]["report_id"] == report.report_id
    assert len(out["report"]["findings"]) == 1


@pytest.mark.asyncio
async def test_show_report_not_found_is_tool_error(tmp_path: Path) -> None:
    store = ReportStore(db_path=tmp_path / "reports.db")
    reg = _registry(_deps(report_store=store))
    spec = reg.get("show_report")
    args = spec.input_schema.model_validate({"report_id": "does-not-exist"})
    with pytest.raises(ToolError) as exc_info:
        await spec.handler(args, _ctx())
    # Not-found message must not contain an internal file path.
    assert "/" not in str(exc_info.value).replace("show_report", "")
    assert str(tmp_path) not in str(exc_info.value)


# --------------------------------------------------------------------------- #
# 2.6 — diff_reports
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_diff_reports_same_target_produces_diff(tmp_path: Path) -> None:
    store = ReportStore(db_path=tmp_path / "reports.db")
    baseline = _make_report(target_id="host-a", message="disk ok")
    current = _make_report(target_id="host-a", message="disk full")
    await store.save(baseline)
    await store.save(current)

    reg = _registry(_deps(report_store=store))
    out = await _dispatch(
        reg,
        "diff_reports",
        {"report_id_a": str(baseline.report_id), "report_id_b": str(current.report_id)},
    )
    assert "diff" in out
    # Different finding messages => added/resolved present.
    diff = out["diff"]
    assert len(diff["added"]) + len(diff["resolved"]) >= 1


@pytest.mark.asyncio
async def test_diff_reports_cross_target_is_tool_error(tmp_path: Path) -> None:
    store = ReportStore(db_path=tmp_path / "reports.db")
    a = _make_report(target_id="host-a")
    b = _make_report(target_id="host-b")
    await store.save(a)
    await store.save(b)

    reg = _registry(_deps(report_store=store))
    spec = reg.get("diff_reports")
    args = spec.input_schema.model_validate(
        {"report_id_a": str(a.report_id), "report_id_b": str(b.report_id)}
    )
    with pytest.raises(ToolError):
        await spec.handler(args, _ctx())


@pytest.mark.asyncio
async def test_diff_reports_missing_id_is_tool_error_before_compute(tmp_path: Path) -> None:
    store = ReportStore(db_path=tmp_path / "reports.db")
    a = _make_report(target_id="host-a")
    await store.save(a)

    reg = _registry(_deps(report_store=store))
    spec = reg.get("diff_reports")
    args = spec.input_schema.model_validate(
        {"report_id_a": str(a.report_id), "report_id_b": "missing"}
    )
    with pytest.raises(ToolError):
        await spec.handler(args, _ctx())


# --------------------------------------------------------------------------- #
# Double-description contract (group B's share of tasks 4.1 / 4.2)
# --------------------------------------------------------------------------- #


def test_six_query_tools_descriptions_distinct_and_nonempty() -> None:
    reg = _registry(_deps())
    for name in QUERY_TOOLS:
        spec = reg.get(name)
        assert spec.agent_description.strip() != ""
        assert spec.mcp_description.strip() != ""
        assert spec.agent_description != spec.mcp_description


def test_six_query_tools_surfaces_and_policy_metadata() -> None:
    reg = _registry(_deps())
    for name in QUERY_TOOLS:
        spec = reg.get(name)
        assert spec.surfaces == {"agent", "mcp"}
        assert spec.requires_approval is False
        assert spec.sensitive_output is True
        assert spec.side_effects in {"none", "read"}


# =========================================================================== #
# Group C — run_schedule_now + build_runner + double-description (all 7 tools)
# =========================================================================== #

ALL_SEVEN_TOOLS = QUERY_TOOLS | {"run_schedule_now"}

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="LocalTarget requires POSIX")

# --------------------------------------------------------------------------- #
# Real-runner wiring helpers (drive run_schedule_now through a FakeBackend).
# --------------------------------------------------------------------------- #

_RUN_TARGET = "local-host"
_RUN_INSPECTOR_INPUT = {"target_name": _RUN_TARGET, "inspector_name": "hello.echo"}


def _msg(*, content: list[Any], stop_reason: str) -> MessageResponse:
    return MessageResponse(
        id="msg_x",
        model="claude-test",
        role="assistant",
        content=content,
        stop_reason=cast(Any, stop_reason),
        usage=Usage(input_tokens=3, output_tokens=2),
    )


def _end_turn(text: str) -> MessageResponse:
    return _msg(content=[TextBlock(type="text", text=text)], stop_reason="end_turn")


def _planner_run_inspector() -> MessageResponse:
    return _msg(
        content=[
            ToolUseBlock(
                type="tool_use", id="tu_plan", name="run_inspector", input=_RUN_INSPECTOR_INPUT
            )
        ],
        stop_reason="tool_use",
    )


def _happy_script() -> list[MessageResponse]:
    return [_planner_run_inspector(), _end_turn("巡检完成。"), _end_turn("诊断完成。")]


class _SpyChannel:
    """Notifier whose ``send`` / ``render`` count calls (proves suppression)."""

    name = "telegram"

    def __init__(self) -> None:
        self.send_calls = 0
        self.render_calls = 0

    def validate_config(self, cfg: dict[str, object]) -> None:  # pragma: no cover - unused
        del cfg

    def render(self, report: Report, *, severity: Severity) -> NotifyPayload:  # pragma: no cover
        del report, severity
        self.render_calls += 1
        return NotifyPayload(channel="tg", channel_type="telegram", body="x")

    async def send(self, payload: NotifyPayload) -> NotifyResult:  # pragma: no cover
        del payload
        self.send_calls += 1
        return NotifyResult(channel="tg", status="sent")


class _UnavailableBackend:
    """Backend that always raises ``BackendUnavailable`` (drives failed_api_unavailable)."""

    name = "unavailable"

    def __init__(self) -> None:
        self.capabilities = FakeBackend(responses=[]).capabilities

    async def messages_create(self, **_kwargs: Any) -> MessageResponse:  # pragma: no cover
        raise BackendUnavailable("simulated outage", backend_name="unavailable")


def _run_settings() -> Settings:
    return Settings(agent=AgentSettings())


def _run_target_registry() -> TargetRegistry:
    registry = TargetRegistry()
    entry = LocalEntry(name=_RUN_TARGET, type="local", enabled=True)
    target = cast(ExecutionTarget, LocalTarget(name=_RUN_TARGET))
    registry.register(target, entry)
    return registry


def _run_ctx() -> ToolContext:
    return ToolContext(
        target_registry=_run_target_registry(),
        inspector_registry=build_registry_from_search_paths([], settings=Settings()).registry,
        config=_run_settings(),
        logger=structlog.get_logger("test_run_schedule_now"),
        approval_service=NoopApprovalService(),
        cancel=asyncio.Event(),
    )


def _run_manifest(
    name: str = "nightly", *, notify: list[dict[str, object]] | None = None
) -> ScheduleManifest:
    return ScheduleManifest(
        name=name,
        schedule=ScheduleSpec(interval=IntervalSpec(minutes=10), timezone="UTC"),
        targets=[_RUN_TARGET],
        intent="检查健康",
        report=ReportConfig(),
        notify=[NotifyConfig(**n) for n in (notify or [])],
    )


def _make_run_deps(
    *,
    backend: LLMBackend,
    tmp_path: Path,
    manifests: list[ScheduleManifest],
    channels: dict[str, Notifier],
) -> ManagementToolDeps:
    run_store = RunStore(db_path=tmp_path / "runs.db")
    report_store = ReportStore(db_path=tmp_path / "reports.db", orphan_dir=tmp_path / "orphans")
    settings = _run_settings()

    build_runner = make_build_runner(
        settings=settings,
        run_store=run_store,
        report_store=report_store,
        channels=channels,
        backend_factory=lambda: backend,
        logger=structlog.get_logger("test_build_runner"),
    )
    return _deps(
        run_store=run_store,
        report_store=report_store,
        manifests=manifests,
        build_runner=build_runner,
    )


# --------------------------------------------------------------------------- #
# 3.2 — run_schedule_now happy path: pipeline runs, notify suppressed
# --------------------------------------------------------------------------- #


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_run_schedule_now_persists_report_suppresses_notify(tmp_path: Path) -> None:
    spy = _SpyChannel()
    manifest = _run_manifest("nightly", notify=[{"channel": "tg"}])
    deps = _make_run_deps(
        backend=cast(LLMBackend, FakeBackend(responses=_happy_script())),
        tmp_path=tmp_path,
        manifests=[manifest],
        channels={"tg": cast(Notifier, spy)},
    )
    reg = _registry(deps)
    spec = reg.get("run_schedule_now")
    args = spec.input_schema.model_validate({"name": "nightly"})
    out = (await spec.handler(args, _run_ctx())).model_dump()

    assert out["status"] in {"ok", "partial"}
    assert out["run_id"]
    assert out["report_id"] is not None
    # Notify dispatch was fully suppressed: render/send never called.
    assert spy.send_calls == 0
    assert spy.render_calls == 0
    # The ledger Run carries an empty notify_results.
    runs = await deps.run_store.list_recent(limit=10)
    assert len(runs) == 1
    assert runs[0].notify_results == []


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_run_schedule_now_report_id_feeds_show_report(tmp_path: Path) -> None:
    manifest = _run_manifest("nightly")
    deps = _make_run_deps(
        backend=cast(LLMBackend, FakeBackend(responses=_happy_script())),
        tmp_path=tmp_path,
        manifests=[manifest],
        channels={},
    )
    reg = _registry(deps)
    spec = reg.get("run_schedule_now")
    args = spec.input_schema.model_validate({"name": "nightly"})
    out = (await spec.handler(args, _run_ctx())).model_dump()

    report_id = out["report_id"]
    assert report_id is not None
    # report_id is a valid get_run key (ledger run_id is not).
    show = await _dispatch(reg, "show_report", {"report_id": report_id})
    assert show["report"] is not None
    # The ledger run_id must NOT resolve via show_report.
    show_spec = reg.get("show_report")
    bad_args = show_spec.input_schema.model_validate({"report_id": out["run_id"]})
    with pytest.raises(ToolError):
        await show_spec.handler(bad_args, _run_ctx())


@pytest.mark.asyncio
async def test_run_schedule_now_unknown_name_not_found_no_pipeline() -> None:
    pipeline_built = {"called": False}

    def _build_runner(
        ctx: ToolContext, ms: list[ScheduleManifest]
    ) -> object:  # pragma: no cover - must not run
        pipeline_built["called"] = True
        raise AssertionError("build_runner must not run for an unknown name")

    deps = _deps(manifests=[_run_manifest("nightly")], build_runner=_build_runner)
    reg = _registry(deps)
    spec = reg.get("run_schedule_now")
    args = spec.input_schema.model_validate({"name": "does-not-exist"})

    with pytest.raises(ToolError) as exc_info:
        await spec.handler(args, _run_ctx())

    assert "schedule_not_found" in str(exc_info.value)
    assert pipeline_built["called"] is False
    # Through the MCP adapter the ToolError becomes a structured envelope, not a
    # bare KeyError pass-through.
    adapter = McpToolsAdapter(reg, _run_ctx)
    envelope = await adapter.dispatch("run_schedule_now", {"name": "does-not-exist"})
    assert envelope["is_error"] is True
    assert envelope["error_kind"] == "ToolError"


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_run_schedule_now_backend_outage_maps_to_failed_api_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Zero the loop's unavailable backoff so retries do not sleep.
    monkeypatch.setattr("hostlens.agent.loop._UNAVAILABLE_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    manifest = _run_manifest("nightly")
    deps = _make_run_deps(
        backend=cast(LLMBackend, _UnavailableBackend()),
        tmp_path=tmp_path,
        manifests=[manifest],
        channels={},
    )
    reg = _registry(deps)
    spec = reg.get("run_schedule_now")
    args = spec.input_schema.model_validate({"name": "nightly"})
    out = (await spec.handler(args, _run_ctx())).model_dump()

    assert out["status"] == "failed_api_unavailable"
    assert out["report_id"] is None
    # No Report was persisted.
    assert await deps.report_store.list_runs(_RUN_TARGET, limit=10) == []


# --------------------------------------------------------------------------- #
# 3.1 — build_runner crosscheck (assembly-data equivalence) + same-source gate
# --------------------------------------------------------------------------- #


def test_build_runner_assembly_data_equivalent_to_cli(tmp_path: Path) -> None:
    """The MCP runner factory wires the same store / channels / registry the CLI
    `_build_runner` does (assembly-data equivalence), and does NOT raise
    typer.Exit on a ConfigError — it lets it propagate."""
    run_store = RunStore(db_path=tmp_path / "runs.db")
    report_store = ReportStore(db_path=tmp_path / "reports.db")
    settings = _run_settings()
    channels: dict[str, Notifier] = {"tg": cast(Notifier, _SpyChannel())}

    def backend_factory() -> LLMBackend:
        return cast(LLMBackend, FakeBackend(responses=[]))

    build_runner = make_build_runner(
        settings=settings,
        run_store=run_store,
        report_store=report_store,
        channels=channels,
        backend_factory=backend_factory,
        logger=structlog.get_logger("t"),
    )
    manifest = _run_manifest("nightly", notify=[{"channel": "tg"}])
    runner = build_runner(_run_ctx(), [manifest])

    # Same injected stores / channels / grace as the CLI path.
    assert runner._run_store is run_store
    assert runner._report_store is report_store
    assert runner._channels is channels
    assert runner._backend_factory is backend_factory
    assert runner._grace_seconds == settings.daemon.shutdown_grace_seconds

    # A manifest referencing an unknown channel raises ConfigError (NOT typer.Exit).
    bad_manifest = _run_manifest("bad", notify=[{"channel": "ghost"}])
    with pytest.raises(ConfigError):
        build_runner(_run_ctx(), [bad_manifest])


def test_daemon_safe_backend_factory_binds_daemon_mode_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spy `create_backend`: the eager-probe construction and every per-fire
    construction must pass the SAME `daemon_mode=True` settings object (the
    same-source invariant). Since no currently-constructable backend can
    *observably* trip the daemon gate (AnthropicAPIBackend's gate is a no-op,
    placeholders raise NotImplementedError before it), the invariant is fixed
    with a spy, not by "construction did not crash"."""
    seen: list[Settings] = []

    def _spy_create_backend(settings: Settings) -> LLMBackend:
        seen.append(settings)
        return cast(LLMBackend, FakeBackend(responses=[]))

    monkeypatch.setattr("hostlens.tools.management_tools.create_backend", _spy_create_backend)

    base = Settings(backend=BackendSettings(type="fake"))
    factory = make_daemon_safe_backend_factory(base)

    # Serve's eager probe + a per-fire construction both go through this factory.
    factory()  # eager probe (serve boot)
    factory()  # per-fire (build_runner)

    assert len(seen) == 2
    # Same object both times (single daemon_mode=True model_copy, closure-bound).
    assert seen[0] is seen[1]
    assert seen[0].daemon_mode is True
    # The original settings were not mutated.
    assert base.daemon_mode is False


# --------------------------------------------------------------------------- #
# 4.1 / 4.2 — all seven tools: distinct descriptions + dual-surface projection
# --------------------------------------------------------------------------- #


def test_seven_tools_descriptions_distinct_and_nonempty() -> None:
    reg = _registry(_deps())
    assert reg.names() >= ALL_SEVEN_TOOLS
    for name in ALL_SEVEN_TOOLS:
        spec = reg.get(name)
        assert spec.agent_description.strip() != ""
        assert spec.mcp_description.strip() != ""
        assert spec.agent_description != spec.mcp_description


def test_seven_tools_projected_to_both_surfaces() -> None:
    reg = _registry(_deps())
    mcp_names = {s.name for s in reg.list_for("mcp")}
    agent_names = {s.name for s in reg.list_for("agent")}
    assert mcp_names >= ALL_SEVEN_TOOLS
    assert agent_names >= ALL_SEVEN_TOOLS


def test_run_schedule_now_policy_metadata() -> None:
    reg = _registry(_deps())
    spec = reg.get("run_schedule_now")
    assert spec.surfaces == {"agent", "mcp"}
    assert spec.requires_approval is False
    assert spec.sensitive_output is True
    assert spec.side_effects == "read"
    assert spec.timeout is not None
    assert spec.timeout >= 120.0


# =========================================================================== #
# Group A — fail-closed self-check, crosscheck regression gate, dispatch
# integration, and the Demo Path offline walkthrough.
# =========================================================================== #


# --------------------------------------------------------------------------- #
# 6.1 — fail-closed self-check: a management-style tool that forgets to declare
# `sensitive_output` must trip the eager `list_for_mcp` probe inside
# `build_server`, *before* the server reaches a running state.
# --------------------------------------------------------------------------- #


class _UndeclaredInput(BaseModel):
    pass


class _UndeclaredOutput(BaseModel):
    pass


def _undeclared_management_spec() -> ToolSpec:
    """A management-style spec that forgets to declare `sensitive_output`.

    Built via the public `@tool` factory (not by poking a private structure)
    so it exactly models the mistake a future management-tool author would
    make: opt into the mcp surface but leave `sensitive_output` at its `None`
    default. `tool()` does not default `sensitive_output`, so omitting it
    leaves the ToolSpec default of `None` — exactly the undeclared state the
    fail-closed gate must reject.
    """

    async def _handler(args: _UndeclaredInput, ctx: ToolContext) -> _UndeclaredOutput:
        del args, ctx
        return _UndeclaredOutput()

    return tool(
        name="forgot_sensitive_output",
        version="1.0.0",
        input_schema=_UndeclaredInput,
        output_schema=_UndeclaredOutput,
        agent_description="management-style probe (agent surface)",
        mcp_description="management-style probe (mcp surface)",
        cli_help=None,
        surfaces={"agent", "mcp"},
        side_effects="none",
    )(cast(Any, _handler))


def test_build_server_fail_closed_on_undeclared_management_tool() -> None:
    spec = _undeclared_management_spec()
    assert spec.sensitive_output is None  # the mistake we are guarding against

    reg = ToolRegistry()
    reg.register(spec)

    with pytest.raises(ToolPolicyViolation) as exc_info:
        build_server(reg, _ctx)

    err = exc_info.value
    assert err.tool_name == "forgot_sensitive_output"
    assert err.surface == "mcp"
    assert err.violated_field == "sensitive_output"


# --------------------------------------------------------------------------- #
# 6.2 — crosscheck regression gate: every one of the seven management tools, as
# assembled by `register_mcp_management_tools`, must declare the four policy
# fields correctly. A single loop over ALL_SEVEN_TOOLS so a future tool added
# to `register_mcp_management_tools` cannot silently skip the gate (a grep
# blind spot the pytest catches).
# --------------------------------------------------------------------------- #


def test_all_seven_tools_policy_metadata_crosscheck() -> None:
    reg = _registry(_deps())
    # The assembly registers exactly the seven management tools; assert the gate
    # covers each of them.
    assert reg.names() >= ALL_SEVEN_TOOLS
    for name in ALL_SEVEN_TOOLS:
        spec = reg.get(name)
        assert spec.sensitive_output is not None, name
        assert spec.surfaces == {"agent", "mcp"}, name
        assert spec.requires_approval is False, name
        assert spec.side_effects in {"none", "read"}, name


# --------------------------------------------------------------------------- #
# 6.3 — dispatch projection integration: every tool driven end-to-end through
# the REAL `McpToolsAdapter.dispatch` (not the spec.handler escape hatch), plus
# the not-found error-envelope paths scrubbed by `scrub_exception_message`.
# --------------------------------------------------------------------------- #


def _query_adapter(deps: ManagementToolDeps) -> McpToolsAdapter:
    """Adapter over a management registry, query tools use the plain `_ctx`."""
    return McpToolsAdapter(_registry(deps), _ctx)


@pytest.mark.asyncio
async def test_dispatch_list_schedules_happy() -> None:
    adapter = _query_adapter(_deps(manifests=[_manifest("daily", notify=[{"channel": "tg"}])]))
    out = await adapter.dispatch("list_schedules", {})
    assert "is_error" not in out
    assert out["schedules"][0]["name"] == "daily"


@pytest.mark.asyncio
async def test_dispatch_get_schedule_status_happy(tmp_path: Path) -> None:
    store = RunStore(db_path=tmp_path / "runs.db")
    await _save_ok_run(store, _make_report(), run_id="ledger-ok")
    adapter = _query_adapter(_deps(run_store=store))
    out = await adapter.dispatch("get_schedule_status", {})
    assert "is_error" not in out
    assert out["runs"][0]["run_id"] == "ledger-ok"


@pytest.mark.asyncio
async def test_dispatch_list_channels_happy() -> None:
    adapter = _query_adapter(_deps(channels=[ChannelSummary(name="tg", type="telegram")]))
    out = await adapter.dispatch("list_channels", {})
    assert "is_error" not in out
    assert out["channels"] == [{"name": "tg", "type": "telegram"}]


@pytest.mark.asyncio
async def test_dispatch_list_reports_happy(tmp_path: Path) -> None:
    store = ReportStore(db_path=tmp_path / "reports.db")
    report = _make_report(target_id="host-a")
    await store.save(report)
    adapter = _query_adapter(_deps(report_store=store))
    out = await adapter.dispatch("list_reports", {"target": "host-a"})
    assert "is_error" not in out
    assert out["reports"][0]["report_id"] == str(report.report_id)


@pytest.mark.asyncio
async def test_dispatch_show_report_happy(tmp_path: Path) -> None:
    store = ReportStore(db_path=tmp_path / "reports.db")
    report = _make_report()
    await store.save(report)
    adapter = _query_adapter(_deps(report_store=store))
    out = await adapter.dispatch("show_report", {"report_id": str(report.report_id)})
    assert "is_error" not in out
    assert str(out["report"]["report_id"]) == str(report.report_id)


@pytest.mark.asyncio
async def test_dispatch_diff_reports_happy(tmp_path: Path) -> None:
    store = ReportStore(db_path=tmp_path / "reports.db")
    baseline = _make_report(target_id="host-a", message="disk ok")
    current = _make_report(target_id="host-a", message="disk full")
    await store.save(baseline)
    await store.save(current)
    adapter = _query_adapter(_deps(report_store=store))
    out = await adapter.dispatch(
        "diff_reports",
        {"report_id_a": str(baseline.report_id), "report_id_b": str(current.report_id)},
    )
    assert "is_error" not in out
    assert "diff" in out


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_dispatch_run_schedule_now_happy(tmp_path: Path) -> None:
    manifest = _run_manifest("nightly")
    deps = _make_run_deps(
        backend=cast(LLMBackend, FakeBackend(responses=_happy_script())),
        tmp_path=tmp_path,
        manifests=[manifest],
        channels={},
    )
    adapter = McpToolsAdapter(_registry(deps), _run_ctx)
    out = await adapter.dispatch("run_schedule_now", {"name": "nightly"})
    assert "is_error" not in out
    assert out["status"] in {"ok", "partial"}
    assert out["report_id"] is not None


@pytest.mark.asyncio
async def test_dispatch_show_report_not_found_envelope_scrubbed(tmp_path: Path) -> None:
    store = ReportStore(db_path=tmp_path / "reports.db")
    adapter = _query_adapter(_deps(report_store=store))
    out = await adapter.dispatch("show_report", {"report_id": "does-not-exist"})

    assert out["is_error"] is True
    assert out["error_kind"] == "ToolError"
    assert out["tool_name"] == "show_report"
    # Message is scrubbed and never carries an internal filesystem path.
    assert out["message"] == scrub_exception_message(out["message"])
    assert str(tmp_path) not in out["message"]
    assert str(tmp_path) not in out["cause"]


@pytest.mark.asyncio
async def test_dispatch_diff_reports_not_found_envelope_scrubbed(tmp_path: Path) -> None:
    store = ReportStore(db_path=tmp_path / "reports.db")
    a = _make_report(target_id="host-a")
    await store.save(a)
    adapter = _query_adapter(_deps(report_store=store))
    out = await adapter.dispatch(
        "diff_reports",
        {"report_id_a": str(a.report_id), "report_id_b": "missing"},
    )

    assert out["is_error"] is True
    assert out["error_kind"] == "ToolError"
    assert out["message"] == scrub_exception_message(out["message"])
    assert str(tmp_path) not in out["message"]


@pytest.mark.asyncio
async def test_dispatch_run_schedule_now_unknown_name_envelope_scrubbed() -> None:
    deps = _deps(manifests=[_run_manifest("nightly")])
    adapter = McpToolsAdapter(_registry(deps), _run_ctx)
    out = await adapter.dispatch("run_schedule_now", {"name": "ghost"})

    assert out["is_error"] is True
    # A plain ToolError (not ToolPolicyViolation / KeyError) is wrapped, never
    # passed through bare.
    assert out["error_kind"] == "ToolError"
    assert "schedule_not_found" in out["message"]
    assert out["message"] == scrub_exception_message(out["message"])


# --------------------------------------------------------------------------- #
# 7.2 — Demo Path offline walkthrough (no SSH, no paid API). Each leg of the
# proposal's Demo Path is exercised here against the FakeBackend / temp stores:
# serve assembles 10 tools (asserted by
# tests/mcp_server/test_serve_assembly.py::test_serve_assembles_ten_tools); the
# six query tools dispatch offline; and run_schedule_now replays through the
# FakeBackend `_happy_script` (the offline "no paid API" stand-in — the report
# pipeline never reaches a real Anthropic endpoint).
# --------------------------------------------------------------------------- #


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_demo_path_offline_end_to_end(tmp_path: Path) -> None:
    """Walk the full Demo Path through one assembled management registry.

    1. The six query tools each dispatch offline (empty stores -> structured
       empty results, never a crash).
    2. `run_schedule_now` replays the FakeBackend `_happy_script`, persisting a
       Report with notify suppressed.
    3. The returned `report_id` round-trips back through `show_report` /
       `list_reports` / `diff_reports` — proving the LLM-produced report is
       readable by the read-only query surface, all offline.
    """
    run_store = RunStore(db_path=tmp_path / "runs.db")
    report_store = ReportStore(db_path=tmp_path / "reports.db", orphan_dir=tmp_path / "orphans")
    settings = _run_settings()
    manifest = _run_manifest("nightly")
    build_runner = make_build_runner(
        settings=settings,
        run_store=run_store,
        report_store=report_store,
        channels={},
        backend_factory=lambda: cast(LLMBackend, FakeBackend(responses=_happy_script())),
        logger=structlog.get_logger("demo_path"),
    )
    deps = _deps(
        run_store=run_store,
        report_store=report_store,
        manifests=[manifest],
        channels=[ChannelSummary(name="tg", type="telegram")],
        build_runner=build_runner,
    )
    adapter = McpToolsAdapter(_registry(deps), _run_ctx)

    # 1. Six query tools dispatch offline (no SSH, empty report/run stores).
    assert (await adapter.dispatch("list_schedules", {}))["schedules"][0]["name"] == "nightly"
    assert (await adapter.dispatch("get_schedule_status", {}))["runs"] == []
    assert (await adapter.dispatch("list_channels", {}))["channels"] == [
        {"name": "tg", "type": "telegram"}
    ]
    assert (await adapter.dispatch("list_reports", {"target": _RUN_TARGET}))["reports"] == []

    # 2. run_schedule_now replays the FakeBackend script offline (no paid API).
    run_out = await adapter.dispatch("run_schedule_now", {"name": "nightly"})
    assert "is_error" not in run_out
    assert run_out["status"] in {"ok", "partial"}
    report_id = run_out["report_id"]
    assert report_id is not None

    # 3. The produced report_id round-trips through the read-only query surface.
    show = await adapter.dispatch("show_report", {"report_id": report_id})
    assert str(show["report"]["report_id"]) == report_id
    listed = await adapter.dispatch("list_reports", {"target": _RUN_TARGET})
    assert listed["reports"][0]["report_id"] == report_id
    status_after = await adapter.dispatch("get_schedule_status", {})
    assert status_after["runs"][0]["report_id"] == report_id
    assert status_after["runs"][0]["notify_results"] == []
