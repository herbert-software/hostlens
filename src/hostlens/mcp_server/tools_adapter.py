"""MCP surface adapter — projects ToolSpec into official `mcp` SDK tool definitions.

Layer 2 of the double-layer capability model (CLAUDE.md §4.10). This module
is the sanctioned bridge between a host-agnostic `ToolSpec` (Layer 1) and the
MCP tools protocol:

- `list_for_mcp()` projects all `surfaces ∋ "mcp"` specs into a list of
  `mcp.types.Tool` instances (`name` / `description=mcp_description` /
  `inputSchema` from Pydantic projection).
- `dispatch(name, args_json, ctx)` performs the untrusted-dict → typed
  Pydantic model boundary check, then runs the MCP policy gate (surface /
  sensitive_output / side_effects / requires_approval), invokes the handler
  with optional timeout, and wraps any non-policy / non-lookup handler
  exception into a structured error envelope (with all string values scrubbed
  by `scrub_exception_message` to prevent secret leakage into the remote LLM
  context).

`ToolPolicyViolation`, `KeyError` (missing tool name), and
`asyncio.CancelledError` propagate unwrapped.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from mcp.types import Tool

from hostlens.agent.tools_adapter import scrub_exception_message
from hostlens.core.exceptions import ToolError, ToolPolicyViolation
from hostlens.tools.base import ToolContext
from hostlens.tools.registry import ToolRegistry

__all__ = ["McpToolsAdapter"]


class McpToolsAdapter:
    """MCP-surface adapter: ToolSpec → official `mcp.types.Tool` definitions.

    Construction takes a `ToolRegistry` and a `Callable[[], ToolContext]`
    factory. The factory is invoked **per `dispatch` call** (not once at
    construction) so that mutable per-call state — most importantly
    `cancel: asyncio.Event` — is never shared across MCP `call_tool`
    invocations.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        context_factory: Callable[[], ToolContext],
    ) -> None:
        self._registry = registry
        self._context_factory = context_factory

    def list_for_mcp(self) -> list[Tool]:
        """Project all `surfaces ∋ "mcp"` specs into MCP SDK tool definitions.

        Each spec must have `sensitive_output is not None` (fail-closed per
        §4.10 rule 6); undeclared specs raise `ToolPolicyViolation` rather
        than being silently skipped.
        """
        tools: list[Tool] = []
        for spec in self._registry.list_for("mcp"):
            if spec.sensitive_output is None:
                raise ToolPolicyViolation(
                    tool_name=spec.name,
                    surface="mcp",
                    violated_field="sensitive_output",
                    reason="sensitive_output_not_declared",
                )
            tools.append(
                Tool(
                    name=spec.name,
                    description=spec.mcp_description,
                    inputSchema=spec.input_schema.model_json_schema(),
                )
            )
        return tools

    async def dispatch(
        self,
        name: str,
        args_json: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Run the full MCP policy gate then invoke the handler.

        Steps (any failure raises before invoking the handler unless noted):

        1. `registry.get(name)` — `KeyError` propagates if missing.
        2. Surface gate: spec must include `"mcp"` in `surfaces`.
        3. Sensitive-output gate: `sensitive_output is not None` required.
        4. Side-effects gate: MCP forbids `write` / `destructive`.
        5. Approval gate: MCP forbids `requires_approval=True`.
        6. Input schema validation: dict → typed Pydantic model. Failure
           raises `TypeError`.
        7. Resolve `ctx` (default = `self._context_factory()`), then
           invoke the handler — wrapped in `asyncio.wait_for` when
           `spec.timeout is not None`.
        8. Output schema sanity check via `isinstance`.
        9. Return `result.model_dump()`.

        Handler-side exceptions (anything **except** `ToolPolicyViolation`,
        `KeyError`, and `asyncio.CancelledError`) are wrapped into an error
        envelope and returned as a dict.
        """
        # 1. Lookup (KeyError propagates by design — adapter-self failure).
        spec = self._registry.get(name)

        # 2. Surface gate.
        if "mcp" not in spec.surfaces:
            raise ToolPolicyViolation(
                tool_name=name,
                surface="mcp",
                violated_field="surfaces",
                reason="not_exposed_to_surface",
            )

        # 3. Sensitive-output gate (fail-closed; symmetric with list_for_mcp).
        if spec.sensitive_output is None:
            raise ToolPolicyViolation(
                tool_name=name,
                surface="mcp",
                violated_field="sensitive_output",
                reason="sensitive_output_not_declared",
            )

        # 4. Side-effects gate (MCP read-only).
        if spec.side_effects in {"write", "destructive"}:
            raise ToolPolicyViolation(
                tool_name=name,
                surface="mcp",
                violated_field="side_effects",
                reason="side_effects_not_permitted",
            )

        # 5. Approval gate (MCP has no approval flow — permanent invariant).
        if spec.requires_approval is True:
            raise ToolPolicyViolation(
                tool_name=name,
                surface="mcp",
                violated_field="requires_approval",
                reason="approval_flow_not_supported",
            )

        # 6. Untrusted dict → typed Pydantic model boundary.
        try:
            args = spec.input_schema.model_validate(args_json)
        except Exception as exc:
            raise TypeError(
                f"McpToolsAdapter.dispatch({name!r}) failed input schema validation: {exc!s}"
            ) from exc

        # 7. Resolve ToolContext and invoke handler (with optional timeout).
        active_ctx = ctx if ctx is not None else self._context_factory()
        try:
            if spec.timeout is not None:
                result = await asyncio.wait_for(
                    spec.handler(args, active_ctx), timeout=spec.timeout
                )
            else:
                result = await spec.handler(args, active_ctx)
        except ToolPolicyViolation:
            raise
        except KeyError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return {
                "is_error": True,
                "error_kind": exc.__class__.__name__,
                "tool_name": name,
                "message": scrub_exception_message(str(exc)),
                "cause": scrub_exception_message(repr(exc)),
            }

        # 8. Output schema sanity check (handler contract).
        if not isinstance(result, spec.output_schema):
            raise ToolError(
                f"McpToolsAdapter.dispatch({name!r}) expected handler to return "
                f"{spec.output_schema.__name__}, got {type(result).__name__}"
            )

        # 9. Serialize to dict for the MCP server layer.
        return result.model_dump()
