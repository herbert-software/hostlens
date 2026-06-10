"""One-shot fixture recorder for `redis.slowlog` (dev-tool, NOT a test).

Records `ReplayTarget` fixtures by driving the real `InspectorRunner` (via
`hostlens.inspectors.recorder.record_fixture`) against the pinned compose redis
services (see `tests/inspectors/compose/docker-compose.yml`). Each container
ships its own `redis-cli` + server, so a docker-exec `ExecutionTarget` lets the
runner render and dispatch the *real* command and capture the *real* JSON output
— zero drift, no local `redis-cli` install. Readiness is polled via the compose
healthcheck (`wait_healthy`), never a fixed `sleep` (design D-5).

The secret is declared as `HOSTLENS_REDIS_PASSWORD` (HOSTLENS_ prefix per the
ssh-execution-target contract); the collector REMAPS it to redis-cli's native
`REDISCLI_AUTH` env channel — never argv. The recording entry points export the
secret value into this process env so the runner forwards it via
`docker exec -e HOSTLENS_REDIS_PASSWORD` into the container.

Usage (this script manages the compose lifecycle itself):

    .venv-impl/bin/python -m tests.inspectors._record_redis_slowlog

Records (into tests/inspectors/fixtures/redis/):
  * slowlog_nonempty.json — seeds slow queries (threshold=0 logs everything) so
    SLOWLOG LEN > 0 with small max_micros (healthy finding-trigger: count rule
    only, status=ok).
  * slowlog_empty.json    — empty slowlog → count=0 (genuine empty, status=ok).
  * slowlog_conn_refused.json — fail-loud path: redis-cli points at a closed
    port, so SLOWLOG LEN exits non-zero with empty stdout. Recorded with
    `allow_failed=True` (the failed run IS the point) → status=exception.
  * slowlog_semantic_abnormal.json — a REAL slow query (DEBUG SLEEP 0.15 →
    >=150ms) recorded at DEFAULT thresholds so `max_micros >= slow_micros
    (100000)` fires the max_micros rule (D-4). The default `warn_count=1` is so
    low that any non-empty slowlog already trips the count rule, so the count
    rule cannot distinguish a healthy non-empty slowlog from a real slow-query
    state — the max_micros rule is the discriminating track. DEBUG SLEEP needs
    `--enable-debug-command yes`, which only the `redis-repl-master` compose
    service enables (the plain `redis` service disables DEBUG); recorded against
    it as a standalone single instance.
  * slowlog_special_char_pw.json — auth instance whose password contains a space
    + glob metachar (`p w*d`), recorded with HOSTLENS_REDIS_PASSWORD set to that
    value. Proves the REDISCLI_AUTH env-remap channel does NOT word-split the
    password into bogus args (would be a bogus auth failure with unquoted `-a`).
    For this metrics-only inspector (count / max_micros integers only) the
    password is never echoed to stdout, so this track validates COMMAND SAFETY
    (no word-split / no broken command string), NOT redaction.

This module is intentionally NOT collected by pytest (no `test_` prefix).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.recorder import record_fixture
from tests.inspectors._compose_record import (
    DockerExecTarget,
    compose_down,
    compose_up,
    container_name,
    wait_healthy,
)

MANIFEST = Path("src/hostlens/inspectors/builtin/redis/slowlog.yaml")
FIXTURE_DIR = Path("tests/inspectors/fixtures/redis")

#: Password with a space AND a glob metachar — the word-split / unquoted-`-a`
#: regression payload. It must survive intact through the REDISCLI_AUTH env
#: channel (env values are never word-split), not through argv. Reuses the SAME
#: value as `redis.persistence` so `_RECORDED_SECRET_VALUES` needs no new entry.
SPECIAL_PW = "p w*d"


async def _record(
    service: str,
    out_name: str,
    *,
    parameters: dict[str, Any] | None = None,
    allow_failed: bool = False,
) -> str:
    manifest = load_manifest(MANIFEST)
    target = DockerExecTarget("recorder", container_name(service))
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


def _exec(service: str, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "exec", container_name(service), *argv],
        capture_output=True,
        text=True,
    )


async def _record_redis_family() -> None:
    # --- empty / nonempty / conn_refused / special-char-pw against the no-auth
    # `redis` service. Export empty secret so preflight's secret-presence gate
    # passes and the collector takes its no-auth branch (design D-3).
    os.environ["HOSTLENS_REDIS_PASSWORD"] = ""
    compose_up("redis")
    wait_healthy("redis")

    # --- nonempty: generate several slow entries (threshold=0 logs everything →
    # small max_micros). Healthy non-empty slowlog (count rule only).
    _exec("redis", "redis-cli", "CONFIG", "SET", "slowlog-log-slower-than", "0")
    _exec("redis", "redis-cli", "SLOWLOG", "RESET")
    for _ in range(6):
        _exec("redis", "redis-cli", "PING")
    await _record("redis", "slowlog_nonempty.json")

    # --- empty: raise the threshold then reset so SLOWLOG LEN == 0 (genuine
    # empty → count=0, status=ok).
    _exec("redis", "redis-cli", "CONFIG", "SET", "slowlog-log-slower-than", "10000000")
    _exec("redis", "redis-cli", "SLOWLOG", "RESET")
    await _record("redis", "slowlog_empty.json")

    # --- conn refused (fail-loud): point redis-cli at a closed port. The
    # collector's SLOWLOG LEN exits non-zero → empty stdout → status=exception.
    text = await _record(
        "redis",
        "slowlog_conn_refused.json",
        parameters={"port": 6390},  # nothing listening
        allow_failed=True,
    )
    main = json.loads(text)["commands"][-1]
    assert main["exit_code"] != 0, "expected the main collect command to have a non-zero exit_code"
    print("conn_refused fixture has non-zero main-command exit")

    # --- special-char password: prove the REDISCLI_AUTH env remap does not
    # word-split a password containing a space + glob metachar. Set a real
    # requirepass on the instance, then record with the matching secret.
    set_pw = _exec("redis", "redis-cli", "CONFIG", "SET", "requirepass", SPECIAL_PW)
    assert set_pw.returncode == 0, set_pw.stderr
    os.environ["HOSTLENS_REDIS_PASSWORD"] = SPECIAL_PW
    try:
        await _record("redis", "slowlog_special_char_pw.json")
    finally:
        # Reset auth so the recorder env state cannot leak into a re-record.
        _exec(
            "redis",
            "redis-cli",
            "-a",
            SPECIAL_PW,
            "--no-auth-warning",
            "CONFIG",
            "SET",
            "requirepass",
            "",
        )
        os.environ["HOSTLENS_REDIS_PASSWORD"] = ""


async def _record_semantic_abnormal() -> None:
    # --- semantic-abnormal: a REAL >=100ms slow query so the DEFAULT thresholds
    # fire the max_micros rule (D-4). `redis-repl-master` is the only compose
    # service with `--enable-debug-command yes`, so DEBUG SLEEP works there; it
    # is brought up alone as a standalone single instance.
    os.environ["HOSTLENS_REDIS_PASSWORD"] = ""
    compose_up("redis-repl-master")
    wait_healthy("redis-repl-master")

    _exec("redis-repl-master", "redis-cli", "CONFIG", "SET", "slowlog-log-slower-than", "0")
    _exec("redis-repl-master", "redis-cli", "SLOWLOG", "RESET")
    # DEBUG SLEEP 0.15 = a real 150ms server-blocking command → a slowlog entry
    # whose duration (max_micros) is ~150000 >= slow_micros(100000) default.
    slept = _exec("redis-repl-master", "redis-cli", "DEBUG", "SLEEP", "0.15")
    assert "OK" in slept.stdout, f"DEBUG SLEEP failed: {slept.stdout} {slept.stderr}"

    text = await _record("redis-repl-master", "slowlog_semantic_abnormal.json")
    out = json.loads(json.loads(text)["commands"][-1]["stdout"])
    assert out["max_micros"] >= 100000, (
        f"semantic-abnormal max_micros must be >= 100000 to trip the max_micros "
        f"rule at default thresholds, got {out['max_micros']}"
    )
    print(f"semantic_abnormal fixture max_micros={out['max_micros']} (>= 100000)")


async def _main() -> None:
    try:
        await _record_redis_family()
    finally:
        compose_down("redis")
    try:
        await _record_semantic_abnormal()
    finally:
        compose_down("redis-repl-master")


if __name__ == "__main__":
    asyncio.run(_main())
