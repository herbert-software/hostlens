"""Spec-mandated integration tests with the exact function names from
``inspect-cli-command/spec.md`` §需求:`hostlens inspect` 集成测试必须覆盖
demo path 第 4 / 5 / 6 / 7 步.

Group 6 already covers the same behaviours under different test names in
``tests/cli/test_inspect.py``; this file pins the *spec-locked* names so
``pytest -k <name>`` invocations from the spec are guaranteed to resolve.
The 4 cases drive ``hostlens.cli.main`` end-to-end through a real
``InspectorRegistry`` + ``TargetRegistry`` + ``LocalTarget`` (the
``hello.echo`` builtin plus a tmp targets.yaml).

The four spec-locked names are:

- ``test_inspect_hello_echo_md_to_stdout_exit_0`` (demo step 4)
- ``test_inspect_hello_echo_json_to_file_exit_0`` (demo step 5)
- ``test_inspect_nonexistent_inspector_exit_3`` (demo step 6)
- ``test_inspect_nonexistent_target_exit_3`` (demo step 7)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from hostlens.cli import main


@pytest.fixture
def targets_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``Settings.targets_config_path`` at a fresh tmp file."""

    path = tmp_path / "targets.yaml"
    path.write_text(
        yaml.safe_dump(
            {"version": "1", "targets": [{"name": "local-host", "type": "local"}]},
            sort_keys=False,
        )
    )
    monkeypatch.setenv("HOSTLENS_TARGETS_CONFIG_PATH", str(path))
    return path


@pytest.fixture
def user_inspectors_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Empty user inspectors dir; builtin registry still resolves ``hello.echo``."""

    user_path = tmp_path / "inspectors"
    user_path.mkdir()
    monkeypatch.setenv("HOSTLENS_INSPECTORS_SEARCH_PATHS", str(user_path))
    return user_path


def _run_main(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[int, str, str]:
    """Invoke ``hostlens.cli.main`` with patched argv and capture exit code."""

    monkeypatch.setattr(sys, "argv", ["hostlens", *argv])
    try:
        main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    else:
        code = 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_inspect_hello_echo_md_to_stdout_exit_0(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """Demo step 4: ``hostlens inspect local-host --inspector hello.echo``.

    Spec §场景:test_inspect_hello_echo_md_to_stdout_exit_0 通过. Asserts
    stdout contains the report title, the meta table header, and the
    summary line for the info finding (``hello.echo`` emits exactly one
    ``info`` finding from its ``len(raw) > 0`` rule).
    """

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--inspector", "hello.echo"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 0, stderr
    assert "# Hostlens Inspection Report" in stdout
    assert "| Field | Value |" in stdout
    assert "- info: 1" in stdout


def test_inspect_hello_echo_json_to_file_exit_0(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
    tmp_path: Path,
) -> None:
    """Demo step 5: ``--format json --output <tmp>``.

    Spec §场景:test_inspect_hello_echo_json_to_file_exit_0 通过. Asserts
    the file exists, ``json.loads`` succeeds, ``schema_version == "1.1"``,
    stdout is empty (no duplicate report leak), exit 0.
    """

    out_path = tmp_path / "report.json"
    exit_code, stdout, stderr = _run_main(
        [
            "inspect",
            "local-host",
            "--inspector",
            "hello.echo",
            "--format",
            "json",
            "--output",
            str(out_path),
        ],
        capsys,
        monkeypatch,
    )
    assert exit_code == 0, stderr
    assert stdout == ""
    assert out_path.exists()
    payload = json.loads(out_path.read_text())
    assert payload["schema_version"] == "1.1"


def test_inspect_nonexistent_inspector_exit_3(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """Demo step 6: ``--inspector nonexistent.foo`` -> exit 3 + stderr hint.

    Spec §场景:test_inspect_nonexistent_inspector_exit_3 通过.
    """

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--inspector", "nonexistent.foo"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 3
    assert stdout == ""
    assert "inspector not found:" in stderr


def test_inspect_nonexistent_target_exit_3(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """Demo step 7: positional ``ghost-host`` -> exit 3 + stderr hint.

    Spec §场景:test_inspect_nonexistent_target_exit_3 通过.
    """

    exit_code, stdout, stderr = _run_main(
        ["inspect", "ghost-host", "--inspector", "hello.echo"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 3
    assert stdout == ""
    assert "target not found:" in stderr
