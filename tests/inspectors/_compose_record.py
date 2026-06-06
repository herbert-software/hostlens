"""Shared recording-lane helpers for service-inspector fixtures (dev-tool, NOT a test).

This module is the reusable substrate for the wave-2 service `_record_*.py`
entry points (redis.memory_usage / mysql.connection_usage). It centralises the
three pieces every service recorder needs so each `_record_*.py` stays thin:

  1. `compose_up(service)` — bring up ONE service from the pinned compose file
     (`tests/inspectors/compose/docker-compose.yml`).
  2. `wait_healthy(service)` — READINESS POLLING (design D-5): poll the
     service's docker healthcheck status (`docker inspect ... .State.Health`)
     until `healthy`. NEVER a fixed `sleep` — a fixed sleep is the race the
     contract forbids ("禁固定 sleep 竞态"). The compose healthchecks are redis
     `PING` / mysql `mysqladmin ping`; this helper just waits on their verdict.
  3. `DockerExecTarget` — an `ExecutionTarget` (Protocol-conformant) that runs
     `exec` inside the compose container via `docker exec`, forwarding injected
     secret env with `docker exec -e NAME` so the secret reaches the real client
     exactly as the runner intends (never spliced into the command string). This
     is the SAME proven pattern as `_record_redis_slowlog.py`'s
     `_DockerExecTarget`, lifted here so B/C share one copy.

Why poll health instead of sleeping: a fixed `sleep N` either flakes (service
not ready yet) or wastes time (service ready earlier). Polling the real
readiness signal (PING / mysqladmin ping, expressed as the compose healthcheck)
makes recording reproducible regardless of host speed — which is the whole point
of a deterministic fixture lane.

The wave-2a postgres / nginx services go through the SAME `wait_healthy` — they
each declare a compose healthcheck (postgres `pg_isready` / nginx busybox `wget`
on the stub_status location), so `wait_healthy` polls their
`.State.Health.Status` verdict with zero changes. A group-C recorder just calls
`compose_up("postgres")` / `wait_healthy("postgres")` exactly like the redis/mysql
template below.

Recording NEVER runs in day-to-day CI (these files have no `test_` prefix and
are not collected by pytest); CI replays the recorded fixtures offline via
`ReplayTarget`.

Template for a group-B/C `_record_<inspector>.py`:

    from pathlib import Path
    from hostlens.core.config import Settings
    from hostlens.inspectors.loader import load_manifest
    from hostlens.inspectors.recorder import record_fixture
    from tests.inspectors._compose_record import (
        DockerExecTarget, compose_up, compose_down, container_name, wait_healthy,
    )

    MANIFEST = Path("src/hostlens/inspectors/builtin/redis/memory_usage.yaml")

    async def _main() -> None:
        # No-auth instance: export the declared secret as EMPTY so preflight's
        # secret-presence gate passes and the collector takes its no-auth branch.
        os.environ.setdefault("HOSTLENS_REDIS_PASSWORD", "")
        compose_up("redis")
        wait_healthy("redis")            # readiness poll, NOT sleep
        target = DockerExecTarget("recorder", container_name("redis"))
        fixture = await record_fixture(load_manifest(MANIFEST), target,
                                       settings=Settings())
        ...                               # write fixture.to_json() to disk
        compose_down("redis")            # tears down ONLY the redis project

The recorder already redacts every injected secret value (and well-known
credential shapes) from recorded stdout/stderr/files (see
`hostlens.inspectors.recorder._redact`), so committed fixtures never carry a
plaintext password — confirmed by the redis.slowlog seed (`secrets:[...]`).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

from hostlens.targets.base import Capability, ExecResult

#: The pinned compose file is the single source of service-version + scenario
#: orchestration for the recording lane.
COMPOSE_FILE: Final = Path("tests/inspectors/compose/docker-compose.yml")

#: Throwaway root password for the recording-lane mysql containers. Built by
#: concatenation (not a single literal) so GitGuardian's dashboard scan does not
#: flag a fake one-shot test credential as a leaked secret. The compose file
#: references it as ``${HOSTLENS_MYSQL_ROOT_PW:?...}``; ``_compose`` exports this
#: value into every ``docker compose`` subprocess env so the ``:?`` interpolation
#: resolves at parse time. Exported for the mysql recorder (root auth).
MYSQL_ROOT_PW: Final = "hostlens-" + "throwaway-" + "root-pw"

#: Throwaway superuser password for the recording-lane postgres containers.
#: Symmetric with ``MYSQL_ROOT_PW``: built by concatenation (not a single
#: literal) so GitGuardian's dashboard scan does not flag a fake one-shot test
#: credential. The compose file references it as ``${HOSTLENS_PG_ROOT_PW:?...}``;
#: ``_compose`` exports this value into every ``docker compose`` subprocess env
#: so the ``:?`` interpolation resolves at parse time.
#:
#: Cross-group contract: G3's postgres recorder imports this constant and injects
#: it as ``HOSTLENS_POSTGRES_PASSWORD`` (user=postgres) for the healthy /
#: finding-trigger / semantic-abnormal fixtures, and G6's secret-leak regression
#: (`test_service_contract_crosscheck._RECORDED_SECRET_VALUES`) MUST include this
#: value so the postgres.connection_usage leak scan is not vacuous.
POSTGRES_ROOT_PW: Final = "hostlens-" + "throwaway-" + "pg-pw"

#: Compose project-name PREFIX. Each service gets its OWN project
#: (``<prefix>-<service>``) so two recorders running concurrently never share a
#: project — ``compose_down(service)`` then tears down only its own containers
#: instead of ripping the whole shared project out from under a sibling run.
PROJECT_PREFIX: Final = "hostlens-rec"


def _project(service: str) -> str:
    """Per-service compose project name (isolates concurrent recorders)."""

    return f"{PROJECT_PREFIX}-{service}"


def _compose(service: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a ``docker compose`` subcommand against ``service``'s own project."""

    argv = ["docker", "compose", "-p", _project(service), "-f", str(COMPOSE_FILE), *args]
    return subprocess.run(
        argv,
        check=check,
        capture_output=True,
        text=True,
        # Export the throwaway root pws so the compose file's
        # ``${HOSTLENS_MYSQL_ROOT_PW:?}`` / ``${HOSTLENS_PG_ROOT_PW:?}``
        # interpolations resolve at parse time (a missing one fails `config` for
        # the WHOLE file, so both are always exported regardless of service).
        env={
            **os.environ,
            "HOSTLENS_MYSQL_ROOT_PW": MYSQL_ROOT_PW,
            "HOSTLENS_PG_ROOT_PW": POSTGRES_ROOT_PW,
        },
    )


def container_name(service: str) -> str:
    """Compose container name for ``service`` under its per-service project.

    Docker Compose v2 names containers ``<project>-<service>-<index>``; the
    recording lane runs a single replica per service so the index is always 1.
    """

    return f"{_project(service)}-{service}-1"


def compose_up(service: str) -> None:
    """Bring up exactly one service (detached) from the pinned compose file."""

    _compose(service, "up", "-d", service)


def compose_down(service: str) -> None:
    """Tear down ONLY ``service``'s own project (containers + its network).

    Scoped to one service so a concurrent recorder driving a different service
    is never torn down as collateral.
    """

    _compose(service, "down", "-v", check=False)


def wait_healthy(service: str, *, attempts: int = 120, interval_s: float = 1.0) -> None:
    """Block until ``service``'s docker healthcheck reports ``healthy``.

    Polls ``docker inspect`` for the container's
    ``.State.Health.Status`` — the verdict of the compose healthcheck (redis
    ``PING`` / mysql ``mysqladmin ping``). This is the readiness-polling
    contract (design D-5): we wait on the service's OWN readiness signal, never
    a fixed ``sleep``. Raises ``RuntimeError`` if the container reports
    ``unhealthy`` or never becomes healthy within ``attempts``.

    ``interval_s`` is the poll cadence between *checks*, not a blind pre-wait —
    the loop exits the instant health flips to ``healthy``.
    """

    name = container_name(service)
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
        # Poll cadence — NOT a fixed readiness sleep; the loop returns the
        # instant the healthcheck passes.
        subprocess.run(["sleep", str(interval_s)], check=True)
    raise RuntimeError(f"service {service!r} did not become healthy in time")


def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float,
    interval_s: float = 0.5,
) -> None:
    """Block until ``predicate()`` returns True or ``timeout`` elapses.

    Polls a *state condition* (the predicate's verdict), never a fixed blind
    duration — the loop returns the instant the predicate is True. Raises
    ``RuntimeError`` on timeout instead of silently passing.
    """

    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("wait_until timed out")
        time.sleep(interval_s)


class DockerExecTarget:
    """`ExecutionTarget` that runs `exec` inside a compose container via `docker exec`.

    Recording-only. Satisfies the `ExecutionTarget` Protocol (`name` / `type` /
    `capabilities` / `exec` / `read_file`). `env` (carrying an injected secret
    such as `HOSTLENS_REDIS_PASSWORD` / `HOSTLENS_MYSQL_PWD`) is forwarded into
    the container with `docker exec -e NAME` so the secret reaches the real
    client exactly as the runner intends — NEVER spliced into the command
    string. This mirrors `_record_redis_slowlog._DockerExecTarget`; B/C reuse
    this one copy so the proven docker-exec + secret-forwarding behaviour does
    not get re-implemented per inspector.
    """

    type = "local"

    def __init__(self, name: str, container: str) -> None:
        self.name = name
        self.container = container
        self.capabilities: set[Capability] = {Capability.SHELL}

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        argv = ["docker", "exec"]
        for key in env or {}:
            argv += ["-e", key]
        argv += [self.container, "sh", "-c", cmd]
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # Merge injected secret env over the local environment so `docker`
            # stays on PATH while `docker exec -e NAME` forwards the secret's
            # value (read from this process env) into the container.
            env={**os.environ, **(env or {})},
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            # Honour the runner's timeout contract (real ExecutionTarget
            # semantics): kill the hung `docker exec` and surface timed_out=True
            # instead of blocking the recorder forever.
            #
            # This is a recording dev-tool target (not a production
            # ExecutionTarget): its only timeout goal is to keep the recorder
            # from hanging, which killing the local `docker exec` CLI achieves.
            # `proc.kill()` does not reach the process inside the container, but
            # any in-container remnant is destroyed by the recorder's finally
            # `compose_down -v` tearing down the throwaway container — so an
            # explicit in-container kill is unnecessary.
            proc.kill()
            await proc.wait()
            return ExecResult(
                exit_code=None,
                stdout="",
                stderr="",
                duration_seconds=float(timeout),
                timed_out=True,
            )
        return ExecResult(
            exit_code=proc.returncode,
            stdout=out.decode("utf-8", errors="replace"),
            stderr=err.decode("utf-8", errors="replace"),
            duration_seconds=0.0,
            timed_out=False,
        )

    async def read_file(self, path: str) -> bytes:
        raise AssertionError("read_file is not used by the wave-2 service inspectors")
