"""CLI tests for the ``hostlens notify`` subcommand group + ``doctor
--check-channels``.

Spec: ``openspec/changes/add-notifier-channels/specs/notify-cli-command/spec.md``.

Scenarios covered (task 7.6):

- ``notify channels`` lists configured channels without sending; a missing /
  malformed ``notifiers.yaml`` gives a readable message and does not crash
  (§场景:列出通道不触发发送 / §场景:notifiers.yaml 缺失或畸形时给出明确提示而非崩溃).
- ``notify render`` renders to stdout without any outbound request; an
  unknown report id and an unknown channel both fail loud with a non-zero
  exit (§场景:渲染既有报告到 stdout 不外发 / §场景:render 目标报告不存在则
  fail-loud).
- ``notify test`` in a non-interactive (no-TTY) run without ``--yes`` exits 1
  and sends nothing (§场景:非交互缺 --yes 退出 1).
- ``doctor --check-channels`` marks an invalid channel (unset env var) red
  under ``checks.channels`` without affecting other checks
  (§场景:无效通道配置被 doctor 标红).

The driver is the same ``_run_main`` (sys.argv patch + ``main()`` + capsys)
used across ``tests/cli`` so the click-UsageError → exit 3 wrapper in
``hostlens.cli.main`` is exercised. ``notifiers.yaml`` is pointed at a tmp
file via ``HOSTLENS_NOTIFIERS_CONFIG_PATH``; the report store via
``XDG_DATA_HOME``.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

import hostlens.inspectors.result  # noqa: F401  (triggers Report.model_rebuild)
from hostlens.cli import main
from hostlens.inspectors.result import InspectorResult
from hostlens.reporting.models import Finding, Report
from hostlens.reporting.store import ReportStore

# --------------------------------------------------------------------------- #
# Fixtures + driver
# --------------------------------------------------------------------------- #


@pytest.fixture
def xdg_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path


def _write_notifiers_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> Path:
    path = tmp_path / "notifiers.yaml"
    path.write_text(body)
    monkeypatch.setenv("HOSTLENS_NOTIFIERS_CONFIG_PATH", str(path))
    return path


def _run_main(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", ["hostlens", *argv])
    try:
        main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    else:
        code = 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _seed_report(store_dir: Path, *, target_name: str = "local-host") -> str:
    import asyncio

    ir = InspectorResult(
        name="hello.echo",
        version="1.0.0",
        status="ok",
        target_name=target_name,
        duration_seconds=0.1,
        output={},
        findings=[Finding(severity="warning", message="disk getting full")],
        error=None,
        missing=[],
    )
    ts = datetime(2026, 6, 4, 12, 0, 0)
    report = Report.from_inspector_results(target_name, [ir], started_at=ts, finished_at=ts)
    store = ReportStore(
        db_path=store_dir / "hostlens" / "reports.db",
        orphan_dir=store_dir / "hostlens" / "orphan_reports",
    )
    return asyncio.run(store.save(report)).run_id


_TELEGRAM_YAML = """\
channels:
  tg-main:
    type: telegram
    bot_token: "123456:fake-token-value"
    chat_id: "42"
"""


# --------------------------------------------------------------------------- #
# notify channels
# --------------------------------------------------------------------------- #


def test_channels_lists_without_sending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_notifiers_yaml(tmp_path, monkeypatch, _TELEGRAM_YAML)

    code, out, err = _run_main(["notify", "channels", "--json"], capsys, monkeypatch)

    assert code == 0, err
    rows = json.loads(out)
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "tg-main"
    assert row["type"] == "telegram"
    assert row["valid"] is True
    # secret never printed
    assert "fake-token-value" not in out


def test_channels_missing_yaml_readable_not_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Point at a non-existent file.
    monkeypatch.setenv("HOSTLENS_NOTIFIERS_CONFIG_PATH", str(tmp_path / "absent.yaml"))

    code, out, err = _run_main(["notify", "channels"], capsys, monkeypatch)

    assert code == 0
    assert "no channels configured" in out
    assert "Traceback" not in err


def test_channels_malformed_yaml_readable_not_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_notifiers_yaml(tmp_path, monkeypatch, "channels: [not, a, mapping\n  : :")

    code, out, err = _run_main(["notify", "channels"], capsys, monkeypatch)

    assert code == 0
    # Unparsable YAML is a present-but-malformed file: reported as malformed
    # (distinct from the genuinely-empty "no channels configured" state).
    assert "malformed" in (out + err)
    assert "no channels configured" not in (out + err)
    assert "Traceback" not in err


def test_channels_malformed_yaml_reported_as_malformed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A top-level list parses but is not a ``channels`` mapping: empty row
    # list, yet the file is present-but-malformed and must be reported as such
    # (distinct from the "no channels configured" empty state).
    _write_notifiers_yaml(tmp_path, monkeypatch, "- just\n- a\n- list\n")

    code, out, err = _run_main(["notify", "channels"], capsys, monkeypatch)

    assert code == 0
    assert "malformed" in (out + err)
    assert "no channels configured" not in (out + err)
    assert "Traceback" not in err


def test_channels_missing_env_var_marked_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("TG_TOKEN", raising=False)
    _write_notifiers_yaml(
        tmp_path,
        monkeypatch,
        'channels:\n  tg:\n    type: telegram\n    bot_token: "${TG_TOKEN}"\n    chat_id: "1"\n',
    )

    code, out, err = _run_main(["notify", "channels", "--json"], capsys, monkeypatch)

    assert code == 0
    assert "Traceback" not in err
    row = json.loads(out)[0]
    assert row["valid"] is False
    assert "TG_TOKEN" in row["missing_env_vars"]


def test_channels_empty_placeholder_marked_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A valid ${SET_VAR} placeholder plus an illegal empty ${} placeholder: the
    # loader raises on ${}, so ``channels`` must report the row invalid (not
    # optimistically expand it to "") without a traceback.
    monkeypatch.setenv("LARK_WEBHOOK", "https://example.invalid/hook")
    _write_notifiers_yaml(
        tmp_path,
        monkeypatch,
        'channels:\n  lk:\n    type: lark\n    webhook_url: "${LARK_WEBHOOK}"\n    secret: "${}"\n',
    )

    code, out, err = _run_main(["notify", "channels", "--json"], capsys, monkeypatch)

    assert code == 0
    assert "Traceback" not in err
    row = json.loads(out)[0]
    assert row["valid"] is False
    assert "${}" in row["error"] or "empty" in row["error"]


# --------------------------------------------------------------------------- #
# notify render
# --------------------------------------------------------------------------- #


def test_render_to_stdout_no_outbound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    xdg_home: Path,
) -> None:
    _write_notifiers_yaml(tmp_path, monkeypatch, _TELEGRAM_YAML)
    run_id = _seed_report(xdg_home)

    code, out, err = _run_main(
        ["notify", "render", "--report", run_id, "--channel", "tg-main"],
        capsys,
        monkeypatch,
    )

    assert code == 0, err
    # The rendered MarkdownV2 body carries the finding message.
    assert "disk getting full" in out
    # Bot token never reaches the rendered payload.
    assert "fake-token-value" not in out


def test_render_unknown_report_fail_loud(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    xdg_home: Path,
) -> None:
    _write_notifiers_yaml(tmp_path, monkeypatch, _TELEGRAM_YAML)
    unknown = "00000000-0000-0000-0000-000000000000"

    code, out, err = _run_main(
        ["notify", "render", "--report", unknown, "--channel", "tg-main"],
        capsys,
        monkeypatch,
    )

    assert code == 1
    assert out == ""
    assert "report not found" in err
    assert "Traceback" not in err


def test_render_unknown_channel_fail_loud(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    xdg_home: Path,
) -> None:
    _write_notifiers_yaml(tmp_path, monkeypatch, _TELEGRAM_YAML)
    run_id = _seed_report(xdg_home)

    code, out, err = _run_main(
        ["notify", "render", "--report", run_id, "--channel", "nope"],
        capsys,
        monkeypatch,
    )

    assert code == 1
    assert out == ""
    assert "unknown channel" in err
    assert "Traceback" not in err


def test_render_only_if_routing_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    xdg_home: Path,
) -> None:
    _write_notifiers_yaml(tmp_path, monkeypatch, _TELEGRAM_YAML)
    run_id = _seed_report(xdg_home)  # aggregate severity = warning

    code, out, err = _run_main(
        [
            "notify",
            "render",
            "--report",
            run_id,
            "--channel",
            "tg-main",
            "--only-if",
            "severity >= critical",
        ],
        capsys,
        monkeypatch,
    )

    assert code == 0, err
    assert "routing: skip" in err
    assert "Traceback" not in err
    # Render still happens (dry-run shows the payload regardless of routing).
    assert "disk getting full" in out


# --------------------------------------------------------------------------- #
# notify test
# --------------------------------------------------------------------------- #


def test_test_non_interactive_without_yes_exits_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_notifiers_yaml(tmp_path, monkeypatch, _TELEGRAM_YAML)
    # capsys-driven run has no TTY; stdin.isatty() is False.
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    code, out, err = _run_main(
        ["notify", "test", "--channel", "tg-main"],
        capsys,
        monkeypatch,
    )

    assert code == 1
    assert "--yes required" in err
    # No send happened (no success/failure send line on stdout).
    assert "sent test ping" not in out


# --------------------------------------------------------------------------- #
# doctor --check-channels
# --------------------------------------------------------------------------- #


def test_doctor_check_channels_marks_invalid_red(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("TG_TOKEN", raising=False)
    _write_notifiers_yaml(
        tmp_path,
        monkeypatch,
        'channels:\n  tg:\n    type: telegram\n    bot_token: "${TG_TOKEN}"\n    chat_id: "1"\n',
    )

    code, out, err = _run_main(["doctor", "--check-channels", "--json"], capsys, monkeypatch)

    assert "Traceback" not in err
    report = json.loads(out)
    assert "channels" in report["checks"]
    assert report["checks"]["channels"]["status"] == "error"
    # Other checks unaffected (python_version still ok).
    assert report["checks"]["python_version"]["status"] == "ok"
    # A failed channel flips overall readiness.
    assert report["ready"] is False
    assert code == 1


def test_doctor_without_flag_has_no_channels_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_notifiers_yaml(tmp_path, monkeypatch, _TELEGRAM_YAML)

    code, out, err = _run_main(["doctor", "--json"], capsys, monkeypatch)

    assert code in (0, 1)
    assert "Traceback" not in err
    report = json.loads(out)
    # Without --check-channels the base schema is untouched.
    assert "channels" not in report["checks"]
