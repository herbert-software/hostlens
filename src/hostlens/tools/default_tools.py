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

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

from pydantic import BaseModel

from hostlens.targets.base import Capability
from hostlens.tools.base import ToolContext
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
    FindingSummary,
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
# Helpers
# ---------------------------------------------------------------------------


def _attr(obj: Any, name: str, default: Any) -> Any:
    """Read `name` from `obj` whether it is a Pydantic model, dataclass,
    plain object, or any `collections.abc.Mapping` (covers `dict`,
    `os._Environ`, and other mapping types). M1 hasn't shipped the real
    `TargetRegistry` / `InspectorRegistry` summary types yet, so handlers
    accept any structurally-compatible object and fall back to mapping
    lookup. This is the only place we accept heterogeneity — once M1
    lands, summaries become typed and this helper can be deleted.
    """
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


# ---------------------------------------------------------------------------
# Handler: run_inspector
# ---------------------------------------------------------------------------


async def run_inspector_handler(args: RunInspectorInput, ctx: ToolContext) -> RunInspectorOutput:
    """M2 stub handler. M1 will replace this once `ExecutionTarget` +
    `Inspector` ship. For now we return a single placeholder finding so
    the demo path can exercise registry dispatch + adapter projection
    end-to-end.
    """
    # We deliberately ignore the registries: M1 isn't here yet. Touching
    # them is reserved for the M1 integration step.
    del ctx
    return RunInspectorOutput(
        target_name=args.target_name,
        inspector_name=args.inspector_name,
        findings=[
            FindingSummary(
                severity="info",
                message=(
                    f"stub finding for inspector {args.inspector_name!r} "
                    f"on target {args.target_name!r} (M1 handler not yet shipped)"
                ),
                evidence={},
            )
        ],
    )


# ---------------------------------------------------------------------------
# Handler: list_inspectors
# ---------------------------------------------------------------------------


async def list_inspectors_handler(
    args: ListInspectorsInput, ctx: ToolContext
) -> ListInspectorsOutput:
    """Read inspector summaries from `ctx.inspector_registry` and apply
    optional `tag` / `target_kind` filters.

    The handler trusts `ctx.inspector_registry.list_summaries()` to
    return objects exposing `name` / `version` / `description` / `tags`
    / `compatible_target_kinds` attributes (or matching dict keys).
    """
    raw_summaries = ctx.inspector_registry.list_summaries()
    summaries: list[InspectorSummary] = []
    for raw in raw_summaries:
        tags = list(_attr(raw, "tags", []))
        compatible = list(_attr(raw, "compatible_target_kinds", []))

        if args.tag is not None and args.tag not in tags:
            continue
        if args.target_kind is not None and args.target_kind not in compatible:
            continue

        summaries.append(
            InspectorSummary(
                name=str(_attr(raw, "name", "")),
                version=str(_attr(raw, "version", "")),
                description=str(_attr(raw, "description", "")),
                tags=tags,
                compatible_target_kinds=compatible,
            )
        )
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


run_inspector = tool(
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
)(cast(_BroadHandler, run_inspector_handler))


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


def register_default_tools(registry: ToolRegistry) -> None:
    """Register the M2 first-batch ToolSpecs into `registry`.

    Non-idempotent: calling twice on the same registry raises
    `ToolError` because `ToolRegistry.register` rejects duplicate names.
    Callers that need a clean state must allocate a fresh
    `ToolRegistry()`.
    """
    registry.register(run_inspector)
    registry.register(list_inspectors)
    registry.register(list_targets)
