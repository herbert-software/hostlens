"""Snapshot tests for the ``mysql.replication_lag`` replication-inspector-contract probe.

This inspector is the mysql wave-2c probe of ``add-replication-lag-inspectors``: it
proves the multi-instance / replication contract on MySQL. ``mysql.replication_lag``
reads the replica-side ``SHOW REPLICA STATUS`` and normalizes to the three-tuple
``(replication_configured, link_healthy, lag_seconds)`` — where ``lag_seconds`` is the
``apply_lag`` class (``Seconds_Behind_Source``, NOT redis's link_freshness; see design
W-1/W-3/W-7).

All fixtures were recorded by ``_record_mysql_replication_lag.py`` driving the real
``InspectorRunner`` against a live mysql-repl-primary + mysql-repl-replica topology,
so the recorded command strings are byte-identical to what the runner sends — replay
hits with zero ``misses``.

Two SEMANTICALLY DISTINCT semantic-abnormal fixtures (design W-4):
  * ``link_down``  — ``STOP REPLICA IO_THREAD`` (or primary stopped) until
    ``Replica_IO_Running=No``; ``link_healthy=false`` → critical "replication link down"
    at the DEFAULT thresholds.
  * ``lagging``    — ``STOP REPLICA SQL_THREAD`` → primary bulk write →
    ``START REPLICA SQL_THREAD`` → poll during catch-up until
    ``Seconds_Behind_Source>=30`` with both threads running; ``link_healthy=true`` but
    apply lag high → critical at the DEFAULT thresholds.

The two are recorded WITH a space+glob-metachar password (``p w*d``) injected as
``HOSTLENS_MYSQL_PWD``; the recorder redacts every injected secret value, so the
committed fixtures never carry the plaintext (the redaction regression, task 3.3).

``test_conn_refused_fails_loud`` is the honesty regression lock (design W-2): a
conn-refused backend surfaces as ``status=exception``, never a fabricated healthy
object. The role-contextual fail-loud (an empty ``SHOW REPLICA STATUS`` → ``ok`` +
``replication_configured=false``, NOT exception) is exercised by task 2.3 and the
crosscheck, plus ``test_unconfigured_standalone_no_finding`` below against a stub.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
import structlog

import hostlens.inspectors as _inspectors_pkg
from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.result import InspectorResult
from hostlens.inspectors.runner import InspectorRunner
from hostlens.targets.base import Capability, ExecResult
from hostlens.targets.registry import TargetRegistry
from hostlens.targets.replay import ReplayTarget

_FIXTURES = Path(__file__).parent / "fixtures" / "mysql_replication_lag"

#: The space+glob-metachar password the two auth (link_down / lagging) fixtures
#: were recorded with. The redaction regression asserts it never leaks into a fixture.
_SPECIAL_PW = "p w*d"

#: Recorded fixtures were captured with this user parameter.
_BASE_PARAMS: dict[str, object] = {"user": "mon"}


def _builtin_root() -> Path:
    pkg_file = _inspectors_pkg.__file__
    assert pkg_file is not None
    return Path(pkg_file).parent / "builtin"


def _manifest_path() -> Path:
    return _builtin_root() / "mysql" / "replication_lag.yaml"


def _runner() -> InspectorRunner:
    return InspectorRunner(
        TargetRegistry(),
        settings=Settings(),
        logger=structlog.get_logger("mysql-replication-lag-test"),
    )


def _params(**extra: object) -> dict[str, object]:
    return {**_BASE_PARAMS, **extra}


@pytest.fixture(autouse=True)
def _mysql_password_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The manifest declares HOSTLENS_MYSQL_PWD as a secret; preflight requires it
    # present. The recorded command text is byte-identical regardless of value — the
    # password rides MYSQL_PWD via env, never inlined — so replay hits with zero misses.
    monkeypatch.setenv("HOSTLENS_MYSQL_PWD", _SPECIAL_PW)


def test_manifest_loads_cleanly() -> None:
    manifest = load_manifest(_manifest_path())
    assert manifest.name == "mysql.replication_lag"
    assert manifest.parse.format == "json"
    assert manifest.requires_binaries == ["mysql"]
    assert manifest.secrets == ["HOSTLENS_MYSQL_PWD"]
    assert "mysql8" in manifest.tags
    assert all("+" not in tag for tag in manifest.tags)
    # The three-tuple output_schema with a nullable lag_seconds.
    props = manifest.output_schema["properties"]
    assert set(props) == {"replication_configured", "link_healthy", "lag_seconds"}
    assert props["lag_seconds"]["type"] == ["integer", "null"]
    # lag semantic class declared in the description (heterogeneity contract, W-1).
    assert "apply_lag" in manifest.description
    # Secret reaches the client only via the MYSQL_PWD env remap — never argv.
    cmd = manifest.collect.command
    assert "MYSQL_PWD" in cmd
    assert "-p" not in cmd
    assert not (_manifest_path().parent / "hook.py").exists()


async def test_healthy_no_findings() -> None:
    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("replrec", fixture=_FIXTURES / "healthy.json")

    result = await _runner().run(manifest, replay, _params())

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output["replication_configured"] is True
    assert result.output["link_healthy"] is True
    lag = result.output["lag_seconds"]
    assert lag is not None
    assert lag < 15
    # lag_seconds < warn_seconds(15) → no finding at the defaults.
    assert result.findings == []


async def test_finding_trigger_emits_warning() -> None:
    """finding-trigger: healthy replica + LOWERED warn_seconds=0 fires a warning.
    Validates finding wiring ONLY (at the defaults this same apply lag is healthy)."""

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("replrec", fixture=_FIXTURES / "finding_trigger.json")

    result = await _runner().run(manifest, replay, _params(warn_seconds=0, critical_seconds=999))

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output["link_healthy"] is True
    # 0 in [warn=0, critical=999) → a single warning.
    assert [f.severity for f in result.findings] == ["warning"]


async def test_lagging_critical_at_default_thresholds() -> None:
    """semantic-abnormal #2 (apply-lag path): a REAL lagging-but-up link
    (Seconds_Behind_Source>=30 during catch-up, both threads running) fires a critical
    at the manifest DEFAULT thresholds. Distinct from link_down: link_healthy stays True."""

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("replrec", fixture=_FIXTURES / "lagging.json")

    result = await _runner().run(manifest, replay, _params())

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output["link_healthy"] is True
    lag = result.output["lag_seconds"]
    assert lag is not None
    assert lag >= 30
    # >= critical_seconds(30) → a single critical on the apply-lag path.
    assert [f.severity for f in result.findings] == ["critical"]
    assert "lag" in result.findings[0].message.lower()


async def test_link_down_critical_at_default_thresholds() -> None:
    """semantic-abnormal #1 (link path): a REAL broken link (IO thread stopped →
    Replica_IO_Running=No) fires a critical "link down" at the DEFAULT thresholds.
    Distinct from lagging: link_healthy is False and lag_seconds is null."""

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("replrec", fixture=_FIXTURES / "link_down.json")

    result = await _runner().run(manifest, replay, _params())

    assert replay.misses == []
    assert result.status == "ok"
    assert result.output == {
        "replication_configured": True,
        "link_healthy": False,
        "lag_seconds": None,
    }
    assert [f.severity for f in result.findings] == ["critical"]
    assert "link down" in result.findings[0].message.lower()


async def test_conn_refused_fails_loud() -> None:
    """Backend unreachable (conn refused) → status=exception, NOT a fabricated
    healthy object (honesty regression lock, design W-2)."""

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("replrec", fixture=_FIXTURES / "conn_refused.json")

    result = await _runner().run(manifest, replay, _params(port=13399))

    assert replay.misses == []
    assert result.status == "exception"
    assert result.output == {}
    assert result.findings == []


# --------------------------------------------------------------------------- #
# Redaction regression (task 3.3): the two auth fixtures carry NO plaintext secret
# and the fixture schema has no per-command ``env`` field.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["link_down", "lagging"])
def test_auth_fixture_redacts_special_password(name: str) -> None:
    import json

    text = (_FIXTURES / f"{name}.json").read_text(encoding="utf-8")
    # Non-vacuous redaction proof: the secret rides MYSQL_PWD via env (the channel the
    # fixture format OMITS), so the recorded command MUST carry that remap — proving the
    # value genuinely flowed through during recording — while the plaintext must appear
    # NOWHERE. A vacuous variant (no secret channel) or a leak would fail this.
    assert _SPECIAL_PW not in text, name
    data = json.loads(text)
    commands = data.get("commands", [])
    assert commands, name
    assert any('MYSQL_PWD="${HOSTLENS_MYSQL_PWD' in c.get("cmd", "") for c in commands), name
    for entry in commands:
        assert "env" not in entry, (name, entry)


# --------------------------------------------------------------------------- #
# Collector shell-logic anchor (design W-1/W-2): the recorded fixtures replay the
# collector's OUTPUT, they do NOT re-run its shell. This test renders the REAL
# `collect.command` (Jinja `| sh` == shlex.quote, exactly as the runner does) and
# runs it under `sh` with a stubbed `mysql` feeding canned `SHOW REPLICA STATUS\G`,
# so every normalization branch — empty-result unconfigured, NULL→null, apply-stall
# (SQL=No while IO=Yes), Connecting (IO!=Yes → link down), non-numeric SBS fail-loud,
# missing columns fail-loud, mysql non-zero exit fail-loud — is exercised
# deterministically in CI (no docker). This is the executable anchor for the
# collector branches the replay fixtures cannot reach.
# --------------------------------------------------------------------------- #


def _render_collector(params: dict[str, object]) -> str:
    import jinja2

    from hostlens.inspectors.runner import _sh_filter

    manifest = load_manifest(_manifest_path())
    # Fill schema defaults (host/port/warn/critical) exactly as the runner does before
    # rendering, then overlay the caller's params (user is required, no default).
    props = manifest.parameters.get("properties", {})
    merged: dict[str, object] = {k: v["default"] for k, v in props.items() if "default" in v}
    merged.update(params)
    env = jinja2.Environment(autoescape=False, undefined=jinja2.StrictUndefined)
    env.filters["sh"] = _sh_filter
    return env.from_string(manifest.collect.command).render(**merged)


def _run_collector(replica_status: str, *, mysql_exit: int = 0) -> tuple[int, str]:
    """Run the rendered collector with a stubbed `mysql`. Returns (rc, stdout)."""

    import os
    import shlex
    import subprocess
    import tempfile

    rendered = _render_collector(_params())
    with tempfile.TemporaryDirectory() as d:
        status_file = Path(d) / "status.txt"
        status_file.write_text(replica_status, encoding="utf-8")
        shim = Path(d) / "mysql"
        # The stub ignores all args, prints the canned `SHOW REPLICA STATUS` output,
        # and exits `mysql_exit` (to exercise the fail-loud `|| exit 1` trap).
        shim.write_text(
            f"#!/bin/sh\ncat {shlex.quote(str(status_file))}\nexit {mysql_exit}\n",
            encoding="utf-8",
        )
        shim.chmod(0o755)
        proc = subprocess.run(
            ["sh", "-c", rendered],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PATH": f"{d}:{os.environ['PATH']}",
                "HOSTLENS_MYSQL_PWD": _SPECIAL_PW,
            },
        )
    return proc.returncode, proc.stdout


_STATUS_OK = (
    "*************************** 1. row ***************************\n"
    "           Replica_IO_Running: {io}\n"
    "          Replica_SQL_Running: {sql}\n"
    "        Seconds_Behind_Source: {sbs}\n"
)

_SHELL_OK_CASES: list[tuple[str, str, dict[str, object]]] = [
    # id, canned SHOW REPLICA STATUS, expected normalized triple
    (
        "healthy",
        _STATUS_OK.format(io="Yes", sql="Yes", sbs="0"),
        {"replication_configured": True, "link_healthy": True, "lag_seconds": 0},
    ),
    (
        "lagging",
        _STATUS_OK.format(io="Yes", sql="Yes", sbs="45"),
        {"replication_configured": True, "link_healthy": True, "lag_seconds": 45},
    ),
    (
        "link_down_io_no",
        _STATUS_OK.format(io="No", sql="Yes", sbs="NULL"),
        {"replication_configured": True, "link_healthy": False, "lag_seconds": None},
    ),
    (
        "apply_stall_sql_no",
        _STATUS_OK.format(io="Yes", sql="No", sbs="NULL"),
        {"replication_configured": True, "link_healthy": False, "lag_seconds": None},
    ),
    (
        "connecting",
        _STATUS_OK.format(io="Connecting", sql="Yes", sbs="NULL"),
        {"replication_configured": True, "link_healthy": False, "lag_seconds": None},
    ),
    (
        "null_while_running",
        _STATUS_OK.format(io="Yes", sql="Yes", sbs="NULL"),
        {"replication_configured": True, "link_healthy": True, "lag_seconds": None},
    ),
    (
        "unconfigured_empty",
        "",
        {"replication_configured": False, "link_healthy": False, "lag_seconds": None},
    ),
    # `''` arm of `case "$sbs" in NULL|''`: io/sql present but the Seconds_Behind_Source
    # line is ABSENT → empty $sbs → lag=null (the one collector branch the cases above
    # never reach, since they all carry an SBS line).
    (
        "sbs_line_absent",
        "*************************** 1. row ***************************\n"
        "           Replica_IO_Running: Yes\n"
        "          Replica_SQL_Running: Yes\n",
        {"replication_configured": True, "link_healthy": True, "lag_seconds": None},
    ),
    # CRLF line endings: the awk `gsub(...[ \t\r]+$...)` strips the trailing CR so the
    # values parse identically to LF.
    (
        "crlf_terminated",
        _STATUS_OK.format(io="Yes", sql="Yes", sbs="0").replace("\n", "\r\n"),
        {"replication_configured": True, "link_healthy": True, "lag_seconds": 0},
    ),
    # Multi-channel (multi-source) replica returns one row per channel; the awk `exit`
    # after the first match makes the FIRST channel win deterministically (single-channel
    # scope, documented in the manifest description).
    (
        "multi_channel_first_wins",
        _STATUS_OK.format(io="Yes", sql="Yes", sbs="5")
        + "*************************** 2. row ***************************\n"
        + _STATUS_OK.format(io="No", sql="No", sbs="NULL"),
        {"replication_configured": True, "link_healthy": True, "lag_seconds": 5},
    ),
]


@pytest.mark.parametrize(
    "label,status,expected", _SHELL_OK_CASES, ids=[c[0] for c in _SHELL_OK_CASES]
)
def test_collector_shell_normalizes_real_status(
    label: str, status: str, expected: dict[str, object]
) -> None:
    import json

    rc, stdout = _run_collector(status)
    assert rc == 0, (label, rc, stdout)
    assert json.loads(stdout) == expected, (label, stdout)


_SHELL_FAILLOUD_CASES: list[tuple[str, str, int]] = [
    # id, canned status, mysql_exit — each must fail loud (rc != 0, no fabricated triple)
    ("non_numeric_sbs", _STATUS_OK.format(io="Yes", sql="Yes", sbs="garbage"), 0),
    (
        "missing_columns",
        "*************************** 1. row ***************************\n"
        "                  Source_Host: primary\n",
        0,
    ),
    # Negative SBS — MySQL never emits it, but the `*[!0-9]*` glob treats the `-` as
    # non-digit → fail-loud (honest, never a fabricated lag).
    ("negative_sbs", _STATUS_OK.format(io="Yes", sql="Yes", sbs="-1"), 0),
    ("mysql_nonzero_exit", "", 1),
    # Partial stdout WITH a non-zero exit (a backend that prints then dies): the `||`
    # trap keys on the exit code, not stdout, so it must still fail loud — never trust
    # the partial healthy-looking output.
    ("partial_stdout_nonzero_exit", _STATUS_OK.format(io="Yes", sql="Yes", sbs="0"), 1),
]


@pytest.mark.parametrize(
    "label,status,mysql_exit", _SHELL_FAILLOUD_CASES, ids=[c[0] for c in _SHELL_FAILLOUD_CASES]
)
def test_collector_shell_fails_loud(label: str, status: str, mysql_exit: int) -> None:
    rc, stdout = _run_collector(status, mysql_exit=mysql_exit)
    # Fail-loud: non-zero exit (→ status=exception upstream) and NEVER a triple stdout.
    assert rc != 0, (label, stdout)
    assert "replication_configured" not in stdout, (label, stdout)


async def test_warn_above_critical_is_graceful() -> None:
    """User misconfig warn_seconds > critical_seconds: the warn range [warn, critical)
    is empty, so only the critical can fire — graceful, no crash, no fabricated health."""

    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("replrec", fixture=_FIXTURES / "lagging.json")  # recorded SBS>=30

    result = await _runner().run(manifest, replay, _params(warn_seconds=50, critical_seconds=10))

    assert replay.misses == []
    assert result.status == "ok"
    # lag(>=30) >= critical(10) → critical; warn range [50,10) empty → no warning.
    assert [f.severity for f in result.findings] == ["critical"]


# --------------------------------------------------------------------------- #
# Role-contextual unconfigured path (design W-2): an empty SHOW REPLICA STATUS emits
# ok + replication_configured=false + no finding (NOT exception, NOT a fabricated
# lag). Exercised here against a stub returning an empty result so the manifest's
# unconfigured branch is hit without a recorded fixture (task 2.3 runs the live
# variant).
# --------------------------------------------------------------------------- #


class _StandaloneTarget:
    """Stub whose collector returns the unconfigured (empty replica status) triple.

    Preflight probes get canned success; the collector returns the empty-result-set
    output the manifest's own command would produce for a standalone instance."""

    type = "local"
    name = "standalone-host"
    capabilities: ClassVar[set[Capability]] = {Capability.SHELL, Capability.FILE_READ}

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        del timeout, env
        if cmd.startswith("command -v ") or cmd.startswith("[ -r "):
            return ExecResult(
                exit_code=0, stdout="", stderr="", duration_seconds=0.0, timed_out=False
            )
        return ExecResult(
            exit_code=0,
            stdout='{"replication_configured":false,"link_healthy":false,"lag_seconds":null}',
            stderr="",
            duration_seconds=0.0,
            timed_out=False,
        )

    async def read_file(self, path: str) -> bytes:
        raise AssertionError(f"read_file must not be reached: {path!r}")


async def test_unconfigured_standalone_no_finding() -> None:
    manifest = load_manifest(_manifest_path())
    target = _StandaloneTarget()

    result = await _runner().run(manifest, target, _params())  # type: ignore[arg-type]

    assert result.status == "ok"
    assert result.output == {
        "replication_configured": False,
        "link_healthy": False,
        "lag_seconds": None,
    }
    # Unconfigured (replication_configured=false) → NO finding (a standalone is not
    # a fault); the link-down finding requires replication_configured to be true.
    assert result.findings == []


# --------------------------------------------------------------------------- #
# Failure classification (design W-2, inherited base D-3): missing client binary /
# missing declared secret both map to requires_unmet (a graceful skip).
# --------------------------------------------------------------------------- #


class _NoBinaryTarget:
    """Stub where every ``command -v X`` probe fails (binary absent)."""

    type = "local"
    name = "no-binary-host"
    capabilities: ClassVar[set[Capability]] = {Capability.SHELL, Capability.FILE_READ}

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        del timeout, env
        if cmd.startswith("command -v "):
            return ExecResult(
                exit_code=1, stdout="", stderr="", duration_seconds=0.0, timed_out=False
            )
        raise AssertionError(f"collector must not run when mysql is absent: {cmd!r}")

    async def read_file(self, path: str) -> bytes:
        raise AssertionError(f"read_file must not be reached: {path!r}")


async def test_missing_mysql_requires_unmet() -> None:
    manifest = load_manifest(_manifest_path())
    target = _NoBinaryTarget()

    result: InspectorResult = await _runner().run(manifest, target, _params())  # type: ignore[arg-type]

    assert result.status == "requires_unmet"
    assert result.findings == []
    assert any(m.startswith("bin:") for m in result.missing), result.missing


async def test_missing_secret_env_requires_unmet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOSTLENS_MYSQL_PWD", raising=False)
    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("replrec", fixture=_FIXTURES / "healthy.json")

    result = await _runner().run(manifest, replay, _params())

    assert result.status == "requires_unmet"
    assert result.findings == []
    assert any("HOSTLENS_MYSQL_PWD" in m for m in result.missing), result.missing
