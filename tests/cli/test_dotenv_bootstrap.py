"""CLI startup `.env` bootstrap behaviour (add-dotenv-env-loading 2.1).

Covers the behavioural scenarios from the `dotenv-env-bootstrap` spec: the
root callback loads `.env` into `os.environ` via `dotenv_values` +
`os.environ.setdefault` so `${VAR}` / inspector secrets can read it, an
explicit `export` (pre-existing `os.environ`) wins (setdefault only fills
gaps), a missing / unreadable / is-a-directory `.env` is silent, `Settings`
values are unchanged (only the hit layer moves from the `.env` file layer to
`os.environ`), `${VAR}` interpolation matches pydantic's file-wins order, and
`PYTHON_DOTENV_DISABLED` cannot silently disable the bootstrap.

Each test `chdir`s into a tmp dir and `delenv`s the variables it asserts
on, so a developer `.env` at the repo root never leaks into the assertion
(CLAUDE.md red line: tests that exercise the root callback must isolate
cwd + env, else local-green / clean-CI-red).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import dotenv_values

from hostlens.cli import _root
from hostlens.core.config import load_settings


def test_dotenv_value_injected_into_environ(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A non-`HOSTLENS_` var in `.env` lands in `os.environ` for `${VAR}`."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=from_dotenv\n")

    _root()

    assert os.environ["TELEGRAM_BOT_TOKEN"] == "from_dotenv"


def test_explicit_export_wins_over_dotenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`override=False`: a pre-set `os.environ` value is not overwritten."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("X", "from_export")
    (tmp_path / ".env").write_text("X=from_dotenv\n")

    _root()

    assert os.environ["X"] == "from_export"


def test_missing_dotenv_is_silent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No `.env`: no exception, no path / missing-file message on any stream."""

    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / ".env").exists()

    _root()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_settings_value_unchanged_after_load(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`HOSTLENS_LOG_MODE=dev` in `.env` (no export) still yields `log_mode=dev`."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HOSTLENS_LOG_MODE", raising=False)
    (tmp_path / ".env").write_text("HOSTLENS_LOG_MODE=dev\n")

    _root()

    assert load_settings().log_mode == "dev"


def test_unusable_dotenv_is_silent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unreadable / is-a-directory `.env` (OSError) is skipped like a missing one."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FOO", raising=False)

    def _raise(**_: object) -> dict[str, str | None]:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("hostlens.cli.dotenv_values", _raise)

    _root()  # must not raise

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert "FOO" not in os.environ


def test_interpolation_matches_pydantic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`${VAR}` written to `os.environ` matches what pydantic's `dotenv_values` computes."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FOO", "from_export")  # shadows the .env FOO
    monkeypatch.delenv("BAR", raising=False)
    (tmp_path / ".env").write_text("FOO=from_dotenv\nBAR=${FOO}/x\n")

    _root()

    assert os.environ["BAR"] == dotenv_values(tmp_path / ".env")["BAR"]


def test_disabled_flag_does_not_kill_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`PYTHON_DOTENV_DISABLED` must not silently no-op the bootstrap (`load_dotenv` would)."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.delenv("HOSTLENS_LOG_MODE", raising=False)
    (tmp_path / ".env").write_text("HOSTLENS_LOG_MODE=dev\n")

    _root()

    assert os.environ["HOSTLENS_LOG_MODE"] == "dev"
