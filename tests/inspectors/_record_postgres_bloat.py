"""One-shot fixture recorder for `postgres.bloat_tables` (dev-tool, NOT a test).

Records `ReplayTarget` fixtures by driving the real `InspectorRunner` (via
`hostlens.inspectors.recorder.record_fixture`) against a throwaway
`postgres:16` docker container that ships its own `psql` + server. The local
machine has no `psql`; the container does, so a docker-exec `ExecutionTarget`
lets the runner render and dispatch the *real* command and capture the *real*
JSON output — zero drift, no `psql` install.

Records (to tests/inspectors/fixtures/postgres_bloat_tables/):
  * bloated.json / healthy.json / empty.json — three scenario DBs, status=ok.
  * conn_refused.json — points PGHOST/PGPORT at an unreachable port (nothing
    listening) so psql exits non-zero with empty stdout → status=exception.
    allow_failed=True; stderr is redacted of the injected password.

Usage (container `hl-pg` already running with the scenario databases seeded):

    HOSTLENS_POSTGRES_PASSWORD=<throwaway-pw> \
        .venv-impl/bin/python tests/inspectors/_record_postgres_bloat.py

(``<throwaway-pw>`` is whatever password the ephemeral ``hl-pg`` container was
started with — it is injected via the HOSTLENS_POSTGRES_PASSWORD env (remapped to
the client-native PGPASSWORD inside the collector) and **redacted** from recorded
stdout/stderr, so the committed fixtures never contain any plaintext password.)

This module is intentionally NOT collected by pytest (filename has no `test_`
prefix) — it is a manual fixture-generation helper kept beside the snapshot
test for reproducibility (see the snapshot test's module docstring).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.recorder import record_fixture
from hostlens.targets.base import Capability, ExecResult

CONTAINER = "hl-pg"
MANIFEST = Path("src/hostlens/inspectors/builtin/postgres/bloat_tables.yaml")
FIXTURE_DIR = Path("tests/inspectors/fixtures/postgres_bloat_tables")


class _DockerExecTarget:
    """`ExecutionTarget` that runs `exec` inside a docker container via `sh -c`.

    Recording-only. Satisfies the `ExecutionTarget` Protocol (`name` / `type` /
    `capabilities` / `exec` / `read_file`). `env` (carrying the injected
    `HOSTLENS_POSTGRES_PASSWORD` secret) is forwarded into the container with
    `docker exec -e NAME` so the secret reaches the real `psql` exactly as the
    runner intends — never spliced into the command string.
    """

    type = "local"

    def __init__(
        self, name: str, container: str, *, extra_env: dict[str, str] | None = None
    ) -> None:
        self.name = name
        self.container = container
        # `extra_env` (e.g. PGHOST/PGPORT to force a conn-refused recording) is
        # forwarded into the container alongside the injected secret env. It is
        # NON-secret connection routing, so its values stay un-redacted — unlike
        # the HOSTLENS_POSTGRES_PASSWORD secret, which the recorder redacts.
        self.extra_env = extra_env or {}
        self.capabilities: set[Capability] = {Capability.SHELL, Capability.FILE_READ}

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        # Run as the `postgres` OS user inside the container so the local-socket
        # peer auth maps to the `postgres` DB role — mirrors a real host where
        # the operator runs psql as a DB-capable user (the manifest itself
        # carries no hardcoded `-U`; connection identity is the caller's).
        argv = ["docker", "exec", "-u", "postgres"]
        merged_env = {**(env or {}), **self.extra_env}
        for key in merged_env:
            argv += ["-e", key]
        argv += [self.container, "sh", "-c", cmd]
        # Merge the injected secret env over the local environment so `docker`
        # stays on PATH while `docker exec -e NAME` forwards the secret (and any
        # extra_env routing) into the container (values read from this process env).
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, **merged_env},
        )
        out, err = await proc.communicate()
        return ExecResult(
            exit_code=proc.returncode,
            stdout=out.decode("utf-8", errors="replace"),
            stderr=err.decode("utf-8", errors="replace"),
            duration_seconds=0.0,
            timed_out=False,
        )

    async def read_file(self, path: str) -> bytes:
        raise AssertionError("read_file not used by postgres.bloat_tables")


async def _record(
    dbname: str,
    out_name: str,
    *,
    extra_env: dict[str, str] | None = None,
    extra_params: dict[str, object] | None = None,
    allow_failed: bool = False,
) -> None:
    manifest = load_manifest(MANIFEST)
    target = _DockerExecTarget("recorder", CONTAINER, extra_env=extra_env)
    fixture = await record_fixture(
        manifest,
        target,  # type: ignore[arg-type]
        settings=Settings(),
        parameters={"dbname": dbname, **(extra_params or {})},
        allow_failed=allow_failed,
    )
    path = FIXTURE_DIR / out_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fixture.to_json())
    print(f"wrote {path}")


async def _main() -> None:
    await _record("bloatdb", "bloated.json")
    await _record("healthydb", "healthy.json")
    await _record("emptydb", "empty.json")
    # exception track: point the connection at an unreachable port (nothing
    # listening) via PGHOST/PGPORT — psql exits non-zero with empty stdout, which
    # the runner surfaces as status=exception (a down backend never fabricates an
    # empty `{"total_tables":0,"results":[]}`). The injected password is still
    # redacted from the recorded stderr.
    await _record(
        "emptydb",
        "conn_refused.json",
        extra_env={"PGHOST": "127.0.0.1", "PGPORT": "15999"},
        allow_failed=True,
    )
    # truncation track: bloatdb has 2 user tables (orders/sessions); max_results=1
    # → `LIMIT 1` keeps only the single most-bloated table (orders) in `results`
    # while `total_tables` still reports the pre-truncation 2 — proving top-N-of-M.
    await _record("bloatdb", "bloated_truncated.json", extra_params={"max_results": 1})


if __name__ == "__main__":
    asyncio.run(_main())
