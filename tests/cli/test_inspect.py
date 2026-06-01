"""Tests for the ``hostlens inspect`` Typer command (Group 6).

Spec: ``openspec/changes/add-report-data-model/specs/inspect-cli-command/spec.md``.

These tests drive ``hostlens.cli.main`` (the project entrypoint) so the
``click.UsageError`` → exit 3 wrapper actually runs. Each invocation
goes through ``_run_main`` which patches ``sys.argv`` and captures the
``SystemExit`` raised by the wrapper, mirroring real shell behaviour
(``CliRunner.invoke(app, ...)`` is intentionally NOT used here — it
calls the raw Typer ``app`` directly and would observe Click's default
exit 2 for usage errors, which the project remaps to 3 only via
``main()``).

Group 7 owns the syrupy snapshot tests + the four spec-mandated
integration cases; this module exercises the unit-level invariants
(parameter parsing, exit code matrix, stdout/stderr separation) plus
enough integration coverage to drive task 6.1 - 6.11 acceptance.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from hostlens.cli import main

# Typer >=0.12 renders `--help` through Rich. In CI (GitHub Actions sets
# ``CI=true``) Rich force-enables colour highlighting AND box-drawing
# layout, which interleaves ANSI escapes inside option names (e.g. the
# leading ``--`` is coloured separately from ``inspector``, so a naive
# ``"--inspector" in stdout`` substring check fails) and may wrap flag
# names across lines. Normalise help output before matching by stripping
# ANSI CSI sequences and collapsing whitespace, mirroring the helper used
# in ``tests/cli/test_doctor_tty.py``.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_BOX_RE = re.compile(r"[│─╭╮╰╯┃━┏┓┗┛]+")


def _normalise_help(text: str) -> str:
    """Strip ANSI escapes + box-drawing chars from Typer/Rich help output."""

    no_ansi = _ANSI_RE.sub("", text)
    no_box = _BOX_RE.sub(" ", no_ansi)
    return re.sub(r"\s+", " ", no_box)


@pytest.fixture
def targets_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``Settings.targets_config_path`` at a fresh tmp file with one local target.

    Mirrors the same env-override approach used by ``tests/cli/test_target.py``
    so the inspector CLI runs end-to-end against a controlled targets config.
    """

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
    """Point inspectors search paths at an empty user directory.

    The builtin path is hardcoded inside ``build_registry_from_search_paths``
    so ``hello.echo`` is still discoverable; the user dir is only there to
    keep the loader from picking up stray manifests under the operator's
    real ``~/.config/hostlens/inspectors``.
    """

    user_path = tmp_path / "inspectors"
    user_path.mkdir()
    monkeypatch.setenv("HOSTLENS_INSPECTORS_SEARCH_PATHS", str(user_path))
    return user_path


def _run_main(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[int, str, str]:
    """Invoke ``hostlens.cli.main`` with patched argv and capture exit code.

    Uses pytest's ``capsys`` to capture both stdout and stderr (including
    Click's ``typer.echo(..., err=True)`` writes). Returns
    ``(exit_code, stdout_text, stderr_text)``.
    """

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
# `--help` and Typer usage exit rewrite
# --------------------------------------------------------------------------- #


def test_inspect_help_lists_all_options_and_exits_0(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`hostlens inspect --help` lists every option + exits 0 (not 3).

    Spec §场景:`--help` 输出含全部参数 + §场景:`--help` 退出码必须为 0.
    The click-UsageError wrapper must NOT demote ``--help``'s exit 0 to
    exit 3 because ``HelpOption`` raises ``SystemExit(0)`` directly,
    bypassing UsageError. After add-intent-cli the option count is 7
    (``--intent`` added).
    """

    exit_code, stdout, _stderr = _run_main(["inspect", "--help"], capsys, monkeypatch)
    assert exit_code == 0
    # All seven options listed. Normalise first so the assertion survives
    # Rich's ANSI-coloured ``--<flag>`` rendering on CI (where ``--`` and
    # the flag name are wrapped in separate escape sequences, breaking a
    # naive substring search).
    normalised = _normalise_help(stdout)
    for name in (
        "--inspector",
        "--intent",
        "--output",
        "--format",
        "--parameters",
        "--allow-privileged",
        "--timeout",
    ):
        assert name in normalised, f"missing {name!r} in --help output:\n{stdout}"


def test_inspect_missing_target_exits_3(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Bare ``hostlens inspect`` -> stderr ``Missing argument`` + exit 3.

    Spec §场景:缺位置参数 target 报错 — Click's default exit 2 for usage
    errors is rewritten to 3 by ``main()``'s wrapper.
    """

    exit_code, _stdout, stderr = _run_main(["inspect"], capsys, monkeypatch)
    assert exit_code == 3
    assert "Missing argument" in stderr
    assert "TARGET" in stderr


def test_inspect_missing_inspector_exits_3(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`hostlens inspect local-host` (no --inspector, no --intent) -> exit 3.

    Spec (add-intent-cli MODIFIED) §场景:缺 --inspector 且缺 --intent 报错. After
    add-intent-cli made ``--inspector`` optional + mutually exclusive with
    ``--intent``, the missing-both case is no longer Click's ``Missing option``
    usage error — it is the command body's explicit mutual-exclusion gate
    (``typer.Exit(code=3)``) with the new one-line message.
    """

    exit_code, _stdout, stderr = _run_main(["inspect", "local-host"], capsys, monkeypatch)
    assert exit_code == 3
    assert "must provide exactly one of --inspector or --intent" in stderr
    assert "Traceback" not in stderr


def test_inspect_invalid_format_exits_3(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """`--format html` -> stderr ``Invalid value for '--format'`` + exit 3.

    Spec §场景:--format 不在 md/json 报错.
    """

    exit_code, _stdout, stderr = _run_main(
        ["inspect", "local-host", "--inspector", "hello.echo", "--format", "html"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 3
    assert "Invalid value for '--format'" in stderr


# --------------------------------------------------------------------------- #
# Target / inspector resolution exit 3
# --------------------------------------------------------------------------- #


def test_inspect_target_not_found_exits_3(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """Unknown target -> exit 3 + stderr hint string locked by spec."""

    exit_code, stdout, stderr = _run_main(
        ["inspect", "ghost-host", "--inspector", "hello.echo"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 3
    # stdout must be empty when target resolution fails (no partial output).
    assert stdout == ""
    assert "target not found: ghost-host" in stderr
    assert "run 'hostlens target list' to see registered targets" in stderr


def test_inspect_inspector_not_found_exits_3(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """Unknown inspector -> exit 3 + stderr hint string locked by spec."""

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--inspector", "nonexistent.foo"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 3
    assert stdout == ""
    assert "inspector not found: nonexistent.foo" in stderr
    assert "run 'hostlens inspectors list'" in stderr


# --------------------------------------------------------------------------- #
# --parameters dual-syntax
# --------------------------------------------------------------------------- #


def test_inspect_parameters_inline_json_object_parses(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """Inline JSON object reaches the runner.

    ``hello.echo`` ignores parameters, so we just check the command ran
    (exit 0) and didn't reject the parameter shape.
    """

    exit_code, stdout, stderr = _run_main(
        [
            "inspect",
            "local-host",
            "--inspector",
            "hello.echo",
            "--parameters",
            '{"k": "v"}',
        ],
        capsys,
        monkeypatch,
    )
    assert exit_code == 0, stderr
    assert "Hostlens Inspection Report" in stdout


def test_inspect_parameters_file_ref_parses(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
    tmp_path: Path,
) -> None:
    """``@<path>`` form reads the file and parses its JSON content."""

    params_path = tmp_path / "params.json"
    params_path.write_text('{"k": "v"}')

    exit_code, stdout, _stderr = _run_main(
        [
            "inspect",
            "local-host",
            "--inspector",
            "hello.echo",
            "--parameters",
            f"@{params_path}",
        ],
        capsys,
        monkeypatch,
    )
    assert exit_code == 0
    assert "Hostlens Inspection Report" in stdout


def test_inspect_parameters_invalid_prefix_exits_3(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """``--parameters 'plain text'`` (no ``{`` / ``@``) -> exit 3."""

    exit_code, _stdout, stderr = _run_main(
        [
            "inspect",
            "local-host",
            "--inspector",
            "hello.echo",
            "--parameters",
            "plain text",
        ],
        capsys,
        monkeypatch,
    )
    assert exit_code == 3
    assert "invalid --parameters: must start with '{' (inline JSON) or '@' (file path)" in stderr


def test_inspect_parameters_bad_inline_json_exits_3(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """``--parameters '{bad json'`` -> exit 3 with stderr ``invalid --parameters:`` prefix."""

    exit_code, _stdout, stderr = _run_main(
        [
            "inspect",
            "local-host",
            "--inspector",
            "hello.echo",
            "--parameters",
            "{bad json",
        ],
        capsys,
        monkeypatch,
    )
    assert exit_code == 3
    assert stderr.startswith("invalid --parameters:")


def test_inspect_parameters_file_missing_exits_3(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """``@/nonexistent/path.json`` -> exit 3 with ``failed to read`` prefix."""

    exit_code, _stdout, stderr = _run_main(
        [
            "inspect",
            "local-host",
            "--inspector",
            "hello.echo",
            "--parameters",
            "@/nonexistent/path.json",
        ],
        capsys,
        monkeypatch,
    )
    assert exit_code == 3
    assert stderr.startswith("failed to read --parameters file:")


def test_parameters_file_not_utf8_exit_3(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
    tmp_path: Path,
) -> None:
    """``@<path>`` pointing at a non-UTF-8 file -> exit 3 with explicit hint.

    The default ``Path.read_text()`` uses UTF-8, so binary / Latin-1 bytes
    raise ``UnicodeDecodeError``. The CLI must catch this alongside
    ``OSError`` and emit a one-line stderr message rather than letting a
    Python traceback escape (spec §需求: 不输出 Python traceback).
    """

    params_path = tmp_path / "params.json"
    params_path.write_bytes(b"\xff\xfe\xfd")

    exit_code, _stdout, stderr = _run_main(
        [
            "inspect",
            "local-host",
            "--inspector",
            "hello.echo",
            "--parameters",
            f"@{params_path}",
        ],
        capsys,
        monkeypatch,
    )
    assert exit_code == 3
    assert "failed to read --parameters file: not valid UTF-8" in stderr
    assert "Traceback" not in stderr


# --------------------------------------------------------------------------- #
# --timeout boundary
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad_value", ["0", "-1", "301", "9999"])
def test_inspect_timeout_out_of_range_exits_3(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
    bad_value: str,
) -> None:
    """``--timeout`` outside [1, 300] -> exit 3 + spec-locked stderr.

    Spec §场景:--timeout 0 或负数被拒绝 + §场景:--timeout 超过上限被拒绝.
    """

    exit_code, _stdout, stderr = _run_main(
        [
            "inspect",
            "local-host",
            "--inspector",
            "hello.echo",
            "--timeout",
            bad_value,
        ],
        capsys,
        monkeypatch,
    )
    assert exit_code == 3
    assert "invalid --timeout: must be in [1, 300]" in stderr


def test_inspect_timeout_boundary_300_accepted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """``--timeout 300`` is accepted (upper boundary inclusive).

    Spec §场景:--timeout 上限 300 边界值接受 — the test asserts the value
    is not rejected for being out of range; the run itself completes
    with exit 0 because hello.echo is fast.
    """

    exit_code, stdout, stderr = _run_main(
        [
            "inspect",
            "local-host",
            "--inspector",
            "hello.echo",
            "--timeout",
            "300",
        ],
        capsys,
        monkeypatch,
    )
    assert exit_code == 0, stderr
    assert "Hostlens Inspection Report" in stdout


# --------------------------------------------------------------------------- #
# Happy path + format + --output
# --------------------------------------------------------------------------- #


def test_inspect_hello_echo_md_to_stdout_exit_0(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """End-to-end: hello.echo on local-host -> exit 0 + md Report on stdout."""

    exit_code, stdout, _stderr = _run_main(
        ["inspect", "local-host", "--inspector", "hello.echo"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 0
    assert "# Hostlens Inspection Report" in stdout
    assert "schema_version" in stdout
    assert "1.1" in stdout


def test_inspect_hello_echo_json_to_file_exit_0(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
    tmp_path: Path,
) -> None:
    """End-to-end: ``--format json --output FILE`` writes JSON Report to file.

    stdout must be **empty** when ``--output`` is used (spec §场景:
    --output 写文件且 stdout 不重复).
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
    assert stdout == ""
    payload = json.loads(out_path.read_text())
    assert payload["schema_version"] == "1.1"


def test_inspect_output_write_failure_exits_3(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """``--output /nonexistent/dir/out.md`` -> exit 3 with documented prefix."""

    exit_code, stdout, stderr = _run_main(
        [
            "inspect",
            "local-host",
            "--inspector",
            "hello.echo",
            "--output",
            "/nonexistent/dir/out.md",
        ],
        capsys,
        monkeypatch,
    )
    assert exit_code == 3
    assert stdout == ""
    assert "failed to write output:" in stderr


# --------------------------------------------------------------------------- #
# Exit code matrix: 0 / 1 / 2 / 3 status mapping
# --------------------------------------------------------------------------- #


def _build_inspector_result(
    *,
    status: str,
    findings: list[dict[str, Any]] | None = None,
    error: str | None = None,
    missing: list[str] | None = None,
) -> Any:
    """Build a stub ``InspectorResult`` for exit-code-matrix tests.

    Going through the real Pydantic model keeps the validator chain
    honest (e.g. ``requires_unmet`` requires non-empty missing list).
    """

    from hostlens.inspectors.result import InspectorResult
    from hostlens.reporting.models import Finding

    parsed_findings = [Finding(**f) for f in (findings or [])]
    return InspectorResult(
        name="hello.echo",
        version="1.0.0",
        status=status,  # type: ignore[arg-type]
        target_name="local-host",
        duration_seconds=0.01,
        output={},
        findings=parsed_findings,
        error=error,
        missing=missing or [],
    )


@pytest.mark.parametrize(
    "result_kwargs, expected_exit",
    [
        # status=ok + info finding only -> exit 0
        ({"status": "ok", "findings": [{"severity": "info", "message": "x"}]}, 0),
        # status=ok + warning finding -> exit 0 (warning does NOT flip to 1)
        ({"status": "ok", "findings": [{"severity": "warning", "message": "x"}]}, 0),
        # status=ok + critical finding -> exit 1
        ({"status": "ok", "findings": [{"severity": "critical", "message": "x"}]}, 1),
        # status=ok + no findings -> exit 0
        ({"status": "ok", "findings": []}, 0),
        # status=timeout -> exit 2
        ({"status": "timeout", "error": "command timed out"}, 2),
        # status=target_unreachable -> exit 2
        ({"status": "target_unreachable", "error": "ssh_connection_lost"}, 2),
        # status=requires_unmet -> exit 2
        ({"status": "requires_unmet", "missing": ["bin:foo"]}, 2),
        # status=exception -> exit 2
        ({"status": "exception", "error": "parse_failed"}, 2),
        # status=timeout + critical finding -> exit 2 (runner failure dominates)
        (
            {
                "status": "timeout",
                "error": "command timed out",
                "findings": [{"severity": "critical", "message": "x"}],
            },
            2,
        ),
    ],
)
def test_inspect_compute_exit_code(
    result_kwargs: dict[str, Any],
    expected_exit: int,
) -> None:
    """Unit test for ``_compute_exit_code`` covering the 4-value exit ladder.

    Spec §需求:`hostlens inspect` 退出码必须语义化 4 值 — covers 8 of the
    9 spec scenarios (the 9th, exit 3 for unknown target, is exercised
    by ``test_inspect_target_not_found_exits_3``).
    """

    from hostlens.cli.inspect import _compute_exit_code

    result = _build_inspector_result(**result_kwargs)
    assert _compute_exit_code(result) == expected_exit


# --------------------------------------------------------------------------- #
# Report ValidationError -> exit 2
# --------------------------------------------------------------------------- #


def test_inspect_report_clock_skew_exits_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """``Report.from_inspector_results`` raising ValidationError -> exit 2.

    Simulate finished_at < started_at by patching ``datetime.now`` to
    return descending timestamps. Spec §场景:Report finished_at <
    started_at 退出 2.
    """

    from datetime import UTC, datetime, timedelta

    from hostlens.cli import inspect as inspect_module

    base = datetime.now(UTC)
    times = iter([base, base - timedelta(seconds=1)])

    class _FakeDatetime:
        @staticmethod
        def now(tz: Any = None) -> datetime:
            return next(times)

    monkeypatch.setattr(inspect_module, "datetime", _FakeDatetime)
    exit_code, _stdout, stderr = _run_main(
        ["inspect", "local-host", "--inspector", "hello.echo"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 2
    assert stderr.startswith("internal: report validation failed:") or (
        "internal: report validation failed:" in stderr
    )


# --------------------------------------------------------------------------- #
# CLI boundary: no Python traceback ever reaches the user
# --------------------------------------------------------------------------- #


def test_inspect_runner_runtime_error_wrapped_no_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """Force the runner to raise; CLI wraps as ``internal: <kind>:`` one-liner.

    Spec §场景:不输出 Python traceback — stderr must NOT contain
    ``Traceback`` or file paths even when an internal bug fires.
    """

    from hostlens.cli import inspect as inspect_module

    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(inspect_module, "_dispatch", _boom)

    exit_code, _stdout, stderr = _run_main(
        ["inspect", "local-host", "--inspector", "hello.echo"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 2
    assert "Traceback" not in stderr
    assert "internal: RuntimeError: boom" in stderr
