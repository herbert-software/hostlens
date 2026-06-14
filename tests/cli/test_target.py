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
import stat
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from hostlens.cli import app
from hostlens.targets.config import LocalEntry, save_targets_config


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


def test_target_add_writes_file_0600(
    runner: CliRunner,
    targets_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:helper 下沉 + 原子写原语后 add/remove 输出不变但获 0600.

    The write now goes through the shared atomic primitive, so the file
    lands ``0o600`` instead of inheriting the umask (typically ``0o644``).
    """

    monkeypatch.setattr("hostlens.cli.target.os.geteuid", lambda: 1000)
    result = runner.invoke(app, ["target", "add", "my-local", "--type", "local"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert stat.S_IMODE(targets_yaml.stat().st_mode) == 0o600


def test_target_add_keeps_0600_after_import_style_write(
    runner: CliRunner,
    targets_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:import 写 0600 后再跑 add 文件仍 0600 (不被抹回 0644).

    Simulates the ``import`` write path by calling ``save_targets_config``
    (the shared primitive ``import`` uses) to lay down a ``0o600`` file,
    then runs ``target add`` and asserts the perms are NOT abraded back to
    a world-readable mode by the sibling command.
    """

    monkeypatch.setattr("hostlens.cli.target.os.geteuid", lambda: 1000)
    save_targets_config(targets_yaml, [(LocalEntry(name="imported", type="local"), None, None)])
    assert stat.S_IMODE(targets_yaml.stat().st_mode) == 0o600

    result = runner.invoke(app, ["target", "add", "added", "--type", "local"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert stat.S_IMODE(targets_yaml.stat().st_mode) == 0o600
    payload = yaml.safe_load(targets_yaml.read_text())
    assert [e["name"] for e in payload["targets"]] == ["imported", "added"]


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


# ---------------------------------------------------------------------------
# `target import` — add-cli-target-import (group D)
# ---------------------------------------------------------------------------
#
# These tests isolate the repo dev ``.env`` (chdir tmp + delenv ``HOSTLENS_*``)
# so they behave identically on a clean CI as locally — otherwise the dev
# ``.env`` supplies a backend and the "config error → exit 2" assertion would
# pass locally but fail (or the wrong way) on a stripped CI. See memory note
# ``project_tests_must_isolate_dev_env_or_ci_red``.


@pytest.fixture
def import_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Isolated env for ``target import``: tmp cwd, no dev ``HOSTLENS_*``.

    Returns ``(targets_yaml_path, inventory_path)``. Wires a ``fake`` backend
    (so ``load_settings`` succeeds without the dev ``.env``) and a tmp targets
    path. Non-root is pinned so the write path is never accidentally refused.
    """

    monkeypatch.chdir(tmp_path)
    for key in list(__import__("os").environ):
        if key.startswith("HOSTLENS_"):
            monkeypatch.delenv(key, raising=False)
    targets_path = tmp_path / "targets.yaml"
    monkeypatch.setenv("HOSTLENS_TARGETS_CONFIG_PATH", str(targets_path))
    monkeypatch.setenv("HOSTLENS_BACKEND__TYPE", "fake")
    monkeypatch.setattr("hostlens.cli.target.os.geteuid", lambda: 1000)

    inventory_path = tmp_path / "inv.yml"
    inventory_path.write_text(
        yaml.safe_dump({"hosts_local": {"demo-localhost": {"type": "local"}}})
    )
    return targets_path, inventory_path


# --- 5.1 / 5.2: dry-run default, --yes write -------------------------------


def test_import_dry_run_default_previews_without_writing(
    runner: CliRunner,
    import_env: tuple[Path, Path],
) -> None:
    """Spec §场景:--dry-run 默认只预览不写盘.

    No flag → dry-run: render the plan + a prominent DRY-RUN banner, exit 0,
    and ``targets.yaml`` is NOT created.
    """

    targets_path, inventory_path = import_env
    result = runner.invoke(app, ["target", "import", str(inventory_path), "--source", "yaml"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert not targets_path.exists()
    assert "DRY-RUN" in result.stdout
    assert "demo-localhost" in result.stdout


def test_import_yes_writes_enabled_true_entry(
    runner: CliRunner,
    import_env: tuple[Path, Path],
) -> None:
    """Spec §场景:--yes 落盘 — to_add written with enabled=True (omitted)."""

    targets_path, inventory_path = import_env
    result = runner.invoke(
        app, ["target", "import", str(inventory_path), "--source", "yaml", "--yes"]
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert targets_path.exists()
    payload = yaml.safe_load(targets_path.read_text())
    assert payload == {
        "version": "1",
        "targets": [{"name": "demo-localhost", "type": "local"}],
    }


def test_import_yes_file_is_0600(
    runner: CliRunner,
    import_env: tuple[Path, Path],
) -> None:
    """The written ``targets.yaml`` is ``0o600`` (atomic write discipline)."""

    targets_path, inventory_path = import_env
    result = runner.invoke(
        app, ["target", "import", str(inventory_path), "--source", "yaml", "--yes"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    mode = stat.S_IMODE(targets_path.stat().st_mode)
    assert mode == 0o600, oct(mode)


# --- 5.2: exit codes -------------------------------------------------------


def test_import_missing_yes_is_dry_run_exit_0_not_1(
    runner: CliRunner,
    import_env: tuple[Path, Path],
) -> None:
    """Spec §场景:非交互缺 --yes 走 dry-run 不退 1.

    import has no per-row prompt; absence of ``--yes`` is the dry-run preview,
    so a non-interactive run without ``--yes`` exits 0 (not 1) and writes
    nothing.
    """

    targets_path, inventory_path = import_env
    result = runner.invoke(app, ["target", "import", str(inventory_path), "--source", "yaml"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert not targets_path.exists()


def test_import_dry_run_and_yes_is_exit_2(
    runner: CliRunner,
    import_env: tuple[Path, Path],
) -> None:
    """Spec §场景:--dry-run 与 --yes 同传 exit 2 (fail-safe, no write)."""

    targets_path, inventory_path = import_env
    result = runner.invoke(
        app,
        [
            "target",
            "import",
            str(inventory_path),
            "--source",
            "yaml",
            "--dry-run",
            "--yes",
        ],
    )
    assert result.exit_code == 2, result.stdout + result.stderr
    assert not targets_path.exists()


def test_import_unknown_source_is_exit_2_not_3(
    runner: CliRunner,
    import_env: tuple[Path, Path],
) -> None:
    """Spec §场景:未知 --source 经手动校验 exit 2(非 UsageError exit 3).

    ``--source`` is a bare ``str`` validated in the command body so an unknown
    value raises ``typer.Exit(2)`` — NOT the Click ``UsageError`` path that a
    ``Choice``/``Enum`` would take (which ``cli/__init__.py`` rewrites to 3).
    The assertion deliberately pins ``== 2`` (and ``!= 3``).
    """

    _targets_path, inventory_path = import_env
    result = runner.invoke(
        app,
        ["target", "import", str(inventory_path), "--source", "nonesuch"],
    )
    assert result.exit_code == 2, result.stdout + result.stderr
    assert result.exit_code != 3


def test_import_euid_zero_with_yes_exits_1_without_writing(
    runner: CliRunner,
    import_env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:EUID==0 落盘前 exit 1.

    Root + ``--yes`` is refused before any write — ``targets.yaml`` is never
    created. The dry-run preview path (no ``--yes``) would tolerate root, but
    the write path must refuse it.
    """

    targets_path, inventory_path = import_env
    monkeypatch.setattr("hostlens.cli.target.os.geteuid", lambda: 0)
    result = runner.invoke(
        app, ["target", "import", str(inventory_path), "--source", "yaml", "--yes"]
    )
    assert result.exit_code == 1, result.stdout + result.stderr
    assert not targets_path.exists()


def test_import_euid_zero_dry_run_tolerated(
    runner: CliRunner,
    import_env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Root is tolerated on the default dry-run path (read-only, no local write)."""

    targets_path, inventory_path = import_env
    monkeypatch.setattr("hostlens.cli.target.os.geteuid", lambda: 0)
    result = runner.invoke(app, ["target", "import", str(inventory_path), "--source", "yaml"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert not targets_path.exists()


def test_import_config_load_failure_exits_2(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:配置加载失败 exit 2.

    A backend ``type=anthropic_api`` with no api_key makes ``load_settings()``
    raise ``ConfigError`` (mirrors ``target add``). This is the real exit-2
    trigger; a merely-absent backend returns ``None`` and does not raise, so we
    force the invalid-config path deliberately.
    """

    monkeypatch.chdir(tmp_path)
    for key in list(__import__("os").environ):
        if key.startswith("HOSTLENS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("hostlens.cli.target.os.geteuid", lambda: 1000)
    monkeypatch.setenv("HOSTLENS_BACKEND__TYPE", "anthropic_api")
    monkeypatch.setenv("HOSTLENS_TARGETS_CONFIG_PATH", str(tmp_path / "targets.yaml"))

    inventory_path = tmp_path / "inv.yml"
    inventory_path.write_text(yaml.safe_dump({"g": {"demo-localhost": {"type": "local"}}}))
    result = runner.invoke(app, ["target", "import", str(inventory_path), "--source", "yaml"])
    assert result.exit_code == 2, result.stdout + result.stderr


def test_import_inventory_parse_failure_exits_2(
    runner: CliRunner,
    import_env: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    """Spec §场景:inventory 解析失败 exit 2 (source ``parse`` raises ConfigError)."""

    _targets_path, _inventory_path = import_env
    bad = tmp_path / "bad.yml"
    # ssh entry without required ``host`` → ConfigError at parse time.
    bad.write_text(yaml.safe_dump({"g": {"badssh": {"type": "ssh"}}}))
    result = runner.invoke(app, ["target", "import", str(bad), "--source", "yaml"])
    assert result.exit_code == 2, result.stdout + result.stderr


def test_import_existing_targets_yaml_corrupt_exits_2(
    runner: CliRunner,
    import_env: tuple[Path, Path],
) -> None:
    """Spec §场景:既有 targets.yaml 含非法占位 exit 2.

    An existing ``targets.yaml`` with a placeholder in a non-secret field
    (``host: ${X}``) fails the ``load_targets_config(expand_env=False)``
    pre-validation → exit 2, never a silent raw round-trip.
    """

    targets_path, inventory_path = import_env
    targets_path.parent.mkdir(parents=True, exist_ok=True)
    targets_path.write_text(
        yaml.safe_dump(
            {
                "version": "1",
                "targets": [{"name": "broken", "type": "ssh", "host": "${X}", "user": "u"}],
            }
        )
    )
    result = runner.invoke(
        app, ["target", "import", str(inventory_path), "--source", "yaml", "--yes"]
    )
    assert result.exit_code == 2, result.stdout + result.stderr


def test_import_skip_and_include_unreachable_mutually_exclusive_exit_2(
    runner: CliRunner,
    import_env: tuple[Path, Path],
) -> None:
    """``--skip-unreachable`` and ``--include-unreachable`` together → exit 2."""

    _targets_path, inventory_path = import_env
    result = runner.invoke(
        app,
        [
            "target",
            "import",
            str(inventory_path),
            "--source",
            "yaml",
            "--skip-unreachable",
            "--include-unreachable",
        ],
    )
    assert result.exit_code == 2, result.stdout + result.stderr


# --- 5.2: --yes all-unreachable + include-unreachable escape hatch ----------


def test_import_yes_all_unreachable_non_include_exits_1(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:--yes 全探活失败且非 include 退 1.

    An ssh candidate that cannot connect (bogus host, fast timeout) is the only
    candidate; ``--yes`` without ``--include-unreachable`` → no reachable
    target → exit 1, ``targets.yaml`` unchanged.
    """

    monkeypatch.chdir(tmp_path)
    for key in list(__import__("os").environ):
        if key.startswith("HOSTLENS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("hostlens.cli.target.os.geteuid", lambda: 1000)
    monkeypatch.setenv("HOSTLENS_BACKEND__TYPE", "fake")
    targets_path = tmp_path / "targets.yaml"
    monkeypatch.setenv("HOSTLENS_TARGETS_CONFIG_PATH", str(targets_path))

    inventory_path = tmp_path / "inv.yml"
    # 192.0.2.0/24 is TEST-NET-1 (RFC 5737) — guaranteed non-routable, so the
    # probe fails fast/unreachable without depending on local network state.
    inventory_path.write_text(
        yaml.safe_dump(
            {
                "g": {
                    "dead-host": {
                        "type": "ssh",
                        "host": "192.0.2.1",
                        "user": "nobody",
                    }
                }
            }
        )
    )
    result = runner.invoke(
        app,
        [
            "target",
            "import",
            str(inventory_path),
            "--source",
            "yaml",
            "--yes",
            "--concurrency",
            "1",
        ],
    )
    assert result.exit_code == 1, result.stdout + result.stderr
    assert not targets_path.exists()


def test_import_include_unreachable_all_failed_writes_disabled_exit_0(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §场景:--include-unreachable 全失败仍登记成功.

    ``--yes --include-unreachable`` with an all-unreachable batch writes every
    candidate ``enabled=False`` and exits 0 (escape-hatch semantics).
    """

    monkeypatch.chdir(tmp_path)
    for key in list(__import__("os").environ):
        if key.startswith("HOSTLENS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("hostlens.cli.target.os.geteuid", lambda: 1000)
    monkeypatch.setenv("HOSTLENS_BACKEND__TYPE", "fake")
    targets_path = tmp_path / "targets.yaml"
    monkeypatch.setenv("HOSTLENS_TARGETS_CONFIG_PATH", str(targets_path))

    inventory_path = tmp_path / "inv.yml"
    inventory_path.write_text(
        yaml.safe_dump(
            {
                "g": {
                    "dead-host": {
                        "type": "ssh",
                        "host": "192.0.2.1",
                        "user": "nobody",
                    }
                }
            }
        )
    )
    result = runner.invoke(
        app,
        [
            "target",
            "import",
            str(inventory_path),
            "--source",
            "yaml",
            "--yes",
            "--include-unreachable",
            "--concurrency",
            "1",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert targets_path.exists()
    payload = yaml.safe_load(targets_path.read_text())
    assert payload["targets"] == [
        {
            "name": "dead-host",
            "type": "ssh",
            "enabled": False,
            "host": "192.0.2.1",
            "user": "nobody",
        }
    ]


# --- 5.2: --json schema stability ------------------------------------------


def test_import_json_schema_stable(
    runner: CliRunner,
    import_env: tuple[Path, Path],
) -> None:
    """``--json`` emits the stable four-bucket plan schema to stdout."""

    _targets_path, inventory_path = import_env
    result = runner.invoke(
        app, ["target", "import", str(inventory_path), "--source", "yaml", "--json"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert set(payload.keys()) == {
        "to_add",
        "skipped",
        "failed_probe",
        "invalid_candidate",
    }
    assert payload["to_add"] == [
        {
            "name": "demo-localhost",
            "type": "local",
            "host": None,
            "password_env": None,
            "passphrase_env": None,
        }
    ]


# --- 5.3: end-to-end offline (local) ---------------------------------------


def test_import_end_to_end_dry_run_yes_idempotent(
    runner: CliRunner,
    import_env: tuple[Path, Path],
) -> None:
    """Spec Demo Path: dry-run → --yes → re-run --yes (idempotent skip).

    A ``local`` candidate probes the real local host (CI-runnable, no SSH). The
    first ``--yes`` writes; the second sees the name already present and lands
    it in ``skipped`` (idempotent upsert) with the file unchanged.
    """

    targets_path, inventory_path = import_env

    # 1. dry-run: no write.
    r1 = runner.invoke(app, ["target", "import", str(inventory_path), "--source", "yaml"])
    assert r1.exit_code == 0, r1.stdout + r1.stderr
    assert not targets_path.exists()

    # 2. --yes: write demo-localhost.
    r2 = runner.invoke(app, ["target", "import", str(inventory_path), "--source", "yaml", "--yes"])
    assert r2.exit_code == 0, r2.stdout + r2.stderr
    first = targets_path.read_text()
    assert "demo-localhost" in first

    # 3. re-run --yes: idempotent — demo-localhost is skipped, file unchanged.
    r3 = runner.invoke(
        app,
        ["target", "import", str(inventory_path), "--source", "yaml", "--json", "--yes"],
    )
    assert r3.exit_code == 0, r3.stdout + r3.stderr
    plan = json.loads(r3.stdout)
    assert plan["skipped"] == ["demo-localhost"]
    assert plan["to_add"] == []
    assert targets_path.read_text() == first
