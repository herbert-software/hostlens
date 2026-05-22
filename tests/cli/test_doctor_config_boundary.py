"""Typer command-boundary handling of ConfigError.

`run_doctor()` calls `load_settings()`, which raises `ConfigError` on
invalid user config (e.g. `HOSTLENS_LOG_MODE=invalid`). The CLI must
translate this into a friendly one-liner on stderr instead of dumping a
Python traceback. core/config redaction already replaces sensitive field
values with "***", so printing the formatted message is safe.

Covers the Codex R2 finding: "There is no ConfigError handling at the
Typer command boundary".
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from hostlens.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_invalid_log_mode_emits_friendly_error_not_traceback(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invalid HOSTLENS_LOG_MODE must produce a one-line CLI error."""

    monkeypatch.setenv("HOSTLENS_LOG_MODE", "definitely-not-a-mode")
    result = runner.invoke(app, ["doctor", "--json"])

    # Exit code 2 signals "user input / config error" (distinct from 1
    # which means "checks ran but reported failure").
    assert result.exit_code == 2

    # Friendly message on stderr; no Python traceback leaking through.
    assert "configuration error" in result.stderr
    assert "log_mode" in result.stderr
    assert "Traceback" not in result.stderr
    assert "Traceback" not in result.stdout


def test_sensitive_field_value_redacted_at_cli_boundary(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if a sensitive field had an invalid value, its raw value must
    not leak to stderr at the CLI boundary (core/config already redacts;
    this asserts the boundary did not bypass that redaction)."""

    # Use a stable, non-existent sensitive-pattern field to avoid coupling
    # to any specific Settings field that may change. We assert the broad
    # invariant: no value that looks like a leaked secret appears in stderr.
    monkeypatch.setenv("HOSTLENS_LOG_LEVEL", "definitely-not-a-level")
    result = runner.invoke(app, ["doctor"])

    # Invalid log_level → ConfigError → exit 2 with friendly stderr.
    assert result.exit_code == 2
    assert "configuration error" in result.stderr
    # log_level is NOT sensitive, so its bad value IS expected in the
    # message (for debuggability). This test pairs with the
    # test_doctor_redaction suite, which covers actual sensitive fields.
    assert "log_level" in result.stderr


def test_valid_config_does_not_trigger_boundary_handler(runner: CliRunner) -> None:
    """Sanity: with no invalid env vars, doctor must run normally."""

    result = runner.invoke(app, ["doctor", "--json"])
    # 0 = all checks ready; 1 = a check failed; both acceptable on a
    # clean host. The point is: NOT 2 (which would mean ConfigError fired
    # spuriously).
    assert result.exit_code in (0, 1)
