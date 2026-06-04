"""Tests for ``hostlens schedule`` CLI + daemon graceful shutdown (task 5.6).

Spec: ``openspec/changes/add-scheduler/specs/schedule-cli-command/spec.md``
(design D-5 / D-9 / D-12).

The CLI subcommands are driven through Typer's ``CliRunner``; the
SIGTERM/daemon_stopped graceful-shutdown invariants are driven directly
against ``SchedulerRunner.graceful_stop`` (deterministic, no real timers /
real signals) per design D-5's "可测性" note.

``XDG_DATA_HOME`` is redirected to a tmp dir in every test so ``RunStore`` /
``ReportStore`` default paths (runs.db / reports.db) never touch the operator's
home; ``monkeypatch.chdir`` points the cwd-relative ``schedules/`` scan at a
tmp dir.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import TYPE_CHECKING, Any, cast

import pytest
import structlog
import typer
import yaml
from typer.testing import CliRunner

from hostlens.agent.backend import (
    LLMBackend,
    MessageResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from hostlens.agent.backends.fake import FakeBackend
from hostlens.cli import app
from hostlens.core.config import AgentSettings, Settings
from hostlens.inspectors.registry import build_registry_from_search_paths
from hostlens.reporting.store import ReportStore
from hostlens.scheduler.runner import SchedulerRunner
from hostlens.scheduler.schema import IntervalSpec, ReportConfig, ScheduleManifest, ScheduleSpec
from hostlens.scheduler.store import RunStatus, RunStore
from hostlens.targets.config import LocalEntry
from hostlens.targets.registry import TargetRegistry
from hostlens.tools.base import NoopApprovalService, ToolContext

if TYPE_CHECKING:
    from pathlib import Path

    from hostlens.targets.base import ExecutionTarget

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="LocalTarget requires POSIX (Linux/macOS)",
)

_TARGET = "local-host"
_RUN_INSPECTOR_INPUT = {"target_name": _TARGET, "inspector_name": "hello.echo"}


# --------------------------------------------------------------------------- #
# Fixtures + helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate cwd (schedules/) + XDG data root (runs.db / reports.db)."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    (tmp_path / "schedules").mkdir()
    targets = tmp_path / "targets.yaml"
    targets.write_text(
        yaml.safe_dump({"version": "1", "targets": [{"name": _TARGET, "type": "local"}]})
    )
    monkeypatch.setenv("HOSTLENS_TARGETS_CONFIG_PATH", str(targets))
    # No inspector search paths so the registry stays minimal/offline.
    monkeypatch.setenv("HOSTLENS_INSPECTORS_SEARCH_PATHS", "")
    # The pipeline needs an ``agent`` block to construct the AgentLoop; provide
    # a minimal one (the backend itself is monkeypatched, so model values are
    # irrelevant — only the block's presence matters).
    monkeypatch.setenv("HOSTLENS_AGENT__PRIMARY_MODEL", "claude-test")
    return tmp_path


def _write_manifest(root: Path, *, name: str, target: str = _TARGET) -> None:
    (root / "schedules" / f"{name}.yaml").write_text(
        yaml.safe_dump(
            {
                "name": name,
                "schedule": {"interval": {"minutes": 10}, "timezone": "UTC"},
                "targets": [target],
                "intent": "检查健康",
            }
        )
    )


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


def _patch_backend(monkeypatch: pytest.MonkeyPatch, factory: Any) -> None:
    """Make the schedule CLI's ``create_backend`` return a scripted backend."""

    monkeypatch.setattr("hostlens.cli.schedule.create_backend", lambda settings: factory())


# --- direct-runner helpers (graceful-stop tests) --------------------------- #


def _make_target_registry() -> TargetRegistry:
    from hostlens.targets.local import LocalTarget

    registry = TargetRegistry()
    entry = LocalEntry(name=_TARGET, type="local", enabled=True)
    target: ExecutionTarget = cast("ExecutionTarget", LocalTarget(name=_TARGET))
    registry.register(target, entry)
    return registry


def _context_factory(target_registry: TargetRegistry) -> Any:
    inspector_registry = build_registry_from_search_paths([], settings=Settings()).registry

    def _make() -> ToolContext:
        return ToolContext(
            target_registry=target_registry,
            inspector_registry=inspector_registry,
            config=Settings(),
            logger=structlog.get_logger("test_schedule"),
            approval_service=NoopApprovalService(),
            cancel=asyncio.Event(),
        )

    return _make


def _manifest(name: str = "nightly") -> ScheduleManifest:
    return ScheduleManifest(
        name=name,
        schedule=ScheduleSpec(interval=IntervalSpec(minutes=10), timezone="UTC"),
        targets=[_TARGET],
        intent="检查健康",
        report=ReportConfig(),
    )


def _build_runner(
    *, backend_factory: Any, run_store: RunStore, report_store: ReportStore, grace: float
) -> SchedulerRunner:
    target_registry = _make_target_registry()
    return SchedulerRunner(
        [_manifest()],
        run_store=run_store,
        report_store=report_store,
        settings=Settings(agent=AgentSettings()),
        backend_factory=backend_factory,
        context_factory=_context_factory(target_registry),
        target_registry=target_registry,
        grace_seconds=grace,
    )


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #


def test_list_shows_next_fire_time(runner: CliRunner, env: Path) -> None:
    _write_manifest(env, name="nightly")
    result = runner.invoke(app, ["schedule", "list"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "nightly" in result.stdout
    assert "next_fire_time=" in result.stdout


def test_list_invalid_manifest_fail_loud(runner: CliRunner, env: Path) -> None:
    _write_manifest(env, name="bad", target="not-registered")
    result = runner.invoke(app, ["schedule", "list"])
    # config/manifest load error → exit 2 (consistent with settings/targets)
    assert result.exit_code == 2, result.stdout + result.stderr
    assert "not-registered" in (result.stdout + result.stderr)


def test_list_malformed_targets_config_fail_loud(
    runner: CliRunner, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed targets.yaml (missing required ``version``) is fail-loud:

    exit 2, stderr names the targets config + reason, and NO raw traceback
    leaks (consistent with ``target list``'s ConfigError/ValidationError gate).
    """

    _write_manifest(env, name="nightly")
    bad_targets = env / "bad-targets.yaml"
    bad_targets.write_text(yaml.safe_dump({"targets": [{"name": _TARGET, "type": "local"}]}))
    monkeypatch.setenv("HOSTLENS_TARGETS_CONFIG_PATH", str(bad_targets))

    result = runner.invoke(app, ["schedule", "list"])
    assert result.exit_code == 2, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "targets config" in combined
    assert "Traceback" not in combined


# --------------------------------------------------------------------------- #
# trigger
# --------------------------------------------------------------------------- #


@_POSIX_ONLY
def test_trigger_produces_run_and_report_retrievable(
    runner: CliRunner, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_manifest(env, name="nightly")
    _patch_backend(monkeypatch, lambda: FakeBackend(responses=_happy_script()))

    result = runner.invoke(app, ["schedule", "trigger", "nightly"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "status=ok" in result.stdout

    # The Run is in runs.db (status default --json) and the Report is in
    # reports.db, retrievable via ``reports show``.
    status = runner.invoke(app, ["schedule", "status", "--json"])
    payload = json.loads(status.stdout)
    assert payload["status_counts"].get("ok") == 1
    report_id = payload["runs"][0]["report_id"]
    assert report_id is not None

    show = runner.invoke(app, ["reports", "show", report_id])
    assert show.exit_code == 0, show.stdout + show.stderr


def test_trigger_unknown_name_fail_loud(
    runner: CliRunner, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_manifest(env, name="nightly")
    _patch_backend(monkeypatch, lambda: FakeBackend(responses=[]))
    result = runner.invoke(app, ["schedule", "trigger", "no-such-name"])
    assert result.exit_code == 1, result.stdout + result.stderr
    assert "no-such-name" in (result.stdout + result.stderr)


def test_trigger_unexpected_exception_clean_exit_and_records_failed_run(
    runner: CliRunner, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected exception during a manual trigger must fail-loud cleanly.

    ``runner.trigger`` records a ``failed`` Run then re-raises; the CLI boundary
    turns that into exit 1 + a redacted message (no raw traceback), and the
    failed Run is still in the ledger.
    """
    _write_manifest(env, name="nightly")

    def _raising_factory() -> object:
        raise RuntimeError("backend boom token=sk-ant-api03-deadbeefdeadbeefdeadbeef")

    _patch_backend(monkeypatch, _raising_factory)

    result = runner.invoke(app, ["schedule", "trigger", "nightly"])
    combined = result.stdout + result.stderr
    assert result.exit_code == 1, combined
    assert "Traceback" not in combined  # clean fail-loud, not a raw traceback
    assert "sk-ant-api03-deadbeef" not in combined  # secret redacted on the CLI boundary

    # The failed Run is still recorded in the ledger (one fire → one Run).
    status = runner.invoke(app, ["schedule", "status", "--json"])
    payload = json.loads(status.stdout)
    assert payload["status_counts"].get("failed") == 1


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


def test_status_empty_history_exit_0(runner: CliRunner, env: Path) -> None:
    _write_manifest(env, name="nightly")
    result = runner.invoke(app, ["schedule", "status"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "无 Run 记录" in result.stdout

    js = runner.invoke(app, ["schedule", "status", "--json"])
    assert js.exit_code == 0
    assert json.loads(js.stdout) == {"runs": [], "status_counts": {}}


def test_status_unknown_name_fail_loud(runner: CliRunner, env: Path) -> None:
    _write_manifest(env, name="nightly")
    result = runner.invoke(app, ["schedule", "status", "--name", "no-such-name"])
    assert result.exit_code == 1, result.stdout + result.stderr
    assert "no-such-name" in (result.stdout + result.stderr)


@_POSIX_ONLY
def test_status_lists_recent_and_distribution(
    runner: CliRunner, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_manifest(env, name="nightly")
    _patch_backend(monkeypatch, lambda: FakeBackend(responses=_happy_script()))
    runner.invoke(app, ["schedule", "trigger", "nightly"])

    result = runner.invoke(app, ["schedule", "status"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "nightly" in result.stdout
    assert "status_counts:" in result.stdout
    assert "ok=1" in result.stdout


# --------------------------------------------------------------------------- #
# daemon backend safety gate (design D-12)
# --------------------------------------------------------------------------- #


def _daemon_unsafe_create_backend(settings: Any) -> Any:
    from hostlens.agent.backend import is_daemon_mode
    from hostlens.core.exceptions import BackendDaemonUnsafe

    # Mirror the real gate: only daemon mode rejects.
    if is_daemon_mode(settings):
        raise BackendDaemonUnsafe(
            backend_name="claude_subscription",
            reason="subscription_in_daemon",
        )
    return FakeBackend(responses=[])


@pytest.mark.parametrize("verb", ["daemon", "run"])
def test_subscription_backend_rejected_for_daemon_and_run(
    runner: CliRunner, env: Path, monkeypatch: pytest.MonkeyPatch, verb: str
) -> None:
    _write_manifest(env, name="nightly")
    monkeypatch.setattr("hostlens.cli.schedule.create_backend", _daemon_unsafe_create_backend)
    # Avoid the root guard tripping for the (unlikely) root CI case.
    monkeypatch.setattr("hostlens.cli.schedule.os.geteuid", lambda: 1000)

    result = runner.invoke(app, ["schedule", verb])
    assert result.exit_code == 1, result.stdout + result.stderr
    assert "daemon" in (result.stdout + result.stderr).lower()


# --------------------------------------------------------------------------- #
# shutdown grace config → runner (add-configurable-shutdown-grace)
# --------------------------------------------------------------------------- #


def _spy_grace_on_serve(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, float]:
    """Spy the PRODUCTION ``SchedulerRunner`` constructor + short-circuit the loop.

    Captures the ``grace_seconds`` the CLI actually passes (patching
    ``_build_runner`` would not — its signature has no ``grace_seconds``), and
    patches ``_serve_loop`` so the daemon loop returns immediately instead of
    blocking on a stop signal. The backend is daemon-safe so the boot gate
    passes.
    """

    captured: dict[str, float] = {}
    real_runner_cls = SchedulerRunner

    def _spy_runner(*args: Any, grace_seconds: float, **kwargs: Any) -> SchedulerRunner:
        captured["grace_seconds"] = grace_seconds
        return real_runner_cls(*args, grace_seconds=grace_seconds, **kwargs)

    monkeypatch.setattr("hostlens.cli.schedule.SchedulerRunner", _spy_runner)
    monkeypatch.setattr(
        "hostlens.cli.schedule.create_backend", lambda settings: FakeBackend(responses=[])
    )

    async def _noop_serve_loop(runner: Any, logger: Any) -> None:
        return None

    monkeypatch.setattr("hostlens.cli.schedule._serve_loop", _noop_serve_loop)
    monkeypatch.setattr("hostlens.cli.schedule.os.geteuid", lambda: 1000)
    return captured


def test_daemon_passes_default_grace_to_runner(
    runner: CliRunner, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_manifest(env, name="nightly")
    captured = _spy_grace_on_serve(monkeypatch)

    result = runner.invoke(app, ["schedule", "daemon"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert captured["grace_seconds"] == 120.0


def test_daemon_passes_env_override_grace_to_runner(
    runner: CliRunner, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_manifest(env, name="nightly")
    captured = _spy_grace_on_serve(monkeypatch)
    monkeypatch.setenv("HOSTLENS_DAEMON__SHUTDOWN_GRACE_SECONDS", "60")

    result = runner.invoke(app, ["schedule", "daemon"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert captured["grace_seconds"] == 60.0


@pytest.mark.parametrize(
    ("bad_value", "expected_constraint"),
    [
        ("0", "greater than or equal to 1"),
        ("-1", "greater than or equal to 1"),
        ("601", "less than or equal to 600"),
        ("not-a-number", "valid number"),
    ],
)
def test_daemon_invalid_grace_fail_loud_exit_2(
    runner: CliRunner,
    env: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_value: str,
    expected_constraint: str,
) -> None:
    """Illegal grace fails at ``load_settings`` (exit 2) before entering the loop."""

    _write_manifest(env, name="nightly")
    monkeypatch.setattr("hostlens.cli.schedule.os.geteuid", lambda: 1000)
    monkeypatch.setenv("HOSTLENS_DAEMON__SHUTDOWN_GRACE_SECONDS", bad_value)

    result = runner.invoke(app, ["schedule", "daemon"])
    output = result.stdout + result.stderr
    assert result.exit_code == 2, output
    assert "daemon.shutdown_grace_seconds" in output
    # Spec: stderr must indicate the field AND the expected range/constraint.
    assert expected_constraint in output


# --------------------------------------------------------------------------- #
# graceful shutdown (design D-5) — driven against the runner directly
# --------------------------------------------------------------------------- #


class _BlockingBackend:
    """Backend whose ``messages_create`` hangs forever (long-running job)."""

    name = "blocking"

    def __init__(self) -> None:
        self.capabilities = FakeBackend(responses=[]).capabilities

    async def messages_create(self, **_kwargs: Any) -> MessageResponse:
        await asyncio.Event().wait()  # never resolves
        raise AssertionError("unreachable")


@_POSIX_ONLY
async def test_graceful_stop_force_cancel_lands_daemon_stopped(tmp_path: Path) -> None:
    """A job exceeding the grace is force-cancelled → exactly one daemon_stopped."""

    run_store = RunStore(db_path=tmp_path / "runs.db")
    report_store = ReportStore(db_path=tmp_path / "reports.db", orphan_dir=tmp_path / "orphans")
    runner = _build_runner(
        backend_factory=lambda: cast(LLMBackend, _BlockingBackend()),
        run_store=run_store,
        report_store=report_store,
        grace=0.05,
    )

    # Start the blocking job as an in-flight task (mimics the executor's
    # internally-created task: the job body registers itself in ``_inflight``).
    job = asyncio.create_task(runner._run_job("nightly"))
    # Let the job body run up to its first ``await`` so it registers + blocks.
    await asyncio.sleep(0.01)
    assert runner._inflight  # the job registered itself

    await runner.graceful_stop()

    # graceful_stop returned → the shielded daemon_stopped save is already
    # drained (no extra sleep needed).
    rows = await run_store.list_recent(limit=10)
    assert len(rows) == 1
    assert rows[0].status is RunStatus.DAEMON_STOPPED
    assert rows[0].report_id is None
    assert job.cancelled() or job.done()


@_POSIX_ONLY
async def test_graceful_stop_within_grace_lands_real_status(tmp_path: Path) -> None:
    """A job finishing inside the grace lands its REAL status, not daemon_stopped."""

    run_store = RunStore(db_path=tmp_path / "runs.db")
    report_store = ReportStore(db_path=tmp_path / "reports.db", orphan_dir=tmp_path / "orphans")
    runner = _build_runner(
        backend_factory=lambda: cast(LLMBackend, FakeBackend(responses=_happy_script())),
        run_store=run_store,
        report_store=report_store,
        grace=5.0,
    )

    job = asyncio.create_task(runner._run_job("nightly"))
    await runner.graceful_stop()
    run = await job

    assert run.status is RunStatus.OK
    rows = await run_store.list_recent(limit=10)
    assert [r.status for r in rows] == [RunStatus.OK]


@_POSIX_ONLY
async def test_no_in_progress_placeholder_and_sigkill_leaves_no_run(tmp_path: Path) -> None:
    """While a job is mid-flight the ledger has NO row (no "in-progress" placeholder).

    This doubles as the SIGKILL contract: a process killed mid-job (no
    terminal write ever runs) leaves no Run record. M4 writes Run rows only at
    a terminal state — there is no start-row, so an un-catchable kill simply
    leaves the ledger empty (the M4 known limitation).
    """

    run_store = RunStore(db_path=tmp_path / "runs.db")
    report_store = ReportStore(db_path=tmp_path / "reports.db", orphan_dir=tmp_path / "orphans")
    runner = _build_runner(
        backend_factory=lambda: cast(LLMBackend, _BlockingBackend()),
        run_store=run_store,
        report_store=report_store,
        grace=5.0,
    )

    job = asyncio.create_task(runner._run_job("nightly"))
    await asyncio.sleep(0.01)
    assert runner._inflight  # the job is genuinely in flight

    # Mid-flight: no "running" placeholder row exists (Run rows are terminal
    # only). A SIGKILL here (no finally, no terminal write) would persist
    # nothing.
    rows_mid = await run_store.list_recent(limit=10)
    assert rows_mid == []

    # Test cleanup: cancel + drain. (This path DOES write daemon_stopped — it
    # models a catchable SIGTERM, not SIGKILL; the SIGKILL assertion above is
    # the mid-flight emptiness, before any terminal write.)
    job.cancel()
    await asyncio.gather(job, return_exceptions=True)


# --------------------------------------------------------------------------- #
# channel wiring → runner (fix: _build_runner injects load_channels result)
# --------------------------------------------------------------------------- #


def _write_notify_manifest(root: Path, *, name: str, channel: str) -> None:
    (root / "schedules" / f"{name}.yaml").write_text(
        yaml.safe_dump(
            {
                "name": name,
                "schedule": {"interval": {"minutes": 10}, "timezone": "UTC"},
                "targets": [_TARGET],
                "intent": "检查健康",
                "notify": [{"channel": channel}],
            }
        )
    )


def _write_notifiers_yaml(root: Path, monkeypatch: pytest.MonkeyPatch, *, channel: str) -> None:
    cfg = root / "notifiers.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "channels": {
                    channel: {
                        "type": "telegram",
                        "bot_token": "${HOSTLENS_TEST_TG_TOKEN}",
                        "chat_id": "12345",
                    }
                }
            }
        )
    )
    monkeypatch.setenv("HOSTLENS_NOTIFIERS_CONFIG_PATH", str(cfg))
    monkeypatch.setenv("HOSTLENS_TEST_TG_TOKEN", "123:abc")


def _cli_build_runner(env: Path) -> SchedulerRunner:
    """Drive the production ``cli/schedule.py:_build_runner`` end to end."""

    from hostlens.cli import schedule as schedule_cli

    settings = schedule_cli._load_settings_or_exit()
    target_registry = schedule_cli._build_target_registry(settings)
    inspector_registry = schedule_cli._build_inspector_registry(settings)
    manifests = schedule_cli._load_manifests_or_exit(settings, target_registry)
    logger = structlog.get_logger("test_cli_build_runner")
    return schedule_cli._build_runner(
        settings, manifests, target_registry, inspector_registry, logger
    )


def test_build_runner_injects_configured_channel(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manifest referencing a configured channel assembles + injects it."""

    _write_notify_manifest(env, name="nightly", channel="tg-main")
    _write_notifiers_yaml(env, monkeypatch, channel="tg-main")

    built = _cli_build_runner(env)
    assert "tg-main" in built._channels
    assert built._channels["tg-main"].name == "telegram"


@_POSIX_ONLY
def test_schedule_run_assembles_with_channels(
    runner: CliRunner, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``schedule trigger`` over a notify manifest assembles + fires (no traceback)."""

    _write_notify_manifest(env, name="nightly", channel="tg-main")
    _write_notifiers_yaml(env, monkeypatch, channel="tg-main")
    _patch_backend(monkeypatch, lambda: FakeBackend(responses=_happy_script()))

    result = runner.invoke(app, ["schedule", "trigger", "nightly"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "Traceback" not in (result.stdout + result.stderr)


def test_build_runner_unknown_channel_fail_loud(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A notify manifest with no notifiers.yaml → unknown channel → fail-loud.

    ``load_channels`` returns an empty map (no file), and the runner's
    assembly-time channel-existence check raises ``ConfigError`` on the
    unresolved reference, which ``_build_runner`` maps to the CLI's clean
    exit-2 (``typer.Exit(code=2)``) rather than a raw traceback.
    """

    _write_notify_manifest(env, name="nightly", channel="tg-main")
    missing = env / "no-such-notifiers.yaml"
    monkeypatch.setenv("HOSTLENS_NOTIFIERS_CONFIG_PATH", str(missing))

    with pytest.raises(typer.Exit) as exc:
        _cli_build_runner(env)
    assert exc.value.exit_code == 2


def test_trigger_unknown_channel_clean_exit_2_no_traceback(
    runner: CliRunner, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``schedule trigger`` over a notify manifest whose channel is not in
    notifiers.yaml exits 2 with a clean message and no raw traceback."""

    _write_notify_manifest(env, name="nightly", channel="ghost")
    missing = env / "no-such-notifiers.yaml"
    monkeypatch.setenv("HOSTLENS_NOTIFIERS_CONFIG_PATH", str(missing))
    monkeypatch.setattr("hostlens.cli.schedule.os.geteuid", lambda: 1000)

    result = runner.invoke(app, ["schedule", "trigger", "nightly"])
    combined = result.stdout + result.stderr
    assert result.exit_code == 2, combined
    assert "unknown channel" in combined
    assert "Traceback" not in combined


def test_build_runner_no_notify_manifest_assembles_without_channels(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manifest with no ``notify`` assembles fine when notifiers.yaml is absent."""

    _write_manifest(env, name="nightly")
    missing = env / "no-such-notifiers.yaml"
    monkeypatch.setenv("HOSTLENS_NOTIFIERS_CONFIG_PATH", str(missing))

    built = _cli_build_runner(env)
    assert built._channels == {}


def test_build_runner_malformed_notifiers_yaml_fail_loud(
    runner: CliRunner, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown channel ``type`` in notifiers.yaml is fail-loud (exit 2, clean)."""

    _write_notify_manifest(env, name="nightly", channel="tg-main")
    cfg = env / "notifiers.yaml"
    cfg.write_text(yaml.safe_dump({"channels": {"tg-main": {"type": "nope", "bot_token": "x"}}}))
    monkeypatch.setenv("HOSTLENS_NOTIFIERS_CONFIG_PATH", str(cfg))
    monkeypatch.setattr("hostlens.cli.schedule.os.geteuid", lambda: 1000)

    result = runner.invoke(app, ["schedule", "trigger", "nightly"])
    combined = result.stdout + result.stderr
    assert result.exit_code == 2, combined
    assert "notifier channels" in combined
    assert "Traceback" not in combined


# --------------------------------------------------------------------------- #
# daemon logging (design 5.5): file sink + secret redaction
# --------------------------------------------------------------------------- #


def test_daemon_log_writes_file_without_secret(tmp_path: Path) -> None:
    """``_configure_file_logging`` writes JSON to the file with secrets redacted."""

    from hostlens.cli.schedule import _configure_file_logging

    log_file = tmp_path / "logs" / "scheduler-daemon.log"
    log_file.parent.mkdir(parents=True)
    _configure_file_logging(Settings(), log_file)

    logger = structlog.get_logger("hostlens.schedule.daemon")
    logger.info("scheduler.started", api_key="sk-supersecretvalue", jobs=2)

    contents = log_file.read_text()
    assert "scheduler.started" in contents
    assert "sk-supersecretvalue" not in contents  # redacted by redact_sensitive
    assert "***" in contents
