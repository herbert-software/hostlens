"""Tests for `hostlens doctor` M1 targets extension.

Covers tasks 7.1-7.4 of ``add-execution-target-abstraction``:

- 7.1 ``_check_targets`` output structure (connectivity / credential /
      capabilities) per target.
- 7.2 ``doctor --json`` carries a ``targets`` key with stable schema.
- 7.3 inline_plaintext warns but doctor stays exit 0; failed target
      flips overall exit to 1; empty registry hints + exit 0.
- 7.4 M0 checks (python_version / anthropic_key / config_dir) remain
      under the same keys so the existing snapshot / redaction tests
      continue to pass.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml
from typer.testing import CliRunner

from hostlens.cli import app
from hostlens.targets.base import Capability
from hostlens.targets.registry import TargetRegistry


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def targets_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "targets.yaml"
    monkeypatch.setenv("HOSTLENS_TARGETS_CONFIG_PATH", str(path))
    return path


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False))


# ---------------------------------------------------------------------------
# Task 7.4 — M0 compatibility: existing keys remain
# ---------------------------------------------------------------------------


def test_doctor_json_keeps_m0_checks(
    runner: CliRunner,
    targets_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M0 checks remain under the same keys regardless of M1 additions.

    Spec task 7.4 explicitly requires the python_version /
    anthropic_key / config_dir entries to keep working without
    test changes after the M1 doctor extension lands.
    """

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-placeholder")
    result = runner.invoke(app, ["doctor", "--json"])
    payload = json.loads(result.stdout)
    assert set(payload["checks"].keys()) >= {"python_version", "anthropic_key", "config_dir"}
    assert payload["version"] == "0.1.0"


# ---------------------------------------------------------------------------
# Task 7.2 — JSON output contains a `targets` key
# ---------------------------------------------------------------------------


def test_doctor_json_has_targets_key_empty_registry(
    runner: CliRunner,
    targets_yaml: Path,
) -> None:
    """No targets.yaml → ``targets: []`` in JSON; doctor exits 0."""

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert "targets" in payload
    assert payload["targets"] == []


def test_doctor_json_targets_row_schema(
    runner: CliRunner,
    targets_yaml: Path,
) -> None:
    """Each `targets` row has the locked field set."""

    _write_yaml(
        targets_yaml,
        {"version": "1", "targets": [{"name": "alpha", "type": "local"}]},
    )
    result = runner.invoke(app, ["doctor", "--json"])
    payload = json.loads(result.stdout)
    assert len(payload["targets"]) == 1
    row = payload["targets"][0]
    expected_keys = {
        "name",
        "type",
        "enabled",
        "connectivity",
        "credential_source",
        "capabilities",
        "error_kind",
    }
    assert set(row.keys()) == expected_keys
    assert row["name"] == "alpha"
    assert row["type"] == "local"
    assert row["enabled"] is True
    assert row["connectivity"] == "ok"
    assert row["credential_source"] == "none"
    assert isinstance(row["capabilities"], list)


# ---------------------------------------------------------------------------
# Task 7.3 — three condition branches
# ---------------------------------------------------------------------------


def test_doctor_inline_plaintext_warns_but_stays_exit_0(
    runner: CliRunner,
    targets_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:doctor 检测明文密码 warn.

    Inline plaintext credentials must emit a warning to stderr but
    NOT flip doctor's overall exit code to 1.
    """

    # Provide ANTHROPIC_API_KEY so the only thing that could flip
    # readiness is the inline-plaintext warning we are testing.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-placeholder")
    _write_yaml(
        targets_yaml,
        {
            "version": "1",
            "targets": [
                {
                    "name": "leaky",
                    "type": "ssh",
                    "host": "127.0.0.1",
                    "user": "noone",
                    "password": "literal-pwd-not-env-placeholder",
                    "enabled": False,  # disabled → skipped, no probe
                },
            ],
        },
    )
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    [row] = payload["targets"]
    assert row["credential_source"] == "inline_plaintext"
    assert row["connectivity"] == "skipped"
    # Warning text on stderr (target name + remediation hint).
    assert "leaky" in result.stderr
    assert "inline" in result.stderr or "${VAR}" in result.stderr


def test_doctor_failed_target_flips_exit_1(
    runner: CliRunner,
    targets_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:某 target 连通失败 doctor exit 1."""

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-placeholder")
    _write_yaml(
        targets_yaml,
        {
            "version": "1",
            "targets": [
                {
                    "name": "ssh-unreachable",
                    "type": "ssh",
                    "host": "192.0.2.1",  # RFC 5737 doc range, never routable
                    "user": "noone",
                    "connect_timeout": 2,
                },
            ],
        },
    )
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 1, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    [row] = payload["targets"]
    assert row["connectivity"] == "failed"
    assert row["error_kind"] is not None
    assert row["error_kind"].startswith("ssh_")
    # Stderr should carry a remediation hint citing the target name.
    assert "ssh-unreachable" in result.stderr


def test_doctor_empty_registry_hints_and_exits_0(
    runner: CliRunner,
    targets_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:空 registry → hint + exit 0.

    The human render emits "run `hostlens target add` to start" so
    operators have a clear next step on a fresh install.
    """

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-placeholder")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "hostlens target add" in result.stdout


# ---------------------------------------------------------------------------
# Task 7.1 — credential_source classification
# ---------------------------------------------------------------------------


def test_doctor_credential_source_env_var(
    runner: CliRunner,
    targets_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`${VAR}` placeholder → ``credential_source == "env_var"``."""

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-placeholder")
    monkeypatch.setenv("DOCTOR_TEST_PWD", "supersecret")
    _write_yaml(
        targets_yaml,
        {
            "version": "1",
            "targets": [
                {
                    "name": "env-cred",
                    "type": "ssh",
                    "host": "127.0.0.1",
                    "user": "noone",
                    "password": "${DOCTOR_TEST_PWD}",
                    "enabled": False,
                },
            ],
        },
    )
    result = runner.invoke(app, ["doctor", "--json"])
    payload = json.loads(result.stdout)
    [row] = payload["targets"]
    assert row["credential_source"] == "env_var"


def test_doctor_credential_source_key_only(
    runner: CliRunner,
    targets_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``key_path`` only (no password/passphrase) → ``key_only``."""

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-placeholder")
    _write_yaml(
        targets_yaml,
        {
            "version": "1",
            "targets": [
                {
                    "name": "key-only",
                    "type": "ssh",
                    "host": "127.0.0.1",
                    "user": "noone",
                    "key_path": "/tmp/id_rsa",
                    "enabled": False,
                },
            ],
        },
    )
    result = runner.invoke(app, ["doctor", "--json"])
    payload = json.loads(result.stdout)
    [row] = payload["targets"]
    assert row["credential_source"] == "key_only"


# ---------------------------------------------------------------------------
# Event-loop unification + aclose plumbing
# ---------------------------------------------------------------------------


class _FakeTarget:
    """Minimal ``ExecutionTarget`` stand-in for doctor-flow tests.

    Records the running-loop id of each ``exec`` call so the test can
    assert all probes share one loop. Exposes ``aclose`` as an
    ``AsyncMock`` so the test can assert doctor releases per-target
    resources after the probe batch finishes.
    """

    def __init__(self, name: str, *, enabled: bool = True) -> None:
        self.name = name
        self.type = "local"
        self.capabilities = {Capability.SHELL, Capability.FILE_READ}
        self.loop_ids_seen: list[int] = []
        self.aclose = AsyncMock()
        from hostlens.targets.config import LocalEntry

        self._entry = LocalEntry(name=name, type="local", enabled=enabled)

    async def exec(self, cmd: str, *, timeout: int) -> Any:
        from hostlens.targets.base import ExecResult

        self.loop_ids_seen.append(id(asyncio.get_running_loop()))
        return ExecResult(
            exit_code=0,
            stdout="hostlens-doctor-probe",
            stderr="",
            duration_seconds=0.001,
            timed_out=False,
        )

    async def read_file(self, path: str) -> bytes:
        return b""


def _install_fake_registry(
    monkeypatch: pytest.MonkeyPatch,
    targets: list[_FakeTarget],
) -> None:
    """Replace doctor's ``build_registry_from_config`` with a fixed registry.

    A real registry is used so ``list_entries`` ordering / ``get`` lookups
    keep matching what doctor already expects from the production wiring.
    """

    def _factory(config: Any, settings: Any) -> TargetRegistry:
        registry = TargetRegistry()
        for t in targets:
            registry.register(t, t._entry)  # type: ignore[arg-type]
        return registry

    monkeypatch.setattr("hostlens.cli.doctor.build_registry_from_config", _factory)


def test_doctor_uses_single_event_loop_for_target_probes(
    runner: CliRunner,
    targets_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-target probes must share one ``asyncio.run`` event loop.

    A new loop per target invalidates any async resource (SSH control
    connection, async lock) the target may cache between calls; doctor
    must gather all probes under a single loop.
    """

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-placeholder")
    _write_yaml(
        targets_yaml,
        {
            "version": "1",
            "targets": [
                {"name": "alpha", "type": "local"},
                {"name": "bravo", "type": "local"},
                {"name": "charlie", "type": "local"},
            ],
        },
    )
    fakes = [_FakeTarget("alpha"), _FakeTarget("bravo"), _FakeTarget("charlie")]
    _install_fake_registry(monkeypatch, fakes)

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr

    observed_loop_ids = {lid for t in fakes for lid in t.loop_ids_seen}
    assert len(observed_loop_ids) == 1
    for t in fakes:
        assert len(t.loop_ids_seen) == 1


def test_doctor_calls_aclose_on_targets_with_aclose(
    runner: CliRunner,
    targets_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Doctor must release each target's async resources after probing.

    ``SSHTarget`` opens a control connection on the first probe; without
    an explicit ``aclose`` the connection lingers until ``__del__`` and
    surfaces a ResourceWarning under pytest's strict mode.
    """

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-placeholder")
    _write_yaml(
        targets_yaml,
        {
            "version": "1",
            "targets": [
                {"name": "alpha", "type": "local"},
                {"name": "bravo", "type": "local"},
            ],
        },
    )
    fakes = [_FakeTarget("alpha"), _FakeTarget("bravo")]
    _install_fake_registry(monkeypatch, fakes)

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    for t in fakes:
        assert t.aclose.await_count >= 1
