"""Tests for ``hostlens.agent.planner.PlannerAgent`` (Group B, §4 + §5).

The Planner is the inspection-semantics assembler over ``AgentLoop``: it
turns a natural-language intent into one fully-wired loop run and condenses
the generic ``LoopResult`` into a ``PlannerResult`` (narrative + structured
findings + loop telemetry). These tests pin the four contracts the spec
mandates:

  * lossless ordered collection of successful ``run_inspector`` findings;
  * skipping error invocations while keeping them in ``tool_invocations``;
  * verbatim pass-through of ``terminal_status`` / ``final_text`` (no second
    judgment, no retry amplification on top of the loop);
  * deterministic, cache-shaped system prompt assembly.

Findings are made deterministic WITHOUT any inspector / target / SSH
infrastructure by registering a **stub ToolSpec whose name equals
``run_inspector.name``**: the Planner collects by tool name, so the loop's
real dispatch runs the stub handler and the resulting ``ToolInvocation.output``
carries exactly the findings the stub returns.

``asyncio_mode = "auto"`` (pyproject) — no ``@pytest.mark.asyncio`` needed.
No ``@pytest.mark.live``: every backend here is fake / playback, so CI runs
them by default without touching the real API.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

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
from hostlens.agent.planner import PlannerAgent, PlannerResult
from hostlens.core.config import AgentSettings, Settings
from hostlens.core.exceptions import BackendRateLimited, BackendUnavailable
from hostlens.reporting.models import Finding
from hostlens.tools.base import ToolContext, ToolSpec
from hostlens.tools.default_tools import run_inspector
from hostlens.tools.registry import ToolRegistry
from hostlens.tools.schemas.run_inspector import RunInspectorInput, RunInspectorOutput

from ._helpers import make_ctx

# ---------------------------------------------------------------------------
# Shared builders
# ---------------------------------------------------------------------------

# Same shape as FakeBackend / PlaybackBackend defaults so the scripted backends
# used in §5 do not drift from production-path capability declarations.
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
_RUN_INSPECTOR_INPUT = {"target_name": "t1", "inspector_name": "insp"}


def _settings(**agent_kwargs: Any) -> Settings:
    return Settings(agent=AgentSettings(**agent_kwargs))


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


def _tool_use_turn(*, block_id: str) -> MessageResponse:
    return _msg(
        content=[
            ToolUseBlock(
                type="tool_use",
                id=block_id,
                name=run_inspector.name,
                input=_RUN_INSPECTOR_INPUT,
            )
        ],
        stop_reason="tool_use",
    )


def _end_turn(text: str) -> MessageResponse:
    return _msg(content=[TextBlock(type="text", text=text)], stop_reason="end_turn")


def _make_finding(msg: str) -> Finding:
    # severity/message are the only required Finding fields; evidence/tags
    # default to empty so a bare info finding is the minimal valid shape.
    return Finding(severity="info", message=msg)


def _stub_spec(
    handler: Any,
) -> ToolSpec:
    """Build a ToolSpec named ``run_inspector.name`` wrapping ``handler``.

    The name match is what makes the Planner collect this tool's output —
    handler identity is irrelevant, so the stub fully controls the findings
    the Planner sees without any real inspector/target infrastructure.
    """
    return ToolSpec(
        name=run_inspector.name,
        version="1.0.0",
        input_schema=RunInspectorInput,
        output_schema=RunInspectorOutput,
        handler=handler,
        agent_description="stub run inspector",
        mcp_description="stub",
        cli_help=None,
        surfaces=cast(Any, {"agent"}),
        side_effects=cast(Any, "read"),
        requires_approval=False,
        sensitive_output=True,
        timeout=30.0,
    )


def _two_finding_output() -> RunInspectorOutput:
    return RunInspectorOutput(
        target_name="t1",
        inspector_name="insp",
        findings=[_make_finding("f1"), _make_finding("f2")],
    )


def _registry_with(spec: ToolSpec) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(spec)
    return reg


def _planner(backend: LLMBackend, registry: ToolRegistry, settings: Settings) -> PlannerAgent:
    return PlannerAgent(backend, registry, settings, make_ctx)


# ---------------------------------------------------------------------------
# 4.1 — lossless ordered findings merge across two successful run_inspector
# ---------------------------------------------------------------------------


async def test_findings_merged_in_order_across_two_run_inspector_calls() -> None:
    async def handler(args: RunInspectorInput, ctx: ToolContext) -> RunInspectorOutput:
        return _two_finding_output()

    registry = _registry_with(_stub_spec(handler))
    backend = FakeBackend(
        responses=[
            _tool_use_turn(block_id="tu_1"),
            _tool_use_turn(block_id="tu_2"),
            _end_turn("综述完成"),
        ]
    )

    result = await _planner(cast(LLMBackend, backend), registry, _settings()).run("检查健康")

    assert isinstance(result, PlannerResult)
    assert result.loop_result.terminal_status == "ok"
    assert result.narrative == "综述完成"
    assert result.intent == "检查健康"
    # Two calls x two findings each, preserved in emission order, lossless.
    assert [f.message for f in result.findings] == ["f1", "f2", "f1", "f2"]
    assert all(isinstance(f, Finding) for f in result.findings)


# ---------------------------------------------------------------------------
# 4.2 — failed tool invocation: findings skipped, invocation retained
# ---------------------------------------------------------------------------


async def test_failed_run_inspector_skipped_but_kept_in_invocations() -> None:
    calls = {"n": 0}

    async def handler(args: RunInspectorInput, ctx: ToolContext) -> RunInspectorOutput:
        calls["n"] += 1
        if calls["n"] == 1:
            return _two_finding_output()
        # Second call fails loud inside the handler → dispatch wraps it into
        # an error envelope (is_error) → ToolInvocation.error set, output None.
        raise RuntimeError("boom")

    registry = _registry_with(_stub_spec(handler))
    backend = FakeBackend(
        responses=[
            _tool_use_turn(block_id="tu_ok"),
            _tool_use_turn(block_id="tu_bad"),
            _end_turn("done"),
        ]
    )

    result = await _planner(cast(LLMBackend, backend), registry, _settings()).run("intent")

    # Only the first (successful) invocation contributes findings.
    assert [f.message for f in result.findings] == ["f1", "f2"]

    invocations = result.loop_result.tool_invocations
    assert len(invocations) == 2
    by_id = {inv.tool_use_id: inv for inv in invocations}
    assert by_id["tu_ok"].output is not None and by_id["tu_ok"].error is None
    # The failed invocation is retained for debugging — error set, output None.
    assert by_id["tu_bad"].error is not None
    assert by_id["tu_bad"].output is None


# ---------------------------------------------------------------------------
# 4.3 — no inspector calls: empty findings, ok, no exception
# ---------------------------------------------------------------------------


async def test_no_inspector_call_returns_empty_findings_ok() -> None:
    async def handler(args: RunInspectorInput, ctx: ToolContext) -> RunInspectorOutput:
        raise AssertionError("handler must not be called when agent never uses the tool")

    registry = _registry_with(_stub_spec(handler))
    backend = FakeBackend(responses=[_end_turn("no inspection needed")])

    result = await _planner(cast(LLMBackend, backend), registry, _settings()).run("just say hi")

    assert result.findings == []
    assert result.loop_result.terminal_status == "ok"
    assert result.narrative == "no inspection needed"


# ---------------------------------------------------------------------------
# 4.4 — degraded_max_turns: partial findings, empty narrative, no retry/raise
# ---------------------------------------------------------------------------


async def test_degraded_max_turns_passthrough_empty_narrative() -> None:
    async def handler(args: RunInspectorInput, ctx: ToolContext) -> RunInspectorOutput:
        return _two_finding_output()

    registry = _registry_with(_stub_spec(handler))
    # max_turns=1: turn 1 dispatches run_inspector (collecting findings); the
    # turn-2 pre-flight guard trips turns(1) >= max_turns(1) → degraded_max_turns
    # before any assistant text is captured, so final_text stays "".
    backend = FakeBackend(
        responses=[
            _tool_use_turn(block_id="tu_1"),
            # A second response exists but must NOT be consumed (guard fires
            # first); FakeBackend would otherwise raise IndexError if reached.
            _end_turn("never reached"),
        ]
    )

    result = await _planner(cast(LLMBackend, backend), registry, _settings(max_turns=1)).run("x")

    assert result.loop_result.terminal_status == "degraded_max_turns"
    # Partial findings collected from the one completed turn are preserved.
    assert [f.message for f in result.findings] == ["f1", "f2"]
    # This degraded path never fills final_text — Planner must pass "" through
    # verbatim, not fabricate a narrative.
    assert result.narrative == ""


# ---------------------------------------------------------------------------
# 4.4b — max_tokens degradation: narrative is the partial text (NON-empty)
# ---------------------------------------------------------------------------


async def test_max_tokens_degraded_narrative_passthrough_nonempty() -> None:
    registry = _registry_with(_stub_spec(_unused_handler))
    # stop_reason == "max_tokens" with a text block: the loop captures the
    # partial text into final_text and finalizes as degraded_token_budget.
    # Small usage keeps the pre-flight token-budget guard from short-circuiting
    # first (default budgets are 100k/30k).
    backend = FakeBackend(
        responses=[
            _msg(
                content=[TextBlock(type="text", text="部分输出")],
                stop_reason="max_tokens",
            )
        ]
    )

    result = await _planner(cast(LLMBackend, backend), registry, _settings()).run("x")

    assert result.loop_result.terminal_status == "degraded_token_budget"
    # The whole point: a degraded terminal_status must NOT blank the narrative
    # when the loop did capture partial model output.
    assert result.narrative == "部分输出"
    assert result.findings == []


async def _unused_handler(args: RunInspectorInput, ctx: ToolContext) -> RunInspectorOutput:
    raise AssertionError("handler must not run in this scenario")


# ---------------------------------------------------------------------------
# 4.5 — system prompt stability + text-block-list shape (cache prerequisite)
# ---------------------------------------------------------------------------


async def test_system_prompt_byte_stable_and_text_block_list_shape() -> None:
    registry = _registry_with(_stub_spec(_unused_handler))
    backend = FakeBackend(responses=[])

    # Two independent constructions over the SAME registry must render the
    # identical system text (byte-stable) — the prompt-caching prerequisite.
    p1 = PlannerAgent(cast(LLMBackend, backend), registry, _settings(), make_ctx)
    p2 = PlannerAgent(cast(LLMBackend, backend), registry, _settings(), make_ctx)

    sys1 = _loop_system(p1)
    sys2 = _loop_system(p2)

    # Shape: single-element text block list, not a bare str (a bare str makes
    # AgentLoop._inject_cache_control skip cache_control injection silently).
    assert isinstance(sys1, list)
    assert len(sys1) == 1
    assert sys1[0]["type"] == "text"
    assert isinstance(sys1[0]["text"], str)
    assert sys1[0]["text"]  # non-empty rendered prompt
    # Byte-stable across constructions.
    assert sys1 == sys2
    assert sys1[0]["text"] == sys2[0]["text"]


def _loop_system(planner: PlannerAgent) -> list[dict[str, Any]] | str:
    """Read the ``system`` the Planner handed to its private ``AgentLoop``.

    Reaching through the private ``_loop._system`` is acceptable in a unit
    test pinning the cache-prerequisite contract (the system shape is not yet
    exposed on a public API; M2.5 cache-hit tests will assert on it too).
    """
    return planner._loop._system


# ---------------------------------------------------------------------------
# 4.6 — end-to-end deterministic playback (cassette, no API quota)
# ---------------------------------------------------------------------------


class _RecordingBackend:
    """Wraps a ``FakeBackend`` and records each request/response as a cassette
    record so the recorded cassette's request keys are guaranteed to match
    what the loop will send on replay (hand-writing the multi-turn messages
    by hand is error-prone).
    """

    name = "recording"

    def __init__(self, inner: FakeBackend) -> None:
        self._inner = inner
        self.capabilities = inner.capabilities
        self.records: list[dict[str, Any]] = []

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
        resp = await self._inner.messages_create(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            timeout=timeout,
        )
        # Deep-copy messages: the loop mutates a single ``messages`` list in
        # place across turns, so a by-reference capture would leave every
        # record pointing at the final mutated list (key mismatch on replay).
        self.records.append(
            {
                "request": {
                    "model": model,
                    "messages": json.loads(json.dumps(messages, ensure_ascii=False)),
                    "tools_count": len(tools),
                },
                "response": resp.model_dump(mode="json"),
            }
        )
        return resp


@pytest.fixture
def planner_cassette(tmp_path: Path) -> tuple[Path, ToolRegistry]:
    """Generate a deterministic cassette by recording a FakeBackend run, then
    return the cassette path plus the SAME registry used to record it.

    Re-using the recording registry on replay keeps ``tools_count`` (part of
    the cassette request key) identical between record and replay, otherwise
    the key would miss.
    """

    async def handler(args: RunInspectorInput, ctx: ToolContext) -> RunInspectorOutput:
        return _two_finding_output()

    registry = _registry_with(_stub_spec(handler))
    fake = FakeBackend(
        responses=[
            _tool_use_turn(block_id="tu_1"),
            _end_turn("机器健康，未发现严重问题。"),  # noqa: RUF001
        ]
    )
    recorder = _RecordingBackend(fake)

    async def _record() -> None:
        await PlannerAgent(cast(LLMBackend, recorder), registry, _settings(), make_ctx).run(
            "检查这台机器的健康状况"
        )

    asyncio.run(_record())

    cassette_path = tmp_path / "planner_health_check.jsonl"
    with cassette_path.open("w", encoding="utf-8") as fp:
        for record in recorder.records:
            fp.write(json.dumps(record, ensure_ascii=False) + "\n")
    return cassette_path, registry


async def test_playback_end_to_end_deterministic(
    planner_cassette: tuple[Path, ToolRegistry],
) -> None:
    cassette_path, registry = planner_cassette

    async def _run_once() -> PlannerResult:
        backend = PlaybackBackend(cassette_path=cassette_path)
        return await PlannerAgent(cast(LLMBackend, backend), registry, _settings(), make_ctx).run(
            "检查这台机器的健康状况"
        )

    first = await _run_once()
    assert first.loop_result.terminal_status == "ok"
    assert first.findings  # non-empty
    assert [f.message for f in first.findings] == ["f1", "f2"]
    assert first.narrative == "机器健康，未发现严重问题。"  # noqa: RUF001

    # Deterministic replay: a second run over the same cassette yields the same
    # condensed result (no API quota consumed in either run).
    second = await _run_once()
    assert second.narrative == first.narrative
    assert [f.message for f in second.findings] == [f.message for f in first.findings]
    assert second.loop_result.terminal_status == first.loop_result.terminal_status


# ---------------------------------------------------------------------------
# 5. Backend failure pass-through (loop owns retry; Planner must not amplify)
# ---------------------------------------------------------------------------


class _CountingFailBackend:
    """Structural ``LLMBackend`` that raises a fixed exception on every call
    and counts invocations, so a test can assert the Planner does NOT retry on
    top of the loop's own retry budget (the call count must equal the loop's
    retry ceiling, not a multiple of it).
    """

    name = "counting-fail"

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.calls = 0
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
        self.calls += 1
        raise self._exc


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the loop's retry backoff to a no-op so the §5 failure tests do not
    actually sleep through the 1s/4s/16s schedule.
    """

    async def _instant(_delay: float) -> None:
        return None

    monkeypatch.setattr("hostlens.agent.loop.asyncio.sleep", _instant)


async def test_persistent_unavailable_failed_api_unavailable_no_amplified_retry() -> None:
    registry = _registry_with(_stub_spec(_unused_handler))
    backend = _CountingFailBackend(BackendUnavailable("down", backend_name="counting-fail"))

    result = await _planner(cast(LLMBackend, backend), registry, _settings()).run("x")

    # No tool result was ever produced (first call already fails) → the loop
    # finalizes as failed_api_unavailable, passed through verbatim.
    assert result.loop_result.terminal_status == "failed_api_unavailable"
    assert result.narrative == ""
    assert result.findings == []
    # Loop retry ceiling for the unavailable family is initial + 3 retries = 4
    # calls. The Planner must not multiply this — exactly the loop's budget.
    assert backend.calls == 4


async def test_persistent_rate_limit_degraded_passthrough_no_amplified_retry() -> None:
    registry = _registry_with(_stub_spec(_unused_handler))
    # retry_after_seconds=0.0 keeps the (patched) backoff trivial and exercises
    # the rate-limit retry path; honoring retry-after is the loop's job, not
    # the Planner's.
    backend = _CountingFailBackend(
        BackendRateLimited(backend_name="counting-fail", retry_after_seconds=0.0)
    )

    result = await _planner(cast(LLMBackend, backend), registry, _settings()).run("x")

    assert result.loop_result.terminal_status == "degraded_rate_limited"
    assert result.narrative == ""
    # Same ceiling: initial + 3 retries = 4. Planner neither swallows nor adds
    # retries on top of the loop.
    assert backend.calls == 4
