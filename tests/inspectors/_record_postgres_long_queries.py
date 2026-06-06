"""One-shot fixture recorder for `postgres.long_queries` (dev-tool, NOT a test).

Drives the real `InspectorRunner` (via `record_fixture`) against the pinned
compose `postgres` service through a docker-exec ExecutionTarget — zero drift, no
local psql install.

Records (to tests/inspectors/fixtures/postgres_long_queries/):
  * healthy.json            — fresh server, NO external long query → count=0 (the
    inspector's OWN backend is excluded via `pid <> pg_backend_pid()`, so a healthy
    instance reports 0, NOT a vacuous self-trigger — design D-3) → status=ok.
  * semantic_abnormal.json  — a background `SELECT pg_sleep(...)` connection held
    active PAST the DEFAULT `threshold_seconds` before sampling, so long_query_count
    crosses the DEFAULT warn_count → warning (a REAL sustained workload running in
    the sampling window — the wave-2b time-coordination, D-3; NOT a lowered
    threshold).
  * access_denied.json      — WRONG password (set, not unset) → psql auth failure →
    exit 1 + empty stdout → status=exception. allow_failed=True.
  * conn_refused.json       — port 15999 (nothing listening) → exit 1 → exception.
    allow_failed=True.

Usage (manages compose lifecycle + the background sleeper itself):
    .venv-impl/bin/python tests/inspectors/_record_postgres_long_queries.py

Intentionally NOT collected by pytest (no `test_` prefix).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.recorder import record_fixture
from tests.inspectors._compose_record import (
    POSTGRES_ROOT_PW as ROOT_PW,
)
from tests.inspectors._compose_record import (
    DockerExecTarget,
    compose_down,
    compose_up,
    container_name,
    wait_healthy,
)

MANIFEST = Path("src/hostlens/inspectors/builtin/postgres/long_queries.yaml")
FIXTURE_DIR = Path("tests/inspectors/fixtures/postgres_long_queries")

_WRONG_PW = "wrong-" + "password"


async def _record(
    out_name: str,
    *,
    container: str,
    pw: str,
    parameters: dict[str, Any] | None = None,
    allow_failed: bool = False,
) -> str:
    manifest = load_manifest(MANIFEST)
    os.environ["HOSTLENS_POSTGRES_PASSWORD"] = pw
    target = DockerExecTarget("recorder", container)
    fixture = await record_fixture(
        manifest,
        target,  # type: ignore[arg-type]
        settings=Settings(),
        parameters=parameters,
        allow_failed=allow_failed,
    )
    path = FIXTURE_DIR / out_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fixture.to_json())
    print(f"wrote {path}")
    return path.read_text()


def _main_command(fixture_json: str) -> dict[str, Any]:
    return json.loads(fixture_json)["commands"][-1]


def _container_hostname(container: str) -> str:
    """The container's own hostname resolves to its non-loopback eth0 IP, which
    matches the postgres image's `host all all all scram-sha-256` pg_hba rule
    (loopback 127.0.0.1 is `trust`, ignoring the password). Used only for the
    access_denied fixture so a WRONG password genuinely fails auth."""

    return subprocess.run(
        ["docker", "exec", container, "hostname"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _spawn_long_query(container: str) -> subprocess.Popen[bytes]:
    """Open a long-lived `SELECT pg_sleep(...)` connection (detached) via local
    socket (trust auth as the postgres OS user — no password needed). It becomes
    an `active` backend that `pg_stat_activity` reports with a growing duration."""

    return subprocess.Popen(
        [
            "docker",
            "exec",
            container,
            "psql",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-c",
            "SELECT pg_sleep(300)",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def _main() -> None:
    sleeper: subprocess.Popen[bytes] | None = None
    try:
        compose_up("postgres")
        wait_healthy("postgres")
        container = container_name("postgres")

        # healthy: no external long query → count=0 after self-exclusion → ok.
        text = await _record(
            "healthy.json", container=container, pw=ROOT_PW, parameters={"user": "postgres"}
        )
        payload = json.loads(_main_command(text)["stdout"])
        assert payload["long_query_count"] == 0, payload

        # access_denied / conn_refused (record BEFORE the long sleep wait so they
        # are quick and unaffected by the held connection).
        hostname = _container_hostname(container)
        text = await _record(
            "access_denied.json",
            container=container,
            pw=_WRONG_PW,
            parameters={"user": "postgres", "host": hostname},
            allow_failed=True,
        )
        assert _main_command(text)["exit_code"] != 0, "expected non-zero (auth failure)"
        text = await _record(
            "conn_refused.json",
            container=container,
            pw=ROOT_PW,
            parameters={"user": "postgres", "port": 15999},
            allow_failed=True,
        )
        assert _main_command(text)["exit_code"] != 0, "expected non-zero (conn refused)"

        # semantic-abnormal: start a real sustained query and let it run PAST the
        # DEFAULT threshold_seconds before sampling.
        manifest = load_manifest(MANIFEST)
        threshold = int(manifest.parameters["properties"]["threshold_seconds"]["default"])
        sleeper = _spawn_long_query(container)
        wait_s = threshold + 8  # margin so now()-query_start > threshold at sample time
        print(f"holding a long query active for ~{wait_s}s (default threshold={threshold}s)...")
        time.sleep(wait_s)
        text = await _record(
            "semantic_abnormal.json",
            container=container,
            pw=ROOT_PW,
            parameters={"user": "postgres"},
        )
        payload = json.loads(_main_command(text)["stdout"])
        assert payload["long_query_count"] >= 1, payload
        assert payload["max_duration_seconds"] >= threshold, payload
    finally:
        if sleeper is not None:
            sleeper.terminate()
        compose_down("postgres")
    print("postgres.long_queries fixtures recorded.")


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))  # type: ignore[func-returns-value]
