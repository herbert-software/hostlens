"""Tests for ``hostlens doctor`` — base checks + targets + inspectors.

Areas covered:

- ``_check_targets`` output structure (connectivity / credential /
  capabilities) per target.
- ``doctor --json`` carries a ``targets`` key with stable schema.
- inline_plaintext credentials warn but doctor stays exit 0; a failed
  target flips overall exit to 1; empty registry hints + exit 0.
- The base checks (python_version / anthropic_key / config_dir) remain
  under their original keys so the existing snapshot / redaction tests
  continue to pass.
- ``_check_inspectors`` returns the three documented statuses
  (``ok`` / ``warn`` / ``fail``) and surfaces fatal
  ``duplicate_inspector`` errors uniformly.
- ``doctor --json`` carries an ``inspectors`` key alongside the
  base + targets sections.
- ``inspectors.status == "fail"`` flips the overall exit code to 1
  even when targets are healthy.
- The pre-existing base + targets keys remain present after the
  additive ``inspectors`` section landed.
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


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    """Persist a manifest dict as yaml under ``path``.

    Local helper for the inspectors tests; matches the same shape used
    by ``tests/cli/test_inspectors.py`` so a future shared fixture file
    can absorb both without churn.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False))


def _valid_manifest_payload(
    *,
    name: str,
    secrets: list[str] | None = None,
) -> dict[str, Any]:
    """Build a minimal valid manifest dict for user-path test fixtures."""

    return {
        "name": name,
        "version": "1.0.0",
        "description": f"Doctor test inspector {name}",
        "tags": [],
        "targets": ["local"],
        "requires_capabilities": [],
        "requires_binaries": [],
        "privilege": "none",
        "secrets": secrets or [],
        "collect": {"command": "echo test", "timeout_seconds": 5},
        "parse": {"format": "raw"},
        "output_schema": {
            "type": "object",
            "properties": {"raw": {"type": "string"}},
            "required": ["raw"],
        },
        "findings": [],
    }


@pytest.fixture
def user_inspectors_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``Settings.inspectors_search_paths`` at a per-test tmp dir.

    Mirrors the same ``HOSTLENS_INSPECTORS_SEARCH_PATHS`` env override used
    by the CLI inspectors test module — see that file for the rationale.
    Without this fixture, doctor would scan the operator's real
    ``~/.config/hostlens/inspectors`` and tests would become host-sensitive.
    """

    user_path = tmp_path / "inspectors"
    user_path.mkdir()
    monkeypatch.setenv("HOSTLENS_INSPECTORS_SEARCH_PATHS", str(user_path))
    return user_path


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
# M0 compatibility: existing keys remain
# ---------------------------------------------------------------------------


def test_doctor_json_keeps_m0_checks(
    runner: CliRunner,
    targets_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Base checks remain under the same keys after additive extensions.

    The python_version / anthropic_key / config_dir entries must keep
    rendering with the same JSON shape even after targets / inspectors
    sections were added.
    """

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-placeholder")
    result = runner.invoke(app, ["doctor", "--json"])
    payload = json.loads(result.stdout)
    assert set(payload["checks"].keys()) >= {"python_version", "anthropic_key", "config_dir"}
    assert payload["version"] == "0.1.0"


# ---------------------------------------------------------------------------
# JSON output contains a `targets` key
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
# three condition branches
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
# credential_source classification
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


# ---------------------------------------------------------------------------
# ``inspectors`` section
# ---------------------------------------------------------------------------


def test_doctor_json_inspectors_status_ok_with_builtins_only(
    runner: CliRunner,
    user_inspectors_dir: Path,
    targets_yaml: Path,
) -> None:
    """Spec §场景:全部加载成功 status=ok.

    The builtin set always loads cleanly; with an empty user path and no
    declared secrets, ``inspectors.status`` is ``ok``, ``loaded == 2``
    (``hello.echo`` + ``system.uptime``), and both error lists are
    empty.
    """

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["inspectors"]["status"] == "ok"
    assert payload["inspectors"]["loaded"] == 2
    assert payload["inspectors"]["errors"] == []
    assert payload["inspectors"]["missing_secrets"] == []


def test_doctor_json_inspectors_status_warn_on_missing_secret(
    runner: CliRunner,
    user_inspectors_dir: Path,
    targets_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:secret 缺失 status=warn.

    A user manifest declaring ``secrets: [PGPASSWORD]`` plus the env var
    being **absent** triggers ``status="warn"``; the overall doctor exit
    stays 0 because ``warn`` does not flip readiness.
    """

    monkeypatch.delenv("PGPASSWORD", raising=False)
    _write_manifest(
        user_inspectors_dir / "pg.yaml",
        _valid_manifest_payload(name="db.pg", secrets=["PGPASSWORD"]),
    )

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["inspectors"]["status"] == "warn"
    assert payload["inspectors"]["missing_secrets"] == [
        {"inspector": "db.pg", "secret": "PGPASSWORD"}
    ]
    # Warn does not produce a load-error row.
    assert payload["inspectors"]["errors"] == []


def test_doctor_json_inspectors_status_fail_on_bad_yaml(
    runner: CliRunner,
    user_inspectors_dir: Path,
    targets_yaml: Path,
) -> None:
    """Spec §场景:加载错误 status=fail.

    A malformed user manifest lands in ``inspectors.errors`` and flips
    ``status`` to ``fail``; doctor's overall exit code becomes 1 because
    ``_is_ready`` rejects ``inspectors.status == "fail"``.
    """

    bad = user_inspectors_dir / "bad.yaml"
    bad.write_text("name: [unclosed")

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 1, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["inspectors"]["status"] == "fail"
    assert len(payload["inspectors"]["errors"]) == 1
    err_row = payload["inspectors"]["errors"][0]
    assert err_row["kind"] == "manifest_parse_error"
    assert "bad.yaml" in err_row["path"]


def test_doctor_duplicate_inspector_reports_builtin_loaded_count(
    runner: CliRunner,
    user_inspectors_dir: Path,
    targets_yaml: Path,
) -> None:
    """``loaded`` reflects already-registered builtins on fatal duplicate.

    ``build_registry_from_search_paths`` scans builtins before user paths.
    When a user manifest shares a name with a builtin, the builder raises
    ``duplicate_inspector`` AFTER the builtins are already registered.
    ``_check_inspectors`` re-derives the builtin count from disk so the
    JSON contract doesn't under-report what's actually available.
    """

    # User-path manifest with the same name as a builtin → fatal duplicate.
    _write_manifest(
        user_inspectors_dir / "hello-clone.yaml",
        _valid_manifest_payload(name="hello.echo"),
    )

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 1, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["inspectors"]["status"] == "fail"
    assert payload["inspectors"]["errors"][0]["kind"] == "duplicate_inspector"
    # Must reflect the actual builtin count (currently 2: hello.echo + system.uptime).
    assert payload["inspectors"]["loaded"] >= 2, payload["inspectors"]


def test_doctor_builtin_failure_reports_loaded_zero(
    runner: CliRunner,
    targets_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Builtin-path fatal failure reports ``loaded=0`` (not the disk count).

    When ``build_registry_from_search_paths`` raises while scanning builtins
    (e.g. broken builtin manifest), the registry state is incomplete; the
    JSON contract must NOT show a "healthy loaded count" while build failed.
    """

    # Simulate a builtin-path failure by patching the builder to raise a
    # non-duplicate fatal kind. We can't easily corrupt the on-disk builtin
    # tree (it's part of the package), so we patch at the module boundary.
    from hostlens.core.exceptions import InspectorError as RealInspectorError

    def _raise_builtin_error(*args: object, **kwargs: object) -> None:
        raise RealInspectorError(
            kind="manifest_parse_error",
            path=Path("/fake/builtin/broken.yaml"),
        )

    monkeypatch.setattr(
        "hostlens.cli.doctor.build_registry_from_search_paths",
        _raise_builtin_error,
    )

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 1, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["inspectors"]["status"] == "fail"
    assert payload["inspectors"]["errors"][0]["kind"] == "manifest_parse_error"
    # MUST be 0 — registry build aborted, nothing actually loaded.
    assert payload["inspectors"]["loaded"] == 0, payload["inspectors"]


def test_doctor_inspectors_fail_with_healthy_targets_still_exits_1(
    runner: CliRunner,
    user_inspectors_dir: Path,
    targets_yaml: Path,
) -> None:
    """``inspectors=fail`` joins the overall fail set even when targets ok.

    Verifies the readiness predicate ANDs the inspector status into the
    existing target / check criteria — a healthy target registry is no
    longer enough to keep doctor at exit 0 once a user manifest is bad.
    """

    bad = user_inspectors_dir / "bad.yaml"
    bad.write_text("name: [unclosed")
    _write_yaml(
        targets_yaml,
        {"version": "1", "targets": [{"name": "alpha", "type": "local"}]},
    )

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 1, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    # Target is healthy.
    [trow] = payload["targets"]
    assert trow["connectivity"] == "ok"
    # Inspectors flipped exit code.
    assert payload["inspectors"]["status"] == "fail"


def test_doctor_json_keeps_all_pre_existing_keys(
    runner: CliRunner,
    user_inspectors_dir: Path,
    targets_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every previously-defined top-level key remains present.

    Locks the additive guarantee — ``checks`` retains its base
    entries, ``targets`` stays under its own key, and ``inspectors``
    sits alongside without displacing anything. Snapshot tests in
    ``test_doctor_schema_snapshot.py`` use the same keyset; failing
    this here surfaces the regression closer to the affected wiring.
    """

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-placeholder")
    result = runner.invoke(app, ["doctor", "--json"])
    payload = json.loads(result.stdout)
    expected = {"version", "timestamp", "checks", "ready", "targets", "inspectors"}
    assert set(payload.keys()) == expected
    assert set(payload["checks"].keys()) >= {
        "python_version",
        "anthropic_key",
        "config_dir",
    }
    # Inspector block has the four documented fields.
    assert set(payload["inspectors"].keys()) == {
        "status",
        "loaded",
        "errors",
        "missing_secrets",
    }
