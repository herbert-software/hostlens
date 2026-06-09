"""Inspector manifest Pydantic v2 schema.

This module defines the M1 manifest field set: `InspectorManifest` plus its
two nested specs (`CollectSpec` / `ParseSpec`) and the four-field finding DSL
(`FindingRule`). All models are frozen and reject unknown fields so that
typos / M1-disabled fields (`hook` / `sampling_window` / `artifacts`) raise
at load time rather than silently being ignored.

The strict character sets and AST-level ReDoS rejection live at the schema
layer so that **manifest authoring time** (`load_manifest`) — not runtime —
is the point where shell-injection / catastrophic-backtracking risk gets
gated. See `inspector-plugin-system/spec.md` for the full requirement set.
"""

from __future__ import annotations

import re
import warnings as _warnings
from typing import Annotated, Any, Literal

import jsonschema
import simpleeval
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# `sre_parse` / `sre_constants` are the only sanctioned way to introspect a
# compiled regex AST without importing private CPython internals (the
# underscore-prefixed `re._parser` alias is not API-stable across releases).
# Suppress the DeprecationWarning under a contextmanager during import.
with _warnings.catch_warnings():
    _warnings.filterwarnings("ignore", category=DeprecationWarning)
    import sre_constants as _sc
    import sre_parse as _sre_parse

__all__ = [
    "CollectSpec",
    "FindingRule",
    "InspectorManifest",
    "ParseSpec",
    "SamplingWindow",
]


# --------------------------------------------------------------------------- #
# Allowed values
# --------------------------------------------------------------------------- #

# M1 capability values — must match `hostlens.targets.capability.Capability`
# Enum lowercase values. Kept as a frozenset so `requires_capabilities`
# validation runs in O(1) per element.
_ALLOWED_CAPABILITIES: frozenset[str] = frozenset(
    {"shell", "file_read", "ssh", "systemd", "docker_cli"}
)


# --------------------------------------------------------------------------- #
# ReDoS static rejection — walks an `sre_parse` AST and returns a tag string
# identifying the first known-bad pattern category encountered, or None if
# the regex passes all six rules. The detector is intentionally strict: M1
# manifests have no business using backreferences, atomic groups, or
# quantifier-on-assert constructs.
# --------------------------------------------------------------------------- #


def _walk_subtree(subtree: Any) -> Any:
    """Yield every (op, args) tuple reachable from a parsed regex subtree.

    `sre_parse.parse(...)` returns a `SubPattern` whose iteration yields
    `(opcode, args)` tuples. Args may themselves be nested SubPatterns,
    lists, or tuples — so we recurse generically.

    Note: `SubPattern` is iterable via `__getitem__` (not `__iter__`), so
    we use `iter()` with a try/except guard rather than `hasattr(...,
    "__iter__")` — that attribute is False for SubPattern even though
    `for child in subpattern` works fine.
    """

    if (
        isinstance(subtree, tuple)
        and len(subtree) == 2
        and not isinstance(subtree[0], (list, tuple))
    ):
        # Looks like an `(op, args)` tuple — yield it then descend into args.
        yield subtree
        yield from _walk_subtree(subtree[1])
        return

    # Avoid descending into strings/bytes which iterate per-character.
    if isinstance(subtree, (str, bytes)):
        return

    try:
        iterator = iter(subtree)
    except TypeError:
        return
    for child in iterator:
        yield from _walk_subtree(child)


def _is_empty_matchable_subtree(args: Any) -> bool:
    """Return True if a `MAX_REPEAT`/`MIN_REPEAT` args subtree can match empty
    without consuming input.

    Per spec rule (f), we deliberately only trigger when the **direct**
    immediate content of the repeat is empty-matchable — not when an
    empty-branch BRANCH is nested deeper after literal content. The
    latter case is covered by rule (e) `prefix_subset_alternation`
    (an empty branch is a literal prefix of every other branch).

    Categories that count as "direct empty-matchable":
      - the subpattern is a single naked BRANCH with an empty alternative
      - the subpattern contains an inner ``MAX_REPEAT`` / ``MIN_REPEAT`` /
        ``POSSESSIVE_REPEAT`` with ``min == 0`` (any ``max`` — covers ``?``
        ``*`` ``{0,N}``) — applies whether wrapped in a ``SUBPATTERN`` or
        not. Treating ``min == 0`` as the trigger (rather than only the
        ``?`` shape ``min=0, max=1``) is what classifies ``(a*)+`` as
        ``quantifier_on_empty_matchable`` rather than ``nested_quantifier``;
        the inner ``a*`` can match the empty string, making the outer
        ``+`` repeat over zero-width matches.
      - the entire subpattern is a single ASSERT/ASSERT_NOT (lookaround)
    """

    # args structure: (min, max, subpattern). The subpattern is iterable of
    # (op, args) tuples.
    if not (isinstance(args, tuple) and len(args) == 3):
        return False
    subpattern = args[2]
    items = list(subpattern)
    if not items:
        return False

    # Pure ASSERT — single direct child that's a lookaround.
    if len(items) == 1 and items[0][0] in (_sc.ASSERT, _sc.ASSERT_NOT):
        return True

    # Single naked BRANCH with empty branch — the entire MR subtree is one
    # alternation that admits the empty match.
    if len(items) == 1 and items[0][0] is _sc.BRANCH:
        sub_args = items[0][1]
        if isinstance(sub_args, tuple) and len(sub_args) == 2:
            branches = sub_args[1]
            if any(len(b) == 0 for b in branches):
                return True

    possessive_op = getattr(_sc, "POSSESSIVE_REPEAT", None)
    empty_matchable_ops: tuple[Any, ...] = (_sc.MAX_REPEAT, _sc.MIN_REPEAT)
    if possessive_op is not None:
        empty_matchable_ops = (*empty_matchable_ops, possessive_op)

    # Inner quantifier with ``min == 0`` — direct or wrapped in SUBPATTERN.
    # Covers ``?`` (min=0, max=1), ``*`` (min=0, max=MAXREPEAT), and any
    # ``{0,N}`` bounded form. Each of these admits the empty match for
    # the inner subpattern, so the outer repeat iterates over zero-width
    # matches → catastrophic backtracking.
    for op, sub_args in items:
        if op in empty_matchable_ops and (
            isinstance(sub_args, tuple) and len(sub_args) >= 2 and sub_args[0] == 0
        ):
            return True
        if op is _sc.SUBPATTERN and (isinstance(sub_args, tuple) and len(sub_args) >= 4):
            # SUBPATTERN args: (group, addflags, delflags, subpattern).
            # Walk the immediate subpattern looking for a min=0 quantifier.
            inner = sub_args[3]
            for inner_op, inner_args in inner:
                if inner_op in empty_matchable_ops and (
                    isinstance(inner_args, tuple) and len(inner_args) >= 2 and inner_args[0] == 0
                ):
                    return True
    return False


def _branch_literal_prefix(branch: Any) -> tuple[int, ...]:
    """Return the leading LITERAL byte sequence of a single BRANCH branch.

    Stops at the first non-LITERAL op (or end of branch). Empty branch
    returns `()` — and an empty tuple is trivially a prefix of any other
    tuple, which is exactly what we want for rule (e) detection.
    """

    prefix: list[int] = []
    for op, args in branch:
        if op is _sc.LITERAL:
            prefix.append(int(args))
        else:
            break
    return tuple(prefix)


def _has_prefix_subset_alternation(ast: Any) -> bool:
    """Walk `ast` and return True if any BRANCH node has two branches where
    one's literal prefix is a prefix of (or equal to) the other's literal
    prefix. Empty branch counts as prefix of anything non-empty.
    """

    for op, args in _walk_subtree(ast):
        if op is not _sc.BRANCH:
            continue
        if not (isinstance(args, tuple) and len(args) == 2):
            continue
        branches = args[1]
        prefixes = [_branch_literal_prefix(b) for b in branches]
        n = len(prefixes)
        for i in range(n):
            for j in range(n):
                if i == j:
                    continue
                pi, pj = prefixes[i], prefixes[j]
                # Strict-or-equal prefix relation — empty branch ([]) means
                # `()` which is prefix of every non-empty tuple.
                if len(pi) <= len(pj) and pj[: len(pi)] == pi:
                    return True
    return False


def _detect_redos_pattern(regex: str) -> str | None:
    """Return the first matching ReDoS-pattern tag or None.

    Detection order (matches the tasks.md fixture expectations):
      1. groupref_forbidden — any GROUPREF anywhere
      2. atomic_group_forbidden — any ATOMIC_GROUP anywhere
      3. quantifier_on_assert — MAX_REPEAT/MIN_REPEAT direct child is ASSERT
      4. quantifier_on_empty_matchable — MR subtree can match empty (rule f)
      5. nested_quantifier — MR subtree contains another MR/POSSESSIVE_REPEAT
      6. prefix_subset_alternation — BRANCH with overlapping literal prefixes
    """

    with _warnings.catch_warnings():
        _warnings.filterwarnings("ignore", category=DeprecationWarning)
        ast = _sre_parse.parse(regex)

    # Rule (c) — GROUPREF / GROUPREF_EXISTS / GROUPREF_LOC_IGNORE anywhere.
    for op, _args in _walk_subtree(ast):
        if op is _sc.GROUPREF:
            return "groupref_forbidden"
        # Defensive: some Python versions also expose GROUPREF_EXISTS /
        # GROUPREF_IGNORE — treat all as forbidden.
        op_name = getattr(op, "name", str(op))
        if op_name.startswith("GROUPREF"):
            return "groupref_forbidden"

    # Rule (d) — any ATOMIC_GROUP node.
    atomic_op = getattr(_sc, "ATOMIC_GROUP", None)
    if atomic_op is not None:
        for op, _args in _walk_subtree(ast):
            if op is atomic_op:
                return "atomic_group_forbidden"

    possessive_op = getattr(_sc, "POSSESSIVE_REPEAT", None)
    repeat_ops: tuple[Any, ...] = (_sc.MAX_REPEAT, _sc.MIN_REPEAT)
    if possessive_op is not None:
        repeat_ops = (*repeat_ops, possessive_op)

    # Rules (b), (f), (a) — walk each repeat node.
    for op, args in _walk_subtree(ast):
        if op not in repeat_ops:
            continue
        if not (isinstance(args, tuple) and len(args) == 3):
            continue
        subpattern = args[2]
        items = list(subpattern)

        # (b) Direct child is ASSERT/ASSERT_NOT
        if len(items) == 1 and items[0][0] in (_sc.ASSERT, _sc.ASSERT_NOT):
            return "quantifier_on_assert"

        # (f) Empty-matchable subtree
        if _is_empty_matchable_subtree(args):
            return "quantifier_on_empty_matchable"

        # (a) Nested quantifier — inner MR/MIN_REPEAT/POSSESSIVE_REPEAT
        # anywhere in the subtree.
        for inner_op, _inner_args in _walk_subtree(subpattern):
            if inner_op in repeat_ops:
                return "nested_quantifier"

    # Rule (e) — overlapping literal prefixes in any BRANCH.
    if _has_prefix_subset_alternation(ast):
        return "prefix_subset_alternation"

    return None


# --------------------------------------------------------------------------- #
# CollectSpec / ParseSpec
# --------------------------------------------------------------------------- #


class SamplingWindow(BaseModel):
    """Optional `collect.sampling_window` block.

    Declaring it makes the runner compute a `[now-duration_seconds, now]`
    UTC window and inject `window_start` / `window_end` (journalctl-friendly
    `YYYY-MM-DD HH:MM:SS` strings) plus `window_seconds` (int) into both the
    Jinja2 command-render context and the Finding DSL evaluation context.

    `duration_seconds` is constrained `> 0` (not via prose) so a `0`/negative
    value is rejected at load time — otherwise `window_start == window_end`,
    which makes the rendered command meaningless for time-windowed probes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    duration_seconds: int = Field(gt=0)


class CollectSpec(BaseModel):
    """Command + timeout configuration block of an Inspector manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command: Annotated[str, Field(min_length=1)]
    timeout_seconds: Annotated[int, Field(ge=1, le=300)] = 60
    # Omitting `sampling_window` (None) keeps the pre-delta behaviour exactly:
    # the runner injects no window variables and the render / DSL contexts are
    # byte-identical to before this field existed.
    sampling_window: SamplingWindow | None = None


class ParseSpec(BaseModel):
    """Parser configuration block.

    M1 supports four formats: `raw` / `table` / `json` / `kv`. Cross-field
    rules (`columns` required for `table`, `raw_extract_regex` only on
    `raw`, etc.) are enforced by a `model_validator` so the schema can
    reject inconsistent combinations at load time.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["raw", "table", "json", "kv"]
    columns: list[str] = Field(default_factory=list)
    # `str.split("", maxsplit=1)` raises `ValueError: empty separator`, which
    # would escape `InspectorRunner.run()` and break the "always return
    # InspectorResult" contract. Reject empty delimiters at the schema layer.
    delimiter: Annotated[str, Field(min_length=1)] = "="
    skip_header_rows: Annotated[int, Field(ge=0)] = 1
    raw_extract_regex: str | None = None

    @model_validator(mode="after")
    def _validate_format_constraints(self) -> ParseSpec:
        fmt = self.format
        # ---- columns / raw_extract_regex consistency ----
        if fmt == "table":
            if not self.columns:
                raise ValueError(
                    "table_columns_required: parse.format='table' requires non-empty columns"
                )
        elif fmt == "raw":
            if self.raw_extract_regex is None:
                if self.columns:
                    raise ValueError(
                        "raw_columns_require_regex: parse.columns is only allowed "
                        "when raw_extract_regex is set"
                    )
            else:
                if not self.columns:
                    raise ValueError(
                        "raw_regex_requires_columns: parse.raw_extract_regex requires "
                        "non-empty columns matching the regex's named groups"
                    )
        else:
            # json / kv: columns must be empty
            if self.columns:
                raise ValueError(
                    f"columns_not_allowed_for_format: parse.columns is only valid for "
                    f"format='table' or format='raw' with raw_extract_regex, got format={fmt!r}"
                )

        # ---- non-table format must keep default skip_header_rows ----
        if fmt != "table" and self.skip_header_rows != 1:
            raise ValueError(
                f"skip_header_rows_not_allowed: parse.skip_header_rows is only valid "
                f"for format='table', got format={fmt!r} skip_header_rows={self.skip_header_rows}"
            )

        # ---- non-kv format must keep default delimiter ----
        if fmt != "kv" and self.delimiter != "=":
            raise ValueError(
                f"delimiter_not_allowed: parse.delimiter is only valid for format='kv', "
                f"got format={fmt!r} delimiter={self.delimiter!r}"
            )

        # ---- non-raw format must not carry raw_extract_regex ----
        if fmt != "raw" and self.raw_extract_regex is not None:
            raise ValueError(
                f"raw_extract_regex_not_allowed: parse.raw_extract_regex is only valid "
                f"for format='raw', got format={fmt!r}"
            )

        # ---- raw_extract_regex — four-layer static gate ----
        if self.raw_extract_regex is not None:
            regex = self.raw_extract_regex

            # Layer 1: length cap
            if len(regex) > 200:
                raise ValueError(f"raw_extract_regex_too_long: max 200 chars, got {len(regex)}")

            # Layer 2: re.compile must succeed
            try:
                compiled = re.compile(regex)
            except re.error as exc:
                raise ValueError(f"raw_extract_regex_invalid: re.compile failed: {exc}") from exc

            # Layer 4 BEFORE layer 3 ordering: ReDoS detection runs before the
            # named-group / column-count check so a manifest that combines a
            # ReDoS pattern with an anonymous inner capturing group (e.g.
            # `(?P<x>(a+)+)`) surfaces the ReDoS tag — which is the actual
            # vulnerability — rather than the cosmetic "anonymous group"
            # violation. The 9 tasks.md fixtures rely on this ordering.
            redos_tag = _detect_redos_pattern(regex)
            if redos_tag is not None:
                raise ValueError(f"{redos_tag}: raw_extract_regex matched ReDoS pattern")

            # Layer 3: all groups must be named AND count must equal columns
            named_groups = set(compiled.groupindex)
            total_groups = compiled.groups
            if total_groups != len(named_groups):
                raise ValueError(
                    f"raw_extract_regex_anonymous_groups_forbidden: regex has "
                    f"{total_groups} groups but only {len(named_groups)} are named; "
                    "all capturing groups must use (?P<name>...) form"
                )
            if len(named_groups) != len(self.columns):
                raise ValueError(
                    f"raw_extract_regex_column_count_mismatch: regex has "
                    f"{len(named_groups)} named groups but columns has "
                    f"{len(self.columns)} entries"
                )

        return self


# --------------------------------------------------------------------------- #
# FindingRule
# --------------------------------------------------------------------------- #


_FOR_EACH_PATTERN = re.compile(r"^(.+?)\s+as\s+([a-z_][a-z_0-9]*)$")
_AGGREGATE_VAR_ATTR_PATTERN = re.compile(r"\{([a-z_][a-z_0-9]*)\.")


def _is_compilable_simpleeval(expr: str) -> tuple[bool, str | None]:
    """Return (ok, error_message).

    Two layers gate the expression:

      1. `dsl.validate_ast(expr)` — the same static AST gate the runtime
         evaluator applies. simpleeval would otherwise let constructs like
         `__import__('os')` through at empty-context eval (NameNotDefined
         on `__import__`) only to surface them at runtime; running the
         gate here forces loader-time rejection so the manifest never
         reaches the runner.
      2. `simpleeval.SimpleEval().eval(expr)` against an empty context.
         NameNotDefined / FunctionNotDefined / AttributeDoesNotExist /
         OperatorNotDefined are expected (runtime binds those); SyntaxError
         and FeatureNotAvailable are real compile failures.

    Imported lazily to keep `schema` independent of `dsl` at module-load
    time (avoids any risk of a future bidirectional import).
    """

    # Lazy import: keep `schema` free of an unconditional `dsl` dependency
    # at module-load time so a hypothetical future `dsl → schema` import
    # cannot trigger a cycle.
    from hostlens.inspectors import dsl as _dsl

    try:
        _dsl.validate_ast(expr)
    except simpleeval.FeatureNotAvailable as exc:
        return False, str(exc)

    evaluator = simpleeval.SimpleEval()
    try:
        evaluator.eval(expr)
    except (
        simpleeval.NameNotDefined,
        simpleeval.FunctionNotDefined,
        simpleeval.AttributeDoesNotExist,
        simpleeval.OperatorNotDefined,
    ):
        # Empty-context evaluation legitimately fails on unbound names /
        # functions / attributes / operators because the runtime context
        # has not yet been substituted in. The expression itself parsed
        # cleanly; treat as compilable.
        return True, None
    except simpleeval.InvalidExpression:
        # Other simpleeval business failures at empty context (NumberTooHigh /
        # IterableTooLong / WrongType / generic InvalidExpression) — the
        # expression parsed but hit a semantic limit at validation time. The
        # runtime DSL evaluator will surface the real context's verdict.
        return True, None
    except (SyntaxError, simpleeval.FeatureNotAvailable) as exc:
        return False, str(exc)
    except (TypeError, AttributeError):
        # Stdlib-level fallbacks for evaluator paths that surface a raw
        # TypeError / AttributeError before simpleeval wraps it (e.g. a
        # binary op against an unbound name resolves to a default sentinel
        # whose ``__add__`` raises ``TypeError``). The expression parsed —
        # the runtime evaluator's job, not the static gate's, to reject.
        return True, None
    return True, None


class FindingRule(BaseModel):
    """Single Finding DSL rule.

    Four fields:
      - `for_each`: optional `<iterable_expr> as <var_name>` form
      - `when`: required simpleeval-compatible boolean expression
      - `severity`: required, three-valued
      - `message`: required Python `.format()`-style template
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    for_each: str | None = None
    when: Annotated[str, Field(min_length=1)]
    severity: Literal["info", "warning", "critical"]
    message: Annotated[str, Field(min_length=1)]

    @model_validator(mode="after")
    def _validate_dsl(self) -> FindingRule:
        # ---- for_each must parse and the iterable part must be a valid expr ----
        if self.for_each is not None:
            m = _FOR_EACH_PATTERN.match(self.for_each)
            if m is None:
                raise ValueError(
                    "finding_when_invalid: for_each must be '<expr> as <var_name>' "
                    "where <var_name> matches ^[a-z_][a-z_0-9]*$"
                )
            iterable_expr = m.group(1)
            ok, err = _is_compilable_simpleeval(iterable_expr)
            if not ok:
                raise ValueError(
                    f"finding_when_invalid: for_each iterable expression failed to compile: {err}"
                )

        # ---- when must compile ----
        ok, err = _is_compilable_simpleeval(self.when)
        if not ok:
            raise ValueError(f"finding_when_invalid: when expression failed to compile: {err}")

        # ---- aggregate-mode message must not reference {var.attr} ----
        if self.for_each is None:
            matches = _AGGREGATE_VAR_ATTR_PATTERN.findall(self.message)
            if matches:
                raise ValueError(
                    f"finding_message_invalid_aggregate_ref: aggregate-mode message "
                    f"references {{var.attr}} syntax (var={matches[0]!r}); "
                    "use for_each mode or remove the attribute access"
                )

        return self


# --------------------------------------------------------------------------- #
# InspectorManifest
# --------------------------------------------------------------------------- #


# Path component sentinels disallowed in `requires_files` paths (defense in
# depth: even after the strict allowlist regex, we reject `.` and `..`
# components to ensure paths are already canonicalised).
_FORBIDDEN_PATH_COMPONENTS: frozenset[str] = frozenset({".", ".."})


class InspectorManifest(BaseModel):
    """Top-level Inspector manifest model.

    M1 field set — additions in future milestones (`hook` / `sampling_window`
    / `artifacts`) are intentionally **not** declared here so that
    `extra="forbid"` rejects them at load time.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # ---- identity ----
    name: Annotated[
        str,
        Field(pattern=r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$"),
    ]
    version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]
    description: Annotated[str, Field(min_length=1)]

    # ---- compatibility / preflight ----
    tags: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]*$")]] = Field(default_factory=list)
    targets: Annotated[list[Literal["local", "ssh", "docker"]], Field(min_length=1)]
    requires_capabilities: list[str] = Field(default_factory=list)
    requires_binaries: list[Annotated[str, Field(pattern=r"^[a-zA-Z0-9._-]+$")]] = Field(
        default_factory=list
    )
    requires_files: list[Annotated[str, Field(pattern=r"^/[A-Za-z0-9._/-]+$")]] = Field(
        default_factory=list
    )
    privilege: Literal["none", "sudo", "root"] = "none"

    # ---- parameterisation ----
    parameters: dict[str, Any] | None = None
    secrets: list[Annotated[str, Field(pattern=r"^[A-Z_][A-Z0-9_]*$")]] = Field(
        default_factory=list
    )

    # ---- collection / parsing / findings ----
    collect: CollectSpec
    parse: ParseSpec
    output_schema: dict[str, Any]
    findings: list[FindingRule] = Field(default_factory=list)

    @field_validator("requires_capabilities")
    @classmethod
    def _validate_capabilities(cls, value: list[str]) -> list[str]:
        unknown = [v for v in value if v not in _ALLOWED_CAPABILITIES]
        if unknown:
            allowed = sorted(_ALLOWED_CAPABILITIES)
            raise ValueError(
                f"unknown_capability: requires_capabilities contains values not in "
                f"{allowed}: {unknown}"
            )
        return value

    @field_validator("requires_files")
    @classmethod
    def _validate_requires_files_components(cls, value: list[str]) -> list[str]:
        for path in value:
            # Path field-level regex has already enforced the strict char set;
            # this is the defense-in-depth `.` / `..` component check.
            components = path.split("/")
            for component in components:
                if component in _FORBIDDEN_PATH_COMPONENTS:
                    raise ValueError(
                        f"requires_files_path_component_forbidden: path={path!r} "
                        f"contains component={component!r} (paths must be canonical "
                        "absolute paths with no '.' or '..' components)"
                    )
        return value

    @field_validator("output_schema")
    @classmethod
    def _validate_output_schema_object(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value.get("type") != "object":
            raise ValueError(
                "output_schema_top_level_not_object: output_schema must have "
                "type='object' at the top level"
            )
        return value

    @model_validator(mode="after")
    def _validate_jsonschema_well_formed(self) -> InspectorManifest:
        """Reject manifests whose ``parameters`` / ``output_schema`` are not
        valid JSON Schema documents.

        Without this gate, a manifest with ``output_schema: {type: bogus}``
        would pass Pydantic + the loader and only crash at runtime inside
        ``jsonschema.validate``, raising ``jsonschema.exceptions.SchemaError``
        which is NOT in the runner's narrow ``except`` list — the exception
        would escape ``run()`` as an unhandled error rather than collapse to
        an ``InspectorResult`` status. Doing the well-formedness check at
        manifest load time turns the failure into the standard
        ``manifest_validation_error`` surface.

        Note: an empty schema ``{}`` is a valid JSON Schema (matches every
        instance) and is intentionally accepted here.
        """

        if self.parameters is not None:
            try:
                jsonschema.Draft202012Validator.check_schema(self.parameters)
            except jsonschema.exceptions.SchemaError as exc:
                raise ValueError(
                    f"manifest_validation_error: parameters is not a valid "
                    f"JSON Schema: {exc.message}"
                ) from exc

        try:
            jsonschema.Draft202012Validator.check_schema(self.output_schema)
        except jsonschema.exceptions.SchemaError as exc:
            raise ValueError(
                f"manifest_validation_error: output_schema is not a valid "
                f"JSON Schema: {exc.message}"
            ) from exc

        return self
