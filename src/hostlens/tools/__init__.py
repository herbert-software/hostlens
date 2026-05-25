"""Hostlens Tool Registry capability layer (M2).

Re-exports the five canonical names per spec §1.1:

    ToolSpec, ToolContext, ToolRegistry, register_default_tools, tool

These are resolved lazily through `__getattr__` so submodules can be
imported without dragging the entire tools subpackage. Without lazy
resolution, importing `hostlens.tools.schemas.list_inspectors` (needed
by `hostlens.inspectors.registry` for `InspectorSummary`) would trigger
`hostlens.tools.base`, which itself depends on
`hostlens.inspectors.registry` — a circular import that crashes at
package load time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hostlens.tools.base import ToolContext, ToolSpec
    from hostlens.tools.decorators import tool
    from hostlens.tools.default_tools import register_default_tools
    from hostlens.tools.registry import ToolRegistry

__all__ = [
    "ToolContext",
    "ToolRegistry",
    "ToolSpec",
    "register_default_tools",
    "tool",
]


def __getattr__(name: str) -> Any:
    """Resolve the five canonical names on first access.

    Loading the submodule that owns each export only when it is asked for
    keeps `import hostlens.tools.schemas.list_inspectors` cheap (it does
    not trigger `tools.base`, which would otherwise pull in
    `inspectors.registry` mid-cycle).
    """

    if name in ("ToolContext", "ToolSpec"):
        from hostlens.tools import base

        value = getattr(base, name)
    elif name == "tool":
        from hostlens.tools import decorators

        value = decorators.tool
    elif name == "register_default_tools":
        from hostlens.tools import default_tools

        value = default_tools.register_default_tools
    elif name == "ToolRegistry":
        from hostlens.tools import registry

        value = registry.ToolRegistry
    else:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        )
    globals()[name] = value
    return value
