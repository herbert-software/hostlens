"""M2 default ToolSpec batch + `register_default_tools` assembly point.

This module is the single place where the M2 first-batch ToolSpecs are
declared (`run_inspector` / `list_inspectors` / `list_targets`) and the
single place where they are registered (`register_default_tools`).

Per CLAUDE.md §4.10 and design.md §D-3, `@tool` is a pure spec factory:
decoration does NOT mutate any module-level registry — assembly is
explicit, called once at agent loop startup.

Per design.md §D-11, `register_default_tools` is intentionally
non-idempotent: a duplicate call on the same registry raises
`ToolError`. Tests that need a clean registry must allocate a fresh
`ToolRegistry()`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, cast

from pydantic import BaseModel

from hostlens.core.exceptions import InspectorError, ToolError
from hostlens.inspectors.runner import InspectorRunner
from hostlens.targets.base import Capability
from hostlens.tools.base import ToolContext, ToolSpec
from hostlens.tools.decorators import tool
from hostlens.tools.registry import ToolRegistry
from hostlens.tools.schemas.list_inspectors import (
    InspectorSummary,
    ListInspectorsInput,
    ListInspectorsOutput,
)
from hostlens.tools.schemas.list_targets import (
    CAPABILITY_ALLOWLIST,
    ListTargetsInput,
    ListTargetsOutput,
    TargetSummary,
    scrub_inventory_string,
)
from hostlens.tools.schemas.run_inspector import (
    RunInspectorInput,
    RunInspectorOutput,
)

__all__ = [
    "list_inspectors",
    "list_targets",
    "register_default_tools",
    "run_inspector",
]


# ---------------------------------------------------------------------------
# Handler: run_inspector
# ---------------------------------------------------------------------------


async def run_inspector_handler(
    args: RunInspectorInput,
    ctx: ToolContext,
    *,
    clock: Callable[[], datetime] | None = None,
) -> RunInspectorOutput:
    """Dispatch one Inspector run against one target via `InspectorRunner`.

    `clock` is normally `None` (production) → `InspectorRunner` uses its
    real-UTC default. The test/replay assembly threads a frozen clock here via
    `register_default_tools(clock=...)` (incident-pack Option C) so
    `sampling_window` inspectors render byte-stable commands for ReplayTarget
    matching; `ToolContext` stays at its locked six-field set (ADR-008).

    Caller-side programming errors (unknown target name / unknown inspector
    name) raise `ToolError` so the agent loop surfaces the misuse instead
    of silently returning empty findings — these are not business failures.

    Every other failure (target unreachable, command timeout, parse /
    schema mismatch, finding-rule errors, missing capabilities, missing
    privilege opt-in) collapses into a successful tool dispatch that
    returns `findings=[]`. The runner's `InspectorStatus` is recorded via
    structlog (per design.md Decision 7) but is **not** projected into
    `RunInspectorOutput` because that schema is M2-locked; M3
    `add-report-data-model` is the proposal that extends the surface to
    expose status / error / missing.

    `allow_privileged` is forced to `False` on the agent surface — the
    Planner Agent cannot opt-in to sudo/root inspectors. Only the human
    CLI / approval flow may do so (and that flow lives downstream).
    """

    # ---- 1. Lookup target -------------------------------------------- #
    # `TargetRegistry.get` raises bare `KeyError` for an unknown name
    # (per execution-target spec §场景:get 未找到 raise KeyError); convert
    # it into a structured `ToolError` so the agent loop can surface the
    # misuse without crashing the tool_use turn on a stray KeyError.
    try:
        target = ctx.target_registry.get(args.target_name)
    except KeyError as exc:
        raise ToolError(
            f"target_not_found: target_name={args.target_name!r} "
            "is not registered in target_registry"
        ) from exc

    # ---- 2. Lookup inspector manifest -------------------------------- #
    try:
        manifest = ctx.inspector_registry.get(args.inspector_name)
    except InspectorError as exc:
        if exc.kind == "inspector_not_found":
            raise ToolError(
                f"inspector_not_found: inspector_name={args.inspector_name!r} "
                "is not registered in inspector_registry"
            ) from exc
        # Any other InspectorError shape on `get` would be a registry bug —
        # let it propagate so the runner-internal contract is visible.
        raise

    # ---- 3. Build runner + dispatch ---------------------------------- #
    if clock is None:
        runner = InspectorRunner(
            ctx.target_registry,
            settings=ctx.config,
            logger=ctx.logger,
        )
    else:
        runner = InspectorRunner(
            ctx.target_registry,
            settings=ctx.config,
            logger=ctx.logger,
            clock=clock,
        )
    result = await runner.run(
        manifest,
        target,
        parameters=dict(args.parameters) if args.parameters else None,
        # Agent surface NEVER opts in to privilege — only the CLI / human
        # approval path may set this to True in a future milestone.
        allow_privileged=False,
        cancel=ctx.cancel,
    )

    # ---- 4. Log status (status field is NOT exposed via RunInspectorOutput) ---- #
    if result.status != "ok":
        # status/error/missing belong to M3's RunInspectorOutput extension;
        # for M2 we surface them only via structlog so the Planner Agent
        # sees an empty findings list + the operator can debug via logs.
        ctx.logger.info(
            "run_inspector_non_ok_status",
            inspector_name=result.name,
            target_name=result.target_name,
            inspector_status=result.status,
            error=result.error,
            missing=result.missing,
        )

    # ---- 5. Project InspectorResult -> RunInspectorOutput ------------ #
    # When status != "ok" the runner already produces an empty findings list,
    # but we enforce findings=[] here at the handler boundary so the
    # ToolSpec contract stays stable even if runner behavior shifts later.
    if result.status != "ok":
        return RunInspectorOutput(
            target_name=result.target_name,
            inspector_name=result.name,
            findings=[],
        )
    # `FindingSummary` is a type alias for `hostlens.reporting.models.Finding`
    # (the same model `InspectorResult.findings` already holds), so we can
    # reuse the runner's findings directly without re-constructing each one.
    return RunInspectorOutput(
        target_name=result.target_name,
        inspector_name=result.name,
        findings=list(result.findings),
    )


# ---------------------------------------------------------------------------
# Handler: list_inspectors
# ---------------------------------------------------------------------------


async def list_inspectors_handler(
    args: ListInspectorsInput, ctx: ToolContext
) -> ListInspectorsOutput:
    """Read inspector summaries from `ctx.inspector_registry` and apply
    optional `tag` / `target_kind` filters.

    `ctx.inspector_registry.list_summaries()` returns
    `list[InspectorSummary]` directly — the real registry already projects
    each manifest into the M2-locked summary schema and sorts `tags` /
    `compatible_target_kinds` in dictionary order for prompt-cache prefix
    stability (per inspector-plugin-system spec §需求:`InspectorRegistry`
    API 必须支持注册 / 查询 / 列表 / summary 投影).
    """
    raw_summaries = ctx.inspector_registry.list_summaries()
    summaries: list[InspectorSummary] = []
    for summary in raw_summaries:
        if args.tag is not None and args.tag not in summary.tags:
            continue
        if args.target_kind is not None and args.target_kind not in summary.compatible_target_kinds:
            continue
        summaries.append(summary)
    return ListInspectorsOutput(inspectors=summaries)


# ---------------------------------------------------------------------------
# Handler: list_targets
# ---------------------------------------------------------------------------


async def list_targets_handler(args: ListTargetsInput, ctx: ToolContext) -> ListTargetsOutput:
    """Project each registered ExecutionTarget down to a redacted `TargetSummary`.

    Source of fields (per tool-registry spec §需求:M2 首批 ToolSpec
    §场景:TargetSummary metadata 字段必须来自 TargetEntry):

    - `name` / `kind` come from the `ExecutionTarget` instance
      (`target.name` / `target.type`).
    - `capabilities` come from `target.capabilities` (a set of
      `Capability` enum members) — projected to `.value` strings and
      filtered through `CAPABILITY_ALLOWLIST` in lexicographic order.
    - `display_name` / `description` / `tags` / `enabled` come from the
      paired `TargetEntry` returned by `ctx.target_registry.get_entry(name)`
      — these fields do **not** live on the `ExecutionTarget` Protocol,
      so any attribute of the same name found on the target instance
      MUST be ignored.

    Every string field (`name` / `display_name` / `description` plus
    every `tags[*]`) MUST pass through `scrub_inventory_string` before
    reaching the agent surface; if scrub returns `None` (sensitive
    substring matched), the whole target is dropped (a half-revealed
    target is a worse leak than a missing row). A structured warning
    is logged with the reason code (e.g.
    `sensitive_substring_in_display_name`) but NOT the offending field
    value.
    """
    targets = ctx.target_registry.list()
    summaries: list[TargetSummary] = []

    for target in targets:
        entry = ctx.target_registry.get_entry(target.name)

        enabled = bool(entry.enabled)
        if not enabled and not args.include_disabled:
            continue

        # `target.type` is a closed Literal set per ExecutionTarget Protocol
        # — but we still defence-in-depth check it here so a misbehaving
        # custom target implementation cannot push an unsupported kind
        # through TargetSummary's discriminator.
        kind = target.type
        if kind not in ("local", "ssh", "docker", "k8s"):
            ctx.logger.warning(
                "list_targets_skip",
                reason="unsupported_kind",
                kind_type=type(kind).__name__,
            )
            continue

        # Scrub scalar string fields sourced from target + entry.
        # `name` ← target.name; `display_name` / `description` ← entry.
        scalar_field_sources: dict[str, str | None] = {
            "name": target.name,
            "display_name": entry.display_name,
            "description": entry.description,
        }
        scrubbed: dict[str, str | None] = {}
        skip_this_target = False
        for field_name, value in scalar_field_sources.items():
            if value is None:
                scrubbed[field_name] = None
                continue
            cleaned = scrub_inventory_string(value, field_kind=field_name)
            if cleaned is None:
                ctx.logger.warning(
                    "list_targets_skip",
                    reason=f"sensitive_substring_in_{field_name}",
                )
                skip_this_target = True
                break
            scrubbed[field_name] = cleaned

        if skip_this_target:
            continue

        # Scrub tag list (sourced from entry.tags).
        clean_tags: list[str] = []
        skip_for_tags = False
        for tag in entry.tags:
            cleaned_tag = scrub_inventory_string(tag, field_kind="tags")
            if cleaned_tag is None:
                ctx.logger.warning(
                    "list_targets_skip",
                    reason="sensitive_substring_in_tags",
                )
                skip_for_tags = True
                break
            clean_tags.append(cleaned_tag)
        if skip_for_tags:
            continue

        # Project capabilities: Capability enum → .value string, filter
        # through CAPABILITY_ALLOWLIST, then sort lexicographically per
        # spec §需求:M2 首批 ToolSpec handler 投影契约.
        capability_values: list[str] = []
        for cap in target.capabilities:
            if not isinstance(cap, Capability):
                # Defence-in-depth: an outside contributor could push a
                # bare string into `target.capabilities`. Skip silently
                # but log so the bug surfaces in observability.
                ctx.logger.warning(
                    "list_targets_capability_not_enum",
                    capability_type=type(cap).__name__,
                )
                continue
            value = cap.value
            if value in CAPABILITY_ALLOWLIST:
                capability_values.append(value)
            else:
                # Non-allowlisted tokens are silently dropped per spec
                # §场景:list_targets 投影过滤 allowlist 外 token.
                ctx.logger.warning(
                    "list_targets_capability_dropped",
                    reason="not_in_allowlist",
                )
        allowlisted_caps = sorted(capability_values)

        # `target.name` is regex-enforced by TargetRegistry.register, so
        # `scrubbed["name"]` should always be a string here. Guard the
        # narrow type cast for mypy.
        clean_name = scrubbed["name"]
        assert clean_name is not None

        summaries.append(
            TargetSummary(
                name=clean_name,
                kind=kind,
                display_name=scrubbed.get("display_name"),
                description=scrubbed.get("description"),
                capabilities=allowlisted_caps,
                tags=clean_tags,
                enabled=enabled,
            )
        )

    return ListTargetsOutput(targets=summaries)


# ---------------------------------------------------------------------------
# ToolSpec definitions (pure spec factories — no global state mutated).
# ---------------------------------------------------------------------------

# Type alias matching the `@tool` decorator's narrow handler shape.
# Concrete handlers are typed against their specific input/output Pydantic
# models for IDE / mypy support inside the function body; we cast back to
# the broad shape at decoration time because `Callable` is contravariant
# in its argument types (a `RunInspectorInput`-typed handler is not a
# structural subtype of a `BaseModel`-typed handler). Runtime correctness
# is enforced by `ToolSpec`'s field validators, not by static types.
_BroadHandler = Callable[[BaseModel, Any], Awaitable[BaseModel]]


def build_run_inspector_spec(handler: _BroadHandler) -> ToolSpec:
    """Build the `run_inspector` ToolSpec around `handler`.

    Factored out so `register_default_tools(clock=...)` can register a
    clock-bound handler closure (incident-pack Option C) that shares the exact
    same policy metadata as the default module-level spec — only the handler's
    clock injection varies, never the surface / side_effects / sensitivity.
    """
    return tool(
        name="run_inspector",
        version="1.0.0",
        input_schema=RunInspectorInput,
        output_schema=RunInspectorOutput,
        agent_description=(
            "Run one inspector against one target and return the inspector's "
            "findings. Use this after picking a target with `list_targets` and "
            "an inspector with `list_inspectors`."
        ),
        mcp_description=(
            "Run one read-only inspector against one target. Output may "
            "contain process / port / connection metadata."
        ),
        cli_help=None,
        surfaces={"agent"},
        side_effects="read",
        sensitive_output=True,
        timeout=30.0,
    )(handler)


run_inspector = build_run_inspector_spec(cast(_BroadHandler, run_inspector_handler))


list_inspectors = tool(
    name="list_inspectors",
    version="1.0.0",
    input_schema=ListInspectorsInput,
    output_schema=ListInspectorsOutput,
    agent_description=(
        "List available inspectors with optional filtering by tag or "
        "compatible target kind. Use this to discover which inspectors "
        "can run against the targets you already know about."
    ),
    mcp_description=(
        "List available inspectors (project metadata). Each entry "
        "carries name / version / description / tags / compatible target "
        "kinds. No secrets."
    ),
    cli_help=None,
    surfaces={"agent"},
    side_effects="none",
    sensitive_output=False,
    timeout=5.0,
)(cast(_BroadHandler, list_inspectors_handler))


list_targets = tool(
    name="list_targets",
    version="1.0.0",
    input_schema=ListTargetsInput,
    output_schema=ListTargetsOutput,
    agent_description=(
        "List configured targets (hosts / containers / pods) with only "
        "the fields safe to expose: name / kind / capabilities / tags. "
        "Credentials and connection strings are never returned."
    ),
    mcp_description=(
        "List configured targets with a redacted summary (no "
        "credentials / hosts / ports). Even the redacted shape reveals "
        "environment structure — gate MCP exposure accordingly."
    ),
    cli_help=None,
    surfaces={"agent"},
    side_effects="none",
    sensitive_output=True,
    timeout=5.0,
)(cast(_BroadHandler, list_targets_handler))


# ---------------------------------------------------------------------------
# Explicit assembly
# ---------------------------------------------------------------------------


def register_default_tools(
    registry: ToolRegistry,
    *,
    clock: Callable[[], datetime] | None = None,
) -> None:
    """Register the M2 first-batch ToolSpecs into `registry`.

    Non-idempotent: calling twice on the same registry raises
    `ToolError` because `ToolRegistry.register` rejects duplicate names.
    Callers that need a clean state must allocate a fresh
    `ToolRegistry()`.

    `clock` is normally `None` (production) and the default module-level
    `run_inspector` spec is registered. When a clock is supplied (incident-pack
    Option C: the offline snapshot / replay assembly), a clock-bound
    `run_inspector` spec is registered instead so `sampling_window` inspectors
    render byte-stable commands under a frozen clock. The clock rides on this
    assembly boundary, not on `ToolContext` — keeping the DI container at its
    locked six-field set (ADR-008).
    """
    if clock is None:
        registry.register(run_inspector)
    else:

        async def _clock_bound_run_inspector(
            args: RunInspectorInput, ctx: ToolContext
        ) -> RunInspectorOutput:
            return await run_inspector_handler(args, ctx, clock=clock)

        registry.register(build_run_inspector_spec(cast(_BroadHandler, _clock_bound_run_inspector)))
    registry.register(list_inspectors)
    registry.register(list_targets)
