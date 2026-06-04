"""End-to-end ``hostlens inspect --intent --persist`` + ``reports`` integration.

Spec: ``openspec/changes/add-intent-report-persistence/specs/`` (report-persistence /
agent-report-assembly / inspect-cli-command).

The ``--intent`` Agent path now assembles a first-class ``Report`` (from the
per-run ``InspectorResultCollector`` snapshot, with the Diagnostician's
hypotheses / narrative projected in) and ``--persist`` saves it to the local
SQLite store. These tests drive the FULL CLI path with a scripted ``FakeBackend``
(one shared instance across both agents, per the "create_backend only once"
contract — no real API) and cover, end to end:

- ``--intent --persist`` saves a FAITHFUL Report (``meta.status`` /
  ``meta.inspectors_used`` / ``meta.token_usage`` + non-empty hypotheses), and
  ``reports show`` retrieves it byte-for-byte through the store (task 4.1).
- Two ``--intent --persist`` runs of the SAME target (both authored to terminate
  ``status=ok`` so ``compute_diff`` does not hit the ``baseline_not_ok`` skip)
  produce a finding-level ``reports diff`` with at least one ``added`` and one
  ``resolved`` (task 4.1).
- A ``request_more_inspection`` supplement whose NEW finding is cited by a
  hypothesis survives the persist → ``reports show`` round-trip (task 4.1).

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
# Fixtures
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
    and the ``reports`` commands share the same db."""

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
        usage=Usage(input_tokens=4, output_tokens=3),
    )


def _end_turn(text: str) -> MessageResponse:
    return _msg(content=[TextBlock(type="text", text=text)], stop_reason="end_turn")


def _run_inspector(inspector_name: str, *, block_id: str = "tu_plan") -> MessageResponse:
    return _msg(
        content=[
            ToolUseBlock(
                type="tool_use",
                id=block_id,
                name="run_inspector",
                input={"target_name": "local-host", "inspector_name": inspector_name},
            )
        ],
        stop_reason="tool_use",
    )


def _correlate(
    *,
    description: str,
    supporting_findings: list[str],
    block_id: str = "tu_corr",
) -> MessageResponse:
    return _msg(
        content=[
            ToolUseBlock(
                type="tool_use",
                id=block_id,
                name="correlate_findings",
                input={
                    "description": description,
                    "confidence": "medium",
                    "supporting_findings": supporting_findings,
                    "suggested_actions": ["复查配置"],
                },
            )
        ],
        stop_reason="tool_use",
    )


def _request_more(*, inspector_name: str, block_id: str = "tu_req") -> MessageResponse:
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


def _make_inspector(user_inspectors_dir: Any, *, name: str, message: str) -> str:
    """Drop a clock-free user inspector emitting one warning finding.

    Clock-free (no ``collect.sampling_window``) — the ``--intent`` path passes
    ``clock=None`` → real UTC, so a sampling-window command would drift the
    rendered command / message / id and flake the assertions.
    """

    (user_inspectors_dir / f"{name.replace('.', '_')}.yaml").write_text(
        yaml.safe_dump(
            {
                "name": name,
                "version": "1.0.0",
                "description": f"Echo a distinct literal ({name}).",
                "tags": ["persist-test"],
                "targets": ["local"],
                "requires_capabilities": [],
                "requires_binaries": ["echo"],
                "privilege": "none",
                "collect": {"command": f"echo {message}", "timeout_seconds": 5},
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
                        "message": "signal: {raw}",
                    }
                ],
            },
            sort_keys=False,
        )
    )
    return name


# --------------------------------------------------------------------------- #
# 4.1 — --intent --persist saves a faithful Report; reports show retrieves it
# --------------------------------------------------------------------------- #


def test_intent_persist_faithful_report_round_trips_reports_show(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
    xdg_home: Any,
) -> None:
    """A --intent --persist run saves a faithful Report (meta + non-empty
    hypotheses); reports show retrieves the SAME report from the store."""

    insp = _make_inspector(user_inspectors_dir, name="diag.alpha", message="alpha-evidence")
    script = [
        _run_inspector(insp),
        _end_turn("巡检完成。"),
        _correlate(description="资源争用", supporting_findings=["F1"]),
        _end_turn("诊断完成：定位到根因。"),  # noqa: RUF001
    ]
    _patch_backend(monkeypatch, _fake(script))

    code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康", "--persist", "--format", "json"],
        capsys,
        monkeypatch,
    )
    assert code == 0, stderr
    report = Report.model_validate_json(stdout)
    assert report.meta is not None

    # Faithful meta: status reconciled to ok, the inspector is recorded, token
    # usage summed across both loops (non-zero — the Planner + Diagnostician each
    # consumed turns), and the hypothesis is projected and non-empty.
    assert report.meta.status == "ok"
    assert {run.name for run in report.meta.inspectors_used} == {"diag.alpha"}
    assert report.meta.token_usage.input_tokens > 0
    assert report.meta.token_usage.output_tokens > 0
    assert len(report.hypotheses) == 1
    assert report.metadata["diagnosis_narrative"] == "诊断完成：定位到根因。"  # noqa: RUF001

    run_id = report.meta.run_id

    # reports show retrieves the persisted Report (hypotheses + meta preserved).
    show_code, show_out, show_err = _run_main(
        ["reports", "show", run_id, "--format", "json"], capsys, monkeypatch
    )
    assert show_code == 0, show_err
    shown = Report.model_validate_json(show_out)
    assert shown.meta is not None
    assert shown.meta.run_id == run_id
    assert shown.meta.status == "ok"
    assert {run.name for run in shown.meta.inspectors_used} == {"diag.alpha"}
    assert len(shown.hypotheses) == 1
    # supporting_findings ⊆ persisted Report.findings ids (round-trip intact).
    finding_ids = {f.id for f in shown.findings}
    for h in shown.hypotheses:
        for ref in h.supporting_findings:
            assert ref in finding_ids


# --------------------------------------------------------------------------- #
# 4.1 — two persisted runs of the same target → finding-level reports diff
# --------------------------------------------------------------------------- #


def test_intent_persist_two_runs_finding_level_diff(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
    xdg_home: Any,
) -> None:
    """Two --intent --persist runs (both terminal status=ok) of the SAME target,
    running DIFFERENT inspectors, produce a finding-level diff with the baseline's
    finding resolved and the current's finding added."""

    alpha = _make_inspector(user_inspectors_dir, name="diag.alpha", message="alpha-evidence")
    beta = _make_inspector(user_inspectors_dir, name="diag.beta", message="beta-evidence")

    # Run A (baseline): runs diag.alpha → one finding; terminates status=ok.
    script_a = [
        _run_inspector(alpha),
        _end_turn("巡检完成。"),
        _correlate(description="A 根因", supporting_findings=["F1"]),
        _end_turn("诊断完成 A。"),
    ]
    _patch_backend(monkeypatch, _fake(script_a))
    code_a, stdout_a, stderr_a = _run_main(
        ["inspect", "local-host", "--intent", "检查健康", "--persist", "--format", "json"],
        capsys,
        monkeypatch,
    )
    assert code_a == 0, stderr_a
    report_a = Report.model_validate_json(stdout_a)
    assert report_a.meta is not None
    assert report_a.meta.status == "ok"
    run_a = report_a.meta.run_id

    # Run B (current): runs diag.beta → a DIFFERENT finding; terminates status=ok.
    script_b = [
        _run_inspector(beta),
        _end_turn("巡检完成。"),
        _correlate(description="B 根因", supporting_findings=["F1"]),
        _end_turn("诊断完成 B。"),
    ]
    _patch_backend(monkeypatch, _fake(script_b))
    code_b, stdout_b, stderr_b = _run_main(
        ["inspect", "local-host", "--intent", "检查健康", "--persist", "--format", "json"],
        capsys,
        monkeypatch,
    )
    assert code_b == 0, stderr_b
    report_b = Report.model_validate_json(stdout_b)
    assert report_b.meta is not None
    assert report_b.meta.status == "ok"
    run_b = report_b.meta.run_id

    # reports diff baseline=A current=B: alpha's finding resolved, beta's added.
    diff_code, diff_out, diff_err = _run_main(
        ["reports", "diff", run_a, run_b], capsys, monkeypatch
    )
    assert diff_code == 0, diff_err
    # The baseline was status=ok (not skipped via baseline_not_ok), so the diff is
    # a real finding-level comparison.
    assert "skipped" not in diff_out
    assert "added (1):" in diff_out
    assert "resolved (1):" in diff_out
    assert "beta-evidence" in diff_out  # added (current-only)
    assert "alpha-evidence" in diff_out  # resolved (baseline-only)


# --------------------------------------------------------------------------- #
# 4.1 — request_more supplement cited by a hypothesis survives persist round-trip
# --------------------------------------------------------------------------- #


def test_intent_persist_supplement_cited_round_trips(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
    xdg_home: Any,
) -> None:
    """A request_more_inspection supplement whose NEW finding is cited by the
    hypothesis is persisted faithfully and retrievable via reports show, with the
    citation still resolving to a real Report.findings id."""

    planner_insp = _make_inspector(user_inspectors_dir, name="diag.alpha", message="alpha-evidence")
    supplement = _make_inspector(user_inspectors_dir, name="diag.supp", message="supp-evidence")

    # Planner runs diag.alpha (F1). Diagnostician supplements with diag.supp (a
    # NEW finding labeled F2), then in a LATER turn cites F2 (the prompt discipline
    # never cites a request_more result in the same turn), then narrates ok.
    script = [
        _run_inspector(planner_insp),
        _end_turn("巡检完成。"),
        _request_more(inspector_name=supplement),
        _correlate(description="补查确认根因", supporting_findings=["F2"]),
        _end_turn("诊断完成：补查确认。"),  # noqa: RUF001
    ]
    _patch_backend(monkeypatch, _fake(script))

    code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康", "--persist", "--format", "json"],
        capsys,
        monkeypatch,
    )
    assert code == 0, stderr
    report = Report.model_validate_json(stdout)
    assert report.meta is not None
    run_id = report.meta.run_id

    # The supplemented finding landed in the Report and is the one cited.
    supplemented = next(f for f in report.findings if "supp-evidence" in f.message)
    assert len(report.hypotheses) == 1
    assert report.hypotheses[0].supporting_findings == [supplemented.id]

    # reports show retrieves the persisted Report; the citation still resolves.
    show_code, show_out, show_err = _run_main(
        ["reports", "show", run_id, "--format", "json"], capsys, monkeypatch
    )
    assert show_code == 0, show_err
    shown = Report.model_validate_json(show_out)
    shown_supplemented = next(f for f in shown.findings if "supp-evidence" in f.message)
    assert len(shown.hypotheses) == 1
    assert shown.hypotheses[0].supporting_findings == [shown_supplemented.id]


# --------------------------------------------------------------------------- #
# 4.1 — a no-result --intent --persist run stores nothing (reports list empty)
# --------------------------------------------------------------------------- #


class _PersistentUnavailableBackend:
    name = "persistent-unavailable"

    def __init__(self) -> None:
        self.capabilities = _DEFAULT_CAPS

    async def messages_create(self, **_kwargs: Any) -> MessageResponse:
        from hostlens.core.exceptions import BackendUnavailable

        raise BackendUnavailable("down", backend_name="persistent-unavailable")


def test_intent_persist_no_result_stores_nothing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
    xdg_home: Any,
) -> None:
    """A no-result (collector empty) --intent --persist run persists nothing."""

    _patch_backend(monkeypatch, cast(LLMBackend, _PersistentUnavailableBackend()))
    code, stdout, _stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康", "--persist"], capsys, monkeypatch
    )
    assert code == 2
    assert stdout == ""

    list_code, list_out, _ = _run_main(
        ["reports", "list", "local-host", "--json"], capsys, monkeypatch
    )
    assert list_code == 0
    assert json.loads(list_out) == []


# --------------------------------------------------------------------------- #
# RC-M1 — degraded-but-has-result + persist (design D-5): the collector is
# NON-empty (the Planner DID run an inspector) yet the reconciled status is a
# ``degraded_*`` value (the Diagnostician hit ``max_turns``). The CLI must still
# assemble the Report, persist it, and exit 2 — the previously zero-coverage
# branch where ``degraded_*`` coexists with a faithful, persisted Report (all
# other degraded tests pair degradation with an EMPTY collector → no-result, and
# all other --persist tests are status=ok).
# --------------------------------------------------------------------------- #


def test_intent_persist_degraded_max_turns_with_result_exit_2_persisted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
    xdg_home: Any,
) -> None:
    """Planner runs an inspector + finalizes ok (collector NON-empty), then the
    Diagnostician spins on ``correlate_findings`` until ``max_turns`` →
    ``degraded_max_turns``. With ``--persist`` the CLI assembles the faithful
    Report (``meta.status == degraded_max_turns`` + the Planner's inspector in
    ``meta.inspectors_used``), persists it, and exits 2 — and the run IS in the
    store (``reports show`` / ``reports list`` find it)."""

    # max_turns=2 makes the degraded cap cheap to hit deterministically. Trace:
    #  Planner: turn1 run_inspector (tool_use, continue), turn2 end_turn → ok
    #           (the top-of-loop cap check fires at turns>=2, but end_turn at
    #           turn2 finalizes first), collector has 1 InspectorResult.
    #  Diagnostician: turn1 correlate (tool_use, continue), turn2 correlate
    #           (tool_use, continue), then the top-of-loop check turns(2)>=2
    #           finalizes degraded_max_turns WITHOUT another backend call.
    monkeypatch.setenv("HOSTLENS_AGENT__MAX_TURNS", "2")

    insp = _make_inspector(user_inspectors_dir, name="diag.alpha", message="alpha-evidence")
    script = [
        _run_inspector(insp),  # Planner turn 1
        _end_turn("巡检完成。"),  # Planner turn 2 → terminal ok, collector non-empty
        _correlate(description="资源争用", supporting_findings=["F1"], block_id="tu_c1"),
        _correlate(description="再次相关", supporting_findings=["F1"], block_id="tu_c2"),
    ]
    _patch_backend(monkeypatch, _fake(script))

    code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康", "--persist", "--format", "json"],
        capsys,
        monkeypatch,
    )
    # Degraded → exit 2, but a Report WAS produced and rendered to stdout.
    assert code == 2, stderr
    report = Report.model_validate_json(stdout)
    assert report.meta is not None
    assert report.meta.status == "degraded_max_turns"
    # The Planner's inspector ran, so the collector was non-empty (the soul of
    # this test: NOT the no-result path).
    assert {run.name for run in report.meta.inspectors_used} == {"diag.alpha"}
    run_id = report.meta.run_id

    # The degraded Report IS in the store: reports show retrieves it with the
    # degraded status preserved.
    show_code, show_out, show_err = _run_main(
        ["reports", "show", run_id, "--format", "json"], capsys, monkeypatch
    )
    assert show_code == 0, show_err
    shown = Report.model_validate_json(show_out)
    assert shown.meta is not None
    assert shown.meta.run_id == run_id
    assert shown.meta.status == "degraded_max_turns"
    assert {run.name for run in shown.meta.inspectors_used} == {"diag.alpha"}

    # reports list also surfaces the run (it really landed in the db).
    list_code, list_out, list_err = _run_main(
        ["reports", "list", "local-host", "--json"], capsys, monkeypatch
    )
    assert list_code == 0, list_err
    listed = json.loads(list_out)
    assert any(row.get("run_id") == run_id for row in listed)


# --------------------------------------------------------------------------- #
# RC-M2 — id-consistency invariant (design D-3) at the CLI boundary: a
# hypothesis citing a finding id that is NOT in Report.findings makes
# ``_assemble_report`` raise ``ValueError("id-consistency invariant ...")``; the
# CLI's blanket ``except Exception`` wraps it into ONE ``internal: ...`` line +
# exit 2 + NO traceback, and — because the raise happens BEFORE the persist
# block — the dangling-reference report is NOT persisted (safety: never store a
# report with a dangling reference). (The unit-level raise is covered in
# test_intent_report_assembly.py; this is the CLI-boundary + no-persist version.)
# --------------------------------------------------------------------------- #


def test_intent_persist_dangling_ref_internal_exit_2_not_persisted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    targets_yaml: Any,
    user_inspectors_dir: Any,
    agent_backend_env: None,
    xdg_home: Any,
) -> None:
    """A Diagnostician result whose hypothesis cites a finding id absent from
    Report.findings makes ``_assemble_report`` fail loud; the CLI wraps it into a
    single ``internal:`` stderr line, exits 2, writes nothing to stdout, leaks no
    traceback, and persists NOTHING."""

    import hostlens.orchestration.pipeline as intent_mod
    from hostlens.agent.diagnostician import DiagnosticianResult
    from hostlens.reporting.models import ReportStatus, RootCauseHypothesis

    fake_id = "deadbeef-not-a-real-finding"

    async def _dangling_run_diagnosis(
        planner_result: Any,
        seeded_findings: Any,
        finding_store: Any,
        diagnostician_agent_factory: Any,
        *,
        observer: Any = None,
    ) -> DiagnosticianResult:
        # Reuse the real planner_result (carries the Planner's ok LoopResult) so
        # the rest of run_intent_diagnosis (token sum / snapshot) stays faithful;
        # only the hypotheses are forced to carry a dangling reference.
        return DiagnosticianResult(
            narrative="",
            findings=[item.finding for item in seeded_findings],
            hypotheses=[
                RootCauseHypothesis(
                    description="悬空引用",
                    confidence="low",
                    supporting_findings=[fake_id],
                    suggested_actions=[],
                )
            ],
            status=ReportStatus.OK,
            planner_result=planner_result,
            diagnostician_loop=None,
        )

    monkeypatch.setattr(intent_mod, "run_diagnosis", _dangling_run_diagnosis)

    insp = _make_inspector(user_inspectors_dir, name="diag.alpha", message="alpha-evidence")
    script = [
        _run_inspector(insp),  # Planner runs one inspector → collector non-empty
        _end_turn("巡检完成。"),  # Planner terminal ok
    ]
    _patch_backend(monkeypatch, _fake(script))

    code, stdout, stderr = _run_main(
        ["inspect", "local-host", "--intent", "检查健康", "--persist", "--format", "json"],
        capsys,
        monkeypatch,
    )
    assert code == 2
    # The assembly invariant fails loud → one internal: line at the CLI boundary.
    internal_lines = [ln for ln in stderr.splitlines() if "internal:" in ln]
    assert len(internal_lines) == 1
    assert "id-consistency" in internal_lines[0] or "invariant" in internal_lines[0]
    # No traceback / pydantic internals leak; nothing rendered to stdout.
    assert "Traceback" not in stderr
    assert stdout == ""

    # The dangling-reference report was NOT persisted (the raise precedes the
    # persist block): the store is empty for this target.
    list_code, list_out, list_err = _run_main(
        ["reports", "list", "local-host", "--json"], capsys, monkeypatch
    )
    assert list_code == 0, list_err
    assert json.loads(list_out) == []
