"""One-shot fixture recorder for `nginx.error_rate` (dev-tool, NOT a test).

Drives the real `InspectorRunner` (via `record_fixture`) against the pinned
compose `nginx-errorrate` service through a docker-exec ExecutionTarget. That
service mounts a `tmpfs` at /var/log/nginx so nginx writes access.log as a REAL
regular file (shadowing the official image's /dev/stdout symlink — tasks 1.1 /
D-4); the inspector reads it at the static path via busybox awk inside the
container. Two locations (`/` -> 200, `/err500` -> 500) let the recorder generate
a controlled 2xx/5xx mix; the log is truncated between scenarios.

Records (to tests/inspectors/fixtures/nginx_error_rate/):
  * empty_log.json        — truncated log, no requests → total_requests=0 →
    error_rate_pct=0 (the awk END{} divide-by-zero guard) → no finding → status=ok.
  * healthy.json          — enough 2xx traffic (total >= min_requests) with a low
    5xx rate (< warn_pct) → no finding → status=ok.
  * semantic_abnormal.json— REAL 5xx traffic so error_rate_pct >= the DEFAULT
    warn_pct AND total_requests >= the DEFAULT min_requests → warning (a real
    accumulated error-rate state — NOT a lowered threshold).
  * small_sample.json     — a single 5xx request → 100% rate but total_requests <
    min_requests → no finding (proves the small-sample gate) → status=ok.

Usage (manages the compose lifecycle itself):
    .venv-impl/bin/python tests/inspectors/_record_nginx_error_rate.py

Intentionally NOT collected by pytest (no `test_` prefix).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

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

MANIFEST = Path("src/hostlens/inspectors/builtin/nginx/error_rate.yaml")
FIXTURE_DIR = Path("tests/inspectors/fixtures/nginx_error_rate")
_ACCESS_LOG = "/var/log/nginx/access.log"


def _truncate(container: str) -> None:
    subprocess.run(["docker", "exec", container, "sh", "-c", f": > {_ACCESS_LOG}"], check=True)


def _gen(container: str, path: str, n: int) -> None:
    """Generate `n` requests to `path` from inside the container (busybox wget;
    a 5xx makes wget exit non-zero, but the request is still logged by nginx)."""

    subprocess.run(
        [
            "docker",
            "exec",
            container,
            "sh",
            "-c",
            f"i=0; while [ $i -lt {n} ]; do wget -q -O /dev/null http://127.0.0.1{path} || true; i=$((i+1)); done",
        ],
        check=True,
    )
    time.sleep(0.5)  # let nginx flush the (unbuffered) access_log


async def _record(out_name: str, container: str) -> dict:
    manifest = load_manifest(MANIFEST)
    target = DockerExecTarget("recorder", container)
    fixture = await record_fixture(manifest, target, settings=Settings())  # type: ignore[arg-type]
    path = FIXTURE_DIR / out_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fixture.to_json())
    print(f"wrote {path}")
    return json.loads(json.loads(path.read_text())["commands"][-1]["stdout"])


async def _main() -> None:
    manifest = load_manifest(MANIFEST)
    props = manifest.parameters["properties"]
    warn_pct = float(props["warn_pct"]["default"])
    min_requests = int(props["min_requests"]["default"])
    try:
        compose_up("nginx-errorrate")
        wait_healthy("nginx-errorrate")
        container = container_name("nginx-errorrate")

        # empty_log: truncated, no traffic → total 0 → ok zero object.
        _truncate(container)
        p = await _record("empty_log.json", container)
        assert p["total_requests"] == 0, p

        # healthy: plenty of 2xx, no 5xx → total >= min_requests, rate < warn_pct.
        _truncate(container)
        _gen(container, "/", max(min_requests + 10, 20))
        p = await _record("healthy.json", container)
        assert p["total_requests"] >= min_requests and p["error_rate_pct"] < warn_pct, p

        # semantic-abnormal: real 5xx so rate >= warn_pct AND total >= min_requests.
        _truncate(container)
        _gen(container, "/err500", max(min_requests, 10))
        _gen(container, "/", max(min_requests, 10))
        p = await _record("semantic_abnormal.json", container)
        assert p["error_rate_pct"] >= warn_pct and p["total_requests"] >= min_requests, p

        # small_sample: one 5xx → 100% rate but total < min_requests → no finding.
        _truncate(container)
        _gen(container, "/err500", 1)
        p = await _record("small_sample.json", container)
        assert p["total_requests"] < min_requests, p
    finally:
        compose_down("nginx-errorrate")
    print("nginx.error_rate fixtures recorded.")


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))  # type: ignore[func-returns-value]
