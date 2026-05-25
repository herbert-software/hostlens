"""`config_dir` permission tests for `hostlens doctor`.

Covers cli-foundation spec §"配置目录不可读时退出 1" plus the
missing-config-dir status branch.

Implementation notes:
- We redirect the doctor's view of the config dir by monkeypatching the
  `_CONFIG_DIR_DEFAULT` module constant in `hostlens.cli.doctor`. This is
  preferred over manipulating `$HOME` because it leaves the rest of the
  process untouched and lets `pytest`'s `tmp_path` cleanup work.
- Tests that `chmod 0o000` a directory MUST `chmod 0o755` it back in a
  fixture teardown; otherwise `tmp_path` cleanup raises `PermissionError`
  and pollutes the test run with red.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hostlens.cli import app
from hostlens.cli import doctor as doctor_mod


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def unreadable_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Create a tmp dir, point doctor at it, chmod 000, restore on teardown."""

    target = tmp_path / "hostlens-cfg"
    target.mkdir()
    monkeypatch.setattr(doctor_mod, "_CONFIG_DIR_DEFAULT", target)
    os.chmod(target, 0o000)
    try:
        yield target
    finally:
        # Restore permissions so pytest's tmp_path cleanup can succeed.
        os.chmod(target, 0o755)


@pytest.fixture
def missing_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point doctor at a path that does not exist."""

    target = tmp_path / "definitely-not-created"
    assert not target.exists()
    monkeypatch.setattr(doctor_mod, "_CONFIG_DIR_DEFAULT", target)
    return target


@pytest.fixture
def readable_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "hostlens-cfg-ok"
    target.mkdir()
    monkeypatch.setattr(doctor_mod, "_CONFIG_DIR_DEFAULT", target)
    return target


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root bypasses POSIX read permissions; chmod 000 trick is a no-op",
)
def test_unreadable_config_dir_exits_one(runner: CliRunner, unreadable_config_dir: Path) -> None:
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 1, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["checks"]["config_dir"]["status"] == "unreadable"
    assert payload["ready"] is False


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root bypasses POSIX read permissions; chmod 000 trick is a no-op",
)
def test_unreadable_config_dir_emits_chmod_hint(
    runner: CliRunner, unreadable_config_dir: Path
) -> None:
    result = runner.invoke(app, ["doctor"])
    # stderr must carry an actionable hint; the spec example is `chmod 755`.
    assert "chmod" in result.stderr, result.stderr


def test_missing_config_dir_reports_missing_status(
    runner: CliRunner,
    missing_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Ensure the anthropic-key existence check doesn't muddy the readiness
    # bit; readiness rule allows config_dir=missing, so doctor must exit 0.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-placeholder")
    result = runner.invoke(app, ["doctor", "--json"])
    payload = json.loads(result.stdout)
    assert payload["checks"]["config_dir"]["status"] == "missing"
    assert result.exit_code == 0


def test_readable_config_dir_reports_ok(
    runner: CliRunner,
    readable_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-placeholder")
    result = runner.invoke(app, ["doctor", "--json"])
    payload = json.loads(result.stdout)
    cfg = payload["checks"]["config_dir"]
    assert cfg["status"] == "ok"
    assert cfg["path"] == str(readable_config_dir)
    assert result.exit_code == 0
