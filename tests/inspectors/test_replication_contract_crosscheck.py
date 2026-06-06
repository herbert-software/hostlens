"""Cross-inspector acceptance for the ``replication-inspector-contract`` spec.

Independent crosscheck for ``redis.replication_lag`` — the replication spike probe.
This file is NOT part of the single-instance ``service-inspector-contract`` cohort
(``test_service_contract_crosscheck.py`` keeps its 11 / 6 manifest counts frozen).

Coverage map (design D-6):

  * **D-6a — inherited single-instance contract items** (mechanically re-run, not
    merely documented):
    - Injection safety: schema ``pattern`` rejects malicious payloads before the
      collector command is rendered or executed.
    - Secret remap: ``HOSTLENS_REDIS_PASSWORD`` → ``REDISCLI_AUTH`` via shell
      ``${...}`` expansion; never in argv (``-a ``) or Jinja2 ``{{ }}``.
    - No target forking: same command serves local and ssh.
    - Timeout discipline: client ``-t 5`` < ``collect.timeout_seconds``.
    - Output shape: aggregate scalar object (no array top-level field).
    - ``targets == ["local", "ssh"]``.

  * **D-6b — replication-specific items**:
    - Normalized triple in ``output_schema``; ``lag_seconds`` type ``[integer, "null"]``.
    - ``description`` declares lag semantic class ``link_freshness``.
    - Three-state replication health by-finding (unconfigured / link-down / stale).
    - Parameter names avoid multi-instance substrings.
    - Semantic-abnormal fixtures ``link_down`` and ``link_stale`` exist and are valid JSON.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any, ClassVar

import pytest
import structlog

import hostlens.inspectors as _inspectors_pkg
from hostlens.core.config import Settings
from hostlens.inspectors.loader import load_manifest
from hostlens.inspectors.runner import InspectorRunner
from hostlens.targets.base import Capability, ExecResult
from hostlens.targets.registry import TargetRegistry
from hostlens.targets.replay import ReplayTarget

_FIXTURES = Path(__file__).parent / "fixtures" / "redis_replication_lag"

_FORBIDDEN_MULTI_INSTANCE_SUBSTRINGS = (
    "replica",
    "primary",
    "replication",
    "lag",
    "instances",
    "nodes",
)

_INJECTION_PAYLOADS: list[tuple[str, str]] = [
    ("command_separator_comment", "'; whoami; #"),
    ("command_substitution", "$(curl evil)"),
    ("space_split", "a b"),
    ("semicolon_chain", "x;y"),
    ("backtick", "`whoami`"),
]


def _builtin_root() -> Path:
    pkg_file = _inspectors_pkg.__file__
    assert pkg_file is not None
    return Path(pkg_file).parent / "builtin"


def _manifest_path() -> Path:
    return _builtin_root() / "redis" / "replication_lag.yaml"


def _manifest():
    return load_manifest(_manifest_path())


def _logger() -> structlog.stdlib.BoundLogger:
    return structlog.get_logger("replication-contract-crosscheck")  # type: ignore[no-any-return]


def _runner() -> InspectorRunner:
    return InspectorRunner(TargetRegistry(), settings=Settings(), logger=_logger())


def _array_top_keys(schema: dict[str, Any]) -> list[str]:
    """Top-level output_schema keys whose declared type is (or includes) array."""

    out: list[str] = []
    for field, spec in schema.get("properties", {}).items():
        ftype = spec.get("type")
        if isinstance(ftype, list):
            if "array" in ftype:
                out.append(field)
        elif ftype == "array":
            out.append(field)
    return out


class _ProbeOnlyTarget:
    """Answers preflight probes; records the collector command without running it.

    Preflight (binary ``command -v X``) runs BEFORE parameter validation, so those
    probes legitimately reach ``exec``. The rendered collector command is the only
    place a malicious value could land in a shell-evaluated position. For a
    rejected payload it must NEVER reach ``exec``.
    """

    type = "local"
    name = "probe-only-host"
    capabilities: ClassVar[set[Capability]] = {Capability.SHELL, Capability.FILE_READ}

    def __init__(self, *, allow_collector: bool, collector_stdout: str = "") -> None:
        self._allow_collector = allow_collector
        self._collector_stdout = collector_stdout
        self.last_collector: str | None = None

    async def exec(
        self,
        cmd: str,
        *,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        del timeout, env
        if cmd.startswith("command -v "):
            binary = cmd[len("command -v ") :].strip().strip("'\"")
            return ExecResult(
                exit_code=0,
                stdout=f"/usr/bin/{binary}\n",
                stderr="",
                duration_seconds=0.0,
                timed_out=False,
            )
        if cmd.startswith("[ -r "):
            return ExecResult(
                exit_code=0, stdout="", stderr="", duration_seconds=0.0, timed_out=False
            )
        if not self._allow_collector:
            raise AssertionError(f"collector must not run for a rejected payload: {cmd!r}")
        self.last_collector = cmd
        return ExecResult(
            exit_code=0,
            stdout=self._collector_stdout,
            stderr="",
            duration_seconds=0.0,
            timed_out=False,
        )

    async def read_file(self, path: str) -> bytes:
        raise AssertionError(f"read_file must not be reached: {path!r}")


_UNCONFIGURED_OK_STDOUT = '{"replication_configured":false,"link_healthy":false,"lag_seconds":null}'


@pytest.fixture(autouse=True)
def _redis_password_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOSTLENS_REDIS_PASSWORD", "")


# --------------------------------------------------------------------------- #
# D-6a: inherited single-instance contract items.
# --------------------------------------------------------------------------- #


class TestInheritedSingleInstanceContract:
    """Design D-6a — re-run inherited service-inspector-contract items."""

    def test_manifest_loads_cleanly(self) -> None:
        manifest = _manifest()
        assert manifest.name == "redis.replication_lag"

    def test_targets_local_and_ssh(self) -> None:
        manifest = _manifest()
        assert manifest.targets == ["local", "ssh"]

    def test_string_params_carry_a_pattern(self) -> None:
        """Every string parameter that flows into the command carries a ``pattern``."""

        manifest = _manifest()
        props = manifest.parameters.get("properties", {})
        for pname, spec in props.items():
            if spec.get("type") == "string":
                assert "pattern" in spec, f"string param {pname!r} lacks a pattern"

    @pytest.mark.parametrize(
        "label,payload", _INJECTION_PAYLOADS, ids=[p[0] for p in _INJECTION_PAYLOADS]
    )
    async def test_injection_payload_rejected_before_command(
        self, label: str, payload: str
    ) -> None:
        """A malicious ``host`` value is rejected by the schema ``pattern`` before
        the collector command is ever rendered or run."""

        manifest = _manifest()
        target = _ProbeOnlyTarget(allow_collector=False)

        result = await _runner().run(
            manifest,
            target,  # type: ignore[arg-type]
            parameters={"host": payload},
        )

        assert result.status == "exception", (label, payload)
        assert result.error is not None
        assert result.error.startswith("parameter_validation_failed"), result.error
        assert result.findings == []
        assert target.last_collector is None

    async def test_benign_host_rides_sh_filter(self) -> None:
        """Positive control: a pattern-valid host is interpolated through
        ``shlex.quote`` (the ``| sh`` filter)."""

        benign = "redis.internal"
        manifest = _manifest()
        target = _ProbeOnlyTarget(allow_collector=True, collector_stdout=_UNCONFIGURED_OK_STDOUT)

        result = await _runner().run(
            manifest,
            target,  # type: ignore[arg-type]
            parameters={"host": benign},
        )

        assert result.status == "ok", result.error
        assert target.last_collector is not None
        assert shlex.quote(benign) in target.last_collector, (
            benign,
            target.last_collector,
        )

    def test_secret_remap_not_in_argv(self) -> None:
        """``HOSTLENS_REDIS_PASSWORD`` is remapped to ``REDISCLI_AUTH`` via shell
        ``${...}`` expansion — never in argv (``-a ``) or Jinja2 ``{{ }}``."""

        manifest = _manifest()
        cmd = manifest.collect.command
        assert manifest.secrets == ["HOSTLENS_REDIS_PASSWORD"]

        assert "REDISCLI_AUTH" in cmd
        assert "-a " not in cmd

        for secret in manifest.secrets:
            assert secret.startswith("HOSTLENS_"), secret
            assert secret in cmd
            assert f"${{{secret}" in cmd, "secret must be a shell ${...} expansion"

            jinja_blocks = re.findall(r"\{\{.*?\}\}", cmd, flags=re.DOTALL)
            for block in jinja_blocks:
                assert secret not in block, (
                    f"secret {secret!r} must not be `{{{{ }}}}`-interpolated (found in {block!r})"
                )

    def test_collector_command_has_no_target_type_branch(self) -> None:
        """The collector command must not branch on the target TYPE."""

        cmd = _manifest().collect.command
        for forbidden in ("target.type", "{{ target", "$TARGET", "${TARGET", "TARGET_TYPE"):
            assert forbidden not in cmd, f"per-target fork token {forbidden!r} present"

    def test_declares_timeout_seconds(self) -> None:
        manifest = _manifest()
        timeout = manifest.collect.timeout_seconds
        assert timeout is not None and timeout > 0

    def test_client_timeout_strictly_smaller_than_collect_timeout(self) -> None:
        manifest = _manifest()
        timeout = manifest.collect.timeout_seconds
        assert timeout is not None
        cmd = manifest.collect.command
        token, value = "-t 5", 5
        assert token in cmd, f"connect-timeout token {token!r} absent"
        assert value < timeout, (value, timeout)

    def test_output_is_aggregate_scalar(self) -> None:
        """Output is a pure aggregate scalar object — no array top-level field."""

        manifest = _manifest()
        schema = manifest.output_schema
        assert schema.get("type") == "object"
        assert _array_top_keys(schema) == []
        for field, spec in schema.get("properties", {}).items():
            ftype = spec.get("type")
            if isinstance(ftype, list):
                assert "array" not in ftype, f"field {field!r} is an array"
            else:
                assert ftype != "array", f"field {field!r} is an array"


# --------------------------------------------------------------------------- #
# D-6b: replication-specific contract items.
# --------------------------------------------------------------------------- #


class TestReplicationSpecificContract:
    """Design D-6b — replication-inspector-contract items."""

    def test_output_schema_normalized_triple(self) -> None:
        manifest = _manifest()
        schema = manifest.output_schema
        props = schema.get("properties", {})
        assert set(props) == {"replication_configured", "link_healthy", "lag_seconds"}
        assert props["replication_configured"]["type"] == "boolean"
        assert props["link_healthy"]["type"] == "boolean"
        assert props["lag_seconds"]["type"] == ["integer", "null"]
        assert schema.get("required") == [
            "replication_configured",
            "link_healthy",
            "lag_seconds",
        ]

    def test_description_declares_link_freshness_semantic_class(self) -> None:
        manifest = _manifest()
        assert "link_freshness" in manifest.description

    def test_findings_cover_three_state_by_finding(self) -> None:
        """Three-state replication health: unconfigured emits no finding; link-down
        is critical; stale freshness is warn/critical by lag thresholds."""

        manifest = _manifest()
        findings = manifest.findings
        assert len(findings) == 3

        when_exprs = [f.when for f in findings]

        link_down = [w for w in when_exprs if "not link_healthy" in w]
        assert len(link_down) == 1, when_exprs
        assert "replication_configured" in link_down[0]

        critical_lag = [w for w in when_exprs if "lag_seconds >= critical_seconds" in w]
        assert len(critical_lag) == 1, when_exprs
        assert "link_healthy" in critical_lag[0]

        warn_lag = [w for w in when_exprs if "lag_seconds >= warn_seconds" in w]
        assert len(warn_lag) == 1, when_exprs
        assert "lag_seconds < critical_seconds" in warn_lag[0]

        # Unconfigured path: no finding fires when replication_configured is false.
        for when in when_exprs:
            if "replication_configured and not link_healthy" in when:
                continue
            assert "link_healthy" in when, (
                f"finding {when!r} could fire without replication_configured guard"
            )

    def test_no_multi_instance_param_substrings(self) -> None:
        """Parameter names must not contain multi-instance substrings."""

        manifest = _manifest()
        props = manifest.parameters.get("properties", {})
        for pname in props:
            for forbidden in _FORBIDDEN_MULTI_INSTANCE_SUBSTRINGS:
                assert forbidden not in pname, (
                    f"multi-instance substring {forbidden!r} in param {pname!r}"
                )

    @pytest.mark.parametrize(
        "fixture_name",
        ["link_down.json", "link_stale.json"],
        ids=["link_down", "link_stale"],
    )
    def test_semantic_abnormal_fixture_exists_and_valid_json(self, fixture_name: str) -> None:
        """Semantic-abnormal fixtures for link-down and link-stale must exist and
        parse as valid JSON (recorded by the orchestrator separately)."""

        path = _FIXTURES / fixture_name
        assert path.is_file(), f"fixture missing: {path}"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)

    async def test_semantic_abnormal_fixtures_trigger_at_defaults_and_are_distinct(self) -> None:
        """D-6b: ``link_down`` and ``link_stale`` are TWO SEMANTICALLY DISTINCT
        semantic-abnormal fixtures that BOTH trigger a critical at the manifest DEFAULT
        thresholds — not merely valid JSON. ``link_down`` is a broken link
        (``link_healthy=false``); ``link_stale`` is a stale-but-UP link
        (``link_healthy=true``, ``lag_seconds>=critical_seconds``). A healthy-stdout
        fixture, or two identical states, would fail here."""

        manifest = _manifest()
        crit_default = manifest.parameters["properties"]["critical_seconds"]["default"]

        down = ReplayTarget("replrec", fixture=_FIXTURES / "link_down.json")
        down_result = await _runner().run(manifest, down, None)
        assert down.misses == []
        assert down_result.status == "ok"
        assert down_result.output["replication_configured"] is True
        assert down_result.output["link_healthy"] is False
        assert [f.severity for f in down_result.findings] == ["critical"]
        assert "link down" in down_result.findings[0].message.lower()

        stale = ReplayTarget("replrec", fixture=_FIXTURES / "link_stale.json")
        stale_result = await _runner().run(manifest, stale, None)
        assert stale.misses == []
        assert stale_result.status == "ok"
        assert stale_result.output["link_healthy"] is True
        assert stale_result.output["lag_seconds"] is not None
        assert stale_result.output["lag_seconds"] >= crit_default
        assert [f.severity for f in stale_result.findings] == ["critical"]

        # SEMANTICALLY DISTINCT: link-down (healthy=false) vs link-up-stale (healthy=true).
        assert down_result.output["link_healthy"] != stale_result.output["link_healthy"]


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
        raise AssertionError(f"collector must not run when redis-cli is absent: {cmd!r}")

    async def read_file(self, path: str) -> bytes:
        raise AssertionError(f"read_file must not be reached: {path!r}")


class TestInheritedFailureClassification:
    """D-6a: the independent crosscheck mechanically RE-RUNS the inherited service-layer
    failure three-state (requires_unmet / exception / ok) — spec req-6「复跑…失败三态…」—
    not merely the injection / secret / timeout / no-fork items."""

    async def test_missing_redis_cli_requires_unmet(self) -> None:
        result = await _runner().run(_manifest(), _NoBinaryTarget(), None)  # type: ignore[arg-type]
        assert result.status == "requires_unmet"
        assert result.findings == []
        assert any(m.startswith("bin:") for m in result.missing), result.missing

    async def test_conn_refused_exception(self) -> None:
        replay = ReplayTarget("replrec", fixture=_FIXTURES / "conn_refused.json")
        result = await _runner().run(_manifest(), replay, {"port": 6390})
        assert replay.misses == []
        assert result.status == "exception"
        assert result.output == {}
        assert result.findings == []

    async def test_healthy_ok(self) -> None:
        replay = ReplayTarget("replrec", fixture=_FIXTURES / "healthy.json")
        result = await _runner().run(_manifest(), replay, None)
        assert replay.misses == []
        assert result.status == "ok"
        assert result.findings == []
