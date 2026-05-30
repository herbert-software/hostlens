"""Prompt-cache strategy tests for ``AgentLoop`` (M2.5, change add-prompt-cache-strategy).

Two-layer breakpoint strategy (spec §需求 + design D-1/D-2/D-3):

- Breakpoint A — static prefix: the last ``system`` block (caches ``tools + system``;
  ``tools`` is deliberately never marked separately, design D-1).
- Breakpoint B — rolling conversation prefix: the last content block of the last
  ``messages`` entry, re-stamped each turn on a request snapshot only.

Validation is split per design D-5:

- **CI structural** (this file, non-live): a recording backend captures a *copy* of
  every ``messages_create`` request (``system`` / ``messages`` / ``tools``) and we
  assert breakpoint placement / count purely on request shape. We deliberately do
  NOT inspect any recorded ``cache_read`` value as acceptance evidence — that would
  be a fixture self-certification trap (design D-5 反自证).
- **live** (``@pytest.mark.live``, opt-in): a real Anthropic API run asserts the
  *aggregate* ``cache_read_input_tokens > 0`` on turn 2 / turn 3. See the live
  test's docstring for its deliberately-narrow coverage claim (3.1b).

The recording backend is local to this file so the production ``FakeBackend`` is
not polluted with per-call request capture (design D-5).
"""

from __future__ import annotations

import os
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
from hostlens.agent.loop import AgentLoop
from hostlens.agent.tools_adapter import ToolsAdapter
from hostlens.core.config import AgentSettings, Settings
from hostlens.tools.registry import ToolRegistry

from ._helpers import ctx_factory, make_spec, ok_handler

# ---------------------------------------------------------------------------
# Capability sets
# ---------------------------------------------------------------------------

_CACHING_ON = BackendCapabilities(
    prompt_caching=True,
    tool_use=True,
    structured_output=True,
    parallel_tool_use=True,
    extended_thinking=False,
    vision=True,
    streaming=False,
)

_CACHING_OFF = BackendCapabilities(
    prompt_caching=False,
    tool_use=True,
    structured_output=True,
    parallel_tool_use=True,
    extended_thinking=False,
    vision=True,
    streaming=False,
)


# ---------------------------------------------------------------------------
# Builders (mirror tests/agent/test_loop.py)
# ---------------------------------------------------------------------------


def _settings() -> Settings:
    return Settings(agent=AgentSettings())


def _msg(*, content: list[Any], stop_reason: str) -> MessageResponse:
    return MessageResponse(
        id="msg_x",
        model="claude-test",
        role="assistant",
        content=content,
        stop_reason=stop_reason,  # type: ignore[arg-type]
        usage=Usage(input_tokens=1, output_tokens=1),
    )


def _text(text: str) -> TextBlock:
    return TextBlock(type="text", text=text)


def _tool_use(*, block_id: str, name: str) -> ToolUseBlock:
    return ToolUseBlock(type="tool_use", id=block_id, name=name, input={})


class _RecordingBackend:
    """``LLMBackend`` that replays a scripted event list and records EVERY call.

    Unlike ``test_loop.py``'s ``_ScriptedBackend`` (which keeps only the *last*
    request), this captures a deep-enough copy of the ``system`` / ``messages`` /
    ``tools`` snapshot of every ``messages_create`` into ``requests`` so per-turn
    breakpoint placement can be asserted across the whole run. The copies are
    independent of the loop's stored ``messages`` list, which the loop mutates by
    ``append`` between turns.

    Event consumption mirrors ``_ScriptedBackend``: events are consumed in order;
    past the end the last event repeats (lets a single trailing ``tool_use``
    drive ``run`` to its turn / budget guard).
    """

    name = "recording"

    def __init__(
        self,
        events: list[MessageResponse],
        *,
        capabilities: BackendCapabilities,
    ) -> None:
        self._events = events
        self._idx = 0
        self.capabilities = capabilities
        self.requests: list[dict[str, Any]] = []

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
        self.requests.append(
            {
                "system": _deep_copy_json(system),
                "messages": _deep_copy_json(messages),
                "tools": _deep_copy_json(tools),
            }
        )
        event = self._events[min(self._idx, len(self._events) - 1)]
        self._idx += 1
        return event


def _deep_copy_json(value: Any) -> Any:
    """Structure-only deep copy of the JSON-ish request parts.

    The request pieces are plain dict/list/str/scalar trees; a recursive copy
    is enough to detach the snapshot from later loop mutation without pulling in
    ``copy.deepcopy``'s general-object machinery.
    """
    if isinstance(value, dict):
        return {k: _deep_copy_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_copy_json(v) for v in value]
    return value


def _loop(
    backend: _RecordingBackend,
    adapter: ToolsAdapter,
    *,
    system: list[dict[str, Any]] | str | None,
) -> AgentLoop:
    return AgentLoop(cast(LLMBackend, backend), adapter, _settings(), system=system)


def _adapter_with(*specs: Any) -> ToolsAdapter:
    reg = ToolRegistry()
    for spec in specs:
        reg.register(spec)
    return ToolsAdapter(reg, ctx_factory())


# Breakpoint counting -------------------------------------------------------


def _has_cc(block: Any) -> bool:
    return isinstance(block, dict) and "cache_control" in block


def _count_breakpoints(request: dict[str, Any]) -> int:
    """Total ``cache_control`` markers across system + tools + every message block."""
    total = 0
    system = request["system"]
    if isinstance(system, list):
        total += sum(1 for block in system if _has_cc(block))
    for tool in request["tools"]:
        if _has_cc(tool):
            total += 1
    for message in request["messages"]:
        content = message.get("content")
        if isinstance(content, list):
            total += sum(1 for block in content if _has_cc(block))
    return total


# A scripted run that drives N+1 ``messages_create`` calls: N tool_use turns
# (each calls the registered ``probe`` tool) then one terminal end_turn. The
# trailing end_turn caps the loop so ``run`` returns cleanly.
def _tool_then_end_events(*, tool_turns: int) -> list[MessageResponse]:
    events: list[MessageResponse] = [
        _msg(content=[_tool_use(block_id=f"toolu_{i}", name="probe")], stop_reason="tool_use")
        for i in range(tool_turns)
    ]
    events.append(_msg(content=[_text("done")], stop_reason="end_turn"))
    return events


_SYSTEM_BLOCK: list[dict[str, Any]] = [{"type": "text", "text": "you inspect hosts"}]


# ---------------------------------------------------------------------------
# 2.2 (a) turn 1 — breakpoint A on system tail, tools unmarked, NO B (bare str)
# ---------------------------------------------------------------------------


async def test_prompt_cache_injection_structure_turn1() -> None:
    spec = make_spec(name="probe", handler=ok_handler)
    backend = _RecordingBackend(_tool_then_end_events(tool_turns=1), capabilities=_CACHING_ON)
    loop = _loop(backend, _adapter_with(spec), system=_SYSTEM_BLOCK)

    await loop.run("inspect host")

    turn1 = backend.requests[0]
    # Breakpoint A: system tail block marked.
    system = turn1["system"]
    assert isinstance(system, list)
    assert system[-1]["cache_control"] == {"type": "ephemeral"}
    # tools array: NO element carries cache_control (design D-1).
    assert turn1["tools"], "probe tool must be advertised so the tools array is non-empty"
    assert all(not _has_cc(tool) for tool in turn1["tools"])
    # The only message is the bare-string intent → breakpoint B is skipped.
    assert turn1["messages"][-1]["content"] == "inspect host"
    assert _count_breakpoints(turn1) == 1


# ---------------------------------------------------------------------------
# 2.2 (b) turn 2+ — rolling breakpoint B only on newest message tail
# ---------------------------------------------------------------------------


async def test_prompt_cache_injection_structure_turn2_rolling_b() -> None:
    spec = make_spec(name="probe", handler=ok_handler)
    backend = _RecordingBackend(_tool_then_end_events(tool_turns=2), capabilities=_CACHING_ON)
    loop = _loop(backend, _adapter_with(spec), system=_SYSTEM_BLOCK)

    await loop.run("inspect host")

    # turn2 request: messages = [user(intent str), assistant(tool_use), user(tool_result list)].
    turn2 = backend.requests[1]
    msgs = turn2["messages"]
    assert len(msgs) >= 3

    # Breakpoint A still present on the system tail.
    assert turn2["system"][-1]["cache_control"] == {"type": "ephemeral"}
    # tools still unmarked.
    assert all(not _has_cc(tool) for tool in turn2["tools"])

    # Breakpoint B: ONLY the last message's last content block carries it.
    last_msg = msgs[-1]
    assert isinstance(last_msg["content"], list)
    assert last_msg["content"][-1]["cache_control"] == {"type": "ephemeral"}

    # Every OTHER message block is clean (no historical breakpoints).
    for message in msgs[:-1]:
        content = message.get("content")
        if isinstance(content, list):
            assert all(not _has_cc(block) for block in content)
    # And within the last message, only its tail block is marked.
    assert all(not _has_cc(block) for block in last_msg["content"][:-1])

    # Exactly two breakpoints total (A + B).
    assert _count_breakpoints(turn2) == 2


# ---------------------------------------------------------------------------
# 2.3 breakpoint budget: count sequence [1, 2, 2, 2, 2, ...] over >= 5 turns
# ---------------------------------------------------------------------------


async def test_breakpoint_count_sequence_never_grows() -> None:
    spec = make_spec(name="probe", handler=ok_handler)
    # 6 tool_use turns then end_turn would be 7 calls; max_turns guard caps the
    # loop at AgentSettings.max_turns=20, so 6 tool turns all fire. We assert on
    # the first >= 5 requests.
    backend = _RecordingBackend(_tool_then_end_events(tool_turns=6), capabilities=_CACHING_ON)
    loop = _loop(backend, _adapter_with(spec), system=_SYSTEM_BLOCK)

    await loop.run("inspect host")

    assert len(backend.requests) >= 5
    counts = [_count_breakpoints(req) for req in backend.requests]
    # turn1 末 message 为裸 str intent → B 跳过 = 1; 后续轮 = 2.
    assert counts[0] == 1
    assert all(c == 2 for c in counts[1:])
    # Never exceeds 2 and never grows with turn count.
    assert max(counts) <= 2


# ---------------------------------------------------------------------------
# 2.4 negative: prompt_caching=False → zero cache_control in all three sites
# ---------------------------------------------------------------------------


async def test_no_injection_when_caching_disabled() -> None:
    spec = make_spec(name="probe", handler=ok_handler)
    backend = _RecordingBackend(_tool_then_end_events(tool_turns=2), capabilities=_CACHING_OFF)
    loop = _loop(backend, _adapter_with(spec), system=_SYSTEM_BLOCK)

    # Loop emits zero cache_control → no BackendCapabilityViolation path taken.
    await loop.run("inspect host")

    assert backend.requests, "expected at least one request"
    for req in backend.requests:
        system = req["system"]
        assert isinstance(system, list)
        assert all(not _has_cc(block) for block in system)
        assert all(not _has_cc(tool) for tool in req["tools"])
        for message in req["messages"]:
            content = message.get("content")
            if isinstance(content, list):
                assert all(not _has_cc(block) for block in content)
        assert _count_breakpoints(req) == 0


# ---------------------------------------------------------------------------
# 2.5 degrade: last message content a bare str → skip B, keep A
# ---------------------------------------------------------------------------


def test_bare_string_tail_message_skips_breakpoint_b_keeps_a() -> None:
    # Direct unit assertion on the two pure injection helpers, exercising the
    # exact degrade branch (design D-2): bare-string tail → no block to mark.
    bare_messages: list[dict[str, Any]] = [{"role": "user", "content": "inspect host"}]
    rolled = AgentLoop._roll_message_cache_breakpoint(bare_messages, _CACHING_ON)
    # Breakpoint B skipped: content stays a bare str, no cache_control anywhere.
    assert rolled[-1]["content"] == "inspect host"
    assert all(
        not (isinstance(m.get("content"), list) and any(_has_cc(b) for b in m["content"]))
        for m in rolled
    )

    # Breakpoint A still applies to the static prefix.
    injected = AgentLoop._inject_cache_control(_SYSTEM_BLOCK, _CACHING_ON)
    assert isinstance(injected, list)
    assert injected[-1]["cache_control"] == {"type": "ephemeral"}

    # Symmetric degrade for A: a bare-string ``system`` has no block to mark, so
    # A is skipped without error (spec 场景「断点A在system末块」末句 covers this).
    assert AgentLoop._inject_cache_control("bare system", _CACHING_ON) == "bare system"

    # And the loop helper does not mutate the caller's stored list.
    assert "cache_control" not in bare_messages[0]
    assert "cache_control" not in _SYSTEM_BLOCK[0]


async def test_bare_string_tail_message_skips_b_via_run() -> None:
    # Same degrade scenario but driven through run(): turn1's tail message is the
    # bare-str intent, so its request carries A only (B skipped).
    spec = make_spec(name="probe", handler=ok_handler)
    backend = _RecordingBackend(_tool_then_end_events(tool_turns=1), capabilities=_CACHING_ON)
    loop = _loop(backend, _adapter_with(spec), system=_SYSTEM_BLOCK)

    await loop.run("inspect host")

    turn1 = backend.requests[0]
    assert turn1["messages"][-1]["content"] == "inspect host"  # bare str, B skipped
    assert turn1["system"][-1]["cache_control"] == {"type": "ephemeral"}  # A present
    assert _count_breakpoints(turn1) == 1


# ---------------------------------------------------------------------------
# 3.1 / 3.1b live: real Anthropic API multi-round static-prefix cache hit
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.asyncio
async def test_static_prefix_cache_read_hits_across_rounds_live() -> None:
    """Real-API ``cache_read_input_tokens > 0`` on turn 2 AND turn 3.

    Coverage claim (task 3.1b — deliberately narrow to avoid a hidden
    self-certification): this live test verifies ONLY (1) the static prefix
    breakpoint A really hits Anthropic's cache from the second request on, and
    (2) that aggregate hit persists across multiple rounds.

    It does NOT attribute the ``cache_read_input_tokens`` figure between
    breakpoint A and breakpoint B: that value is a single aggregate, breakpoint
    B is only *created* on turn 2 and first *read* on turn 3, and its read
    contribution cannot be cleanly separated from A's within the aggregate.
    Breakpoint B's correctness (placement, never-written-back-to-stored-messages,
    the ``[1,2,2,…]`` count sequence) is therefore guaranteed by the CI
    structural assertions above, NOT claimed here.

    Precondition (must hold, else a 0 is a false negative not a real signal):
    the ``tools + system`` static prefix must exceed the model's minimum
    cacheable threshold. We use ``claude-haiku-4-5`` (threshold ≈ 2048 input
    tokens) and explicitly pad a stable ``system`` block past it, rather than
    betting on a real Planner prefix being large enough.

    Rather than relying on the model to deterministically emit a tool_use every
    turn (non-deterministic), we issue three sequential ``messages_create``
    calls in the EXACT request shape ``AgentLoop`` emits — padded ``system`` with
    breakpoint A via ``_inject_cache_control``, and a per-round-growing
    ``messages`` list with the rolling breakpoint B via
    ``_roll_message_cache_breakpoint``. This drives the same static-prefix reuse
    the spec scenarios require without depending on model tool-calling behavior.
    """
    from hostlens.agent.backends.anthropic_api import AnthropicAPIBackend

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        pytest.skip("ANTHROPIC_API_KEY not set; skip live cache-hit test")

    model = "claude-haiku-4-5"
    # Pad the static prefix well past Haiku's ≈2048-token minimum cacheable
    # threshold with a stable, byte-identical filler so the cache key is stable
    # across the three rounds. ~6000 words comfortably clears the threshold.
    filler = ("hostlens inspects servers and reports findings. " * 600).strip()
    system: list[dict[str, Any]] = [{"type": "text", "text": filler}]

    backend = AnthropicAPIBackend(api_key=api_key)

    async def call_round(messages: list[dict[str, Any]]) -> MessageResponse:
        return await backend.messages_create(
            model=model,
            system=AgentLoop._inject_cache_control(system, backend.capabilities),
            messages=AgentLoop._roll_message_cache_breakpoint(messages, backend.capabilities),
            tools=[],
            max_tokens=16,
            timeout=30.0,
        )

    # Round 1 — cold: writes the static prefix (A). Tail message is a block list
    # so B is created here too.
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": "Reply with the word ok."}]}
    ]
    await call_round(messages)

    # Round 2 — static prefix A must now be a cache READ.
    messages.append({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
    messages.append(
        {"role": "user", "content": [{"type": "text", "text": "Reply with the word ok again."}]}
    )
    r2 = await call_round(messages)
    assert r2.usage.cache_read_input_tokens > 0, (
        "turn2 must read the static prefix cache; if 0, the prefix is below the "
        "model threshold or breakpoint A is misplaced"
    )

    # Round 3 — aggregate cache hit must persist.
    messages.append({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
    messages.append(
        {
            "role": "user",
            "content": [{"type": "text", "text": "Reply with the word ok one more time."}],
        }
    )
    r3 = await call_round(messages)
    assert r3.usage.cache_read_input_tokens > 0, (
        "turn3 aggregate cache hit must persist across rounds"
    )
