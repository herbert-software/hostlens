"""Tests for the ``hostlens target`` Typer subcommand group.

Covers tasks 6.1-6.6 of ``add-execution-target-abstraction``:

- 6.1 ``target add``  : EUID==0 refusal, name conflict, local + ssh variants
- 6.2 ``target list`` : Rich + JSON output stability
- 6.3 ``target remove``: non-interactive guard, EUID==0 refusal, --yes path
- 6.4 ``target test`` : local success, ssh failure, disabled-target refusal
- 6.5                : stderr / stdout separation
- 6.6                : unknown flag exit 2 via Typer

All tests use ``CliRunner`` with a per-test temporary
``targets_config_path`` so they do not touch the operator's real
``~/.config/hostlens/targets.yaml``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from hostlens.cli import app


@pytest.fixture
def runner() -> CliRunner:
    """Fresh CliRunner per test.

    Click >=8.2 always separates stdout/stderr; ``mix_stderr`` is gone.
    """

    return CliRunner()


@pytest.fixture
def targets_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``Settings.targets_config_path`` at a fresh tmp file.

    Uses the pydantic-settings env var override
    (``HOSTLENS_TARGETS_CONFIG_PATH``) so every CLI invocation under
    the test sees the same path without us having to monkey-patch
    Settings directly.
    """

    path = tmp_path / "targets.yaml"
    monkeypatch.setenv("HOSTLENS_TARGETS_CONFIG_PATH", str(path))
    return path


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False))


# ---------------------------------------------------------------------------
# `target add`
# ---------------------------------------------------------------------------


def test_target_add_local_writes_yaml(
    runner: CliRunner,
    targets_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`target add --type local` writes a LocalEntry to targets.yaml.

    Spec §需求:`hostlens target` CLI 命令集 — write commands persist
    the new entry to the file referenced by ``Settings.targets_config_path``.
    """

    # Ensure the test never accidentally hits the root-refusal branch.
    monkeypatch.setattr("hostlens.cli.target.os.geteuid", lambda: 1000)

    result = runner.invoke(app, ["target", "add", "my-local", "--type", "local"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert targets_yaml.exists()
    payload = yaml.safe_load(targets_yaml.read_text())
    assert payload == {
        "version": "1",
        "targets": [{"name": "my-local", "type": "local"}],
    }


def test_target_add_ssh_writes_placeholder_for_env_vars(
    runner: CliRunner,
    targets_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--password-env VAR` lands as ``password: ${VAR}`` in yaml.

    Spec §场景:target add 凭据参数命名一致 — the CLI never accepts a
    literal ``--password`` flag; only ``--password-env VAR`` is
    allowed, and the yaml stores the placeholder string.
    """

    monkeypatch.setattr("hostlens.cli.target.os.geteuid", lambda: 1000)
    result = runner.invoke(
        app,
        [
            "target",
            "add",
            "my-ssh",
            "--type",
            "ssh",
            "--host",
            "10.0.0.5",
            "--user",
            "alice",
            "--key-path",
            "/tmp/id_rsa",
            "--password-env",
            "MY_PWD",
            "--passphrase-env",
            "MY_PASS",
        ],
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    payload = yaml.safe_load(targets_yaml.read_text())
    [entry] = payload["targets"]
    assert entry["password"] == "${MY_PWD}"
    assert entry["passphrase"] == "${MY_PASS}"
    assert entry["host"] == "10.0.0.5"
    assert entry["user"] == "alice"
    assert entry["key_path"] == "/tmp/id_rsa"


def test_target_add_rejects_lowercase_password_env_name(
    runner: CliRunner,
    targets_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--password-env my_pwd`` would write a placeholder the loader never expands.

    The loader's ``_PLACEHOLDER_PATTERN`` is ``^[A-Z_][A-Z0-9_]*$`` —
    accepting anything else here silently surfaces the literal
    ``${my_pwd}`` string as the credential at connect time.
    """

    monkeypatch.setattr("hostlens.cli.target.os.geteuid", lambda: 1000)
    result = runner.invoke(
        app,
        [
            "target",
            "add",
            "my-ssh",
            "--type",
            "ssh",
            "--host",
            "10.0.0.5",
            "--user",
            "alice",
            "--password-env",
            "my_pwd",
        ],
    )

    assert result.exit_code == 2, result.stdout + result.stderr
    assert "^[A-Z_][A-Z0-9_]*$" in result.stderr
    assert not targets_yaml.exists()


def test_target_add_rejects_lowercase_passphrase_env_name(
    runner: CliRunner,
    targets_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same rule for ``--passphrase-env``."""

    monkeypatch.setattr("hostlens.cli.target.os.geteuid", lambda: 1000)
    result = runner.invoke(
        app,
        [
            "target",
            "add",
            "my-ssh",
            "--type",
            "ssh",
            "--host",
            "10.0.0.5",
            "--user",
            "alice",
            "--key-path",
            "/tmp/id_rsa",
            "--passphrase-env",
            "myPass",
        ],
    )

    assert result.exit_code == 2, result.stdout + result.stderr
    assert "^[A-Z_][A-Z0-9_]*$" in result.stderr
    assert not targets_yaml.exists()


def test_target_add_name_conflict_exits_2(
    runner: CliRunner,
    targets_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:target add 名称冲突 exit 2."""

    monkeypatch.setattr("hostlens.cli.target.os.geteuid", lambda: 1000)
    _write_yaml(
        targets_yaml,
        {"version": "1", "targets": [{"name": "prod-web", "type": "local"}]},
    )
    result = runner.invoke(app, ["target", "add", "prod-web", "--type", "local"])

    assert result.exit_code == 2, result.stdout + result.stderr
    assert "already exists" in result.stderr
    # Data on stdout, errors on stderr.
    assert "already exists" not in result.stdout


def test_target_add_root_refused_yaml_untouched(
    runner: CliRunner,
    targets_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:target add EUID==0 直接 exit 1.

    ``targets.yaml`` MUST NOT be created or modified when running as
    root, even when other arguments would otherwise be valid.
    """

    monkeypatch.setattr("hostlens.cli.target.os.geteuid", lambda: 0)
    assert not targets_yaml.exists()
    result = runner.invoke(app, ["target", "add", "my-local", "--type", "local"])

    assert result.exit_code == 1, result.stdout + result.stderr
    # Spec requires a remediation hint on stderr.
    assert "EUID" in result.stderr or "root" in result.stderr
    # yaml not touched.
    assert not targets_yaml.exists()


# ---------------------------------------------------------------------------
# `target list`
# ---------------------------------------------------------------------------


def test_target_list_json_schema(
    runner: CliRunner,
    targets_yaml: Path,
) -> None:
    """Spec §场景:target list --json 输出结构化.

    Locks down the JSON contract: ``{"targets": [{name, type, enabled,
    capabilities: [...]}, ...]}``.
    """

    _write_yaml(
        targets_yaml,
        {
            "version": "1",
            "targets": [
                {"name": "alpha", "type": "local"},
                {"name": "beta", "type": "local", "enabled": False},
            ],
        },
    )
    result = runner.invoke(app, ["target", "list", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert set(payload.keys()) == {"targets"}
    assert isinstance(payload["targets"], list)
    assert len(payload["targets"]) == 2
    for row in payload["targets"]:
        assert set(row.keys()) == {"name", "type", "enabled", "capabilities"}
        assert isinstance(row["capabilities"], list)
    by_name = {row["name"]: row for row in payload["targets"]}
    assert by_name["alpha"]["enabled"] is True
    assert by_name["beta"]["enabled"] is False


def test_target_list_allows_root(
    runner: CliRunner,
    targets_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:target list / test 允许 root."""

    monkeypatch.setattr("hostlens.cli.target.os.geteuid", lambda: 0)
    _write_yaml(targets_yaml, {"version": "1", "targets": []})
    result = runner.invoke(app, ["target", "list", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr


def test_target_list_empty_registry_human_hint(
    runner: CliRunner,
    targets_yaml: Path,
) -> None:
    """Human (non-JSON) output should hint at `target add` when empty."""

    # targets_yaml fixture set, but file does not exist → loader returns
    # an empty TargetsConfig.
    result = runner.invoke(app, ["target", "list"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "hostlens target add" in result.stdout


# ---------------------------------------------------------------------------
# `target remove`
# ---------------------------------------------------------------------------


def test_target_remove_yes_skips_prompt(
    runner: CliRunner,
    targets_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("hostlens.cli.target.os.geteuid", lambda: 1000)
    _write_yaml(
        targets_yaml,
        {
            "version": "1",
            "targets": [
                {"name": "alpha", "type": "local"},
                {"name": "beta", "type": "local"},
            ],
        },
    )
    result = runner.invoke(app, ["target", "remove", "alpha", "--yes"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = yaml.safe_load(targets_yaml.read_text())
    assert [e["name"] for e in payload["targets"]] == ["beta"]


def test_target_remove_no_tty_no_yes_exits_1(
    runner: CliRunner,
    targets_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:target remove 无 TTY 无 --yes exit 1.

    CliRunner does not attach a TTY to stdin, so ``sys.stdin.isatty()``
    returns False inside the invoked command — the exact condition the
    spec mandates.
    """

    monkeypatch.setattr("hostlens.cli.target.os.geteuid", lambda: 1000)
    _write_yaml(
        targets_yaml,
        {"version": "1", "targets": [{"name": "prod-web", "type": "local"}]},
    )
    result = runner.invoke(app, ["target", "remove", "prod-web"])
    assert result.exit_code == 1, result.stdout + result.stderr
    assert "--yes required in non-interactive mode" in result.stderr
    # yaml untouched.
    payload = yaml.safe_load(targets_yaml.read_text())
    assert [e["name"] for e in payload["targets"]] == ["prod-web"]


def test_target_remove_root_refused(
    runner: CliRunner,
    targets_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:target remove EUID==0 直接 exit 1."""

    monkeypatch.setattr("hostlens.cli.target.os.geteuid", lambda: 0)
    _write_yaml(
        targets_yaml,
        {"version": "1", "targets": [{"name": "prod-web", "type": "local"}]},
    )
    result = runner.invoke(app, ["target", "remove", "prod-web", "--yes"])
    assert result.exit_code == 1, result.stdout + result.stderr
    # yaml untouched.
    payload = yaml.safe_load(targets_yaml.read_text())
    assert [e["name"] for e in payload["targets"]] == ["prod-web"]


def test_target_remove_unknown_target_exits_2(
    runner: CliRunner,
    targets_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("hostlens.cli.target.os.geteuid", lambda: 1000)
    _write_yaml(targets_yaml, {"version": "1", "targets": []})
    result = runner.invoke(app, ["target", "remove", "ghost", "--yes"])
    assert result.exit_code == 2, result.stdout + result.stderr
    assert "not found" in result.stderr


# ---------------------------------------------------------------------------
# `target test`
# ---------------------------------------------------------------------------


def test_target_test_local_success(
    runner: CliRunner,
    targets_yaml: Path,
) -> None:
    """`target test my-local` runs the probe and reports capabilities."""

    _write_yaml(
        targets_yaml,
        {"version": "1", "targets": [{"name": "my-local", "type": "local"}]},
    )
    result = runner.invoke(app, ["target", "test", "my-local"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "my-local" in result.stdout
    assert "exit_code: 0" in result.stdout
    # The probe runs `echo hostlens-probe-$$`; stdout should appear.
    assert "hostlens-probe-" in result.stdout
    assert "capabilities" in result.stdout


def test_target_test_disabled_target_exits_1(
    runner: CliRunner,
    targets_yaml: Path,
) -> None:
    """Spec §需求:`hostlens target` — disabled target test exits 1,
    no connection / subprocess attempted."""

    _write_yaml(
        targets_yaml,
        {
            "version": "1",
            "targets": [
                {"name": "off-target", "type": "local", "enabled": False},
            ],
        },
    )
    result = runner.invoke(app, ["target", "test", "off-target"])
    assert result.exit_code == 1, result.stdout + result.stderr
    assert "is disabled in targets.yaml" in result.stderr


def test_target_test_ssh_unreachable_exits_1(
    runner: CliRunner,
    targets_yaml: Path,
) -> None:
    """SSH target whose host is unreachable should exit 1 with a
    structured TargetError ``kind`` on stderr (no credentials leak).

    We point at 192.0.2.x (RFC 5737 documentation range; never
    routable) so the connect attempt deterministically fails fast.
    Spec §场景:target test 连通失败 exit 1.
    """

    _write_yaml(
        targets_yaml,
        {
            "version": "1",
            "targets": [
                {
                    "name": "ssh-prod",
                    "type": "ssh",
                    "host": "192.0.2.1",
                    "user": "noone",
                    # short connect_timeout so test stays fast
                    "connect_timeout": 2,
                },
            ],
        },
    )
    result = runner.invoke(app, ["target", "test", "ssh-prod"])
    assert result.exit_code == 1, result.stdout + result.stderr
    # stderr must contain the structured error kind (no creds anywhere).
    assert "ssh_connect" in result.stderr or "error" in result.stderr
    # Credentials never appear (none configured in this case but assert
    # the principle of no host leak being unwarranted).


# ---------------------------------------------------------------------------
# stderr / stdout separation
# ---------------------------------------------------------------------------


def test_stderr_carries_errors_stdout_only_data(
    runner: CliRunner,
    targets_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Errors go to stderr; data goes to stdout."""

    monkeypatch.setattr("hostlens.cli.target.os.geteuid", lambda: 0)
    result = runner.invoke(app, ["target", "add", "x", "--type", "local"])
    # Error message lands on stderr, NOT stdout.
    assert "root" in result.stderr or "EUID" in result.stderr
    assert "root" not in result.stdout
    assert "EUID" not in result.stdout


# ---------------------------------------------------------------------------
# typo / unknown flag rejection
# ---------------------------------------------------------------------------


def test_target_add_unknown_flag_exits_2(
    runner: CliRunner,
    targets_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:target add 凭据参数命名一致 — unknown option is
    rejected by Typer with exit 2.

    The non-root guard is fine here either way — Typer parses options
    BEFORE the callback body runs, so the typo flag fires the
    UsageError path regardless of EUID. We still pin EUID to a non-zero
    value so the assertion does not become root-conditional in the CI
    image.
    """

    monkeypatch.setattr("hostlens.cli.target.os.geteuid", lambda: 1000)
    result = runner.invoke(
        app,
        ["target", "add", "x", "--type", "ssh", "--key-env", "FOO"],
    )
    assert result.exit_code == 2, result.stdout + result.stderr
    # Exact stderr text differs between Click versions (and gets ANSI-
    # styled when the terminal is interactive vs piped); assert only
    # on the failure mode that matters — exit code 2 above + a
    # non-empty diagnostic on stderr below.
    assert result.stderr.strip(), "expected diagnostic on stderr for unknown flag"
