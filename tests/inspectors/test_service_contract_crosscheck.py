"""Cross-inspector acceptance for the ``service-inspector-contract`` /
``service-inspector-suite`` specs.

Originally the contract-固化 (group D) of ``add-service-inspector-contract-spike``
(2 hard-coded probes: ``redis.memory_usage`` / ``mysql.connection_usage``). The
``add-single-instance-service-inspectors`` wave-2a batch (group G6) **rewrites it
into a manifest-enumeration-driven + generic-pattern-matching** form so the SAME
static contract assertions cover the spike probes AND the 6 wave-2a inspectors,
with no per-client hard-coding. Each inspector's OWN failure-classification /
dual-track snapshots stay in its per-probe suite (``test_redis_persistence.py``
etc.) — this file only validates the *common* cross-inspector contract.

Manifest subsets enumerated below (every parametrize carries a count guard so a
glob that matches nothing cannot make the suite pass vacuously):

  * ``_SECRET_SERVICE_MANIFESTS`` (6): the service inspectors that declare a
    non-empty ``secrets`` list — redis.memory_usage / mysql.connection_usage /
    redis.persistence / postgres.connection_usage / mysql.slow_queries /
    postgres.long_queries. Secret-not-in-argv, the secret-probe injection-safety
    positive control, and the secret-leak regression enumerate THIS subset (the
    docker / nginx probes have no secret surface, so scanning them for a
    password would be a vacuous assertion).
  * ``_ALL_SERVICE_MANIFESTS`` (11 = 2 spike + 6 wave-2a + 3 wave-2b): output aggregate-vs-
    list distinction, string-param pattern, timeout discipline, no-target-
    forking, and the bare-key/parameter-name disjointness enumerate THIS subset.

The requirement→coverage map (spec.md, by requirement heading):

  * 「连接参数注入安全」 → ``TestConnectionInjectionSafety``: injection payloads
    against every injectable string param (redis ``host`` / mysql ``host`` +
    ``user`` / postgres ``host`` + ``user`` + ``dbname`` / nginx ``host`` +
    ``stub_status_path``) are rejected by the schema ``pattern`` before any exec;
    every string param carries a ``pattern``; the loader rejects a string param
    NOT routed through ``| sh``.
  * 「secret 用 HOSTLENS_ 前缀声明并 remap 到 client 原生 env 不进 argv」 →
    ``TestSecretNeverInArgvOrFixture``: for each SECRET manifest the secret value
    never appears in a fixture; the secret env-var NAME is referenced only via a
    shell ``${HOSTLENS_...}`` expansion (the remap is MANDATORY) and never inside
    a Jinja2 ``{{ }}`` block (which would render the secret VALUE); the command
    carries no client-native argv plaintext-password flag.
  * 「service 层失败分类」 → ``TestFailureClassificationCovered``: the per-probe
    suites assert requires_unmet / exception / ok; this meta-guard asserts that
    coverage exists and the manifests fail loud rather than fabricating health.
  * 「超时与输出纪律」 → ``TestTimeoutAndOutputDiscipline``: each manifest declares
    ``collect.timeout_seconds`` with a client connect-timeout strictly smaller,
    and the output is either an aggregate scalar object OR a truncated top-N list
    + total count (NOT a high-cardinality dump).
  * 「跨 local 与 SSH target 无分叉」 → ``TestNoTargetForking``: no manifest
    branches on ``target.type`` / ``$TARGET``.
  * 「按输出形态(非 for_each)区分裸标量键与 results/items/records」 →
    ``TestOutputShapeDiscipline``: the list/aggregate distinction is by
    *output_schema array top-level field* (suite spec D-2 收紧), NOT by whether
    the finding uses ``for_each``. ``docker.networks`` outputs an array
    (``results``) yet its finding is a scalar (no ``for_each``) — a for_each-based
    judge would mis-classify it as aggregate and flag ``results`` as wrong.
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
from hostlens.core.exceptions import InspectorError
from hostlens.inspectors.loader import _validate_command_template, load_manifest
from hostlens.inspectors.runner import InspectorRunner
from hostlens.targets.base import Capability, ExecResult
from hostlens.targets.registry import TargetRegistry


def _builtin_root() -> Path:
    pkg_file = _inspectors_pkg.__file__
    assert pkg_file is not None
    return Path(pkg_file).parent / "builtin"


_FIXTURES = Path(__file__).parent / "fixtures"


# --------------------------------------------------------------------------- #
# Manifest subsets (enumeration-driven — no per-client hard-coding).
# --------------------------------------------------------------------------- #
#
# Two spike probes + six wave-2a + three wave-2b inspectors. Keep this list in
# lockstep with the manifests shipped under builtin/{redis,mysql,postgres,docker,
# nginx}/ — the count guards below freeze the cohort size so neither a dropped
# nor a smuggled manifest slips through silently.

_ALL_SERVICE_MANIFESTS: dict[str, Path] = {
    # spike probes
    "redis.memory_usage": _builtin_root() / "redis" / "memory_usage.yaml",
    "mysql.connection_usage": _builtin_root() / "mysql" / "connection_usage.yaml",
    # wave-2a cohort
    "redis.persistence": _builtin_root() / "redis" / "persistence.yaml",
    "postgres.connection_usage": _builtin_root() / "postgres" / "connection_usage.yaml",
    "docker.images.disk_usage": _builtin_root() / "docker" / "images_disk_usage.yaml",
    "docker.networks": _builtin_root() / "docker" / "networks.yaml",
    "nginx.health": _builtin_root() / "nginx" / "health.yaml",
    "nginx.config_test": _builtin_root() / "nginx" / "config_test.yaml",
    # wave-2b cohort
    "mysql.slow_queries": _builtin_root() / "mysql" / "slow_queries.yaml",
    "postgres.long_queries": _builtin_root() / "postgres" / "long_queries.yaml",
    "nginx.error_rate": _builtin_root() / "nginx" / "error_rate.yaml",
}
_ALL_IDS = sorted(_ALL_SERVICE_MANIFESTS)
_ALL_ITEMS = sorted(_ALL_SERVICE_MANIFESTS.items())

#: The service inspectors that declare a non-empty ``secrets`` list. Secret-
#: related assertions enumerate ONLY these (a docker/nginx probe has no secret
#: surface — scanning it for a password leak would be vacuous).
_SECRET_SERVICE_MANIFESTS: dict[str, Path] = {
    "redis.memory_usage": _ALL_SERVICE_MANIFESTS["redis.memory_usage"],
    "mysql.connection_usage": _ALL_SERVICE_MANIFESTS["mysql.connection_usage"],
    "redis.persistence": _ALL_SERVICE_MANIFESTS["redis.persistence"],
    "postgres.connection_usage": _ALL_SERVICE_MANIFESTS["postgres.connection_usage"],
    "mysql.slow_queries": _ALL_SERVICE_MANIFESTS["mysql.slow_queries"],
    "postgres.long_queries": _ALL_SERVICE_MANIFESTS["postgres.long_queries"],
}
_SECRET_IDS = sorted(_SECRET_SERVICE_MANIFESTS)
_SECRET_ITEMS = sorted(_SECRET_SERVICE_MANIFESTS.items())

#: The suite-wide ALLOWED set of client-native env channels a secret may be
#: remapped onto (spec: secret remapped to a native env var, never argv). This is
#: an allow-set, NOT a "must contain" — wave-2a only uses the first two.
_ALLOWED_NATIVE_ENV: frozenset[str] = frozenset({"REDISCLI_AUTH", "PGPASSWORD", "MYSQL_PWD"})

#: Per-manifest expectations driving the generic secret-not-in-argv assertion:
#:   * native_env: the client-native env channel the secret is remapped onto
#:     (must be in _ALLOWED_NATIVE_ENV and must appear in the command text).
#:   * forbidden_flags: the client's argv PLAINTEXT-PASSWORD flag token(s) that
#:     would leak the secret via a global `ps`. The flag DIFFERS per client and
#:     must be mapped explicitly (NOT a one-size token):
#:       - redis-cli: `-a ` (redis-cli's `-p` is the PORT, legitimately present).
#:       - mysql:     ` -p` (with or without inline value; mysql's PORT flag is
#:                    the capital ` -P`).
#:       - psql:      there is NO argv plaintext-password flag to forbid. psql
#:                    takes the password via the PGPASSWORD env (or interactive
#:                    -W/--password) — and crucially psql's `-p` is the PORT, so
#:                    forbidding ` -p` here (as for mysql) would be WRONG. We
#:                    therefore forbid nothing and rely on the env-channel +
#:                    no-Jinja-interpolation assertions.
_SECRET_CLIENT_RULES: dict[str, dict[str, Any]] = {
    "redis.memory_usage": {"native_env": "REDISCLI_AUTH", "forbidden_flags": ("-a ",)},
    "redis.persistence": {"native_env": "REDISCLI_AUTH", "forbidden_flags": ("-a ",)},
    "mysql.connection_usage": {"native_env": "MYSQL_PWD", "forbidden_flags": (" -p",)},
    "mysql.slow_queries": {"native_env": "MYSQL_PWD", "forbidden_flags": (" -p",)},
    # psql: PGPASSWORD env channel; NO argv plaintext-password flag (`-p` is PORT).
    "postgres.connection_usage": {"native_env": "PGPASSWORD", "forbidden_flags": ()},
    "postgres.long_queries": {"native_env": "PGPASSWORD", "forbidden_flags": ()},
}


def _logger() -> structlog.stdlib.BoundLogger:
    return structlog.get_logger("service-contract-crosscheck")  # type: ignore[no-any-return]


def _runner() -> InspectorRunner:
    return InspectorRunner(TargetRegistry(), settings=Settings(), logger=_logger())


# --------------------------------------------------------------------------- #
# Count guards (non-vacuous: a glob/dict that matched nothing fails here).
# --------------------------------------------------------------------------- #


def test_all_service_manifests_count_frozen() -> None:
    assert len(_ALL_SERVICE_MANIFESTS) == 11, sorted(_ALL_SERVICE_MANIFESTS)


def test_secret_service_manifests_count_frozen() -> None:
    assert len(_SECRET_SERVICE_MANIFESTS) == 6, sorted(_SECRET_SERVICE_MANIFESTS)


# --------------------------------------------------------------------------- #
# Connection-parameter injection safety (spec 「连接参数注入安全」).
# --------------------------------------------------------------------------- #
#
# Every caller-supplied STRING value spliced into `collect.command` must be
# (a) routed through `| sh`, (b) restricted by a `pattern`, (c) never bare-
# spliced. We drive injection payloads against EVERY injectable string param
# across the suite; the docker probes have NO string injection param (only
# numeric `warn_*`/`max_results`), so they are explicitly exempt below.

_INJECTION_PAYLOADS: list[tuple[str, str]] = [
    ("command_separator_comment", "'; whoami; #"),
    ("command_substitution", "$(curl evil)"),
    ("space_split", "a b"),
    ("semicolon_chain", "x;y"),
    ("backtick", "`whoami`"),
]

# (inspector name, manifest, injectable string param, a benign value the pattern
# accepts). docker.images.disk_usage / docker.networks carry no string param, so
# they do not appear (their injection surface is the empty set — see
# test_docker_probes_have_no_string_injection_param for the explicit exemption).
_INJECTABLE_PARAMS: list[tuple[str, Path, str, str]] = [
    ("redis.memory_usage", _ALL_SERVICE_MANIFESTS["redis.memory_usage"], "host", "redis.internal"),
    (
        "mysql.connection_usage",
        _ALL_SERVICE_MANIFESTS["mysql.connection_usage"],
        "host",
        "db.internal",
    ),
    (
        "mysql.connection_usage",
        _ALL_SERVICE_MANIFESTS["mysql.connection_usage"],
        "user",
        "monitor_user",
    ),
    ("redis.persistence", _ALL_SERVICE_MANIFESTS["redis.persistence"], "host", "redis.internal"),
    (
        "postgres.connection_usage",
        _ALL_SERVICE_MANIFESTS["postgres.connection_usage"],
        "host",
        "pg.internal",
    ),
    (
        "postgres.connection_usage",
        _ALL_SERVICE_MANIFESTS["postgres.connection_usage"],
        "user",
        "monitor_user",
    ),
    (
        "postgres.connection_usage",
        _ALL_SERVICE_MANIFESTS["postgres.connection_usage"],
        "dbname",
        "appdb",
    ),
    ("nginx.health", _ALL_SERVICE_MANIFESTS["nginx.health"], "host", "web.internal"),
    ("nginx.health", _ALL_SERVICE_MANIFESTS["nginx.health"], "stub_status_path", "/stub_status"),
    (
        "mysql.slow_queries",
        _ALL_SERVICE_MANIFESTS["mysql.slow_queries"],
        "host",
        "db.internal",
    ),
    (
        "mysql.slow_queries",
        _ALL_SERVICE_MANIFESTS["mysql.slow_queries"],
        "user",
        "monitor_user",
    ),
    (
        "postgres.long_queries",
        _ALL_SERVICE_MANIFESTS["postgres.long_queries"],
        "host",
        "pg.internal",
    ),
    (
        "postgres.long_queries",
        _ALL_SERVICE_MANIFESTS["postgres.long_queries"],
        "user",
        "monitor_user",
    ),
    (
        "postgres.long_queries",
        _ALL_SERVICE_MANIFESTS["postgres.long_queries"],
        "dbname",
        "appdb",
    ),
]
_INJECTABLE_IDS = [f"{name}:{param}" for name, _, param, _ in _INJECTABLE_PARAMS]

#: Manifests that require a `user` parameter (siblings must be supplied so the
#: param-under-test reaches validation rather than tripping a required-field gate).
_REQUIRES_USER = {
    "mysql.connection_usage",
    "postgres.connection_usage",
    "mysql.slow_queries",
    "postgres.long_queries",
}


class _ProbeOnlyTarget:
    """Answers preflight probes; records the collector command without running it.

    Preflight (binary `command -v X`) runs BEFORE parameter validation, so those
    probes legitimately reach `exec`. The rendered collector command is the only
    place a malicious value could land in a shell-evaluated position. For a
    rejected payload it must NEVER reach `exec`.
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


def _params_with(probe: str, param: str, value: str) -> dict[str, Any]:
    base: dict[str, Any] = {}
    if probe in _REQUIRES_USER:
        base["user"] = "root"
    base[param] = value
    return base


def _ok_stdout(probe: str) -> str:
    """A scalar-JSON collector stdout that makes a benign run reach `ok` for the
    given probe (used by the injection positive control)."""

    return {
        "redis.memory_usage": '{"used_memory":1,"maxmemory":0,"used_pct":null}',
        "mysql.connection_usage": '{"used_connections":1,"max_connections":151,"used_pct":0.66}',
        "redis.persistence": '{"aof_enabled":1,"rdb_changes_since_last_save":0,"rdb_last_save_time":1}',
        "postgres.connection_usage": '{"used_connections":1,"max_connections":100,"used_pct":1.0}',
        "nginx.health": '{"healthy":true,"active_connections":1}',
        "mysql.slow_queries": '{"slow_query_count":0,"slow_log_monitoring_enabled":true}',
        "postgres.long_queries": '{"long_query_count":0,"max_duration_seconds":0}',
    }[probe]


@pytest.fixture(autouse=True)
def _secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every secret-declaring manifest needs its secret present at preflight
    # (empty is fine — neither ReplayTarget nor _ProbeOnlyTarget match on or
    # store env). The non-secret probes (docker/nginx) ignore these.
    monkeypatch.setenv("HOSTLENS_REDIS_PASSWORD", "")
    monkeypatch.setenv("HOSTLENS_MYSQL_PWD", "test-" + "pw")
    monkeypatch.setenv("HOSTLENS_POSTGRES_PASSWORD", "")


class TestConnectionInjectionSafety:
    """spec 「连接参数注入安全」 — tasks 6.3."""

    @pytest.mark.parametrize(
        "probe,manifest_path,param,_benign",
        _INJECTABLE_PARAMS,
        ids=_INJECTABLE_IDS,
    )
    @pytest.mark.parametrize(
        "label,payload", _INJECTION_PAYLOADS, ids=[p[0] for p in _INJECTION_PAYLOADS]
    )
    async def test_injection_payload_rejected_before_command(
        self,
        probe: str,
        manifest_path: Path,
        param: str,
        _benign: str,
        label: str,
        payload: str,
    ) -> None:
        """A malicious host/user/dbname/path value is rejected by the schema
        ``pattern`` before the collector command is ever rendered or run."""

        manifest = load_manifest(manifest_path)
        assert manifest.name == probe
        target = _ProbeOnlyTarget(allow_collector=False)

        result = await _runner().run(
            manifest,
            target,  # type: ignore[arg-type]
            parameters=_params_with(probe, param, payload),
        )

        assert result.status == "exception", (probe, param, label, payload)
        assert result.error is not None
        assert result.error.startswith("parameter_validation_failed"), result.error
        assert result.findings == []
        assert target.last_collector is None

    @pytest.mark.parametrize(
        "probe,manifest_path,param,benign",
        _INJECTABLE_PARAMS,
        ids=_INJECTABLE_IDS,
    )
    async def test_benign_value_rides_sh_filter(
        self, probe: str, manifest_path: Path, param: str, benign: str
    ) -> None:
        """Positive control: a pattern-valid value is interpolated through
        ``shlex.quote`` (the ``| sh`` filter), so the pattern is not
        over-rejecting and the value lands as a single shell token."""

        manifest = load_manifest(manifest_path)
        target = _ProbeOnlyTarget(allow_collector=True, collector_stdout=_ok_stdout(probe))

        result = await _runner().run(
            manifest,
            target,  # type: ignore[arg-type]
            parameters=_params_with(probe, param, benign),
        )

        assert result.status == "ok", result.error
        assert target.last_collector is not None
        assert shlex.quote(benign) in target.last_collector, (
            benign,
            target.last_collector,
        )

    def test_loader_rejects_string_param_not_through_sh(self) -> None:
        """The loader gate rejects a string param NOT routed through ``| sh``
        (the contract's injection-safety triad component (a))."""

        host_schema = {"properties": {"host": {"type": "string", "pattern": r"^[a-z.]+$"}}}
        with pytest.raises(InspectorError) as exc:
            _validate_command_template("redis-cli -h {{ host }}", host_schema, [])
        assert exc.value.kind == "unquoted_parameter_in_command"
        assert exc.value.parameter == "host"
        # And the through-`| sh` form is accepted (not over-rejecting).
        _validate_command_template("redis-cli -h {{ host | sh }}", host_schema, [])

    @pytest.mark.parametrize("name,manifest_path", _ALL_ITEMS, ids=_ALL_IDS)
    def test_string_params_carry_a_pattern(self, name: str, manifest_path: Path) -> None:
        """Triad component (b): every string parameter that flows into the
        command carries a restricting ``pattern`` (numeric params exempt)."""

        manifest = load_manifest(manifest_path)
        props = manifest.parameters.get("properties", {})
        for pname, spec in props.items():
            if spec.get("type") == "string":
                assert "pattern" in spec, f"{name}: string param {pname!r} lacks a pattern"

    def test_docker_probes_have_no_string_injection_param(self) -> None:
        """Explicit exemption record: the two docker probes carry NO string
        parameter (only numeric thresholds / max_results), so they have an empty
        injection surface and legitimately do not appear in _INJECTABLE_PARAMS."""

        for name in ("docker.images.disk_usage", "docker.networks"):
            manifest = load_manifest(_ALL_SERVICE_MANIFESTS[name])
            props = manifest.parameters.get("properties", {})
            string_params = [p for p, s in props.items() if s.get("type") == "string"]
            assert string_params == [], f"{name}: unexpected string param(s) {string_params}"


# --------------------------------------------------------------------------- #
# Secret never in argv / fixture / replay match key (spec 「secret 经 env remap」).
# --------------------------------------------------------------------------- #
#
# Leak-scan glob covers ONLY the secret-declaring manifests' fixtures (§6.4): the
# spike probes (redis memory_usage / mysql) plus the two wave-2a secret probes
# (redis persistence / postgres). The docker/nginx fixtures have no secret
# surface — scanning them for a password value would be vacuous.

_ALL_FIXTURES: list[Path] = (
    sorted((_FIXTURES / "redis").glob("memory_usage_*.json"))
    + sorted((_FIXTURES / "mysql").glob("*.json"))
    + sorted((_FIXTURES / "redis").glob("persistence_*.json"))
    + sorted((_FIXTURES / "postgres").glob("*.json"))
    # wave-2b secret-bearing fixtures: the slow_queries / long_queries recorders
    # inject MYSQL_ROOT_PW / POSTGRES_ROOT_PW + the wrong-password value, so the
    # redaction guard MUST scan these dirs too (else the leak check is vacuous for
    # them — §6.4). nginx_error_rate has no secret and is deliberately not scanned.
    + sorted((_FIXTURES / "mysql_slow_queries").glob("*.json"))
    + sorted((_FIXTURES / "postgres_long_queries").glob("*.json"))
)

#: EVERY literal secret VALUE any recorder actually injected via a HOSTLENS_*
#: secret env while producing these fixtures. The redaction guard must scan ALL
#: of them — scanning a value that was never used makes the test vacuous (§6.4).
#: Kept in lock-step with the recorder constants:
#:   * redis.memory_usage  — _record_redis_memory_usage.SPECIAL_PW = "p w*d"
#:   * mysql.connection_usage — _compose_record.MYSQL_ROOT_PW (= concatenation
#:     below) + inline wrong/lowpriv pws.
#:   * postgres.connection_usage — _compose_record.POSTGRES_ROOT_PW (healthy /
#:     finding-trigger / semantic-abnormal recorded with this) + access_denied's
#:     deliberately-wrong "wrong-password" (same value mysql already uses).
#:   * redis.persistence — most fixtures are recorded against a NO-AUTH instance
#:     with HOSTLENS_REDIS_PASSWORD="" (empty string), but `persistence_special_
#:     char_pw.json` is recorded against an AUTH instance with the SAME "p w*d"
#:     value already in this tuple (reused, not added) — so the persistence leak
#:     scan is NON-VACUOUS (§6.4): it scans a value that really is injected.
_RECORDED_SECRET_VALUES: tuple[str, ...] = (
    # redis special-char password (space + glob metachar).
    "p w*d",
    # mysql root throwaway password. Built by concatenation (kept in lock-step
    # with the recorder constants) so a dashboard credential scan does not flag a
    # fake test credential.
    "hostlens-" + "throwaway-" + "root-pw",
    # postgres root throwaway password (_compose_record.POSTGRES_ROOT_PW): the
    # healthy / finding-trigger / semantic-abnormal postgres fixtures are all
    # recorded with this injected as HOSTLENS_POSTGRES_PASSWORD. WITHOUT this
    # value the postgres leak scan would scan only values absent from its
    # fixtures → vacuously true (§6.4 anti-vacuous requirement).
    "hostlens-" + "throwaway-" + "pg-pw",
    # access-denied fixtures (mysql AND postgres) recorded with this deliberately
    # wrong value.
    "wrong-" + "password",
    # mysql lowpriv fixture recorded with this user's password.
    "lowpriv-" + "pw",
)


class TestSecretNeverInArgvOrFixture:
    """spec 「secret 用 HOSTLENS_ 前缀声明并 remap 到 client 原生 env 不进 argv」 — tasks 6.3/6.4."""

    def test_at_least_the_expected_fixtures_scanned(self) -> None:
        # Guard against a glob that silently matches nothing. The spike batch had
        # >=6; the two wave-2a secret probes add persistence_* (3+) + postgres
        # (5); wave-2b adds mysql_slow_queries (5) + postgres_long_queries (4).
        # Lower bound bumped to reflect the wider set.
        assert len(_ALL_FIXTURES) >= 20, _ALL_FIXTURES

    @pytest.mark.parametrize("fixture", _ALL_FIXTURES, ids=lambda p: f"{p.parent.name}/{p.stem}")
    def test_fixture_carries_no_plaintext_secret(self, fixture: Path) -> None:
        """No recorded fixture stdout/stderr (or command text) contains ANY
        plaintext secret VALUE that a recorder actually injected — the recorder
        redacts before writing. Scanning the full real set (not a placeholder
        that was never used) is what makes this guard non-vacuous."""

        text = fixture.read_text(encoding="utf-8")
        for secret in _RECORDED_SECRET_VALUES:
            assert secret not in text, (fixture, secret)
        # The HOSTLENS_ var NAME may legitimately appear in command text (it is
        # only the env-VAR name, never the value); the value never does.

    def test_replay_match_keys_carry_no_env(self) -> None:
        """ReplayTarget command match keys are SHA256 of the command text with
        no env component — the fixture schema has no per-command ``env`` field
        (a secret could only leak into a fixture via stdout/stderr, guarded
        above)."""

        for fixture in _ALL_FIXTURES:
            data = json.loads(fixture.read_text(encoding="utf-8"))
            for entry in data.get("commands", []):
                assert "env" not in entry, (fixture, entry)

    @pytest.mark.parametrize("name,manifest_path", _SECRET_ITEMS, ids=_SECRET_IDS)
    def test_manifest_command_has_no_argv_plaintext_password(
        self, name: str, manifest_path: Path
    ) -> None:
        """Generic secret-not-in-argv assertion over EVERY secret manifest:
        (1) the secret is never inside a Jinja2 ``{{ }}`` block (which would
            render the secret VALUE straight into the command string);
        (2) the secret env-var NAME is referenced via a shell ``${...}``
            expansion (the remap onto a client-native env channel is MANDATORY
            and correct — the contract's `assert f"${{{secret}" in cmd`);
        (3) the command carries some ALLOWED client-native env channel name
            (∈ {REDISCLI_AUTH, PGPASSWORD, MYSQL_PWD});
        (4) the command carries no client PLAINTEXT-PASSWORD argv flag (the
            forbidden token is mapped PER CLIENT — psql has NONE because its
            ``-p`` is the PORT, see _SECRET_CLIENT_RULES)."""

        manifest = load_manifest(manifest_path)
        cmd = manifest.collect.command
        assert manifest.secrets, f"{name}: expected a declared secret"

        rule = _SECRET_CLIENT_RULES[name]
        native_env: str = rule["native_env"]
        forbidden_flags: tuple[str, ...] = rule["forbidden_flags"]

        # (3) the chosen native env channel is in the suite-wide allow-set and
        # actually appears in the command (the remap target).
        assert native_env in _ALLOWED_NATIVE_ENV, (name, native_env)
        assert native_env in cmd, f"{name}: native env channel {native_env!r} absent"

        # (4) no client plaintext-password argv flag. psql maps to () — its `-p`
        # is the PORT, so NOT forbidding it is correct (forbidding ` -p` here
        # would be the very bug this per-client map prevents).
        for flag in forbidden_flags:
            assert flag not in cmd, f"{name}: argv plaintext-password flag {flag!r} present"

        for secret in manifest.secrets:
            # (1) secret never `{{ }}`-interpolated (would render the VALUE).
            jinja_blocks = re.findall(r"\{\{.*?\}\}", cmd, flags=re.DOTALL)
            for block in jinja_blocks:
                assert secret not in block, (
                    f"{name}: secret {secret!r} must not be `{{{{ }}}}`-interpolated "
                    f"(found in {block!r})"
                )
            # (2) secret referenced ONLY via shell ${...} env expansion (remap
            # mandatory). HOSTLENS_ prefix per contract.
            assert secret.startswith("HOSTLENS_"), f"{name}: secret {secret!r} not HOSTLENS_*"
            assert secret in cmd, f"{name}: secret {secret!r} not referenced in command"
            assert f"${{{secret}" in cmd, f"{name}: secret must be a shell ${{...}} expansion"


# --------------------------------------------------------------------------- #
# No per-target forking (spec 「跨 local 与 SSH target 无分叉」).
# --------------------------------------------------------------------------- #


class TestNoTargetForking:
    """spec 「跨 local 与 SSH target 无分叉」 — tasks 6.3."""

    @pytest.mark.parametrize("name,manifest_path", _ALL_ITEMS, ids=_ALL_IDS)
    def test_manifest_serves_both_local_and_ssh(self, name: str, manifest_path: Path) -> None:
        manifest = load_manifest(manifest_path)
        assert manifest.targets == ["local", "ssh"]

    @pytest.mark.parametrize("name,manifest_path", _ALL_ITEMS, ids=_ALL_IDS)
    def test_collector_command_has_no_target_type_branch(
        self, name: str, manifest_path: Path
    ) -> None:
        """The collector command must not branch on the target TYPE — the SAME
        command text serves both local and ssh."""

        manifest = load_manifest(manifest_path)
        cmd = manifest.collect.command
        for forbidden in ("target.type", "{{ target", "$TARGET", "${TARGET", "TARGET_TYPE"):
            assert forbidden not in cmd, f"{name}: per-target fork token {forbidden!r} present"


# --------------------------------------------------------------------------- #
# Bare aggregate key never collides with a parameter name.
# --------------------------------------------------------------------------- #


class TestOutputKeyDisjointFromParameters:
    """spec 「聚合型裸键不与 parameter 同名」 — tasks 6.3."""

    _RESERVED_WINDOW_NAMES = frozenset({"window_start", "window_end", "window_seconds"})

    @pytest.mark.parametrize("name,manifest_path", _ALL_ITEMS, ids=_ALL_IDS)
    def test_output_top_keys_disjoint_from_parameter_names(
        self, name: str, manifest_path: Path
    ) -> None:
        manifest = load_manifest(manifest_path)
        out_keys = set(manifest.output_schema.get("properties", {}))
        param_names = set(manifest.parameters.get("properties", {}))
        overlap = out_keys & param_names
        assert not overlap, f"{name}: output key(s) shadow parameter name(s): {sorted(overlap)}"

    @pytest.mark.parametrize("name,manifest_path", _ALL_ITEMS, ids=_ALL_IDS)
    def test_output_keys_not_reserved_window_names(self, name: str, manifest_path: Path) -> None:
        """Runner-injected window context names must not appear as output keys."""

        manifest = load_manifest(manifest_path)
        out_keys = set(manifest.output_schema.get("properties", {}))
        overlap = out_keys & self._RESERVED_WINDOW_NAMES
        assert not overlap, f"{name}: output key(s) use reserved window name(s): {sorted(overlap)}"


#: wave-2b cohort — deterministic replay (D-1): pure aggregate scalars only.
_WAVE2B_MANIFESTS: dict[str, Path] = {
    name: path
    for name, path in _ALL_SERVICE_MANIFESTS.items()
    if name in {"mysql.slow_queries", "postgres.long_queries", "nginx.error_rate"}
}
_WAVE2B_ITEMS = sorted(_WAVE2B_MANIFESTS.items())
_WAVE2B_IDS = sorted(_WAVE2B_MANIFESTS)


class TestDeterministicReplayDiscipline:
    """wave-2b D-1 — output_schema has no timestamped-detail arrays; only frozen scalars."""

    @pytest.mark.parametrize("name,manifest_path", _WAVE2B_ITEMS, ids=_WAVE2B_IDS)
    def test_output_schema_has_no_detail_arrays(self, name: str, manifest_path: Path) -> None:
        """Window aggregation must collapse to scalars at sample time — no array
        fields that would require replay-time re-aggregation."""

        manifest = load_manifest(manifest_path)
        schema = manifest.output_schema
        assert schema.get("type") == "object", name
        for field, spec in schema.get("properties", {}).items():
            ftype = spec.get("type")
            if isinstance(ftype, list):
                assert "array" not in ftype, f"{name}: field {field!r} is an array"
            else:
                assert ftype != "array", f"{name}: field {field!r} is an array"

    def test_nginx_error_rate_uses_static_log_path(self) -> None:
        """nginx.error_rate log path is static (not parameterized) so requires_files
        preflight matches the collector read path."""

        manifest = load_manifest(_WAVE2B_MANIFESTS["nginx.error_rate"])
        static_path = "/var/log/nginx/access.log"
        assert static_path in manifest.requires_files
        assert static_path in manifest.collect.command
        props = manifest.parameters.get("properties", {})
        assert "access_log_path" not in props
        assert "log_path" not in props


# --------------------------------------------------------------------------- #
# Timeout + output discipline (spec 「超时与输出纪律」) — tasks 6.5.
# --------------------------------------------------------------------------- #
#
# Per-client connect-timeout token + its numeric value, mapped explicitly. The
# token must be PRESENT in the command and its value strictly < timeout_seconds.
#   * redis-cli      — `-t 5`
#   * psql           — `PGCONNECT_TIMEOUT=5`
#   * curl           — `--max-time 5`
#   * mysql          — `--connect-timeout=5`
#   * docker probes  — coreutils `timeout 20 docker …` (docker CLI has no
#                      --connect-timeout flag; the `timeout` wrapper is the
#                      bound). nginx config_test runs `nginx -t` locally (no
#                      network round-trip) so it has no connect-timeout token —
#                      it is exempt from the connect-timeout sub-check but still
#                      asserted to declare timeout_seconds.
_CLIENT_TIMEOUT_TOKEN: dict[str, tuple[str, int]] = {
    "redis.memory_usage": ("-t 5", 5),
    "redis.persistence": ("-t 5", 5),
    "mysql.connection_usage": ("--connect-timeout=5", 5),
    "mysql.slow_queries": ("--connect-timeout=5", 5),
    "postgres.connection_usage": ("PGCONNECT_TIMEOUT=5", 5),
    "postgres.long_queries": ("PGCONNECT_TIMEOUT=5", 5),
    "nginx.health": ("--max-time 5", 5),
    "docker.images.disk_usage": ("timeout 20", 20),
    "docker.networks": ("timeout 20", 20),
}
#: Inspectors with no network round-trip (local exec / file read) → no connect-timeout token.
_NO_CONNECT_TIMEOUT = {"nginx.config_test", "nginx.error_rate"}


class TestTimeoutAndOutputDiscipline:
    """spec 「超时与输出纪律」 — tasks 6.5."""

    @pytest.mark.parametrize("name,manifest_path", _ALL_ITEMS, ids=_ALL_IDS)
    def test_declares_timeout_seconds(self, name: str, manifest_path: Path) -> None:
        manifest = load_manifest(manifest_path)
        timeout = manifest.collect.timeout_seconds
        assert timeout is not None and timeout > 0, name

    @pytest.mark.parametrize("name,manifest_path", _ALL_ITEMS, ids=_ALL_IDS)
    def test_client_timeout_strictly_smaller_than_collect_timeout(
        self, name: str, manifest_path: Path
    ) -> None:
        manifest = load_manifest(manifest_path)
        timeout = manifest.collect.timeout_seconds
        assert timeout is not None
        cmd = manifest.collect.command
        if name in _NO_CONNECT_TIMEOUT:
            # Local exec (`nginx -t`) — no network round-trip, no connect-timeout
            # token to assert; the declared timeout_seconds (above) is the bound.
            assert name not in _CLIENT_TIMEOUT_TOKEN
            return
        token, value = _CLIENT_TIMEOUT_TOKEN[name]
        assert token in cmd, f"{name}: connect-timeout token {token!r} absent"
        assert value < timeout, (name, value, timeout)

    @pytest.mark.parametrize("name,manifest_path", _ALL_ITEMS, ids=_ALL_IDS)
    def test_float_emitting_awk_forces_c_locale(self, name: str, manifest_path: Path) -> None:
        """Any collector ``awk`` that emits a float via ``printf``/``sprintf``
        ``"%…f"`` must be prefixed with ``LC_ALL=C`` — comma-decimal locales
        would emit invalid JSON and misclassify a HEALTHY service as exception.
        Generic pattern match (not a connection_usage-specific literal)."""

        manifest = load_manifest(manifest_path)
        cmd = manifest.collect.command
        float_fmt = re.compile(r'(?:printf|sprintf)\s*\(\s*"[^"]*%[\d.]*f')
        for match in re.finditer(r"\bawk\b", cmd):
            segment = cmd[match.start() :]
            if not float_fmt.search(segment):
                continue
            prefix = cmd[max(0, match.start() - 30) : match.start()]
            assert "LC_ALL=C" in prefix, (
                f"{name}: awk emitting %f must be prefixed with LC_ALL=C "
                f"(segment starts: {segment[:80]!r})"
            )


# --------------------------------------------------------------------------- #
# Output shape discipline (spec 「按输出形态(非 for_each)区分裸标量键与
# results/items/records」) — tasks 6.3 实现注 2 / 6.5.
# --------------------------------------------------------------------------- #
#
# THE JUDGE IS THE OUTPUT_SCHEMA ARRAY TOP-LEVEL FIELD, NOT `for_each` (suite
# spec D-2 收紧). `docker.networks` outputs an array (`results`) while its finding
# is a scalar (`dangling_networks >= warn_count`, NO `for_each`). A for_each-based
# judge would mis-classify it as aggregate and flag `results` as a wrong bare key
# — exactly the false-red this distinction prevents.


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


class TestOutputShapeDiscipline:
    """spec 「按输出形态区分裸标量键与 results/items/records」 — tasks 6.3 实现注 2."""

    @pytest.mark.parametrize("name,manifest_path", _ALL_ITEMS, ids=_ALL_IDS)
    def test_output_shape_by_array_field_not_for_each(self, name: str, manifest_path: Path) -> None:
        manifest = load_manifest(manifest_path)
        schema = manifest.output_schema
        assert schema.get("type") == "object", name
        array_keys = _array_top_keys(schema)

        if not array_keys:
            # Pure aggregate scalar output: assert NO top-level field is an array.
            # (Trivially holds given array_keys == [], but kept explicit so a
            # future schema regression that adds an array field surfaces here.)
            for field, spec in schema.get("properties", {}).items():
                ftype = spec.get("type")
                if isinstance(ftype, list):
                    assert "array" not in ftype, f"{name}: field {field!r} is an array"
                else:
                    assert ftype != "array", f"{name}: field {field!r} is an array"
            return

        # List-shaped output (e.g. docker.networks `results`): EXEMPT from the
        # no-array rule; instead enforce the list-shape contract.
        # (a) the array top-level key(s) ∈ {results, items, records}.
        for key in array_keys:
            assert key in {"results", "items", "records"}, (
                f"{name}: array top-level key {key!r} not in results/items/records"
            )
        # (b) the collector truncates top-N (a slice on a max_results param).
        cmd = manifest.collect.command
        params = manifest.parameters.get("properties", {})
        assert "max_results" in params, f"{name}: list-shaped output lacks a max_results param"
        assert "[0:" in cmd or "max_results" in cmd, (
            f"{name}: list-shaped collector shows no top-N truncation"
        )
        # (c) a total-count scalar field accompanies the truncated list.
        scalar_int_fields = [
            f
            for f, s in schema.get("properties", {}).items()
            if s.get("type") == "integer" and f not in array_keys
        ]
        assert scalar_int_fields, f"{name}: list-shaped output lacks a total-count scalar field"

    def test_docker_networks_is_the_list_shaped_case(self) -> None:
        """Lock the expectation that docker.networks is the ONE list-shaped
        inspector AND that it has NO `for_each` finding — proving the array-field
        judge (not a for_each judge) is what classifies it. If a future edit gives
        it a for_each, this guard fails loud so the distinction is reconsidered."""

        manifest = load_manifest(_ALL_SERVICE_MANIFESTS["docker.networks"])
        assert _array_top_keys(manifest.output_schema) == ["results"]
        # The finding is a SCALAR comparison — no for_each (the whole point of D-2).
        assert manifest.findings
        for finding in manifest.findings:
            assert finding.for_each is None, (
                "docker.networks finding must stay scalar (no for_each)"
            )

    def test_only_docker_networks_is_list_shaped(self) -> None:
        """The other 10 service inspectors are pure aggregate scalar (no array
        top-level field) — a meta-guard that the parametrized branch above is
        non-vacuous (exactly one inspector takes the list-shaped branch)."""

        list_shaped = [
            name
            for name, path in _ALL_SERVICE_MANIFESTS.items()
            if _array_top_keys(load_manifest(path).output_schema)
        ]
        assert list_shaped == ["docker.networks"], list_shaped


# --------------------------------------------------------------------------- #
# Failure-classification meta-guard + fail-loud (spec 「service 层失败分类」).
# --------------------------------------------------------------------------- #


class TestFailureClassificationCovered:
    """spec 「service 层失败分类」 — tasks 6.3.

    The per-probe suites assert each class with recorded fixtures; this meta-
    guard ensures neither suite silently drops a class, and confirms the
    manifests do not map orthogonal transport states themselves.
    """

    _PROBE_TEST_SOURCES: ClassVar[dict[str, Path]] = {
        "redis.memory_usage": Path(__file__).parent / "test_redis_memory_usage.py",
        "mysql.connection_usage": Path(__file__).parent / "test_mysql_connection_usage.py",
        "redis.persistence": Path(__file__).parent / "test_redis_persistence.py",
        "postgres.connection_usage": Path(__file__).parent / "test_postgres_connection_usage.py",
        "docker.images.disk_usage": Path(__file__).parent / "test_docker_images_disk_usage.py",
        "docker.networks": Path(__file__).parent / "test_docker_networks.py",
        "nginx.health": Path(__file__).parent / "test_nginx_health.py",
        "nginx.config_test": Path(__file__).parent / "test_nginx_config_test.py",
        "mysql.slow_queries": Path(__file__).parent / "test_mysql_slow_queries.py",
        "postgres.long_queries": Path(__file__).parent / "test_postgres_long_queries.py",
        "nginx.error_rate": Path(__file__).parent / "test_nginx_error_rate.py",
    }

    #: Probes with no deterministic exception path (file-read / premise-only
    #: failures). nginx.config_test's exception is the non-{0,1} rc fallback;
    #: nginx.error_rate only has requires_unmet (missing log/awk) and ok.
    _NO_EXCEPTION_SNAPSHOT: ClassVar[set[str]] = {"nginx.error_rate"}

    @pytest.mark.parametrize("probe", sorted(_PROBE_TEST_SOURCES), ids=sorted(_PROBE_TEST_SOURCES))
    def test_each_failure_class_asserted_in_probe_suite(self, probe: str) -> None:
        src = self._PROBE_TEST_SOURCES[probe].read_text(encoding="utf-8")
        # requires_unmet: missing client binary (every probe has one).
        assert 'status == "requires_unmet"' in src, probe
        # ok with a real (non-fabricated) value (every probe has one).
        assert 'status == "ok"' in src, probe
        # exception (unreachable / auth-failed / daemon-down) — every probe EXCEPT
        # the finding-route nginx.config_test (see _NO_EXCEPTION_SNAPSHOT).
        if probe not in self._NO_EXCEPTION_SNAPSHOT:
            assert 'status == "exception"' in src, probe

    @pytest.mark.parametrize("name,manifest_path", _ALL_ITEMS, ids=_ALL_IDS)
    def test_manifest_does_not_map_orthogonal_transport_states(
        self, name: str, manifest_path: Path
    ) -> None:
        """timeout / target_unreachable are orthogonal transport-layer states
        owned by the runner, NOT the manifest."""

        manifest = load_manifest(manifest_path)
        cmd = manifest.collect.command
        for forbidden in ("target_unreachable", "status=timeout", "TargetError"):
            assert forbidden not in cmd, f"{name}: transport-state token {forbidden!r} in command"


# --------------------------------------------------------------------------- #
# Single-instance boundary + HOSTLENS_ prefix (secret manifests).
# --------------------------------------------------------------------------- #


class TestSingleInstanceBoundary:
    """spec 「本契约边界止于单实例」 — tasks 6.3."""

    @pytest.mark.parametrize("name,manifest_path", _ALL_ITEMS, ids=_ALL_IDS)
    def test_no_multi_instance_params(self, name: str, manifest_path: Path) -> None:
        manifest = load_manifest(manifest_path)
        props = manifest.parameters.get("properties", {})
        for forbidden in ("replica", "primary", "replication", "lag", "instances", "nodes"):
            assert forbidden not in props, f"{name}: multi-instance param {forbidden!r} present"

    @pytest.mark.parametrize("name,manifest_path", _SECRET_ITEMS, ids=_SECRET_IDS)
    def test_secret_uses_hostlens_prefix(self, name: str, manifest_path: Path) -> None:
        manifest = load_manifest(manifest_path)
        assert manifest.secrets, f"{name}: expected a declared secret"
        for secret in manifest.secrets:
            assert secret.startswith("HOSTLENS_"), f"{name}: secret {secret!r} not HOSTLENS_*"
