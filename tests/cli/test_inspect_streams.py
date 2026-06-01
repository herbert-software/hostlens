"""End-to-end stdout/stderr separation for ``hostlens inspect`` (Group 8.2).

Spec: ``openspec/changes/add-report-data-model/specs/inspect-cli-command/spec.md``
§需求:`hostlens inspect` 必须以 stdout/stderr 分离 与 默认 stdout 模式工作.

The CLI honours POSIX stream separation:

  - Rendered Report (md or json) writes to **stdout** (or ``--output`` file)
  - Errors / warnings write to **stderr**
  - When ``--output FILE`` is set, stdout stays empty (no duplicated Report)
  - Python tracebacks are **never** surfaced to the user — every internal
    failure becomes a single stderr line ``internal: <kind>: <msg>``

Four spec scenarios are exercised here:

  1. ``缺省输出 stdout`` — no ``--output`` -> Report on stdout, stderr empty
  2. ``--output 写文件且 stdout 不重复`` — file gets Report, stdout empty
  3. ``错误信息走 stderr`` — usage error -> stderr populated, stdout empty
  4. ``不输出 Python traceback`` — runner RuntimeError wrapped as one-liner

Group 6 already touches scenarios 1 / 2 / 3 in passing
(``test_inspect_hello_echo_md_to_stdout_exit_0`` checks stdout but not the
stderr-is-empty invariant explicitly; ``test_inspect_hello_echo_json_to_file_exit_0``
asserts ``stdout == ""`` for ``--output``; ``test_inspect_target_not_found_exits_3``
asserts ``stdout == ""`` on the error path). This module is the explicit
"streams" coverage — every scenario asserts **both** sides of the separation
(stdout + stderr) to lock the invariant.

Driver: same ``_run_main`` (``sys.argv`` patch + ``main()`` + ``capsys``)
used by Group 6 — see ``tests/cli/test_inspect_exit_codes.py`` docstring for
why ``CliRunner.invoke(app, ...)`` is unsuitable (it bypasses the click
UsageError → exit 3 wrapper in ``main()``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from hostlens.cli import inspect as inspect_module
from hostlens.cli import main


@pytest.fixture
def targets_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``Settings.targets_config_path`` at a tmp targets.yaml."""

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
    """Point inspectors search paths at an empty user dir; builtins still resolve."""

    user_path = tmp_path / "inspectors"
    user_path.mkdir()
    monkeypatch.setenv("HOSTLENS_INSPECTORS_SEARCH_PATHS", str(user_path))
    return user_path


def _run_main(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[int, str, str]:
    """Invoke ``hostlens.cli.main`` end-to-end and capture (exit, stdout, stderr)."""

    monkeypatch.setattr(sys, "argv", ["hostlens", *argv])
    try:
        main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    else:
        code = 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --------------------------------------------------------------------------- #
# Scenario 1: default output -> stdout (no --output)
# --------------------------------------------------------------------------- #


def test_inspect_default_writes_report_to_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """Spec §场景:缺省输出 stdout.

    Without ``--output``, the rendered md Report must land on **stdout**
    and stderr must be empty (spec: ``stderr 必须为空 (无错误时)``). The
    CLI raises the structlog filter to WARNING for the inspect command's
    scope so happy-path ``inspector_started`` / ``inspector_finished``
    info events do not fire at all — anything reaching stderr on a
    successful run is a regression.
    """

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--inspector", "hello.echo"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 0, stderr

    # stdout has the full Report.
    assert "# Hostlens Inspection Report" in stdout
    assert "schema_version" in stdout
    # Spec invariant: stderr empty on the happy path. No info log noise,
    # no Report bytes (the stdout/stderr separation contract is one-way).
    assert stderr == "", f"expected stderr to be empty on happy path, got:\n{stderr!r}"


# --------------------------------------------------------------------------- #
# Scenario 2: --output writes file, stdout stays empty
# --------------------------------------------------------------------------- #


def test_inspect_output_flag_writes_file_and_stdout_silent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
    tmp_path: Path,
) -> None:
    """Spec §场景:--output 写文件且 stdout 不重复.

    File must contain the Report (md); stdout must be empty (no
    duplication, no progress hint that includes Report bytes).
    """

    out_path = tmp_path / "report.md"
    exit_code, stdout, stderr = _run_main(
        [
            "inspect",
            "local-host",
            "--inspector",
            "hello.echo",
            "--output",
            str(out_path),
        ],
        capsys,
        monkeypatch,
    )
    assert exit_code == 0, stderr

    # File got the Report.
    assert out_path.exists()
    file_text = out_path.read_text()
    assert "# Hostlens Inspection Report" in file_text
    assert "schema_version" in file_text
    # stdout is empty (spec: "stdout 必须为空 (无 Report 内容)").
    assert stdout == "", f"unexpected stdout when --output set:\n{stdout!r}"


def test_inspect_output_flag_json_writes_file_and_stdout_silent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
    tmp_path: Path,
) -> None:
    """Same as above but ``--format json`` — locks the JSON path too.

    Companion case: the md path is covered by the previous test; here
    we lock JSON so a future regression that special-cases one format
    (e.g. adding stdout flushing) cannot leak Report bytes to stdout.
    """

    out_path = tmp_path / "report.json"
    exit_code, stdout, _stderr = _run_main(
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
    assert exit_code == 0
    payload = json.loads(out_path.read_text())
    assert payload["schema_version"] == "1.1"
    assert stdout == ""


# --------------------------------------------------------------------------- #
# Scenario 3: errors land on stderr, stdout stays empty
# --------------------------------------------------------------------------- #


def test_inspect_error_message_goes_to_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """Spec §场景:错误信息走 stderr.

    Unknown target → stderr carries the user-facing error message; stdout
    must stay empty (no partial Report header, no progress hint).
    """

    exit_code, stdout, stderr = _run_main(
        ["inspect", "ghost", "--inspector", "hello.echo"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 3
    # stdout is silent on the error path.
    assert stdout == ""
    # stderr contains the spec-locked error message.
    assert "target not found: ghost" in stderr


# --------------------------------------------------------------------------- #
# Scenario 4: no Python traceback ever reaches the user
# --------------------------------------------------------------------------- #


def test_inspect_large_report_emits_stderr_warning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """Spec: ``hostlens inspect`` 必须以 stdout/stderr 分离 与 默认 stdout
    模式工作 ("warning - 如 evidence 字节数 > 8MB").

    Force the report's evidence byte count above the 8 MiB threshold by
    monkey-patching ``Report.total_evidence_bytes`` (the alternative —
    constructing an Inspector that emits >8 MiB of stdout — is wasted
    work for a unit test). The CLI must emit a single ``warning:``
    line on stderr and still render the full Report to stdout.
    """

    from hostlens.reporting import models as reporting_models

    monkeypatch.setattr(
        reporting_models.Report,
        "total_evidence_bytes",
        lambda self: 9 * 1024 * 1024,
    )

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--inspector", "hello.echo"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 0
    assert "# Hostlens Inspection Report" in stdout
    assert "warning: report evidence is " in stderr
    assert "threshold 8 MiB" in stderr


def test_inspect_internal_error_no_traceback_in_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """Spec §场景:不输出 Python traceback.

    Force the runner dispatch path to raise ``RuntimeError``; the CLI
    boundary must wrap it as a single ``internal: RuntimeError: <msg>``
    line on stderr with **no** ``Traceback (most recent call last):``
    header and **no** ``File "..."`` frames leaking into the output.
    Stdout must also be empty (no partial Report rendered before the
    exception).
    """

    async def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("simulated runner bug")

    monkeypatch.setattr(inspect_module, "_dispatch", _boom)

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--inspector", "hello.echo"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 2
    # No partial Report leaked to stdout.
    assert stdout == ""
    # No Python traceback artefacts in stderr.
    assert "Traceback" not in stderr
    assert 'File "' not in stderr
    assert "  raise " not in stderr
    # The single canonical one-liner is present.
    assert "internal: RuntimeError: simulated runner bug" in stderr
