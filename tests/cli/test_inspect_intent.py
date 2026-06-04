"""Tests for the ``hostlens inspect --intent`` Planner Agent CLI path (Group 3b).

Spec: ``openspec/changes/add-intent-cli/specs/inspect-cli-command/spec.md``.

These tests drive ``hostlens.cli.main`` (the project entrypoint) so the
``click.UsageError`` → exit 3 wrapper runs, mirroring ``tests/cli/test_inspect.py``.
``_run_main`` patches ``sys.argv`` and captures the ``SystemExit`` the wrapper
raises (``CliRunner.invoke`` is intentionally NOT used — it calls the raw Typer
``app`` and would observe Click's default exit 2 for usage errors).

The backend factory is the only seam these tests replace: several cases
monkeypatch ``hostlens.cli._intent.create_backend`` so the CLI runs its full
``_run_intent`` + ``RichLiveObserver`` + ``render_planner_result`` path while a
deterministic backend (scripted ``FakeBackend`` / record-then-replay
``PlaybackBackend`` / a persistently-failing fake) stands in for a paid API.
This is the orchestrator-endorsed low-friction approach: the unit under test is
the CLI path, not the backend factory. ``asyncio_mode = "auto"`` (pyproject) —
no ``@pytest.mark.asyncio``; no ``@pytest.mark.live`` (every backend is fake).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from hostlens.agent.backend import (
    BackendCapabilities,
    LLMBackend,
    MessageResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from hostlens.agent.backends.fake import FakeBackend
from hostlens.agent.backends.playback import PlaybackBackend
from hostlens.cli import main

# --------------------------------------------------------------------------- #
# Backend / settings injection fixtures
# --------------------------------------------------------------------------- #
#
# The ``--intent`` path calls ``load_settings()`` then ``create_backend(settings)``.
# ``create_backend`` raises ``ConfigError`` when ``settings.backend is None`` (no
# LLM block configured). We provide a configured backend block by env so the
# AgentLoop's ``settings.agent`` is non-None; the actual backend object is
# swapped at ``hostlens.cli._intent.create_backend`` so no real API is hit.


_DEFAULT_CAPS = BackendCapabilities(
    prompt_caching=True,
    tool_use=True,
    structured_output=True,
    parallel_tool_use=True,
    extended_thinking=False,
    vision=True,
    streaming=False,
)

# run_inspector tool_use input must satisfy RunInspectorInput (extra="forbid").
# Points at the local-host target + hello.echo inspector wired by the fixtures.
_RUN_INSPECTOR_INPUT = {"target_name": "local-host", "inspector_name": "hello.echo"}


@pytest.fixture
def targets_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``Settings.targets_config_path`` at a tmp file with one local target."""

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
    """Point inspectors search paths at an empty user dir (builtins stay visible)."""

    user_path = tmp_path / "inspectors"
    user_path.mkdir()
    monkeypatch.setenv("HOSTLENS_INSPECTORS_SEARCH_PATHS", str(user_path))
    return user_path


@pytest.fixture
def agent_backend_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure a ``backend`` + ``agent`` namespace via env.

    ``backend.type=anthropic_api`` + a dummy ``api_key`` makes ``load_settings``
    build a non-None ``settings.backend`` / ``settings.agent`` (the AgentLoop
    requires ``settings.agent``). The backend object itself is replaced by
    monkeypatching ``create_backend`` in the tests, so the dummy key is never
    used to reach the network.
    """

    monkeypatch.setenv("HOSTLENS_BACKEND__TYPE", "anthropic_api")
    monkeypatch.setenv("HOSTLENS_BACKEND__API_KEY", "sk-ant-test-not-real")
    monkeypatch.setenv("HOSTLENS_AGENT__PRIMARY_MODEL", "claude-test")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op the loop retry backoff so the degraded-path test does not sleep."""

    async def _instant(_delay: float) -> None:
        return None

    monkeypatch.setattr("hostlens.agent.loop.asyncio.sleep", _instant)


def _run_main(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[int, str, str]:
    """Invoke ``hostlens.cli.main`` with patched argv; return (code, stdout, stderr)."""

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
# MessageResponse builders (shared with the planner test shapes)
# --------------------------------------------------------------------------- #


def _msg(
    *,
    content: list[Any],
    stop_reason: str,
    input_tokens: int = 1,
    output_tokens: int = 1,
) -> MessageResponse:
    return MessageResponse(
        id="msg_x",
        model="claude-test",
        role="assistant",
        content=content,
        stop_reason=cast(Any, stop_reason),
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _end_turn(text: str) -> MessageResponse:
    return _msg(content=[TextBlock(type="text", text=text)], stop_reason="end_turn")


def _tool_use_turn(*, block_id: str) -> MessageResponse:
    return _msg(
        content=[
            ToolUseBlock(
                type="tool_use",
                id=block_id,
                name="run_inspector",
                input=_RUN_INSPECTOR_INPUT,
            )
        ],
        stop_reason="tool_use",
    )


def _correlate_turn(*, block_id: str) -> MessageResponse:
    """A Diagnostician ``correlate_findings`` tool_use turn citing label ``F1``.

    ``F1`` is the label the FindingStore assigns to the single hello.echo finding
    the Planner stamps + seeds, so the hit-check passes and the harvest resolves
    it to a real id.
    """

    return _msg(
        content=[
            ToolUseBlock(
                type="tool_use",
                id=block_id,
                name="correlate_findings",
                input={
                    "description": "hello.echo 正常",
                    "confidence": "low",
                    "supporting_findings": ["F1"],
                    "suggested_actions": [],
                },
            )
        ],
        stop_reason="tool_use",
    )


def _patch_backend(monkeypatch: pytest.MonkeyPatch, backend: LLMBackend) -> None:
    """Replace ``create_backend`` in the CLI intent module with a constant factory.

    This is the single seam: the CLI still goes through ``build_planner`` →
    ``PlannerAgent`` → ``AgentLoop`` → ``_run_intent`` → observer → render, but
    talks to ``backend`` instead of a real API.
    """

    monkeypatch.setattr("hostlens.cli._intent.create_backend", lambda _settings: backend)


# --------------------------------------------------------------------------- #
# 5.3① / ② — mutual exclusion usage errors
# --------------------------------------------------------------------------- #


def test_intent_both_missing_exits_3(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Neither --inspector nor --intent -> exit 3 + "must provide exactly one".

    Spec §场景:缺 --inspector 且缺 --intent 报错.
    """

    exit_code, _stdout, stderr = _run_main(["inspect", "local-host"], capsys, monkeypatch)
    assert exit_code == 3
    assert "must provide exactly one of --inspector or --intent" in stderr
    assert "Traceback" not in stderr


def test_intent_both_set_exits_3(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both --inspector and --intent -> exit 3 + "mutually exclusive".

    Spec §场景:--inspector 与 --intent 同时提供报错.
    """

    exit_code, _stdout, stderr = _run_main(
        ["inspect", "local-host", "--inspector", "hello.echo", "--intent", "检查健康"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 3
    assert "mutually exclusive" in stderr
    assert "Traceback" not in stderr


# --------------------------------------------------------------------------- #
# 5.3③ — inspector-only path not regressed (smoke)
# --------------------------------------------------------------------------- #


def test_inspector_only_path_still_works_smoke(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """``--inspector hello.echo`` (no --intent) still runs the M1 pipeline.

    Confirms making --inspector optional + adding the 0a mutual-exclusion gate
    did not break the existing single-inspector path (spec §场景:仅 --inspector
    走 M1 单 Inspector 管线 行为不变).
    """

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--inspector", "hello.echo"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 0, stderr
    assert "Hostlens Inspection Report" in stdout


# --------------------------------------------------------------------------- #
# 5.3④ — backend not configured -> exit 3 pointing at doctor
# --------------------------------------------------------------------------- #


def test_intent_backend_not_configured_exits_3(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
) -> None:
    """``--intent`` with no backend block -> exit 3 + doctor hint, no traceback.

    Spec §场景:backend 未配置报配置错误 — ``create_backend`` raises ConfigError
    (settings.backend is None) and the CLI maps it to exit 3.
    """

    # Deliberately do NOT use the agent_backend_env fixture so backend is None.
    # Clear any inherited backend env from the operator's shell / .env.
    for var in ("HOSTLENS_BACKEND__TYPE", "HOSTLENS_BACKEND__API_KEY"):
        monkeypatch.delenv(var, raising=False)

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 3
    assert stdout == ""
    assert "backend not configured" in stderr
    assert "hostlens doctor" in stderr
    assert "Traceback" not in stderr


# --------------------------------------------------------------------------- #
# 5.3⑤ — playback end-to-end: narrative + findings, progress on stderr, exit 0
# --------------------------------------------------------------------------- #


def test_intent_playback_end_to_end_md_exit_0(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
    agent_backend_env: None,
) -> None:
    """End-to-end ``--intent`` over the full Planner → Diagnostician pipeline.

    Spec §场景:实时进度与报告分流 + §场景:md 模式输出综述与 findings 摘要 +
    §场景:健康巡检退出 0. The Planner calls run_inspector (hello.echo on
    local-host → one info finding) then narrates; the Diagnostician records one
    hypothesis (citing F1) then narrates; reconciled status=ok → exit 0. A single
    shared FakeBackend serves all four turns in order across both agents (the
    "create_backend only once" contract). stdout carries the diagnosis narrative
    + findings summary + 根因假设 + telemetry; stderr carries both progress trees.
    """

    backend = FakeBackend(
        responses=[
            _tool_use_turn(block_id="tu_1"),
            _end_turn("机器健康，未发现严重问题。"),  # noqa: RUF001 (planner)
            _correlate_turn(block_id="tu_2"),
            _end_turn("综合诊断：无根因风险。"),  # noqa: RUF001 (diagnostician)
        ]
    )
    _patch_backend(monkeypatch, cast(LLMBackend, backend))

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查这台机器的健康状况"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 0, stderr
    # Diagnosis narrative + findings summary + root-cause section land on stdout.
    assert "综合诊断：无根因风险。" in stdout  # noqa: RUF001
    assert "## Findings" in stdout
    assert "hello received" in stdout  # hello.echo emits an info finding
    assert "## 根因假设" in stdout
    # Telemetry line on stdout (reconciled status).
    assert "status=ok" in stdout
    # Both progress trees land on stderr, not stdout.
    assert "run_inspector" in stderr
    assert "correlate_findings" in stderr
    assert "run_inspector" not in stdout
    assert "correlate_findings" not in stdout


# --------------------------------------------------------------------------- #
# 5.3⑥ — json mode emits a parseable DiagnosticianResult
# --------------------------------------------------------------------------- #


def test_intent_playback_json_mode_parseable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
    agent_backend_env: None,
) -> None:
    """``--intent --format json`` -> stdout is a valid Report JSON (BREAKING).

    Migrated to the Report contract: the --intent json surface is now an
    assembled ``Report`` (``meta`` / ``findings`` / ``hypotheses`` /
    ``metadata[diagnosis_narrative]``), not a ``DiagnosticianResult`` — the old
    top-level ``planner_result`` / ``diagnostician_loop`` keys are gone.
    """

    from hostlens.reporting.models import Report

    backend = FakeBackend(
        responses=[
            _tool_use_turn(block_id="tu_1"),
            _end_turn("综述完成"),  # planner
            _correlate_turn(block_id="tu_2"),
            _end_turn("诊断完成"),  # diagnostician
        ]
    )
    _patch_backend(monkeypatch, cast(LLMBackend, backend))

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查这台机器的健康状况", "--format", "json"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 0, stderr
    report = Report.model_validate_json(stdout)
    assert report.meta is not None
    assert report.meta.status == "ok"
    assert report.findings  # hello.echo produced one finding
    assert report.metadata["diagnosis_narrative"] == "诊断完成"

    payload = json.loads(stdout)
    assert "meta" in payload
    assert "planner_result" not in payload
    assert "diagnostician_loop" not in payload


# --------------------------------------------------------------------------- #
# 5.3⑦ — degradation: exit 2 + partial output retained
# --------------------------------------------------------------------------- #


def test_intent_degraded_max_tokens_no_result_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
    agent_backend_env: None,
) -> None:
    """Planner ``max_tokens`` degradation BEFORE any inspector -> no-result, exit 2.

    Migrated to the Report contract: a Planner stop_reason=max_tokens with a text
    block (and no preceding run_inspector tool call) finalizes
    degraded_token_budget with an EMPTY per-run collector. Per design D-5 the
    no-result judge is "the collector is empty" — so there is no Report to
    assemble, render, or persist; the CLI emits the generic no-result degrade note
    to stderr, leaves stdout empty (no faked skeleton), exits 2, and must NOT
    retry.
    """

    backend = FakeBackend(
        responses=[
            _msg(content=[TextBlock(type="text", text="部分输出")], stop_reason="max_tokens")
        ]
    )
    _patch_backend(monkeypatch, cast(LLMBackend, backend))

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 2
    # No Report → empty stdout (no faked skeleton).
    assert stdout == ""
    # Generic no-result degrade note on stderr (not a hardcoded status string).
    assert "no inspector results collected" in stderr
    assert "no report produced" in stderr
    assert "Traceback" not in stderr


# --------------------------------------------------------------------------- #
# add-backend-disable-thinking 4.7④ — end-to-end: a normalized thinking-block
# BackendError fails loud through the loop to the CLI boundary as ONE clean line.
# --------------------------------------------------------------------------- #


class _ThinkingBlockBackend:
    """Structural ``LLMBackend`` raising the already-normalized thinking-block error
    ``AnthropicAPIBackend`` produces when a provider ignores ``thinking:disabled``
    and returns a ``type="thinking"`` content block.

    A ``MessageResponse`` carrying a ``thinking`` block cannot be constructed (the
    discriminated union rejects it), so the realistic seam is the post-normalization
    ``BackendError(kind="unsupported_content_block")`` the production backend raises
    out of ``messages_create``. This drives the genuine AgentLoop → CLI-boundary
    chain: the error is non-retryable, so the loop fail-louds it (does NOT finalize a
    degraded LoopResult) and the CLI's ``except Exception`` wraps it into one
    ``internal: BackendError: ...`` line.
    """

    name = "thinking-block"

    def __init__(self) -> None:
        self.capabilities = _DEFAULT_CAPS

    async def messages_create(
        self,
        *,
        model: str,
        system: list[dict[str, Any]] | str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        timeout: float,
    ) -> MessageResponse:
        from hostlens.core.exceptions import BackendError

        raise BackendError(
            "response contains an unmodeled content block "
            "(provider may have ignored thinking:disabled)",
            backend_name="anthropic_api",
            kind="unsupported_content_block",
        )


def test_intent_thinking_block_fails_loud_one_line_no_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
    agent_backend_env: None,
) -> None:
    """A provider thinking block (normalized to BackendError) -> exit≠0 + one line.

    add-backend-disable-thinking tasks 4.7④ / design D-5: the normalized
    ``BackendError(kind="unsupported_content_block")`` is NOT a degradation — the
    loop re-raises it (non-retryable) and the CLI wraps it into a single
    ``internal: BackendError: ...`` stderr line, never a degraded note and never a
    pydantic traceback. Locks the fail-loud chain so a future ``except BackendError``
    in the loop can't silently downgrade it.
    """

    _patch_backend(monkeypatch, cast(LLMBackend, _ThinkingBlockBackend()))

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"],
        capsys,
        monkeypatch,
    )
    assert exit_code != 0
    assert exit_code == 2
    # Exactly one ``internal:`` error line carrying the normalized kind; NOT a
    # degradation. (stderr also carries the RichLiveObserver progress tree —
    # ``agent run`` / ``turn N`` — but the error itself is a single line.)
    internal_lines = [ln for ln in stderr.splitlines() if "internal:" in ln]
    assert len(internal_lines) == 1
    error_line = internal_lines[0]
    assert "internal: BackendError:" in error_line
    assert "unsupported_content_block" in error_line
    assert "degraded run" not in stderr
    # No leaked stack frames / pydantic internals on either stream.
    assert "Traceback" not in stderr
    assert "ValidationError" not in stderr
    assert "pydantic" not in stderr.lower()
    # No PlannerResult was produced, so nothing rendered to stdout.
    assert stdout == ""


# --------------------------------------------------------------------------- #
# 5.4 — degradation (BackendUnavailable) vs fixture-failure (CassetteMiss)
# --------------------------------------------------------------------------- #


class _PersistentUnavailableBackend:
    """Structural ``LLMBackend`` that raises ``BackendUnavailable`` on every call.

    Drives the loop's retry budget to exhaustion → finalize
    ``failed_api_unavailable`` (no tool result was ever produced). The CLI must
    map that terminal_status to exit 2 (degraded/failed) and NOT retry on top of
    the loop (ADR-005).
    """

    name = "persistent-unavailable"

    def __init__(self) -> None:
        self.capabilities = _DEFAULT_CAPS
        self.calls = 0

    async def messages_create(
        self,
        *,
        model: str,
        system: list[dict[str, Any]] | str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        timeout: float,
    ) -> MessageResponse:
        from hostlens.core.exceptions import BackendUnavailable

        self.calls += 1
        raise BackendUnavailable("down", backend_name="persistent-unavailable")


def test_intent_persistent_unavailable_no_result_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
    agent_backend_env: None,
) -> None:
    """Persistent BackendUnavailable -> Planner failed_api_unavailable -> no-result.

    Spec §场景:Planner API 不可达无结果退出 2. The Planner loop owns retry and
    finalizes failed_api_unavailable; per design D-5 there is NO DiagnosticianResult
    (reconcile_status would raise), so the CLI takes the no-result path: a one-line
    degrade note on stderr, EMPTY stdout (no faked skeleton), exit 2, and the CLI
    never retries on top of the loop.
    """

    backend = _PersistentUnavailableBackend()
    _patch_backend(monkeypatch, cast(LLMBackend, backend))

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 2
    # No-result degrade note, not an internal-wrap line.
    assert "degraded run" in stderr
    assert "failed_api_unavailable" in stderr
    assert "no report produced" in stderr
    assert "internal:" not in stderr
    assert "Traceback" not in stderr
    # The loop owns retry (initial + 3 = 4); the CLI must not multiply it, and the
    # Diagnostician (reusing the same backend) is never launched (no 5th call).
    assert backend.calls == 4
    # No DiagnosticianResult was produced, so stdout stays empty (no skeleton).
    assert stdout == ""


def test_intent_cassette_miss_wrapped_internal_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
    agent_backend_env: None,
    tmp_path: Path,
) -> None:
    """``CassetteMiss`` (non-retriable) -> loop re-raises -> CLI wraps internal.

    Spec proposal FM5 + §需求 CLI 边界: a ``CassetteMiss`` is NOT a degradation
    (the loop does not finalize it) — it propagates out of ``run`` and the CLI's
    ``except Exception`` wraps it into one ``internal: CassetteMiss: ...`` line →
    exit 2. This asserts the wrap (NOT a terminal_status degradation) and that no
    traceback leaks.
    """

    # An empty cassette always misses on the first request.
    empty_cassette = tmp_path / "empty.jsonl"
    empty_cassette.write_text("")
    _patch_backend(monkeypatch, PlaybackBackend(cassette_path=empty_cassette))

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 2
    assert "internal: CassetteMiss:" in stderr
    # NOT a degradation note — the loop never finalized this error.
    assert "degraded run" not in stderr
    assert "Traceback" not in stderr
    # No PlannerResult was produced, so nothing rendered to stdout.
    assert stdout == ""


# --------------------------------------------------------------------------- #
# 5.5 — secret redaction: a sensitive string in a tool failure envelope must
# not surface un-redacted on either stream.
# --------------------------------------------------------------------------- #


def test_intent_secret_in_tool_failure_not_leaked(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Path,
    user_inspectors_dir: Path,
    agent_backend_env: None,
) -> None:
    """A secret-bearing path in a tool failure is scrubbed before either stream.

    Spec CLAUDE.md §4.4 / §7: the dispatch boundary scrubs exception messages
    (``scrub_exception_message``) before they reach the error envelope. The CLI
    renders findings (stdout) + progress (stderr) from the already-scrubbed
    invocation and must NOT re-derive / un-scrub. We register a
    ``run_inspector``-named tool whose handler raises with a sensitive
    ``/Users/<name>`` path; ``scrub_exception_message`` redacts the home-dir
    path before it reaches the error envelope, so the original username must not
    appear on either stream.

    ``ToolSpec`` is frozen, so we cannot mutate the real ``run_inspector``'s
    handler. Instead we patch the CLI's ``register_default_tools`` (matching its
    new ``(registry, *, clock=None, collector=None)`` signature) to register a
    leaky stub spec (same name, so the loop dispatches it). This still exercises
    the real dispatch scrub boundary + CLI rendering — only the handler body
    differs.

    Because the handler RAISES (run_inspector fails, nothing appended to the
    per-run collector), the collector stays empty → no Report → the no-result path
    (exit 2, empty stdout). The redaction surface under test is the stderr
    progress / error envelope, which is unaffected by the no-result outcome.
    """

    import hostlens.orchestration.pipeline as intent_mod
    from hostlens.tools.registry import ToolRegistry
    from hostlens.tools.schemas.run_inspector import RunInspectorInput, RunInspectorOutput

    secret_user = "topsecretuser"
    secret_path = f"/Users/{secret_user}/.ssh/id_rsa_supersecret"

    async def _leaky_handler(args: RunInspectorInput, ctx: Any) -> RunInspectorOutput:
        raise RuntimeError(f"failed reading {secret_path}")

    def _register_leaky(
        registry: ToolRegistry, *, clock: Any = None, collector: Any = None
    ) -> None:
        from hostlens.tools.base import ToolSpec

        registry.register(
            ToolSpec(
                name="run_inspector",
                version="1.0.0",
                input_schema=RunInspectorInput,
                output_schema=RunInspectorOutput,
                handler=cast(Any, _leaky_handler),
                agent_description="stub leaky run inspector",
                mcp_description="stub",
                cli_help=None,
                surfaces=cast(Any, {"agent"}),
                side_effects=cast(Any, "read"),
                requires_approval=False,
                sensitive_output=True,
                timeout=30.0,
            )
        )

    monkeypatch.setattr(intent_mod, "register_default_tools", _register_leaky)

    backend = FakeBackend(
        responses=[
            _tool_use_turn(block_id="tu_1"),
            _end_turn("巡检遇到工具错误。"),  # planner
            _end_turn("诊断结束。"),
        ]
    )
    _patch_backend(monkeypatch, cast(LLMBackend, backend))

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"],
        capsys,
        monkeypatch,
    )
    # The leaky handler raised → empty collector → no-result path (exit 2).
    assert exit_code == 2
    assert stdout == ""
    # The sensitive username / full path must not appear on EITHER stream.
    assert secret_user not in stdout
    assert secret_user not in stderr
    assert secret_path not in stdout
    assert secret_path not in stderr
