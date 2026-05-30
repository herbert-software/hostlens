"""Hand-written Anthropic tool-use Agent loop (M2.2 skeleton).

This is the project's核心展示点 (CLAUDE.md §4.1): a framework-free, readable
multi-turn tool-use loop. No LangChain, no SDK import — every model call goes
through the injected ``LLMBackend.messages_create`` (CLAUDE.md §4.11), every
tool call goes through the injected ``ToolsAdapter.dispatch`` (CLAUDE.md
§4.10). The backend is held privately and deliberately kept out of
``ToolContext`` so a tool handler can never reach back to call the LLM itself
(ADR-008 / CLAUDE.md §7 反模式).

The loop is a generic tool dispatcher: it never special-cases ``run_inspector``
or any specific tool. It returns a generic ``LoopResult`` (design.md D-1), not
a ``Report`` — assembling a ``Report`` is the M2.4 Planner / M2.7 CLI job that
knows the intent semantics.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from hostlens.agent.backend import (
    BackendCapabilities,
    MessageResponse,
    TextBlock,
    ToolUseBlock,
)
from hostlens.agent.events import (
    ModelResponded,
    RunFinalized,
    ToolCompleted,
    ToolStarted,
    TurnStarted,
)
from hostlens.agent.tools_adapter import ToolsAdapter, scrub_exception_message
from hostlens.core.exceptions import (
    BackendRateLimited,
    BackendUnavailable,
    ConfigError,
    UnexpectedStopReason,
)

if TYPE_CHECKING:
    from hostlens.agent.backend import LLMBackend
    from hostlens.agent.events import LoopObserver
    from hostlens.core.config import Settings

__all__ = ["AgentLoop", "LoopResult", "LoopUsage", "ToolInvocation"]


# ---------------------------------------------------------------------------
# Retry / timeout constants — aligned with ARCHITECTURE.md §9 Failure Semantics
# ---------------------------------------------------------------------------

# §9 caps retries at 3 for both rate-limit (429/529) and unavailable
# (5xx / connection timeout) families. The loop is the single retry owner
# (ADR-005); the backend does zero retries.
_MAX_RATE_LIMIT_RETRIES = 3
_MAX_UNAVAILABLE_RETRIES = 3

# Fixed backoff used when ``BackendRateLimited.retry_after_seconds`` is None
# (529 overload events carry no retry-after hint — design.md D-3).
_FIXED_BACKOFF_SECONDS = 1.0

# Exponential backoff schedule for ``BackendUnavailable`` (§9: 1s / 4s / 16s).
# Indexed by retry attempt (0-based); length matches _MAX_UNAVAILABLE_RETRIES.
_UNAVAILABLE_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 4.0, 16.0)

# Per-call ``messages_create`` timeout. Hardcoded in M2.2; promoting it to
# ``AgentSettings`` is deferred to M2.7 if the CLI needs it (design.md
# 待解决问题).
_MESSAGES_CREATE_TIMEOUT = 60.0


# ---------------------------------------------------------------------------
# Output data models
# ---------------------------------------------------------------------------

_TerminalStatus = Literal[
    "ok",
    "degraded_rate_limited",
    "degraded_token_budget",
    "degraded_max_turns",
    "degraded_no_planner",
    "empty_response",
    "failed_api_unavailable",
]


class LoopUsage(BaseModel):
    """Cumulative token usage across all turns of a single ``run()``.

    Cache fields are tracked so M2.5 can assert prompt-cache effectiveness
    (CLAUDE.md §4.8) without re-running the loop.
    """

    model_config = ConfigDict(frozen=True)

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class ToolInvocation(BaseModel):
    """Record of one tool dispatch attempt within a turn.

    Exactly one of ``output`` (success) / ``error`` (any failure path —
    dispatch error envelope, malformed-args, or hallucinated tool name) is
    populated; the ``model_validator`` enforces that invariant so a caller
    reading the record can branch on presence without ambiguity.
    """

    model_config = ConfigDict(frozen=True)

    tool_name: str
    tool_use_id: str
    input: dict[str, Any]
    output: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _exactly_one_outcome(self) -> ToolInvocation:
        if (self.output is None) == (self.error is None):
            raise ValueError("ToolInvocation requires exactly one of output / error to be set")
        return self


class LoopResult(BaseModel):
    """Terminal result of an ``AgentLoop.run()`` (design.md D-1).

    ``terminal_status`` is the M2 string closed-set (design.md D-4); M3
    upgrades it to a typed enum with the same values (value-stable migration).
    ``stop_reason`` is the last response's stop_reason, or ``None`` when the
    loop ended on a budget / retry / max-turns guard before any (or after the
    last) successful response.
    """

    model_config = ConfigDict(frozen=True)

    final_text: str
    tool_invocations: list[ToolInvocation]
    turns: int
    terminal_status: _TerminalStatus
    usage_totals: LoopUsage
    stop_reason: str | None


# ---------------------------------------------------------------------------
# AgentLoop
# ---------------------------------------------------------------------------


class AgentLoop:
    """Multi-turn Anthropic tool-use loop over an injected ``LLMBackend``."""

    def __init__(
        self,
        backend: LLMBackend,
        tool_adapter: ToolsAdapter,
        settings: Settings,
        *,
        system: list[dict[str, Any]] | str | None = None,
    ) -> None:
        # Construct-time validation (design.md D-7): the loop reads
        # settings.agent.* unconditionally, so a None agent block is a hard
        # config error that must surface now, not silently default nor fail
        # lazily at run().
        if settings.agent is None:
            raise ConfigError(
                "agent settings required to construct AgentLoop",
                kind="missing_agent_settings",
            )
        # Backend is private — never exposed via ToolContext (ADR-008).
        self._backend = backend
        self._tool_adapter = tool_adapter
        self._settings = settings
        self._agent = settings.agent
        # system is injected by the caller (M2.4 Planner passes its prompt at
        # construction); the M2.2 skeleton builds no system content. Construct-
        # time injection because the system prompt is fixed for a loop instance
        # (design D-2).
        self._system: list[dict[str, Any]] | str = system if system is not None else []

    async def run(self, intent: str, *, observer: LoopObserver | None = None) -> LoopResult:
        """Drive the tool-use loop from a natural-language ``intent``.

        ``observer`` (default None = no-op) receives typed ``LoopEvent`` values
        at turn / model-response / tool / finalize boundaries. Emits are
        direct calls with no try/except: the loop stays fail-loud and the
        observer owns its own error isolation (design D-2). ``observer`` is a
        per-run local — never stored on self — so ``run`` stays re-entrant.
        """
        messages: list[dict[str, Any]] = [{"role": "user", "content": intent}]
        tools = self._tool_adapter.list_for_agent()
        advertised_names = {tool["name"] for tool in tools}

        tool_invocations: list[ToolInvocation] = []
        usage = LoopUsage()
        turns = 0
        last_stop_reason: str | None = None

        while True:
            # Pre-flight guards (design.md D-6): never burn a turn we already
            # know is over budget / over the turn cap.
            if usage.input_tokens >= self._agent.token_budget_input or (
                usage.output_tokens >= self._agent.token_budget_output
            ):
                return self._finalize(
                    "degraded_token_budget",
                    tool_invocations,
                    turns,
                    usage,
                    last_stop_reason,
                    observer=observer,
                )
            if turns >= self._agent.max_turns:
                return self._finalize(
                    "degraded_max_turns",
                    tool_invocations,
                    turns,
                    usage,
                    last_stop_reason,
                    observer=observer,
                )

            # Per-run output budget shrinks max_tokens each turn so total output
            # <= budget; passing full-budget-per-call would let a run overshoot
            # ~2x. The guard above ensures usage.output_tokens < budget, so
            # remaining >= 1 (max(1, ...) is belt-and-suspenders).
            remaining_output = max(1, self._agent.token_budget_output - usage.output_tokens)
            if observer is not None:
                observer.on_event(TurnStarted(turn=turns + 1))
            outcome = await self._call_with_retry(
                system=self._inject_cache_control(self._system, self._backend.capabilities),
                messages=self._roll_message_cache_breakpoint(messages, self._backend.capabilities),
                tools=tools,
                max_tokens=remaining_output,
            )
            if isinstance(outcome, str):
                # Retry budget exhausted → degraded terminal status. For the
                # unavailable family the status depends on whether any tool
                # already produced a result (design.md D-3 / §9).
                if outcome == "failed_api_unavailable" and tool_invocations:
                    terminal: _TerminalStatus = "degraded_no_planner"
                else:
                    terminal = outcome
                return self._finalize(
                    terminal,
                    tool_invocations,
                    turns,
                    usage,
                    last_stop_reason,
                    observer=observer,
                )

            response = outcome
            turns += 1
            usage = self._accumulate_usage(usage, response)
            last_stop_reason = response.stop_reason
            if observer is not None:
                observer.on_event(
                    ModelResponded(
                        turn=turns,
                        stop_reason=response.stop_reason,
                        text=self._join_text(response),
                    )
                )

            if response.stop_reason == "tool_use":
                assistant_content, tool_results, new_invocations = await self._run_tool_turn(
                    response, advertised_names, observer, turns
                )
                tool_invocations.extend(new_invocations)
                messages.append({"role": "assistant", "content": assistant_content})
                messages.append({"role": "user", "content": tool_results})
                continue

            if response.stop_reason == "end_turn":
                final_text = self._join_text(response)
                status: _TerminalStatus = "ok" if response.content else "empty_response"
                return self._finalize(
                    status,
                    tool_invocations,
                    turns,
                    usage,
                    last_stop_reason,
                    final_text,
                    observer=observer,
                )

            if response.stop_reason == "refusal":
                return self._finalize(
                    "empty_response",
                    tool_invocations,
                    turns,
                    usage,
                    last_stop_reason,
                    observer=observer,
                )

            if response.stop_reason == "max_tokens":
                return self._finalize(
                    "degraded_token_budget",
                    tool_invocations,
                    turns,
                    usage,
                    last_stop_reason,
                    self._join_text(response),
                    observer=observer,
                )

            # stop_sequence / pause_turn — Hostlens solicits neither (D-8).
            raise UnexpectedStopReason(response.stop_reason)

    # -- model call + retry ------------------------------------------------

    async def _call_with_retry(
        self,
        *,
        system: list[dict[str, Any]] | str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
    ) -> MessageResponse | _TerminalStatus:
        """Call ``messages_create`` honoring the §9 per-family retry policy.

        Returns the ``MessageResponse`` on success, or a terminal-status
        string when a retryable family exhausts its budget. Non-retryable
        backend errors (``BackendError(kind=...)`` / ``BackendCapabilityViolation``)
        propagate unwrapped — config errors and loop bugs must surface
        immediately (design.md D-3).
        """
        rate_limit_attempts = 0
        unavailable_attempts = 0

        while True:
            try:
                return await self._backend.messages_create(
                    model=self._agent.primary_model,
                    system=system,
                    messages=messages,
                    tools=tools,
                    max_tokens=max_tokens,
                    timeout=_MESSAGES_CREATE_TIMEOUT,
                )
            except BackendRateLimited as exc:
                if rate_limit_attempts >= _MAX_RATE_LIMIT_RETRIES:
                    return "degraded_rate_limited"
                delay = (
                    exc.retry_after_seconds
                    if exc.retry_after_seconds is not None
                    else _FIXED_BACKOFF_SECONDS
                )
                rate_limit_attempts += 1
                await asyncio.sleep(delay)
            except BackendUnavailable:
                if unavailable_attempts >= _MAX_UNAVAILABLE_RETRIES:
                    # Caller maps this to degraded_no_planner when results exist.
                    return "failed_api_unavailable"
                await asyncio.sleep(_UNAVAILABLE_BACKOFF_SECONDS[unavailable_attempts])
                unavailable_attempts += 1

    # -- tool turn ---------------------------------------------------------

    async def _run_tool_turn(
        self,
        response: MessageResponse,
        advertised_names: set[str],
        observer: LoopObserver | None,
        turn: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[ToolInvocation]]:
        """Dispatch every ``tool_use`` block in ``response`` in parallel.

        Returns ``(assistant_content, tool_result_blocks, invocations)``.
        ``gather`` is used WITHOUT ``return_exceptions`` (design.md D-5/4.6):
        the per-block paths that the model can self-correct (hallucinated
        name / malformed args / handler envelope) never raise; fail-loud
        paths (``KeyError`` from a registered handler, ``ToolPolicyViolation``,
        ``ToolError`` from an output-schema mismatch, ``CancelledError``) must
        abort the whole turn, cancel any sibling tasks, and propagate.
        """
        tool_use_blocks = [b for b in response.content if isinstance(b, ToolUseBlock)]
        tasks = [
            asyncio.create_task(self._dispatch_one(block, advertised_names, observer, turn))
            for block in tool_use_blocks
        ]
        try:
            per_block = await asyncio.gather(*tasks)
        except BaseException:
            # asyncio.gather defaults to propagating only the first exception
            # without cancelling siblings. Explicitly cancel and drain the
            # unfinished parallel tasks to keep an orphaned long-running handler
            # (SSH / inspector collection) from leaking resources, then re-raise
            # verbatim to preserve the fail-loud exception type (TaskGroup is
            # avoided because it would wrap into an ExceptionGroup).
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        tool_results = [result for result, _ in per_block]
        invocations = [inv for _, inv in per_block]
        # Pass the model's content verbatim back as the assistant message so
        # the next turn sees the exact tool_use blocks it emitted.
        assistant_content = [block.model_dump() for block in response.content]
        return assistant_content, tool_results, invocations

    async def _dispatch_one(
        self,
        block: ToolUseBlock,
        advertised_names: set[str],
        observer: LoopObserver | None,
        turn: int,
    ) -> tuple[dict[str, Any], ToolInvocation]:
        """Dispatch a single ``tool_use`` block per the §4 error-routing table.

        Returns the Anthropic ``tool_result`` block paired with its
        ``ToolInvocation`` record. Fail-loud exceptions are not caught here —
        they propagate out of ``gather`` (design.md D-5 paths 4-6), so their
        ``ToolStarted`` has no paired ``ToolCompleted`` (design D-3).
        """
        # ToolStarted is emitted at the entry — before any branch check (incl.
        # the hallucinated-name interception) — so every block, even fail-loud
        # ones, appears in the event stream (design D-3).
        if observer is not None:
            observer.on_event(
                ToolStarted(
                    turn=turn,
                    tool_name=block.name,
                    tool_use_id=block.id,
                    # Detached copy: the frozen event must be a true value object,
                    # so an observer can never mutate the dict dispatched to the
                    # tool right after (keeps observation side-effect-free).
                    tool_input=dict(block.input),
                )
            )

        # Path: hallucinated tool name. Intercepted by membership check BEFORE
        # dispatch so the dispatch ``KeyError`` (which is ambiguous between a
        # missing-name lookup and a handler-internal KeyError) cannot mask a
        # real handler bug (design.md D-5 关键前置).
        if block.name not in advertised_names:
            envelope = {
                "is_error": True,
                "error_kind": "UnknownTool",
                "tool_name": block.name,
                "message": f"no such tool: {block.name}",
            }
            invocation = ToolInvocation(
                tool_name=block.name,
                tool_use_id=block.id,
                input=block.input,
                error=envelope,
            )
            if observer is not None:
                observer.on_event(ToolCompleted(turn=turn, invocation=invocation))
            return self._tool_result_block(block.id, envelope, is_error=True), invocation

        try:
            result = await self._tool_adapter.dispatch(block.name, block.input)
        except TypeError as exc:
            # Malformed tool args (input failed the spec's schema). The model
            # can self-correct — feed the scrubbed error back. This is the
            # ONLY path where the loop scrubs itself (dispatch already scrubs
            # its envelopes).
            envelope = {
                "is_error": True,
                "error_kind": "TypeError",
                "tool_name": block.name,
                "message": scrub_exception_message(str(exc)),
            }
            invocation = ToolInvocation(
                tool_name=block.name,
                tool_use_id=block.id,
                input=block.input,
                error=envelope,
            )
            if observer is not None:
                observer.on_event(ToolCompleted(turn=turn, invocation=invocation))
            return self._tool_result_block(block.id, envelope, is_error=True), invocation

        # dispatch returned a dict. Distinguish a scrubbed error envelope from
        # a normal model_dump() by the full envelope signature, NOT a bare
        # ``is_error`` truthiness — a business output_schema could legitimately
        # carry an ``is_error`` field (design.md D-5 路径 2).
        if self._is_error_envelope(result):
            invocation = ToolInvocation(
                tool_name=block.name,
                tool_use_id=block.id,
                input=block.input,
                error=result,
            )
            if observer is not None:
                observer.on_event(ToolCompleted(turn=turn, invocation=invocation))
            return self._tool_result_block(block.id, result, is_error=True), invocation

        invocation = ToolInvocation(
            tool_name=block.name,
            tool_use_id=block.id,
            input=block.input,
            output=result,
        )
        if observer is not None:
            observer.on_event(ToolCompleted(turn=turn, invocation=invocation))
        return self._tool_result_block(block.id, result, is_error=False), invocation

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _inject_cache_control(
        system: list[dict[str, Any]] | str,
        capabilities: BackendCapabilities,
    ) -> list[dict[str, Any]] | str:
        """Inject breakpoint A (static prefix) when capability allows it.

        The decision lives in the loop (CLAUDE.md §4.8 / §4.11 rule #2): the
        backend must never silently drop a ``cache_control`` block, so the
        loop must not emit one when ``prompt_caching`` is False.

        WHAT this breakpoint caches (design.md D-1): the Anthropic prompt-cache
        prefix order is ``tools → system → messages``, so a single breakpoint on
        the last ``system`` block caches the whole ``tools + system`` static
        prefix — the ``system`` breakpoint naturally swallows the ``tools`` array
        that precedes it. Hostlens renders both ``tools`` and ``system`` byte-
        stable across runs (M2.4), making ``tools + system`` one contiguous
        stable prefix that a single breakpoint fully covers.

        The ``tools`` array is therefore **deliberately not** marked separately:
        breakpoint A already covers it. A second breakpoint on ``tools`` would
        only create a redundant ``tools``-alone cache entry, burning one of the
        ≤4 breakpoint budget slots and an extra cache-write for no gain (that
        split only helps when ``tools`` is stable but ``system`` varies, which
        is not Hostlens's case).

        ``system`` as a bare string or empty list has nowhere to mark → skip A
        (no error); breakpoint B on the rolling messages may still apply.
        """
        if not capabilities.prompt_caching:
            return system
        if not isinstance(system, list) or not system:
            return system
        marked = list(system)
        last = dict(marked[-1])
        last["cache_control"] = {"type": "ephemeral"}
        marked[-1] = last
        return marked

    @staticmethod
    def _roll_message_cache_breakpoint(
        messages: list[dict[str, Any]],
        capabilities: BackendCapabilities,
    ) -> list[dict[str, Any]]:
        """Inject the rolling conversation-prefix breakpoint B (design.md D-2/D-3).

        Each ``messages_create`` marks **only** the last content block of the
        last message, so the request carries at most one breakpoint B regardless
        of turn count. Combined with the API's longest-prefix match (with ~20
        block look-back), marking only the newest tail still lets turn N hit the
        shorter conversation prefix written on turn N-1 — no need to retain
        historical breakpoints.

        Snapshot invariant (WHY we never mutate stored ``messages``): the loop
        appends to and re-uses the ``messages`` list across turns. Writing the
        mark back into a stored dict would dirty the conversation state between
        turns and let historical breakpoints accumulate (request breakpoint
        count would grow past the API's limit of 4). So injection operates on a
        shallow copy and only shallow-copies the marked tail message + its tail
        block — stored ``messages`` and every dict inside stay untouched.

        Because the stored ``messages`` are therefore guaranteed never to carry
        ``cache_control``, there is no historical breakpoint to clear: a "clear
        all previous breakpoints" pass would be defending an impossible branch
        (CLAUDE.md §6), so it is deliberately omitted. The ``[1, 2, 2, …]``
        breakpoint-count structural test guards this invariant.

        Degrade (D-2): when the last message ``content`` is not a non-empty
        block list (e.g. a bare ``str`` such as the turn-1 ``intent``), there is
        no block to mark → skip B (no coercion to list, to avoid changing wire
        semantics); breakpoint A still applies.
        """
        # The three skip paths return the original ``messages`` reference
        # (zero-copy): they emit no cache_control, and the caller (run) treats
        # the result as a read-only request snapshot — safe under the loop's
        # sequential, single-coroutine call structure.
        if not capabilities.prompt_caching:
            return messages
        if not messages:
            return messages
        last_content = messages[-1].get("content")
        if not isinstance(last_content, list) or not last_content:
            return messages
        rolled = list(messages)
        last_msg = dict(rolled[-1])
        content = list(last_content)
        last_block = dict(content[-1])
        last_block["cache_control"] = {"type": "ephemeral"}
        content[-1] = last_block
        last_msg["content"] = content
        rolled[-1] = last_msg
        return rolled

    @staticmethod
    def _is_error_envelope(result: dict[str, Any]) -> bool:
        return result.get("is_error") is True and "error_kind" in result and "message" in result

    @staticmethod
    def _tool_result_block(
        tool_use_id: str,
        content: dict[str, Any],
        *,
        is_error: bool,
    ) -> dict[str, Any]:
        # Anthropic tool_result.content accepts only a string or a list of
        # content blocks — a bare dict is invalid on the wire. The structured
        # dict is preserved in ToolInvocation.output/error; here it is carried
        # as JSON text for SDK-valid transport.
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": json.dumps(content, ensure_ascii=False),
        }
        if is_error:
            block["is_error"] = True
        return block

    @staticmethod
    def _join_text(response: MessageResponse) -> str:
        return "".join(b.text for b in response.content if isinstance(b, TextBlock))

    @staticmethod
    def _accumulate_usage(usage: LoopUsage, response: MessageResponse) -> LoopUsage:
        u = response.usage
        return LoopUsage(
            input_tokens=usage.input_tokens + u.input_tokens,
            output_tokens=usage.output_tokens + u.output_tokens,
            cache_creation_input_tokens=(
                usage.cache_creation_input_tokens + u.cache_creation_input_tokens
            ),
            cache_read_input_tokens=usage.cache_read_input_tokens + u.cache_read_input_tokens,
        )

    @staticmethod
    def _finalize(
        terminal_status: _TerminalStatus,
        tool_invocations: list[ToolInvocation],
        turns: int,
        usage: LoopUsage,
        stop_reason: str | None,
        final_text: str = "",
        *,
        observer: LoopObserver | None = None,
    ) -> LoopResult:
        if observer is not None:
            observer.on_event(RunFinalized(terminal_status=terminal_status, turns=turns))
        return LoopResult(
            final_text=final_text,
            tool_invocations=tool_invocations,
            turns=turns,
            terminal_status=terminal_status,
            usage_totals=usage,
            stop_reason=stop_reason,
        )
