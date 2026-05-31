"""End-to-end ``hostlens demo`` CLI tests (add-demo-cli group 4.2).

Spec: ``openspec/changes/add-demo-cli/specs/demo-cli-command/spec.md``.

These drive ``hostlens.cli.main`` (the project entrypoint) via the ``_run_main``
helper — the same honest E2E driver ``tests/cli/test_inspect_intent.py`` uses.
``CliRunner.invoke(app, ...)`` is intentionally NOT used: it calls the raw Typer
``app`` and would observe Click's default usage exit, bypassing ``main()``'s
``UsageError`` → exit-3 rewrite and the ``standalone_mode=False`` ``typer.Exit``
plumbing the demo exit codes ride on. ``capsys`` is non-TTY, so
``RichLiveObserver`` auto-degrades to plain output — exactly the pipeline/CI
posture the spec requires (progress to stderr, never stdout).

The seam these tests replace is ``hostlens.cli.demo.build_demo_planner`` (or
``asset_exists`` for the pre-flight branches): the unit under test is the CLI's
exception → exit-code mapping + stream routing, not the assembly itself, so the
error paths (corrupt cassette / runtime drift / missing asset) are induced by
monkeypatching that one boundary. The happy paths run the *real* assembly over
the committed packaged assets — no mock of ``PlannerAgent`` / ``AgentLoop``.

``asyncio_mode = "auto"`` (pyproject) — no markers; every backend is a
``PlaybackBackend`` so nothing hits the network.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import hostlens.cli.demo as demo_mod
from hostlens.cli import main

if TYPE_CHECKING:
    from hostlens.agent.planner import PlannerAgent
    from hostlens.targets.replay import ReplayTarget


def _run_main(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[int, str, str]:
    """Invoke ``hostlens.cli.main`` end-to-end; return (code, stdout, stderr).

    Same shape as ``tests/cli/test_inspect_intent.py::_run_main`` — patches
    ``sys.argv``, calls ``main()``, and captures both streams via ``capsys``
    (which is non-TTY, so RichLiveObserver degrades to plain output).
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
# Happy path — real assembly over packaged assets, no API key
# --------------------------------------------------------------------------- #


def test_demo_run_cpu_saturation_no_api_key(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``demo run cpu_saturation`` with no ``ANTHROPIC_API_KEY`` still renders.

    Spec §场景:对已知场景跑通离线回放 + §场景:不触达 API 的结构性保证 (the
    缺 key 不影响运行 half). cpu_saturation replays to ``ok`` + critical
    findings, so the 4-value contract yields exit 1 (NOT a failure for "did the
    demo run offline"). The report (narrative + findings) lands on stdout.
    """

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    code, stdout, stderr = _run_main(["demo", "run", "cpu_saturation"], capsys, monkeypatch)

    assert code == 1, stderr  # ok + critical finding (offline run succeeded)
    assert "## Findings" in stdout
    assert "critical:" in stdout
    assert "status=ok" in stdout
    assert "Traceback" not in stderr


def test_demo_run_ignores_malformed_hostlens_env(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Malformed ``HOSTLENS_*`` env / .env must not break the offline demo (D7).

    Demo is self-contained: it isolates env sources so a user's broken
    ``HOSTLENS_LOG_MODE`` / ``HOSTLENS_LLM__PRIMARY_MODEL`` cannot turn the
    replay into a ConfigError exit 2. The run still succeeds offline and exits 1
    (ok + critical finding), never failing because of the env.
    """

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("HOSTLENS_LOG_MODE", "invalid")
    monkeypatch.setenv("HOSTLENS_LLM__PRIMARY_MODEL", "garbage")

    code, stdout, stderr = _run_main(
        ["demo", "run", "cpu_saturation", "--quiet"], capsys, monkeypatch
    )

    assert code == 1, stderr  # offline run succeeded despite malformed env
    assert "## Findings" in stdout
    assert "Traceback" not in stderr


def test_demo_run_kebab_case_normalizes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``cpu-saturation`` (kebab) is normalized to ``cpu_saturation``.

    Spec §场景:kebab-case 输入归一化到 snake_case — the kebab run must be
    byte-identical (same exit code + same stdout report) to the snake run.
    """

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    code_snake, out_snake, _ = _run_main(["demo", "run", "cpu_saturation"], capsys, monkeypatch)
    code_kebab, out_kebab, _ = _run_main(["demo", "run", "cpu-saturation"], capsys, monkeypatch)

    assert code_snake == code_kebab == 1
    assert out_kebab == out_snake


def test_demo_run_md_and_json_same_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``-f md`` and ``-f json`` exit identically (code is format-independent).

    Spec §场景:json 与 md 退出码一致 — the code derives from terminal_status +
    finding severity, never the render format. The json branch must also emit a
    parseable ``PlannerResult`` on stdout.
    """

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    code_md, _, _ = _run_main(["demo", "run", "cpu_saturation", "-f", "md"], capsys, monkeypatch)
    code_json, out_json, _ = _run_main(
        ["demo", "run", "cpu_saturation", "-f", "json"], capsys, monkeypatch
    )

    assert code_md == code_json == 1
    payload = json.loads(out_json)
    assert payload["loop_result"]["terminal_status"] == "ok"
    assert payload["findings"]


# --------------------------------------------------------------------------- #
# Progress routing — stderr only, stdout stays a clean report (non-TTY)
# --------------------------------------------------------------------------- #


def test_demo_run_progress_on_stderr_report_on_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Default (progress on): progress → stderr, report → stdout, no cross-leak.

    Spec §场景:进度到 stderr 报告到 stdout 不互相污染 — even under a non-TTY
    pipe (capsys), stdout must be the pure report and carry no progress
    decoration. The observer names the ``run_inspector`` step in its progress
    tree, so that token must appear on stderr but never on stdout.
    """

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    _code, stdout, stderr = _run_main(["demo", "run", "cpu_saturation"], capsys, monkeypatch)

    assert "run_inspector" in stderr
    assert "run_inspector" not in stdout
    # The rendered report still lands cleanly on stdout.
    assert "## Findings" in stdout


def test_demo_run_quiet_and_no_progress_identical(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--quiet`` and ``--no-progress`` are two spellings of one switch.

    Spec §场景:`--quiet` / `--no-progress` 关闭进度 — both suppress the progress
    stream (no ``run_inspector`` step on stderr) while still rendering the full
    report to stdout, and the two spellings behave identically.
    """

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    code_q, out_q, err_q = _run_main(
        ["demo", "run", "cpu_saturation", "--quiet"], capsys, monkeypatch
    )
    code_np, out_np, err_np = _run_main(
        ["demo", "run", "cpu_saturation", "--no-progress"], capsys, monkeypatch
    )

    assert code_q == code_np == 1
    assert out_q == out_np
    # Progress suppressed on both: the observer's run_inspector step is absent.
    assert "run_inspector" not in err_q
    assert "run_inspector" not in err_np
    # Report still rendered.
    assert "## Findings" in out_q


# --------------------------------------------------------------------------- #
# --output write
# --------------------------------------------------------------------------- #


def test_demo_run_output_writes_file_stdout_quiet(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """``-o FILE`` writes the report to FILE; stdout carries no report body.

    Spec §场景:`--output` 写文件 — the rendered report goes to the file and the
    report body is absent from stdout.
    """

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out_file = tmp_path / "report.md"

    code, stdout, _stderr = _run_main(
        ["demo", "run", "cpu_saturation", "-o", str(out_file)], capsys, monkeypatch
    )

    assert code == 1
    written = out_file.read_text(encoding="utf-8")
    assert "## Findings" in written
    # Report body is in the file, not on stdout.
    assert "## Findings" not in stdout


def test_demo_run_output_unwritable_path_exit_3(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """``-o`` to an unwritable path → single stderr line, exit 3, no traceback.

    Spec §场景:`--output` 写到不可写路径 — the write failure is a caller-boundary
    error (exit 3), reported as one stderr line; stdout stays empty of the
    report body and no Python traceback leaks. The pre-flight catches it before
    assembly, so no progress decoration is emitted before the exit.
    """

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # A path whose parent directory does not exist → caught by output pre-flight.
    bad_path = tmp_path / "missing_dir" / "report.md"

    code, stdout, stderr = _run_main(
        ["demo", "run", "cpu_saturation", "-o", str(bad_path)], capsys, monkeypatch
    )

    assert code == 3
    assert "failed to write output" in stderr
    assert "## Findings" not in stdout
    assert "Traceback" not in stderr
    # Pre-flight fails before any assembly / progress: no inspector step emitted.
    assert "run_inspector" not in stderr
    assert "run_inspector" not in stdout


def test_demo_run_output_existing_readonly_file_exit_3(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """``-o`` to an existing read-only file → single stderr line, exit 3.

    Spec §场景:`--output` 写到不可写路径 — a pre-existing file whose mode is
    ``0o444`` is unwritable even though its parent dir is writable; the
    pre-flight must catch it before assembly, so exit 3 is emitted with one
    stderr line, no traceback, and no progress decoration.
    """

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ro_file = tmp_path / "existing_readonly.md"
    ro_file.write_text("", encoding="utf-8")
    ro_file.chmod(0o444)

    code, stdout, stderr = _run_main(
        ["demo", "run", "cpu_saturation", "-o", str(ro_file)], capsys, monkeypatch
    )

    assert code == 3
    assert "failed to write output" in stderr
    assert "## Findings" not in stdout
    assert "Traceback" not in stderr
    # Pre-flight fails before any assembly / progress: no inspector step emitted.
    assert "run_inspector" not in stderr
    assert "run_inspector" not in stdout


# --------------------------------------------------------------------------- #
# Pre-flight exit 3: unknown scenario / missing asset
# --------------------------------------------------------------------------- #


def test_demo_run_unknown_scenario_exit_3(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unknown scenario → exit 3, single stderr line pointing at ``demo list``.

    Spec §场景:未知场景报错并指引 list — the normalized key misses the registry;
    pre-flight maps it to exit 3 with the ``unknown scenario`` hint and no
    traceback.
    """

    code, stdout, stderr = _run_main(["demo", "run", "not-a-scenario"], capsys, monkeypatch)

    assert code == 3
    assert stdout == ""
    assert "unknown scenario" in stderr
    assert "hostlens demo list" in stderr
    assert "Traceback" not in stderr


def test_demo_run_missing_asset_exit_3(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Registered scenario but absent packaged asset → exit 3 (pre-flight).

    Spec §场景:资产缺失时 fail-loud — distinguished from corrupt-asset exit 2:
    the pre-flight ``asset_exists`` check (Traversable ``is_file``) returns False
    *before* assembly, so the scenario is exit 3 (asset absent), with a single
    ``missing scenario asset`` line and no traceback.
    """

    monkeypatch.setattr(demo_mod, "asset_exists", lambda _key, _kind: False)

    code, stdout, stderr = _run_main(["demo", "run", "cpu_saturation"], capsys, monkeypatch)

    assert code == 3
    assert stdout == ""
    assert "missing scenario asset" in stderr
    assert "Traceback" not in stderr


# --------------------------------------------------------------------------- #
# Assembly-phase corrupt asset (exit 2) vs runtime drift (exit 2), distinct
# from the missing-asset exit 3 above.
# --------------------------------------------------------------------------- #


def test_demo_run_corrupt_cassette_assembly_value_error_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Corrupt cassette JSON (assembly ``ValueError``) → exit 2, not exit 3.

    Spec §场景:装配期资产损坏退出 2 — the asset is *present* (pre-flight passes)
    but corrupt, so ``PlaybackBackend`` construction raises ``ValueError`` during
    assembly. That is an assembly-phase failure (exit 2), distinct from the
    missing-asset pre-flight (exit 3). Induced at the ``build_demo_planner`` seam
    with the exact phrasing the real loader emits.
    """

    def _raise_value_error(_key: str, *, exit_stack: object) -> object:
        raise ValueError("invalid cassette format at line 1")

    monkeypatch.setattr(demo_mod, "build_demo_planner", _raise_value_error)

    code, stdout, stderr = _run_main(["demo", "run", "cpu_saturation"], capsys, monkeypatch)

    assert code == 2
    assert stdout == ""
    assert "internal: ValueError:" in stderr
    assert "invalid cassette format" in stderr
    assert "Traceback" not in stderr


def test_demo_run_runtime_cassette_miss_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Runtime Agent drift (``CassetteMiss``) → exit 2, single internal line.

    Spec §场景:运行期 cassette miss 退出 2 — distinct from corrupt-asset (an
    assembly ``ValueError``): here assembly *succeeds* but the model request key
    finds no matching record at run time. We run the real assembly, then swap in
    an empty-cassette ``PlaybackBackend`` so the first ``messages_create`` misses
    — the genuine runtime-drift path. The CLI wraps it as one
    ``internal: CassetteMiss: ...`` line, never a traceback.
    """

    from hostlens.agent.backends.playback import PlaybackBackend

    real_build = demo_mod.build_demo_planner
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")

    def _build_then_break(key: str, *, exit_stack: object) -> tuple[PlannerAgent, ReplayTarget]:
        planner, target = real_build(key, exit_stack=exit_stack)  # type: ignore[arg-type]
        # Replace the matched backend with one whose cassette always misses.
        planner._loop._backend = PlaybackBackend(cassette_path=empty)
        return planner, target

    monkeypatch.setattr(demo_mod, "build_demo_planner", _build_then_break)

    code, stdout, stderr = _run_main(["demo", "run", "cpu_saturation"], capsys, monkeypatch)

    assert code == 2
    assert stdout == ""
    assert "internal: CassetteMiss:" in stderr
    assert "Traceback" not in stderr


def test_run_cancelled_wrapped_no_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Agent cancellation (``asyncio.CancelledError``) → exit 2, no traceback.

    Spec §命令在任何分支均禁止向用户输出 Python traceback. ``CancelledError`` is a
    ``BaseException`` (not ``Exception``), so the generic handler can't catch it;
    the loop propagates it verbatim on agent cancellation. The CLI must wrap it as
    one ``internal: CancelledError`` line, never a traceback.
    """

    import asyncio

    real_build = demo_mod.build_demo_planner

    def _build_then_cancel(key: str, *, exit_stack: object) -> tuple[PlannerAgent, ReplayTarget]:
        planner, target = real_build(key, exit_stack=exit_stack)  # type: ignore[arg-type]

        async def _cancel(*_args: object, **_kwargs: object) -> object:
            raise asyncio.CancelledError

        monkeypatch.setattr(planner, "run", _cancel)
        return planner, target

    monkeypatch.setattr(demo_mod, "build_demo_planner", _build_then_cancel)

    code, stdout, stderr = _run_main(["demo", "run", "cpu_saturation"], capsys, monkeypatch)

    assert code == 2
    assert stdout == ""
    assert "internal: CancelledError:" in stderr
    assert "Traceback" not in stderr


# --------------------------------------------------------------------------- #
# demo list
# --------------------------------------------------------------------------- #


def test_demo_list_emits_registry(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``demo list`` emits every registry key + its one-line description, exit 0.

    Spec §场景:列出场景 — the listed set must equal what ``demo run`` accepts
    (single SOT). We assert cpu_saturation (proven runnable above) appears.
    """

    from hostlens.demo.registry import list_scenarios

    code, stdout, _stderr = _run_main(["demo", "list"], capsys, monkeypatch)

    assert code == 0
    for scenario in list_scenarios():
        assert scenario.key in stdout
        assert scenario.description in stdout


def test_demo_list_empty_registry_exit_0(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Empty registry → ``无可用场景`` + exit 0, no traceback.

    Spec §场景:registry 为空时不崩 — an empty scenario set must not crash; it
    emits the no-scenarios notice and exits 0.
    """

    monkeypatch.setattr(demo_mod, "list_scenarios", list)

    code, stdout, stderr = _run_main(["demo", "list"], capsys, monkeypatch)

    assert code == 0
    assert "无可用场景" in stdout
    assert "Traceback" not in stderr
