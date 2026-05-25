"""Finding DSL evaluator.

Three public functions:

* `evaluate(expr, context, *, timeout_seconds=1.0)` — run a single
  simpleeval expression with a fixed function set, a static AST gate that
  blocks the constructs simpleeval would otherwise accept (lambda /
  comprehensions / dunder access / import), and a hard `asyncio.wait_for`
  timeout as a soft fallback.
* `parse_for_each(for_each)` — split the `"<expr> as <var>"` form into
  `(expr, var)` and raise `InspectorError(finding_when_invalid)` on miss.
* `format_message(template, context)` — render a Python `.format()`
  template; any `KeyError` / `IndexError` / `AttributeError` propagates to
  the runner so a single bad rule skips itself without taking down the
  whole inspector.

Static-AST rejection comes **before** evaluation: even though simpleeval
already blocks lambda / comprehensions / dunder access, the AST gate
documents the threat model explicitly and removes the risk that a future
simpleeval default change silently re-enables one of them.
"""

from __future__ import annotations

import ast
import asyncio
import re
from datetime import UTC, datetime
from typing import Any

import simpleeval

from hostlens.core.exceptions import InspectorError

__all__ = ["evaluate", "format_message", "parse_for_each", "validate_ast"]


_FOR_EACH_PATTERN = re.compile(r"^(.+?)\s+as\s+([a-z_][a-z_0-9]*)$")


# Forbidden AST node types — simpleeval already rejects most of these, but
# the static gate makes the contract explicit and survives any future
# upstream policy change.
_FORBIDDEN_AST_NODES: tuple[type[ast.AST], ...] = (
    ast.Lambda,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.Import,
    ast.ImportFrom,
)


# Deny-list of dangerous Python builtins. simpleeval's runtime check would
# catch them via ``FunctionNotDefined`` (since they aren't in
# ``_DSL_FUNCTIONS``), but the static gate's contract is "reject before
# evaluation reaches simpleeval". Surfacing the rejection at `validate_ast`
# makes the manifest-load-time error message clear and stops a future
# simpleeval policy change from silently allowing these.
#
# The deny-list is enforced on **any** ``ast.Name`` reference (call target,
# argument, or bare reference) — passing ``getattr`` / ``eval`` as a value
# to another callable is just as dangerous as calling them directly, and
# enforcing the rule at the Name level (not just on ``Call.func``) is
# defense in depth against simpleeval policy regressions.
_FORBIDDEN_NAMES: frozenset[str] = frozenset(
    {
        # Code-execution / introspection primitives
        "eval", "exec", "compile", "open", "globals", "locals", "vars", "dir",
        "__import__",
        # Attribute indirection — bypass dunder restrictions by string
        "getattr", "setattr", "delattr", "hasattr",
        # Reflection / object identity
        "type", "id", "hash", "repr",
        # Iteration primitives — can drive arbitrary __next__ side effects
        "iter", "next",
        # IO primitives
        "print", "input", "breakpoint",
        # Memory / buffer constructors — exposing C-level conversions
        "memoryview", "bytearray", "bytes",
        # Descriptor / class machinery — escape paths via method binding
        "classmethod", "staticmethod", "property", "super",
    }
)


def _utc_now() -> datetime:
    """Return a tz-aware UTC `datetime` — used as the `now()` DSL builtin."""

    return datetime.now(UTC)


# Function set registered onto every `SimpleEval` instance. `float` / `int`
# are intentionally included so `system.uptime`'s `float(load1) > 4.0`
# finding rule can run without a separate Python hook.
_DSL_FUNCTIONS: dict[str, Any] = {
    "len": len,
    "sum": sum,
    "min": min,
    "max": max,
    "any": any,
    "all": all,
    "now": _utc_now,
    "float": float,
    "int": int,
}


def validate_ast(expr: str) -> None:
    """Static gate — reject lambda / comprehensions / imports / dunder access.

    Raises `simpleeval.FeatureNotAvailable` on any hit so the caller sees
    the same exception type whether the rejection came from the AST gate
    or from simpleeval itself.

    Exposed publicly so the schema-layer `FindingRule._validate_dsl` can
    apply the same static gate at manifest load time — otherwise
    constructs like `__import__('os')` slip past simpleeval's empty-context
    eval and only fail at runtime.
    """

    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise simpleeval.FeatureNotAvailable(
            f"expression failed to parse: {exc}"
        ) from exc

    for node in ast.walk(tree):
        if isinstance(node, _FORBIDDEN_AST_NODES):
            raise simpleeval.FeatureNotAvailable(
                f"AST node {type(node).__name__} is not permitted in DSL expressions"
            )
        # Reject any attribute access whose name starts with `__` — covers
        # `obj.__class__` / `obj.__bases__` / `obj.__subclasses__()` etc.
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise simpleeval.FeatureNotAvailable(
                f"dunder attribute access ({node.attr!r}) is not permitted"
            )
        # Reject any bare name reference whose id starts with `__` — defends
        # against `__import__('os')` style escapes.
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise simpleeval.FeatureNotAvailable(
                f"dunder name reference ({node.id!r}) is not permitted"
            )
        # Reject any ``ast.Name`` whose id is in ``_FORBIDDEN_NAMES``,
        # regardless of position (call target, argument, or bare reference).
        # simpleeval would block missing names at runtime via
        # ``NameNotDefined`` / ``FunctionNotDefined``, but the static gate's
        # contract is to reject *before* evaluation so the threat model
        # isn't dependent on the runtime function set — and so passing a
        # forbidden builtin as an argument (e.g. ``f(eval)``) is rejected
        # even when ``f`` itself is benign.
        if isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            raise simpleeval.FeatureNotAvailable(
                f"reference to {node.id!r} is not permitted in DSL expressions"
            )


def _build_evaluator(context: dict[str, Any]) -> simpleeval.SimpleEval:
    """Construct a fresh `SimpleEval` for a single evaluation.

    `SimpleEval` is cheap to instantiate and not designed for reuse across
    contexts — the per-call construction avoids state bleed between
    different finding rules within the same inspector run.
    """

    evaluator = simpleeval.SimpleEval()
    evaluator.functions = dict(_DSL_FUNCTIONS)
    evaluator.names = context
    return evaluator


async def evaluate(
    expr: str,
    context: dict[str, Any],
    *,
    timeout_seconds: float = 1.0,
) -> Any:
    """Evaluate `expr` against `context` with a hard timeout.

    The function is `async` because the timeout is implemented via
    `asyncio.wait_for(asyncio.to_thread(...))`. The static AST gate runs
    synchronously before any thread is dispatched so trivially-rejected
    expressions don't pay the threading overhead.

    Raises:
        simpleeval.FeatureNotAvailable: AST gate hit, or simpleeval refused
          the construct internally.
        simpleeval.InvalidExpression: any other simpleeval failure.
        asyncio.TimeoutError: evaluation exceeded `timeout_seconds`.
    """

    validate_ast(expr)
    evaluator = _build_evaluator(context)
    return await asyncio.wait_for(
        asyncio.to_thread(evaluator.eval, expr),
        timeout=timeout_seconds,
    )


def parse_for_each(for_each: str) -> tuple[str, str]:
    """Split a `"<iterable_expr> as <var_name>"` string.

    Returns `(iterable_expr, var_name)`. Mismatches raise
    `InspectorError(kind="finding_when_invalid")` so the loader and
    runner see the same error kind for a malformed `for_each`.
    """

    match = _FOR_EACH_PATTERN.match(for_each)
    if match is None:
        raise InspectorError(kind="finding_when_invalid")
    return match.group(1), match.group(2)


def format_message(template: str, context: dict[str, Any]) -> str:
    """Render `template` via `str.format(**context)`.

    `KeyError` / `IndexError` / `AttributeError` are allowed to propagate
    — the runner catches them at this exact call site (per design.md
    decision: the **only** place runner is allowed to catch `KeyError` or
    `AttributeError`) and skips the offending finding rule.
    """

    return template.format(**context)
