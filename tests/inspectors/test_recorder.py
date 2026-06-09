"""Tests for the inspector fixture recorder (dev-tool).

Covers tasks.md §1.4's four contracts:

  1. The recorded fixture replays — driving the runner against a ``ReplayTarget``
     built from the recorder output hits every command (zero ``misses``) and
     reproduces the live ``InspectorResult``.
  2. Command drift fails replay — mutating the manifest command after recording
     makes the runner send a command the fixture has no record for, surfacing a
     ``ReplayMiss`` / a non-empty ``misses`` list (drift is never silently
     swallowed).
  3. No plaintext secret in the fixture — a command that echoes an injected
     ``secrets_env`` value lands ``***REDACTED***`` in the fixture, never the
     plaintext.
  4. Sampling-window / timestamp freezing makes the recording repeatable —
     recording the same window-bearing inspector twice produces byte-identical
     fixtures, and a nondeterministic timestamp in output is frozen by a scrubber.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
import structlog

from hostlens.core.config import Settings
from hostlens.core.exceptions import ConfigError, ReplayMiss
from hostlens.inspectors.recorder import record_fixture
from hostlens.inspectors.runner import InspectorRunner
from hostlens.inspectors.schema import (
    CollectSpec,
    FindingRule,
    InspectorManifest,
    ParseSpec,
    SamplingWindow,
)
from hostlens.targets.base import Capability, ExecResult
from hostlens.targets.registry import TargetRegistry
from hostlens.targets.replay import ReplayTarget


def _logger() -> structlog.stdlib.BoundLogger:
    return structlog.get_logger("test")  # type: ignore[no-any-return]


class _ScriptedTarget:
    """Fake ``ExecutionTarget`` returning canned output per command substring.

    ``script`` maps a substring -> ``ExecResult``; the first substring contained
    in the incoming ``cmd`` wins. Binary / file probes (``command -v ...`` /
    ``[ -r ... ]``) default to exit-code 0 so preflight passes. This avoids any
    real subprocess while still letting the runner render and dispatch the exact
    command strings.
    """

    type = "local"

    def __init__(self, name: str, script: list[tuple[str, ExecResult]]) -> None:
        self.name = name
        self.capabilities: set[Capability] = {Capability.SHELL, Capability.FILE_READ}
        self._script = script
        self.seen: list[str] = []

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        self.seen.append(cmd)
        for needle, result in self._script:
            if needle in cmd:
                return result
        if cmd.startswith("command -v ") or cmd.startswith("[ -r "):
            return ExecResult(
                exit_code=0, stdout="", stderr="", duration_seconds=0.0, timed_out=False
            )
        raise AssertionError(f"unscripted command: {cmd!r}")

    async def read_file(self, path: str) -> bytes:
        raise AssertionError("read_file not used in these tests")


class _TypedScriptedTarget(_ScriptedTarget):
    """``_ScriptedTarget`` whose ``type`` is set per-instance (not the class
    default ``"local"``) so recorder impersonation can be exercised across
    target types without mutating the shared class attribute."""

    def __init__(
        self, name: str, script: list[tuple[str, ExecResult]], *, target_type: str
    ) -> None:
        super().__init__(name, script)
        self.type = target_type


def _make_manifest(
    *,
    command: str,
    requires_binaries: list[str] | None = None,
    secrets: list[str] | None = None,
    output_schema: dict[str, Any] | None = None,
    findings: list[FindingRule] | None = None,
    sampling_window: SamplingWindow | None = None,
    targets: list[str] | None = None,
) -> InspectorManifest:
    return InspectorManifest(
        name="test.recorder",
        version="1.0.0",
        description="recorder test inspector",
        targets=targets or ["local"],
        requires_binaries=requires_binaries or [],
        requires_capabilities=["shell"],
        secrets=secrets or [],
        collect=CollectSpec(command=command, sampling_window=sampling_window),
        parse=ParseSpec(format="json"),
        output_schema=output_schema or {"type": "object"},
        findings=findings or [],
    )


def _runner() -> InspectorRunner:
    return InspectorRunner(TargetRegistry(), settings=Settings(), logger=_logger())


# --------------------------------------------------------------------------- #
# 1. recorded fixture replays — zero misses, reproduces the live result
# --------------------------------------------------------------------------- #


async def test_recorded_fixture_replays(tmp_path: Any) -> None:
    manifest = _make_manifest(
        command='echo \'{"results": [{"name": "t1", "bloat": 42}]}\'',
        requires_binaries=["psql"],
        output_schema={"type": "object"},
        findings=[
            FindingRule(
                for_each="results as row",
                when="row.bloat > 10",
                severity="warning",
                message="bloated table {row.name}",
            )
        ],
    )
    payload = '{"results": [{"name": "t1", "bloat": 42}]}\n'
    target = _ScriptedTarget(
        "rec",
        [
            (
                "echo",
                ExecResult(
                    exit_code=0, stdout=payload, stderr="", duration_seconds=0.0, timed_out=False
                ),
            )
        ],
    )

    fixture = await record_fixture(manifest, target, settings=Settings())

    # The fixture must carry BOTH the binary probe and the main command.
    recorded_cmds = [c["cmd"] for c in fixture.commands]
    assert any(c.startswith("command -v ") and "psql" in c for c in recorded_cmds), recorded_cmds
    assert any("echo" in c for c in recorded_cmds), recorded_cmds

    # Capability declaration is projected onto the fixture.
    assert "shell" in fixture.capabilities

    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(fixture.to_json())

    # Live run for the expected result.
    live_result = await _runner().run(manifest, target, None)

    # Replay run against the recorded fixture: zero misses + same findings.
    replay = ReplayTarget("rec", fixture=fixture_path)
    replay_result = await _runner().run(manifest, replay, None)

    assert replay.misses == []
    assert replay_result.status == "ok"
    assert replay_result.status == live_result.status
    assert [f.message for f in replay_result.findings] == [f.message for f in live_result.findings]
    assert replay_result.output == live_result.output


# --------------------------------------------------------------------------- #
# 2. command drift fails replay (never silently passes)
# --------------------------------------------------------------------------- #


async def test_command_drift_fails_replay(tmp_path: Any) -> None:
    manifest = _make_manifest(
        command="echo '{\"results\": []}'",
        requires_binaries=["psql"],
    )
    target = _ScriptedTarget(
        "rec",
        [
            (
                "echo",
                ExecResult(
                    exit_code=0,
                    stdout='{"results": []}\n',
                    stderr="",
                    duration_seconds=0.0,
                    timed_out=False,
                ),
            )
        ],
    )
    fixture = await record_fixture(manifest, target, settings=Settings())
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(fixture.to_json())

    # Author edits the inspector command after the fixture was recorded.
    drifted = _make_manifest(
        command="echo '{\"results\": [1]}'",  # different bytes -> different match key
        requires_binaries=["psql"],
    )

    replay = ReplayTarget("rec", fixture=fixture_path)

    # Drift must surface loudly. ``ReplayMiss`` deliberately does NOT inherit
    # ``TargetError`` so the runner cannot swallow it as ``target_unreachable``
    # — it propagates out of ``run``. The drifted command also lands in
    # ``replay.misses`` regardless of the raise (the strict-consumption guard).
    with pytest.raises(ReplayMiss):
        await _runner().run(drifted, replay, None)
    assert any(m["kind"] == "exec" for m in replay.misses), replay.misses


# --------------------------------------------------------------------------- #
# 3. echoed secret never lands plaintext in the fixture
# --------------------------------------------------------------------------- #


async def test_echoed_secret_redacted(tmp_path: Any, monkeypatch: Any) -> None:
    secret_value = "s3cr3t" + "-bot-token-" + "abc123"  # split: not a contiguous literal
    monkeypatch.setenv("PGPASSWORD", secret_value)

    manifest = _make_manifest(
        command="echo {{ PGPASSWORD if false else '' }}; print_secret",
        secrets=["PGPASSWORD"],
    )
    # Simulate a command that echoes the injected secret into stdout/stderr.
    echoed = ExecResult(
        exit_code=0,
        stdout=f'{{"results": []}} leaked={secret_value}',
        stderr=f"warning: connecting with password {secret_value}",
        duration_seconds=0.0,
        timed_out=False,
    )
    target = _ScriptedTarget("rec", [("print_secret", echoed)])

    # The echoed-secret stdout is intentionally non-JSON (the leak is the point),
    # so the run status is `exception`; recording it requires `allow_failed`.
    fixture = await record_fixture(manifest, target, settings=Settings(), allow_failed=True)
    fixture_json = fixture.to_json()

    assert secret_value not in fixture_json, "plaintext secret leaked into fixture"
    assert "***REDACTED***" in fixture_json


async def test_heuristic_token_and_webhook_redacted(tmp_path: Any) -> None:
    # No declared secret — these arrive via output heuristics only.
    # Secret-shaped literals split via concatenation so scanners cannot match a
    # contiguous literal (.gitguardian.yaml convention); runtime value unchanged.
    bot_token = "123456789" + ":AAEhBOweik9ai2" + "-3kJ7uYxabcdefghijkLMN"
    webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/" + "abcdef" + "0123456789"
    weak_pw = "hunter" + "2"
    leaked = ExecResult(
        exit_code=0,
        stdout=f'{{"results": []}} token={bot_token} url={webhook} password={weak_pw}',
        stderr="",
        duration_seconds=0.0,
        timed_out=False,
    )
    manifest = _make_manifest(command="echo dump; leak_creds")
    target = _ScriptedTarget("rec", [("leak_creds", leaked)])

    # Non-JSON leak payload → status=exception; recording needs `allow_failed`.
    fixture = await record_fixture(manifest, target, settings=Settings(), allow_failed=True)
    fixture_json = fixture.to_json()

    assert bot_token not in fixture_json
    assert ("abcdef" + "0123456789") not in fixture_json
    assert weak_pw not in fixture_json
    # The non-secret webhook host prefix is preserved (only the tail is masked).
    assert "https://open.feishu.cn/open-apis/bot/v2/hook/" in fixture_json


# --------------------------------------------------------------------------- #
# 4. sampling-window / timestamp freezing makes recordings repeatable
# --------------------------------------------------------------------------- #


async def test_sampling_window_recording_is_repeatable() -> None:
    manifest = _make_manifest(
        command="echo since={{ window_start | sh }} until={{ window_end | sh }}; query",
        sampling_window=SamplingWindow(duration_seconds=3600),
    )
    out = ExecResult(
        exit_code=0, stdout='{"results": []}', stderr="", duration_seconds=0.0, timed_out=False
    )

    target_a = _ScriptedTarget("rec", [("query", out)])
    target_b = _ScriptedTarget("rec", [("query", out)])

    fixture_a = await record_fixture(manifest, target_a, settings=Settings())
    fixture_b = await record_fixture(manifest, target_b, settings=Settings())

    # The frozen clock makes the window-bearing command byte-identical across
    # two independent recordings — a drifting wall-clock would change the
    # rendered `window_start`/`window_end` and break the match key.
    assert fixture_a.to_json() == fixture_b.to_json()
    main = [c["cmd"] for c in fixture_a.commands if "since=" in c["cmd"]]
    assert main, fixture_a.commands
    assert "2024-01-01 00:00:00" in main[0]
    assert "2023-12-31 23:00:00" in main[0]


async def test_output_scrubber_freezes_nondeterministic_timestamp() -> None:
    manifest = _make_manifest(command="echo now; query")
    out = ExecResult(
        exit_code=0,
        stdout='{"now": "2026-06-05T12:34:56Z", "results": []}',
        stderr="",
        duration_seconds=0.0,
        timed_out=False,
    )
    target = _ScriptedTarget("rec", [("query", out)])

    scrubber = (re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"), "FROZEN_TS")
    fixture = await record_fixture(manifest, target, settings=Settings(), scrubbers=[scrubber])

    main = [c for c in fixture.commands if "query" in c["cmd"]]
    assert main
    assert "FROZEN_TS" in main[0]["stdout"]
    assert "2026-06-05T12:34:56Z" not in main[0]["stdout"]


# --------------------------------------------------------------------------- #
# 5. the recorder refuses to bless a failed run as a fixture (fail-loud)
# --------------------------------------------------------------------------- #


async def test_record_rejects_failed_run_by_default() -> None:
    """A run whose status != ``ok`` must NOT be recorded by default — the
    recorder raises so a broken-backend / parse-error capture never gets
    committed as a healthy baseline fixture (Authoring Contract rule 8).
    """

    manifest = _make_manifest(command="echo bad; boom")
    # Non-zero exit + non-JSON stdout → the runner yields status=exception.
    boom = ExecResult(
        exit_code=1, stdout="", stderr="backend down", duration_seconds=0.0, timed_out=False
    )
    target = _ScriptedTarget("rec", [("boom", boom)])

    with pytest.raises(RuntimeError, match="refusing to record fixture"):
        await record_fixture(manifest, target, settings=Settings())


async def test_record_allows_failed_run_with_opt_in() -> None:
    """``allow_failed=True`` is the explicit escape hatch for failure-path
    fixtures (where a non-zero / non-JSON run is the whole point)."""

    manifest = _make_manifest(command="echo bad; boom")
    boom = ExecResult(
        exit_code=1, stdout="", stderr="backend down", duration_seconds=0.0, timed_out=False
    )
    target = _ScriptedTarget("rec", [("boom", boom)])

    fixture = await record_fixture(manifest, target, settings=Settings(), allow_failed=True)
    main = [c for c in fixture.commands if "boom" in c["cmd"]]
    assert main
    assert main[0]["exit_code"] == 1
    assert main[0]["stdout"] == ""


# --------------------------------------------------------------------------- #
# 6. impersonation is fail-loud — supported types pass through, others raise
# --------------------------------------------------------------------------- #


async def test_docker_target_impersonates_docker() -> None:
    """A ``type=="docker"`` target records a fixture with ``impersonate ==
    "docker"`` (not silently coerced to ``"local"``)."""

    manifest = _make_manifest(command="echo '{\"results\": []}'", targets=["docker"])
    target = _TypedScriptedTarget(
        "rec",
        [
            (
                "echo",
                ExecResult(
                    exit_code=0,
                    stdout='{"results": []}\n',
                    stderr="",
                    duration_seconds=0.0,
                    timed_out=False,
                ),
            )
        ],
        target_type="docker",
    )

    fixture = await record_fixture(manifest, target, settings=Settings())

    assert fixture.impersonate == "docker"


async def test_unsupported_target_type_raises() -> None:
    """An unsupported target type (e.g. ``k8s``) fails loud rather than being
    silently coerced into a mislabelled ``"local"`` fixture.

    The manifest declares ``targets=["k8s"]`` via ``model_copy`` (bypassing
    the ``Literal["local","ssh","docker"]`` validator) so the runner preflight's
    ``target.type in manifest.targets`` gate passes and execution reaches the
    recorder's own impersonation guard — the line under test.
    """

    base = _make_manifest(command="echo '{\"results\": []}'")
    manifest = base.model_copy(update={"targets": ["k8s"]})
    target = _TypedScriptedTarget(
        "rec",
        [
            (
                "echo",
                ExecResult(
                    exit_code=0,
                    stdout='{"results": []}\n',
                    stderr="",
                    duration_seconds=0.0,
                    timed_out=False,
                ),
            )
        ],
        target_type="k8s",
    )

    with pytest.raises(ConfigError) as exc_info:
        await record_fixture(manifest, target, settings=Settings())
    assert exc_info.value.kind == "recorder_unsupported_target_type"
