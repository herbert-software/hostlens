"""Unit tests for ``hostlens.agent.loop.AgentLoop`` (Group B, §6).

Zero-API: happy-path responses come from the existing ``FakeBackend``
(sequential canned ``MessageResponse`` queue); failure / call-count
assertions use the local ``_ScriptedBackend`` defined below (returns or
raises per a scripted event list, records ``calls`` + last request fields).

Retry backoff (``asyncio.sleep``) is patched to a no-op in the failure tests
so they do not actually sleep 1/4/16s.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from hostlens.agent.backend import (
    BackendCapabilities,
    LLMBackend,
    MessageResponse,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    Usage,
)
from hostlens.agent.backends.fake import FakeBackend
from hostlens.agent.loop import AgentLoop, LoopResult, LoopUsage, ToolInvocation
from hostlens.agent.tools_adapter import ToolsAdapter
from hostlens.core.config import AgentSettings, Settings
from hostlens.core.exceptions import (
    BackendCapabilityViolation,
    BackendError,
    BackendRateLimited,
    BackendUnavailable,
    ConfigError,
    ToolError,
    ToolPolicyViolation,
    UnexpectedStopReason,
)
from hostlens.tools.base import ToolContext
from hostlens.tools.registry import ToolRegistry

from ._helpers import (
    EmptyOutput,
    TypedInput,
    TypedOutput,
    ctx_factory,
    make_spec,
    ok_handler,
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Clear ``HOSTLENS_*`` env and chdir off the repo so a dev ``.env`` /
    exported ``HOSTLENS_*`` don't leak a configured agent into the test
    asserting ``settings.agent is None`` → ConfigError. ``Settings()`` reads
    ``.env`` from cwd, so the chdir is what actually blocks the file read."""

    for key in list(os.environ):
        if key.startswith("HOSTLENS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

_NO_CACHE_CAPS = BackendCapabilities(
    prompt_caching=False,
    tool_use=True,
    structured_output=True,
    parallel_tool_use=True,
    extended_thinking=False,
    vision=True,
    streaming=False,
)


def _settings() -> Settings:
    return Settings(agent=AgentSettings())


def _settings_with(**agent_kwargs: Any) -> Settings:
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
        stop_reason=stop_reason,  # type: ignore[arg-type]
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _text(text: str) -> TextBlock:
    return TextBlock(type="text", text=text)


def _tool_use(*, block_id: str, name: str, tool_input: dict[str, Any]) -> ToolUseBlock:
    return ToolUseBlock(type="tool_use", id=block_id, name=name, input=tool_input)


def _thinking(*, thinking: str, signature: str, **extra: Any) -> ThinkingBlock:
    return ThinkingBlock(type="thinking", thinking=thinking, signature=signature, **extra)


class _ScriptedBackend:
    """Structural ``LLMBackend`` that replays a scripted event list.

    Each event is either a ``MessageResponse`` (returned) or an ``Exception``
    (raised). ``calls`` counts every ``messages_create`` invocation; the last
    request's ``system`` / ``messages`` / ``tools`` are retained for assertion.

    Event-consumption semantics: events are consumed in order via an index.
    When the index runs past the list, the **last** event is repeated — this
    lets a single trailing ``BackendUnavailable()`` model "every retry fails"
    without enumerating one per retry.
    """

    name = "scripted"

    def __init__(
        self,
        events: list[MessageResponse | Exception],
        *,
        capabilities: BackendCapabilities | None = None,
    ) -> None:
        self._events = events
        self._idx = 0
        self.calls = 0
        self.last_system: list[dict[str, Any]] | str | None = None
        self.last_messages: list[dict[str, Any]] | None = None
        self.last_tools: list[dict[str, Any]] | None = None
        self.last_max_tokens: int | None = None
        self.capabilities = capabilities if capabilities is not None else _DEFAULT_CAPS

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
        self.last_system = system
        # Copy so later loop mutation of the messages list cannot rewrite the
        # snapshot we assert against.
        self.last_messages = list(messages)
        self.last_tools = tools
        self.last_max_tokens = max_tokens
        event = self._events[min(self._idx, len(self._events) - 1)]
        self._idx += 1
        if isinstance(event, Exception):
            raise event
        return event


_DEFAULT_CAPS = BackendCapabilities(
    prompt_caching=True,
    tool_use=True,
    structured_output=True,
    parallel_tool_use=True,
    extended_thinking=False,
    vision=True,
    streaming=False,
)


def _adapter_with(*specs: Any) -> ToolsAdapter:
    reg = ToolRegistry()
    for spec in specs:
        reg.register(spec)
    return ToolsAdapter(reg, ctx_factory())


def _empty_adapter() -> ToolsAdapter:
    return ToolsAdapter(ToolRegistry(), ctx_factory())


def _loop(
    backend: FakeBackend | _ScriptedBackend,
    adapter: ToolsAdapter,
    settings: Settings,
    *,
    system: list[dict[str, Any]] | str | None = None,
) -> AgentLoop:
    # FakeBackend / _ScriptedBackend declare ``name`` as a ClassVar while the
    # LLMBackend Protocol expects an instance var; both satisfy the protocol
    # structurally at runtime. The cast mirrors create_backend's workaround so
    # AgentLoop's LLMBackend-typed param accepts these test backends.
    return AgentLoop(cast(LLMBackend, backend), adapter, settings, system=system)


# Handlers used across tests -------------------------------------------------


async def _boom_value_handler(args: Any, ctx: ToolContext) -> EmptyOutput:
    raise ValueError("boom")


async def _keyerror_handler(args: Any, ctx: ToolContext) -> EmptyOutput:
    raise KeyError("internal-missing-key")


# ---------------------------------------------------------------------------
# 6.1 single + multi turn
# ---------------------------------------------------------------------------


async def test_single_turn_end_turn_ok() -> None:
    backend = FakeBackend(responses=[_msg(content=[_text("done")], stop_reason="end_turn")])
    loop = _loop(backend, _empty_adapter(), _settings())

    result = await loop.run("inspect host")

    assert result.turns == 1
    assert result.terminal_status == "ok"
    assert result.final_text == "done"
    assert result.tool_invocations == []
    assert result.stop_reason == "end_turn"


async def test_tool_use_then_end_turn_two_turns() -> None:
    spec = make_spec(name="list_inspectors", handler=ok_handler)
    backend = _ScriptedBackend(
        [
            _msg(
                content=[_tool_use(block_id="toolu_1", name="list_inspectors", tool_input={})],
                stop_reason="tool_use",
            ),
            _msg(content=[_text("all good")], stop_reason="end_turn"),
        ]
    )
    loop = _loop(backend, _adapter_with(spec), _settings())

    result = await loop.run("list inspectors")

    assert result.turns == 2
    assert result.terminal_status == "ok"
    assert result.final_text == "all good"
    assert len(result.tool_invocations) == 1
    inv = result.tool_invocations[0]
    assert inv.tool_name == "list_inspectors"
    assert inv.tool_use_id == "toolu_1"
    assert inv.output is not None
    assert inv.error is None

    # Second-turn messages must carry the assistant tool_use + the user
    # tool_result it answers.
    msgs = backend.last_messages
    assert msgs is not None
    assistant = next(m for m in msgs if m["role"] == "assistant")
    assert any(
        block.get("type") == "tool_use" and block.get("id") == "toolu_1"
        for block in assistant["content"]
    )
    tool_result_msg = next(
        m
        for m in msgs
        if m["role"] == "user"
        and isinstance(m["content"], list)
        and any(block.get("type") == "tool_result" for block in m["content"])
    )
    tr = tool_result_msg["content"][0]
    assert tr["tool_use_id"] == "toolu_1"
    assert isinstance(tr["content"], str)


# ---------------------------------------------------------------------------
# 6.2 parallel dispatch
# ---------------------------------------------------------------------------


async def test_parallel_dispatch_results_route_by_id_one_fails() -> None:
    ok_spec = make_spec(name="ok_tool", handler=ok_handler)
    bad_spec = make_spec(name="bad_tool", handler=_boom_value_handler)
    backend = _ScriptedBackend(
        [
            _msg(
                content=[
                    _tool_use(block_id="toolu_ok", name="ok_tool", tool_input={}),
                    _tool_use(block_id="toolu_bad", name="bad_tool", tool_input={}),
                ],
                stop_reason="tool_use",
            ),
            _msg(content=[_text("finished")], stop_reason="end_turn"),
        ]
    )
    loop = _loop(backend, _adapter_with(ok_spec, bad_spec), _settings())

    result = await loop.run("run two tools")

    assert result.terminal_status == "ok"
    assert len(result.tool_invocations) == 2
    by_id = {inv.tool_use_id: inv for inv in result.tool_invocations}
    assert by_id["toolu_ok"].output is not None
    assert by_id["toolu_ok"].error is None
    assert by_id["toolu_bad"].error is not None
    assert by_id["toolu_bad"].output is None

    # Both tool_result blocks land in the next-turn user message, each keyed to
    # the originating tool_use id.
    msgs = backend.last_messages
    assert msgs is not None
    tool_result_msg = next(
        m
        for m in msgs
        if m["role"] == "user"
        and isinstance(m["content"], list)
        and any(block.get("type") == "tool_result" for block in m["content"])
    )
    result_ids = {block["tool_use_id"] for block in tool_result_msg["content"]}
    assert result_ids == {"toolu_ok", "toolu_bad"}
    error_block = next(b for b in tool_result_msg["content"] if b["tool_use_id"] == "toolu_bad")
    assert error_block.get("is_error") is True


async def test_parallel_failloud_cancels_siblings() -> None:
    # One parallel tool fails loud (output-contract ToolError), while a sibling
    # tool is a long-running handler that never returns on its own. The loop
    # must cancel the still-running sibling instead of orphaning it (gather
    # default leaks unfinished siblings on first exception).
    cancelled = {"v": False}

    async def _long_running_handler(args: Any, ctx: ToolContext) -> EmptyOutput:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled["v"] = True
            raise
        return EmptyOutput()

    # bad_out: ok_handler returns EmptyOutput but output_schema declares
    # TypedOutput → dispatch raises ToolError (fail-loud).
    bad_spec = make_spec(name="bad_out", output_schema=TypedOutput, handler=ok_handler)
    long_spec = make_spec(name="long_running", handler=_long_running_handler)
    backend = _ScriptedBackend(
        [
            _msg(
                content=[
                    _tool_use(block_id="toolu_bad", name="bad_out", tool_input={}),
                    _tool_use(block_id="toolu_long", name="long_running", tool_input={}),
                ],
                stop_reason="tool_use",
            ),
        ]
    )
    loop = _loop(backend, _adapter_with(bad_spec, long_spec), _settings())

    with pytest.raises(ToolError):
        await loop.run("one fails, one hangs")

    assert cancelled["v"] is True


# ---------------------------------------------------------------------------
# 6.3 error routing (D-5)
# ---------------------------------------------------------------------------


async def test_handler_exception_envelope_fed_back_not_double_scrubbed() -> None:
    spec = make_spec(name="boom_tool", handler=_boom_value_handler)
    backend = _ScriptedBackend(
        [
            _msg(
                content=[_tool_use(block_id="toolu_1", name="boom_tool", tool_input={})],
                stop_reason="tool_use",
            ),
            _msg(content=[_text("ok")], stop_reason="end_turn"),
        ]
    )
    loop = _loop(backend, _adapter_with(spec), _settings())

    result = await loop.run("boom")

    assert result.terminal_status == "ok"
    inv = result.tool_invocations[0]
    assert inv.error is not None
    # dispatch already scrubbed; the envelope is fed back verbatim. "boom" has
    # no sensitive substrings so it survives — proving no second scrub.
    assert inv.error["is_error"] is True
    assert inv.error["error_kind"] == "ValueError"
    assert "boom" in inv.error["message"]

    # The tool_result content carries the same envelope JSON-serialized.
    msgs = backend.last_messages
    assert msgs is not None
    tr_msg = next(
        m
        for m in msgs
        if m["role"] == "user"
        and isinstance(m["content"], list)
        and any(b.get("type") == "tool_result" for b in m["content"])
    )
    payload = json.loads(tr_msg["content"][0]["content"])
    assert payload["error_kind"] == "ValueError"
    assert "boom" in payload["message"]


async def test_malformed_args_typeerror_fed_back() -> None:
    # TypedInput requires name + version; empty input fails schema → TypeError.
    spec = make_spec(name="typed_tool", input_schema=TypedInput, handler=ok_handler)
    backend = _ScriptedBackend(
        [
            _msg(
                content=[_tool_use(block_id="toolu_1", name="typed_tool", tool_input={})],
                stop_reason="tool_use",
            ),
            _msg(content=[_text("done")], stop_reason="end_turn"),
        ]
    )
    loop = _loop(backend, _adapter_with(spec), _settings())

    result = await loop.run("bad args")

    assert result.terminal_status == "ok"
    inv = result.tool_invocations[0]
    assert inv.error is not None
    assert inv.error["error_kind"] == "TypeError"
    assert inv.output is None


async def test_output_contract_toolerror_propagates() -> None:
    # ok_handler returns EmptyOutput, but output_schema declares TypedOutput →
    # dispatch raises ToolError (handler/adapter code bug). ToolError is not a
    # TypeError subclass, so the loop's malformed-args TypeError branch does not
    # catch it; it propagates out of run() to fail loud.
    spec = make_spec(name="bad_out", output_schema=TypedOutput, handler=ok_handler)
    backend = _ScriptedBackend(
        [
            _msg(
                content=[_tool_use(block_id="toolu_1", name="bad_out", tool_input={})],
                stop_reason="tool_use",
            ),
            _msg(content=[_text("unreached")], stop_reason="end_turn"),
        ]
    )
    loop = _loop(backend, _adapter_with(spec), _settings())

    with pytest.raises(ToolError):
        await loop.run("bad output")


async def test_hallucinated_tool_name_intercepted_before_dispatch() -> None:
    real = make_spec(name="real_tool", handler=ok_handler)
    backend = _ScriptedBackend(
        [
            _msg(
                content=[_tool_use(block_id="toolu_1", name="ghost_tool", tool_input={})],
                stop_reason="tool_use",
            ),
            _msg(content=[_text("done")], stop_reason="end_turn"),
        ]
    )
    loop = _loop(backend, _adapter_with(real), _settings())

    result = await loop.run("call ghost")

    assert result.terminal_status == "ok"
    inv = result.tool_invocations[0]
    assert inv.tool_name == "ghost_tool"
    assert inv.error is not None
    assert inv.error["error_kind"] == "UnknownTool"
    assert "ghost_tool" in inv.error["message"]


async def test_registered_handler_keyerror_propagates() -> None:
    spec = make_spec(name="key_tool", handler=_keyerror_handler)
    backend = _ScriptedBackend(
        [
            _msg(
                content=[_tool_use(block_id="toolu_1", name="key_tool", tool_input={})],
                stop_reason="tool_use",
            ),
        ]
    )
    loop = _loop(backend, _adapter_with(spec), _settings())

    with pytest.raises(KeyError):
        await loop.run("trigger keyerror")


async def test_tool_policy_violation_propagates() -> None:
    # write side-effect → dispatch raises ToolPolicyViolation, but it is still
    # advertised to the agent (surfaces includes "agent"), so the loop reaches
    # dispatch and the violation propagates.
    spec = make_spec(
        name="policy_tool",
        side_effects="write",
        handler=ok_handler,
    )
    backend = _ScriptedBackend(
        [
            _msg(
                content=[_tool_use(block_id="toolu_1", name="policy_tool", tool_input={})],
                stop_reason="tool_use",
            ),
        ]
    )
    loop = _loop(backend, _adapter_with(spec), _settings())

    with pytest.raises(ToolPolicyViolation):
        await loop.run("trigger policy")


# ---------------------------------------------------------------------------
# 6.4 cache_control gate
# ---------------------------------------------------------------------------


async def test_cache_control_not_injected_when_disabled() -> None:
    backend = _ScriptedBackend(
        [_msg(content=[_text("ok")], stop_reason="end_turn")],
        capabilities=_NO_CACHE_CAPS,
    )
    loop = _loop(
        backend,
        _empty_adapter(),
        _settings(),
        system=[{"type": "text", "text": "sys"}],
    )

    await loop.run("hi")

    assert isinstance(backend.last_system, list)
    assert all("cache_control" not in block for block in backend.last_system)


async def test_cache_control_injected_when_enabled() -> None:
    backend = _ScriptedBackend(
        [_msg(content=[_text("ok")], stop_reason="end_turn")],
        capabilities=_DEFAULT_CAPS,
    )
    loop = _loop(
        backend,
        _empty_adapter(),
        _settings(),
        system=[{"type": "text", "text": "sys"}],
    )

    await loop.run("hi")

    assert isinstance(backend.last_system, list)
    assert backend.last_system[-1]["cache_control"] == {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# 6.5 budget / max_turns guards
# ---------------------------------------------------------------------------


async def test_token_budget_exceeded_stops_after_one_call() -> None:
    spec = make_spec(name="t", handler=ok_handler)
    backend = _ScriptedBackend(
        [
            _msg(
                content=[_tool_use(block_id="toolu_1", name="t", tool_input={})],
                stop_reason="tool_use",
                output_tokens=5,
            ),
            _msg(content=[_text("never reached")], stop_reason="end_turn"),
        ]
    )
    loop = _loop(backend, _adapter_with(spec), _settings_with(token_budget_output=1))

    result = await loop.run("over budget")

    assert result.terminal_status == "degraded_token_budget"
    assert backend.calls == 1


async def test_max_tokens_shrinks_to_remaining_budget() -> None:
    spec = make_spec(name="t", handler=ok_handler)
    backend = _ScriptedBackend(
        [
            _msg(
                content=[_tool_use(block_id="toolu_1", name="t", tool_input={})],
                stop_reason="tool_use",
                output_tokens=30,
            ),
            _msg(content=[_text("done")], stop_reason="end_turn"),
        ]
    )
    loop = _loop(backend, _adapter_with(spec), _settings_with(token_budget_output=100))

    await loop.run("shrink budget")

    # Second turn requests only the remaining 100 - 30 = 70 output tokens.
    assert backend.last_max_tokens == 70


async def test_max_turns_guard_stops_at_limit() -> None:
    spec = make_spec(name="t", handler=ok_handler)
    backend = _ScriptedBackend(
        [
            _msg(
                content=[_tool_use(block_id="toolu_1", name="t", tool_input={})],
                stop_reason="tool_use",
            )
        ]
    )
    loop = _loop(backend, _adapter_with(spec), _settings_with(max_turns=2))

    result = await loop.run("loop forever")

    assert result.turns == 2
    assert result.terminal_status == "degraded_max_turns"
    assert backend.calls == 2


# ---------------------------------------------------------------------------
# 6.6 backend failure (asyncio.sleep patched to no-op)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _instant(_delay: float) -> None:
        return None

    monkeypatch.setattr("hostlens.agent.loop.asyncio.sleep", _instant)


async def test_rate_limited_retry_then_success() -> None:
    backend = _ScriptedBackend(
        [
            BackendRateLimited(backend_name="scripted", retry_after_seconds=0),
            _msg(content=[_text("recovered")], stop_reason="end_turn"),
        ]
    )
    loop = _loop(backend, _empty_adapter(), _settings())

    result = await loop.run("retry me")

    assert result.terminal_status == "ok"
    assert result.final_text == "recovered"
    assert backend.calls == 2


async def test_persistent_rate_limit_degrades() -> None:
    backend = _ScriptedBackend([BackendRateLimited(backend_name="scripted")])
    loop = _loop(backend, _empty_adapter(), _settings())

    result = await loop.run("always limited")

    assert result.terminal_status == "degraded_rate_limited"


async def test_unavailable_no_result_fails() -> None:
    backend = _ScriptedBackend([BackendUnavailable("down", backend_name="scripted")])
    loop = _loop(backend, _empty_adapter(), _settings())

    result = await loop.run("down")

    assert result.terminal_status == "failed_api_unavailable"
    assert result.tool_invocations == []


async def test_unavailable_with_prior_result_degrades_no_planner() -> None:
    spec = make_spec(name="t", handler=ok_handler)
    backend = _ScriptedBackend(
        [
            _msg(
                content=[_tool_use(block_id="toolu_1", name="t", tool_input={})],
                stop_reason="tool_use",
            ),
            BackendUnavailable("down", backend_name="scripted"),
        ]
    )
    loop = _loop(backend, _adapter_with(spec), _settings())

    result = await loop.run("first ok then down")

    assert result.terminal_status == "degraded_no_planner"
    assert len(result.tool_invocations) == 1


# ---------------------------------------------------------------------------
# 6.7 non-retryable backend errors propagate
# ---------------------------------------------------------------------------


async def test_capability_violation_propagates() -> None:
    backend = _ScriptedBackend(
        [
            BackendCapabilityViolation(
                backend_name="scripted",
                capability="prompt_caching",
                attempted_feature="cache_control_in_system_block",
            )
        ]
    )
    loop = _loop(backend, _empty_adapter(), _settings())

    with pytest.raises(BackendCapabilityViolation):
        await loop.run("violate")


async def test_auth_invalid_backend_error_propagates() -> None:
    backend = _ScriptedBackend([BackendError("nope", backend_name="scripted", kind="auth_invalid")])
    loop = _loop(backend, _empty_adapter(), _settings())

    with pytest.raises(BackendError):
        await loop.run("auth fail")


# ---------------------------------------------------------------------------
# 6.8 stop_reason exhaustion (D-8)
# ---------------------------------------------------------------------------


async def test_empty_end_turn_is_empty_response() -> None:
    backend = FakeBackend(responses=[_msg(content=[], stop_reason="end_turn")])
    loop = _loop(backend, _empty_adapter(), _settings())

    result = await loop.run("empty")

    assert result.terminal_status == "empty_response"


async def test_refusal_is_empty_response() -> None:
    backend = FakeBackend(responses=[_msg(content=[_text("no")], stop_reason="refusal")])
    loop = _loop(backend, _empty_adapter(), _settings())

    result = await loop.run("refuse")

    assert result.terminal_status == "empty_response"


async def test_max_tokens_is_degraded_token_budget() -> None:
    backend = FakeBackend(responses=[_msg(content=[_text("trunc")], stop_reason="max_tokens")])
    loop = _loop(backend, _empty_adapter(), _settings())

    result = await loop.run("truncated")

    assert result.terminal_status == "degraded_token_budget"


async def test_max_tokens_stop_preserves_partial_text() -> None:
    backend = FakeBackend(responses=[_msg(content=[_text("partial")], stop_reason="max_tokens")])
    loop = _loop(backend, _empty_adapter(), _settings())

    result = await loop.run("truncated partial")

    assert result.terminal_status == "degraded_token_budget"
    assert result.final_text == "partial"


async def test_stop_sequence_raises_unexpected_stop_reason() -> None:
    backend = FakeBackend(responses=[_msg(content=[_text("x")], stop_reason="stop_sequence")])
    loop = _loop(backend, _empty_adapter(), _settings())

    with pytest.raises(UnexpectedStopReason) as ei:
        await loop.run("stop seq")
    assert ei.value.stop_reason == "stop_sequence"


# ---------------------------------------------------------------------------
# 6.9 construction
# ---------------------------------------------------------------------------


async def test_missing_agent_settings_raises_config_error() -> None:
    backend = FakeBackend(responses=[])
    with pytest.raises(ConfigError):
        AgentLoop(cast(LLMBackend, backend), _empty_adapter(), Settings())


async def test_agent_settings_present_constructs() -> None:
    backend = FakeBackend(responses=[])
    loop = _loop(backend, _empty_adapter(), _settings())
    assert isinstance(loop, AgentLoop)


# ---------------------------------------------------------------------------
# 6.10 LoopResult schema
# ---------------------------------------------------------------------------


def test_loop_result_rejects_out_of_set_terminal_status() -> None:
    with pytest.raises(ValidationError):
        LoopResult(
            final_text="",
            tool_invocations=[],
            turns=1,
            terminal_status="bogus",  # type: ignore[arg-type]
            usage_totals=LoopUsage(),
            stop_reason=None,
        )


async def test_usage_totals_accumulate_across_turns() -> None:
    spec = make_spec(name="t", handler=ok_handler)
    backend = _ScriptedBackend(
        [
            _msg(
                content=[_tool_use(block_id="toolu_1", name="t", tool_input={})],
                stop_reason="tool_use",
                input_tokens=10,
                output_tokens=5,
            ),
            _msg(
                content=[_text("done")],
                stop_reason="end_turn",
                input_tokens=20,
                output_tokens=7,
            ),
        ]
    )
    loop = _loop(backend, _adapter_with(spec), _settings())

    result = await loop.run("accumulate")

    assert result.usage_totals.input_tokens == 30
    assert result.usage_totals.output_tokens == 12


def test_tool_invocation_requires_exactly_one_outcome() -> None:
    with pytest.raises(ValidationError):
        ToolInvocation(tool_name="t", tool_use_id="x", input={})
    with pytest.raises(ValidationError):
        ToolInvocation(
            tool_name="t",
            tool_use_id="x",
            input={},
            output={"a": 1},
            error={"b": 2},
        )


# ---------------------------------------------------------------------------
# 6.11 inbound-thinking relay structure (tolerate-inbound-thinking, §4 / D-3)
#
# Pillar ③ "multi-turn relay is naturally true" — no production code changes;
# these are structural regression tests that pin the invariants:
#   4.1 cache_control breakpoint count sequence == [1, 2, 2, …] and never lands
#       on a thinking / redacted_thinking block (design.md D-3).
#   4.2 the loop relays a thinking-bearing assistant content verbatim via the
#       SDK ``model_dump()`` form (no exclude_unset / exclude_none) so every
#       field — signature + provider-private extras — round-trips byte-for-byte.
# ---------------------------------------------------------------------------


_THINKING_TYPES = {"thinking", "redacted_thinking"}


class _RecordingBackend:
    """Structural ``LLMBackend`` that records EVERY request (not just the last).

    Unlike ``_ScriptedBackend`` (which keeps only ``last_*``), this captures a
    per-call snapshot of the exact ``system`` / ``messages`` passed to
    ``messages_create`` AFTER the loop's cache_control injection — so a test
    can assert the breakpoint-count sequence and which block types carry
    ``cache_control`` across all turns.
    """

    name = "recording"

    def __init__(
        self,
        responses: list[MessageResponse],
        *,
        capabilities: BackendCapabilities | None = None,
    ) -> None:
        self._responses = responses
        self._idx = 0
        self.requests: list[dict[str, Any]] = []
        self.capabilities = capabilities if capabilities is not None else _DEFAULT_CAPS

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
        # Deep-copy via JSON round-trip so later loop mutation of the shared
        # ``messages`` list cannot rewrite the snapshot we assert against.
        self.requests.append(
            {
                "system": json.loads(json.dumps(system)),
                "messages": json.loads(json.dumps(messages)),
            }
        )
        response = self._responses[min(self._idx, len(self._responses) - 1)]
        self._idx += 1
        return response


def _count_cache_breakpoints(request: dict[str, Any]) -> int:
    """Count ``cache_control`` markers across system + messages of one request."""
    count = 0
    system = request["system"]
    if isinstance(system, list):
        count += sum(1 for block in system if isinstance(block, dict) and "cache_control" in block)
    for message in request["messages"]:
        content = message.get("content")
        if isinstance(content, list):
            count += sum(
                1 for block in content if isinstance(block, dict) and "cache_control" in block
            )
    return count


def _thinking_blocks_carry_no_breakpoint(request: dict[str, Any]) -> bool:
    """Return True iff no thinking / redacted_thinking block carries ``cache_control``."""
    for message in request["messages"]:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") in _THINKING_TYPES
                and "cache_control" in block
            ):
                return False
    return True


async def test_thinking_relay_cache_breakpoint_sequence_never_on_thinking() -> None:
    # turn1: assistant emits [thinking, tool_use] → tool_result → turn2 end_turn.
    # System is a non-empty list so breakpoint A is always present; the rolling
    # breakpoint B lands on the last message's last block, which is a user
    # tool_result on turn2+ (never a thinking block, design.md D-3).
    spec = make_spec(name="probe", handler=ok_handler)
    backend = _RecordingBackend(
        [
            _msg(
                content=[
                    _thinking(thinking="let me check", signature="sig_1"),
                    _tool_use(block_id="toolu_1", name="probe", tool_input={}),
                ],
                stop_reason="tool_use",
            ),
            _msg(
                content=[
                    _thinking(thinking="now I conclude", signature="sig_2"),
                    _text("all good"),
                ],
                stop_reason="end_turn",
            ),
        ]
    )
    loop = _loop(
        backend,
        _adapter_with(spec),
        _settings(),
        system=[{"type": "text", "text": "sys"}],
    )

    result = await loop.run("inspect with thinking")

    assert result.terminal_status == "ok"
    assert result.turns == 2

    # Two requests issued (turn1 tool_use, turn2 end_turn).
    assert len(backend.requests) == 2

    # Breakpoint-count sequence == [1, 2, 2, …]: turn1 has only breakpoint A
    # (system); the turn-1 user message content is the bare intent string with
    # no block to mark. turn2 adds breakpoint B on the last user tool_result
    # block → 2. (design.md D-3)
    counts = [_count_cache_breakpoints(req) for req in backend.requests]
    assert counts == [1, 2]
    assert counts[0] == 1
    assert all(c == 2 for c in counts[1:])

    # Critically: no thinking / redacted_thinking block EVER carries a breakpoint.
    assert all(_thinking_blocks_carry_no_breakpoint(req) for req in backend.requests)

    # Sanity: the turn-2 request really did relay the turn-1 thinking block into
    # the assistant message (so the "never on thinking" assertion is non-vacuous).
    turn2_assistant = next(m for m in backend.requests[1]["messages"] if m["role"] == "assistant")
    assert any(block.get("type") == "thinking" for block in turn2_assistant["content"])


async def test_thinking_block_relayed_verbatim_model_dump() -> None:
    # A thinking block carrying a provider-private extra field must survive the
    # relay byte-for-byte. The loop relays via ``block.model_dump()`` with NO
    # exclude_unset / exclude_none — pin that so a future relay change that adds
    # an exclude kwarg (which would drop unset/None fields and break verbatim
    # fidelity) turns this test red.
    spec = make_spec(name="probe", handler=ok_handler)
    inbound_thinking = _thinking(
        thinking="reasoning text",
        signature="sig_abc",
        provider_private="keep-me",
    )
    backend = _RecordingBackend(
        [
            _msg(
                content=[
                    inbound_thinking,
                    _tool_use(block_id="toolu_1", name="probe", tool_input={}),
                ],
                stop_reason="tool_use",
            ),
            _msg(content=[_text("done")], stop_reason="end_turn"),
        ]
    )
    loop = _loop(
        backend,
        _adapter_with(spec),
        _settings(),
        system=[{"type": "text", "text": "sys"}],
    )

    result = await loop.run("relay verbatim")
    assert result.terminal_status == "ok"

    # The turn-2 assistant message must contain the turn-1 thinking block,
    # first (order preserved) and byte-for-byte equal to the loop's actual
    # relay form: ``model_dump()`` with no exclude kwargs.
    turn2_assistant = next(m for m in backend.requests[1]["messages"] if m["role"] == "assistant")
    relayed_thinking = turn2_assistant["content"][0]
    assert relayed_thinking == inbound_thinking.model_dump()

    # Order: thinking precedes tool_use, as emitted.
    assert turn2_assistant["content"][0]["type"] == "thinking"
    assert turn2_assistant["content"][1]["type"] == "tool_use"

    # Verbatim fidelity, field-by-field (the provider-private extra survives
    # because ThinkingBlock uses extra="allow" and the relay applies no exclude).
    assert relayed_thinking["thinking"] == "reasoning text"
    assert relayed_thinking["signature"] == "sig_abc"
    assert relayed_thinking["provider_private"] == "keep-me"

    # Guard against a future exclude_unset / exclude_none on the relay: such a
    # dump would diverge from the plain model_dump() the assertion above pins.
    assert relayed_thinking == inbound_thinking.model_dump(exclude_unset=False, exclude_none=False)
