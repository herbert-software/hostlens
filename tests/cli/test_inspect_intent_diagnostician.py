"""Tests for the ``hostlens inspect --intent`` Planner → Diagnostician CLI path.

Spec: ``openspec/changes/add-diagnostician-agent/specs/inspect-cli-command/spec.md``
(group D, tasks 5.1 / 5.2 / 5.3 / 5.4).

The ``--intent`` path now runs TWO agents back-to-back: the Planner condenses the
intent into stamped findings, then the Diagnostician correlates them into
root-cause hypotheses. The single seam these tests replace is
``hostlens.cli._intent.create_backend`` — a scripted ``FakeBackend`` stands in
for a paid API and serves canned ``MessageResponse`` objects **in order across
both agents** (the same backend instance is reused, per the "create_backend only
once" contract). This drives the full CLI path
(``run_intent_diagnosis`` → id-stamp → ``DiagnosticianAgent`` →
``render_diagnostician_result`` → ``_compute_diag_exit_code``) without a network
call.

``_run_main`` drives ``hostlens.cli.main`` (so the ``click.UsageError`` → exit 3
wrapper runs) and captures the ``SystemExit`` exit code, mirroring
``test_inspect_intent.py``. ``asyncio_mode = "auto"`` (pyproject) — no
``@pytest.mark.asyncio``; every backend is fake so no ``@pytest.mark.live``.
"""

from __future__ import annotations

import sys
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
from hostlens.cli import main

# --------------------------------------------------------------------------- #
# Fixtures (mirror test_inspect_intent.py so the CLI assembles a real local
# target + the builtin hello.echo inspector + a configured backend block)
# --------------------------------------------------------------------------- #


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
_RUN_INSPECTOR_INPUT = {"target_name": "local-host", "inspector_name": "hello.echo"}


@pytest.fixture
def targets_yaml(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
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
def user_inspectors_dir(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point inspectors search paths at an empty user dir (builtins stay visible)."""

    user_path = tmp_path / "inspectors"
    user_path.mkdir()
    monkeypatch.setenv("HOSTLENS_INSPECTORS_SEARCH_PATHS", str(user_path))
    return user_path


@pytest.fixture
def agent_backend_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure a ``backend`` + ``agent`` namespace via env (object swapped in test)."""

    monkeypatch.setenv("HOSTLENS_BACKEND__TYPE", "anthropic_api")
    monkeypatch.setenv("HOSTLENS_BACKEND__API_KEY", "sk-ant-test-not-real")
    monkeypatch.setenv("HOSTLENS_AGENT__PRIMARY_MODEL", "claude-test")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op the loop retry backoff so degraded-path tests do not sleep."""

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


def _patch_backend(monkeypatch: pytest.MonkeyPatch, backend: LLMBackend) -> None:
    """Replace ``create_backend`` so BOTH agents talk to ``backend`` (one instance)."""

    monkeypatch.setattr("hostlens.cli._intent.create_backend", lambda _settings: backend)


def _fake(responses: list[MessageResponse]) -> LLMBackend:
    """A ``FakeBackend`` typed as ``LLMBackend`` (its ``name`` ClassVar trips the
    structural check; the cast is the same convention used in test_inspect_intent.py).
    """

    return cast(LLMBackend, FakeBackend(responses=responses))


# --------------------------------------------------------------------------- #
# MessageResponse builders
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


def _planner_run_inspector(*, block_id: str = "tu_plan") -> MessageResponse:
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


def _correlate(
    *,
    block_id: str = "tu_corr",
    description: str = "可能是配置漂移",
    confidence: str = "medium",
    supporting_findings: list[str] | None = None,
    suggested_actions: list[str] | None = None,
) -> MessageResponse:
    return _msg(
        content=[
            ToolUseBlock(
                type="tool_use",
                id=block_id,
                name="correlate_findings",
                input={
                    "description": description,
                    "confidence": confidence,
                    "supporting_findings": supporting_findings or ["F1"],
                    "suggested_actions": suggested_actions or ["复查配置"],
                },
            )
        ],
        stop_reason="tool_use",
    )


# Canonical happy-path script: Planner runs one inspector then narrates;
# Diagnostician records one hypothesis (citing F1) then narrates. The single
# shared FakeBackend serves these four responses in order across both agents.
def _happy_script(
    *,
    planner_narrative: str = "巡检完成。",
    diag_narrative: str = "诊断完成：未见严重问题。",  # noqa: RUF001
    with_hypothesis: bool = True,
) -> list[MessageResponse]:
    script: list[MessageResponse] = [
        _planner_run_inspector(),
        _end_turn(planner_narrative),
    ]
    if with_hypothesis:
        script.append(_correlate())
    script.append(_end_turn(diag_narrative))
    return script


# --------------------------------------------------------------------------- #
# 5.1 — orchestration: two-stage progress on stderr, report-only on stdout
# --------------------------------------------------------------------------- #


def test_intent_two_stage_progress_stderr_report_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
) -> None:
    """Planner + Diagnostician both stream progress to stderr; stdout = report only.

    Spec §场景:实时进度与报告分流 — stderr carries two progress segments (the
    Planner's run_inspector + the Diagnostician's correlate_findings), stdout
    carries only the rendered report (root-cause section + telemetry).
    """

    _patch_backend(monkeypatch, _fake(_happy_script()))

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查这台机器的健康状况"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 0, stderr
    # Two-stage progress on stderr: Planner's run_inspector + Diag's correlate.
    assert "run_inspector" in stderr
    assert "correlate_findings" in stderr
    # The progress tool names must NOT leak onto stdout.
    assert "run_inspector" not in stdout
    assert "correlate_findings" not in stdout
    # stdout carries the rendered report (root-cause section + findings).
    assert "## 根因假设" in stdout
    assert "## Findings" in stdout
    assert "可能是配置漂移" in stdout


def test_intent_no_result_path_planner_api_unavailable_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
) -> None:
    """Planner failed_api_unavailable -> no-result path: exit 2, empty stdout.

    Spec §场景:Planner API 不可达无结果退出 2 — the Diagnostician is never
    launched, no DiagnosticianResult is produced, stdout stays empty (no faked
    skeleton), stderr gets one degrade line, exit 2.
    """

    backend = _PersistentUnavailableBackend()
    _patch_backend(monkeypatch, cast(LLMBackend, backend))

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 2
    assert stdout == ""
    assert "failed_api_unavailable" in stderr
    assert "no report produced" in stderr
    assert "Traceback" not in stderr
    # The loop owns retry (initial + 3 = 4); the CLI must not multiply it, and
    # the Diagnostician backend reuse must not add a 5th call (it never runs).
    assert backend.calls == 4


class _PersistentUnavailableBackend:
    """Structural ``LLMBackend`` that raises ``BackendUnavailable`` on every call."""

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


# --------------------------------------------------------------------------- #
# 5.2 — render_diagnostician_result md/json (CLI surface coverage)
# --------------------------------------------------------------------------- #


def test_intent_md_renders_narrative_findings_hypotheses_telemetry(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
) -> None:
    """md mode: narrative + ## Findings + ## 根因假设 (with evidence) + telemetry.

    Spec §场景:md 模式输出综述、findings 摘要与根因假设.
    """

    _patch_backend(monkeypatch, _fake(_happy_script()))

    exit_code, stdout, _stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 0
    assert "诊断完成：未见严重问题。" in stdout  # noqa: RUF001 narrative
    assert "## Findings" in stdout
    assert "## 根因假设" in stdout
    assert "### 可能是配置漂移" in stdout
    assert "**Confidence:** medium" in stdout
    assert "**Supporting findings:**" in stdout
    # Telemetry line: status reflects the reconciled DiagnosticianResult.status.
    assert "status=ok" in stdout


def test_intent_md_empty_hypotheses_placeholder(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
) -> None:
    """No hypotheses -> ``_暂无根因假设_`` placeholder, rest renders, no error.

    Spec §场景:无根因假设时显示占位 — an ``ok`` end_turn that produced narrative
    text but recorded no correlate_findings hypothesis.
    """

    _patch_backend(monkeypatch, _fake(_happy_script(with_hypothesis=False)))

    exit_code, stdout, _stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 0
    assert "## 根因假设" in stdout
    assert "_暂无根因假设_" in stdout
    assert "## Findings" in stdout  # findings still rendered


def test_intent_md_empty_narrative_no_empty_heading(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
) -> None:
    """Degraded -> empty narrative renders without an empty title; rest intact.

    Spec §场景:降级致 narrative 为空时渲染容忍. A diagnostician ``max_tokens``
    stop with NO text block makes the loop finalize degraded_token_budget with an
    empty final_text; the renderer must emit findings + 根因假设 placeholder +
    telemetry and never lead with a blank line / empty heading.
    """

    script = [
        _planner_run_inspector(),
        _end_turn("巡检完成。"),
        # Diagnostician: max_tokens with no text -> empty narrative, degraded.
        _msg(content=[], stop_reason="max_tokens"),
    ]
    _patch_backend(monkeypatch, _fake(script))

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 2  # degraded_token_budget
    # No empty narrative heading: the first non-empty content line is ## Findings.
    first_line = stdout.lstrip("\n").splitlines()[0]
    assert first_line == "## Findings"
    assert "_暂无根因假设_" in stdout
    assert "degraded run" in stderr


def test_intent_empty_response_vs_ok_no_hypothesis_only_status_differs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
) -> None:
    """empty_response (findings non-empty) vs ok-no-hypothesis (findings non-empty):
    the findings + 根因假设 body is byte-identical; only the status differs.

    Migrated to the Report contract: the --intent json/md surface is now an
    assembled ``Report`` and the md telemetry line is ``status=... tokens_in=...
    tokens_out=...`` (no ``turns=`` — the loop counters are summed into
    token_usage). Both paths still produce the SAME ``## Findings`` +
    ``_暂无根因假设_`` placeholder body; the only observable differences are the
    telemetry ``status=`` token (``ok`` vs ``empty_response``), the exit code, and
    the (legitimately) present-vs-absent narrative. This locks the distinction
    down to ``Report.meta.status``, not a divergent rendering.
    """

    # Path A: ok end_turn but no hypothesis recorded (carries a narrative).
    _patch_backend(monkeypatch, _fake(_happy_script(with_hypothesis=False)))
    code_a, stdout_a, _ = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"],
        capsys,
        monkeypatch,
    )

    # Path B: empty_response — diagnostician returns an empty content list on a
    # plain end_turn so the loop finalizes empty_response (no text, no tool use →
    # empty narrative, distinct from an ok end_turn with text).
    script_b = [
        _planner_run_inspector(),
        _end_turn("巡检完成。"),
        _msg(content=[], stop_reason="end_turn"),
    ]
    _patch_backend(monkeypatch, _fake(script_b))
    code_b, stdout_b, _ = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"],
        capsys,
        monkeypatch,
    )

    assert code_a == 0
    assert code_b == 2

    # The findings + 根因假设 body (everything from ``## Findings`` up to the
    # telemetry line) must be byte-identical across the two adjacent paths. The
    # Report telemetry line starts with ``status=`` (no ``turns=``).
    def _body(text: str) -> str:
        lines = text.rstrip("\n").splitlines()
        start = lines.index("## Findings")
        end = next(i for i, ln in enumerate(lines) if ln.startswith("status="))
        return "\n".join(lines[start:end]).rstrip("\n")

    assert _body(stdout_a) == _body(stdout_b)

    # The ONLY telemetry difference is the status token.
    def _status_token(text: str) -> str:
        line = next(ln for ln in text.splitlines() if ln.startswith("status="))
        return next(tok for tok in line.split() if tok.startswith("status="))

    assert _status_token(stdout_a) == "status=ok"
    assert _status_token(stdout_b) == "status=empty_response"


# --------------------------------------------------------------------------- #
# 5.3 — exit codes mapped from DiagnosticianResult
# --------------------------------------------------------------------------- #


def test_intent_healthy_exit_0(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
) -> None:
    """status=ok + no critical finding -> exit 0 (spec §场景:健康巡检退出 0)."""

    _patch_backend(monkeypatch, _fake(_happy_script()))
    exit_code, _stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 0, stderr


# critical-finding → exit 1 migrated to the Report contract in
# ``test_inspect_intent_report.py::test_intent_critical_finding_exit_1`` (which
# feeds the per-run collector so the assembled Report carries the critical
# finding and ``_compute_intent_report_exit_code`` maps it to exit 1). Not
# re-covered here.


def test_intent_empty_response_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
) -> None:
    """Diagnostician empty_response -> exit 2, findings + placeholder still on stdout.

    Spec §场景:诊断师空响应 empty_response 退出 2.
    """

    script = [
        _planner_run_inspector(),
        _end_turn("巡检完成。"),
        _msg(content=[], stop_reason="end_turn"),
    ]
    _patch_backend(monkeypatch, _fake(script))

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 2
    assert "## Findings" in stdout
    assert "_暂无根因假设_" in stdout
    assert "status=empty_response" in stdout
    assert "degraded run" in stderr


def test_intent_degraded_no_planner_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
) -> None:
    """reconcile degraded_no_planner -> exit 2, Planner findings retained.

    Spec §场景:reconcile 产生的 degraded_no_planner 退出 2 — Planner ok, then the
    Diagnostician hits a persistent BackendUnavailable before any tool call so its
    loop finalizes failed_api_unavailable, which reconcile maps to
    degraded_no_planner (Planner findings are never discarded).
    """

    backend = _PlannerOkThenDiagUnavailable()
    _patch_backend(monkeypatch, cast(LLMBackend, backend))

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 2
    # Planner's findings survive (hello.echo info finding).
    assert "## Findings" in stdout
    assert "hello received" in stdout
    assert "_暂无根因假设_" in stdout
    assert "status=degraded_no_planner" in stdout
    assert "degraded run" in stderr


class _PlannerOkThenDiagUnavailable:
    """Serve the Planner happy path, then raise BackendUnavailable every call.

    The Planner's two calls (run_inspector + end_turn) succeed; once the
    Diagnostician starts, every call raises BackendUnavailable so its loop
    exhausts retries and finalizes failed_api_unavailable before any tool call.
    """

    name = "planner-ok-then-diag-down"

    def __init__(self) -> None:
        self.capabilities = _DEFAULT_CAPS
        self._planner = FakeBackend(responses=[_planner_run_inspector(), _end_turn("巡检完成。")])
        self._planner_calls = 0

    async def messages_create(self, **kwargs: Any) -> MessageResponse:
        if self._planner_calls < 2:
            self._planner_calls += 1
            return await self._planner.messages_create(**kwargs)
        from hostlens.core.exceptions import BackendUnavailable

        raise BackendUnavailable("down", backend_name=self.name)


# ``--persist`` with ``--intent`` is now SUPPORTED (the Agent path assembles a
# faithful Report): the round-trip is covered by
# ``test_inspect_intent_report.py::test_intent_persist_round_trips_reports_show``
# and the end-to-end persist + diff suite in ``test_inspect_intent_persist.py``.
# The old usage-error rejection no longer exists.


# 5.4 — JSON schema stability migrated to the Report contract in
# ``test_inspect_intent_report.py::test_intent_json_round_trips_as_report``
# (the --intent json surface is now a ``Report``, not a ``DiagnosticianResult``;
# the canonical top-level findings + hypotheses⊆findings-id invariant is asserted
# there). Not re-covered here.


# --------------------------------------------------------------------------- #
# 6.1 / 6.2 — request_more_inspection: the Diagnostician supplements evidence
# mid-loop, the FindingStore snapshot grows, and a hypothesis cites the NEW
# finding. Driven by an authored FakeBackend (the proposal Demo Path's
# zero-key, deterministic, reproducible mechanism), then re-validated as a
# record→replay round-trip (RecordingBackend wrapping the same FakeBackend →
# PlaybackBackend) to exercise the cassette_key normalization surface for the
# two-loop diagnosis request shape (the committed 8 incident cassettes are
# Planner-only, so this round-trip is genuinely incremental).
# --------------------------------------------------------------------------- #


@pytest.fixture
def supplement_inspector(user_inspectors_dir: Any) -> str:
    """Drop a clock-free user inspector emitting a DISTINCT deterministic message.

    ``request_more_inspection`` re-runs a real inspector through the real
    ``InspectorRunner``; ``hello.echo`` would re-emit the same message → the
    same ``compute_finding_id`` → the same real id as the Planner's F1, making
    "cites the NEW finding" visually indistinguishable from "cites F1". This
    inspector echoes a unique literal, so the supplemented finding gets its own
    distinct real id — proving the snapshot grew AND the citation is the new id.
    Emits a ``critical`` finding so the supplement also exercises the
    ``_compute_diag_exit_code`` critical detection on a mid-loop addition.

    Clock-free (no ``collect.sampling_window``) per task 6.1: the ``--intent``
    path passes ``clock=None`` → real UTC, so a sampling-window command would
    drift the rendered command / message / id and flake the round-trip.
    """

    name = "diag.supplement"
    (user_inspectors_dir / "supplement.yaml").write_text(
        yaml.safe_dump(
            {
                "name": name,
                "version": "1.0.0",
                "description": "Echo a distinct literal to supplement diagnosis evidence.",
                "tags": ["diag-test"],
                "targets": ["local"],
                "requires_capabilities": [],
                "requires_binaries": ["echo"],
                "privilege": "none",
                "collect": {"command": "echo supplemental-evidence", "timeout_seconds": 5},
                "parse": {"format": "raw"},
                "output_schema": {
                    "type": "object",
                    "properties": {"raw": {"type": "string"}},
                    "required": ["raw"],
                    "additionalProperties": False,
                },
                "findings": [
                    {
                        "when": "len(raw) > 0",
                        "severity": "critical",
                        "message": "supplemental signal: {raw}",
                    }
                ],
            },
            sort_keys=False,
        )
    )
    return name


def _request_more(*, inspector_name: str, block_id: str = "tu_req") -> MessageResponse:
    """A Diagnostician ``request_more_inspection`` tool_use turn (no target_name)."""

    return _msg(
        content=[
            ToolUseBlock(
                type="tool_use",
                id=block_id,
                name="request_more_inspection",
                input={"inspector_name": inspector_name},
            )
        ],
        stop_reason="tool_use",
    )


def _supplement_diag_script(inspector_name: str) -> list[MessageResponse]:
    """Authored 6-turn script across both agents.

    Planner: run_inspector (hello.echo → F1) → narrate. Diagnostician:
    request_more_inspection (``inspector_name`` → a NEW finding labeled F2) →
    [next turn] correlate_findings citing F2 (NOT F1) → narrate. The split
    across turns honors the prompt discipline (never cite a request_more
    result in the SAME turn) and lets the FindingStore assign F2 before the
    citation, so the hit-check passes and harvest resolves F2's real id.
    """

    return [
        _planner_run_inspector(),
        _end_turn("巡检完成。"),
        _request_more(inspector_name=inspector_name),
        _correlate(
            description="补查证据指向资源异常",
            supporting_findings=["F2"],
            suggested_actions=["进一步排查"],
        ),
        _end_turn("诊断完成：补查确认了根因。"),  # noqa: RUF001
    ]


def test_intent_request_more_inspection_grows_store_and_is_cited(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    supplement_inspector: str,
    agent_backend_env: None,
) -> None:
    """6.2: a hypothesis cites a request_more_inspection NEW finding (Report snapshot).

    Spec §场景:诊断师补查证据后引用新 finding. The Diagnostician supplements via
    ``request_more_inspection`` (a distinct inspector → a NEW canonical finding),
    then correlates citing that new finding's label (F2). Migrated to the Report
    contract: the --intent json surface is now an assembled ``Report``; the
    supplemented finding must appear in ``Report.findings`` (proving the per-run
    collector snapshot incorporated the supplement, not just the Planner's F1) and
    the single hypothesis must cite that NEW finding's real id.
    """
    from hostlens.reporting.models import Report

    _patch_backend(monkeypatch, _fake(_supplement_diag_script(supplement_inspector)))

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康", "--format", "json"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 1, stderr  # supplemented critical finding → exit 1

    report = Report.model_validate_json(stdout)

    # The snapshot grew: Planner's hello.echo finding PLUS the supplemented one.
    messages = [f.message for f in report.findings]
    assert any("hello received" in m for m in messages)
    assert any("supplemental signal" in m for m in messages)
    assert len(report.findings) == 2

    # The supplemented finding has its OWN distinct real id (different message →
    # different compute_finding_id), and the single hypothesis cites THAT id.
    supplemented = next(f for f in report.findings if "supplemental signal" in f.message)
    planner_finding = next(f for f in report.findings if "hello received" in f.message)
    assert supplemented.id is not None
    assert planner_finding.id is not None
    assert supplemented.id != planner_finding.id

    assert len(report.hypotheses) == 1
    cited = report.hypotheses[0].supporting_findings
    assert cited == [supplemented.id]  # cites the NEW finding, not the Planner's F1


def test_intent_request_more_inspection_md_evidence_link(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    supplement_inspector: str,
    agent_backend_env: None,
) -> None:
    """6.2 (md surface): the rendered root-cause section links the new finding's id.

    Spec §场景:md 模式输出综述、findings 摘要与根因假设 — the ``## 根因假设``
    section's ``Supporting findings`` line carries the supplemented finding's real
    id, and that id is the one rendered for the new finding in ``## Findings``.
    """

    _patch_backend(monkeypatch, _fake(_supplement_diag_script(supplement_inspector)))

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 1, stderr
    assert "## 根因假设" in stdout
    assert "### 补查证据指向资源异常" in stdout
    assert "supplemental signal" in stdout  # the new finding rendered in ## Findings
    assert "**Supporting findings:**" in stdout
    # The progress for the supplement landed on stderr only.
    assert "request_more_inspection" in stderr
    assert "request_more_inspection" not in stdout


def test_intent_request_more_inspection_record_replay_round_trip(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    supplement_inspector: str,
    agent_backend_env: None,
    tmp_path: Any,
) -> None:
    """6.1: record the diagnosis path with RecordingBackend, then replay it.

    Along the ``tests/incidents/_generate.py`` mechanism: a ``RecordingBackend``
    wraps the authored ``FakeBackend`` (zero Anthropic key, deterministic), records
    the two-loop ``(request, response)`` pairs, persists a cassette, then a fresh
    CLI run over a ``PlaybackBackend`` replays it. The replay producing the IDENTICAL
    stdout (down to the supplemented finding + cited id) proves cassette_key
    normalization hits for the diagnosis request shape and the round-trip is stable.
    """

    from support.cassette_recording import RecordingBackend

    from hostlens.agent.backends.playback import PlaybackBackend

    cassette = tmp_path / "diag_supplement.jsonl"

    # --- record pass: drive the full CLI through a RecordingBackend ---------- #
    recorder = RecordingBackend(
        cassette_path=cassette,
        inner=cast(Any, FakeBackend(responses=_supplement_diag_script(supplement_inspector))),
    )
    monkeypatch.setattr("hostlens.cli._intent.create_backend", lambda _settings: recorder)
    code_rec, stdout_rec, stderr_rec = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"],
        capsys,
        monkeypatch,
    )
    recorder.flush(persist=True)
    assert code_rec == 1, stderr_rec
    assert cassette.exists()
    assert cassette.read_text(encoding="utf-8")  # non-empty: records were persisted

    # --- replay pass: a fresh CLI run over the recorded cassette ------------ #
    monkeypatch.setattr(
        "hostlens.cli._intent.create_backend",
        lambda _settings: PlaybackBackend(cassette_path=cassette),
    )
    code_rep, stdout_rep, stderr_rep = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"],
        capsys,
        monkeypatch,
    )
    assert code_rep == 1, stderr_rep
    # cassette_key normalization hit on every turn → byte-identical stdout.
    assert "CassetteMiss" not in stderr_rep
    assert stdout_rep == stdout_rec
    # The supplemented evidence survives the round-trip.
    assert "supplemental signal" in stdout_rep
    assert "### 补查证据指向资源异常" in stdout_rep


# --------------------------------------------------------------------------- #
# 6.3 — Anthropic degraded acceptance. Parts (b) degraded_no_planner and
# (c) Planner failed_api_unavailable no-result are already covered above
# (test_intent_degraded_no_planner_exit_2 /
# test_intent_no_result_path_planner_api_unavailable_exit_2). Part (a) —
# Planner ok + Diagnostician rate-limit exhausted → degraded_rate_limited —
# is the one remaining degraded path, added here.
# --------------------------------------------------------------------------- #


class _PlannerOkThenDiagRateLimited:
    """Serve the Planner happy path, then raise BackendRateLimited every call.

    The Planner's two calls succeed; once the Diagnostician starts, every call
    raises ``BackendRateLimited`` so its loop exhausts the retry budget and
    finalizes ``degraded_rate_limited`` (reconcile maps it same-name). The
    autouse ``_no_sleep`` fixture no-ops the retry backoff.
    """

    name = "planner-ok-then-diag-rate-limited"

    def __init__(self) -> None:
        self.capabilities = _DEFAULT_CAPS
        self._planner = FakeBackend(responses=[_planner_run_inspector(), _end_turn("巡检完成。")])
        self._planner_calls = 0
        self.diag_calls = 0

    async def messages_create(self, **kwargs: Any) -> MessageResponse:
        if self._planner_calls < 2:
            self._planner_calls += 1
            return await self._planner.messages_create(**kwargs)
        from hostlens.core.exceptions import BackendRateLimited

        self.diag_calls += 1
        raise BackendRateLimited(backend_name=self.name, retry_after_seconds=None)


def test_intent_diag_rate_limited_exit_2_findings_empty_hypotheses(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
) -> None:
    """6.3(a): Planner ok + Diagnostician rate-limited -> degraded_rate_limited, exit 2.

    Spec §场景:诊断师 rate limit 降级. The Planner's findings are output, the
    hypotheses section shows the ``_暂无根因假设_`` placeholder, the (possibly
    empty) narrative renders without an empty heading, and the CLI does NOT retry
    on top of the loop (the loop owns retry: initial + 3 = 4 diagnostician calls).
    """

    backend = _PlannerOkThenDiagRateLimited()
    _patch_backend(monkeypatch, cast(LLMBackend, backend))

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 2
    # Planner findings survive (hello.echo info finding).
    assert "## Findings" in stdout
    assert "hello received" in stdout
    # Degraded narrative is empty → no empty heading: first content line is ## Findings.
    first_line = stdout.lstrip("\n").splitlines()[0]
    assert first_line == "## Findings"
    # Empty-hypotheses placeholder + degraded status on stdout.
    assert "_暂无根因假设_" in stdout
    assert "status=degraded_rate_limited" in stdout
    assert "degraded run" in stderr
    assert "Traceback" not in stderr
    # The loop owns retry (initial + 3 = 4); the CLI must not multiply it.
    assert backend.diag_calls == 4


# --------------------------------------------------------------------------- #
# 6.4 — secret redaction: the Diagnostician adds no new leak path. Confirm a
# real env var VALUE / token / webhook URL present in the process environment
# never surfaces on stdout (hypotheses text + DiagnosticianResult json) or
# stderr (progress) of a normal diagnosis run.
# --------------------------------------------------------------------------- #


def test_intent_diagnosis_does_not_leak_env_secrets(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
) -> None:
    """6.4: env var values / tokens / webhook URLs never leak on either stream.

    Scope (honest): this proves the Diagnostician introduces **no new leak
    surface** — it consumes already-redacted findings and adds no env→output
    path of its own. It does NOT exercise the redaction pipeline's own efficacy
    (the authored narrative / findings never contain these secret values to begin
    with, so the assertion is "the Diagnostician path doesn't manufacture a
    leak", aligned with tasks 6.4 "确认无新泄露路径", NOT "redaction works").

    Spec proposal §Security & Secrets: the Diagnostician consumes already-redacted
    findings and adds no new leak path. We seed the process environment with a
    fake API key, a bearer token, and a webhook URL, run a full diagnosis
    (md + json), and assert none of those raw values appears on stdout (rendered
    hypotheses description / suggested_actions + the DiagnosticianResult json) or
    stderr (the RichLiveObserver progress trees).
    """

    secret_key = "sk-ant-SECRET-deadbeefcafef00d1234567890"
    secret_token = "Bearer ghp_SUPERSECRETtoken0123456789abcdef"
    secret_webhook = "https://hooks.example.invalid/T0000/B1111/SECRETPATHxyz"
    monkeypatch.setenv("HOSTLENS_BACKEND__API_KEY", secret_key)
    monkeypatch.setenv("HOSTLENS_TEST_FAKE_TOKEN", secret_token)
    monkeypatch.setenv("HOSTLENS_TEST_FAKE_WEBHOOK", secret_webhook)

    # md run.
    _patch_backend(monkeypatch, _fake(_happy_script()))
    code_md, stdout_md, stderr_md = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"],
        capsys,
        monkeypatch,
    )
    assert code_md == 0, stderr_md

    # json run (DiagnosticianResult serialization surface).
    _patch_backend(monkeypatch, _fake(_happy_script()))
    code_json, stdout_json, stderr_json = _run_main(
        ["inspect", "local-host", "--intent", "检查健康", "--format", "json"],
        capsys,
        monkeypatch,
    )
    assert code_json == 0, stderr_json

    # NB: ``secret_webhook`` is NOT in ``redact_text``'s pattern set (only ``sk-``
    # / bearer / JWT / keyword-assignment forms are). Its assertion passing proves
    # "the webhook value never entered the output path" — NOT "it was masked". The
    # actual redaction efficacy is proven by the two ``sk-...`` tests below.
    for secret in (secret_key, secret_token, secret_webhook):
        assert secret not in stdout_md
        assert secret not in stderr_md
        assert secret not in stdout_json
        assert secret not in stderr_json


# --------------------------------------------------------------------------- #
# Redaction efficacy: a finding whose evidence/message ACTUALLY carries a
# maskable secret pattern must be scrubbed on the --intent render boundary
# (md + json), matching the Report render path. Unlike the env-leak test above
# (which only proves "no new leak path"), these seed a real secret into the
# finding so the redaction pipeline itself is exercised.
# --------------------------------------------------------------------------- #


# A token redact_text masks (long enough that `_mask` keeps a prefix/suffix
# rather than fully masking — makes the "masked placeholder present" assertion
# concrete). `sk-...` and the `password=` assignment form both fire.
_SECRET_SK = "sk-deadbeefcafef00d1234567890ABCDEF"
_SECRET_MASKED_SK = "sk-d...CDEF"


def _register_secret_bearing_inspector(secret_in_message: bool) -> Any:
    """Build a ``register_default_tools`` stub registering a ``run_inspector``
    whose handler returns a Finding carrying ``_SECRET_SK``.

    The stub mirrors the new ``register_default_tools(registry, *, clock=None,
    collector=None)`` signature and feeds the per-run collector a matching
    ``InspectorResult`` (the handler stub bypasses the real runner, so the
    assembled Report only carries the secret-bearing finding if we append it to
    the collector the orchestration snapshots).

    When ``secret_in_message`` the secret is in the Finding.message (surfaces in
    the top-level ## Findings / Report.findings json). The secret is ALWAYS placed
    in the Evidence.stdout, which (since the collector appends the full finding
    with its evidence) lands in ``Report.findings[*].evidence[*].stdout`` — the
    surface the recursive redact walker must reach even when the message is benign.
    """

    from typing import cast as _cast

    from hostlens.inspectors.result import InspectorResult
    from hostlens.reporting.models import Evidence, Finding
    from hostlens.tools.base import ToolSpec
    from hostlens.tools.registry import ToolRegistry
    from hostlens.tools.schemas.run_inspector import RunInspectorInput, RunInspectorOutput

    message = f"leaked {_SECRET_SK}" if secret_in_message else "benign finding"

    def _secret_finding() -> Finding:
        return Finding(
            severity="info",
            message=message,
            evidence=[
                Evidence(
                    kind="command_output",
                    command="cat /tmp/creds",
                    stdout=f"token output: {_SECRET_SK}",
                    exit_code=0,
                )
            ],
        )

    def _register(registry: ToolRegistry, *, clock: Any = None, collector: Any = None) -> None:
        async def _handler(args: RunInspectorInput, ctx: Any) -> RunInspectorOutput:
            finding = _secret_finding()
            if collector is not None:
                collector.append(
                    InspectorResult(
                        name="hello.echo",
                        version="1.0.0",
                        status="ok",
                        target_name="local-host",
                        duration_seconds=0.1,
                        findings=[finding],
                    )
                )
            return RunInspectorOutput(
                target_name="local-host",
                inspector_name="hello.echo",
                findings=[finding],
            )

        registry.register(
            ToolSpec(
                name="run_inspector",
                version="1.0.0",
                input_schema=RunInspectorInput,
                output_schema=RunInspectorOutput,
                handler=_cast(Any, _handler),
                agent_description="stub secret-bearing run inspector",
                mcp_description="stub",
                cli_help=None,
                surfaces=_cast(Any, {"agent"}),
                side_effects=_cast(Any, "read"),
                requires_approval=False,
                sensitive_output=True,
                timeout=30.0,
            )
        )

    return _register


def test_intent_redacts_finding_secret_in_md_and_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
) -> None:
    """A secret in a finding's message/evidence is masked on both md and json stdout.

    The ``--intent`` render path must scrub any ``redact_text``-covered pattern
    (parity with the Report render path), so the raw ``sk-...`` never appears and
    the masked placeholder does.
    """

    import hostlens.orchestration.pipeline as intent_mod

    monkeypatch.setattr(
        intent_mod, "register_default_tools", _register_secret_bearing_inspector(True)
    )

    # md surface.
    _patch_backend(monkeypatch, _fake(_happy_script()))
    code_md, stdout_md, stderr_md = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"],
        capsys,
        monkeypatch,
    )
    assert code_md == 0, stderr_md
    assert _SECRET_SK not in stdout_md
    assert _SECRET_MASKED_SK in stdout_md

    # json surface.
    monkeypatch.setattr(
        intent_mod, "register_default_tools", _register_secret_bearing_inspector(True)
    )
    _patch_backend(monkeypatch, _fake(_happy_script()))
    code_json, stdout_json, stderr_json = _run_main(
        ["inspect", "local-host", "--intent", "检查健康", "--format", "json"],
        capsys,
        monkeypatch,
    )
    assert code_json == 0, stderr_json
    assert _SECRET_SK not in stdout_json
    assert _SECRET_MASKED_SK in stdout_json


def test_intent_redacts_diagnosis_narrative_secret_in_report_md_and_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
) -> None:
    """A secret in the Diagnostician's narrative is masked in the rendered Report.

    Task 4.2 redaction regression: the diagnosis narrative is projected into
    ``Report.metadata[diagnosis_narrative]``. Both the intent-style md renderer
    (which reads that key back) and the json surface go through
    ``redact_report_for_render`` (which masks ``metadata`` values), so a secret
    the model restated in its narrative must not surface raw on stdout — the
    masked placeholder must appear instead.
    """

    leaky_narrative = f"诊断完成：发现凭据 {_SECRET_SK} 泄露。"  # noqa: RUF001

    # md surface (narrative rendered from metadata[diagnosis_narrative]).
    _patch_backend(monkeypatch, _fake(_happy_script(diag_narrative=leaky_narrative)))
    code_md, stdout_md, stderr_md = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"],
        capsys,
        monkeypatch,
    )
    assert code_md == 0, stderr_md
    assert _SECRET_SK not in stdout_md
    assert _SECRET_MASKED_SK in stdout_md

    # json surface (metadata serialized through the same redaction boundary).
    _patch_backend(monkeypatch, _fake(_happy_script(diag_narrative=leaky_narrative)))
    code_json, stdout_json, stderr_json = _run_main(
        ["inspect", "local-host", "--intent", "检查健康", "--format", "json"],
        capsys,
        monkeypatch,
    )
    assert code_json == 0, stderr_json
    assert _SECRET_SK not in stdout_json
    assert _SECRET_MASKED_SK in stdout_json


def test_intent_redacts_model_narrative_secret_on_stderr_progress(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
) -> None:
    """A secret in the model's narrative text is masked on the stderr progress preview.

    ``RichLiveObserver`` echoes ``ModelResponded.text`` (the model narrative) onto
    stderr as a one-line progress preview. If the model restates a secret-bearing
    finding in its narrative, that raw value must not bypass the render-boundary
    redaction and surface on stderr. Both streams must stay clean.
    """

    leaky_narrative = f"诊断完成：发现凭据 {_SECRET_SK} 泄露。"  # noqa: RUF001
    _patch_backend(monkeypatch, _fake(_happy_script(diag_narrative=leaky_narrative)))

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 0, stderr
    assert _SECRET_SK not in stderr
    assert _SECRET_SK not in stdout
    # The masked placeholder proves the preview still showed the (scrubbed) narrative.
    assert _SECRET_MASKED_SK in stderr


def test_intent_redacts_hallucinated_tool_name_secret_on_stderr_progress(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
) -> None:
    """A secret-shaped hallucinated tool name is masked on the stderr progress label.

    The loop emits ``ToolStarted`` (and the UnknownTool ``ToolCompleted``) *before*
    the white-list check, so a model-hallucinated ``tool_use`` name (model-controlled
    free text) reaches ``RichLiveObserver``'s stderr progress label. If that name
    carries a maskable secret pattern, it must be scrubbed there too — defense in
    depth parity with the narrative-text redaction above.

    Coverage note: on the UnknownTool path ``ToolCompleted`` overwrites the same
    Rich node in place and ``Live`` only renders the final tree, so the masked
    placeholder captured here comes from the ``ToolCompleted`` label
    (``redact_text(invocation.tool_name)``). The sibling ``ToolStarted`` redaction
    (``redact_text(tool_name)``) is harmless defense-in-depth reachable only on a
    fail-loud ToolStarted-without-ToolCompleted path, which fires only for
    white-listed (non-secret) tool names — so it is not separately asserted here.
    """

    # The hallucinated tool name IS the secret (a ``sk-...`` string redact_text
    # masks). The name is not in the white-list, so the loop takes the UnknownTool
    # path after emitting ToolStarted/ToolCompleted with this raw name. Because the
    # model never calls a real run_inspector, the per-run collector stays empty →
    # no Report → the no-result path (exit 2, empty stdout). The redaction surface
    # under test is the stderr progress label, which is unaffected by no-result.
    script = [
        _msg(
            content=[
                ToolUseBlock(
                    type="tool_use",
                    id="tu_halluc",
                    name=_SECRET_SK,
                    input={},
                )
            ],
            stop_reason="tool_use",
        ),
        _end_turn("巡检完成。"),
        _end_turn("诊断完成：未见严重问题。"),  # noqa: RUF001
    ]
    _patch_backend(monkeypatch, _fake(script))

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 2  # no-result: hallucinated tool ran no inspector
    assert stdout == ""
    assert _SECRET_SK not in stderr
    assert _SECRET_SK not in stdout
    # The masked placeholder proves the progress label still showed the (scrubbed) name.
    assert _SECRET_MASKED_SK in stderr


def test_intent_redacts_loop_telemetry_secret_in_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
) -> None:
    """A secret living ONLY in a finding's evidence (benign message) is masked too.

    Migrated to the Report contract: the loop telemetry top-level keys
    (``planner_result`` / ``diagnostician_loop``) are gone — the --intent json
    surface is now a ``Report``. The raw ``run_inspector`` Evidence.stdout secret
    lands in ``Report.findings[*].evidence[*].stdout`` (a nested surface, with a
    benign finding message), so the recursive ``redact_report_for_render`` walker
    must still reach it — ``--format json`` must not leak the raw secret even when
    the finding message itself is benign.
    """

    import hostlens.orchestration.pipeline as intent_mod
    from hostlens.reporting.models import Report

    monkeypatch.setattr(
        intent_mod, "register_default_tools", _register_secret_bearing_inspector(False)
    )
    _patch_backend(monkeypatch, _fake(_happy_script()))

    code_json, stdout_json, stderr_json = _run_main(
        ["inspect", "local-host", "--intent", "检查健康", "--format", "json"],
        capsys,
        monkeypatch,
    )
    assert code_json == 0, stderr_json

    # The secret lives only in the nested evidence (the message is benign); the
    # masked placeholder proves the recursive walker reached evidence.stdout.
    report = Report.model_validate_json(stdout_json)
    assert report.findings, "expected the run_inspector finding in the Report"
    assert all("leaked" not in f.message for f in report.findings)  # benign message

    assert _SECRET_SK not in stdout_json
    assert _SECRET_MASKED_SK in stdout_json


def test_intent_redaction_preserves_round_trip(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
) -> None:
    """The redacted ``--format json`` output is still a valid Report.

    Migrated to the Report contract: the ``model_dump`` → recursive-redact →
    ``model_validate`` round-trip must not break the schema (datetime / enum / int
    scalars survive), so downstream can still ``Report.model_validate_json`` the
    masked output.
    """

    import hostlens.orchestration.pipeline as intent_mod
    from hostlens.reporting.models import Report

    monkeypatch.setattr(
        intent_mod, "register_default_tools", _register_secret_bearing_inspector(True)
    )
    _patch_backend(monkeypatch, _fake(_happy_script()))

    code_json, stdout_json, stderr_json = _run_main(
        ["inspect", "local-host", "--intent", "检查健康", "--format", "json"],
        capsys,
        monkeypatch,
    )
    assert code_json == 0, stderr_json

    report = Report.model_validate_json(stdout_json)
    assert report.findings
    assert all(f.id is not None for f in report.findings)


# --------------------------------------------------------------------------- #
# ConfigError routing: a non-backend ConfigError (the lazy DiagnosticianAgent
# prompt loader's kind="diagnostician_prompt_missing") must NOT be reported as a
# "backend not configured ... run doctor" error — it gets the generic
# configuration-error message (both still exit 3).
# --------------------------------------------------------------------------- #


def test_intent_prompt_missing_config_error_generic_message_exit_3(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
) -> None:
    """A diagnostician_prompt_missing ConfigError -> generic message, exit 3.

    The backend IS configured (create_backend succeeds), so the misleading
    "backend not configured / run doctor" hint must not appear; the operator
    sees a generic "configuration error" line instead.
    """

    import hostlens.orchestration.pipeline as intent_mod
    from hostlens.core.exceptions import ConfigError

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise ConfigError(
            "diagnostician prompt template not found",
            kind="diagnostician_prompt_missing",
        )

    monkeypatch.setattr(intent_mod, "DiagnosticianAgent", _boom)
    _patch_backend(monkeypatch, _fake(_happy_script()))

    exit_code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"],
        capsys,
        monkeypatch,
    )
    assert exit_code == 3
    assert stdout == ""
    assert "configuration error" in stderr
    assert "backend not configured" not in stderr
    assert "Traceback" not in stderr
