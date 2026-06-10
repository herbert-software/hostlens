"""Offline snapshot tests for the `postgres.bloat_tables` SQL inspector.

These replay committed `ReplayTarget` fixtures (recorded against a real
`postgres:16` container by ``tests/inspectors/_record_postgres_bloat.py``)
through the real `InspectorRunner` — zero `psql`, zero server, deterministic.

They prove the `add-inspector-authoring-contract` rules for the SQL data shape:

  * `json_build_object('results', ...)` emits a top-level OBJECT so
    `parse.format: json` accepts it (承重墙 4) and the `results` key survives
    parameter merge (承重墙 3);
  * all bloat derivation (`dead_ratio`) is a SQL computed column — the Finding
    DSL only threshold-compares ready scalars (承重墙 1);
  * the empty case (`{"results":[]}`) yields zero findings, not a parse error;
  * the recorded fixtures carry no plaintext connection password.

Re-record with the seeded container (see the recorder module docstring):

    docker run -d --name hl-pg -e POSTGRES_PASSWORD=<throwaway-pw> postgres:16
    # seed bloatdb / healthydb / emptydb (autovacuum disabled on bloated tables)
    HOSTLENS_POSTGRES_PASSWORD=<throwaway-pw> .venv-impl/bin/python tests/inspectors/_record_postgres_bloat.py
    docker rm -f hl-pg
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

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

_FIXTURES = Path(__file__).parent / "fixtures" / "postgres_bloat_tables"


def _manifest_path() -> Path:
    pkg_file = _inspectors_pkg.__file__
    assert pkg_file is not None
    return Path(pkg_file).parent / "builtin" / "postgres" / "bloat_tables.yaml"


def _runner() -> InspectorRunner:
    return InspectorRunner(
        TargetRegistry(),
        settings=Settings(),
        logger=structlog.get_logger("postgres-bloat-test"),
    )


@pytest.fixture(autouse=True)
def _postgres_pwd_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The runner reads HOSTLENS_POSTGRES_PASSWORD from os.environ (declared in
    # `manifest.secrets`), remaps it to native PGPASSWORD via env=; ReplayTarget
    # never matches on or stores env, so the secret stays out of the fixture.
    monkeypatch.setenv("HOSTLENS_POSTGRES_PASSWORD", "test-" + "injected-pw")


async def _run(fixture: str, dbname: str, params: dict[str, Any] | None = None) -> InspectorResult:
    manifest = load_manifest(_manifest_path())
    target = ReplayTarget("rec", fixture=_FIXTURES / fixture)
    merged = {"dbname": dbname, **(params or {})}
    result = await _runner().run(manifest, target, merged)
    assert target.misses == [], target.misses
    return result


async def test_bloated_db_flags_only_over_threshold_tables() -> None:
    result = await _run("bloated.json", "bloatdb")

    assert result.status == "ok"
    assert result.name == "postgres.bloat_tables"

    assert result.output["total_tables"] == 2

    # The SQL computed `dead_ratio` reached the parsed output untouched by the DSL.
    rows = {r["table"]: r for r in result.output["results"]}
    assert rows["orders"]["n_dead_tup"] == 4000
    assert rows["orders"]["dead_ratio"] == pytest.approx(0.6667)
    assert rows["sessions"]["dead_ratio"] == pytest.approx(0.0256)

    # Only `orders` clears both the ratio (>= 0.2) and dead-tuple (>= 1000)
    # thresholds; `sessions` (ratio 0.0256) does not.
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.severity == "warning"
    assert "orders" in finding.message
    assert "4000 dead tuples" in finding.message


async def test_finding_trigger_with_lowered_thresholds() -> None:
    """Finding-trigger track: replay the SAME bloated fixture but with lowered
    threshold parameters so the otherwise-benign ``sessions`` row
    (n_dead_tup=100, dead_ratio=0.0256) now crosses both thresholds and emits a
    warning — proving finding wiring. Thresholds are Finding DSL parameters (not
    in the recorded command string), so ReplayTarget matches the same fixture.

    At lowered thresholds ``orders`` (already over) still fires too, so
    ``findings >= 2``; assert by presence, never ``len(findings) == 1``.
    """

    result = await _run(
        "bloated.json",
        "bloatdb",
        {"dead_ratio_threshold": 0.01, "dead_tuple_threshold": 10},
    )

    assert result.status == "ok"
    assert any("sessions" in f.message for f in result.findings)
    assert any("orders" in f.message for f in result.findings)
    assert len(result.findings) >= 2


async def test_truncation_top_n_of_m() -> None:
    # bloatdb has 2 user tables; max_results=1 → `LIMIT 1` keeps only the single
    # most-bloated table (orders) in `results`, while `total_tables` still reports
    # the pre-truncation count 2 — proving top-N-of-M list-shape truncation.
    result = await _run("bloated_truncated.json", "bloatdb", {"max_results": 1})

    assert result.status == "ok"
    assert len(result.output["results"]) == 1  # truncated to top-N
    assert result.output["total_tables"] == 2  # total reports pre-truncation count
    assert result.output["total_tables"] > len(result.output["results"])  # M > N


async def test_negative_max_results_rejected() -> None:
    # `max_results` has a schema lower bound (minimum: 1); a 0 or negative value is
    # rejected by parameter validation BEFORE the collector renders `LIMIT {{ ... }}`,
    # so a degenerate `LIMIT 0` / `LIMIT -1` can never reach SQL → status=exception.
    result = await _run("bloated.json", "bloatdb", {"max_results": -1})

    assert result.status == "exception"
    assert result.error is not None
    assert result.error.startswith("parameter_validation_failed"), result.error


async def test_healthy_db_yields_no_findings() -> None:
    result = await _run("healthy.json", "healthydb")

    assert result.status == "ok"
    assert result.output["total_tables"] == 1
    assert result.findings == []
    # A zero-dead-tuple table is present in the output but never flagged.
    assert result.output["results"][0]["table"] == "accounts"
    assert result.output["results"][0]["n_dead_tup"] == 0


async def test_empty_db_parses_object_not_array() -> None:
    # `coalesce(json_agg(t), '[]'::json)` inside `json_build_object` keeps the
    # empty case a top-level OBJECT `{"total_tables":0,"results":[]}` — a bare
    # `json_agg` would emit a top-level array, rejected by parse_json_not_object.
    result = await _run("empty.json", "emptydb")

    assert result.status == "ok"
    assert result.output == {"total_tables": 0, "results": []}
    assert result.findings == []


class _NoBinaryTarget:
    """Stub target where every ``command -v X`` probe fails (binary absent)."""

    type = "local"
    name = "no-binary-host"
    capabilities: ClassVar[set[Capability]] = {Capability.SHELL}

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
        raise AssertionError(f"collector must not run when psql is absent: {cmd!r}")

    async def read_file(self, path: str) -> bytes:
        raise AssertionError(f"read_file must not be reached: {path!r}")


async def test_missing_psql_binary_requires_unmet() -> None:
    """A target without the psql client → preflight requires_unmet skip."""

    manifest = load_manifest(_manifest_path())
    target = _NoBinaryTarget()

    result: InspectorResult = await _runner().run(manifest, target, {"dbname": "appdb"})  # type: ignore[arg-type]

    assert result.status == "requires_unmet"
    assert result.findings == []
    assert any(m.startswith("bin:") for m in result.missing), result.missing


async def test_legacy_pgpassword_alone_yields_requires_unmet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BREAKING regression: only the legacy ``PGPASSWORD`` env set (the new
    ``HOSTLENS_POSTGRES_PASSWORD`` absent) → the declared secret is unmet so the
    inspector honestly skips with ``status == "requires_unmet"`` rather than
    silently running with the wrong env name.
    """

    monkeypatch.delenv("HOSTLENS_POSTGRES_PASSWORD", raising=False)
    monkeypatch.setenv("PGPASSWORD", "test-" + "legacy-pw")
    manifest = load_manifest(_manifest_path())
    replay = ReplayTarget("rec", fixture=_FIXTURES / "bloated.json")

    result = await _runner().run(manifest, replay, {"dbname": "bloatdb"})

    assert result.status == "requires_unmet"
    assert result.missing == ["env:HOSTLENS_POSTGRES_PASSWORD"]
    assert result.output == {}


async def test_conn_refused_fails_loud() -> None:
    """Backend unreachable (port closed) → psql non-zero exit + empty stdout →
    parse failure → ``status == "exception"``, NOT a fabricated healthy result.
    """

    result = await _run("conn_refused.json", "emptydb")

    assert result.status == "exception"
    assert result.output == {}
    assert result.findings == []


def test_fixtures_inject_password_via_env_not_plaintext() -> None:
    """The recorded command must reference the password through the env var
    (``$HOSTLENS_POSTGRES_PASSWORD``), never embed a plaintext literal — proving
    secrets reach ``psql`` via the ``secrets_env`` mechanism, not the recorded
    command string."""

    for fixture in ("bloated.json", "healthy.json", "empty.json"):
        text = (_FIXTURES / fixture).read_text()
        # The recorded command remaps the password through the env var
        # (`PGPASSWORD="${HOSTLENS_POSTGRES_PASSWORD:-}"`), so the only password
        # reference is the env-ref form — never a plaintext literal value.
        assert "${HOSTLENS_POSTGRES_PASSWORD:-}" in text, (
            f"{fixture}: expected env-ref password injection"
        )
        assert "throwaway" not in text, f"{fixture}: plaintext password leaked"
        assert "injected-pw" not in text, f"{fixture}: plaintext password leaked"
