"""Agent surface adapter — projects ToolSpec into Anthropic `tool_use` schema.

Layer 2 of the double-layer capability model (CLAUDE.md §4.10). This module
is the **only** sanctioned bridge between a host-agnostic `ToolSpec` (Layer
1) and the Anthropic Messages API `tool_use` protocol:

- `list_for_agent()` projects all `surfaces ∋ "agent"` specs into a list of
  Anthropic-compatible `{name, description, input_schema}` dicts.
- `dispatch(name, args_json, ctx)` performs the untrusted-dict → typed
  Pydantic model boundary check, then runs the policy gate (surface /
  side_effects / requires_approval), invokes the handler with optional
  timeout, and wraps any non-policy / non-lookup handler exception into a
  structured `tool_error` envelope (with all string values scrubbed by
  `scrub_exception_message` to prevent secret leakage into the LLM
  context).

`ToolPolicyViolation` and `KeyError` (missing tool name) propagate
unwrapped — those are adapter-self failures that the Agent loop must
decide how to surface.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Any

from hostlens.core.exceptions import ToolError, ToolPolicyViolation
from hostlens.tools.base import ToolContext
from hostlens.tools.registry import ToolRegistry

__all__ = ["ToolsAdapter", "scrub_exception_message"]


# ---------------------------------------------------------------------------
# scrub_exception_message — defensive string-value redaction
# ---------------------------------------------------------------------------
#
# This is intentionally separate from `hostlens.core.logging.redact_sensitive`:
# the logging redactor only scrubs **mapping keys** by name, but here we need
# to clean string **values** (raw `str(exc)` output) that may have arbitrary
# secrets concatenated by upstream code. We use a fixed set of conservative
# regex patterns; over-redaction is preferred to leaking even a single byte
# of credentials into the Agent's tool_use context window.

_SCRUB_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 1. Path substrings: home dirs, .ssh, .aws/credentials, .kube/config
    re.compile(r"/Users/[^/\s]+"),
    re.compile(r"/home/[^/\s]+"),
    re.compile(r"\.ssh/[^\s]+"),
    re.compile(r"\.aws/credentials"),
    re.compile(r"\.kube/config"),
    # 2. IPv4 literals (greedy enough for "1.2.3.4")
    re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}"),
    # 2b. IPv6-ish literals. Two-pattern form mirrors
    # `hostlens.tools.schemas.list_targets._SKIP_IPV6_PATTERN` so that
    # shortened-prefix forms (`::1`, `::ffff:10.0.0.5`) are caught — the
    # earlier single-pattern form required at least one hex char before the
    # first colon and silently missed those cases.
    re.compile(
        r"(?:[0-9a-fA-F]{1,4}:){2,}[0-9a-fA-F:]+"
        r"|::1\b"
        r"|::[0-9a-fA-F]{1,4}\b"
    ),
    # 3. Credential signatures: FOO_KEY=... / Bearer ... / sk-...
    re.compile(r"[A-Za-z]+_(?:KEY|TOKEN|SECRET|PASSWORD)=[^\s]+"),
    re.compile(r"[Bb]earer\s+[\w.\-]+"),
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),
    # 4. Identity key-value pairs (`user=admin`, `username=...`, etc.)
    re.compile(r"(?:user|username|usr|uid|account|login)=[^\s,;]+"),
    # 5. Email / user@host (incl. user@IPv4)
    re.compile(r"[\w.+\-]+@(?:[\w.\-]+|(?:\d{1,3}\.){3}\d{1,3})"),
)


def scrub_exception_message(text: str) -> str:
    """Return `text` with sensitive substrings replaced by `"***"`.

    Patterns covered (each pattern unconditionally replaced):
    paths under `/Users/` / `/home/` / `.ssh/...` / `.aws/credentials` /
    `.kube/config`; IPv4 + IPv6 literals; `FOO_KEY=...` / `Bearer ...` /
    `sk-...` style credentials; `user=admin` / `username=alice` style
    identity assignments; `alice@example.com` / `admin@10.0.0.5` style
    email / user@host patterns.

    This function is intentionally permissive (over-redacts) — false
    positives cost a developer one redacted log line; false negatives can
    leak a credential into the LLM context window forever.
    """
    out = text
    for pattern in _SCRUB_PATTERNS:
        out = pattern.sub("***", out)
    return out


# ---------------------------------------------------------------------------
# ToolsAdapter
# ---------------------------------------------------------------------------


class ToolsAdapter:
    """Agent-surface adapter: ToolSpec → Anthropic `tool_use` schema.

    Construction takes a `ToolRegistry` and a `Callable[[], ToolContext]`
    factory. The factory is invoked **per `dispatch` call** (not once at
    construction) so that mutable per-turn state — most importantly
    `cancel: asyncio.Event` — is never shared across Agent loop turns.

    Construction does NOT validate that the registry is non-empty: tests
    and incremental-assembly callers need to be able to wire an adapter
    against an empty registry. `list_for_agent` returns `[]` in that case;
    `dispatch` will raise `KeyError` when the tool name is missing.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        context_factory: Callable[[], ToolContext],
    ) -> None:
        self._registry = registry
        self._context_factory = context_factory

    def list_for_agent(self) -> list[dict[str, Any]]:
        """Project all `surfaces ∋ "agent"` specs into Anthropic schema dicts.

        Returns a list (already sorted by spec.name via `ToolRegistry.
        list_for`) where each entry is a fresh dict literal built with the
        key order `name → description → input_schema`. We deliberately use
        a dict literal (not `**unpack` or `dict(sorted(...))`) to guarantee
        Python 3.7+ insertion-order semantics — which in turn keeps the
        Anthropic `tools` payload token-stable across turns and lets
        prompt caching hit consistently.
        """
        return [
            {
                "name": spec.name,
                "description": spec.agent_description,
                "input_schema": spec.input_schema.model_json_schema(),
            }
            for spec in self._registry.list_for("agent")
        ]

    async def dispatch(
        self,
        name: str,
        args_json: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Run the full policy gate then invoke the handler.

        Steps (any failure raises before invoking the handler):

        1. `registry.get(name)` — `KeyError` propagates if missing.
        2. Surface gate: spec must include `"agent"` in `surfaces`.
        3. Side-effects gate: agent surface permanently forbids `write` /
           `destructive`.
        4. Approval gate: agent surface permanently forbids
           `requires_approval=True`.
        5. Input schema validation: dict → typed Pydantic model. Failure
           raises `TypeError` (it's a type error, not a policy refusal).
        6. Resolve `ctx` (default = `self._context_factory()`), then
           invoke the handler — wrapped in `asyncio.wait_for` when
           `spec.timeout is not None`.
        7. Output schema sanity check via `isinstance`.
        8. Return `result.model_dump()` for the LLM-facing tool_result.

        Handler-side exceptions (anything **except** `ToolPolicyViolation`
        and `KeyError`) are wrapped into a `tool_error` envelope and
        returned as a dict; `ToolPolicyViolation` / `KeyError` propagate.
        """
        # 1. Lookup (KeyError propagates by design — adapter-self failure).
        spec = self._registry.get(name)

        # 2. Surface gate.
        if "agent" not in spec.surfaces:
            raise ToolPolicyViolation(
                tool_name=name,
                surface="agent",
                violated_field="surfaces",
                reason="not_exposed_to_surface",
            )

        # 3. Side-effects gate (agent surface permanently read-only).
        if spec.side_effects in {"write", "destructive"}:
            raise ToolPolicyViolation(
                tool_name=name,
                surface="agent",
                violated_field="side_effects",
                reason="side_effects_not_permitted",
            )

        # 4. Approval gate (agent surface has no approval flow — permanent invariant).
        if spec.requires_approval is True:
            raise ToolPolicyViolation(
                tool_name=name,
                surface="agent",
                violated_field="requires_approval",
                reason="approval_flow_not_supported",
            )

        # 5. Untrusted dict → typed Pydantic model boundary.
        try:
            args = spec.input_schema.model_validate(args_json)
        except Exception as exc:
            raise TypeError(
                f"ToolsAdapter.dispatch({name!r}) failed input schema validation: {exc!s}"
            ) from exc

        # 6. Resolve ToolContext and invoke handler (with optional timeout).
        active_ctx = ctx if ctx is not None else self._context_factory()
        try:
            if spec.timeout is not None:
                result = await asyncio.wait_for(
                    spec.handler(args, active_ctx), timeout=spec.timeout
                )
            else:
                result = await spec.handler(args, active_ctx)
        except ToolPolicyViolation:
            # Policy errors must surface to the Agent loop unwrapped.
            raise
        except KeyError:
            # Registry lookup errors must surface unwrapped (defensive — a
            # handler raising KeyError is unusual but the spec mandates the
            # same propagation semantics as adapter-self KeyError).
            raise
        except asyncio.CancelledError:
            # Cooperative cancellation (Ctrl-C / task cancel) must propagate
            # so upstream callers can stop the agent loop. Without this, the
            # broad `except Exception` below would catch CancelledError (it
            # subclasses Exception in Python 3.11+) and turn cancellation
            # into a normal tool_error envelope, leaving callers stuck.
            raise
        except Exception as exc:  # intentional broad catch for envelope
            return {
                "is_error": True,
                "error_kind": exc.__class__.__name__,
                "tool_name": name,
                "message": scrub_exception_message(str(exc)),
                "cause": scrub_exception_message(repr(exc)),
            }

        # 7. Output schema sanity check (handler contract).
        if not isinstance(result, spec.output_schema):
            # WHY ToolError (not TypeError): an output-schema mismatch is a
            # handler/adapter code bug, distinct from the step-5 input-schema
            # TypeError which signals recoverable malformed model args that the
            # Agent loop feeds back for self-correction. A separate exception
            # class lets the loop type-discriminate and fail loud on code bugs.
            raise ToolError(
                f"ToolsAdapter.dispatch({name!r}) expected handler to return "
                f"{spec.output_schema.__name__}, got {type(result).__name__}"
            )

        # 8. Serialize to dict for the LLM-facing tool_result.
        return result.model_dump()
