"""Cross-inspector acceptance for the ``replication-inspector-contract`` spec.

Independent crosscheck enumerating every delivered replication inspector
(``redis.replication_lag`` + ``mysql.replication_lag``). This file is NOT part
of the single-instance ``service-inspector-contract`` cohort
(``test_service_contract_crosscheck.py`` keeps its 11 / 6 manifest counts frozen).

Coverage map (design D-6 / W-5):

  * **D-6a — inherited single-instance contract items** (mechanically re-run per
    replication inspector, not merely documented):
    - Injection safety: schema ``pattern`` rejects malicious payloads before the
      collector command is rendered or executed.
    - Secret remap: per-DB ``HOSTLENS_*`` → client-native env via shell
      ``${...}`` expansion; never in argv or Jinja2 ``{{ }}``.
    - No target forking: same command serves local and ssh.
    - Timeout discipline: client connect-timeout token < ``collect.timeout_seconds``.
    - Output shape: aggregate scalar object (no array top-level field).
    - ``targets == ["local", "ssh"]``.

  * **D-6b — replication-specific items**:
    - Normalized triple in ``output_schema``; ``lag_seconds`` type ``[integer, "null"]``.
    - ``description`` declares the DB's lag semantic class (``link_freshness`` /
      ``apply_lag``).
    - Three-state replication health by-finding (unconfigured / link-down / lag).
    - Parameter names avoid multi-instance substrings.
    - Two semantic-abnormal fixtures per DB (names differ: redis ``link_stale``,
      mysql ``lagging``) exist, are valid JSON, and are semantically distinct.
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


# --------------------------------------------------------------------------- #
# Replication cohort enumeration (W-5).
# --------------------------------------------------------------------------- #

_REPLICATION_MANIFESTS: dict[str, Path] = {
    "redis.replication_lag": _builtin_root() / "redis" / "replication_lag.yaml",
    "mysql.replication_lag": _builtin_root() / "mysql" / "replication_lag.yaml",
    "postgres.replication_lag": _builtin_root() / "postgres" / "replication_lag.yaml",
}
_REPLICATION_IDS = sorted(_REPLICATION_MANIFESTS)
_REPLICATION_ITEMS = sorted(_REPLICATION_MANIFESTS.items())

_REPL_FIXTURE_DIR: dict[str, Path] = {
    "redis.replication_lag": Path(__file__).parent / "fixtures" / "redis_replication_lag",
    "mysql.replication_lag": Path(__file__).parent / "fixtures" / "mysql_replication_lag",
    "postgres.replication_lag": Path(__file__).parent / "fixtures" / "postgres_replication_lag",
}
_SEMANTIC_ABNORMAL_FIXTURES: dict[str, tuple[str, str]] = {
    # (link_down_fixture, lag/stale_fixture) — link_healthy False vs True
    "redis.replication_lag": ("link_down.json", "link_stale.json"),
    "mysql.replication_lag": ("link_down.json", "lagging.json"),
    "postgres.replication_lag": ("link_down.json", "lagging.json"),
}

# Per-DB connection / secret / timeout / replay expectations.
_REPLICATION_DB_CONFIG: dict[str, dict[str, Any]] = {
    "redis.replication_lag": {
        "secret_env": "HOSTLENS_REDIS_PASSWORD",
        "client_native_env": "REDISCLI_AUTH",
        "forbidden_flags": ("-a ",),
        "timeout_token": "-t 5",
        "timeout_value": 5,
        "semantic_class": "link_freshness",
        "required_binary": "redis-cli",
        "benign_host": "redis.internal",
        "conn_refused_port": 6390,
        "run_params": {},
        "replay_params": {},
    },
    "mysql.replication_lag": {
        "secret_env": "HOSTLENS_MYSQL_PWD",
        "client_native_env": "MYSQL_PWD",
        "forbidden_flags": (" -p",),
        "timeout_token": "--connect-timeout=5",
        "timeout_value": 5,
        "semantic_class": "apply_lag",
        "required_binary": "mysql",
        "benign_host": "db.internal",
        "conn_refused_port": 13399,
        "run_params": {"user": "mon"},
        "replay_params": {"user": "mon"},
    },
    "postgres.replication_lag": {
        "secret_env": "HOSTLENS_POSTGRES_PASSWORD",
        "client_native_env": "PGPASSWORD",
        # postgres routes the secret via the PGPASSWORD env (never -W / inline).
        "forbidden_flags": ("-W", "--password"),
        "timeout_token": "PGCONNECT_TIMEOUT=5",
        "timeout_value": 5,
        "semantic_class": "apply_lag",
        "required_binary": "psql",
        "benign_host": "pg.internal",
        "conn_refused_port": 15439,
        "run_params": {"user": "postgres"},
        "replay_params": {"user": "postgres"},
    },
}

_SEMANTIC_ABNORMAL_ITEMS: list[tuple[str, str]] = [
    (inspector, fixture)
    for inspector in _REPLICATION_IDS
    for fixture in _SEMANTIC_ABNORMAL_FIXTURES[inspector]
]
_SEMANTIC_ABNORMAL_IDS = [
    f"{inspector}/{fixture}" for inspector, fixture in _SEMANTIC_ABNORMAL_ITEMS
]


def _db_config(name: str) -> dict[str, Any]:
    return _REPLICATION_DB_CONFIG[name]


def _fixture_dir(name: str) -> Path:
    return _REPL_FIXTURE_DIR[name]


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
def _replication_secret_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOSTLENS_REDIS_PASSWORD", "")
    monkeypatch.setenv("HOSTLENS_MYSQL_PWD", "p w*d")
    monkeypatch.setenv("HOSTLENS_POSTGRES_PASSWORD", "p w*d")


# --------------------------------------------------------------------------- #
# Count guards (non-vacuous: empty dict / missing manifests cannot pass).
# --------------------------------------------------------------------------- #


def test_replication_manifests_non_empty_before_parametrize() -> None:
    assert len(_REPLICATION_MANIFESTS) > 0, "replication cohort dict must not be empty"


def test_replication_manifests_count_frozen() -> None:
    assert len(_REPLICATION_MANIFESTS) == 3, sorted(_REPLICATION_MANIFESTS)


def test_replication_manifest_paths_exist() -> None:
    assert len(_REPLICATION_MANIFESTS) > 0
    for name, path in _REPLICATION_MANIFESTS.items():
        assert path.is_file(), f"{name}: manifest missing at {path}"


def test_at_least_two_semantic_abnormal_fixtures_mapped() -> None:
    assert len(_SEMANTIC_ABNORMAL_ITEMS) >= 6, _SEMANTIC_ABNORMAL_ITEMS


# --------------------------------------------------------------------------- #
# D-6a: inherited single-instance contract items.
# --------------------------------------------------------------------------- #


class TestInheritedSingleInstanceContract:
    """Design D-6a — re-run inherited service-inspector-contract items."""

    @pytest.mark.parametrize("name,manifest_path", _REPLICATION_ITEMS, ids=_REPLICATION_IDS)
    def test_manifest_loads_cleanly(self, name: str, manifest_path: Path) -> None:
        manifest = load_manifest(manifest_path)
        assert manifest.name == name

    @pytest.mark.parametrize("name,manifest_path", _REPLICATION_ITEMS, ids=_REPLICATION_IDS)
    def test_targets_local_and_ssh(self, name: str, manifest_path: Path) -> None:
        manifest = load_manifest(manifest_path)
        assert manifest.targets == ["local", "ssh"]

    @pytest.mark.parametrize("name,manifest_path", _REPLICATION_ITEMS, ids=_REPLICATION_IDS)
    def test_string_params_carry_a_pattern(self, name: str, manifest_path: Path) -> None:
        """Every string parameter that flows into the command carries a ``pattern``."""

        manifest = load_manifest(manifest_path)
        props = manifest.parameters.get("properties", {})
        for pname, spec in props.items():
            if spec.get("type") == "string":
                assert "pattern" in spec, f"{name}: string param {pname!r} lacks a pattern"

    @pytest.mark.parametrize("name,manifest_path", _REPLICATION_ITEMS, ids=_REPLICATION_IDS)
    @pytest.mark.parametrize(
        "label,payload", _INJECTION_PAYLOADS, ids=[p[0] for p in _INJECTION_PAYLOADS]
    )
    async def test_injection_payload_rejected_before_command(
        self,
        name: str,
        manifest_path: Path,
        label: str,
        payload: str,
    ) -> None:
        """A malicious ``host`` value is rejected by the schema ``pattern`` before
        the collector command is ever rendered or run."""

        manifest = load_manifest(manifest_path)
        target = _ProbeOnlyTarget(allow_collector=False)
        params = {"host": payload, **_db_config(name)["run_params"]}

        result = await _runner().run(
            manifest,
            target,  # type: ignore[arg-type]
            parameters=params,
        )

        assert result.status == "exception", (name, label, payload)
        assert result.error is not None
        assert result.error.startswith("parameter_validation_failed"), result.error
        assert result.findings == []
        assert target.last_collector is None

    @pytest.mark.parametrize("name,manifest_path", _REPLICATION_ITEMS, ids=_REPLICATION_IDS)
    async def test_benign_host_rides_sh_filter(self, name: str, manifest_path: Path) -> None:
        """Positive control: a pattern-valid host is interpolated through
        ``shlex.quote`` (the ``| sh`` filter)."""

        cfg = _db_config(name)
        benign = cfg["benign_host"]
        manifest = load_manifest(manifest_path)
        target = _ProbeOnlyTarget(allow_collector=True, collector_stdout=_UNCONFIGURED_OK_STDOUT)
        params = {"host": benign, **cfg["run_params"]}

        result = await _runner().run(
            manifest,
            target,  # type: ignore[arg-type]
            parameters=params,
        )

        assert result.status == "ok", result.error
        assert target.last_collector is not None
        assert shlex.quote(benign) in target.last_collector, (
            name,
            benign,
            target.last_collector,
        )

    @pytest.mark.parametrize("name,manifest_path", _REPLICATION_ITEMS, ids=_REPLICATION_IDS)
    def test_secret_remap_not_in_argv(self, name: str, manifest_path: Path) -> None:
        """Per-DB secret is remapped to the client-native env via shell ``${...}``
        expansion — never in argv or Jinja2 ``{{ }}``."""

        cfg = _db_config(name)
        manifest = load_manifest(manifest_path)
        cmd = manifest.collect.command
        secret_env = cfg["secret_env"]
        native_env = cfg["client_native_env"]

        assert manifest.secrets == [secret_env]
        assert native_env in cmd
        for forbidden in cfg["forbidden_flags"]:
            assert forbidden not in cmd, f"{name}: forbidden argv flag {forbidden!r}"

        for secret in manifest.secrets:
            assert secret.startswith("HOSTLENS_"), secret
            assert secret in cmd
            assert f"${{{secret}" in cmd, "secret must be a shell ${...} expansion"

            jinja_blocks = re.findall(r"\{\{.*?\}\}", cmd, flags=re.DOTALL)
            for block in jinja_blocks:
                assert secret not in block, (
                    f"secret {secret!r} must not be `{{{{ }}}}`-interpolated (found in {block!r})"
                )

    @pytest.mark.parametrize("name,manifest_path", _REPLICATION_ITEMS, ids=_REPLICATION_IDS)
    def test_collector_command_has_no_target_type_branch(
        self, name: str, manifest_path: Path
    ) -> None:
        """The collector command must not branch on the target TYPE."""

        cmd = load_manifest(manifest_path).collect.command
        for forbidden in ("target.type", "{{ target", "$TARGET", "${TARGET", "TARGET_TYPE"):
            assert forbidden not in cmd, f"{name}: per-target fork token {forbidden!r} present"

    @pytest.mark.parametrize("name,manifest_path", _REPLICATION_ITEMS, ids=_REPLICATION_IDS)
    def test_declares_timeout_seconds(self, name: str, manifest_path: Path) -> None:
        manifest = load_manifest(manifest_path)
        timeout = manifest.collect.timeout_seconds
        assert timeout is not None and timeout > 0

    @pytest.mark.parametrize("name,manifest_path", _REPLICATION_ITEMS, ids=_REPLICATION_IDS)
    def test_client_timeout_strictly_smaller_than_collect_timeout(
        self, name: str, manifest_path: Path
    ) -> None:
        cfg = _db_config(name)
        manifest = load_manifest(manifest_path)
        timeout = manifest.collect.timeout_seconds
        assert timeout is not None
        cmd = manifest.collect.command
        token, value = cfg["timeout_token"], cfg["timeout_value"]
        assert token in cmd, f"{name}: connect-timeout token {token!r} absent"
        assert value < timeout, (name, value, timeout)

    @pytest.mark.parametrize("name,manifest_path", _REPLICATION_ITEMS, ids=_REPLICATION_IDS)
    def test_output_is_aggregate_scalar(self, name: str, manifest_path: Path) -> None:
        """Output is a pure aggregate scalar object — no array top-level field."""

        manifest = load_manifest(manifest_path)
        schema = manifest.output_schema
        assert schema.get("type") == "object"
        assert _array_top_keys(schema) == []
        for field, spec in schema.get("properties", {}).items():
            ftype = spec.get("type")
            if isinstance(ftype, list):
                assert "array" not in ftype, f"{name}: field {field!r} is an array"
            else:
                assert ftype != "array", f"{name}: field {field!r} is an array"


# --------------------------------------------------------------------------- #
# D-6b: replication-specific contract items.
# --------------------------------------------------------------------------- #


class TestReplicationSpecificContract:
    """Design D-6b — replication-inspector-contract items."""

    @pytest.mark.parametrize("name,manifest_path", _REPLICATION_ITEMS, ids=_REPLICATION_IDS)
    def test_output_schema_normalized_triple(self, name: str, manifest_path: Path) -> None:
        manifest = load_manifest(manifest_path)
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

    @pytest.mark.parametrize("name,manifest_path", _REPLICATION_ITEMS, ids=_REPLICATION_IDS)
    def test_description_declares_lag_semantic_class(self, name: str, manifest_path: Path) -> None:
        manifest = load_manifest(manifest_path)
        semantic_class = _db_config(name)["semantic_class"]
        assert semantic_class in manifest.description

    @pytest.mark.parametrize("name,manifest_path", _REPLICATION_ITEMS, ids=_REPLICATION_IDS)
    def test_findings_cover_three_state_by_finding(self, name: str, manifest_path: Path) -> None:
        """Three-state replication health: unconfigured emits no finding; link-down
        is critical; lag is warn/critical by lag thresholds."""

        manifest = load_manifest(manifest_path)
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
                f"{name}: finding {when!r} could fire without replication_configured guard"
            )

    @pytest.mark.parametrize("name,manifest_path", _REPLICATION_ITEMS, ids=_REPLICATION_IDS)
    def test_no_multi_instance_param_substrings(self, name: str, manifest_path: Path) -> None:
        """Parameter names must not contain multi-instance substrings."""

        manifest = load_manifest(manifest_path)
        props = manifest.parameters.get("properties", {})
        for pname in props:
            for forbidden in _FORBIDDEN_MULTI_INSTANCE_SUBSTRINGS:
                assert forbidden not in pname, (
                    f"{name}: multi-instance substring {forbidden!r} in param {pname!r}"
                )

    @pytest.mark.parametrize(
        "inspector,fixture_name", _SEMANTIC_ABNORMAL_ITEMS, ids=_SEMANTIC_ABNORMAL_IDS
    )
    def test_semantic_abnormal_fixture_exists_and_valid_json(
        self, inspector: str, fixture_name: str
    ) -> None:
        """Semantic-abnormal fixtures for link-down and lag/stale must exist and
        parse as valid JSON (recorded by the orchestrator separately)."""

        path = _fixture_dir(inspector) / fixture_name
        assert path.is_file(), f"{inspector}: fixture missing: {path}"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)

    @pytest.mark.parametrize("name,manifest_path", _REPLICATION_ITEMS, ids=_REPLICATION_IDS)
    async def test_semantic_abnormal_fixtures_trigger_at_defaults_and_are_distinct(
        self, name: str, manifest_path: Path
    ) -> None:
        """D-6b: the two semantic-abnormal fixtures are SEMANTICALLY DISTINCT and
        BOTH trigger a critical at the manifest DEFAULT thresholds — not merely
        valid JSON. The link-down fixture has ``link_healthy=false``; the
        lag/stale fixture has ``link_healthy=true`` with ``lag_seconds`` at or
        above the default critical threshold."""

        manifest = load_manifest(manifest_path)
        crit_default = manifest.parameters["properties"]["critical_seconds"]["default"]
        cfg = _db_config(name)
        replay_params = cfg["replay_params"]
        fixtures = _fixture_dir(name)
        link_down_name, lag_name = _SEMANTIC_ABNORMAL_FIXTURES[name]

        down = ReplayTarget("replrec", fixture=fixtures / link_down_name)
        down_result = await _runner().run(manifest, down, replay_params or None)
        assert down.misses == []
        assert down_result.status == "ok"
        assert down_result.output["replication_configured"] is True
        assert down_result.output["link_healthy"] is False
        assert [f.severity for f in down_result.findings] == ["critical"]
        assert "link down" in down_result.findings[0].message.lower()

        lag = ReplayTarget("replrec", fixture=fixtures / lag_name)
        lag_result = await _runner().run(manifest, lag, replay_params or None)
        assert lag.misses == []
        assert lag_result.status == "ok"
        assert lag_result.output["link_healthy"] is True
        assert lag_result.output["lag_seconds"] is not None
        assert lag_result.output["lag_seconds"] >= crit_default
        assert [f.severity for f in lag_result.findings] == ["critical"]

        # SEMANTICALLY DISTINCT: link-down (healthy=false) vs link-up-lag (healthy=true).
        assert down_result.output["link_healthy"] != lag_result.output["link_healthy"]


class _NoBinaryTarget:
    """Stub where every ``command -v X`` probe fails (binary absent)."""

    type = "local"
    name = "no-binary-host"
    capabilities: ClassVar[set[Capability]] = {Capability.SHELL, Capability.FILE_READ}

    def __init__(self, *, required_binary: str) -> None:
        self._required_binary = required_binary

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
        raise AssertionError(
            f"collector must not run when {self._required_binary!r} is absent: {cmd!r}"
        )

    async def read_file(self, path: str) -> bytes:
        raise AssertionError(f"read_file must not be reached: {path!r}")


class TestInheritedFailureClassification:
    """D-6a: the independent crosscheck mechanically RE-RUNS the inherited service-layer
    failure three-state (requires_unmet / exception / ok) — spec req-6「复跑…失败三态…」—
    not merely the injection / secret / timeout / no-fork items."""

    @pytest.mark.parametrize("name,manifest_path", _REPLICATION_ITEMS, ids=_REPLICATION_IDS)
    async def test_missing_client_requires_unmet(self, name: str, manifest_path: Path) -> None:
        cfg = _db_config(name)
        manifest = load_manifest(manifest_path)
        result = await _runner().run(
            manifest,
            _NoBinaryTarget(required_binary=cfg["required_binary"]),  # type: ignore[arg-type]
            cfg["run_params"] or None,
        )
        assert result.status == "requires_unmet"
        assert result.findings == []
        assert any(m.startswith("bin:") for m in result.missing), result.missing

    @pytest.mark.parametrize("name,manifest_path", _REPLICATION_ITEMS, ids=_REPLICATION_IDS)
    async def test_conn_refused_exception(self, name: str, manifest_path: Path) -> None:
        cfg = _db_config(name)
        manifest = load_manifest(manifest_path)
        fixtures = _fixture_dir(name)
        params = {**cfg["replay_params"], "port": cfg["conn_refused_port"]}
        replay = ReplayTarget("replrec", fixture=fixtures / "conn_refused.json")
        result = await _runner().run(manifest, replay, params)
        assert replay.misses == []
        assert result.status == "exception"
        assert result.output == {}
        assert result.findings == []

    @pytest.mark.parametrize("name,manifest_path", _REPLICATION_ITEMS, ids=_REPLICATION_IDS)
    async def test_healthy_ok(self, name: str, manifest_path: Path) -> None:
        cfg = _db_config(name)
        manifest = load_manifest(manifest_path)
        fixtures = _fixture_dir(name)
        replay = ReplayTarget("replrec", fixture=fixtures / "healthy.json")
        result = await _runner().run(manifest, replay, cfg["replay_params"] or None)
        assert replay.misses == []
        assert result.status == "ok"
        assert result.findings == []


# --------------------------------------------------------------------------- #
# postgres-specific assertions (master-side reduction / empty-set / prereqs).
# These cover the postgres single-carrier fixtures that other DBs do not have
# (multi_replica / unconfigured / underprivileged_all) plus the documented
# prerequisites that have no other mechanical gate.
# --------------------------------------------------------------------------- #
_PG = "postgres.replication_lag"


def test_postgres_description_declares_pg_monitor_and_primary() -> None:
    """W3-6/W3-7: the postgres description MUST state the pg_monitor prerequisite
    and the 'point at primary' topology — the under-priv false-healthy and the
    topology-inversion misuse have no other mechanical gate, so the loud
    documentation is the safeguard."""
    desc = load_manifest(_REPLICATION_MANIFESTS[_PG]).description.lower()
    assert "pg_monitor" in desc, desc
    assert "primary" in desc, desc


async def test_postgres_multi_replica_reduction_nontrivial() -> None:
    """W3-1/W3-10: the frozen multi_replica fixture exercises the master-side
    reduction non-trivially — link_healthy=false comes from a non-streaming row
    (AND over a mixed set) and lag_seconds is the MAX of the distinct non-NULL
    streaming lags (>= default critical), not an identity over a single row."""
    manifest = load_manifest(_REPLICATION_MANIFESTS[_PG])
    crit = manifest.parameters["properties"]["critical_seconds"]["default"]
    target = ReplayTarget("replrec", fixture=_fixture_dir(_PG) / "multi_replica.json")
    result = await _runner().run(manifest, target, _db_config(_PG)["replay_params"])
    assert target.misses == []
    assert result.status == "ok"
    assert result.output["replication_configured"] is True
    assert result.output["link_healthy"] is False  # AND over a non-streaming row
    assert result.output["lag_seconds"] is not None
    assert result.output["lag_seconds"] >= crit  # max picked the large streaming lag
    assert [f.severity for f in result.findings] == ["critical"]
    assert "link down" in result.findings[0].message.lower()


async def test_postgres_unconfigured_empty_set_is_ok_no_finding() -> None:
    """W3-3/W3-4: empty pg_stat_replication -> (false,false,null), status ok, no
    finding (vacuous-true guard; master-side cannot distinguish single-primary
    from total standby disconnect — it does not fabricate health)."""
    manifest = load_manifest(_REPLICATION_MANIFESTS[_PG])
    target = ReplayTarget("replrec", fixture=_fixture_dir(_PG) / "unconfigured.json")
    result = await _runner().run(manifest, target, _db_config(_PG)["replay_params"])
    assert target.misses == []
    assert result.status == "ok"
    assert result.output == {
        "replication_configured": False,
        "link_healthy": False,
        "lag_seconds": None,
    }
    assert result.findings == []


async def test_postgres_underprivileged_all_is_loud_critical_not_silent() -> None:
    """W3-6/L1: a CONNECT-only account reads state columns as NULL; the collector's
    bool_and(coalesce(state,'')='streaming') maps NULL -> false -> link_healthy
    false -> critical (LOUD, prompts investigation), NOT a silent false-healthy.
    A bare bool_and(state='streaming') would ignore the NULL rows and report
    healthy — this fixture is the regression guard for the coalesce neutralisation."""
    manifest = load_manifest(_REPLICATION_MANIFESTS[_PG])
    target = ReplayTarget("replrec", fixture=_fixture_dir(_PG) / "underprivileged_all.json")
    # Recorded as the CONNECT-only role 'lowmon' (the under-privileged account),
    # so the frozen collector command renders -U lowmon — replay with that user.
    result = await _runner().run(manifest, target, {"user": "lowmon"})
    assert target.misses == []
    assert result.status == "ok"
    assert result.output["link_healthy"] is False
    assert [f.severity for f in result.findings] == ["critical"]
