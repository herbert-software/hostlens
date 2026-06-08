"""One-shot fixture recorder for `postgres.replication_lag` (dev-tool, NOT a test).

Records `ReplayTarget` fixtures for the replication-inspector-contract probe by
driving the real `InspectorRunner` (via `record_fixture`) against a live
pg-repl-primary + 3 standby topology from the pinned compose file. Unlike the
single-instance recorders, ALL services come up in ONE shared compose project so
the standbys' `pg_basebackup -h pg-repl-primary` resolves on a shared network
(per-service projects would isolate them and break replication).

This is the PRIMARY-SIDE recorder (postgres apply-lag is read from the primary's
`pg_stat_replication`, one row per online standby), unlike redis/mysql which read
the replica side. The collector runs INSIDE the primary container (host=127.0.0.1)
and reduces the multi-row view via a SINGLE SQL aggregate.

Readiness is ALWAYS polled (`wait_until` on streaming count / replay_lag / state)
— never a fixed `sleep` (design W3-5). Gate-probed recipes (design 未决问题):
  * lagging:  ALTER SYSTEM SET recovery_min_apply_delay on a standby + primary
    write loop -> state stays 'streaming', replay_lag grows. Poll replay_lag>=30.
  * non-streaming row (link_down / multi_replica): a THROTTLED `pg_basebackup
    --max-rate` shows a HOLDABLE state='backup' walsender in pg_stat_replication.
    (`catchup` ships sub-poll-interval on fast local loopback -> too transient to
    latch reliably; `backup` is the controllable non-streaming state, same
    accepted-false-positive set, satisfies state != 'streaming'.)
  * underprivileged: a CONNECT-only role sees pg_stat_replication rows but
    state/replay_lag columns masked to NULL -> bool_and(coalesce(state,''))=false
    -> link_healthy=false -> critical (loud, not silent false-healthy).

Usage (manages the compose lifecycle itself):

    python tests/inspectors/_record_postgres_replication_lag.py

Records into tests/inspectors/fixtures/postgres_replication_lag/ — fixtures:
  * idle.json              — streaming + idle primary -> replay_lag NULL ->
    lag_seconds=null, link_healthy=true, no finding (status=ok).
  * underprivileged_all.json — CONNECT-only role; all state cols NULL ->
    coalesce->false -> link_healthy=false -> critical. Proves coalesce neutralises
    bool_and's NULL-ignore (L1). Recorded WITH the special-char password.
  * healthy.json           — streaming, small replay_lag (<15 default warn) ->
    no finding.
  * finding_trigger.json   — healthy topology + LOWERED warn_seconds=0 -> warning
    at a lag that is healthy under defaults. Wiring only.
  * lagging.json           — semantic-abnormal #2: recovery_min_apply_delay +
    write loop -> replay_lag>=30 while state='streaming' -> link_healthy=true,
    critical. Recorded WITH the special-char password.
  * multi_replica.json     — 3-row single carrier: 2 distinct non-NULL streaming
    (small + large via apply_delay) + 1 backup walsender. Reduction verified at RECORD
    TIME by a single-snapshot CTE recompute (max/AND over raw rows == aggregate);
    replay does NOT re-run the SQL.
  * link_down.json         — semantic-abnormal #1: a present row in a non-streaming
    state ('backup' via throttled pg_basebackup) -> link_healthy=false -> critical
    "link down". Recorded WITH the special-char password.
  * unconfigured.json      — all standbys disconnected -> empty pg_stat_replication
    -> (false,false,null), status=ok, no finding (empty-set guard W3-3/W3-4).
  * conn_refused.json      — psql at a closed port -> non-zero exit + empty stdout
    -> status=exception.

The fixtures recorded WITH a space+glob-metachar password (`p w*d`) injected as
HOSTLENS_POSTGRES_PASSWORD exercise redaction; the recorder asserts the plaintext
never lands in the committed fixture.

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
from tests.inspectors._compose_record import DockerExecTarget, wait_until

MANIFEST = Path("src/hostlens/inspectors/builtin/postgres/replication_lag.yaml")
FIXTURE_DIR = Path("tests/inspectors/fixtures/postgres_replication_lag")
COMPOSE_FILE = Path("tests/inspectors/compose/docker-compose.yml")

#: Dedicated SHARED compose project so primary + standbys land on one network.
PROJECT: Final = "hostlens-pgrepl-rec"
PRIMARY: Final = "pg-repl-primary"
STANDBYS: Final = ["pg-repl-standby", "pg-repl-standby-2", "pg-repl-standby-3"]
NETWORK: Final = f"{PROJECT}_default"

PG_ROOT_PW: Final = "pgrec_rootpw_unused_trust"
MYSQL_ROOT_PW: Final = "unused_but_compose_interpolates_it"
#: Password with a space AND a glob metachar — the redaction payload.
SPECIAL_PW: Final = "p w*d"
LOWPRIV_USER: Final = "lowmon"

DEFAULT_WARN: Final = 15
DEFAULT_CRIT: Final = 30


# --------------------------------------------------------------------------- #
# compose / docker helpers
# --------------------------------------------------------------------------- #
def _compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    argv = ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE_FILE), *args]
    return subprocess.run(
        argv,
        check=check,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOSTLENS_PG_ROOT_PW": PG_ROOT_PW,
            "HOSTLENS_MYSQL_ROOT_PW": MYSQL_ROOT_PW,
        },
    )


def _cname(service: str) -> str:
    return f"{PROJECT}-{service}-1"


def _up() -> None:
    _compose("up", "-d", PRIMARY, *STANDBYS)


def _down() -> None:
    _compose("down", "-v", check=False)


def _exec(container: str, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "exec", container, *argv],
        capture_output=True,
        text=True,
    )


def _psql(service: str, sql: str, *, user: str = "postgres", db: str = "postgres") -> str:
    proc = _exec(_cname(service), "psql", "-U", user, "-d", db, "-tAc", sql)
    return proc.stdout.strip()


def _wait_health(service: str, *, attempts: int = 120) -> None:
    name = _cname(service)
    for _ in range(attempts):
        proc = subprocess.run(
            ["docker", "inspect", "-f", "{{json .State.Health}}", name],
            capture_output=True,
            text=True,
        )
        if '"healthy"' in proc.stdout:
            return
        time.sleep(1.0)
    raise RuntimeError(f"{name} did not become healthy")


def _net_disconnect(service: str) -> None:
    subprocess.run(["docker", "network", "disconnect", NETWORK, _cname(service)], check=False)


def _net_connect(service: str) -> None:
    subprocess.run(["docker", "network", "connect", NETWORK, _cname(service)], check=False)


# --------------------------------------------------------------------------- #
# topology state helpers (all read the primary's pg_stat_replication)
# --------------------------------------------------------------------------- #
def _streaming_count() -> int:
    out = _psql(PRIMARY, "SELECT count(*) FROM pg_stat_replication WHERE state='streaming'")
    return int(out) if out.isdigit() else -1


def _row_count() -> int:
    out = _psql(PRIMARY, "SELECT count(*) FROM pg_stat_replication")
    return int(out) if out.isdigit() else -1


def _states() -> str:
    return _psql(PRIMARY, "SELECT coalesce(string_agg(state,','),'') FROM pg_stat_replication")


def _max_replay_lag_s() -> float | None:
    out = _psql(
        PRIMARY,
        "SELECT coalesce(EXTRACT(EPOCH FROM max(replay_lag))::text,'') FROM pg_stat_replication",
    )
    return float(out) if out else None


def _set_apply_delay(service: str, value: str) -> None:
    _psql(service, f"ALTER SYSTEM SET recovery_min_apply_delay = '{value}'")
    _psql(service, "SELECT pg_reload_conf()")


def _load(rows: int) -> None:
    _psql(
        PRIMARY,
        f"INSERT INTO t(v) SELECT repeat('x',300) FROM generate_series(1,{rows})",
    )


def _start_backup_walsender() -> None:
    """Induce a HOLDABLE non-streaming walsender row: a THROTTLED `pg_basebackup`
    shows up in pg_stat_replication with state='backup' for the duration of the
    (rate-limited) base backup. Gate-probed: `catchup` ships sub-poll-interval on
    fast local loopback (too transient to latch); `backup` via --max-rate is the
    reliable controllable non-streaming state (same accepted-false-positive set,
    satisfies the contract's `state != 'streaming'` link_down assertion)."""
    _exec(_cname(PRIMARY), "bash", "-c", "rm -rf /tmp/bk")
    subprocess.run(
        [
            "docker",
            "exec",
            "-d",
            _cname(PRIMARY),
            "bash",
            "-c",
            "pg_basebackup -h 127.0.0.1 -U postgres --max-rate=256k -X none -D /tmp/bk -Fp",
        ],
        check=False,
    )


def _stop_backup_walsender() -> None:
    _exec(_cname(PRIMARY), "pkill", "-f", "pg_basebackup")
    _exec(_cname(PRIMARY), "bash", "-c", "rm -rf /tmp/bk")


def _setup_primary() -> None:
    # trust adds only 'host all all all trust' — replication needs an explicit line.
    _exec(
        _cname(PRIMARY),
        "bash",
        "-c",
        'grep -q "host replication all all trust" "$PGDATA/pg_hba.conf" '
        '|| echo "host replication all all trust" >> "$PGDATA/pg_hba.conf"',
    )
    _psql(PRIMARY, "SELECT pg_reload_conf()")
    _psql(PRIMARY, "CREATE TABLE IF NOT EXISTS t(id serial primary key, v text)")
    # CONNECT-only role (no pg_monitor) for the underprivileged fixture.
    _psql(PRIMARY, f"DROP ROLE IF EXISTS {LOWPRIV_USER}")
    _psql(PRIMARY, f"CREATE ROLE {LOWPRIV_USER} LOGIN PASSWORD 'x'")


# --------------------------------------------------------------------------- #
# record helper
# --------------------------------------------------------------------------- #
async def _record(
    out_name: str,
    *,
    user: str = "postgres",
    parameters: dict[str, Any] | None = None,
    allow_failed: bool = False,
) -> str:
    manifest = load_manifest(MANIFEST)
    target = DockerExecTarget("recorder", _cname(PRIMARY))
    params: dict[str, Any] = {"user": user}
    if parameters:
        params.update(parameters)
    fixture = await record_fixture(
        manifest,
        target,  # type: ignore[arg-type]
        settings=Settings(),
        parameters=params,
        allow_failed=allow_failed,
    )
    path = FIXTURE_DIR / out_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fixture.to_json())
    print(f"wrote {path}")
    return path.read_text()


def _triple(text: str) -> dict[str, Any]:
    return json.loads(json.loads(text)["commands"][-1]["stdout"])


async def _record_when_state(
    out_name: str,
    state_token: str,
    *,
    extra_check: Any = None,
    verify: bool = False,
    timeout: float = 60.0,
) -> str:
    """Poll until a row with `state_token` (and extra_check) is present, then
    verify (optionally) and record. `backup` is holdable (throttled basebackup),
    so wait_until-then-record is safe — but we keep it tight for symmetry."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = _states()
        if state_token in st and (extra_check is None or extra_check(st)):
            if verify:
                _verify_reduction_same_snapshot()
            return await _record(out_name)
        time.sleep(0.3)
    raise RuntimeError(f"state '{state_token}' not seen for {out_name}; states=[{_states()}]")


def _verify_reduction_same_snapshot() -> None:
    """Record-time reduction assertion (design W3-10.6): single MVCC-snapshot CTE
    returns raw rows + the aggregate; recompute max/AND from raw in Python and
    assert == the aggregate columns. Proves the collector's aggregate SQL logic
    on a CONSISTENT snapshot (NOT two independent round-trips, which race)."""
    sql = (
        "WITH r AS (SELECT state, replay_lag FROM pg_stat_replication) "
        "SELECT json_agg(json_build_object("
        "'state', state, "
        "'lag', FLOOR(EXTRACT(EPOCH FROM replay_lag))::bigint))::text, "
        "(SELECT count(*) FROM r), "
        "(SELECT bool_and(coalesce(state::text,'')='streaming') FROM r), "
        "(SELECT FLOOR(EXTRACT(EPOCH FROM max(replay_lag)))::bigint FROM r) "
        "FROM r"
    )
    # psql -tA default '|' sep could clash; use a tab field sep (json has no tabs).
    proc = _exec(
        _cname(PRIMARY), "psql", "-U", "postgres", "-d", "postgres", "-tA", "-F", "\t", "-c", sql
    )
    line = proc.stdout.strip()
    j_rows, cnt, agg_link, agg_lag = line.split("\t")
    rows = json.loads(j_rows)
    # also lock the count(*) term (the empty-set guard's pivot) on the same snapshot
    assert int(cnt) == len(rows), f"count mismatch: count(*)={cnt} json_agg rows={len(rows)}"
    py_link = all(r["state"] == "streaming" for r in rows)
    lags = [r["lag"] for r in rows if r["lag"] is not None]
    py_lag = max(lags) if lags else None
    sql_link = agg_link == "t"
    sql_lag = int(agg_lag) if agg_lag != "" else None
    assert py_link == sql_link, f"AND mismatch: py={py_link} sql={sql_link} rows={rows}"
    assert py_lag == sql_lag, f"max mismatch: py={py_lag} sql={sql_lag} rows={rows}"
    print(f"  reduction verified (same snapshot): rows={rows} -> link={sql_link} lag={sql_lag}")


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
async def _record_fixtures() -> None:
    os.environ["HOSTLENS_POSTGRES_PASSWORD"] = SPECIAL_PW

    # 0) all 3 standbys streaming
    wait_until(lambda: _streaming_count() == 3, timeout=120.0)
    print(f"3 standbys streaming; states=[{_states()}]")

    # 1) idle.json — no apply_delay, primary idle -> replay_lag NULL.
    for sb in STANDBYS:
        _set_apply_delay(sb, "0")
    # poll for the settled (NULL replay_lag) state — no fixed sleep (the reload +
    # idle catch-up is awaited by the condition poll, keeping the no-fixed-sleep claim true).
    wait_until(lambda: _max_replay_lag_s() is None, timeout=30.0)
    text = await _record("idle.json")
    out = _triple(text)
    assert out["replication_configured"] is True and out["link_healthy"] is True, out
    assert out["lag_seconds"] is None, out
    print(f"idle: {out}")

    # 2) underprivileged_all.json — CONNECT-only role; state cols NULL -> critical.
    text = await _record("underprivileged_all.json", user=LOWPRIV_USER)
    out = _triple(text)
    assert out["replication_configured"] is True and out["link_healthy"] is False, out
    assert SPECIAL_PW not in text, "special password leaked into underprivileged_all fixture"
    print(f"underprivileged_all: {out} (coalesce neutralised NULL state -> critical)")

    # 3) healthy.json — small non-NULL lag (<15) via apply_delay=5s + light writes.
    _set_apply_delay(STANDBYS[0], "5s")

    def _small_lag() -> bool:
        _load(2000)
        lag = _max_replay_lag_s()
        return lag is not None and 1.0 <= lag <= 14.0

    wait_until(_small_lag, timeout=60.0)
    text = await _record("healthy.json")
    out = _triple(text)
    assert out["link_healthy"] is True and out["lag_seconds"] is not None, out
    assert out["lag_seconds"] < DEFAULT_WARN, out
    print(f"healthy: {out}")

    # 4) finding_trigger.json — same topology, warn_seconds=0 -> warning.
    _load(2000)
    text = await _record(
        "finding_trigger.json", parameters={"warn_seconds": 0, "critical_seconds": 999}
    )
    print("finding_trigger recorded (warn_seconds=0)")

    # 5) lagging.json — apply_delay=40s + write loop -> replay_lag>=30, streaming.
    _set_apply_delay(STANDBYS[0], "40s")

    def _lag_high() -> bool:
        _load(4000)
        lag = _max_replay_lag_s()
        return lag is not None and lag >= DEFAULT_CRIT and _streaming_count() == 3

    wait_until(_lag_high, timeout=120.0)
    text = await _record("lagging.json")
    out = _triple(text)
    assert out["link_healthy"] is True, out
    assert out["lag_seconds"] is not None and out["lag_seconds"] >= DEFAULT_CRIT, out
    assert SPECIAL_PW not in text, "special password leaked into lagging fixture"
    print(f"lagging: {out}")

    # 6) multi_replica.json — 3-row carrier: sb0 small streaming, sb1 large
    #    streaming (distinct) + 1 backup walsender. max/AND both non-trivial.
    _set_apply_delay(STANDBYS[0], "3s")  # small
    _set_apply_delay(STANDBYS[1], "45s")  # large, distinct from sb0

    def _sb1_lag_high() -> bool:
        _load(4000)
        lag = _max_replay_lag_s()
        return lag is not None and lag >= DEFAULT_CRIT and _streaming_count() == 3

    wait_until(_sb1_lag_high, timeout=120.0)  # sb1 lag stable-high (distinct from sb0)
    # Non-streaming row via a HOLDABLE 'backup' walsender (throttled basebackup),
    # coexisting with the 3 streaming standbys (sb0 ~3s, sb1 >=30, sb2 ~0).
    _start_backup_walsender()
    try:
        text = await _record_when_state(
            "multi_replica.json",
            "backup",
            extra_check=lambda st: st.count("streaming") >= 2,
            verify=True,
        )
    finally:
        _stop_backup_walsender()
    out = _triple(text)
    assert out["link_healthy"] is False, out  # backup row -> AND false
    assert out["lag_seconds"] is not None and out["lag_seconds"] >= DEFAULT_CRIT, out
    print(f"multi_replica: {out}")

    # 7) link_down.json — a present row in a non-streaming state ('backup').
    #    Reset delays so only the non-streaming row drives link_healthy=false.
    for sb in STANDBYS:
        _set_apply_delay(sb, "0")
    wait_until(lambda: _streaming_count() == 3, timeout=120.0)
    _start_backup_walsender()
    try:
        text = await _record_when_state("link_down.json", "backup")
    finally:
        _stop_backup_walsender()
    out = _triple(text)
    assert out["replication_configured"] is True and out["link_healthy"] is False, out
    assert SPECIAL_PW not in text, "special password leaked into link_down fixture"
    print(f"link_down: {out}")
    wait_until(lambda: _streaming_count() == 3, timeout=120.0)

    # 8) conn_refused.json — closed port -> non-zero exit + empty stdout.
    text = await _record("conn_refused.json", parameters={"port": 15439}, allow_failed=True)
    main = json.loads(text)["commands"][-1]
    assert main["exit_code"] not in (0, None), "conn_refused must have non-zero exit"
    print("conn_refused recorded (non-zero main exit)")

    # 9) unconfigured.json — disconnect all standbys -> empty pg_stat_replication.
    for sb in STANDBYS:
        _net_disconnect(sb)
    wait_until(lambda: _row_count() == 0, timeout=60.0)
    text = await _record("unconfigured.json")
    out = _triple(text)
    assert out == {
        "replication_configured": False,
        "link_healthy": False,
        "lag_seconds": None,
    }, out
    print(f"unconfigured: {out}")


async def _main() -> None:
    _down()
    try:
        _up()
        _wait_health(PRIMARY)
        _setup_primary()
        await _record_fixtures()
        print("\nALL postgres.replication_lag fixtures recorded.")
    finally:
        _down()


if __name__ == "__main__":
    asyncio.run(_main())
