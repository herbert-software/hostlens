from __future__ import annotations

from pathlib import Path

import pytest

from hostlens.core.config import Settings, load_settings
from hostlens.core.exceptions import ConfigError, HostlensError


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Clear HOSTLENS_* env vars and run each test in a clean cwd.

    `BaseSettings` reads from CWD-relative `.env`; running tests inside
    `tmp_path` keeps any developer-local `.env` out of the picture.
    """

    for key in list(__import__("os").environ):
        if key.startswith("HOSTLENS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


def test_defaults_apply_when_no_env_set() -> None:
    settings = load_settings()
    assert settings.log_level == "INFO"
    assert settings.log_mode == "prod"
    assert isinstance(settings.config_dir, Path)


def test_env_var_overrides_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOSTLENS_LOG_LEVEL", "INFO")
    settings = load_settings()
    assert settings.log_level == "INFO"


def test_env_var_overrides_log_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOSTLENS_LOG_MODE", "dev")
    settings = load_settings()
    assert settings.log_mode == "dev"


def test_invalid_log_level_raises_config_error_with_field_and_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOSTLENS_LOG_LEVEL", "NotALevel")

    with pytest.raises(ConfigError) as excinfo:
        load_settings()

    msg = str(excinfo.value)
    # Field name present
    assert "log_level" in msg
    # Actual offending value preserved (non-sensitive field => keep value for debugging)
    assert "NotALevel" in msg
    # Expected enum values surfaced so the user knows what to fix
    for valid in ("DEBUG", "INFO", "WARNING", "ERROR"):
        assert valid in msg


def test_config_error_chains_original_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOSTLENS_LOG_LEVEL", "NotALevel")

    with pytest.raises(ConfigError) as excinfo:
        load_settings()

    # ConfigError(original=...) preserves the underlying ValidationError so
    # advanced callers can introspect raw error metadata.
    original = excinfo.value.original
    assert original is not None
    assert original.__class__.__name__ == "ValidationError"


def test_config_error_is_hostlens_error() -> None:
    err = ConfigError("boom")
    assert isinstance(err, HostlensError)


def test_settings_direct_construction_raises_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per spec: bare `Settings()` is the lib-internal path and emits
    `pydantic.ValidationError`; only `load_settings()` converts to ConfigError.
    """

    from pydantic import ValidationError

    monkeypatch.setenv("HOSTLENS_LOG_LEVEL", "NotALevel")
    with pytest.raises(ValidationError):
        Settings()


def test_targets_config_path_default() -> None:
    settings = load_settings()
    assert isinstance(settings.targets_config_path, Path)
    assert settings.targets_config_path == Path("~/.config/hostlens/targets.yaml").expanduser()


def test_targets_config_path_env_var_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOSTLENS_TARGETS_CONFIG_PATH", "/tmp/x.yaml")
    settings = load_settings()
    assert settings.targets_config_path == Path("/tmp/x.yaml")


def test_ssh_idle_timeout_seconds_default() -> None:
    settings = load_settings()
    assert settings.ssh.idle_timeout_seconds == 300


def test_ssh_idle_timeout_seconds_env_var_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOSTLENS_SSH__IDLE_TIMEOUT_SECONDS", "120")
    settings = load_settings()
    assert settings.ssh.idle_timeout_seconds == 120


def test_daemon_shutdown_grace_seconds_default() -> None:
    settings = load_settings()
    assert settings.daemon.shutdown_grace_seconds == 120.0


def test_daemon_shutdown_grace_seconds_env_var_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOSTLENS_DAEMON__SHUTDOWN_GRACE_SECONDS", "60")
    settings = load_settings()
    assert settings.daemon.shutdown_grace_seconds == 60.0


@pytest.mark.parametrize(
    ("bad_value", "expected_constraint"),
    [
        ("0", "greater than or equal to 1"),
        ("-1", "greater than or equal to 1"),
        ("601", "less than or equal to 600"),
        ("not-a-number", "valid number"),
    ],
)
def test_daemon_shutdown_grace_seconds_invalid_raises_config_error(
    monkeypatch: pytest.MonkeyPatch, bad_value: str, expected_constraint: str
) -> None:
    monkeypatch.setenv("HOSTLENS_DAEMON__SHUTDOWN_GRACE_SECONDS", bad_value)

    with pytest.raises(ConfigError) as excinfo:
        load_settings()

    msg = str(excinfo.value)
    # Field name present (namespaced) so the operator knows what to fix.
    assert "daemon.shutdown_grace_seconds" in msg
    # Offending value preserved (non-sensitive field => keep value for debugging).
    assert bad_value in msg
    # Spec: error must indicate the expected range/constraint, not just the field.
    assert expected_constraint in msg


def test_inspectors_search_paths_default() -> None:
    settings = load_settings()
    assert settings.inspectors_search_paths == [Path("~/.config/hostlens/inspectors").expanduser()]


def test_inspectors_search_paths_env_var_override_single(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOSTLENS_INSPECTORS_SEARCH_PATHS", "/etc/hostlens/inspectors")
    settings = load_settings()
    assert settings.inspectors_search_paths == [Path("/etc/hostlens/inspectors")]


def test_inspectors_search_paths_env_var_override_multi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "HOSTLENS_INSPECTORS_SEARCH_PATHS",
        "/etc/hostlens/inspectors:/opt/team-inspectors",
    )
    settings = load_settings()
    assert settings.inspectors_search_paths == [
        Path("/etc/hostlens/inspectors"),
        Path("/opt/team-inspectors"),
    ]


def test_inspectors_search_paths_env_var_override_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOSTLENS_INSPECTORS_SEARCH_PATHS", "")
    settings = load_settings()
    assert settings.inspectors_search_paths == []


def test_inspectors_search_paths_drops_empty_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty segments (`":/a"`, `"/a::/b"`, `"/a:"`) must not inject CWD.

    ``Path("")`` silently resolves to the current working directory; a
    stray colon in the env value could let a hostile ``$PWD`` shadow
    trusted inspector locations. The parser drops empty parts so only
    explicit paths are scanned.
    """
    monkeypatch.setenv(
        "HOSTLENS_INSPECTORS_SEARCH_PATHS",
        ":/etc/hostlens/inspectors:/opt/team::",
    )
    settings = load_settings()
    assert settings.inspectors_search_paths == [
        Path("/etc/hostlens/inspectors"),
        Path("/opt/team"),
    ]
    assert Path("") not in settings.inspectors_search_paths
