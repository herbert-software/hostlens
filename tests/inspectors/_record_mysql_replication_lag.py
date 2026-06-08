"""One-shot fixture recorder for `mysql.replication_lag` (dev-tool, NOT a test).

Records `ReplayTarget` fixtures for the replication-inspector-contract probe by
driving the real `InspectorRunner` (via `record_fixture`) against a live
mysql-repl-primary + mysql-repl-replica topology from the pinned compose file.
Unlike the single-instance recorders, BOTH services come up in ONE shared compose
project so the replica's `CHANGE REPLICATION SOURCE TO SOURCE_HOST=mysql-repl-primary`
resolves on a shared network (per-service projects would isolate them and break
replication).

Readiness is ALWAYS polled (compose healthcheck via `_wait_health`, and the
replica's `Replica_IO_Running` / `Replica_SQL_Running` / `Seconds_Behind_Source`
via `wait_until`) — never a fixed `sleep` (design W-4 / D-5).

Usage (manages the compose lifecycle itself):

    python tests/inspectors/_record_mysql_replication_lag.py

Re-record steps:
  1. Ensure Docker is running and ports 13310/13311 are free.
  2. Run this script from the repo root (it tears down any stale project first).
  3. The script: brings up primary+replica → polls health → bootstraps replication
     (repl + mon accounts, CHANGE REPLICATION SOURCE TO, START REPLICA) → polls
     until IO/SQL Running=Yes → records all 5 fixtures → tears down.
  4. If `lagging.json` fails to latch (SBS never reaches 30), tune the backlog
     volume in `_generate_replication_backlog()` and re-run.

Records (into tests/inspectors/fixtures/mysql_replication_lag/) — 5 fixtures:
  * healthy.json        — replica, link up, small apply lag (<15s default warn) →
    no finding (status=ok). Connects as `mon` via HOSTLENS_MYSQL_PWD.
  * finding_trigger.json — healthy replica recorded with LOWERED warn_seconds=0
    (critical kept high) so the wiring fires a *warning* at a lag that is healthy
    under the defaults. Validates finding wiring ONLY (not semantic).
  * link_down.json      — semantic-abnormal #1: `STOP REPLICA IO_THREAD` on the
    replica, poll until Replica_IO_Running=No, freeze. link_healthy=false →
    critical at DEFAULT thresholds. Recorded WITH the special-char password
    (redaction). Restores IO thread before the next fixture.
  * lagging.json        — semantic-abnormal #2, SEMANTICALLY DISTINCT from link_down
    (design W-4 ONLY viable recipe): STOP REPLICA SQL_THREAD → primary writes a
    LARGE backlog → START REPLICA SQL_THREAD → poll during catch-up until
    Seconds_Behind_Source>=30 (non-NULL) AND IO/SQL Running=Yes, freeze.
    link_healthy=true but lag_seconds>=30 → critical at DEFAULT thresholds.
    NEVER poll SBS while SQL_THREAD is stopped (SBS is NULL when applier is off).
    Recorded WITH the special-char password (redaction).
  * conn_refused.json   — fail-loud: mysql points at a closed port (13399) → the
    collector exits non-zero with empty stdout → status=exception.

The two semantic-abnormal fixtures are recorded with a space+glob-metachar password
(`p w*d`) injected as HOSTLENS_MYSQL_PWD; the recorder redacts every injected
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

MANIFEST = Path("src/hostlens/inspectors/builtin/mysql/replication_lag.yaml")
FIXTURE_DIR = Path("tests/inspectors/fixtures/mysql_replication_lag")

#: Dedicated SHARED compose project so primary + replica land on one network and
#: the replica's `SOURCE_HOST=mysql-repl-primary` resolves. (The per-service
#: `compose_up` helper isolates each service in its own project/network, which
#: would break replication — hence this recorder's own bring-up.)
PROJECT: Final = "hostlens-rec-mysql-repl"
PRIMARY: Final = "mysql-repl-primary"
REPLICA: Final = "mysql-repl-replica"

#: Password with a space AND a glob metachar — the redaction payload for
#: link_down / lagging fixtures (task 3.3).
SPECIAL_PW: Final = "p w*d"

#: Inspector connects as this monitoring user (REPLICATION CLIENT only).
MON_USER: Final = "mon"


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
    _compose("up", "-d", PRIMARY, REPLICA)


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


def _replica_status(field: str) -> str | None:
    """Read one `SHOW REPLICA STATUS` field from the replica (None if absent)."""

    # `-s` (silent) keeps the vertical `\G` field NAMES; `-N`/--skip-column-names
    # would strip them (printing values only), so it is deliberately NOT used here.
    out = _exec(
        REPLICA,
        "mysql",
        "-uroot",
        f"-p{MYSQL_ROOT_PW}",
        "-s",
        "-e",
        "SHOW REPLICA STATUS\\G",
    ).stdout
    for line in out.splitlines():
        line = line.strip()
        if line.startswith(f"{field}:"):
            return line.split(":", 1)[1].strip()
    return None


def _mysql_primary(sql: str) -> subprocess.CompletedProcess[str]:
    return _exec(PRIMARY, "mysql", "-uroot", f"-p{MYSQL_ROOT_PW}", "-e", sql)


def _mysql_replica(sql: str) -> subprocess.CompletedProcess[str]:
    return _exec(REPLICA, "mysql", "-uroot", f"-p{MYSQL_ROOT_PW}", "-e", sql)


def _replication_link_up() -> bool:
    return (
        _replica_status("Replica_IO_Running") == "Yes"
        and _replica_status("Replica_SQL_Running") == "Yes"
    )


def _seconds_behind_source() -> int | None:
    val = _replica_status("Seconds_Behind_Source")
    if val is None or val == "NULL":
        return None
    return int(val)


def _mon_user_replicated() -> bool:
    """True once the primary's ``CREATE USER 'mon'`` has been APPLIED on the replica.

    ``_replication_link_up`` only confirms the IO/SQL threads are RUNNING, not that
    they have caught up — the inspector account is created on the primary and rides
    GTID replication, so the collector (which connects AS ``mon``) gets Access-denied
    until this returns True. A plain tabular ``COUNT(*)`` is read with ``-N -s``.
    """

    out = _exec(
        REPLICA,
        "mysql",
        "-uroot",
        f"-p{MYSQL_ROOT_PW}",
        "-N",
        "-s",
        "-e",
        f"SELECT COUNT(*) FROM mysql.user WHERE user='{MON_USER}'",
    ).stdout
    return out.strip() == "1"


def _bootstrap() -> None:
    """Create replication + monitoring accounts and establish GTID replication."""

    pw = SPECIAL_PW.replace("'", "''")
    # Create BOTH accounts on the PRIMARY only. With GTID auto-position the replica
    # fetches and applies these CREATE USER transactions, so `mon` exists on the
    # replica too. Creating `mon` locally on the replica as well would mint a second
    # CREATE USER under the replica's own server-uuid (an errant transaction); the
    # replica would then hit a duplicate-user error applying the primary's copy and
    # the SQL thread would stop — breaking replication.
    _mysql_primary(
        f"CREATE USER 'repl'@'%' IDENTIFIED WITH mysql_native_password BY '{pw}';"
        f"GRANT REPLICATION SLAVE ON *.* TO 'repl'@'%';"
        f"CREATE USER '{MON_USER}'@'%' IDENTIFIED WITH mysql_native_password BY '{pw}';"
        f"GRANT REPLICATION CLIENT ON *.* TO '{MON_USER}'@'%';"
    )
    _mysql_replica(
        "CHANGE REPLICATION SOURCE TO "
        f"SOURCE_HOST='{PRIMARY}', SOURCE_PORT=3306, "
        f"SOURCE_USER='repl', SOURCE_PASSWORD='{pw}', "
        "SOURCE_AUTO_POSITION=1, GET_SOURCE_PUBLIC_KEY=1;"
        "START REPLICA;"
    )
    wait_until(_replication_link_up, timeout=120.0)
    # Threads are running; also wait until the `mon` account has actually replicated
    # to the replica, else the collector (connecting AS mon) races Access-denied.
    wait_until(_mon_user_replicated, timeout=60.0)
    sbs = _replica_status("Seconds_Behind_Source")
    if sbs is None:
        raise RuntimeError("Seconds_Behind_Source not readable after bootstrap")
    print(f"bootstrap ok: Seconds_Behind_Source={sbs}")


#: Number of `INSERT ... SELECT` doublings (2**N rows). Empirically calibrated on
#: the recording lane: 2**21 (~2.1M CHAR(255) rows) makes the relay-log build +
#: single-threaded apply span well over the 30s critical threshold, so
#: Seconds_Behind_Source latches >= 30 for ~20s of catch-up (many poll windows).
#: 2**19 catches up in seconds (SBS never reaches 30) — do NOT shrink this.
_BACKLOG_DOUBLINGS: Final = 21


def _generate_replication_backlog() -> None:
    """Write a large transaction backlog on the primary so the replica's apply lag
    crosses the 30s critical threshold during catch-up (design W-4, volume-based —
    NOT a fixed sleep). The table is qualified with the `hostlens` DB because these
    `mysql -e` calls connect with no default database selected."""

    _mysql_primary(
        "CREATE TABLE IF NOT EXISTS hostlens.t("
        "id INT AUTO_INCREMENT PRIMARY KEY, pad CHAR(255)"
        "); TRUNCATE TABLE hostlens.t;"
    )
    _mysql_primary("INSERT INTO hostlens.t(pad) VALUES (REPEAT('x', 255))")
    for _ in range(_BACKLOG_DOUBLINGS):
        _mysql_primary("INSERT INTO hostlens.t(pad) SELECT REPEAT('x', 255) FROM hostlens.t")


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


def _parse_triple(text: str) -> dict[str, Any]:
    return json.loads(json.loads(text)["commands"][-1]["stdout"])


async def _record_fixtures() -> None:
    os.environ["HOSTLENS_MYSQL_PWD"] = SPECIAL_PW
    params = {"user": MON_USER}

    # healthy: link up, small lag (<15s default warn) → no finding.
    wait_until(_replication_link_up, timeout=60.0)
    text = await _record("healthy.json", parameters=params)
    out = _parse_triple(text)
    assert out["replication_configured"] is True and out["link_healthy"] is True, out
    assert out["lag_seconds"] is not None and out["lag_seconds"] < 15, out
    print(f"healthy: lag_seconds={out['lag_seconds']}")

    # finding_trigger: healthy replica + lowered warn_seconds=0 (critical high).
    await _record(
        "finding_trigger.json",
        parameters={"user": MON_USER, "warn_seconds": 0, "critical_seconds": 999},
    )

    # conn_refused (fail-loud): closed port → non-zero exit + empty stdout → exception.
    text = await _record(
        "conn_refused.json",
        parameters={"user": MON_USER, "port": 13399},
        allow_failed=True,
    )
    main = json.loads(text)["commands"][-1]
    assert main["exit_code"] != 0, "conn_refused main command must have non-zero exit_code"
    print("conn_refused fixture has non-zero main-command exit")

    # --- link_down (semantic-abnormal #1): STOP IO thread, poll until IO=No, record.
    _mysql_replica("STOP REPLICA IO_THREAD;")
    wait_until(lambda: _replica_status("Replica_IO_Running") == "No", timeout=120.0)
    text = await _record("link_down.json", parameters=params)
    out = _parse_triple(text)
    assert out["replication_configured"] is True and out["link_healthy"] is False, out
    assert SPECIAL_PW not in text, "special password leaked into link_down fixture"
    print("link_down: link down (semantic-distinct from lagging)")
    _mysql_replica("START REPLICA IO_THREAD;")
    wait_until(_replication_link_up, timeout=120.0)

    # --- lagging (semantic-abnormal #2, W-4 recipe): STOP SQL → backlog → START SQL
    # → poll during catch-up until SBS>=30 with IO/SQL Running=Yes, record immediately.
    _mysql_replica("STOP REPLICA SQL_THREAD;")
    _generate_replication_backlog()
    _mysql_replica("START REPLICA SQL_THREAD;")

    def _lagging_ready() -> bool:
        if not _replication_link_up():
            return False
        sbs = _seconds_behind_source()
        return sbs is not None and sbs >= 30

    wait_until(_lagging_ready, timeout=180.0)
    text = await _record("lagging.json", parameters=params)
    out = _parse_triple(text)
    assert out["link_healthy"] is True, out
    assert out["lag_seconds"] is not None and out["lag_seconds"] >= 30, out
    assert SPECIAL_PW not in text, "special password leaked into lagging fixture"
    print(f"lagging: lag_seconds={out['lag_seconds']}, link up (semantic-distinct from down)")


async def _main() -> None:
    _down()  # clean any stale project
    try:
        _up()
        _wait_health(PRIMARY)
        _wait_health(REPLICA)
        _bootstrap()
        await _record_fixtures()
    finally:
        _down()


if __name__ == "__main__":
    asyncio.run(_main())
