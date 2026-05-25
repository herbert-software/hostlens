"""Tests for the ``hostlens inspectors`` Typer subcommand group.

Covers Group 8a tasks 10.1 / 10.2 / 10.3 of
``add-inspector-plugin-system``:

- 10.1 ``inspectors list``: builtin enumeration, ``--tag`` / ``--target-kind``
       filters, ``--json`` output, root tolerance, per-file load error
       handling.
- 10.2 ``inspectors show``: default + JSON rendering, ``inspector_not_found``
       exit 1, ``secrets`` redaction.
- 10.3                    : stdout / stderr separation (Click 8.2+ always
       separates these streams; ``CliRunner`` exposes them via
       ``result.stdout`` / ``result.stderr``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from hostlens.cli import app
from hostlens.tools.schemas.list_inspectors import InspectorSummary


@pytest.fixture
def runner() -> CliRunner:
    """Fresh CliRunner per test.

    Click >=8.2 always splits stdout / stderr; ``mix_stderr`` keyword is
    gone, so we only need a bare ``CliRunner()`` to read ``result.stderr``.
    """

    return CliRunner()


@pytest.fixture
def user_inspectors_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``Settings.inspectors_search_paths`` at a per-test tmp directory.

    Uses the documented ``HOSTLENS_INSPECTORS_SEARCH_PATHS`` env override so
    every CLI invocation under the test reads from this path. The builtin
    directory is hardcoded inside ``build_registry_from_search_paths`` and
    is therefore always present in the registry too.
    """

    user_path = tmp_path / "inspectors"
    user_path.mkdir()
    monkeypatch.setenv("HOSTLENS_INSPECTORS_SEARCH_PATHS", str(user_path))
    return user_path


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False))


def _valid_manifest_payload(
    *,
    name: str,
    tags: list[str] | None = None,
    targets: list[str] | None = None,
    secrets: list[str] | None = None,
) -> dict[str, Any]:
    """Build a minimal valid manifest dict (raw-format + no findings).

    Kept here rather than as a shared fixture because the per-field tweaks
    each test needs (different name / tag / secrets) make a function-style
    helper easier to read than parametrised fixtures.
    """

    return {
        "name": name,
        "version": "1.0.0",
        "description": f"Test inspector {name}",
        "tags": tags or [],
        "targets": targets or ["local"],
        "requires_capabilities": [],
        "requires_binaries": [],
        "privilege": "none",
        "secrets": secrets or [],
        "collect": {"command": "echo test", "timeout_seconds": 5},
        "parse": {"format": "raw"},
        "output_schema": {
            "type": "object",
            "properties": {"raw": {"type": "string"}},
            "required": ["raw"],
        },
        "findings": [],
    }


# ---------------------------------------------------------------------------
# `inspectors list`
# ---------------------------------------------------------------------------


def test_list_default_returns_builtin_inspectors(
    runner: CliRunner,
    user_inspectors_dir: Path,
) -> None:
    """No filter: stdout enumerates the two builtins in dictionary order.

    Spec §场景:无过滤显示全部 — the M1 builtin set is exactly
    ``hello.echo`` + ``system.uptime`` and the list must be sorted by
    name so prompt-cache prefixes consuming this listing stay stable.
    """

    result = runner.invoke(app, ["inspectors", "list"])
    assert result.exit_code == 0, result.stdout + result.stderr
    # Both builtins land in the rendered table.
    assert "hello.echo" in result.stdout
    assert "system.uptime" in result.stdout
    # Dictionary order: hello.echo (h) precedes system.uptime (s).
    assert result.stdout.index("hello.echo") < result.stdout.index("system.uptime")


def test_list_filter_by_tag_only_returns_matching(
    runner: CliRunner,
    user_inspectors_dir: Path,
) -> None:
    """``--tag linux`` shows only ``system.uptime`` (which has ``linux`` tag).

    Spec §场景:--tag 过滤 — ``hello.echo`` carries ``[demo, hello]`` so
    the filter excludes it; ``system.uptime`` carries ``[linux,
    performance, system]`` so it matches.
    """

    result = runner.invoke(app, ["inspectors", "list", "--tag", "linux"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "system.uptime" in result.stdout
    assert "hello.echo" not in result.stdout


def test_list_filter_by_target_kind_ssh_returns_both_builtins(
    runner: CliRunner,
    user_inspectors_dir: Path,
) -> None:
    """``--target-kind ssh`` returns both builtins.

    Both builtin manifests declare ``targets: [local, ssh]`` so an SSH
    filter must keep them both. Locks the AND semantics with the
    no-other-filter baseline before we layer ``--tag`` on top.
    """

    result = runner.invoke(app, ["inspectors", "list", "--target-kind", "ssh"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "hello.echo" in result.stdout
    assert "system.uptime" in result.stdout


def test_list_json_schema_is_stable(
    runner: CliRunner,
    user_inspectors_dir: Path,
) -> None:
    """``--json`` output is parseable + each row matches ``InspectorSummary``.

    Spec §场景:--json schema 稳定 — every row is validated through the
    Pydantic ``InspectorSummary`` model so a field-rename / extra-key
    regression in either the loader or the CLI surface fails loudly.
    """

    result = runner.invoke(app, ["inspectors", "list", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert [row["name"] for row in payload] == ["hello.echo", "system.uptime"]
    # Validate each row conforms to the locked Pydantic schema.
    for row in payload:
        InspectorSummary.model_validate(row)
        assert set(row.keys()) == {
            "name",
            "version",
            "description",
            "tags",
            "compatible_target_kinds",
        }


def test_list_allows_root(
    runner: CliRunner,
    user_inspectors_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:root 不被拒绝.

    ``inspectors list`` is read-only and must tolerate ``EUID==0`` —
    contrast ``hostlens target add`` which refuses root via
    ``_refuse_root_for_write``. There is no equivalent guard on the
    inspectors path; this test makes the absence of one explicit by
    forcing ``os.geteuid()`` to ``0`` and checking that we still get a
    success exit.
    """

    monkeypatch.setattr("os.geteuid", lambda: 0, raising=False)
    result = runner.invoke(app, ["inspectors", "list", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr


def test_list_bad_user_yaml_emits_stderr_and_exits_1(
    runner: CliRunner,
    user_inspectors_dir: Path,
) -> None:
    """Spec §场景:加载错误 exit 1 且 stderr 显示每个失败文件.

    A malformed user yaml lands in ``RegistryBuildResult.errors``; the
    CLI emits one stderr line per failure (with the file path + error
    kind) and flips the exit code to 1, while the otherwise-loaded
    builtins still appear on stdout. Silent skip is forbidden so an
    attacker can't drop a same-named manifest under the user path and
    have the operator miss the failure.
    """

    # 1 bad yaml + 2 good user manifests, plus the always-present builtins.
    bad = user_inspectors_dir / "bad.yaml"
    bad.write_text("name: [unclosed bracket")
    _write_manifest(
        user_inspectors_dir / "alpha.yaml",
        _valid_manifest_payload(name="user.alpha"),
    )
    _write_manifest(
        user_inspectors_dir / "beta.yaml",
        _valid_manifest_payload(name="user.beta"),
    )
    result = runner.invoke(app, ["inspectors", "list"])

    assert result.exit_code == 1, result.stdout + result.stderr
    # User goods + builtins on stdout.
    assert "user.alpha" in result.stdout
    assert "user.beta" in result.stdout
    assert "hello.echo" in result.stdout
    # Error line on stderr.
    assert "manifest_parse_error" in result.stderr
    assert "bad.yaml" in result.stderr


# ---------------------------------------------------------------------------
# `inspectors show`
# ---------------------------------------------------------------------------


def test_show_known_inspector_renders_default(
    runner: CliRunner,
    user_inspectors_dir: Path,
) -> None:
    """``show hello.echo`` succeeds and includes the key manifest fields.

    Default output renders a Rich key/value table; we assert the
    operator-visible fields (name / version / description / tags /
    collect.command snippet) are present in the captured text.
    """

    result = runner.invoke(app, ["inspectors", "show", "hello.echo"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "hello.echo" in result.stdout
    assert "1.0.0" in result.stdout
    # Tags rendered (Rich tables serialise the JSON-coerced value).
    assert "demo" in result.stdout
    assert "echo hello" in result.stdout


def test_show_json_is_stable_round_trip(
    runner: CliRunner,
    user_inspectors_dir: Path,
) -> None:
    """``show hello.echo --json`` round-trips back into ``InspectorManifest``.

    Locks the on-the-wire field set: any future schema change has to
    update both the Pydantic model and this assertion, surfacing
    accidental drift to callers consuming the JSON contract.
    """

    result = runner.invoke(app, ["inspectors", "show", "hello.echo", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["name"] == "hello.echo"
    assert payload["version"] == "1.0.0"
    # `secrets` is the list of declared env-var **names** — empty for
    # ``hello.echo`` because the manifest declares no secrets. The
    # type-only assertion guards against a regression where the loader
    # accidentally resolves env values into the manifest.
    assert payload["secrets"] == []
    assert payload["collect"]["command"] == "echo hello"


def test_show_unknown_inspector_exits_1_with_stderr(
    runner: CliRunner,
    user_inspectors_dir: Path,
) -> None:
    """Spec §场景:不存在的 name exit 1.

    Missing names map to ``InspectorError(kind="inspector_not_found")``
    which the CLI surfaces as a structured stderr one-liner — never a
    bare traceback.
    """

    result = runner.invoke(app, ["inspectors", "show", "does.not.exist"])
    assert result.exit_code == 1, result.stdout + result.stderr
    assert "inspector_not_found" in result.stderr


def test_show_known_inspector_with_load_errors_exits_1(
    runner: CliRunner,
    user_inspectors_dir: Path,
) -> None:
    """Show flips exit code to 1 when sibling manifests fail to load.

    Matches ``inspectors list`` semantics + design.md decision 6: a
    malformed user manifest must surface to the operator even when the
    requested-by-name inspector itself loads cleanly. Silent ignore would
    let an attacker drop a same-named manifest under the user path with
    no visible failure signal.
    """

    bad = user_inspectors_dir / "bad.yaml"
    bad.write_text("name: [unclosed bracket")

    result = runner.invoke(app, ["inspectors", "show", "hello.echo"])
    assert result.exit_code == 1, result.stdout + result.stderr
    # The requested manifest still renders to stdout.
    assert "hello.echo" in result.stdout
    # The failed sibling surfaces on stderr.
    assert "manifest_parse_error" in result.stderr
    assert "bad.yaml" in result.stderr


def test_show_redacts_secrets_to_names_only(
    runner: CliRunner,
    user_inspectors_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:secrets 字段只显示名字.

    The fixture manifest declares ``secrets: [PGPASSWORD]`` and the test
    sets ``PGPASSWORD`` to a sentinel string in the environment. After
    invoking ``show``, both stdout and stderr must contain the env-var
    **name** (so operators can see the dependency) but never the literal
    value. The schema stores only the name, so this is a structural
    invariant — the test simply asserts the CLI does not introspect
    ``os.environ`` somewhere unsafe.
    """

    sentinel = "literal-secret-do-not-leak-xyz"
    monkeypatch.setenv("PGPASSWORD", sentinel)
    _write_manifest(
        user_inspectors_dir / "pg.yaml",
        _valid_manifest_payload(name="db.pg", secrets=["PGPASSWORD"]),
    )

    result = runner.invoke(app, ["inspectors", "show", "db.pg"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "PGPASSWORD" in result.stdout
    assert sentinel not in result.stdout
    assert sentinel not in result.stderr


def test_show_json_redacts_secrets_to_names_only(
    runner: CliRunner,
    user_inspectors_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same redaction invariant under ``--json``.

    The JSON serialiser uses ``manifest.model_dump(mode='json')`` which
    walks the Pydantic schema — there is no path through which an env
    value could reach the output, but locking it here forces a future
    refactor that changes the dump strategy to keep the contract.
    """

    sentinel = "literal-secret-json-route-do-not-leak"
    monkeypatch.setenv("PGPASSWORD", sentinel)
    _write_manifest(
        user_inspectors_dir / "pg.yaml",
        _valid_manifest_payload(name="db.pg", secrets=["PGPASSWORD"]),
    )

    result = runner.invoke(app, ["inspectors", "show", "db.pg", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["secrets"] == ["PGPASSWORD"]
    assert sentinel not in result.stdout


# ---------------------------------------------------------------------------
# stderr / stdout separation
# ---------------------------------------------------------------------------


def test_stderr_carries_load_errors_stdout_carries_table(
    runner: CliRunner,
    user_inspectors_dir: Path,
) -> None:
    """Data on stdout, diagnostics on stderr.

    The bad-yaml scenario already exercises the dual-stream split; this
    test pins it down explicitly so the contract can't regress quietly
    if a future contributor flips an ``err=True`` flag or replaces a
    ``typer.echo`` call.
    """

    bad = user_inspectors_dir / "bad.yaml"
    bad.write_text("name: [unclosed")

    result = runner.invoke(app, ["inspectors", "list"])
    # Stdout has the table; stderr has the error line.
    assert "hello.echo" in result.stdout
    assert "manifest_parse_error" not in result.stdout
    assert "manifest_parse_error" in result.stderr
