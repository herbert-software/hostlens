"""One-shot fixture recorder for `redis.replication_lag` (dev-tool, NOT a test).

Records `ReplayTarget` fixtures for the replication-inspector-contract probe by
driving the real `InspectorRunner` (via `record_fixture`) against a live
redis-repl-master + redis-repl-replica topology from the pinned compose file.
Unlike the single-instance recorders, BOTH services come up in ONE shared compose
project so the replica's `--replicaof redis-repl-master 6379` resolves on a shared
network (per-service projects would isolate them and break replication).

Readiness is ALWAYS polled (compose healthcheck via `_wait_health`, and the
replica's `master_link_status` / `master_last_io_seconds_ago` via `wait_until`) —
never a fixed `sleep` (design D-5).

Usage (manages the compose lifecycle itself):

    python tests/inspectors/_record_redis_replication_lag.py

Records (into tests/inspectors/fixtures/redis_replication_lag/) — 5 fixtures:
  * healthy.json        — replica, link up, fresh IO (master_last_io_seconds_ago
    well under the 15s default warn) → no finding (status=ok). No auth.
  * finding_trigger.json — healthy replica recorded with LOWERED warn_seconds=0
    (critical kept high) so the wiring fires a *warning* at a freshness that is
    healthy under the defaults. Validates finding wiring ONLY (not semantic). No auth.
  * link_down.json      — semantic-abnormal #1: the master container is STOPPED
    (TCP teardown, link flips to `down` in seconds — NOT dependent on repl-timeout),
    poll the replica until master_link_status==down, freeze. link_healthy=false →
    critical at DEFAULT thresholds. Recorded WITH a special-char password (redaction).
  * link_stale.json     — semantic-abnormal #2, SEMANTICALLY DISTINCT from link_down:
    `DEBUG SLEEP ~35` freezes the master event loop (35s < repl-timeout 3600s, so the
    link stays `up`), poll the replica until master_link_status==up AND
    master_last_io_seconds_ago>=30, freeze. link_healthy=true but lag_seconds>=30 →
    critical at DEFAULT thresholds (the freshness/lag path). Recorded WITH the
    special-char password (redaction).
  * conn_refused.json   — fail-loud: redis-cli points at a closed port (6390) → the
    collector exits non-zero with empty stdout → status=exception. No auth.

The two semantic-abnormal fixtures are recorded with a space+glob-metachar password
(`p w*d`) injected as HOSTLENS_REDIS_PASSWORD; the recorder redacts every injected
secret value before writing, so the committed fixtures never carry the plaintext.

This module is intentionally NOT collected by pytest (no `test_` prefix).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Final

from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.recorder import record_fixture
from tests.inspectors._compose_record import (
    COMPOSE_FILE,
    MYSQL_ROOT_PW,
    POSTGRES_ROOT_PW,
    DockerExecTarget,
    wait_until,
)

MANIFEST = Path("src/hostlens/inspectors/builtin/redis/replication_lag.yaml")
FIXTURE_DIR = Path("tests/inspectors/fixtures/redis_replication_lag")

#: Dedicated SHARED compose project so master + replica land on one network and
#: the replica's `--replicaof redis-repl-master` resolves. (The per-service
#: `compose_up` helper isolates each service in its own project/network, which
#: would break replication — hence this recorder's own bring-up.)
PROJECT: Final = "hostlens-rec-repl"
MASTER: Final = "redis-repl-master"
REPLICA: Final = "redis-repl-replica"

#: Password with a space AND a glob metachar — the word-split / unquoted-`-a`
#: redaction payload. Injected for the link_down / link_stale fixtures so the
#: redaction guard (task 3.3) is non-vacuous.
SPECIAL_PW: Final = "p w*d"


def _compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    argv = ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE_FILE), *args]
    return subprocess.run(
        argv,
        check=check,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOSTLENS_MYSQL_ROOT_PW": MYSQL_ROOT_PW,
            "HOSTLENS_PG_ROOT_PW": POSTGRES_ROOT_PW,
        },
    )


def _cname(service: str) -> str:
    return f"{PROJECT}-{service}-1"


def _up() -> None:
    _compose("up", "-d", MASTER, REPLICA)


def _down() -> None:
    _compose("down", "-v", check=False)


def _exec(service: str, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "exec", _cname(service), *argv],
        capture_output=True,
        text=True,
    )


def _wait_health(service: str, *, attempts: int = 120) -> None:
    name = _cname(service)
    for _ in range(attempts):
        proc = subprocess.run(
            ["docker", "inspect", "-f", "{{json .State.Health}}", name],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0 and proc.stdout.strip() not in ("", "null"):
            status = json.loads(proc.stdout).get("Status")
            if status == "healthy":
                return
            if status == "unhealthy":
                raise RuntimeError(f"service {service!r} reported unhealthy")
        time.sleep(1.0)
    raise RuntimeError(f"service {service!r} did not become healthy in time")


def _replica_field(field: str, *, auth: str | None = None) -> str | None:
    """Read one `INFO replication` field from the replica (None if absent)."""

    argv = ["redis-cli"]
    if auth is not None:
        argv += ["-a", auth, "--no-auth-warning"]
    argv += ["INFO", "replication"]
    out = _exec(REPLICA, *argv).stdout
    for line in out.splitlines():
        line = line.strip().replace("\r", "")
        if line.startswith(f"{field}:"):
            return line.split(":", 1)[1]
    return None


async def _record(
    out_name: str,
    *,
    parameters: dict[str, Any] | None = None,
    allow_failed: bool = False,
) -> str:
    manifest = load_manifest(MANIFEST)
    target = DockerExecTarget("recorder", _cname(REPLICA))
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


async def _record_noauth_family() -> None:
    # No-auth instances: export the declared secret as EMPTY so preflight's
    # secret-presence gate passes and the collector takes its no-auth branch.
    os.environ["HOSTLENS_REDIS_PASSWORD"] = ""

    # healthy: link up, fresh IO (<15s default warn) → no finding.
    wait_until(lambda: _replica_field("master_link_status") == "up", timeout=60.0)
    text = await _record("healthy.json")
    out = json.loads(json.loads(text)["commands"][-1]["stdout"])
    assert out["replication_configured"] is True and out["link_healthy"] is True, out
    assert out["lag_seconds"] is not None and out["lag_seconds"] < 15, out
    print(f"healthy: lag_seconds={out['lag_seconds']}")

    # finding-trigger: healthy replica + lowered warn_seconds=0 (critical high) →
    # a warning at a freshness that is healthy under the defaults (wiring only).
    await _record("finding_trigger.json", parameters={"warn_seconds": 0, "critical_seconds": 999})

    # conn_refused (fail-loud): closed port → non-zero exit + empty stdout → exception.
    text = await _record("conn_refused.json", parameters={"port": 6390}, allow_failed=True)
    main = json.loads(text)["commands"][-1]
    assert main["exit_code"] != 0, "conn_refused main command must have non-zero exit_code"
    print("conn_refused fixture has non-zero main-command exit")


async def _record_auth_family() -> None:
    # Set replication auth so the link_down / link_stale fixtures carry a secret
    # (redaction proof, task 3.3). Order: masterauth on the replica FIRST so it can
    # re-auth once the master starts requiring a password, then requirepass on both.
    assert _exec(REPLICA, "redis-cli", "CONFIG", "SET", "masterauth", SPECIAL_PW).returncode == 0
    assert _exec(MASTER, "redis-cli", "CONFIG", "SET", "requirepass", SPECIAL_PW).returncode == 0
    assert _exec(REPLICA, "redis-cli", "CONFIG", "SET", "requirepass", SPECIAL_PW).returncode == 0
    os.environ["HOSTLENS_REDIS_PASSWORD"] = SPECIAL_PW

    # Wait for the (now-authenticated) replication link to be re-established.
    wait_until(lambda: _replica_field("master_link_status", auth=SPECIAL_PW) == "up", timeout=60.0)

    # --- link_stale (semantic-abnormal #2): freeze the master event loop with an
    # ASYNC DEBUG SLEEP (35s < repl-timeout 3600s → link stays up), poll the replica
    # until master_link_status==up AND master_last_io_seconds_ago>=30, then record.
    sleeper = subprocess.Popen(
        [
            "docker",
            "exec",
            _cname(MASTER),
            "redis-cli",
            "-a",
            SPECIAL_PW,
            "--no-auth-warning",
            "DEBUG",
            "SLEEP",
            "40",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:

        def _stale() -> bool:
            status = _replica_field("master_link_status", auth=SPECIAL_PW)
            last = _replica_field("master_last_io_seconds_ago", auth=SPECIAL_PW)
            return status == "up" and last is not None and last.isdigit() and int(last) >= 30

        wait_until(_stale, timeout=120.0)
        text = await _record("link_stale.json")
        out = json.loads(json.loads(text)["commands"][-1]["stdout"])
        assert out["link_healthy"] is True, out
        assert out["lag_seconds"] is not None and out["lag_seconds"] >= 30, out
        assert SPECIAL_PW not in text, "special password leaked into link_stale fixture"
        print(
            f"link_stale: lag_seconds={out['lag_seconds']}, link up (semantic-distinct from down)"
        )
    finally:
        sleeper.wait()

    # Let the master resume and the link settle back to up before the down test.
    wait_until(lambda: _replica_field("master_link_status", auth=SPECIAL_PW) == "up", timeout=120.0)

    # --- link_down (semantic-abnormal #1): STOP the master container (TCP teardown
    # → link flips to down in seconds, NOT dependent on repl-timeout), poll the
    # replica until master_link_status==down, then record.
    _compose("stop", MASTER)
    wait_until(
        lambda: _replica_field("master_link_status", auth=SPECIAL_PW) == "down", timeout=120.0
    )
    text = await _record("link_down.json")
    out = json.loads(json.loads(text)["commands"][-1]["stdout"])
    assert out["replication_configured"] is True and out["link_healthy"] is False, out
    assert SPECIAL_PW not in text, "special password leaked into link_down fixture"
    print("link_down: link down (semantic-distinct from stale)")


async def _main() -> None:
    _down()  # clean any stale project
    try:
        _up()
        _wait_health(MASTER)
        _wait_health(REPLICA)
        await _record_noauth_family()
        await _record_auth_family()
    finally:
        _down()


if __name__ == "__main__":
    asyncio.run(_main())
