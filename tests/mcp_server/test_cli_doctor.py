"""CLI and doctor tests for the MCP optional-dependency surface."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hostlens.cli import app
from hostlens.cli._doctor_schema import CheckResult
from hostlens.cli.mcp import MCP_INSTALL_HINT


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_mcp_help_lists_serve(runner: CliRunner) -> None:
    result = runner.invoke(app, ["mcp", "--help"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "serve" in result.stdout


def test_root_help_lists_mcp(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "mcp" in result.stdout


def test_serve_missing_mcp_sdk_exits_1_with_hint(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hostlens.cli.mcp as mcp_cli

    def _raise_import_error() -> tuple[Any, Any]:
        raise ImportError("No module named 'mcp'")

    monkeypatch.setattr(mcp_cli, "_import_mcp_server", _raise_import_error)

    result = runner.invoke(app, ["mcp", "serve"])
    assert result.exit_code == 1, result.stdout + result.stderr
    assert MCP_INSTALL_HINT in result.stderr
    assert "Traceback" not in result.stderr


def test_serve_build_server_policy_violation_exits_1_cleanly(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # A registry whose mcp-surface tool forgot to declare sensitive_output makes
    # build_server's eager fail-closed check raise; serve must surface that as a
    # clean exit 1, not a raw traceback. serve now constructs a daemon-safe
    # backend and eager-probes it *before* build_server, so reaching the
    # build_server step requires a backend that passes the probe — supply a fake
    # one and isolate the dev .env so the assertion targets build_server's exit,
    # not the backend probe's config error.
    import hostlens.cli.mcp as mcp_cli
    from hostlens.core.exceptions import ToolPolicyViolation

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOSTLENS_INSPECTORS_SEARCH_PATHS", "")
    monkeypatch.setenv("HOSTLENS_TARGETS_CONFIG_PATH", str(tmp_path / "targets.yaml"))
    monkeypatch.setenv("HOSTLENS_NOTIFIERS_CONFIG_PATH", str(tmp_path / "notifiers.yaml"))
    for var in (
        "HOSTLENS_BACKEND__API_KEY",
        "HOSTLENS_BACKEND__BASE_URL",
        "HOSTLENS_BACKEND__DISABLE_THINKING",
        "HOSTLENS_BACKEND__PROMPT_CACHING",
        "HOSTLENS_BACKEND__EXTRA_HEADERS",
        "HOSTLENS_AGENT__PRIMARY_MODEL",
        "HOSTLENS_AGENT__HEALTH_CHECK_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOSTLENS_BACKEND__TYPE", "fake")

    def _build_raises(*_args: Any, **_kwargs: Any) -> Any:
        raise ToolPolicyViolation(
            tool_name="undeclared_mcp",
            surface="mcp",
            violated_field="sensitive_output",
            reason="sensitive_output_not_declared",
        )

    async def _never_run(_server: Any) -> None:  # pragma: no cover - unreached
        raise AssertionError("run_stdio must not run when build_server raised")

    monkeypatch.setattr(mcp_cli, "_import_mcp_server", lambda: (_build_raises, _never_run))

    result = runner.invoke(app, ["mcp", "serve"])
    assert result.exit_code == 1, result.stdout + result.stderr
    assert "refused to start" in result.stderr
    assert "Traceback" not in result.stderr


def test_doctor_json_includes_mcp_check_ok(runner: CliRunner) -> None:
    if importlib.util.find_spec("mcp") is None:
        pytest.skip("mcp SDK not installed in test environment")

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code in (0, 1), result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert "mcp" in report["checks"]
    assert report["checks"]["mcp"]["status"] == "ok"


def test_doctor_json_mcp_missing_is_non_fatal(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hostlens.cli.doctor as doctor_mod

    monkeypatch.setattr(
        doctor_mod,
        "check_mcp",
        lambda: CheckResult(status="missing", detail=None),
    )
    result_missing = runner.invoke(app, ["doctor", "--json"])
    assert result_missing.exit_code in (0, 1), result_missing.stdout + result_missing.stderr
    report_missing = json.loads(result_missing.stdout)
    assert report_missing["checks"]["mcp"]["status"] == "missing"

    monkeypatch.setattr(
        doctor_mod,
        "check_mcp",
        lambda: CheckResult(status="ok", detail=None),
    )
    result_ok = runner.invoke(app, ["doctor", "--json"])
    assert result_ok.exit_code in (0, 1), result_ok.stdout + result_ok.stderr
    report_ok = json.loads(result_ok.stdout)
    assert report_ok["checks"]["mcp"]["status"] == "ok"

    assert report_missing["ready"] == report_ok["ready"]


def test_doctor_human_mcp_missing_shows_install_hint(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hostlens.cli.doctor as doctor_mod

    monkeypatch.setattr(
        doctor_mod,
        "check_mcp",
        lambda: CheckResult(status="missing", detail=None),
    )

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code in (0, 1), result.stdout + result.stderr
    assert "mcp" in result.stdout
    assert 'pip install "hostlens[mcp]"' in result.stderr
