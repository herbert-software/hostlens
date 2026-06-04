"""CLI tests for the ``hostlens inspect --intent`` Report path (group BC).

Spec: ``openspec/changes/add-intent-report-persistence/specs/`` (agent-report-assembly /
inspect-cli-command / report-persistence).

The ``--intent`` path now produces a first-class ``Report`` (assembled from the
per-run ``InspectorResultCollector`` snapshot after the diagnosis loop, with the
Diagnostician's hypotheses / narrative projected in). These tests drive the full
CLI path with a scripted ``FakeBackend`` (one shared instance across both agents,
per the "create_backend only once" contract) and cover:

- snapshot timing: a ``request_more_inspection`` supplement lands in
  ``Report.inspector_results`` / ``Report.findings`` (task 2.1)
- the no-result path: collector empty (Planner failed, or Planner-ok-but-the-
  model-never-called-run_inspector) → exit 2, empty stdout, no persist (task 2.4)
- md / json rendering of the assembled Report (tasks 3.1 / 3.4)
- Report-based exit codes incl. ``partial`` (task 3.2)
- ``--intent --persist`` round-trips through ``reports show`` (task 3.3)

``_run_main`` drives ``hostlens.cli.main`` (so the click-UsageError wrapper runs)
and captures the ``SystemExit`` code, mirroring ``test_inspect_intent.py``.
``asyncio_mode = "auto"`` (pyproject) — no marker; every backend is fake.
"""

from __future__ import annotations

import json
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
from hostlens.reporting.models import Report

# --------------------------------------------------------------------------- #
# Fixtures (mirror test_inspect_intent_diagnostician.py)
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

_RUN_INSPECTOR_INPUT = {"target_name": "local-host", "inspector_name": "hello.echo"}


@pytest.fixture
def targets_yaml(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
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
    user_path = tmp_path / "inspectors"
    user_path.mkdir()
    monkeypatch.setenv("HOSTLENS_INSPECTORS_SEARCH_PATHS", str(user_path))
    return user_path


@pytest.fixture
def agent_backend_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOSTLENS_BACKEND__TYPE", "anthropic_api")
    monkeypatch.setenv("HOSTLENS_BACKEND__API_KEY", "sk-ant-test-not-real")
    monkeypatch.setenv("HOSTLENS_AGENT__PRIMARY_MODEL", "claude-test")


@pytest.fixture
def xdg_home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Redirect the default ReportStore at a tmp ``$XDG_DATA_HOME`` so --persist
    and ``reports show`` share the same db."""

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _instant(_delay: float) -> None:
        return None

    monkeypatch.setattr("hostlens.agent.loop.asyncio.sleep", _instant)


def _run_main(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[int, str, str]:
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
    monkeypatch.setattr("hostlens.cli._intent.create_backend", lambda _settings: backend)


def _fake(responses: list[MessageResponse]) -> LLMBackend:
    return cast(LLMBackend, FakeBackend(responses=responses))


# --------------------------------------------------------------------------- #
# MessageResponse builders
# --------------------------------------------------------------------------- #


def _msg(*, content: list[Any], stop_reason: str) -> MessageResponse:
    return MessageResponse(
        id="msg_x",
        model="claude-test",
        role="assistant",
        content=content,
        stop_reason=cast(Any, stop_reason),
        usage=Usage(input_tokens=3, output_tokens=2),
    )


def _end_turn(text: str) -> MessageResponse:
    return _msg(content=[TextBlock(type="text", text=text)], stop_reason="end_turn")


def _planner_run_inspector(*, block_id: str = "tu_plan") -> MessageResponse:
    return _msg(
        content=[
            ToolUseBlock(
                type="tool_use", id=block_id, name="run_inspector", input=_RUN_INSPECTOR_INPUT
            )
        ],
        stop_reason="tool_use",
    )


def _correlate(
    *,
    description: str = "可能是配置漂移",
    supporting_findings: list[str] | None = None,
    suggested_actions: list[str] | None = None,
) -> MessageResponse:
    return _msg(
        content=[
            ToolUseBlock(
                type="tool_use",
                id="tu_corr",
                name="correlate_findings",
                input={
                    "description": description,
                    "confidence": "medium",
                    "supporting_findings": supporting_findings or ["F1"],
                    "suggested_actions": suggested_actions or ["复查配置"],
                },
            )
        ],
        stop_reason="tool_use",
    )


def _request_more(*, inspector_name: str) -> MessageResponse:
    return _msg(
        content=[
            ToolUseBlock(
                type="tool_use",
                id="tu_req",
                name="request_more_inspection",
                input={"inspector_name": inspector_name},
            )
        ],
        stop_reason="tool_use",
    )


def _happy_script(*, with_hypothesis: bool = True) -> list[MessageResponse]:
    script: list[MessageResponse] = [_planner_run_inspector(), _end_turn("巡检完成。")]
    if with_hypothesis:
        script.append(_correlate())
    script.append(_end_turn("诊断完成：未见严重问题。"))  # noqa: RUF001
    return script


@pytest.fixture
def supplement_inspector(user_inspectors_dir: Any) -> str:
    """A clock-free user inspector emitting a DISTINCT deterministic message."""

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
                        "severity": "warning",
                        "message": "supplemental signal: {raw}",
                    }
                ],
            },
            sort_keys=False,
        )
    )
    return name


# --------------------------------------------------------------------------- #
# 2.1 — snapshot timing: supplement lands in Report.inspector_results/findings
# --------------------------------------------------------------------------- #


def test_intent_supplement_lands_in_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    supplement_inspector: str,
    agent_backend_env: None,
) -> None:
    """A request_more_inspection supplement (post-Planner) appears in the assembled
    Report — proving snapshot happens AFTER the diagnosis loop (task 2.1)."""

    script = [
        _planner_run_inspector(),
        _end_turn("巡检完成。"),
        _request_more(inspector_name=supplement_inspector),
        _correlate(description="补查证据指向配置", supporting_findings=["F2"]),
        _end_turn("诊断完成：补查确认。"),  # noqa: RUF001
    ]
    _patch_backend(monkeypatch, _fake(script))

    code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康", "--format", "json"],
        capsys,
        monkeypatch,
    )
    assert code == 0, stderr

    report = Report.model_validate_json(stdout)
    # Both the Planner's inspector AND the supplemented one are in the snapshot.
    inspector_names = {ir.name for ir in report.inspector_results}
    assert "hello.echo" in inspector_names
    assert supplement_inspector in inspector_names
    # The supplemented finding made it into the flattened Report.findings.
    messages = [f.message for f in report.findings]
    assert any("supplemental signal" in m for m in messages)


# --------------------------------------------------------------------------- #
# 2.4 — no-result paths (collector empty)
# --------------------------------------------------------------------------- #


def test_intent_no_result_planner_unavailable_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
) -> None:
    """Planner failed_api_unavailable → empty collector → no Report → exit 2, empty stdout."""

    backend = _PersistentUnavailableBackend()
    _patch_backend(monkeypatch, cast(LLMBackend, backend))

    code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"], capsys, monkeypatch
    )
    assert code == 2
    assert stdout == ""
    # The degrade message uses generic "no inspector results" wording, NOT a
    # hardcoded failed_api_unavailable string. (The loop's terminal_status may
    # appear elsewhere in the RichLiveObserver progress tree on the same captured
    # physical line, so we assert the exact degrade phrase shape directly.)
    assert (
        "hostlens inspect: degraded run (no inspector results collected); "
        "no report produced" in stderr
    )
    assert "Traceback" not in stderr


def test_intent_no_result_planner_ok_but_no_inspector_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
) -> None:
    """Planner finalizes ok but the model NEVER calls run_inspector → empty
    collector → no Report → exit 2, empty stdout (task 2.4 sub-case)."""

    # Planner immediately end_turns with no tool use, so no InspectorResult is
    # ever collected; the Diagnostician then also end_turns without supplementing,
    # so the collector stays empty across BOTH loops (terminal_status=ok both).
    _patch_backend(monkeypatch, _fake([_end_turn("无需巡检。"), _end_turn("无可诊断。")]))

    code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"], capsys, monkeypatch
    )
    assert code == 2
    assert stdout == ""
    assert "no report produced" in stderr
    assert "Traceback" not in stderr


class _PersistentUnavailableBackend:
    name = "persistent-unavailable"

    def __init__(self) -> None:
        self.capabilities = _DEFAULT_CAPS

    async def messages_create(self, **_kwargs: Any) -> MessageResponse:
        from hostlens.core.exceptions import BackendUnavailable

        raise BackendUnavailable("down", backend_name="persistent-unavailable")


# --------------------------------------------------------------------------- #
# 2.4 — all-non-ok inspector → partial Report (still assembled)
# --------------------------------------------------------------------------- #


def test_intent_all_non_ok_inspector_partial(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
) -> None:
    """An inspector that runs requires_unmet (non-ok, but a real InspectorResult)
    → collector non-empty → Report with meta.status=partial → exit 2."""

    # An inspector requiring a binary that does not exist runs requires_unmet.
    name = "diag.needs_missing"
    (user_inspectors_dir / "needs_missing.yaml").write_text(
        yaml.safe_dump(
            {
                "name": name,
                "version": "1.0.0",
                "description": "Requires a binary that is not present.",
                "tags": ["diag-test"],
                "targets": ["local"],
                "requires_capabilities": [],
                "requires_binaries": ["definitely-not-a-real-binary-xyz"],
                "privilege": "none",
                "collect": {"command": "echo nope", "timeout_seconds": 5},
                "parse": {"format": "raw"},
                "output_schema": {
                    "type": "object",
                    "properties": {"raw": {"type": "string"}},
                    "required": ["raw"],
                    "additionalProperties": False,
                },
                "findings": [],
            },
            sort_keys=False,
        )
    )

    script = [
        _msg(
            content=[
                ToolUseBlock(
                    type="tool_use",
                    id="tu_plan",
                    name="run_inspector",
                    input={"target_name": "local-host", "inspector_name": name},
                )
            ],
            stop_reason="tool_use",
        ),
        _end_turn("巡检完成。"),
        _end_turn("诊断完成。"),
    ]
    _patch_backend(monkeypatch, _fake(script))

    code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康", "--format", "json"],
        capsys,
        monkeypatch,
    )
    assert code == 2, stderr
    report = Report.model_validate_json(stdout)
    assert report.meta is not None
    assert report.meta.status == "partial"
    assert "degraded run (status=partial)" in stderr


# --------------------------------------------------------------------------- #
# 3.1 — md rendering (intent-style; no Inspector Results JSON dump)
# --------------------------------------------------------------------------- #


def test_intent_md_narrative_findings_hypotheses_telemetry(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
) -> None:
    """md: narrative + ## Findings + ## 根因假设 + telemetry; NO ## Inspector Results."""

    _patch_backend(monkeypatch, _fake(_happy_script()))
    code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"], capsys, monkeypatch
    )
    assert code == 0, stderr
    assert "诊断完成：未见严重问题。" in stdout  # noqa: RUF001 narrative
    assert "## Findings" in stdout
    assert "## 根因假设" in stdout
    assert "### 可能是配置漂移" in stdout
    assert "**Confidence:** medium" in stdout
    assert "**Supporting findings:**" in stdout
    assert "status=ok" in stdout
    # The intent-style renderer must NOT emit the mechanical Report structure.
    assert "## Inspector Results" not in stdout
    assert "# Hostlens Inspection Report" not in stdout


def test_intent_md_empty_hypotheses_placeholder(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
) -> None:
    _patch_backend(monkeypatch, _fake(_happy_script(with_hypothesis=False)))
    code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"], capsys, monkeypatch
    )
    assert code == 0, stderr
    assert "## 根因假设" in stdout
    assert "_暂无根因假设_" in stdout
    assert "## Findings" in stdout


def test_intent_md_empty_narrative_no_empty_heading(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
) -> None:
    """Degraded → empty narrative → first content line is ## Findings (no blank heading)."""

    script = [
        _planner_run_inspector(),
        _end_turn("巡检完成。"),
        _msg(content=[], stop_reason="max_tokens"),  # degraded, empty narrative
    ]
    _patch_backend(monkeypatch, _fake(script))
    code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"], capsys, monkeypatch
    )
    assert code == 2  # degraded_token_budget
    first_line = stdout.lstrip("\n").splitlines()[0]
    assert first_line == "## Findings"
    assert "_暂无根因假设_" in stdout
    assert "degraded run" in stderr


# --------------------------------------------------------------------------- #
# 3.2 — Report-based exit codes (incl. partial above; critical here)
# --------------------------------------------------------------------------- #


def test_intent_critical_finding_exit_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
) -> None:
    """status=ok + ≥1 critical finding → exit 1 (Report.findings based)."""

    import hostlens.orchestration.pipeline as intent_mod
    from hostlens.reporting.models import Finding
    from hostlens.tools.registry import ToolRegistry
    from hostlens.tools.schemas.run_inspector import RunInspectorInput, RunInspectorOutput

    async def _critical_handler(args: RunInspectorInput, ctx: Any) -> RunInspectorOutput:
        return RunInspectorOutput(
            target_name="local-host",
            inspector_name="hello.echo",
            findings=[Finding(severity="critical", message="disk full")],
        )

    def _register_critical(registry: ToolRegistry, *, clock: Any = None, collector: Any) -> None:
        from hostlens.inspectors.result import InspectorResult
        from hostlens.tools.base import ToolSpec

        # Also feed the collector a matching InspectorResult so the assembled
        # Report carries the critical finding (the handler stub bypasses the
        # real runner, so we append the InspectorResult the orchestration needs).
        async def _wrapped(args: RunInspectorInput, ctx: Any) -> RunInspectorOutput:
            out = await _critical_handler(args, ctx)
            collector.append(
                InspectorResult(
                    name="hello.echo",
                    version="1.0.0",
                    status="ok",
                    target_name="local-host",
                    duration_seconds=0.1,
                    findings=list(out.findings),
                )
            )
            return out

        registry.register(
            ToolSpec(
                name="run_inspector",
                version="1.0.0",
                input_schema=RunInspectorInput,
                output_schema=RunInspectorOutput,
                handler=cast(Any, _wrapped),
                agent_description="stub critical run inspector",
                mcp_description="stub",
                cli_help=None,
                surfaces=cast(Any, {"agent"}),
                side_effects=cast(Any, "read"),
                requires_approval=False,
                sensitive_output=True,
                timeout=30.0,
            )
        )

    monkeypatch.setattr(intent_mod, "register_default_tools", _register_critical)
    _patch_backend(monkeypatch, _fake(_happy_script()))

    code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"], capsys, monkeypatch
    )
    assert code == 1, stderr
    assert "disk full" in stdout


def test_intent_healthy_exit_0(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
) -> None:
    _patch_backend(monkeypatch, _fake(_happy_script()))
    code, _stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康"], capsys, monkeypatch
    )
    assert code == 0, stderr


# --------------------------------------------------------------------------- #
# 3.4 — json round-trips as a Report (BREAKING: was DiagnosticianResult)
# --------------------------------------------------------------------------- #


def test_intent_json_round_trips_as_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
) -> None:
    """--format json outputs a Report (not DiagnosticianResult); round-trips."""

    _patch_backend(monkeypatch, _fake(_happy_script()))
    code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康", "--format", "json"],
        capsys,
        monkeypatch,
    )
    assert code == 0, stderr

    report = Report.model_validate_json(stdout)
    assert report.meta is not None
    # hypotheses projected + narrative in metadata.
    assert len(report.hypotheses) == 1
    assert "diagnosis_narrative" in report.metadata
    assert report.metadata["diagnosis_narrative"]
    # supporting_findings ⊆ Report.findings ids.
    finding_ids = {f.id for f in report.findings}
    for h in report.hypotheses:
        for ref in h.supporting_findings:
            assert ref in finding_ids

    # The old DiagnosticianResult-only top-level keys are gone.
    payload = json.loads(stdout)
    assert "planner_result" not in payload
    assert "diagnostician_loop" not in payload


# --------------------------------------------------------------------------- #
# 3.3 — --intent --persist round-trips through reports show; no-result not stored
# --------------------------------------------------------------------------- #


def test_intent_persist_round_trips_reports_show(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
    xdg_home: Any,
) -> None:
    """--intent --persist saves a Report; reports show retrieves it with hypotheses."""

    _patch_backend(monkeypatch, _fake(_happy_script()))
    code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康", "--persist", "--format", "json"],
        capsys,
        monkeypatch,
    )
    assert code == 0, stderr
    report = Report.model_validate_json(stdout)
    assert report.meta is not None
    run_id = report.meta.run_id

    # reports show retrieves the persisted report (with hypotheses preserved).
    show_code, show_out, show_err = _run_main(
        ["reports", "show", run_id, "--format", "json"], capsys, monkeypatch
    )
    assert show_code == 0, show_err
    shown = Report.model_validate_json(show_out)
    assert shown.meta is not None
    assert shown.meta.run_id == run_id
    assert len(shown.hypotheses) == 1
    assert shown.meta.inspectors_used  # faithful meta


def test_intent_no_result_not_persisted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
    xdg_home: Any,
) -> None:
    """A no-result --intent --persist run stores nothing (reports list stays empty)."""

    backend = _PersistentUnavailableBackend()
    _patch_backend(monkeypatch, cast(LLMBackend, backend))
    code, stdout, _stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康", "--persist"], capsys, monkeypatch
    )
    assert code == 2
    assert stdout == ""

    # reports list shows nothing was persisted.
    list_code, list_out, _ = _run_main(
        ["reports", "list", "local-host", "--json"], capsys, monkeypatch
    )
    assert list_code == 0
    assert json.loads(list_out) == []


# --------------------------------------------------------------------------- #
# Persist-failure / orphan escalation dominates a critical-finding exit 1.
# A user who explicitly asked for --persist must see the persist failure
# (exit 2) even when the report also carries a critical finding (exit 1):
# the function's documented priority is 3 > 2 > 1 > 0.
# --------------------------------------------------------------------------- #


def _register_critical_ok_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch default-tools registration so the run yields status=ok + a critical finding."""

    import hostlens.orchestration.pipeline as intent_mod
    from hostlens.inspectors.result import InspectorResult
    from hostlens.reporting.models import Finding
    from hostlens.tools.base import ToolSpec
    from hostlens.tools.registry import ToolRegistry
    from hostlens.tools.schemas.run_inspector import RunInspectorInput, RunInspectorOutput

    def _register(registry: ToolRegistry, *, clock: Any = None, collector: Any) -> None:
        async def _wrapped(args: RunInspectorInput, ctx: Any) -> RunInspectorOutput:
            findings = [Finding(severity="critical", message="disk full")]
            collector.append(
                InspectorResult(
                    name="hello.echo",
                    version="1.0.0",
                    status="ok",
                    target_name="local-host",
                    duration_seconds=0.1,
                    findings=list(findings),
                )
            )
            return RunInspectorOutput(
                target_name="local-host",
                inspector_name="hello.echo",
                findings=findings,
            )

        registry.register(
            ToolSpec(
                name="run_inspector",
                version="1.0.0",
                input_schema=RunInspectorInput,
                output_schema=RunInspectorOutput,
                handler=cast(Any, _wrapped),
                agent_description="stub critical run inspector",
                mcp_description="stub",
                cli_help=None,
                surfaces=cast(Any, {"agent"}),
                side_effects=cast(Any, "read"),
                requires_approval=False,
                sensitive_output=True,
                timeout=30.0,
            )
        )

    monkeypatch.setattr(intent_mod, "register_default_tools", _register)


def test_intent_persist_orphan_overrides_critical_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
) -> None:
    """status=ok + critical finding + --persist orphan → exit 2 (not 1)."""

    import hostlens.cli.inspect as inspect_mod

    _register_critical_ok_report(monkeypatch)
    _patch_backend(monkeypatch, _fake(_happy_script()))
    # Orphan persist: _persist_report returns True.
    monkeypatch.setattr(inspect_mod, "_persist_report", lambda _report: True)

    code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康", "--persist"], capsys, monkeypatch
    )
    assert code == 2, stderr
    assert "disk full" in stdout


def test_intent_persist_failure_overrides_critical_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
) -> None:
    """status=ok + critical finding + --persist raise → exit 2 (not 1)."""

    import hostlens.cli.inspect as inspect_mod

    def _boom(_report: Report) -> bool:
        raise RuntimeError("store exploded")

    _register_critical_ok_report(monkeypatch)
    _patch_backend(monkeypatch, _fake(_happy_script()))
    monkeypatch.setattr(inspect_mod, "_persist_report", _boom)

    code, _stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康", "--persist"], capsys, monkeypatch
    )
    assert code == 2, stderr
    assert "failed to persist report" in stderr


def test_intent_persist_failure_status_ok_no_misleading_degraded_note(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
) -> None:
    """Pure persist failure on a status=ok report → exit 2 but no "degraded run (status=ok)" line."""

    import hostlens.cli.inspect as inspect_mod

    def _boom(_report: Report) -> bool:
        raise RuntimeError("store exploded")

    _patch_backend(monkeypatch, _fake(_happy_script()))
    monkeypatch.setattr(inspect_mod, "_persist_report", _boom)

    code, _stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康", "--persist"], capsys, monkeypatch
    )
    assert code == 2, stderr
    assert "failed to persist report" in stderr
    assert "degraded run (status=ok)" not in stderr
