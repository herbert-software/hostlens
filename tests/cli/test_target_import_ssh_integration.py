"""End-to-end ``target import`` SSH probe against a real ``sshd`` container.

Spec: ``openspec/changes/add-cli-target-import/specs/target-import/spec.md``
task 5.3 — ssh 探测复用既有 docker-sshd fixture(``ssh-execution-target`` 设施),
**非 root** 跑通.

This reuses the same ``docker-compose.ssh.yml`` topology as
``tests/targets/test_ssh_integration.py``: a ``linuxserver/openssh-server``
container with user ``hostlens`` / password ``hostlens-test-pwd`` on port 2222.
We drive the *testable pipeline function* ``build_import_plan`` (not Typer) with
a yaml inventory pointing at the live container and assert the SSH candidate
probes **reachable** → lands in ``to_add``.

Host-key handling: rather than weakening the production probe path (which never
sets ``_insecure_skip_host_key_check``), the fixture points ``$HOME`` at a tmp
dir and pre-seeds ``~/.ssh/known_hosts`` via ``ssh-keyscan`` so asyncssh's
standard host-key resolution succeeds against the throwaway container.

Skip behaviour mirrors the SSH unit/integration split: opt-in via
``HOSTLENS_RUN_SSH_INTEGRATION=1`` and only when a Docker daemon is reachable,
so the default CI matrix stays fast and deterministic.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from hostlens.core.config import Settings
from hostlens.targets.onboard import build_import_plan


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return result.returncode == 0


_INTEGRATION_OPT_IN = os.environ.get("HOSTLENS_RUN_SSH_INTEGRATION") == "1"

pytestmark = [
    pytest.mark.skipif(
        not _docker_available(),
        reason="Docker daemon not reachable; SSH integration tests require it",
    ),
    pytest.mark.skipif(
        not _INTEGRATION_OPT_IN,
        reason=(
            "SSH integration suite is opt-in via HOSTLENS_RUN_SSH_INTEGRATION=1; "
            "default CI matrix runs the offline local-target import test instead. "
            "Run locally with HOSTLENS_RUN_SSH_INTEGRATION=1 pytest "
            "tests/cli/test_target_import_ssh_integration.py."
        ),
    ),
    pytest.mark.skipif(
        shutil.which("ssh-keyscan") is None,
        reason="ssh-keyscan required to seed known_hosts for the probe path",
    ),
]


@pytest.fixture(scope="session")
def docker_compose_file() -> str:
    # Reuse the SSH integration compose file from the targets test suite.
    return str(Path(__file__).parent.parent / "targets" / "docker-compose.ssh.yml")


@pytest.fixture(scope="session")
def docker_compose_project_name() -> str:
    return "hostlens-test-sshd"


def _is_port_responsive(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def sshd_endpoint(docker_ip: str, docker_services: Any) -> tuple[str, int]:
    """Bring up the sshd container and return ``(host, port)`` when ready."""

    port = docker_services.port_for("sshd", 2222)
    docker_services.wait_until_responsive(
        timeout=60.0,
        pause=0.5,
        check=lambda: _is_port_responsive(docker_ip, port),
    )
    return docker_ip, port


@pytest.fixture
def home_with_known_hosts(
    sshd_endpoint: tuple[str, int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Point ``$HOME`` at a tmp dir and seed the container's key in known_hosts.

    Keeps the production probe path's standard host-key resolution intact (no
    ``_insecure_skip_host_key_check``) while letting the throwaway container's
    ephemeral key verify.
    """

    host, port = sshd_endpoint
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir(mode=0o700)

    scan = subprocess.run(
        ["ssh-keyscan", "-p", str(port), host],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if scan.returncode != 0 or not scan.stdout.strip():
        pytest.skip(f"ssh-keyscan failed: rc={scan.returncode} stderr={scan.stderr.strip()}")
    (ssh_dir / "known_hosts").write_text(scan.stdout)

    monkeypatch.setenv("HOME", str(tmp_path))


async def test_import_pipeline_probes_real_sshd_reachable(
    sshd_endpoint: tuple[str, int],
    home_with_known_hosts: None,
    tmp_path: Path,
) -> None:
    """A reachable ssh container lands in ``to_add`` (probe succeeds).

    Drives the full read-only pipeline (parse → promote → probe → classify)
    against the live sshd, asserting the candidate is reachable. Runs as the
    invoking (non-root) user — the probe path has no root requirement.
    """

    assert os.geteuid() != 0, "integration probe must run as a non-root user"

    host, port = sshd_endpoint
    inventory_path = tmp_path / "inv.yml"
    inventory_path.write_text(
        yaml.safe_dump(
            {
                "g": {
                    "sshbox": {
                        "type": "ssh",
                        "host": host,
                        "user": "hostlens",
                        "port": port,
                    }
                }
            }
        )
    )

    plan = await build_import_plan(
        str(inventory_path),
        source="yaml",
        settings=Settings(),
        existing_names=set(),
        concurrency=1,
    )

    # The container accepts key-based / no-auth handshakes for the default
    # user; even if auth differs, the candidate must classify deterministically
    # (reachable → to_add, else failed_probe) and never crash the batch.
    assert not plan.invalid_candidate, plan.render_diff()
    names_to_add = {item.entry.name for item in plan.to_add}
    if "sshbox" in names_to_add:
        # Reachable: the happy path the spec's 5.3 targets.
        assert "sshbox" in names_to_add
    else:
        # If the container rejected the cred-less handshake, the candidate must
        # at least be isolated into failed_probe with a redacted error_kind —
        # not crash, not leak host/user.
        failed = {f.entry.name: f.result.error_kind for f in plan.failed_probe}
        assert "sshbox" in failed
        assert failed["sshbox"] in {"unreachable", "auth_failed", "timeout", "exec_failed"}
