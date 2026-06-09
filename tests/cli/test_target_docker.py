"""``hostlens target test`` + ``doctor --json`` coverage for docker targets.

Covers task 6.1 (``target test``) and task 6.2 (``doctor --json``).

The ``target test`` tests pin the *type-agnostic* claim for
``cli/target.py``: it carries no ``type == "docker"`` special-casing,
yet must:

- honour the disabled gate (exit 1) without dialling the daemon, and
- faithfully pass through the docker-class ``TargetError.kind`` raised by
  ``DockerTarget`` (``docker_sdk_unavailable`` when the ``[docker]`` extra
  is absent, otherwise ``docker_unavailable`` / ``container_not_found`` /
  ``container_not_running``).

No docker daemon is required: the disabled gate short-circuits before any
docker call, and the connectivity probe surfaces the docker-class kind
regardless of whether docker-py is installed (a non-existent ``docker_host``
socket forces ``docker_unavailable`` when it is). The set of acceptable
docker-class kinds below tolerates both environments so the test is stable
on CI (no docker) and on a developer box with the extra installed.

The ``doctor --json`` tests (task 6.2) pin the same type-agnostic claim for
``cli/doctor.py``: now that ``cli/_doctor_schema.TargetHealth.type`` admits
``"docker"``, a configured docker target no longer crashes doctor with a
``pydantic.ValidationError``. A disabled docker target reports
``connectivity == "skipped"`` (no daemon dial); an enabled one pointed at an
unreachable socket reports ``connectivity == "failed"`` and flips the
overall exit code to 1 — all without any docker-specific doctor branch.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from hostlens.cli import app

# Every kind ``DockerTarget`` may raise from its connectivity path. The
# CLI / doctor must surface whichever one applies verbatim — they never
# rewrite or swallow it.
_DOCKER_PROBE_KINDS = frozenset(
    {
        "docker_sdk_unavailable",
        "docker_unavailable",
        "container_not_found",
        "container_not_running",
    }
)


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
# 6.1 ``hostlens target test <docker-target>``
# ---------------------------------------------------------------------------


def test_target_test_disabled_docker_exits_1_no_daemon(
    runner: CliRunner,
    targets_yaml: Path,
) -> None:
    """A disabled docker target exits 1 via the shared disabled gate.

    The ``enabled is False`` check in ``test_cmd`` is type-agnostic and
    fires before ``DockerTarget`` would touch docker-py, so this passes
    with or without a daemon / the ``[docker]`` extra.
    """

    _write_yaml(
        targets_yaml,
        {
            "version": "1",
            "targets": [
                {
                    "name": "off-docker",
                    "type": "docker",
                    "container": "some-container",
                    "enabled": False,
                },
            ],
        },
    )
    result = runner.invoke(app, ["target", "test", "off-docker"])
    assert result.exit_code == 1, result.stdout + result.stderr
    assert "is disabled in targets.yaml" in result.stderr


def test_target_test_docker_passes_through_docker_kind(
    runner: CliRunner,
    targets_yaml: Path,
) -> None:
    """An enabled docker target whose daemon is unreachable exits 1 and
    surfaces a docker-class ``TargetError.kind`` on stderr.

    Pointing ``docker_host`` at a non-existent unix socket forces
    ``docker_unavailable`` when docker-py is installed; when it is not,
    ``_build_client`` raises ``docker_sdk_unavailable`` first. Either way
    the CLI passes the kind through unchanged — confirming ``target test``
    needs no docker-specific branch (task 6.1, no spec MODIFY).
    """

    _write_yaml(
        targets_yaml,
        {
            "version": "1",
            "targets": [
                {
                    "name": "demo-docker",
                    "type": "docker",
                    "container": "no-such-container",
                    "docker_host": "unix:///tmp/hostlens-nonexistent-docker.sock",
                },
            ],
        },
    )
    result = runner.invoke(app, ["target", "test", "demo-docker"])
    assert result.exit_code == 1, result.stdout + result.stderr
    assert any(kind in result.stderr for kind in _DOCKER_PROBE_KINDS), result.stderr
    assert "target=demo-docker" in result.stderr


# ---------------------------------------------------------------------------
# 6.2 ``hostlens doctor --json`` covers docker targets (type-agnostic)
# ---------------------------------------------------------------------------


def test_doctor_json_disabled_docker_skipped_no_daemon(
    runner: CliRunner,
    targets_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disabled docker target → ``connectivity == "skipped"``, exit 0.

    The disabled gate in ``_check_targets`` short-circuits before any probe,
    so this needs no daemon / ``[docker]`` extra. The row must carry
    ``type == "docker"`` (regression nail for the ``TargetHealth.type``
    Literal gap) and doctor must emit valid JSON without raising.
    """

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-placeholder")
    _write_yaml(
        targets_yaml,
        {
            "version": "1",
            "targets": [
                {
                    "name": "off-docker",
                    "type": "docker",
                    "container": "some-container",
                    "enabled": False,
                },
            ],
        },
    )
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)  # valid JSON, no ValidationError
    [row] = payload["targets"]
    assert row["type"] == "docker"
    assert row["connectivity"] == "skipped"
    assert row["enabled"] is False


def test_doctor_json_enabled_docker_unreachable_socket_fails(
    runner: CliRunner,
    targets_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An enabled docker target on an unreachable socket → ``failed``, exit 1.

    ``docker_host`` is validated to a non-empty local ``unix://`` socket, so
    a non-existent path is accepted by config but unreachable at probe time;
    the docker-class probe surfaces ``connectivity == "failed"`` and flips the
    overall exit to 1. doctor must still produce valid JSON (no crash),
    confirming the ``TargetHealth.type`` Literal now admits ``"docker"``.
    """

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-placeholder")
    _write_yaml(
        targets_yaml,
        {
            "version": "1",
            "targets": [
                {
                    "name": "demo-docker",
                    "type": "docker",
                    "container": "no-such-container",
                    "docker_host": "unix:///tmp/hostlens-nonexistent-docker.sock",
                },
            ],
        },
    )
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 1, result.stdout + result.stderr
    payload = json.loads(result.stdout)  # valid JSON, no ValidationError
    [row] = payload["targets"]
    assert row["type"] == "docker"
    assert row["connectivity"] == "failed"
    assert row["error_kind"] in _DOCKER_PROBE_KINDS
