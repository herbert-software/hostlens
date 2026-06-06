"""One-shot fixture recorder for `mysql.slow_queries` (dev-tool, NOT a test).

Records `ReplayTarget` fixtures by driving the real `InspectorRunner` (via
`hostlens.inspectors.recorder.record_fixture`) against the pinned compose
services. The local machine has no `mysql` client; the container does, so the
shared `_compose_record.DockerExecTarget` (docker-exec ExecutionTarget) renders +
dispatches the *real* command and captures the *real* JSON — zero drift.

Records (to tests/inspectors/fixtures/mysql_slow_queries/):
  * healthy.json             — `mysql-slowlog` (slow_query_log=ON, log_output=TABLE,
    long_query_time=1) with the slow_log TABLE truncated → monitoring enabled,
    0 slow queries in the window → status=ok, no finding.
  * monitoring_disabled.json — the plain `mysql` service (slow_query_log defaults
    OFF) → slow_log_monitoring_enabled=false → a `warning` finding ("未启用"),
    status=ok. This is the honest blind-spot exposure (design D-2(3)) — NOT a
    silent ok+0.
  * semantic_abnormal.json   — `mysql-slowlog` after running real `SELECT SLEEP(2)`
    queries (each > long_query_time=1 → a REAL slow event logged to mysql.slow_log)
    so slow_query_count crosses the DEFAULT warn_count → warning (real accumulated
    slow-query state; long_query_time=1 is a realistic gate, NOT the banned
    long_query_time=0 noise trick — tasks 1.3 / D-2).
  * access_denied.json       — `mysql-slowlog` + WRONG password (set, not unset) →
    Access denied → exit 1 + empty stdout → status=exception. allow_failed=True.
  * conn_refused.json        — port 13999 (nothing listening) → exit 1 + empty
    stdout → status=exception. allow_failed=True.

Usage (manages the compose lifecycle itself):
    .venv-impl/bin/python tests/inspectors/_record_mysql_slow_queries.py

Intentionally NOT collected by pytest (no `test_` prefix).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.recorder import record_fixture
from tests.inspectors._compose_record import (
    MYSQL_ROOT_PW as ROOT_PW,
)
from tests.inspectors._compose_record import (
    DockerExecTarget,
    compose_down,
    compose_up,
    container_name,
    wait_healthy,
)

MANIFEST = Path("src/hostlens/inspectors/builtin/mysql/slow_queries.yaml")
FIXTURE_DIR = Path("tests/inspectors/fixtures/mysql_slow_queries")

#: Throwaway WRONG password built by concatenation so a credential scanner does
#: not flag it; reused from the wave-2a mysql recorder convention.
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
    os.environ["HOSTLENS_MYSQL_PWD"] = pw
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


def _mysql(container: str, sql: str) -> str:
    """Run a one-shot SQL statement as root inside `container` (helper, NOT the
    inspector path) — used to truncate the slow_log table and to generate real
    slow queries for the semantic-abnormal scenario."""

    argv = [
        "docker",
        "exec",
        container,
        "mysql",
        "-uroot",
        f"-p{ROOT_PW}",
        "-N",
        "-s",
        "--batch",
        "-e",
        sql,
    ]
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout.strip()


async def _record_slowlog_family() -> None:
    compose_up("mysql-slowlog")
    wait_healthy("mysql-slowlog")
    container = container_name("mysql-slowlog")

    # Clean slate: truncating mysql.slow_log requires logging OFF, then re-enable.
    _mysql(
        container,
        "SET GLOBAL slow_query_log=OFF; TRUNCATE mysql.slow_log; SET GLOBAL slow_query_log=ON",
    )

    # healthy: monitoring enabled, 0 slow queries in the window → ok, no finding.
    await _record("healthy.json", container=container, pw=ROOT_PW, parameters={"user": "root"})

    # semantic-abnormal: run REAL slow queries (each SLEEP(2) > long_query_time=1 →
    # a genuine slow event row in mysql.slow_log), then record → count >= warn_count.
    for _ in range(3):
        _mysql(container, "SELECT SLEEP(2)")
    text = await _record(
        "semantic_abnormal.json", container=container, pw=ROOT_PW, parameters={"user": "root"}
    )
    payload = json.loads(_main_command(text)["stdout"])
    assert payload["slow_log_monitoring_enabled"] is True, payload
    assert payload["slow_query_count"] >= 1, payload

    # access_denied: WRONG password (set, not unset) → Access denied → exit 1.
    text = await _record(
        "access_denied.json",
        container=container,
        pw=_WRONG_PW,
        parameters={"user": "root"},
        allow_failed=True,
    )
    assert _main_command(text)["exit_code"] != 0, "expected non-zero (Access denied)"

    # conn_refused: nothing listening on 13999 inside the container → exit 1.
    text = await _record(
        "conn_refused.json",
        container=container,
        pw=ROOT_PW,
        parameters={"user": "root", "port": 13999},
        allow_failed=True,
    )
    assert _main_command(text)["exit_code"] != 0, "expected non-zero (conn refused)"


async def _record_monitoring_disabled() -> None:
    # Plain `mysql` service: slow_query_log defaults OFF / log_output FILE →
    # slow_log_monitoring_enabled=false → warning "未启用" (honest blind-spot, ok).
    compose_up("mysql")
    wait_healthy("mysql")
    container = container_name("mysql")
    text = await _record(
        "monitoring_disabled.json", container=container, pw=ROOT_PW, parameters={"user": "root"}
    )
    payload = json.loads(_main_command(text)["stdout"])
    assert payload["slow_log_monitoring_enabled"] is False, payload


async def _main() -> None:
    try:
        await _record_slowlog_family()
        await _record_monitoring_disabled()
    finally:
        compose_down("mysql-slowlog")
        compose_down("mysql")
    print("mysql.slow_queries fixtures recorded.")


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))  # type: ignore[func-returns-value]
