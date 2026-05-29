"""Planner Agent — the inspection-semantics assembler over ``AgentLoop``.

``AgentLoop`` (M2.2) is intent-agnostic: it drives a generic tool-use loop but
neither builds a system prompt nor knows what "inspection" means.
``ToolsAdapter`` (M2.3) only projects/dispatches ToolSpecs. ``PlannerAgent`` is
the missing middle layer (CLAUDE.md §4.2 "Agent is the scheduler"): it turns a
natural-language intent into one fully-wired ``AgentLoop`` run, then condenses
the generic ``LoopResult`` into a consumable ``PlannerResult`` (narrative +
structured findings + loop telemetry).

Two readable steps:
  1. assembly (``__init__``) — load the external prompt template, render the
     tool overview deterministically, wrap it as a single text block, and wire
     ``ToolsAdapter`` + ``AgentLoop``.
  2. condensation (``run``) — drive the loop and collect ``run_inspector``
     findings without re-interpreting the terminal status.

The backend is handed to ``AgentLoop`` only — never to the ``context_factory``
that produces ``ToolContext`` — so a tool handler can never reach the LLM
(ADR-008 / CLAUDE.md §7).
"""

from __future__ import annotations

from importlib.resources import files
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from hostlens.agent.loop import AgentLoop, LoopResult
from hostlens.agent.tools_adapter import ToolsAdapter
from hostlens.core.exceptions import ConfigError
from hostlens.reporting.models import Finding
from hostlens.tools.default_tools import run_inspector
from hostlens.tools.schemas.run_inspector import RunInspectorOutput

if TYPE_CHECKING:
    from collections.abc import Callable

    from hostlens.agent.backend import LLMBackend
    from hostlens.core.config import Settings
    from hostlens.tools.base import ToolContext
    from hostlens.tools.registry import ToolRegistry

__all__ = ["PlannerAgent", "PlannerResult"]


# Placeholder in ``planner.md`` replaced with the rendered tool overview. A
# single ``str.replace`` keeps the Agent layer free of a template engine
# (Jinja2 stays a notifier/report concern) for one substitution (design D-2).
_TOOL_OVERVIEW_PLACEHOLDER = "{tool_overview}"

# Package + resource name of the external prompt template. Externalizing the
# prompt is mandated by CLAUDE.md §7; ``importlib.resources`` keeps it readable
# after a pip install.
_PROMPT_PACKAGE = "hostlens.agent.prompts"
_PROMPT_RESOURCE = "planner.md"


class PlannerResult(BaseModel):
    """Condensed result of one ``PlannerAgent.run`` (design D-3).

    Deliberately NOT a ``reporting.models.Report``: ``run_inspector`` output
    carries lossless ``Finding`` objects but no InspectorResult-level fields
    (status / timing / result-level evidence), so assembling a full ``Report``
    here would force fabrication. That assembly + correlation is M3.
    """

    model_config = ConfigDict(frozen=True)

    narrative: str
    findings: list[Finding]
    loop_result: LoopResult
    intent: str


class PlannerAgent:
    """Assembles a system prompt + tool set + backend into one ``AgentLoop`` run."""

    def __init__(
        self,
        backend: LLMBackend,
        registry: ToolRegistry,
        settings: Settings,
        context_factory: Callable[[], ToolContext],
        *,
        prompt_path: str | None = None,
    ) -> None:
        # Render a cross-run-stable system prompt from the external template.
        # The rendered text MUST be a single-element text block list — a bare
        # str makes ``AgentLoop._inject_cache_control`` skip cache_control
        # injection, silently disabling prompt caching (design D-2).
        rendered = self._render_system_prompt(registry, prompt_path)
        system: list[dict[str, Any]] = [{"type": "text", "text": rendered}]

        adapter = ToolsAdapter(registry, context_factory)
        # Backend reaches ONLY the loop — never the context_factory's
        # ToolContext (ADR-008). This is the single site where backend is used.
        self._loop = AgentLoop(backend, adapter, settings, system=system)

    @staticmethod
    def _render_system_prompt(
        registry: ToolRegistry,
        prompt_path: str | None,
    ) -> str:
        """Load the template and replace the tool-overview placeholder.

        Missing/unreadable template fails loud (``ConfigError``) at construction
        rather than silently degrading to an empty prompt, which would make the
        Agent's behavior uncontrollable (design D-2, Failure Mode 5).
        """
        try:
            if prompt_path is not None:
                from pathlib import Path

                template = Path(prompt_path).read_text(encoding="utf-8")
            else:
                template = (
                    files(_PROMPT_PACKAGE).joinpath(_PROMPT_RESOURCE).read_text(encoding="utf-8")
                )
        except (FileNotFoundError, OSError) as exc:
            raise ConfigError(
                "planner prompt template not found",
                kind="planner_prompt_missing",
                original=exc,
            ) from exc

        # ``list_for("agent")`` is already sorted by spec.name ascending, so the
        # overview is byte-stable across runs for a fixed tool set — the prompt
        # caching prerequisite (CLAUDE.md §4.8 / design D-2).
        overview = "\n".join(
            f"- {spec.name}: {spec.agent_description}" for spec in registry.list_for("agent")
        )
        return template.replace(_TOOL_OVERVIEW_PLACEHOLDER, overview)

    async def run(self, intent: str) -> PlannerResult:
        """Drive the loop, then condense ``LoopResult`` into ``PlannerResult``.

        Findings are collected from successful ``run_inspector`` invocations
        only; error invocations (``output is None``) are skipped but stay in
        ``loop_result.tool_invocations`` for debugging. terminal_status and
        ``final_text`` are passed through verbatim — the loop is the single
        owner of retry and status semantics (ADR-005 / design D-4).
        """
        loop_result = await self._loop.run(intent)

        findings: list[Finding] = []
        for inv in loop_result.tool_invocations:
            if inv.tool_name != run_inspector.name or inv.output is None:
                continue
            # output is this process's own dispatch model_dump() — schema is
            # self-consistent, so a model_validate failure is a code bug and
            # should fail loud, not be swallowed (CLAUDE.md §6, design risk).
            findings.extend(RunInspectorOutput.model_validate(inv.output).findings)

        return PlannerResult(
            narrative=loop_result.final_text,
            findings=findings,
            loop_result=loop_result,
            intent=intent,
        )
