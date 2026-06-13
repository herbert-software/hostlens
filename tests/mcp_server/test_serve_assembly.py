"""serve assembly wiring tests for ``hostlens mcp serve`` (tasks.md 5.1 / 5.2).

Covers the management-tool wiring added to ``cli/mcp.py serve``:

- ``list_tools`` projects 10 tools (read-only trio + 7 management);
- a daemon-unsafe / unimplemented backend (placeholder → ``NotImplementedError``)
  is rejected at the boot-time eager probe with **exit 1** (no raw traceback);
- an unreadable / malformed ``notifiers.yaml`` (``ConfigError``) fails before
  any running state with **exit 2**;
- the assembly order is fixed: a "mcp SDK missing + notifiers.yaml unreadable"
  combination deterministically takes the SDK-missing **exit 1** path.

Every test ``monkeypatch.chdir``es into a tmp dir and sets ``HOSTLENS_*`` env
explicitly so the developer ``.env`` at the repo root (which configures a real
backend + key) never leaks into the assembly under test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hostlens.cli import app
from hostlens.cli.mcp import MCP_INSTALL_HINT


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point every config path at tmp + strip the dev ``.env`` backend block.

    chdir keeps ``load_settings`` from reading the repo-root ``.env`` and keeps
    the cwd-relative ``schedules/`` scan empty. The explicit ``delenv`` calls
    cover the case where the process already inherited a backend block from the
    shell environment.
    """

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOSTLENS_INSPECTORS_SEARCH_PATHS", "")
    monkeypatch.setenv("HOSTLENS_TARGETS_CONFIG_PATH", str(tmp_path / "targets.yaml"))
    monkeypatch.setenv("HOSTLENS_NOTIFIERS_CONFIG_PATH", str(tmp_path / "notifiers.yaml"))
    for var in (
        "HOSTLENS_BACKEND__TYPE",
        "HOSTLENS_BACKEND__API_KEY",
        "HOSTLENS_BACKEND__BASE_URL",
        "HOSTLENS_BACKEND__DISABLE_THINKING",
        "HOSTLENS_BACKEND__PROMPT_CACHING",
        "HOSTLENS_BACKEND__EXTRA_HEADERS",
        "HOSTLENS_AGENT__PRIMARY_MODEL",
        "HOSTLENS_AGENT__HEALTH_CHECK_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)


# --------------------------------------------------------------------------- #
# 5.1 — list_tools == 10 (read-only trio + 7 management)
# --------------------------------------------------------------------------- #


def test_serve_assembles_ten_tools(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """serve registers the 3 default + 7 management tools = 10 on the mcp surface.

    ``build_server`` is replaced with a capturing stub so we assert the wired
    registry's mcp projection without entering the stdio run loop. A ``fake``
    backend makes the eager probe succeed (its daemon gate is a no-op).
    """
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HOSTLENS_BACKEND__TYPE", "fake")

    import hostlens.cli.mcp as mcp_cli

    captured: dict[str, Any] = {}

    def _capturing_build_server(registry: Any, context_factory: Any) -> Any:
        mcp_names = {spec.name for spec in registry.list_for("mcp")}
        captured["mcp_names"] = mcp_names
        return object()

    async def _noop_run_stdio(_server: Any) -> None:
        captured["ran"] = True

    monkeypatch.setattr(
        mcp_cli,
        "_import_mcp_server",
        lambda: (_capturing_build_server, _noop_run_stdio),
    )

    result = runner.invoke(app, ["mcp", "serve"])
    assert result.exit_code == 0, result.stdout + result.stderr

    names = captured["mcp_names"]
    assert len(names) == 10, sorted(names)
    assert names == {
        "list_inspectors",
        "list_targets",
        "run_inspector",
        "list_schedules",
        "get_schedule_status",
        "run_schedule_now",
        "list_channels",
        "list_reports",
        "show_report",
        "diff_reports",
    }
    assert captured["ran"] is True


# --------------------------------------------------------------------------- #
# 5.1 — daemon-unsafe / unimplemented backend → exit 1 (eager probe)
# --------------------------------------------------------------------------- #


def test_serve_unimplemented_backend_eager_probe_exits_1(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A placeholder backend (``bedrock`` → ``NotImplementedError``) is rejected
    at the boot eager probe with a clean exit 1 — no raw traceback. build_server
    must never run (the probe fires before it)."""
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HOSTLENS_BACKEND__TYPE", "bedrock")

    import hostlens.cli.mcp as mcp_cli

    def _build_must_not_run(*_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("build_server must not run when the eager probe rejects")

    async def _never_run(_server: Any) -> None:  # pragma: no cover
        raise AssertionError("run_stdio must not run")

    monkeypatch.setattr(mcp_cli, "_import_mcp_server", lambda: (_build_must_not_run, _never_run))

    result = runner.invoke(app, ["mcp", "serve"])
    assert result.exit_code == 1, result.stdout + result.stderr
    assert "backend not available" in result.stderr
    assert "Traceback" not in result.stderr


def test_serve_daemon_unsafe_backend_eager_probe_exits_1(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The forward-looking daemon gate path: a backend whose probe raises
    ``BackendDaemonUnsafe`` exits 1 (covers the future subscription backend; the
    serve catch is asserted directly by spying ``create_backend``)."""
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HOSTLENS_BACKEND__TYPE", "fake")

    import hostlens.cli.mcp as mcp_cli
    from hostlens.core.exceptions import BackendDaemonUnsafe

    def _spy_create_backend(_settings: Any) -> Any:
        raise BackendDaemonUnsafe(
            backend_name="claude_subscription",
            reason="subscription_in_daemon",
        )

    # The serve daemon-safe factory closes over ``create_backend`` imported into
    # management_tools; patch it there so the eager probe trips the daemon gate.
    monkeypatch.setattr("hostlens.tools.management_tools.create_backend", _spy_create_backend)

    def _build_must_not_run(*_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("build_server must not run when the eager probe rejects")

    async def _never_run(_server: Any) -> None:  # pragma: no cover
        raise AssertionError("run_stdio must not run")

    monkeypatch.setattr(mcp_cli, "_import_mcp_server", lambda: (_build_must_not_run, _never_run))

    result = runner.invoke(app, ["mcp", "serve"])
    assert result.exit_code == 1, result.stdout + result.stderr
    assert "backend not available" in result.stderr
    assert "Traceback" not in result.stderr


# --------------------------------------------------------------------------- #
# 5.2 — unreadable / malformed notifiers.yaml → exit 2 (deps construction)
# --------------------------------------------------------------------------- #


def test_serve_malformed_notifiers_yaml_exits_2(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A malformed ``notifiers.yaml`` (channel missing ``type``) makes the
    management-deps construction raise ``ConfigError`` → exit 2, before any
    running state, with a redacted message and no raw traceback."""
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HOSTLENS_BACKEND__TYPE", "fake")

    notifiers = tmp_path / "notifiers.yaml"
    notifiers.write_text(
        "channels:\n  tg:\n    bot_token: ${TG_TOKEN}\n",
        encoding="utf-8",
    )

    import hostlens.cli.mcp as mcp_cli

    def _build_must_not_run(*_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("build_server must not run on a config error")

    async def _never_run(_server: Any) -> None:  # pragma: no cover
        raise AssertionError("run_stdio must not run")

    monkeypatch.setattr(mcp_cli, "_import_mcp_server", lambda: (_build_must_not_run, _never_run))

    result = runner.invoke(app, ["mcp", "serve"])
    assert result.exit_code == 2, result.stdout + result.stderr
    assert "configuration error" in result.stderr
    assert "Traceback" not in result.stderr


def test_serve_unset_channel_env_var_exits_2(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A channel referencing an unset ``${ENV_VAR}`` makes the eager
    ``load_channels`` expansion fail-loud (``ConfigError`` →
    ``missing_env_var``) during management-deps construction → exit 2, before
    any running state. This is the intentional serve-boot coupling (design.md
    risk table): even though the surface is read-only, ``run_schedule_now``
    suppresses notify, and ``list_channels`` uses a non-expanding raw reader,
    serve still eager-resolves every configured channel's secrets at boot."""
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HOSTLENS_BACKEND__TYPE", "fake")
    monkeypatch.delenv("UNSET_TG_TOKEN", raising=False)

    notifiers = tmp_path / "notifiers.yaml"
    notifiers.write_text(
        "channels:\n  tg:\n    type: telegram\n    bot_token: ${UNSET_TG_TOKEN}\n",
        encoding="utf-8",
    )

    import hostlens.cli.mcp as mcp_cli

    def _build_must_not_run(*_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("build_server must not run on a config error")

    async def _never_run(_server: Any) -> None:  # pragma: no cover
        raise AssertionError("run_stdio must not run")

    monkeypatch.setattr(mcp_cli, "_import_mcp_server", lambda: (_build_must_not_run, _never_run))

    result = runner.invoke(app, ["mcp", "serve"])
    assert result.exit_code == 2, result.stdout + result.stderr
    assert "configuration error" in result.stderr
    assert "Traceback" not in result.stderr


# --------------------------------------------------------------------------- #
# 5.1 — assembly order: SDK missing wins over notifiers.yaml unreadable
# --------------------------------------------------------------------------- #


def test_serve_sdk_missing_wins_over_config_error(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When BOTH the mcp SDK is missing AND notifiers.yaml is malformed, the SDK
    import check (which runs first) wins → exit 1 with the install hint, not the
    ConfigError exit 2 path."""
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HOSTLENS_BACKEND__TYPE", "fake")

    # Malformed notifiers.yaml that would, on its own, drive exit 2.
    (tmp_path / "notifiers.yaml").write_text(
        "channels:\n  tg:\n    bot_token: ${TG_TOKEN}\n",
        encoding="utf-8",
    )

    import hostlens.cli.mcp as mcp_cli

    def _raise_import_error() -> tuple[Any, Any]:
        raise ImportError("No module named 'mcp'")

    monkeypatch.setattr(mcp_cli, "_import_mcp_server", _raise_import_error)

    result = runner.invoke(app, ["mcp", "serve"])
    assert result.exit_code == 1, result.stdout + result.stderr
    assert MCP_INSTALL_HINT in result.stderr
    assert "Traceback" not in result.stderr
